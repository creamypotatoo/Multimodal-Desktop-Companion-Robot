from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from intelligence.recognize import load_recognizer
from perception.service import CameraService, FaceAnalysis
from runtime import Components, UnifiedRuntime
from test_web import WebTestCase


class Detector:
    boxes = [(20, 25, 90, 90)]

    def detectMultiScale(self, *args, **kwargs):
        return self.boxes


class FakeCamera:
    def __init__(self, config):
        self.settings = dict(frame_width=320, frame_height=240, fps=30)
        self.image = np.zeros((240, 320, 3), dtype=np.uint8)
        self.opens = self.releases = 0
        self.failed = Event()

    def open(self):
        self.opens += 1

    def read(self):
        return not self.failed.is_set(), self.image

    def release(self):
        self.releases += 1


def await_true(predicate):
    deadline = monotonic() + 4
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError('Timed out waiting for perception state')
        sleep(.01)


def mock_service():
    device = FakeCamera(None)
    detector = Detector()

    def missing():
        raise FileNotFoundError('Private data absent')

    service = CameraService(factory=lambda _: device, detector=lambda: detector, recognizer=missing,
                            discover=lambda: [{'device': '0', 'label': 'Mock camera'}], simulated=True)
    return service, device, detector


class CameraServiceTests(unittest.TestCase):
    def test_capture_is_exclusive_and_stop_revokes_preview(self):
        service, camera, _ = mock_service()
        self.addCleanup(service.shutdown)
        service.start()
        await_true(service.preview)
        with self.assertRaises(RuntimeError):
            service.start()
        self.assertIsNotNone(cv2.imdecode(np.frombuffer(service.preview(), np.uint8), cv2.IMREAD_COLOR))
        self.assertEqual(camera.opens, 1)
        state = service.snapshot()['recognition']
        self.assertTrue(state['face_present'])
        self.assertFalse(state['available'])
        self.assertEqual(state['identity_status'], 'unavailable')
        service.stop()
        self.assertIsNone(service.preview())
        self.assertIsNone(service.snapshot()['recognition']['face_present'])
        await_true(lambda: service.snapshot()['camera']['state'] == 'off')
        service.start()
        await_true(service.preview)
        self.assertTrue(service.shutdown())
        self.assertEqual(camera.releases, 2)
        with self.assertRaises(RuntimeError):
            service.start()

    def test_disconnection_releases_device_and_clears_detection(self):
        service, camera, _ = mock_service()
        self.addCleanup(service.shutdown)
        service.start()
        await_true(service.preview)
        camera.failed.set()
        await_true(lambda: service.snapshot()['camera']['state'] == 'error')
        self.assertEqual(camera.releases, 1)
        self.assertIsNone(service.preview())
        self.assertIsNone(service.snapshot()['recognition']['gaze'])

    def test_stale_frame_expires_while_driver_read_is_pending(self):
        service, camera, _ = mock_service()
        blocked, release = Event(), Event()
        read = camera.read
        clock = [0.]
        service.clock = lambda: clock[0]
        service.recognizer = lambda: lambda crop: ('Synthetic identity', 0.)
        remaining = iter(range(3))

        def stalled_read():
            if next(remaining, None) is None:
                blocked.set()
                release.wait(3)
            return read()

        camera.read = stalled_read
        self.addCleanup(service.shutdown)
        self.addCleanup(release.set)
        service.start()
        self.assertTrue(blocked.wait(3))
        self.assertEqual(service.snapshot()['recognition']['identity'], 'Synthetic identity')
        clock[0] = 3.
        self.assertIsNone(service.preview())
        self.assertIsNone(service.snapshot()['recognition']['identity'])

    def test_discovery_does_not_open_while_camera_is_active(self):
        service, camera, _ = mock_service()
        self.addCleanup(service.shutdown)
        service.discover()
        await_true(lambda: not service.devices()['scanning'])
        self.assertEqual(service.devices()['devices'][0]['device'], '0')
        self.assertEqual(camera.opens, 0)
        service.start()
        await_true(service.preview)
        self.assertFalse(service.discover()['scanning'])
        self.assertEqual(camera.opens, 1)


class RecognitionTests(unittest.TestCase):
    def test_default_lbph_threshold_and_explicit_override(self):
        predictor = Mock()
        face = SimpleNamespace(LBPHFaceRecognizer_create=lambda: predictor)
        with TemporaryDirectory() as directory, patch.object(cv2, 'face', face, create=True):
            model = Path(directory) / 'model.yml'
            labels = Path(directory) / 'labels.txt'
            model.touch()
            labels.write_text('Synthetic identity\n', encoding='utf-8')
            crop = FakeCamera(None).image[:, :, 0]
            default = load_recognizer(model, labels)
            predictor.predict.return_value = (0, 69.9)
            self.assertEqual(default(crop), ('Synthetic identity', 69.9))
            self.assertEqual(load_recognizer(model, labels, 60)(crop), ('Unknown', 69.9))
            predictor.predict.return_value = (0, 70.0)
            self.assertEqual(default(crop), ('Unknown', 70.0))

    def test_dashboard_uses_shared_threshold_and_honors_override(self):
        from web.__main__ import main

        for options, expected in (([], 70), (['--face-threshold', '61'], 61)):
            with self.subTest(options=options):
                camera, _, _ = mock_service()
                with (patch('perception.service.CameraService', return_value=camera),
                      patch('intelligence.recognize.load_recognizer') as loader,
                      patch('flask.Flask.run'),
                      patch.dict('os.environ', {'ELI_WEB_PASSWORD': 'synthetic test password'}, clear=True)):
                    self.assertEqual(main(['--unified', '--local-camera', *options]), 0)
                    camera.recognizer()
                loader.assert_called_once_with('data/trained_model.yml', 'data/labels.txt', expected)

    def test_confirm_unknown_and_clear_after_missing_or_multiple_faces(self):
        detector = Detector()
        results = iter([('Test A', 21.)]*3 + [('Test A', 21.), ('Unknown', 70.)])
        analysis = FaceAnalysis(detector, lambda crop: next(results))
        image = FakeCamera(None).image
        self.assertEqual(analysis.analyze(image, 1)['identity_status'], 'confirming')
        self.assertIsNone(analysis.analyze(image, 2)['identity'])
        self.assertEqual(analysis.analyze(image, 3)['identity'], 'Test A')
        detector.boxes = [(20, 25, 90, 90), (170, 25, 90, 90)]
        state = analysis.analyze(image, 4)
        self.assertEqual(state['identity_status'], 'multiple_faces')
        self.assertIsNone(state['identity'])
        self.assertIsNone(state['gaze'])
        detector.boxes = [(20, 25, 90, 90)]
        self.assertEqual(analysis.analyze(image, 5)['identity_status'], 'confirming')
        self.assertEqual(analysis.analyze(image, 6)['distance'], 70.)
        detector.boxes = []
        self.assertFalse(analysis.analyze(image, 7)['face_present'])
        detector.boxes = [(20, 25, 90, 90)]

    def test_missing_private_model_and_real_lbph_with_synthetic_crop(self):
        with TemporaryDirectory() as directory:
            model = Path(directory) / 'synthetic.yml'
            labels = Path(directory) / 'synthetic.txt'
            with self.assertRaises(FileNotFoundError):
                load_recognizer(model, labels)
            if not hasattr(cv2, 'face'):
                self.skipTest('OpenCV contrib LBPH is unavailable')
            recognizer = cv2.face.LBPHFaceRecognizer_create()
            crop = np.random.default_rng(5).integers(0, 255, (90, 90), dtype=np.uint8)
            recognizer.train([crop], np.array([0], dtype=np.int32))
            recognizer.write(str(model))
            labels.write_text('Synthetic identity\n', encoding='utf-8')
            self.assertEqual(load_recognizer(model, labels)(crop), ('Synthetic identity', 0.))
            name, distance = load_recognizer(model, labels, 1.)(np.zeros_like(crop))
            self.assertEqual(name, 'Unknown')
            self.assertGreater(distance, 1.)


class PerceptionAPITests(WebTestCase):
    def test_authenticated_preview_and_csrf_protected_controls(self):
        service, _, _ = mock_service()
        self.addCleanup(service.shutdown)
        self.app.extensions['eli_runtime'] = SimpleNamespace(camera=service)
        prefix = '/api/v1/perception'
        self.assertEqual(self.client.get(prefix + '/frame').status_code, 401)
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(self.client.get(prefix + '/frame').status_code, 204)
        self.assertEqual(self.call('POST', prefix + '/camera', {'action': 'start'}, csrf=False).status_code, 403)
        self.assert_ok(self.call('POST', prefix + '/camera', {'action': 'start'}))
        await_true(service.preview)
        response = self.client.get(prefix + '/frame')
        self.assertEqual(response.mimetype, 'image/jpeg')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assert_ok(self.call('POST', prefix + '/camera', {'action': 'stop'}))
        self.assertEqual(self.client.get(prefix + '/frame').status_code, 204)


class RuntimePerceptionTests(unittest.TestCase):
    def test_context_is_locked_and_eye_gaze_releases_without_motor_actions(self):
        service, _, detector = mock_service()
        service.recognizer = lambda: lambda crop: ('Synthetic identity', 0.)
        gazes = []
        eyes = SimpleNamespace(start=lambda: None, stop=lambda: None, set_expression=lambda _: None,
                               set_gaze=lambda *xy: gazes.append(xy), release_gaze=lambda: gazes.append(None))
        runtime = UnifiedRuntime(Components(lambda: eyes, None, None, lambda *a: '', None, camera=service))
        self.addCleanup(runtime.shutdown)
        runtime.startup()
        service.start()
        await_true(lambda: service.snapshot()['recognition']['identity'] == 'Synthetic identity')
        session = runtime._new_session(0, lambda: True)
        self.assertEqual(session.person_name, 'Synthetic identity')
        self.assertTrue(session.context['face_present'])
        self.assertTrue(any(point is not None for point in gazes))
        detector.boxes = []
        await_true(lambda: bool(gazes) and gazes[-1] is None)
        self.assertEqual(session.person_name, 'Synthetic identity')
        self.assertFalse(runtime._new_session(0, lambda: True).context['face_present'])
        self.assertFalse(runtime.controller.movement()['moving'])
