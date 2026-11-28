"""Capture, analyze, and encode a bounded latest-frame preview in one worker."""
from copy import deepcopy
from dataclasses import replace
from math import isfinite
from pathlib import Path
import platform
import re
from threading import Event, Lock, Thread
from time import monotonic

import cv2

from intelligence.recognize import load_recognizer
from perception.camera import Camera, CameraConfig, load_face_cascade

FRESH_SECONDS = 2
MAX_PREVIEW_FPS = 8


def camera_devices():
    if platform.system() == 'Linux':
        paths = sorted(p for p in Path('/dev').glob('video*') if re.fullmatch(r'video[0-9]+', p.name))
        return [{'device': str(p), 'label': p.name} for p in paths]
    found = []
    for index in range(5):
        capture = cv2.VideoCapture(index, cv2.CAP_DSHOW if platform.system() == 'Windows' else cv2.CAP_ANY)
        try:
            if capture.isOpened():
                found.append({'device': str(index), 'label': f'Camera index {index}'})
        finally:
            capture.release()
    return found


def empty_perception():
    return dict(faces=[], face_present=False, identity=None, distance=None,
                identity_status='unavailable', expression=None, gaze=None)


def overlap(a, b):
    x, y, w, h = a
    u, v, p, q = b
    shared = max(0, min(x + w, u + p) - max(x, u)) * max(0, min(y + h, v + q) - max(y, v))
    return shared / (w*h + p*q - shared)


class FaceAnalysis:
    """Confirm a single continuous face over three frames before using its name."""
    def __init__(self, detector, recognize=None, classify=None):
        self.detector = detector
        self.recognize = recognize
        self.classify = classify
        self.previous = None
        self.candidate = None
        self.count = 0
        self.expression = None
        self.next_expression = 0

    def analyze(self, image, now):
        state = empty_perception()
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        boxes = [tuple(map(int, box)) for box in self.detector.detectMultiScale(
            gray, 1.1, 5, minSize=(30, 30))][:16] if self.detector is not None else []
        state['face_present'] = True if boxes else False if self.detector is not None else None
        state['faces'] = [dict(box=box, label='Face') for box in boxes]
        if len(boxes) != 1:
            self.previous = self.candidate = self.expression = None
            self.count = self.next_expression = 0
            state['identity_status'] = 'multiple_faces' if boxes else (
                'no_face' if self.detector is not None else 'unavailable')
            return state

        box = boxes[0]
        x, y, w, h = box
        height, width = gray.shape
        state['gaze'] = ((2 * (x + w/2) / width - 1) * .65, (2 * (y + h/2) / height - 1) * .65)
        continuous = self.previous is not None and overlap(box, self.previous) >= .3
        self.previous = box
        crop = gray[y:y+h, x:x+w]
        if self.recognize is not None and min(w, h) >= 80:
            name, distance = self.recognize(crop)
            state['distance'] = distance if isfinite(distance) else None
            known = name and name != 'Unknown' and state['distance'] is not None
            self.count = self.count + 1 if known and continuous and name == self.candidate else int(bool(known))
            self.candidate = name if known else None
            state['identity_status'] = 'confirming' if known else 'unknown'
            if self.count >= 3:
                state.update(identity=name, identity_status='recognized')
                state['faces'][0]['label'] = name
            elif not known:
                state['faces'][0]['label'] = 'Unknown'
        else:
            self.candidate, self.count = None, 0
        if self.classify is not None:
            if not continuous or now >= self.next_expression:
                try:
                    self.expression = self.classify(crop)
                except Exception:
                    self.classify = None
                    self.expression = None
                self.next_expression = now + 1
            state['expression'] = self.expression
        return state


class CameraService:
    def __init__(self, config=None, *, factory=Camera, detector=load_face_cascade,
                 recognizer=load_recognizer, classifier=None, discover=camera_devices,
                 simulated=False, clock=monotonic):
        backend = cv2.CAP_DSHOW if platform.system() == 'Windows' else None
        self.config = config or CameraConfig(fps=15, backend=backend)
        self.factory, self.detector, self.recognizer = factory, detector, recognizer
        self.classifier, self._discover = classifier, discover
        self.simulated, self.clock = simulated, clock
        self._lock = Lock()
        self._stop = Event()
        self._thread = None
        self._closed = False
        self._state = 'off'
        self._detail = 'Camera is off'
        self._sample = None
        self._settings = None
        self._recognition = False
        self._detection = False
        self._devices = []
        self._device_error = None

    def discover(self):
        with self._lock:
            if not self._closed and not (self._thread and self._thread.is_alive()):
                self._stop.clear()
                self._state, self._detail = 'discovering', 'Checking camera devices'
                self._device_error = None
                self._thread = Thread(target=self._find_devices, name='eli-camera-discovery', daemon=True)
                self._thread.start()
            return self._device_snapshot()

    def devices(self):
        with self._lock:
            return self._device_snapshot()

    def _device_snapshot(self):
        return dict(devices=deepcopy(self._devices), scanning=self._state == 'discovering',
                    error=self._device_error)

    def _find_devices(self):
        devices, error = [], None
        try:
            devices = self._discover()
        except Exception:
            error = 'Camera discovery failed'
        finally:
            with self._lock:
                if not self._stop.is_set():
                    self._devices, self._device_error = devices, error
                self._state, self._detail = 'off', 'Camera is off'

    def start(self, device=None, width=None, height=None, fps=None):
        settings = {}
        if device is not None:
            if not isinstance(device, str) or not re.fullmatch(r'(?:[0-9]{1,2}|/dev/video[0-9]{1,3})', device):
                raise ValueError('Select a camera index or /dev/video device')
            source = int(device) if device.isdecimal() else device
            if isinstance(source, int) and platform.system() == 'Linux':
                source = f'/dev/video{source}'
            settings['source'] = source
        for name, value, low, high in [('width', width, 160, 1920), ('height', height, 120, 1080),
                                       ('fps', fps, 1, 30)]:
            if value is not None:
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f'{name} must be an integer from {low} to {high}')
                settings[name] = value
        with self._lock:
            if self._closed or (self._thread is not None and self._thread.is_alive()):
                raise RuntimeError('Camera is already active or shutting down')
            self.config = replace(self.config, **settings)
            self._stop.clear()
            self._sample = self._settings = None
            self._recognition = self._detection = False
            self._state, self._detail = 'starting', 'Opening camera'
            self._thread = Thread(target=self._capture, name='eli-camera', daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stop.set()
            self._sample = self._settings = None
            if self._thread is not None and self._thread.is_alive():
                self._state, self._detail = 'stopping', 'Releasing camera'
            else:
                self._state, self._detail = 'off', 'Camera is off'

    def shutdown(self):
        with self._lock:
            self._closed = True
        self.stop()
        with self._lock:
            worker = self._thread
        if worker is not None:
            worker.join(timeout=.5)
        return worker is None or not worker.is_alive()

    def snapshot(self):
        with self._lock:
            sample = self._fresh()
            state = deepcopy(sample[1]) if sample else empty_perception()
            if sample is None:
                state['face_present'] = None
            detail = 'Waiting for a fresh camera frame' if self._state == 'active' and sample is None else self._detail
            camera = dict(state=self._state, detail=detail, settings=deepcopy(self._settings),
                          available=True, simulated=self.simulated, preview=sample is not None,
                          requested=dict(device=str(self.config.resolved()[0]), width=self.config.width,
                                         height=self.config.height, fps=self.config.fps),
                          preview_fps=min(self.config.fps, MAX_PREVIEW_FPS))
            return dict(camera=camera, recognition=dict(available=self._recognition and self._state == 'active',
                                                         detection=self._detection and self._state == 'active',
                                                         **state))

    def preview(self):
        with self._lock:
            sample = self._fresh()
            return sample[2] if sample else None

    def _fresh(self):
        if self._sample is not None and self.clock() - self._sample[0] < FRESH_SECONDS:
            return self._sample
        return None

    def _capture(self):
        camera = None
        detail = 'Camera is off'
        try:
            camera = self.factory(self.config)
            camera.open()
            try:
                detector = self.detector()
            except (OSError, RuntimeError, cv2.error):
                detector = None
            try:
                recognize = self.recognizer()
            except (OSError, ValueError, AttributeError, cv2.error):
                recognize = None
            try:
                classify = self.classifier() if self.classifier else None
            except Exception:
                classify = None
            analysis = FaceAnalysis(detector, recognize, classify)
            with self._lock:
                if self._stop.is_set():
                    return
                self._settings = dict(camera.settings or {})
                self._detection, self._recognition = detector is not None, recognize is not None and detector is not None
                self._state, self._detail = 'active', 'Camera active'
            period = 1 / min(self.config.fps, MAX_PREVIEW_FPS)
            while not self._stop.is_set():
                started = self.clock()
                ok, image = camera.read()
                if not ok or image is None or not image.size:
                    raise RuntimeError('Camera disconnected or frame read failed')
                image = image.copy()
                try:
                    state = analysis.analyze(image, started)
                except cv2.error:
                    # A corrupt/incompatible recognizer must not break video.
                    analysis = FaceAnalysis(detector)
                    with self._lock:
                        self._recognition = False
                    try:
                        state = analysis.analyze(image, started)
                    except cv2.error:
                        analysis = FaceAnalysis(None)
                        with self._lock:
                            self._detection = False
                        state = analysis.analyze(image, started)
                for face in state['faces']:
                    x, y, w, h = face['box']
                    cv2.rectangle(image, (x, y), (x+w, y+h), (0, 220, 100), 2)
                    cv2.putText(image, face['label'], (x, max(16, y-6)), cv2.FONT_HERSHEY_SIMPLEX,
                                .55, (0, 220, 100), 1, cv2.LINE_AA)
                if max(image.shape[:2]) > 960:
                    scale = 960 / max(image.shape[:2])
                    image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if not ok:
                    raise RuntimeError('Camera preview encoding failed')
                with self._lock:
                    if self._stop.is_set():
                        return
                    self._sample = (started, state, encoded.tobytes())
                self._stop.wait(max(0, period - (self.clock() - started)))
        except Exception as exc:
            detail = str(exc) if isinstance(exc, RuntimeError) else 'Camera capture failed'
        finally:
            try:
                if camera is not None:
                    camera.release()
            finally:
                with self._lock:
                    self._sample = self._settings = None
                    stopped = self._stop.is_set()
                    self._state, self._detail = ('off', 'Camera is off') if stopped else ('error', detail)
