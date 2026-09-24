from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtWidgets import QApplication


def _event_bytes(eventType) -> bytes:
    if isinstance(eventType, (bytes, bytearray)):
        return bytes(eventType)
    try:
        return bytes(eventType)
    except Exception:
        return b""


def _message_address(message) -> int | None:
    if isinstance(message, int):
        return message
    try:
        return int(message)
    except Exception:
        return None


class _HotkeyFilter(QAbstractNativeEventFilter):
    """Qt only dispatches this override when the class is not also a QObject."""

    def __init__(self, owner: "WinRecordHotkeys"):
        super().__init__()
        self._owner = owner

    def nativeEventFilter(self, eventType, message):
        return self._owner.handle_native(eventType, message)


class WinRecordHotkeys(QObject):
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
        super().__init__(parent)
        self._registered: set[int] = set()
        self._filter = _HotkeyFilter(self)
        self._installed = False

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
        if app is not None and not self._installed:
            app.installNativeEventFilter(self._filter)
            self._installed = True
        return True

    def unregister(self) -> None:
        if self._registered:
            user32 = ctypes.windll.user32
            for hotkey_id in list(self._registered):
                try:
                    user32.UnregisterHotKey(None, hotkey_id)
                except Exception:
                    pass
            self._registered.clear()
        if self._installed:
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._filter)
            self._installed = False

    def handle_native(self, eventType, message):
        kind = _event_bytes(eventType)
        if kind not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        address = _message_address(message)
        if not address:
            return False, 0
        try:
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(address)
        except Exception:
            return False, 0
        if msg.message != self.WM_HOTKEY or int(msg.wParam) not in self._registered:
            return False, 0
        for hotkey_id, _vk, name in self.BINDS:
            if hotkey_id == int(msg.wParam):
                self.action.emit(name)
                return True, 0
        return False, 0
