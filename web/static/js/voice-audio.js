import { api } from './api.js';
import { $, $$, busy, setText, toast } from './ui.js';

export function startVoiceAudio() {
  const panel = $('[data-voice-controls]');
  if (!panel) return;
  const form = $('[data-wake-form]');
  const sensitivity = $('[data-audio-sensitivity]');
  const input = $('[data-audio-input]');
  const mute = $('[data-audio-action="mute"]');
  let state;
  let dirty = false;
  let sensitivityDirty = false;
  let deviceSignature;
  let timer;

  function render(data) {
    state = data;
    panel.hidden = data.enabled === false;
    if (panel.hidden) return;
    const devices = data.devices;
    setText('[data-audio-backend]', [data.backend, devices.stt, devices.tts].filter(Boolean).join(' / '));
    setText('[data-audio-output]', devices.output || 'Simulated audio output');
    setText('[data-audio-device]', devices.input_name || 'No physical microphone');
    setText('[data-audio-model]', data.model
      ? `${data.model.name} (${data.model.loaded ? 'loaded' : 'not loaded'})` : 'Unavailable');
    setText('[data-audio-mic-test]', data.microphone_test);
    for (const type of ['wake', 'command']) {
      const result = data.recognition[type];
      setText(`[data-audio-${type}-transcript]`, result
        ? `${result.error || result.text || 'No text'} · ${result.latency_ms} ms STT` : 'No recent transcription');
    }
    const capture = data.capture;
    const microphoneControls = $('[data-microphone-controls]');
    if (microphoneControls) microphoneControls.hidden = !capture;
    setText('[data-audio-speech]', capture
      ? `${capture.speech} · RMS ${data.input_level}% · peak ${(capture.peak * 100).toFixed(1)}%${capture.noise !== null ? ` · noise ${capture.noise} / onset ${capture.onset} int16 RMS` : ''}`
      : 'Simulated audio');
    if (capture && sensitivity && !sensitivityDirty) {
      sensitivity.elements.threshold.value = capture.threshold;
      sensitivity.elements.noise_ratio.value = capture.noise_ratio;
    }
    setText('[data-audio-wake]', `${data.detector} · ${data.health.wake?.detail || 'Inactive'}`);
    setText('[data-audio-health]', ['microphone', 'stt', 'audio', 'ai']
      .map((key) => data.health[key]?.detail).filter(Boolean).join(' · '));
    const level = $('[data-audio-level]');
    if (level) level.value = data.input_level;
    if (mute) {
      const muted = data.volume === 0;
      mute.textContent = muted ? 'Unmute' : 'Mute';
      mute.setAttribute('aria-pressed', String(muted));
    }
    if (input && devices.inputs) {
      $('[data-audio-input-field]').hidden = false;
      const signature = JSON.stringify(devices.inputs);
      if (deviceSignature !== signature) {
        input.replaceChildren(new Option('System default', ''), ...devices.inputs.map(
          ({ id, name }) => new Option(name, String(id))));
        deviceSignature = signature;
      }
      input.value = String(devices.selected_input ?? '');
      if (input.selectedIndex < 0) {
        input.add(new Option(String(devices.selected_input), String(devices.selected_input)));
        input.value = String(devices.selected_input);
      }
    }
    if (form && !dirty) {
      form.elements.phrase.value = data.wake.phrase;
      form.elements.enabled.checked = data.wake.enabled;
      form.elements.in_sleep.checked = data.wake.in_sleep;
    }
  }

  async function refresh() {
    clearTimeout(timer);
    try { render(await api.get('/audio')); }
    catch (error) { setText('[data-audio-wake]', error.message); }
    if (!document.hidden && state?.enabled !== false) timer = setTimeout(refresh, 800);
  }

  form?.addEventListener('input', () => { dirty = true; });
  sensitivity?.addEventListener('input', () => { sensitivityDirty = true; });
  sensitivity?.addEventListener('submit', (event) => {
    event.preventDefault();
    busy($('button[type=submit]', sensitivity), async () => {
      await api.put('/audio/sensitivity', { threshold: Number(sensitivity.elements.threshold.value),
        noise_ratio: Number(sensitivity.elements.noise_ratio.value) });
      sensitivityDirty = false;
      await refresh();
    });
  });
  form?.addEventListener('submit', (event) => {
    event.preventDefault();
    busy($('button[type=submit]', form), async () => {
      await api.put('/audio/wake', { phrase: form.elements.phrase.value,
        enabled: form.elements.enabled.checked, in_sleep: form.elements.in_sleep.checked });
      dirty = false;
      await refresh();
    });
  });
  input?.addEventListener('change', async () => {
    try {
      await api.put('/audio/input', { device: input.value === '' ? null : Number(input.value) });
      toast('Microphone selected for subsequent capture');
    } catch (error) { toast(error.message, 'error'); }
    await refresh();
  });
  $$('[data-audio-action]').forEach((button) => button.addEventListener('click', () => {
    busy(button, async () => {
      const action = button.dataset.audioAction;
      if (action === 'mute') await api.put('/audio/mute', { muted: state.volume !== 0 });
      else {
        await api.post(`/audio/${action}`, {});
        toast({ stop: 'Stop requested', test: 'Speaker test queued',
          'microphone-test': 'Microphone test queued' }[action]);
      }
      await refresh();
    });
  }));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  refresh();
}
