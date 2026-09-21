from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtWidgets import QApplication


class WinStopHotkey(QObject, QAbstractNativeEventFilter):
    pressed = Signal()
    HOTKEY_ID = 0x71C0
    WM_HOTKEY = 0x0312
    VK_F10 = 0x79
    MOD_NOREPEAT = 0x4000

    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._registered = False

    def register(self) -> bool:
        if sys.platform != "win32":
            return False
        self.unregister()
        try:
            ok = ctypes.windll.user32.RegisterHotKey(
                None, self.HOTKEY_ID, self.MOD_NOREPEAT, self.VK_F10
            )
        except Exception:
            return False
        if not ok:
            return False
        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self)
        self._registered = True
        return True

    def unregister(self) -> None:
        if not self._registered:
            return
        try:
            ctypes.windll.user32.UnregisterHotKey(None, self.HOTKEY_ID)
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self)
        self._registered = False

    def nativeEventFilter(self, eventType, message):
        kind = eventType if isinstance(eventType, (bytes, bytearray)) else str(eventType).encode()
        if kind not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        try:
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
            self.pressed.emit()
            return True, 0
        return False, 0
