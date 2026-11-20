"""Headless LBPH diagnostic: python -m perception.diagnose_faces."""
import argparse
from datetime import datetime
from pathlib import Path
import statistics
import time

import cv2

from intelligence.recognize import CONFIDENCE_THRESHOLD, recognize_face
from perception.camera import Camera, CameraConfig
from perception.capture import save_jpeg
from perception.face import face_cascade


def select_primary_face(faces, min_face_size=(80, 80)):
    """Filter both dimensions, then select the largest candidate by area."""
    candidates = [box for box in faces
                  if box[2] >= min_face_size[0] and box[3] >= min_face_size[1]]
    return max(candidates, key=lambda box: int(box[2]) * int(box[3]), default=None)


def collect(camera, output, samples=20, timeout=120, min_face_size=(80, 80)):
    """Collect one original grayscale face crop per frame, without training."""
    if face_cascade.empty():
        raise RuntimeError('Face cascade did not load')
    distances = []
    deadline = time.monotonic() + timeout
    frames = 0
    saved = False
    print(f'Collecting {samples} detections; threshold remains {CONFIDENCE_THRESHOLD}', flush=True)
    while len(distances) < samples:
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Timed out: collected {len(distances)}/{samples} detections')
        ok, frame = camera.read()
        frames += 1
        if not ok or frame is None or not frame.size:
            raise RuntimeError(f'Camera read failed on frame {frames}')
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=min_face_size)
        selected = select_primary_face(faces, min_face_size)
        if selected is None:
            print(f'frame={frames} no candidate >= {min_face_size[0]}x{min_face_size[1]}', flush=True)
            continue
        x, y, w, h = selected
        crop = gray[y:y+h, x:x+w]
        name, distance = recognize_face(crop)
        distances.append(float(distance))
        print(f'{len(distances):02d}/{samples} frame={frames} name={name} '
              f'distance={distance:.2f} bbox=({x},{y},{w},{h}) crop={w}x{h}', flush=True)
        if not saved:
            annotated = frame.copy()
            cv2.rectangle(annotated, (x, y), (x+w, y+h), (0, 255, 0), 2)
            cv2.putText(annotated, f'{name}: {distance:.2f}', (x, max(20, y-10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            save_jpeg(output, annotated)
            saved = True
            print(f'Saved {Path(output).resolve()}', flush=True)
    print(f'Distances (n={len(distances)}): min={min(distances):.2f} '
          f'max={max(distances):.2f} mean={statistics.mean(distances):.2f} '
          f'median={statistics.median(distances):.2f}', flush=True)
    return distances


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', help='Device path or numeric webcam index')
    parser.add_argument('--backend', choices=['auto', 'v4l2', 'dshow', 'msmf'])
    parser.add_argument('--timeout', type=float, default=120, help='Collection deadline in seconds')
    parser.add_argument('--min-face-size', type=int, nargs=2, default=(80, 80),
                        metavar=('WIDTH', 'HEIGHT'), help='Minimum face dimensions (default: 80 80)')
    parser.add_argument('--output', type=Path, default=Path(
        datetime.now().strftime('face-diagnostic-%Y%m%d-%H%M%S-%f.jpg')))
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if min(args.min_face_size) <= 0:
        parser.error('--min-face-size dimensions must be positive')
    source = args.source
    if source is not None and source.isdecimal():
        source = int(source)
    backends = dict(auto=cv2.CAP_ANY, v4l2=cv2.CAP_V4L2,
                    dshow=cv2.CAP_DSHOW, msmf=cv2.CAP_MSMF)
    try:
        if args.output.exists():
            raise FileExistsError(f'Refusing to overwrite {args.output}')
        with Camera(CameraConfig(source=source, backend=backends.get(args.backend))) as camera:
            print('Camera settings:', camera.settings, flush=True)
            collect(camera, args.output, timeout=args.timeout,
                    min_face_size=tuple(args.min_face_size))
        return 0
    except (OSError, RuntimeError, cv2.error) as exc:
        parser.exit(1, f'Face diagnostic failed: {exc}\n')
    except KeyboardInterrupt:
        parser.exit(130, 'Face diagnostic interrupted\n')


if __name__ == '__main__':
    raise SystemExit(main())
