import { api } from '../api.js';
import { startShell } from '../shell.js';
import { $, $$, fillStatusRow, formatDuration, setBadge, setText } from '../ui.js';

const REFRESH_MS = 3000;
// Thresholds for meter colours: [warn, hot]; temperature scaled to 0-85 °C.
const LEVELS = { cpu_percent: [70, 90], memory_percent: [75, 90], temperature: [65, 78] };

function renderMetric(name, metric) {
  const card = $(`[data-metric="${name}"]`);
  if (!card) return;
  card.dataset.avail = metric.availability;
  setBadge($('[data-badge]', card), metric.availability);
  const value = metric.value;
  const fill = $('[data-fill]', card);
  if (value === null || value === undefined) {
    setText($('[data-value]', card), 'Unavailable');
    if (fill) fill.style.width = '0';
    return;
  }
  const text = name === 'uptime' ? formatDuration(value) : `${value}${metric.unit === '%' ? '%' : ` ${metric.unit}`}`;
  setText($('[data-value]', card), text);
  if (fill) {
    const pct = name === 'temperature' ? Math.min(100, (value / 85) * 100) : value;
    const [warn, hot] = LEVELS[name] || [101, 101];
    fill.style.width = `${pct}%`;
    fill.classList.toggle('is-warn', value >= warn && value < hot);
    fill.classList.toggle('is-hot', value >= hot);
  }
}

async function load() {
  try {
    const d = await api.get('/diagnostics');
    const h = d.host;
    setText('[data-field="platform"]', `· ${h.platform}${h.cpu_count ? `, ${h.cpu_count} cores` : ''}`);
    for (const name of ['cpu_percent', 'memory_percent', 'temperature', 'uptime']) renderMetric(name, h[name]);
    if (h.memory) setText($('[data-metric="memory_percent"] .metric__label'), `RAM · ${h.memory.used_mb}/${h.memory.total_mb} MB`);
    fillStatusRow('esp32', d.esp32);
    fillStatusRow('service', d.service, `${d.service.backend} backend · up ${formatDuration(d.service.uptime_s)}`);
    const { capabilities, latest_error: failure } = d.controller;
    for (const name of ['movement', 'safety_sensors']) {
      fillStatusRow(name, { availability: capabilities[name] });
    }
    const controllerError = $('[data-field="controller-error"]');
    controllerError.hidden = !failure;
    controllerError.textContent = failure ? `Last controller error · ${failure.message}` : '';
    for (const [name, info] of Object.entries(d.sensors)) fillStatusRow(name, info);
    setText('[data-field="refreshed"]', `Updated ${new Date().toLocaleTimeString()}`);
  } catch (err) {
    setText('[data-field="refreshed"]', `Update failed: ${err.message}`);
  }
}

let timer;
function loop() { clearTimeout(timer); if (!document.hidden) timer = setTimeout(async () => { await load(); loop(); }, REFRESH_MS); }
document.addEventListener('visibilitychange', () => { if (!document.hidden) load().then(loop); });
// CPU % needs two samples, so the first reading may be unavailable for a moment.
load().then(loop);
$$('.metric').forEach((m) => m.setAttribute('aria-live', 'off'));
startShell();
