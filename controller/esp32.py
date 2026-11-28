"""ESP32 protocol v1. Only the worker opens, reads, writes and closes the port."""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import json
from math import isfinite
import os
from pathlib import Path
import re
from threading import Condition, Thread
from time import monotonic

from .errors import AdapterError, LIVE, UNAVAILABLE

HEARTBEAT = 0.2
STATUS_PERIOD = 0.25
ACK_TIMEOUT = 0.15
SERIAL_STARTUP_SECONDS = 3.0
MAX_PULSE_MS = 1000
DIRECTIONS = {'forward', 'backward', 'left', 'right'}
STATES = {'stopped', 'moving', 'reversing', 'estopped', 'fault'}


class LinkError(RuntimeError):
    pass


def _unique_fields(items):
    result = dict(items)
    if len(result) != len(items):
        raise ValueError('Duplicate JSON field')
    return result


def open_serial(port, baud):
    import serial

    link = serial.Serial(baudrate=baud, timeout=0.02, write_timeout=0.05,
                         exclusive=True if os.name == 'posix' else None)
    try:
        link.dtr = False
        link.rts = False
        link.port = port
        link.open()
    except Exception:
        try:
            link.close()
        except OSError:
            pass
        raise
    return link


def discover_ports():
    from serial.tools.list_ports import comports

    ports = {item.device: item.description for item in sorted(comports())}
    if os.name == 'posix':
        aliases = {}
        for directory in (Path('/dev/serial/by-id'), Path('/dev/serial/by-path')):
            for link in sorted(directory.glob('*')):
                target = os.path.realpath(link)
                if validate_port(str(link)) and link.is_symlink() and link.exists():
                    aliases[str(link)] = ports.get(target, Path(target).name)
        ports = {**aliases, **ports}
    return [dict(port=port, description=description) for port, description in ports.items()]


def validate_port(port):
    return (isinstance(port, str) and len(port) <= 256 and
            bool(re.fullmatch(r'COM[1-9][0-9]*|/dev/(?:tty[A-Za-z0-9_-]+|serial/by-(?:id|path)/[A-Za-z0-9_.:+-]+)',
                              port, re.IGNORECASE)) and
            '..' not in port.split('/'))


@dataclass(frozen=True)
class Request:
    command: str
    revision: int
    fields: dict
    expires: float | None = None


class ESP32MotorAdapter:
    def __init__(self, port=None, baud=115200, *, opener=open_serial, discover=discover_ports,
                 clock=monotonic, startup_seconds=SERIAL_STARTUP_SECONDS):
        if port is not None and not validate_port(port):
            raise ValueError('Select a COM port or an absolute /dev serial device')
        if type(baud) is not int or not 1200 <= baud <= 1000000:
            raise ValueError('Invalid serial baud rate')
        if not isfinite(startup_seconds) or not 0 <= startup_seconds <= SERIAL_STARTUP_SECONDS:
            raise ValueError('Invalid serial startup interval')
        self._opener, self._discover, self._clock = opener, discover, clock
        self._startup_seconds = startup_seconds
        self._condition = Condition()
        self._thread = None
        self._closing = False
        self._shutdown_estop = False
        self._revision = 0
        self._requests = deque()
        self._current = None
        self._port = port
        self._baud = baud
        self._state = 'disconnected'
        self._error = None
        self._status = None
        self._last_ack = None
        self._last_speed = None
        self._heartbeat_at = None
        self._reset_count = 0
        self._fault_count = 0
        # Worker-owned protocol state.
        self._serial = None
        self._session = None
        self._firmware_version = None
        self._last_uptime = None
        self._id = 0
        self._rx = bytearray()
        self._frames = deque()
        self._next_heartbeat = self._next_status = 0

    @property
    def availability(self):
        with self._condition:
            return LIVE if self._state == 'connected' else UNAVAILABLE

    def snapshot(self):
        with self._condition:
            return dict(backend='esp32', connection=self._state, port=self._port, baud=self._baud,
                        telemetry=deepcopy(self._status), last_ack=deepcopy(self._last_ack),
                        commanded_speed=self._last_speed, error=self._error,
                        heartbeat_age_ms=(None if self._heartbeat_at is None else
                                          round(1000 * (self._clock() - self._heartbeat_at))),
                        reset_count=self._reset_count, fault_count=self._fault_count,
                        reset_pending=self._pending('reset_safety'),
                        command_pending=self._pending('move') or
                        any(self._pending(name) for name in ('stop', 'estop', 'reset_safety')))

    def ports(self):
        try:
            return self._discover()
        except (ImportError, OSError) as exc:
            raise AdapterError(f'Could not list serial ports: {exc}') from None

    def startup(self):
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._closing = False
            self._shutdown_estop = False
            self._thread = Thread(target=self._run, name='eli-esp32', daemon=True)
            self._thread.start()

    def shutdown(self):
        with self._condition:
            if not self._thread:
                return
            self._closing = True
            self._shutdown_estop |= self._pending('estop')
            self._requests.clear()
            self._revision += 1
            self._condition.notify()
            thread = self._thread
        thread.join(timeout=0.8)

    def _pending(self, command):
        return ((self._current is not None and self._current.command == command)
                or any(request.command == command for request in self._requests))

    def _enqueue(self, command, *, expires=None, **fields):
        self._requests.append(Request(command, self._revision, fields, expires))
        self._condition.notify()

    def connect(self, port, baud=None):
        if not validate_port(port):
            raise AdapterError('Select a COM port or an absolute /dev serial device')
        if baud is not None and (type(baud) is not int or not 1200 <= baud <= 1000000):
            raise AdapterError('Invalid serial baud rate')
        with self._condition:
            if (self._closing or self._thread is None or not self._thread.is_alive()
                    or self._state not in ('disconnected', 'fault')):
                raise AdapterError('Disconnect the current link before connecting')
            self._port, self._baud = port, self._baud if baud is None else baud
            self._state, self._error = 'connecting', None
            self._status = self._last_ack = self._last_speed = self._heartbeat_at = None
            self._revision += 1
            self._enqueue('connect', port=port, baud=self._baud)

    def disconnect(self):
        with self._condition:
            if self._state in ('disconnected', 'disconnecting'):
                return
            self._state = 'disconnecting'
            self._fence('disconnect')

    def _fence(self, command):
        self._revision += 1
        # Preserve a requested emergency stop ahead of a subsequent STOP/disconnect.
        self._requests = deque(r for r in self._requests if r.command == 'estop')
        if not self._pending(command) or command in ('stop', 'estop'):
            if not any(r.command == command for r in self._requests):
                self._enqueue(command)

    def stop(self):
        with self._condition:
            if self._state == 'connected':
                self._fence('stop')
            elif self._state == 'connecting':
                if not self._pending('stop'):
                    self._enqueue('stop')
            elif self._state == 'disconnecting':
                return
            elif self._state != 'disconnected':
                raise AdapterError('ESP32 stop acknowledgement unavailable')

    def emergency_stop(self):
        with self._condition:
            if self._state not in ('connected', 'connecting', 'disconnecting'):
                raise AdapterError('ESP32 emergency stop acknowledgement unavailable')
            if self._state == 'connecting':
                if not self._pending('estop'):
                    self._enqueue('estop')
            elif self._state == 'disconnecting':
                # Keep the final close queued, with the latched stop ahead of it.
                self._fence('estop')
                self._enqueue('disconnect')
            else:
                self._fence('estop')

    def reset_safety(self):
        with self._condition:
            if self._state != 'connected' or self._requests or self._current:
                raise AdapterError('Wait for a connected, idle ESP32 link before reset')
            self._enqueue('reset_safety')

    def move(self, direction, speed, duration_ms):
        if (not isinstance(direction, str) or direction not in DIRECTIONS or type(speed) is not int
                or not 1 <= speed <= 100 or type(duration_ms) is not int
                or not 1 <= duration_ms <= MAX_PULSE_MS):
            raise AdapterError('Invalid ESP32 movement pulse')
        with self._condition:
            if self._state != 'connected' or self._current is not None or self._requests:
                raise AdapterError('ESP32 link is busy or unavailable')
            self._enqueue('move', direction=direction, speed=speed,
                          expires=self._clock() + duration_ms / 1000)

    def _run(self):
        try:
            while True:
                with self._condition:
                    if self._closing:
                        return
                    request = self._requests.popleft() if self._requests else None
                    self._current = request
                try:
                    if request:
                        self._handle(request)
                    elif self._serial:
                        now = self._clock()
                        if self._session and now >= self._next_heartbeat:
                            self._heartbeat()
                        elif now >= self._next_status:
                            self._poll_status()
                        else:
                            self._wait()
                    else:
                        self._wait()
                except (LinkError, OSError, ValueError, TypeError, ImportError) as exc:
                    self._fail(str(exc))
                finally:
                    with self._condition:
                        self._current = None
        finally:
            if self._serial:
                self._best_effort_stop('estop' if self._shutdown_estop else 'stop')
            self._close()
            with self._condition:
                self._state = 'disconnected'
                self._status = self._heartbeat_at = self._last_speed = None

    def _wait(self):
        with self._condition:
            if not self._requests and not self._closing:
                self._condition.wait(0.02)

    def _current_revision(self, request):
        with self._condition:
            return request.revision == self._revision and not self._closing

    def _handle(self, request):
        command = request.command
        if command == 'connect':
            self._connect(request)
            return
        if not self._serial:
            if command == 'disconnect':
                with self._condition:
                    self._state = 'disconnected'
            return
        if command in ('stop', 'estop', 'disconnect'):
            ack = self._exchange('stop' if command == 'disconnect' else command)
            self._session = None
            with self._condition:
                self._heartbeat_at = self._last_speed = None
                # Do not present pre-fence telemetry as the new commanded state.
                self._status = None
                self._save_ack('stop' if command == 'disconnect' else command, ack)
            if command == 'disconnect':
                self._close()
                with self._condition:
                    self._state = 'disconnected'
            return
        if not self._current_revision(request):
            return
        if command == 'move' and (request.expires - self._clock()) < 0.001:
            return
        if not self._session:
            self._negotiate()
            status = self._poll_status()
            if command == 'move' and (status['estop'] or not status['driver_ready']):
                raise LinkError('ESP32 safety prevents movement')
        if not self._current_revision(request):
            return
        if command == 'reset_safety':
            ack = self._exchange(command, session=self._session)
            self._session = None
            self._poll_status()
            with self._condition:
                self._heartbeat_at = None
                if self._current_revision(request):
                    self._reset_count += 1
                    self._save_ack(command, ack)
            return
        remaining = int((request.expires - self._clock()) * 1000)
        if remaining > 0 and self._current_revision(request):
            ack = self._exchange(command, session=self._session, duration_ms=remaining, **request.fields)
            with self._condition:
                self._save_ack(command, ack)
                self._last_ack.update(direction=request.fields['direction'], speed=request.fields['speed'],
                                      duration_ms=remaining)
                self._last_speed = request.fields['speed']
                self._status = None
            self._next_status = 0

    def _save_ack(self, command, ack):
        self._last_ack = dict(command=command, id=ack['id'], state=ack['state'])

    def _connect(self, request):
        if not self._current_revision(request):
            return
        connected = False
        try:
            self._serial = self._opener(**request.fields)
            # Opening USB serial can reset the board and leave startup bytes queued.
            with self._condition:
                cancelled = self._condition.wait_for(lambda: not self._current_revision(request),
                                                     timeout=self._startup_seconds)
            if cancelled:
                return
            self._serial.reset_input_buffer()
            self._rx.clear()
            self._frames.clear()
            if not self._negotiate(boot_request=request) or not self._current_revision(request):
                return
            self._poll_status()
            with self._condition:
                if self._current_revision(request):
                    self._state = 'connected'
                    connected = True
        except (LinkError, OSError):
            if self._current_revision(request):
                raise
        finally:
            if self._serial and not connected and not self._current_revision(request):
                with self._condition:
                    emergency = self._shutdown_estop or self._pending('estop')
                try:
                    self._best_effort_stop('estop' if emergency else 'stop')
                finally:
                    self._close()
                    with self._condition:
                        self._status = self._heartbeat_at = None

    def _negotiate(self, *, boot_request=None):
        boot = boot_request is not None
        deadline = self._clock() + (2.5 if boot else ACK_TIMEOUT)
        while True:
            if boot and not self._current_revision(boot_request):
                return False
            try:
                hello = self._exchange('hello', boot=boot)
                break
            except TimeoutError:
                with self._condition:
                    if not boot or self._clock() >= deadline:
                        raise LinkError('ESP32 handshake timed out') from None
        token = hello.get('session')
        if (type(hello.get('protocol_version')) is not int or hello['protocol_version'] != 1
                or not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{16}', token)
                or not isinstance(hello.get('firmware_version'), str)):
            raise LinkError('Incompatible ESP32 handshake')
        self._session = token
        self._firmware_version = hello['firmware_version']
        if boot and not self._current_revision(boot_request):
            return False
        self._heartbeat()
        return True

    def _heartbeat(self):
        started = self._clock()
        self._exchange('heartbeat', session=self._session)
        with self._condition:
            self._heartbeat_at = self._clock()
        self._next_heartbeat = started + HEARTBEAT

    def _poll_status(self):
        status = self._exchange('status')
        if (type(status.get('protocol_version')) is not int or status['protocol_version'] != 1
                or status.get('firmware_version') != self._firmware_version
                or type(status.get('estop')) is not bool or type(status.get('driver_ready')) is not bool
                or status.get('communication') not in ('disconnected', 'awaiting_heartbeat', 'ready')
                or status.get('watchdog') not in ('armed', 'disarmed', 'tripped')
                or status.get('direction') not in DIRECTIONS | {'none'}
                or status.get('sensors') != {'cliff': 'unavailable', 'obstacle': 'unavailable'}
                or any(type(status.get(key)) is not int or not 0 <= status[key] <= limit
                       for key, limit in (('left_pwm', 255), ('right_pwm', 255),
                                          ('remaining_ms', MAX_PULSE_MS), ('uptime_ms', 2**64-1)))
                or (status.get('last_error') is not None and not isinstance(status['last_error'], str))):
            raise LinkError('Invalid ESP32 status')
        if self._session and (status['communication'] != 'ready' or status['watchdog'] != 'armed'):
            raise LinkError('ESP32 session was reset or watchdog expired')
        with self._condition:
            if self._last_uptime is not None and status['uptime_ms'] < self._last_uptime:
                raise LinkError('ESP32 firmware reset detected')
            self._last_uptime = status['uptime_ms']
            self._status = status
        self._next_status = self._clock() + STATUS_PERIOD
        return status

    def _exchange(self, command, *, boot=False, **fields):
        if self._id == 2147483647:
            raise LinkError('Command ID space exhausted; restart the host')
        self._id += 1
        packet = (json.dumps(dict(id=self._id, cmd=command, **fields), separators=(',', ':')) + '\n').encode()
        if self._serial.write(packet) != len(packet):
            raise LinkError('Incomplete serial write')
        deadline = self._clock() + ACK_TIMEOUT
        while self._clock() < deadline:
            if not self._frames:
                self._rx.extend(self._serial.read(64))
                while b'\n' in self._rx:
                    line, _, rest = self._rx.partition(b'\n')
                    self._rx = bytearray(rest)
                    self._frames.append(bytes(line))
                if len(self._rx) > 512:
                    raise LinkError('Oversized ESP32 response')
            if not self._frames:
                continue
            line = self._frames.popleft().strip()
            if len(line) > 512:
                raise LinkError('Oversized ESP32 response')
            try:
                frame = json.loads(line, object_pairs_hook=_unique_fields)
                if (not isinstance(frame, dict) or type(frame.get('id')) is not int
                        or type(frame.get('ok')) is not bool or frame.get('state') not in STATES):
                    raise ValueError
            except (ValueError, TypeError):
                raise LinkError('Malformed ESP32 response') from None
            if frame['id'] != self._id:
                continue
            if not frame['ok']:
                raise LinkError(f'ESP32 rejected {command}: {str(frame.get("error", "unknown"))[:80]}')
            return frame
        if boot:
            raise TimeoutError
        raise LinkError(f'ESP32 {command} acknowledgement timed out')

    def _best_effort_stop(self, command='stop'):
        try:
            self._exchange(command)
        except (LinkError, OSError, ValueError):
            pass

    def _fail(self, reason):
        with self._condition:
            emergency = self._pending('estop')
            self._state = 'fault'
            self._error = reason
            self._fault_count += 1
            self._status = self._heartbeat_at = self._last_speed = None
            self._last_ack = None
            self._revision += 1
            self._requests.clear()
        if self._serial:
            self._best_effort_stop('estop' if emergency else 'stop')
        self._close()

    def _close(self):
        if self._serial:
            try:
                self._serial.close()
            except OSError:
                pass
        self._serial = self._session = None
        self._last_uptime = self._firmware_version = None
        self._rx.clear()
        self._frames.clear()
