"""Bounded, pull-based event subscriptions. Consumer code never runs in the controller."""
from collections import deque
from copy import deepcopy
from threading import Lock


class EventStream:
    def __init__(self):
        self._lock = Lock()
        self._events = deque(maxlen=128)
        self._subscriptions = {}
        self._sequence = 0

    def publish(self, event, **data):
        with self._lock:
            self._sequence += 1
            self._events.append(dict(id=self._sequence, event=event, data=deepcopy(data)))

    def subscribe(self, name):
        with self._lock:
            if name in self._subscriptions:
                raise ValueError('Subscriber already registered')
            if len(self._subscriptions) >= 32:
                raise ValueError('Subscriber limit reached')
            self._subscriptions[name] = self._sequence

    def read(self, name):
        with self._lock:
            cursor = self._subscriptions[name]
            self._subscriptions[name] = self._sequence
            return deepcopy([item for item in self._events if item['id'] > cursor])

    def unsubscribe(self, name):
        with self._lock:
            self._subscriptions.pop(name, None)

    def close(self):
        with self._lock:
            self._subscriptions.clear()
            self._events.clear()
