import { api } from '../api.js';
import { mountEyes } from '../eyes.js';
import { onStatus, startShell } from '../shell.js';
import { $, formatDuration, setText } from '../ui.js';
import { formatTime, NAMES, PHASES, STATES, visualTime, watchUtilities } from '../utilities.js';

const eyes = mountEyes();
api.get('/settings').then((s) => { eyes?.setColor(s.eye_color); eyes?.setSpeed(s.animation_speed); }).catch(() => {});

function describe(entry) {
  const { command, at, ...rest } = entry;
  const details = Object.entries(rest).map(([k, v]) => `${k.replace('_', ' ')}: ${v}`).join(', ');
  return details ? `${command} · ${details}` : command;
}

function renderHistory(history) {
  const list = $('[data-activity]');
  if (!history.length) return;
  list.replaceChildren(...history.slice(0, 8).map((entry) => {
    const li = document.createElement('li');
    const text = document.createElement('span');
    text.textContent = describe(entry);
    const time = document.createElement('time');
    const date = new Date(entry.at * 1000);
    time.dateTime = date.toISOString();
    time.textContent = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    li.append(text, time);
    return li;
  }));
}

onStatus((s) => {
  eyes?.setExpression(s.expression);
  setText('[data-field="state-label"]', s.mode === 'sleep' ? 'asleep' : s.state?.toLowerCase() || 'idle');
  setText('[data-field="mode"]', s.mode);
  setText('[data-field="expression"]', s.expression);
  setText('[data-field="volume"]', s.volume);
  setText('[data-field="uptime"]', formatDuration(s.uptime_s));
  renderHistory(s.history);
});
startShell();

const activeUtilities = $('.active-utilities');
watchUtilities({
  update(data) {
    let any = false;
    for (const [name, label] of Object.entries(NAMES)) {
      const row = $(`[data-active-utility="${name}"]`);
      const model = data[name];
      row.hidden = !['running', 'paused'].includes(model.state);
      any ||= !row.hidden;
      const title = name === 'pomodoro' ? PHASES[model.phase] : label;
      $('span', row).textContent = `${title} · ${STATES[model.state]}`;
    }
    activeUtilities.hidden = !any;
  },
  paint(data, delta) {
    for (const name of Object.keys(NAMES)) {
      const row = $(`[data-active-utility="${name}"]`);
      if (row.hidden) continue;
      const display = $('time', row);
      const seconds = visualTime(data[name], delta);
      const time = formatTime(name === 'stopwatch' ? Math.floor(seconds) : seconds);
      if (display.textContent !== time) display.textContent = time;
    }
  },
  connection(message) {
    setText('[data-utility-connection]', message);
  },
});
