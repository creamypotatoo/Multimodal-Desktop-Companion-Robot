import { api } from '../api.js';
import { onStatus, refresh, startShell, updateMode } from '../shell.js';
import { $, $$, bindRange, busy, capitalize, setBadge, setText, toast } from '../ui.js';
import { startMotorLink } from '../motor-link.js';

const gate = $('[data-mode-gate]');
const readout = $('[data-field="motion"]');
const moveButtons = $$('[data-move]');
const servoInputs = $$('[data-servo]');
let enabled = false;
let readoutTimer = null;
let mode;
let physical = false;

const speed = bindRange($('[data-input="speed"]'), (value) =>
  api.put('/movement/speed', { speed: value }).then(render).catch((e) => toast(e.message, 'error')),
{ format: (v) => `${v}%` });

const servos = Object.fromEntries(servoInputs.map((input) => [input.dataset.servo, bindRange(input, (angle) =>
  api.put('/movement/servo', { servo: input.dataset.servo, angle }).then(render)
    .catch((e) => toast(e.message, 'error')), { format: (v) => `${v}°` })]));

function showMotion(m) {
  clearTimeout(readoutTimer);
  if (physical) {
    const report = m.serial.telemetry;
    readout.classList.toggle('is-moving', report?.state === 'moving');
    readout.textContent = report ? `Commanded ${report.state}${report.direction === 'none' ? '' : ` · ${report.direction}`}`
      : m.serial.command_pending ? 'Command pending' : 'Commanded state unavailable';
    return;
  }
  readout.classList.toggle('is-moving', m.moving);
  readout.textContent = m.moving ? `Moving ${m.direction} (simulated)`
    : m.moving === null ? 'Motor stop unconfirmed' : 'Stopped';
  if (m.moving) readoutTimer = setTimeout(() => showMotion({ moving: false }), m.remaining_ms);
}

function render(m) {
  physical = m.motor_backend === 'esp32';
  enabled = m.enabled;
  gate.hidden = mode === 'movement';
  const driveBadge = $('[data-movement-backend]');
  setBadge(driveBadge, m.availability);
  driveBadge.textContent = physical ? 'ESP32 serial' : 'Simulated drive';
  setBadge($('[data-servo-backend]'), m.servo_availability);
  setText('[data-speed-hint]', physical ? '250 ms per tap by default. Use low speed for bench checks.'
    : 'Each press sends a 0.5 s simulated pulse.');
  setText('[data-motor-keys]', physical ? 'Use the direction buttons. Space requests STOP.'
    : 'Keyboard: arrow keys or W/A/S/D to move, Space to stop.');
  moveButtons.forEach((b) => { b.disabled = !enabled || m.speed === 0; });
  servoInputs.forEach((i) => { i.disabled = !enabled || physical; });
  speed.set(m.speed);
  for (const [name, info] of Object.entries(m.servos)) servos[name]?.set(info.angle);
  $('[data-field="permission"]').textContent = m.safety.blocked_reason
    || (m.speed === 0 ? 'Raise motor speed to drive' : 'Movement permitted');
  $('[data-field="estop"]').textContent = m.safety.emergency_stop ? 'Emergency stop latched'
    : m.safety.fault || 'Emergency stop clear';
  setText('[data-field="safety-sensors"]', physical
    ? 'Obstacle and cliff sensors unavailable. Manual floor operation only.'
    : `Safety sensors · ${m.safety.availability}`);
  $('[data-action="reset-safety"]').disabled = (!m.safety.emergency_stop && !m.safety.fault)
    || (physical && (m.serial.connection !== 'connected' || m.serial.reset_pending));
  $('[data-action="center"]').disabled = !enabled || physical;
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
    toast(physical ? 'STOP requested' : 'Stopped');
  } catch (err) {
    toast(err.message, 'error');
  }
}

moveButtons.forEach((b) => b.addEventListener('click', () => move(b.dataset.move, b)));
$$('[data-action="stop"]').forEach((b) => b.addEventListener('click', stop));
$('[data-action="emergency-stop"]').addEventListener('click', (e) => busy(e.currentTarget, async () => {
  try {
    render(await api.post('/movement/emergency-stop'));
    toast(physical ? 'Emergency stop requested; host latch set' : 'Emergency stop latched');
  } finally {
    await refresh();
  }
}));
$('[data-action="reset-safety"]').addEventListener('click', async (e) => {
  await busy(e.currentTarget, async () => render(await api.post('/movement/reset-safety')));
  await refresh();
});
$('[data-action="center"]').addEventListener('click', (e) => busy(e.currentTarget, async () => {
  for (const name of Object.keys(servos)) render(await api.put('/movement/servo', { servo: name, angle: 90 }));
}));
$('[data-action="movement-mode"]').addEventListener('click', (e) => busy(e.currentTarget, async () => {
  const data = await api.put('/mode', { mode: 'movement' });
  mode = data.mode;
  updateMode(data.mode);
  toast(`${capitalize(data.mode)} mode`);
  render(await api.get('/movement'));
  refresh();
}));

const KEYS = { ArrowUp: 'forward', w: 'forward', ArrowDown: 'backward', s: 'backward',
  ArrowLeft: 'left', a: 'left', ArrowRight: 'right', d: 'right' };
document.addEventListener('keydown', (e) => {
  if (e.target.closest('input, textarea, select') || e.repeat || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === ' ') { e.preventDefault(); stop(); return; }
  if (physical) return;
  const dir = KEYS[e.key.length === 1 ? e.key.toLowerCase() : e.key];
  if (dir) { e.preventDefault(); move(dir, $(`[data-move="${dir}"]`)); }
});

onStatus((s) => {
  mode = s.mode;
  render(s.movement);
});
startShell();
startMotorLink(render);
