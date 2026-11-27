"""Read-only host metrics from the operating system, never invented.

On Linux (the Raspberry Pi) values come from ``/proc`` and the kernel thermal
zone, which are plain file reads and initialise no hardware. Anything the OS
does not expose (e.g. CPU temperature on Windows) is reported as ``None`` with
availability ``unavailable``. No third-party packages are used.
"""
import os
import platform
import threading
import time

from web.services.base import LIVE, UNAVAILABLE

PROC_STAT = '/proc/stat'
PROC_MEMINFO = '/proc/meminfo'
PROC_UPTIME = '/proc/uptime'
THERMAL_ZONE = '/sys/class/thermal/thermal_zone0/temp'


def _read(path):
    try:
        with open(path, encoding='ascii') as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _metric(value, unit, source):
    if value is None:
        return {'value': None, 'unit': unit, 'availability': UNAVAILABLE, 'source': None}
    return {'value': value, 'unit': unit, 'availability': LIVE, 'source': source}


class HostMetrics:
    """CPU %, RAM, temperature and uptime of the machine running the dashboard."""

    def __init__(self, reader=_read, clock=time.monotonic):
        self._read = reader
        self._clock = clock
        self._lock = threading.Lock()
        self._last_cpu = None  # (idle, total) from the previous /proc/stat sample

    def cpu_percent(self):
        """Busy % since the previous call; None on the first call or without /proc."""
        text = self._read(PROC_STAT)
        if not text or not text.startswith('cpu '):
            return None
        try:
            fields = [int(v) for v in text.splitlines()[0].split()[1:]]
        except ValueError:
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        with self._lock:
            previous, self._last_cpu = self._last_cpu, (idle, total)
        if previous is None or total <= previous[1]:
            return None
        busy = 1 - (idle - previous[0]) / (total - previous[1])
        return round(max(0.0, min(1.0, busy)) * 100, 1)

    def memory(self):
        text = self._read(PROC_MEMINFO)
        if not text:
            return None
        info = {}
        for line in text.splitlines():
            name, _, rest = line.partition(':')
            parts = rest.split()
            if parts and parts[0].isdigit():
                info[name] = int(parts[0])  # kB
        total, available = info.get('MemTotal'), info.get('MemAvailable')
        if not total or available is None:
            return None
        used = total - available
        return {'total_mb': round(total / 1024), 'used_mb': round(used / 1024),
                'percent': round(used / total * 100, 1)}

    def temperature_c(self):
        text = self._read(THERMAL_ZONE)
        try:
            return round(int(text.strip()) / 1000, 1) if text else None
        except ValueError:
            return None

    def uptime_s(self):
        text = self._read(PROC_UPTIME)
        try:
            return int(float(text.split()[0])) if text else None
        except (ValueError, IndexError):
            return None

    def snapshot(self):
        source = 'linux_procfs'
        memory = self.memory()
        return {
            'platform': platform.system() or 'unknown',
            'cpu_count': os.cpu_count(),
            'cpu_percent': _metric(self.cpu_percent(), '%', source),
            'memory_percent': _metric(memory and memory['percent'], '%', source),
            'memory': memory,
            'temperature': _metric(self.temperature_c(), '°C', 'linux_thermal_zone'),
            'uptime': _metric(self.uptime_s(), 's', source),
        }
