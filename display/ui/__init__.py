"""Eli OS touchscreen UI (Phase 1: navigation framework).

Modules:
  theme    colours, status styles, local fonts
  icons    vector icons drawn with Pillow primitives
  widgets  Button, grid layout, hit testing, button drawing
  screens  eyes / menu / placeholder screens and the ScreenRegistry
  manager  UIManager: navigation, touch feedback, dirty-region frames

The UI never talks to the display itself: the EyeEngine worker (the single
display owner) asks ``UIManager.render()`` for a frame on each tick.
"""
from display.ui.manager import UIManager
from display.ui.screens import EYES, MENU, UTILITIES, ScreenRegistry, default_registry

__all__ = ['UIManager', 'ScreenRegistry', 'default_registry', 'UTILITIES', 'EYES', 'MENU']
