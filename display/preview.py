"""Desktop preview window for Eli's eyes (Tkinter, no Raspberry Pi hardware).

Tk must run on the main thread, while the eye engine renders on its worker
thread. ``show()`` therefore only pastes the frame into one preallocated slot
under a lock; ``run()`` (main thread) polls that slot and updates the window.
Closing the window, pressing Esc/q or Ctrl+C makes ``run()`` return.
``run(on_mouse=...)`` reports left-button presses/releases in frame pixels
(window coordinates divided by the zoom), used to simulate touch taps.
"""
import signal
import threading

from hardware.tft_display import DisplayUnavailableError


class PreviewDisplay:
    def __init__(self, width=320, height=240, scale=2, title='Eli - eye preview'):
        if int(scale) < 1:
            raise ValueError('Preview scale must be a positive integer')
        self.width = width
        self.height = height
        self.scale = int(scale)
        self.title = title
        self.is_open = False
        self._lock = threading.Lock()
        self._frame = None
        self._dirty = False
        self._close_requested = threading.Event()
        self._root = self._label = self._photo = None
        self._image_tk = None

    def open(self):
        if self.is_open:
            return self
        try:
            import tkinter
            from PIL import Image, ImageTk
        except ImportError as exc:
            raise DisplayUnavailableError(f'Preview needs tkinter and Pillow ImageTk: {exc}') from exc
        try:
            root = tkinter.Tk()
        except tkinter.TclError as exc:
            raise DisplayUnavailableError(f'No desktop display for the preview window: {exc}') from exc
        root.title(self.title)
        root.configure(background='black')
        root.resizable(False, False)
        size = (self.width * self.scale, self.height * self.scale)
        self._frame = Image.new('RGB', (self.width, self.height))
        self._photo = ImageTk.PhotoImage(Image.new('RGB', size))
        self._label = tkinter.Label(root, image=self._photo, borderwidth=0, background='black')
        self._label.pack()
        root.protocol('WM_DELETE_WINDOW', self.request_close)
        self._root, self._image_tk = root, ImageTk
        self._close_requested.clear()
        self.is_open = True
        return self

    def show(self, image, bbox=None):
        """Called from the animation thread: copy into the slot, never touch Tk."""
        if not self.is_open:
            raise RuntimeError('Preview display is not open')
        with self._lock:
            self._frame.paste(image)
            self._dirty = True

    def clear(self):
        if self._frame is not None:
            with self._lock:
                self._frame.paste((0, 0, 0), (0, 0, self.width, self.height))
                self._dirty = True

    def request_close(self):
        """Thread-safe: ask ``run()`` to return."""
        self._close_requested.set()

    def frame_coords(self, x, y):
        """Window pixel -> frame pixel, clamped to the frame."""
        return (max(0, min(self.width - 1, int(x) // self.scale)),
                max(0, min(self.height - 1, int(y) // self.scale)))

    def run(self, on_key=None, poll_ms=15, on_mouse=None):
        """Run the window on the main thread until it is closed.

        ``on_mouse(kind, x, y)`` gets ``'press'``/``'release'`` in frame pixels.
        """
        if not self.is_open:
            raise RuntimeError('Preview display is not open')
        root = self._root
        if on_key is not None:
            root.bind('<Key>', lambda event: on_key(event.keysym))
        if on_mouse is not None:
            self._label.configure(cursor='hand2')
            self._label.bind('<ButtonPress-1>',
                             lambda event: on_mouse('press', *self.frame_coords(event.x, event.y)))
            self._label.bind('<ButtonRelease-1>',
                             lambda event: on_mouse('release', *self.frame_coords(event.x, event.y)))
        root.bind('<Escape>', lambda event: self.request_close())
        root.bind('q', lambda event: self.request_close())

        def poll():
            if self._close_requested.is_set():
                root.quit()
                return
            with self._lock:
                frame = self._frame.copy() if self._dirty else None
                self._dirty = False
            if frame is not None:
                if self.scale > 1:
                    frame = frame.resize((self.width * self.scale, self.height * self.scale),
                                         resample=0)  # nearest: crisp "pixel" look
                self._photo.paste(frame)
            root.after(poll_ms, poll)

        previous = None
        if threading.current_thread() is threading.main_thread():
            # Tk swallows exceptions in callbacks, so turn Ctrl+C into a close request.
            previous = signal.signal(signal.SIGINT, lambda *_: self.request_close())
        try:
            root.after(poll_ms, poll)
            root.mainloop()
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    def close(self):
        self.is_open = False
        root, self._root = self._root, None
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        self._photo = self._label = None
