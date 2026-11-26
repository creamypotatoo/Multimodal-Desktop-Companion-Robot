"""Eli's animated robotic eyes for the 320x240 ILI9341 TFT (Phase 1).

Modules:
  expressions  expression definitions (pure data)
  animation    time-driven animation state: interpolation, blinks, gaze
  renderer     Pillow frame rendering
  eyes         EyeEngine lifecycle/worker thread and display factory
  preview      Tkinter desktop preview backend
  demo         ``python -m display.demo`` standalone demonstration
  touch        touch calibration, filtering, debouncing, polling thread
  ui/          Eli OS menu: screens, widgets, icons, UIManager
  robot_touch  main.py integration for the touchscreen (ELI_TOUCH_BACKEND)
  ui_demo      ``python -m display.ui_demo`` interactive touch/menu preview

Hardware lives in ``hardware.tft_display`` (ILI9341) and
``hardware.mock_hardware.MockDisplay``, touch in ``hardware.touch_xpt2046``
and ``MockTouch``; all import without a Raspberry Pi.
"""
