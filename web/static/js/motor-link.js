import { api } from './api.js';
import { $, $$, busy, setText, toast } from './ui.js';

export function startMotorLink(onMovement) {
  const panel = $('[data-motor-link]');
  if (!panel) return;
  const form = $('[data-serial-form]');
  let edited = false;
  let timer;

  function render(data) {
    onMovement?.(data);
    panel.hidden = !data.serial;
    if (panel.hidden) return;
    const link = data.serial;
    const active = data.motor_backend === 'esp32';
    const report = link.telemetry;
    const ack = link.last_ack;
    setText('[data-serial-backend]', active ? 'ESP32 serial' : 'Mock motors');
    setText('[data-serial-connection]', active
      ? `${link.connection}${report ? ` · firmware ${report.communication}` : ''}`
      : 'Serial backend inactive');
    setText('[data-serial-version]', report ? `${report.firmware_version} / v${report.protocol_version}` : 'Unavailable');
    setText('[data-serial-heartbeat]', link.heartbeat_age_ms === null
      ? `No active heartbeat · watchdog ${report?.watchdog || 'unavailable'}`
      : `${link.heartbeat_age_ms} ms since acknowledgment · watchdog ${report?.watchdog || 'unavailable'}`);
    setText('[data-serial-motion]', report
      ? `${report.state} · ${report.direction} · PWM ${report.left_pwm} / ${report.right_pwm}${link.commanded_speed === null ? '' : ` · last move ${link.commanded_speed}%`}`
      : link.command_pending ? 'Awaiting ESP32 acknowledgment' : 'Unavailable');
    setText('[data-serial-ack]', ack
      ? `${ack.command} #${ack.id} · ${ack.state}${ack.direction ? ` · ${ack.direction} at ${ack.speed}% for ${ack.duration_ms} ms` : ''}`
      : 'None');
    setText('[data-serial-estop]', report ? (report.estop ? 'Firmware latch set' : 'Firmware latch clear') : 'Firmware latch unknown');
    setText('[data-serial-error]', link.error || report?.last_error || 'None reported');
    if (form) {
      if (!edited) {
        form.elements.port.value = link.port || '';
        form.elements.baud.value = link.baud;
      }
      $('button[type=submit]', form).disabled = !active || !['disconnected', 'fault'].includes(link.connection);
      $('[data-motor-operation="disconnect"]', form).disabled = !active || ['disconnected', 'disconnecting'].includes(link.connection);
    }
  }

  async function refresh() {
    clearTimeout(timer);
    try { render(await api.get('/movement')); }
    catch (error) { setText('[data-serial-error]', error.message); }
    if (!document.hidden) timer = setTimeout(refresh, 450);
  }

  function send(button, data) {
    return busy(button, async () => {
      render(await api.post('/movement/link', data));
      if (data.operation === 'connect') edited = false;
      await refresh();
    });
  }

  $$('[data-motor-select]').forEach((button) => button.addEventListener('click', () =>
    send(button, { operation: 'select', backend: button.dataset.motorSelect })));
  $$('[data-motor-operation]').forEach((button) => button.addEventListener('click', () =>
    send(button, { operation: button.dataset.motorOperation })));
  form?.addEventListener('input', () => { edited = true; });
  form?.addEventListener('submit', (event) => {
    event.preventDefault();
    send($('button[type=submit]', form), {
      operation: 'connect', port: form.elements.port.value.trim(), baud: Number(form.elements.baud.value),
    });
  });
  $('[data-serial-discover]')?.addEventListener('click', (event) => busy(event.currentTarget, async () => {
    const { ports } = await api.get('/movement/ports');
    $('#eli-serial-ports').replaceChildren(...ports.map(({ port, description }) => new Option(description, port)));
    toast(ports.length ? ports.map(({ port, description }) => `${port} · ${description}`).join(', ') : 'No serial ports found');
  }));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  refresh();
  return refresh;
}
