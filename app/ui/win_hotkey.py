from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtWidgets import QApplication


class WinRecordHotkeys(QObject, QAbstractNativeEventFilter):
    """System-wide keys that work while this window is minimized."""

    action = Signal(str)
    WM_HOTKEY = 0x0312
    MOD_NOREPEAT = 0x4000
    # id, virtual key, action name
    BINDS = (
        (0x71C0, 0x79, "stop"),  # F10
        (0x71C1, 0x77, "hide-camera"),  # F8
        (0x71C2, 0x78, "show-camera"),  # F9
        (0x71C3, 0x76, "close-camera"),  # F7
    )

    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        QAbstractNativeEventFilter.__init__(self)
        self._registered: set[int] = set()

    def register(self) -> bool:
        if sys.platform != "win32":
            return False
        self.unregister()
        user32 = ctypes.windll.user32
        for hotkey_id, vk, _name in self.BINDS:
            try:
                ok = user32.RegisterHotKey(None, hotkey_id, self.MOD_NOREPEAT, vk)
            except Exception:
                ok = False
            if ok:
                self._registered.add(hotkey_id)
        if not self._registered:
            return False
        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self)
        return True

    def unregister(self) -> None:
        if not self._registered:
            return
        user32 = ctypes.windll.user32
        for hotkey_id in list(self._registered):
            try:
                user32.UnregisterHotKey(None, hotkey_id)
            except Exception:
                pass
        self._registered.clear()
        app = QApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self)

    def nativeEventFilter(self, eventType, message):
        kind = eventType if isinstance(eventType, (bytes, bytearray)) else str(eventType).encode()
        if kind not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        try:
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
        except Exception:
            return False, 0
        if msg.message != self.WM_HOTKEY or int(msg.wParam) not in self._registered:
            return False, 0
        for hotkey_id, _vk, name in self.BINDS:
            if hotkey_id == int(msg.wParam):
                self.action.emit(name)
                return True, 0
        return False, 0
