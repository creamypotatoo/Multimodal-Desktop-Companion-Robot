// Shared DOM helpers.
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function toast(message, type = 'info', ms = 3200) {
  const host = $('#toasts');
  if (!host) return;
  const el = document.createElement('div');
  el.className = `toast${type === 'error' ? ' toast--error' : ''}`;
  el.textContent = message;
  host.append(el);
  setTimeout(() => el.remove(), ms);
}

const LABELS = { live: 'Live', simulated: 'Simulated', unavailable: 'Unavailable' };

export function setBadge(el, availability) {
  if (!el) return;
  const kind = LABELS[availability] ? availability : 'unavailable';
  el.className = `badge badge--${kind}`;
  el.textContent = LABELS[kind];
}

// Fill a status-row macro from {availability, detail}.
export function fillStatusRow(key, info, detailText) {
  const row = $(`[data-status="${key}"]`);
  if (!row || !info) return;
  row.dataset.avail = info.availability;
  setBadge($('[data-badge]', row), info.availability);
  $('[data-detail]', row).textContent = detailText ?? info.detail ?? '';
}

export function setText(sel, value) {
  const el = typeof sel === 'string' ? $(sel) : sel;
  if (el) el.textContent = value;
}

export function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

// Paint the filled part of a range track (WebKit has no ::range-progress).
export function paintRange(input) {
  const min = Number(input.min || 0), max = Number(input.max || 100);
  const pct = ((Number(input.value) - min) / (max - min)) * 100;
  input.style.setProperty('--pct', `${pct}%`);
}

export function bindRange(input, onCommit, { format = (v) => v, out } = {}) {
  const output = out || $(`[data-out="${input.dataset.input || input.dataset.servo}"]`);
  const show = () => { paintRange(input); if (output) output.textContent = format(input.value); };
  input.addEventListener('input', show);
  input.addEventListener('change', () => onCommit(Number(input.value)));
  return {
    set(value) {
      if (document.activeElement === input) return; // don't fight the user's drag
      input.value = value;
      show();
    },
  };
}

// Run an async action with the button disabled and errors shown as toasts.
export async function busy(button, action) {
  if (button) button.disabled = true;
  try {
    return await action();
  } catch (err) {
    toast(err.message, 'error');
    return undefined;
  } finally {
    if (button) button.disabled = false;
  }
}

export const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
export const prefersReducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
