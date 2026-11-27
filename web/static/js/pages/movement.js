import { api } from '../api.js';
import { onStatus, refresh, startShell, updateMode } from '../shell.js';
import { $, $$, bindRange, busy, capitalize, toast } from '../ui.js';

const gate = $('[data-mode-gate]');
const readout = $('[data-field="motion"]');
const moveButtons = $$('[data-move]');
const servoInputs = $$('[data-servo]');
let enabled = false;
let readoutTimer = null;

const speed = bindRange($('[data-input="speed"]'), (value) =>
  api.put('/movement/speed', { speed: value }).then(render).catch((e) => toast(e.message, 'error')),
{ format: (v) => `${v}%` });

const servos = Object.fromEntries(servoInputs.map((input) => [input.dataset.servo, bindRange(input, (angle) =>
  api.put('/movement/servo', { servo: input.dataset.servo, angle }).then(render)
    .catch((e) => toast(e.message, 'error')), { format: (v) => `${v}°` })]));

function showMotion(m) {
  clearTimeout(readoutTimer);
  readout.classList.toggle('is-moving', m.moving);
  readout.textContent = m.moving ? `Moving ${m.direction} (simulated)` : 'Stopped';
  if (m.moving) readoutTimer = setTimeout(() => showMotion({ moving: false }), m.remaining_ms);
}

function render(m) {
  enabled = m.enabled;
  gate.hidden = enabled;
  moveButtons.forEach((b) => { b.disabled = !enabled; });
  servoInputs.forEach((i) => { i.disabled = !enabled; });
  speed.set(m.speed);
  for (const [name, info] of Object.entries(m.servos)) servos[name]?.set(info.angle);
  showMotion(m);
}

async function move(direction, button) {
  if (!enabled) return;
  button?.classList.add('is-pressed');
  setTimeout(() => button?.classList.remove('is-pressed'), 160);
  try {
    render(await api.post('/movement/move', { direction }));
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function stop() {
  try {
    render(await api.post('/movement/stop'));
    toast('Stopped');
  } catch (err) {
    toast(err.message, 'error');
  }
}

moveButtons.forEach((b) => b.addEventListener('click', () => move(b.dataset.move, b)));
$$('[data-action="stop"]').forEach((b) => b.addEventListener('click', stop));
$('[data-action="center"]').addEventListener('click', (e) => busy(e.currentTarget, async () => {
  for (const name of Object.keys(servos)) render(await api.put('/movement/servo', { servo: name, angle: 90 }));
}));
$('[data-action="manual"]').addEventListener('click', (e) => busy(e.currentTarget, async () => {
  const data = await api.put('/mode', { mode: 'manual' });
  updateMode(data.mode);
  toast(`${capitalize(data.mode)} mode (simulated)`);
  render(await api.get('/movement'));
  refresh();
}));

const KEYS = { ArrowUp: 'forward', w: 'forward', ArrowDown: 'backward', s: 'backward',
  ArrowLeft: 'left', a: 'left', ArrowRight: 'right', d: 'right' };
document.addEventListener('keydown', (e) => {
  if (e.target.closest('input, textarea, select') || e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === ' ') { e.preventDefault(); stop(); return; }
  const dir = KEYS[e.key.length === 1 ? e.key.toLowerCase() : e.key];
  if (dir) { e.preventDefault(); move(dir, $(`[data-move="${dir}"]`)); }
});

onStatus((s) => {
  if ((s.mode === 'manual') !== enabled) api.get('/movement').then(render).catch(() => {});
});
api.get('/movement').then(render).catch((e) => toast(e.message, 'error'));
startShell();
