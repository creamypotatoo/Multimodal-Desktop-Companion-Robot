"""Headless smoke test: python -m perception.capture --output image.jpg."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import cv2
from perception.camera import Camera, CameraConfig


def save_jpeg(path, frame):
    ok, encoded = cv2.imencode('.jpg', frame)
    if not ok:
        raise RuntimeError('JPEG encoding failed')
    # Exclusive creation also protects against concurrent writers.
    with Path(path).open('xb') as output:
        output.write(encoded.tobytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', help='Device path or numeric webcam index')
    parser.add_argument('--backend', choices=['auto', 'v4l2', 'dshow', 'msmf'])
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=480)
    parser.add_argument('--fps', type=float, default=30)
    parser.add_argument('--pixel-format', default='MJPG', help="FourCC, or 'default'")
    parser.add_argument('--output', type=Path, default=Path(datetime.now().strftime('camera-%Y%m%d-%H%M%S-%f.jpg')))
    args = parser.parse_args(argv)
    backends = dict(auto=cv2.CAP_ANY, v4l2=cv2.CAP_V4L2, dshow=cv2.CAP_DSHOW, msmf=cv2.CAP_MSMF)
    source = args.source
    if source is not None and source.isdecimal():
        source = int(source)
    try:
        if args.output.exists():
            raise FileExistsError(f'Refusing to overwrite {args.output}')
        config = CameraConfig(source, backends.get(args.backend), args.width, args.height,
                              args.fps, None if args.pixel_format == 'default' else args.pixel_format)
        with Camera(config) as camera:
            print(json.dumps(camera.settings, indent=2))
            ok, frame = camera.read()
            if not ok:
                raise RuntimeError('Camera read failed')
            save_jpeg(args.output, frame)
        print(f'Saved {args.output.resolve()}')
        return 0
    except (OSError, ValueError, RuntimeError, cv2.error) as exc:
        parser.exit(1, f'Camera test failed: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
