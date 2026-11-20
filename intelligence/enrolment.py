"""Explicit headless capture, candidate training, evaluation and activation.

Run from the project root: python -m intelligence.enrolment --help
"""
import argparse
from datetime import datetime
import os
from pathlib import Path
import shutil
import statistics
import time
from uuid import uuid4

import cv2
import numpy as np

from perception.camera import Camera, CameraConfig
from perception.capture import save_jpeg

ROOT = Path(__file__).resolve().parents[1] / 'data'


def labels_at(root):
    names = (root / 'labels.txt').read_text().splitlines()
    if not names or len(names) != len(set(names)) or any(
            not n or n in ('.', '..') or '/' in n or '\\' in n for n in names):
        raise ValueError('Labels must be unique, nonempty directory names')
    return names


def new_run(root):
    path = root / 'enrolment_runs' / (datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex)
    path.mkdir(parents=True, exist_ok=False)
    return path


def backup(root, destination):
    destination.mkdir(parents=True, exist_ok=False)
    for filename in ('trained_model.yml', 'labels.txt'):
        shutil.copy2(root / filename, destination / filename)


def capture(root, name, samples=40, interval=3.0, timeout=600, minimum=80,
            source='/dev/video0', backend=cv2.CAP_V4L2):
    # Lazy imports keep training/evaluation independent of camera/model startup.
    from perception.face import face_cascade
    from perception.diagnose_faces import select_primary_face

    if name not in labels_at(root):
        raise ValueError('Identity must already exist in labels.txt')
    if not 30 <= samples <= 50 or interval < 2 or timeout <= 0 or minimum <= 0:
        raise ValueError('Use 30–50 samples, interval >= 2 seconds, positive timeout/minimum')
    if face_cascade.empty():
        raise RuntimeError('Face cascade is empty')
    # A separate validation block avoids interleaving nearly adjacent captures.
    validation_count = max(6, samples // 5)
    training_count = samples - validation_count
    token = uuid4().hex
    train_dir = root / 'faces' / name
    validation_dir = root / 'face_validation' / name / token
    train_dir.mkdir(parents=True, exist_ok=True)
    validation_dir.mkdir(parents=True, exist_ok=False)
    prompts = ['Face forward, relaxed', 'Turn slightly left', 'Turn slightly right',
               'Tilt slightly up', 'Tilt slightly down', 'Smile / change expression']
    count = 0
    deadline = time.monotonic() + timeout
    next_capture = time.monotonic() + interval
    print('Only the named person should be visible. Move slowly; keep your face unobstructed.', flush=True)
    print(prompts[0], flush=True)
    with Camera(CameraConfig(source=source, backend=backend)) as camera:
        print('Camera:', camera.settings, flush=True)
        while count < samples:
            now = time.monotonic()
            if now >= deadline:
                raise RuntimeError(f'Timeout: {count}/{samples} saved; partial samples retained')
            ok, frame = camera.read()
            if not ok or frame is None or not frame.size:
                raise RuntimeError('Camera read failed; partial samples retained')
            if now < next_capture:
                continue  # Drain frames so spacing does not retain stale buffered images.
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            boxes = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(minimum, minimum))
            box = select_primary_face(boxes, (minimum, minimum))
            if box is None:
                continue
            x, y, w, h = box
            validation = count >= training_count
            target = validation_dir if validation else train_dir
            path = target / f'enrol-{token}-{count:03d}.jpg'
            save_jpeg(path, gray[y:y+h, x:x+w])
            count += 1
            print(f'{count}/{samples} {"validation" if validation else "training"} '
                  f'crop={w}x{h} {path}', flush=True)
            if count == training_count:
                print('Validation block: change pose, then repeat varied angles and expressions.', flush=True)
            print(prompts[count % len(prompts)], flush=True)
            next_capture = time.monotonic() + interval
    print(f'Saved {training_count} training and {validation_count} held-out images. No training performed.')


def load_training(root, names):
    faces = root / 'faces'
    unexpected = [p.name for p in faces.iterdir() if p.is_dir() and p.name not in names]
    if unexpected:
        raise ValueError(f'Unmapped identity directories: {unexpected}')
    images, ids = [], []
    for label, name in enumerate(names):
        # Read all original training files just as the original trainer does,
        # but fail visibly on unreadable data rather than silently skipping it.
        paths = sorted(p for p in (faces / name).iterdir() if p.is_file())
        if not paths:
            raise ValueError(f'No training images for {name}')
        for path in paths:
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError(f'Unreadable training image: {path}')
            images.append(image)
            ids.append(label)
    return images, np.asarray(ids, dtype=np.int32)


def train_candidate(root):
    names = labels_at(root)
    images, ids = load_training(root, names)
    old = cv2.face.LBPHFaceRecognizer_create()
    old.read(str(root / 'trained_model.yml'))
    if any(int(i) < 0 or int(i) >= len(names) for i in old.getLabels().flatten()):
        raise ValueError('Existing model IDs do not match labels.txt')
    run = new_run(root)
    backup(root, run / 'backup')  # Must succeed before any training.
    candidate = cv2.face.LBPHFaceRecognizer_create(
        old.getRadius(), old.getNeighbors(), old.getGridX(), old.getGridY(), old.getThreshold())
    candidate.train(images, ids)
    candidate.save(str(run / 'candidate.yml'))
    shutil.copy2(root / 'labels.txt', run / 'labels.txt')
    print(f'Trained {len(images)} images. Active model unchanged. Candidate: {run / "candidate.yml"}')
    return run / 'candidate.yml'


def evaluate(root, model):
    from intelligence.recognize import CONFIDENCE_THRESHOLD

    names = labels_at(root)
    if model.resolve() != (root / 'trained_model.yml').resolve():
        if model.with_name('labels.txt').read_bytes() != (root / 'labels.txt').read_bytes():
            raise ValueError('Candidate label mapping differs from active labels')
    recognizer = cv2.face.LBPHFaceRecognizer_create()
    recognizer.read(str(model))
    distances, correct = [], 0
    for name in names:
        for path in sorted((root / 'face_validation' / name).rglob('*.jpg')):
            image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError(f'Unreadable held-out image: {path}')
            label, distance = recognizer.predict(image)
            if not 0 <= label < len(names):
                raise ValueError(f'Invalid predicted label {label}')
            result = names[label] if distance < CONFIDENCE_THRESHOLD else 'Unknown'
            distances.append(float(distance))
            correct += result == name
            print(f'{path}: expected={name} nearest={names[label]} distance={distance:.2f} result={result}')
    if not distances:
        raise ValueError('No held-out validation images found')
    print(f'n={len(distances)} min={min(distances):.2f} max={max(distances):.2f} '
          f'mean={statistics.mean(distances):.2f} median={statistics.median(distances):.2f}; '
          f'correct accepted={correct}/{len(distances)} threshold={CONFIDENCE_THRESHOLD}')
    print('Known-person samples alone cannot estimate false acceptance of other people.')


def activate(root, model):
    if model.resolve() == (root / 'trained_model.yml').resolve():
        raise ValueError('Select a candidate, not the active model')
    if model.with_name('labels.txt').read_bytes() != (root / 'labels.txt').read_bytes():
        raise ValueError('Candidate label mapping differs from active labels')
    check = cv2.face.LBPHFaceRecognizer_create()
    check.read(str(model))
    names = labels_at(root)
    ids = check.getLabels().flatten()
    if not len(ids) or any(int(i) < 0 or int(i) >= len(names) for i in ids):
        raise ValueError('Candidate has missing or invalid labels')
    run = new_run(root)
    backup(root, run / 'backup')
    temporary = run / 'replacement.yml'
    shutil.copy2(model, temporary)
    os.replace(temporary, root / 'trained_model.yml')
    print(f'Activated {model}; recovery backup: {run / "backup"}. Labels unchanged.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    cap = commands.add_parser('capture')
    cap.add_argument('--name', default='Shanta')
    cap.add_argument('--samples', type=int, default=40)
    cap.add_argument('--interval', type=float, default=3)
    cap.add_argument('--timeout', type=float, default=600)
    cap.add_argument('--min-face-size', type=int, default=80)
    commands.add_parser('train')
    for command in ('evaluate', 'activate'):
        sub = commands.add_parser(command)
        sub.add_argument('--model', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'capture':
            capture(ROOT, args.name, args.samples, args.interval, args.timeout, args.min_face_size)
        elif args.command == 'train':
            train_candidate(ROOT)
        elif args.command == 'evaluate':
            evaluate(ROOT, args.model)
        else:
            activate(ROOT, args.model)
    except (OSError, ValueError, RuntimeError, cv2.error) as exc:
        parser.exit(1, f'Enrolment failed: {exc}\n')
    except KeyboardInterrupt:
        parser.exit(130, 'Interrupted; partial captures/backups retained\n')


if __name__ == '__main__':
    main()
