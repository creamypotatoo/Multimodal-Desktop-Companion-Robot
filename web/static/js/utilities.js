import { api } from './api.js';
import { $, toast } from './ui.js';

export const PHASES = { focus: 'Focus', short_break: 'Short break', long_break: 'Long break' };
export const STATES = { idle: 'Idle', running: 'Running', paused: 'Paused', completed: 'Completed' };
export const NAMES = { timer: 'Timer', stopwatch: 'Stopwatch', pomodoro: 'Pomodoro' };
const pad = (n) => String(n).padStart(2, '0');

export function formatTime(seconds, stopwatch = false) {
  const whole = stopwatch ? Math.floor(seconds) : Math.ceil(seconds);
  const hours = Math.floor(whole / 3600);
  const time = `${hours ? pad(hours) + ':' : ''}${pad(Math.floor(whole / 60) % 60)}:${pad(whole % 60)}`;
  return stopwatch ? `${time}.${pad(Math.floor(seconds * 100) % 100)}` : time;
}

let audio;
let cursor;
try { cursor = JSON.parse(sessionStorage.getItem('eli.utility.cursor')); } catch { /* optional */ }

function announce(data) {
  if (cursor?.stream === data.stream) {
    for (const event of data.events.filter((item) => item.id > cursor.id)) {
      const message = event.kind === 'timer_done' ? 'Countdown complete.'
        : `${PHASES[event.phase]} complete. Next phase: ${PHASES[event.next_phase]}.`;
      toast(message, 'info', 6500);
      if (globalThis.Notification?.permission === 'granted') {
        try { new Notification('Eli', { body: message, tag: `eli-${data.stream}-${event.id}` }); }
        catch { /* some browsers only support service-worker notifications */ }
      }
      if (audio?.state === 'running') {
        const tone = audio.createOscillator();
        const gain = audio.createGain();
        const now = audio.currentTime;
        tone.frequency.value = 660;
        gain.gain.setValueAtTime(0.12, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.4);
        tone.connect(gain).connect(audio.destination);
        tone.start(now);
        tone.stop(now + 0.4);
      }
    }
  }
  cursor = { stream: data.stream, id: data.cursor };
  try { sessionStorage.setItem('eli.utility.cursor', JSON.stringify(cursor)); } catch { /* optional */ }
}

export function bindNotifications() {
  const button = $('[data-utility-notifications]');
  if (button && globalThis.isSecureContext && globalThis.Notification) {
    const render = () => {
      button.disabled = Notification.permission !== 'default';
      button.textContent = ({ granted: 'Browser notifications enabled', denied: 'Browser notifications blocked' })[Notification.permission]
        ?? 'Enable browser notifications';
    };
    button.hidden = false;
    render();
    button.addEventListener('click', async () => {
      try { await Notification.requestPermission(); }
      catch { toast('Browser notification permission is unavailable.', 'error'); }
      render();
    });
  }
  const sound = $('[data-utility-audio]');
  if (sound && globalThis.AudioContext) {
    sound.hidden = false;
    sound.addEventListener('click', async () => {
      sound.disabled = true;
      try {
        if (audio?.state === 'running') await audio.suspend();
        else {
          audio ??= new AudioContext();
          await audio.resume();
        }
      } catch { toast('Completion sound is unavailable.', 'error'); }
      finally {
        const enabled = audio?.state === 'running';
        sound.setAttribute('aria-pressed', String(enabled));
        sound.textContent = `Completion sound ${enabled ? 'on' : 'off'}`;
        sound.disabled = false;
      }
    });
  }
}

export function watchUtilities({ update = () => {}, paint = () => {}, connection = () => {} } = {}) {
  let data;
  let received;
  let pending = false;
  let reading;
  let poll;
  let frame;
  let nextPaint = 0;

  function animate(now) {
    frame = undefined;
    if (data && now >= nextPaint) {
      paint(data, Math.max(0, now - received) / 1000);
      nextPaint = now + 40;
    }
    if (!document.hidden && data && Object.keys(NAMES).some((key) => data[key].state === 'running')) {
      frame = requestAnimationFrame(animate);
    }
  }

  function schedule() {
    clearTimeout(poll);
    if (!document.hidden) poll = setTimeout(refresh, 2000);
  }

  async function run(load, action = false) {
    if (pending) return false;
    pending = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const fresh = await load(controller.signal);
      received = performance.now();
      data = fresh;
      update(data);
      connection('');
      announce(data);
      if (frame !== undefined) cancelAnimationFrame(frame);
      nextPaint = 0;
      animate(received);
      return true;
    } catch (error) {
      if (action) toast(error.message, 'error');
      if (!error.status) connection(data ? 'Connection lost. Showing the last synchronized state.'
        : 'Cannot reach Eli. Retrying…');
      return false;
    } finally {
      clearTimeout(timeout);
      pending = false;
      schedule();
    }
  }

  function refresh() {
    if (pending) return;
    reading = run((signal) => api.get('/utilities', signal));
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(poll);
      if (frame !== undefined) cancelAnimationFrame(frame);
    } else refresh();
  });
  window.addEventListener('online', refresh);
  window.addEventListener('pageshow', (event) => { if (event.persisted) refresh(); });
  refresh();
  return async (method, path, body) => {
    await reading;
    const success = await run((signal) => api[method](`/utilities/${path}`, body, signal), true);
    if (!success) refresh();
    return success;
  };
}

export function visualTime(model, delta) {
  const elapsed = model.state === 'running' ? delta : 0;
  return 'elapsed' in model ? model.elapsed + elapsed : Math.max(0, model.remaining - elapsed);
}
