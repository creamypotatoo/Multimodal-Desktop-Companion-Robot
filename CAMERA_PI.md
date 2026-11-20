# Raspberry Pi camera test over SSH (Phase 1)

Copy the project to the Pi, then run these commands from its root directory.
No Raspberry Pi packages need to be installed on Windows.

Target: Raspbian 13 (Trixie), **32-bit armhf userland**, Python 3.13.5,
with a 64-bit aarch64 kernel. Select binaries for the userland and Python ABI;
`uname -m` alone is not a package architecture check. Do not use arm64 wheels.

Confirm the environment on the Pi before selecting dependencies:

```sh
dpkg --print-architecture
/usr/bin/python3 -c "import sys, struct; print(sys.version); print(struct.calcsize('P') * 8)"
apt-cache policy python3-opencv python3-numpy
```

Expected architecture/pointer width: `armhf` and `32`. Camera capture and these
tests require only OpenCV and NumPy; contrib/LBPH is not required for this phase.
Do not install ONNX Runtime, Faster Whisper, or CTranslate2 for this phase.
Avoid pip source builds or assuming a compatible Python 3.13 armhf wheel exists.

First check whether distro OpenCV and NumPy are already available:

```sh
/usr/bin/python3 -c "import cv2, numpy; print(cv2.__version__, numpy.__version__)"
```

If available, expose those existing packages through a separate project venv
without replacing an existing environment or installing packages globally:

```sh
cd ~/eli_robot
/usr/bin/python3 -m venv --system-site-packages .venv-camera-armhf
. .venv-camera-armhf/bin/activate
```

Choose a fresh venv path if that directory already exists. If imports fail,
resolve package availability first; do not install unrelated AI dependencies.
Debian Trixie lists `python3-opencv` for armhf, but the Pi's configured Raspbian
repositories and system Python ABI must be checked locally. Distro OpenCV can
include GUI libraries while still supporting this command without a display.
No dependency installation is performed by this project.

References: [Trixie armhf OpenCV](https://packages.debian.org/trixie/armhf/python3-opencv),
[Raspberry Pi venv guidance](https://www.raspberrypi.com/documentation/computers/os.html).

```sh
cd ~/eli_robot
. .venv-camera-armhf/bin/activate
python -m perception.capture --source /dev/video0 --backend v4l2 --width 640 --height 480 --fps 30 --pixel-format MJPG
```

This prints negotiated backend, format, resolution, FPS, fallback mode and actual
frame dimensions, then saves a timestamped JPEG in the current directory. It
opens no GUI and imports no face model, emotion, audio or hardware modules.
Reported FPS is the driver's value, not a throughput measurement. A successful
capture at a different resolution/format is reported rather than rejected.

For a specific new filename, append `--output pi-camera-test-01.jpg`. Existing
files are refused, including concurrent creation; select a new filename to retry.
The output directory must already exist. Retrieve the image from your laptop:

```sh
scp YOUR_PI_USER@YOUR_PI_HOST:~/eli_robot/camera-*.jpg .
```

If capture fails, check `/dev/video0` exists, the SSH user can access it (usually
through the `video` group), and another application is not using it. Inspect
supported formats with `v4l2-ctl --device /dev/video0 --list-formats-ext` if that
utility is installed. Retry with `--pixel-format default` to skip explicit MJPEG.
Fallbacks reopen the same source/backend using size/FPS only, then driver defaults.
No fallback attempts a different camera device.

Windows headless smoke test, from the project root in your existing environment:

```powershell
python -m perception.capture --source 0 --backend auto
```

The existing `start_camera_feed()` function keeps its face-annotated GUI preview;
it also accepts a `CameraConfig`. `main.py` and the other camera callers have not
yet been migrated in Phase 1. No dataset/model training or registration is run.

Focused automated tests, on either platform:

```sh
python -m unittest tests.test_camera -v
```

These use mock cameras and temporary JPEG files, requiring OpenCV and NumPy but
no attached hardware, GUI, audio packages, or pytest. Actual Pi V4L2 capture must
still be verified on the Pi. A plain Git clone omits ignored local face datasets
and the emotion model; copy those separately for later full application testing.
