"""Phase 5 capture and isolated playback. Both are owned by one runtime worker."""
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import signal
import subprocess
import sys
from tempfile import mkstemp, TemporaryFile
from threading import Event, Lock, Thread
from time import monotonic


class CaptureCancelled(Exception):
    pass


class PlaybackNotStopped(RuntimeError):
    def __init__(self, message, process):
        super().__init__(message)
        self.process = process


def prepare_pcm(samples, max_gain=1.0):
    """Convert int16 to Whisper PCM. Boost only gated audio with a measurable signal."""
    import numpy as np

    pcm = np.asarray(samples, dtype=np.float32).reshape(-1) / np.float32(32768)
    if pcm.size and max_gain > 1:
        peak = float(np.max(np.abs(pcm)))
        rms = float(np.sqrt(np.mean(pcm * pcm)))
        if rms >= 0.002 and peak > 0:
            pcm *= min(max_gain, max(1.0, 0.7 / peak))
    return np.ascontiguousarray(pcm)


class Microphone:
    def __init__(self, *, device=None, rate=16000, input_module=None, pcm=False, max_gain=1):
        from audio.speech_to_text import SAMPLE_RATE, VadSettings
        if not 1 <= max_gain <= 8:
            raise ValueError('ELI_AUDIO_GAIN_MAX must be from 1 to 8')
        self.device = device
        self.rate = rate
        self.output_rate = SAMPLE_RATE
        if not 8000 <= rate <= 48000:
            raise ValueError('Sample rate must be from 8000 to 48000 Hz')
        if input_module is None:
            import sounddevice as input_module
        self.driver = input_module
        self._owner = Lock()
        self.level = 0
        self.peak = 0.0
        self.speech = 'idle'
        self.noise = self.onset = None
        self.pcm = pcm
        self.max_gain = max_gain
        self.settings = VadSettings.from_env()
        self._baseline_device = self.device
        self._baseline = None

    def configure(self, threshold, noise_ratio):
        from dataclasses import replace
        self.settings = replace(self.settings, threshold=threshold, noise_ratio=noise_ratio)

    def _status(self, speech, noise, onset):
        self.speech = speech
        self.noise = round(noise, 1)
        self.onset = round(onset, 1)

    def devices(self):
        return [{'id': i, 'name': item['name']} for i, item in enumerate(self.driver.query_devices())
                if item['max_input_channels'] > 0]

    def capture(self, current, *, wake=False, timeout=None, test=False):
        from dataclasses import replace
        import numpy as np
        from audio.speech_to_text import capture_utterance, chunk_rms, write_wav

        if not self._owner.acquire(blocking=False):
            raise RuntimeError('Microphone is already owned')
        size = int(self.rate * 0.03)
        chunks = Queue(maxsize=8)
        saw_speech = False
        settings = self.settings
        if wake:
            settings = replace(settings, start_timeout=3, silence=0.55, max_duration=4)
        elif timeout is not None:
            settings = replace(settings, start_timeout=min(settings.start_timeout, timeout))
        settings = replace(settings, max_duration=min(settings.max_duration, 60),
                           start_timeout=min(settings.start_timeout, 60))

        def accept(data, frames, timestamp, status):
            try:
                chunks.put_nowait(data.copy())
            except Full:
                pass

        def observe(speech, noise, onset):
            nonlocal saw_speech
            saw_speech = saw_speech or speech == 'speech'
            self._status(speech, noise, onset)

        try:
            if not current():
                return None
            self.speech = 'calibrating'
            self.peak = 0.0
            device = self.device
            if device != self._baseline_device or test:
                self._baseline, self._baseline_device = None, device
                self.noise = self.onset = None
            with self.driver.InputStream(device=device, samplerate=self.rate, blocksize=size,
                                         channels=1, dtype='int16', callback=accept) as stream:
                def read():
                    deadline = monotonic() + 1.0
                    while current():
                        try:
                            data = chunks.get(timeout=0.08)
                        except Empty:
                            if not stream.active or monotonic() >= deadline:
                                raise RuntimeError('Microphone stream disconnected')
                        else:
                            self.level = round(chunk_rms(data) * 100 / 32768, 2)
                            self.peak = max(self.peak, float(np.max(np.abs(data.astype(np.float32)))) / 32768)
                            return data
                    raise CaptureCancelled

                audio = capture_utterance(read, settings, block_seconds=size / self.rate,
                                          on_status=observe, initial_noise=self._baseline)
            self._baseline = self.noise
            if audio is None or not current():
                return True if test and saw_speech else None
            self.speech = 'utterance captured'
            if test:
                return True
            if self.rate != self.output_rate:
                positions = np.arange(round(len(audio) * self.output_rate / self.rate))
                audio = np.interp(positions * self.rate / self.output_rate,
                                  np.arange(len(audio)), audio).astype(np.int16)
            if self.pcm:
                return prepare_pcm(audio, self.max_gain)
            fd, filename = mkstemp(prefix='eli-voice-', suffix='.wav')
            os.close(fd)
            try:
                write_wav(filename, audio)
            except Exception:
                Path(filename).unlink(missing_ok=True)
                raise
            return filename
        except CaptureCancelled:
            self.speech = 'cancelled'
            return True if test and saw_speech else None
        except Exception:
            self.speech = 'input error'
            raise
        finally:
            self.level = 0
            self._owner.release()


class OfflineWhisper:
    """Load only an explicitly supplied faster-whisper model directory."""
    def __init__(self, path, *, prompt='Eli is a personal robot assistant.', beam_size=5):
        self.path = Path(path)
        if not all((self.path / name).is_file() for name in ('model.bin', 'tokenizer.json', 'config.json')):
            raise ValueError('Set ELI_WHISPER_MODEL to a local faster-whisper directory with model.bin, tokenizer.json and config.json')
        self.model = None
        if not 1 <= beam_size <= 10 or len(prompt) > 500:
            raise ValueError('Whisper beam size must be 1 to 10 and prompt at most 500 characters')
        self.beam_size = beam_size
        self.prompt = prompt

    def __call__(self, recording, *, timeout=None):
        import numpy as np
        if not isinstance(recording, np.ndarray) or recording.dtype != np.float32 or recording.ndim != 1:
            raise ValueError('Windows Whisper requires mono 16 kHz float32 PCM')
        if self.model is None:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(str(self.path), device='cpu', compute_type='int8',
                                      local_files_only=True)
        segments, _ = self.model.transcribe(recording, language='en', beam_size=self.beam_size,
                                            initial_prompt=self.prompt or None,
                                            condition_on_previous_text=False)
        return ' '.join(segment.text for segment in segments).strip()


_PLAYER = """
import json, sys
from audio.text_to_speech import TTSConfig, speak
config, text = json.load(sys.stdin)
speak(text, TTSConfig(**config), should_play=lambda: True,
      on_start=lambda: print('PLAYING', flush=True))
"""


def isolated_player(config, text, volume, current, on_start, *, launch=subprocess.Popen):
    if not current():
        return False
    payload = json.dumps([dict(backend=config.backend, device=config.device, volume=volume / 100),
                          text[:4000]])
    options = ({'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32'
               else {'start_new_session': True})
    with TemporaryFile() as arguments:
        arguments.write(payload.encode('ascii'))
        arguments.seek(0)
        process = launch([sys.executable, '-c', _PLAYER], stdin=arguments,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **options)
    playing = Event()

    def read_start():
        for line in iter(lambda: process.stdout.readline(64), b''):
            if line.strip() == b'PLAYING':
                playing.set()

    signal_reader = Thread(target=read_start, name='eli-playback-start', daemon=True)
    signal_reader.start()
    deadline = monotonic() + 60
    signalled = exited = False
    try:
        while current() and monotonic() < deadline:
            if not signalled and playing.is_set() and on_start:
                on_start()
                signalled = True
            try:
                code = process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                continue
            exited = True
            if code:
                raise RuntimeError('Speech backend exited unsuccessfully')
            return current()
        if current():
            raise TimeoutError('Speech backend exceeded its playback limit')
        return False
    finally:
        if not exited:
            try:
                if sys.platform == 'win32':
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                if process.poll() is None:
                    raise PlaybackNotStopped('Playback could not be stopped', process) from None
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise PlaybackNotStopped('Playback exit could not be confirmed', process) from None
        signal_reader.join(timeout=0.1)
        if not signal_reader.is_alive():
            process.stdout.close()
