"""Eli's animated robotic eyes for the 320x240 ILI9341 TFT (Phase 1).

Modules:
  expressions  expression definitions (pure data)
  animation    time-driven animation state: interpolation, blinks, gaze
  renderer     Pillow frame rendering
  eyes         EyeEngine lifecycle/worker thread and display factory
  preview      Tkinter desktop preview backend
  demo         ``python -m display.demo`` standalone demonstration

Hardware lives in ``hardware.tft_display`` (ILI9341) and
``hardware.mock_hardware.MockDisplay``; both import without a Raspberry Pi.
"""
