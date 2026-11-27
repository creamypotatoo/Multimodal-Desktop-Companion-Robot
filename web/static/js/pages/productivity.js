import { startShell } from '../shell.js';
import { $, $$, toast } from '../ui.js';
import { bindNotifications, formatTime, PHASES, STATES, visualTime, watchUtilities } from '../utilities.js';

const panels = $$('[data-utility]');
const tabs = $$('[role="tab"]');
let snapshot;
let submitting = false;
let visualDelta = 0;

function selectTab(tab) {
  for (const candidate of tabs) {
    const selected = candidate === tab;
    candidate.classList.toggle('is-active', selected);
    candidate.setAttribute('aria-selected', String(selected));
    candidate.tabIndex = selected ? 0 : -1;
    document.getElementById(candidate.getAttribute('aria-controls')).hidden = !selected;
  }
  if (snapshot) paint(snapshot, visualDelta);
}

tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectTab(tab));
  tab.addEventListener('keydown', (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step && !['Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs.at(-1)
      : tabs[(index + step + tabs.length) % tabs.length];
    selectTab(target);
    target.focus();
  });
});

function renderForm(form, values) {
  for (const [name, value] of Object.entries(values)) {
    const input = form.elements.namedItem(name);
    if (input.dataset.edited) continue;
    if (input.type === 'checkbox') input.checked = value;
    else if (input.value !== String(value)) input.value = value;
  }
}

function renderLaps(model) {
  const list = $('[data-laps]');
  const signature = JSON.stringify(model.laps);
  if (list.dataset.signature === signature) return;
  list.dataset.signature = signature;
  list.replaceChildren(...[...model.laps].reverse().map((lap) => {
    const row = document.createElement('tr');
    for (const value of [lap.number, formatTime(lap.duration, true), formatTime(lap.total, true)]) {
      row.insertCell().textContent = value;
    }
    return row;
  }));
  $('.lap-table').hidden = !model.laps.length;
  $('[data-lap-summary]').textContent = model.fastest
    ? `Fastest #${model.fastest.number} · ${formatTime(model.fastest.duration, true)} / Slowest #${model.slowest.number} · ${formatTime(model.slowest.duration, true)}`
    : 'No laps recorded.';
}

function update(data) {
  snapshot = data;
  for (const panel of panels) {
    const name = panel.dataset.utility;
    const model = data[name];
    const active = ['running', 'paused'].includes(model.state);
    const toggle = $('[data-action="toggle"]', panel);
    const form = $('[data-settings]', panel);
    $('fieldset', panel).disabled = submitting;
    $('[data-state]', panel).textContent = STATES[model.state];
    toggle.textContent = ({ running: 'Pause', paused: 'Resume' })[model.state] ?? 'Start';
    toggle.disabled = name === 'timer' && !model.duration;
    if (form) {
      for (const element of form.elements) element.disabled = active;
      if (!active) renderForm(form, name === 'timer' ? {
        hours: Math.floor(model.duration / 3600),
        minutes: Math.floor(model.duration / 60) % 60,
        seconds: model.duration % 60,
      } : model.settings);
    }
    if (name === 'timer') {
      for (const preset of $$('[data-preset]', panel)) {
        preset.disabled = active;
        const selected = Number(preset.dataset.preset) === model.duration;
        preset.classList.toggle('is-active', selected);
        preset.setAttribute('aria-pressed', String(selected));
      }
      $('[data-ring]', panel).classList.toggle('is-done', model.state === 'completed');
    } else if (name === 'stopwatch') {
      $('[data-action="lap"]', panel).disabled = model.state !== 'running';
      $('[data-action="clear_laps"]', panel).disabled = !model.laps.length;
      renderLaps(model);
    } else {
      const onBreak = model.phase !== 'focus';
      $('[data-phase]', panel).textContent = `${PHASES[model.phase]} ·`;
      $('.phase', panel).classList.toggle('is-break', onBreak);
      $('[data-ring]', panel).classList.toggle('ring--break', onBreak);
      const target = model.settings.long_every;
      const position = model.phase === 'long_break' ? target : Math.min(model.sessions_in_cycle, target);
      const dots = $('[data-session-dots]', panel);
      if (dots.children.length !== target) {
        dots.replaceChildren(...Array.from({ length: target }, () => document.createElement('span')));
      }
      Array.from(dots.children).forEach((dot, index) => dot.classList.toggle('filled', index < position));
      $('[data-sessions]', panel).textContent = `${position}/${target} this cycle · ${model.completed} completed · ${formatTime(model.total_focus)} focused`;
    }
  }
}

function paint(data, delta) {
  visualDelta = delta;
  const panel = panels.find((section) => !section.hidden);
  const name = panel.dataset.utility;
  const model = data[name];
  const time = visualTime(model, delta);
  const display = $('[data-display]', panel);
  const formatted = formatTime(time, name === 'stopwatch');
  if (display.textContent !== formatted) display.textContent = formatted;
  const ring = $('[data-ring]', panel);
  if (ring) {
    const progress = model.duration ? Math.max(0, 100 * (1 - time / model.duration)) : 0;
    $('.ring__bar', ring).setAttribute('stroke-dashoffset', 100 - progress);
    const text = `${Math.floor(progress)}% elapsed`;
    const label = $('[data-progress]', panel);
    if (label.textContent !== text) label.textContent = text;
  }
}

const request = watchUtilities({
  update, paint,
  connection: (message) => { $('[data-utility-connection]').textContent = message; },
});

for (const panel of panels) {
  const name = panel.dataset.utility;
  const form = $('[data-settings]', panel);
  async function send(method, path, data) {
    submitting = true;
    update(snapshot);
    try {
      const success = await request(method, `${name}/${path}`, data);
      if (success && path === 'settings') {
        for (const element of form.elements) delete element.dataset.edited;
      }
    } finally {
      submitting = false;
      update(snapshot);
    }
  }
  for (const control of $$('[data-action]', panel)) {
    control.addEventListener('click', () => {
      const action = control.dataset.action === 'toggle'
        ? ({ running: 'pause', paused: 'resume' })[snapshot[name].state] ?? 'start'
        : control.dataset.action;
      send('post', 'actions', { action });
    });
  }
  for (const preset of $$('[data-preset]', panel)) {
    preset.addEventListener('click', () => send('put', 'settings', { duration: Number(preset.dataset.preset) }));
  }
  if (!form) continue;
  form.addEventListener('input', (event) => { event.target.dataset.edited = 'true'; });
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const fields = new FormData(form);
    if (name === 'timer') {
      const duration = Number(fields.get('hours')) * 3600 + Number(fields.get('minutes')) * 60 + Number(fields.get('seconds'));
      if (!duration) { toast('Enter at least one second.', 'error'); return; }
      send('put', 'settings', { duration });
    } else {
      send('put', 'settings', {
        focus: Number(fields.get('focus')), short_break: Number(fields.get('short_break')),
        long_break: Number(fields.get('long_break')), long_every: Number(fields.get('long_every')),
        automatic: fields.has('automatic'),
      });
    }
  });
}

bindNotifications();
startShell();
