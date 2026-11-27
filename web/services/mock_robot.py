"""Dashboard compatibility facade backed by the application controller."""
import time

from controller import Action, Authority, Command, RobotController, Source
from controller.errors import SIMULATED, UNAVAILABLE, ServiceError
from web.services.base import RobotService
from web.services.host_metrics import HostMetrics


class MockRobotService(RobotService):
    backend = 'mock'

    def __init__(self, clock=time.monotonic, wall_clock=time.time, metrics=None, *, controller=None,
                 utilities=None):
        self.controller = (controller if controller is not None
                           else RobotController(clock=clock, wall_clock=wall_clock, utilities=utilities))
        self._metrics = metrics if metrics is not None else HostMetrics()

    def command(self, operation, *, authority=Authority(), **args):
        result = self.controller.execute(Command(operation, Source.DASHBOARD, args), authority)
        if not result.ok:
            exc = ServiceError(result.message, field=result.field)
            exc.status, exc.code = result.status, result.code
            raise exc
        return result.state

    def status(self):
        return self.command(Action.STATUS)

    def get_mode(self):
        return self.controller.get_mode()

    def set_mode(self, mode, *, authority=Authority()):
        return self.command(Action.MODE, mode=mode, authority=authority)

    def get_expression(self):
        return self.controller.get_expression()

    def set_expression(self, expression):
        return self.command(Action.EXPRESSION, expression=expression)

    def get_volume(self):
        return self.controller.get_volume()

    def set_volume(self, volume):
        return self.command(Action.VOLUME, volume=volume)

    def get_settings(self):
        return self.controller.get_settings()

    def update_settings(self, changes):
        return self.command(Action.SETTINGS, changes=changes)

    def movement(self):
        return self.controller.movement()

    def move(self, direction, duration_ms=None, *, authority=Authority()):
        return self.command(Action.MOVE, direction=direction, duration_ms=duration_ms,
                            authority=authority)

    def set_speed(self, speed, *, authority=Authority()):
        return self.command(Action.SPEED, speed=speed, authority=authority)

    def stop(self):
        return self.command(Action.STOP)

    def set_servo(self, servo, angle, *, authority=Authority()):
        return self.command(Action.SERVO, servo=servo, angle=angle, authority=authority)

    def companion(self):
        return self.controller.companion()

    def conversation(self, action):
        return self.command(Action.CONVERSATION, action=action)

    def perception(self):
        return self.controller.perception()

    def diagnostics(self):
        components = ('camera', 'microphone', 'speaker', 'tft_display')
        status = self.controller.status()
        return {
            'host': self._metrics.snapshot(),
            'esp32': {'availability': UNAVAILABLE, 'detail': 'ESP32 link is unavailable.',
                      'connected': False},
            'sensors': {name: {'availability': UNAVAILABLE, 'detail': 'Hardware is unavailable.'}
                        for name in components},
            'service': {'backend': self.backend, 'availability': SIMULATED,
                        'uptime_s': status['uptime_s']},
            'controller': {'safety': status['movement']['safety'],
                           'capabilities': status['capabilities'],
                           'runtime': status.get('runtime'),
                           'latest_error': next(iter(status['errors']), None)},
        }
