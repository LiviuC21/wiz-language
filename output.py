"""output.py - return focus to the game, then type the translated chat line.

Flow when you press Enter in the overlay:
  1. translate Romanian -> English (Argos, background thread)
  2. re-activate the window that was focused before you clicked the overlay
  3. simulate keystrokes with pynput

NOTE: pynput cannot inject keys into other apps on a native Wayland session.
Log in with an "X11 / Xorg" session on Debian (main.py forces Qt to use XWayland).
"""
from __future__ import annotations

import ctypes
import logging
import platform
import queue
import shutil
import subprocess
import threading
import time
from typing import Optional, Set

from PyQt5 import QtCore

import config
from translation import Translator

log = logging.getLogger(__name__)


class FocusTracker:
    """Remembers the last foreground window that is NOT the overlay."""

    def __init__(self) -> None:
        self.own_ids: Set[int] = set()    # fill with int(overlay.winId())
        self._last: Optional[int] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._system = platform.system()
        self._xdotool = shutil.which("xdotool") if self._system == "Linux" else None
        if self._system == "Windows":
            from ctypes import wintypes

            self._user32 = ctypes.windll.user32
            self._user32.GetForegroundWindow.restype = wintypes.HWND
            self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            self._user32.SetForegroundWindow.restype = wintypes.BOOL
        elif self._system == "Linux" and not self._xdotool:
            log.warning("xdotool not found: will fall back to Alt+Tab to return to the game "
                        "(install with: sudo apt install xdotool)")

    def start(self) -> None:
        threading.Thread(target=self._loop, name="focus-tracker", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _active_window(self) -> Optional[int]:
        try:
            if self._system == "Windows":
                h = self._user32.GetForegroundWindow()
                return int(h) if h else None
            if self._xdotool:
                out = subprocess.run([self._xdotool, "getactivewindow"],
                                     capture_output=True, text=True, timeout=1)
                if out.returncode == 0 and out.stdout.strip():
                    return int(out.stdout.strip())
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
        return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            win = self._active_window()
            if win and win not in self.own_ids:
                with self._lock:
                    self._last = win
            time.sleep(0.3)

    def restore(self) -> bool:
        """Re-activate the remembered window. Returns False if we couldn't."""
        with self._lock:
            target = self._last
        if target is None:
            return False
        try:
            if self._system == "Windows":
                return bool(self._user32.SetForegroundWindow(target))
            if self._xdotool:
                subprocess.run([self._xdotool, "windowactivate", str(target)], timeout=2)
                return True
        except (subprocess.SubprocessError, OSError):
            log.exception("Could not restore focus")
        return False


class OutgoingWorker(QtCore.QThread):
    """Translates typed text and sends it to the game without touching the GUI thread."""

    translated = QtCore.pyqtSignal(str, str)  # (romanian, english)
    status = QtCore.pyqtSignal(str)

    def __init__(self, translator: Translator, focus: FocusTracker) -> None:
        super().__init__()
        self._translator = translator
        self._focus = focus
        self._q: "queue.Queue[str]" = queue.Queue()
        self._stop_flag = False

    def submit(self, text: str) -> None:
        self._q.put(text)

    def stop(self) -> None:
        self._stop_flag = True

    def run(self) -> None:
        self._translator.warmup([(config.CHAT_INPUT_LANG, config.CHAT_OUTPUT_LANG)])
        keyboard = None
        while not self._stop_flag:
            try:
                text = self._q.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                english = self._translator.translate(text, config.CHAT_INPUT_LANG, config.CHAT_OUTPUT_LANG)
                if english is None:
                    self.status.emit(f"Missing pack {config.CHAT_INPUT_LANG}->{config.CHAT_OUTPUT_LANG} (run --setup)")
                    continue
                self.translated.emit(text, english)
                if keyboard is None:
                    from pynput.keyboard import Controller
                    keyboard = Controller()
                self._send(keyboard, english)
            except Exception as exc:  # noqa: BLE001
                log.exception("Sending chat failed")
                self.status.emit(f"Send error: {exc}")

    def _send(self, kb, text: str) -> None:
        from pynput.keyboard import Key

        if not self._focus.restore():
            # Fallback: Alt+Tab usually returns to the previously active window.
            with kb.pressed(Key.alt):
                kb.press(Key.tab)
                kb.release(Key.tab)
        time.sleep(config.REFOCUS_DELAY)

        if config.CHAT_OPEN_KEY:
            key = config.CHAT_OPEN_KEY
            key = getattr(Key, key) if len(key) > 1 else key
            kb.press(key)
            kb.release(key)
            time.sleep(0.12)

        for ch in text:
            kb.type(ch)
            time.sleep(config.TYPE_DELAY)

        if config.PRESS_ENTER_AFTER:
            time.sleep(0.05)
            kb.press(Key.enter)
            kb.release(Key.enter)
