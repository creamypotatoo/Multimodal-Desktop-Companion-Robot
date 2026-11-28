import { api } from '../api.js';
import { mountEyes } from '../eyes.js';
import { onStatus, startShell } from '../shell.js';
import { $, setBadge, setText, toast } from '../ui.js';

const eyes = mountEyes();
const form = $('[data-camera-form]');
const image = $('[data-camera-preview]');
const stop = $('[data-camera-stop]');
const submit = $('button[type=submit]', form);
const discover = $('[data-camera-discover]');
let shown = false;
let frameTimer;
let frameNumber = 0;
let initialized = false;
let saving = false;
let refreshing = false;
let revision = 0;
let gaze;
let expression = 'idle';
let previewFps = 8;

function updateGaze() {
  eyes?.setGaze(['idle', 'listening'].includes(expression) ? gaze : null);
}

onStatus((status) => {
  expression = status.expression;
  eyes?.setExpression(expression);
  updateGaze();
});

function nextFrame() {
  if (shown && !document.hidden) image.src = `/api/v1/perception/frame?sample=${++frameNumber}`;
}

function pausePreview() {
  shown = false;
  clearTimeout(frameTimer);
  image.removeAttribute('src');
  image.hidden = true;
  $('[data-camera-placeholder]').hidden = false;
}

image.addEventListener('load', () => {
  if (!shown) return;
  image.hidden = false;
  $('[data-camera-placeholder]').hidden = true;
  frameTimer = setTimeout(nextFrame, 1000 / previewFps);
});
image.addEventListener('error', () => {
  if (!shown) return;
  image.hidden = true;
  $('[data-camera-placeholder]').hidden = false;
  frameTimer = setTimeout(nextFrame, 500);
});

function render({ camera, recognition }) {
  form.hidden = !camera.available;
  if (camera.available && !initialized) {
    for (const [key, value] of Object.entries(camera.requested)) form.elements[key].value = value;
    initialized = true;
  }
  const active = camera.state === 'active';
  submit.disabled = saving || ['active', 'starting', 'stopping', 'discovering'].includes(camera.state);
  discover.disabled = saving || submit.disabled;
  stop.disabled = saving || !['active', 'starting'].includes(camera.state);
  setBadge($('[data-camera-state]'), active ? (camera.simulated ? 'simulated' : 'live') : 'unavailable');
  setText('[data-camera-state]', active && camera.simulated ? 'Simulated camera' : camera.state ?? 'Unavailable');
  setText('[data-camera-detail]', camera.detail);
  const settings = camera.settings;
  previewFps = camera.preview_fps ?? 8;
  setText('[data-camera-format]', settings
    ? `${settings.frame_width} × ${settings.frame_height} · ${settings.fps || 'Unknown'} FPS reported by driver · Preview up to ${previewFps} FPS` : '');
  const performance = camera.performance ?? {};
  const metrics = [
    performance.fps == null ? '' : `${performance.fps} FPS measured`,
    performance.analysis_ms == null ? '' : `${performance.analysis_ms} ms face processing`,
    performance.frame_ms == null ? '' : `${performance.frame_ms} ms capture to JPEG`,
  ];
  setText('[data-camera-performance]', metrics.filter(Boolean).join(' · '));
  if (!camera.preview || document.hidden) pausePreview();
  else if (!shown) {
    shown = true;
    nextFrame();
  }
  const count = recognition.faces?.length ?? 0;
  setText('[data-face-detection]', recognition.detection ? `${count} face${count === 1 ? '' : 's'}` : 'Unavailable');
  const labels = { unavailable: 'Unavailable', multiple_faces: 'Multiple faces', no_face: 'No face',
    confirming: 'Confirming', unknown: 'Unknown' };
  setText('[data-face-identity]', recognition.identity ?? labels[recognition.identity_status] ?? 'Unavailable');
  setText('[data-face-distance]', recognition.distance == null ? 'Unavailable' : recognition.distance.toFixed(1));
  $('[data-face-expression-row]').hidden = !recognition.expression;
  setText('[data-face-expression]', recognition.expression ?? '');
  gaze = recognition.gaze;
  updateGaze();
}

async function refresh() {
  if (document.hidden || saving || refreshing) return;
  refreshing = true;
  const started = revision;
  try {
    const data = await api.get('/perception');
    if (started === revision && !document.hidden) render(data);
  }
  catch (error) { pausePreview(); setText('[data-camera-detail]', error.message); }
  finally { refreshing = false; }
}

async function control(action, options = {}) {
  saving = true;
  revision++;
  submit.disabled = stop.disabled = true;
  if (action === 'stop') pausePreview();
  try { await api.post('/perception/camera', { action, ...options }); }
  catch (error) { toast(error.message, 'error'); }
  finally { saving = false; await refresh(); }
}

form.addEventListener('submit', (event) => {
  event.preventDefault();
  const { device, width, height, fps } = form.elements;
  control('start', { device: device.value.trim(), width: +width.value, height: +height.value, fps: +fps.value });
});
stop.addEventListener('click', () => control('stop'));
discover.addEventListener('click', async () => {
  try {
    discover.disabled = submit.disabled = true;
    let result = await api.post('/perception/devices');
    while (result.scanning && !document.hidden) {
      await new Promise((resolve) => setTimeout(resolve, 350));
      result = await api.get('/perception/devices');
    }
    if (result.scanning) return;
    if (result.error) throw new Error(result.error);
    const { devices } = result;
    $('#camera-devices').replaceChildren(...devices.map(({ device, label }) => new Option(label, device)));
    if (devices.length) form.elements.device.focus();
    else toast('No camera devices found');
  } catch (error) { toast(error.message, 'error'); }
  finally { refresh(); }
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) pausePreview();
  else refresh();
});
refresh();
setInterval(refresh, 450);
startShell();
