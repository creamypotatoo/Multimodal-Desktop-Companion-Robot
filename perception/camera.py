"""Configurable, verified OpenCV capture without model or audio imports."""
from dataclasses import dataclass
from pathlib import Path
import platform
import cv2


def load_face_cascade():
    filename = 'haarcascade_frontalface_default.xml'
    bundled = getattr(getattr(cv2, 'data', None), 'haarcascades', None)
    paths = [Path('/usr/share/opencv4/haarcascades') / filename]
    if bundled:
        paths.insert(0, Path(bundled) / filename)
    for path in paths:
        if path.is_file():
            detector = cv2.CascadeClassifier(str(path))
            if not detector.empty():
                return detector
    raise RuntimeError('OpenCV face cascade unavailable')


@dataclass(frozen=True)
class CameraConfig:
    source: object = None
    backend: int = None
    width: int = 640
    height: int = 480
    fps: float = 30
    pixel_format: str = 'MJPG'
    read_attempts: int = 10

    def __post_init__(self):
        if min(self.width, self.height, self.fps, self.read_attempts) <= 0:
            raise ValueError('Dimensions, FPS and read attempts must be positive')
        if self.pixel_format is not None and len(self.pixel_format) != 4:
            raise ValueError('Pixel format must be a FourCC or None')

    def resolved(self):
        linux = platform.system() == 'Linux'
        return (self.source if self.source is not None else ('/dev/video0' if linux else 0),
                self.backend if self.backend is not None else (cv2.CAP_V4L2 if linux else cv2.CAP_ANY))


class Camera:
    """Try requested settings, size/FPS only, then fresh driver defaults.

    A successful frame is retained for the first read. Source/backend choices
    are never silently changed. Reported FPS is negotiated, not measured.
    """
    def __init__(self, config=None):
        self.config = config or CameraConfig()
        self._capture = None
        self._first_frame = None
        self.settings = None

    def open(self):
        if self._capture is not None:
            return self
        source, backend = self.config.resolved()
        modes = ['requested', 'size/fps', 'driver defaults']
        if self.config.pixel_format is None:
            modes.remove('requested')
        failures = []
        for mode in modes:
            cap = cv2.VideoCapture(source, backend)
            accepted = False
            try:
                if not cap.isOpened():
                    failures.append(f'{mode}: could not open')
                    continue
                if mode == 'requested':
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.config.pixel_format))
                if mode != 'driver defaults':
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
                    cap.set(cv2.CAP_PROP_FPS, self.config.fps)
                for _ in range(self.config.read_attempts):
                    ok, frame = cap.read()
                    if ok and frame is not None and frame.size:
                        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
                        self.settings = dict(source=source, backend=cap.getBackendName(), mode=mode,
                            width=cap.get(cv2.CAP_PROP_FRAME_WIDTH),
                            height=cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
                            fps=cap.get(cv2.CAP_PROP_FPS),
                            pixel_format=''.join(chr((fourcc >> (8*i)) & 255) for i in range(4)),
                            frame_width=frame.shape[1], frame_height=frame.shape[0])
                        self._capture, self._first_frame = cap, frame
                        accepted = True
                        return self
                failures.append(f'{mode}: no valid frame')
            except cv2.error as exc:
                failures.append(f'{mode}: {exc}')
            finally:
                if not accepted:
                    cap.release()
        raise RuntimeError(f'Camera {source!r} failed: ' + '; '.join(failures))

    def read(self):
        if self._capture is None:
            raise RuntimeError('Camera is not open')
        if self._first_frame is not None:
            frame, self._first_frame = self._first_frame, None
            return True, frame
        return self._capture.read()

    def release(self):
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        self._first_frame = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *args):
        self.release()


def start_camera_feed(config=None):
    from perception.face import detect_faces
    try:
        with Camera(config) as camera:
            print('Camera settings:', camera.settings)
            print("Webcam feed started. Press 'q' to quit.")
            while True:
                ret, frame = camera.read()
                if not ret:
                    print('Failed to grab frame.')
                    break
                cv2.imshow("Eli's Eye - Webcam Feed", detect_faces(frame))
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break
    finally:
        cv2.destroyAllWindows()
