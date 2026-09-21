from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPainter, QPen, QScreen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from app.engine.capture import even


HANDLE = 10
MIN_W = 160
MIN_H = 120
BAR_H = 40


def exclude_from_capture(widget: QWidget) -> None:
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x00000011)
    except Exception:
        pass


class OutlineStrip(QWidget):
    """Red bar that is visible on screen but omitted from Windows capture."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("recordingOutline")
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        does_not_focus = getattr(Qt.WindowType, "WindowDoesNotAcceptFocus", None)
        if does_not_focus is not None:
            flags |= does_not_focus
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setStyleSheet("background-color: #e23d3d;")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        exclude_from_capture(self)
        QTimer.singleShot(0, lambda: exclude_from_capture(self))


def screen_at_index(index: int) -> QScreen:
    screens = QGuiApplication.screens()
    if not screens:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            raise RuntimeError("No screen is available.")
        return screen
    return screens[max(0, min(index, len(screens) - 1))]


class RecordingBar(QWidget):
    stopRequested = Signal()
    hideCameraRequested = Signal()
    showCameraRequested = Signal()
    closeCameraRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("recordingBar")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setFixedHeight(BAR_H)
        self._elapsed = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self.time_label = QLabel("REC  0:00")
        self.time_label.setObjectName("recordTimeLabel")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("accentButton")
        self.hide_btn = QPushButton("Hide camera")
        self.show_btn = QPushButton("Show camera")
        self.close_cam_btn = QPushButton("Close camera")
        hint = QLabel("F10")
        hint.setObjectName("hintLabel")

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(6)
        row.addWidget(self.time_label)
        row.addWidget(self.stop_btn)
        row.addWidget(self.hide_btn)
        row.addWidget(self.show_btn)
        row.addWidget(self.close_cam_btn)
        row.addWidget(hint)
        row.addStretch()

        self.stop_btn.clicked.connect(lambda: self.stopRequested.emit())
        self.hide_btn.clicked.connect(lambda: self.hideCameraRequested.emit())
        self.show_btn.clicked.connect(lambda: self.showCameraRequested.emit())
        self.close_cam_btn.clicked.connect(lambda: self.closeCameraRequested.emit())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        exclude_from_capture(self)

    def start_clock(self) -> None:
        self._elapsed = 0
        self.time_label.setText("REC  0:00")
        self._timer.start()

    def stop_clock(self) -> None:
        self._timer.stop()

    def set_camera_enabled(self, enabled: bool) -> None:
        self.hide_btn.setVisible(enabled)
        self.show_btn.setVisible(enabled)
        self.close_cam_btn.setVisible(enabled)

    def set_camera_hidden(self, hidden: bool, closed: bool = False) -> None:
        self.hide_btn.setEnabled(not hidden and not closed)
        self.show_btn.setEnabled(hidden or closed)
        self.close_cam_btn.setEnabled(not closed)

    def place_outside(self, hole: QRect, bounds: QRect) -> None:
        width = max(420, self.sizeHint().width())
        x = hole.x()
        y = hole.y() - BAR_H - 8
        if y < bounds.y():
            y = hole.bottom() + 8
        if y + BAR_H > bounds.bottom():
            y = bounds.y() + 8
            x = hole.right() + 8
        if x + width > bounds.right():
            x = bounds.right() - width - 8
        x = max(bounds.x() + 8, x)
        self.setFixedWidth(min(width, bounds.width() - 16))
        self.move(x, y)

    def _tick(self) -> None:
        self._elapsed += 1
        minutes, seconds = divmod(self._elapsed, 60)
        self.time_label.setText(f"REC  {minutes}:{seconds:02d}")


class RecordingOutline:
    """Red frame around the capture rect. Strips sit outside the hole and are
    excluded from Windows capture so they do not appear in the recording."""

    THICK = 4

    def __init__(self) -> None:
        self._strips = [OutlineStrip() for _ in range(4)]

    def show_around(self, hole: QRect) -> None:
        t = self.THICK
        north, east, south, west = self._strips
        north.setGeometry(hole.x() - t, hole.y() - t, hole.width() + t * 2, t)
        south.setGeometry(hole.x() - t, hole.bottom() + 1, hole.width() + t * 2, t)
        west.setGeometry(hole.x() - t, hole.y(), t, hole.height())
        east.setGeometry(hole.right() + 1, hole.y(), t, hole.height())
        for strip in self._strips:
            strip.show()
            strip.raise_()
            exclude_from_capture(strip)

    def hide(self) -> None:
        for strip in self._strips:
            strip.hide()


class RegionFrame(QWidget):
    regionChanged = Signal()
    regionCommitted = Signal()
    startRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("regionFrame")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self._hole = QRect(80, 80, 960, 540)
        self._drag_mode = ""
        self._drag_origin = QPoint()
        self._hole_start = QRect()
        self._screen_index = 0

        self.toolbar = QWidget(self)
        self.toolbar.setObjectName("recordingBar")
        self.size_label = QLabel("960 × 540")
        self.size_label.setObjectName("hintLabel")
        self.start_btn = QPushButton("Start recording")
        self.start_btn.setObjectName("accentButton")
        self.cancel_btn = QPushButton("Cancel")
        row = QHBoxLayout(self.toolbar)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(6)
        row.addWidget(self.size_label)
        row.addWidget(self.start_btn)
        row.addWidget(self.cancel_btn)
        self.start_btn.clicked.connect(lambda: self.startRequested.emit())
        self.cancel_btn.clicked.connect(lambda: self.cancelRequested.emit())
        self.toolbar.setFixedHeight(BAR_H)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        exclude_from_capture(self)

    def attach_screen(self, index: int, hole: QRect | None = None) -> None:
        screen = screen_at_index(index)
        self._screen_index = index
        geo = screen.geometry()
        self.setGeometry(geo)
        if hole is None or not geo.intersects(hole):
            margin = 48
            self._hole = QRect(
                margin,
                margin,
                even(geo.width() - margin * 2),
                even(geo.height() - margin * 2),
            )
        else:
            local = QRect(hole.x() - geo.x(), hole.y() - geo.y(), hole.width(), hole.height())
            self._hole = self._clamp(local)
        self._refresh_chrome()
        self.show()
        self.raise_()

    def use_full_screen(self, index: int) -> None:
        screen = screen_at_index(index)
        self._screen_index = index
        geo = screen.geometry()
        self.setGeometry(geo)
        self._hole = QRect(0, 0, even(geo.width()), even(geo.height()))
        self._refresh_chrome()
        self.show()
        self.raise_()

    def capture_rect(self) -> QRect:
        geo = self.geometry()
        hole = self._clamp(self._hole)
        return QRect(geo.x() + hole.x(), geo.y() + hole.y(), hole.width(), hole.height())

    def screen_index(self) -> int:
        return self._screen_index

    def _clamp(self, hole: QRect) -> QRect:
        bounds = self.rect()
        width = even(max(MIN_W, min(hole.width(), bounds.width())))
        height = even(max(MIN_H, min(hole.height(), bounds.height())))
        x = max(0, min(hole.x(), bounds.width() - width))
        y = max(0, min(hole.y(), bounds.height() - height))
        return QRect(x, y, width, height)

    def _refresh_chrome(self) -> None:
        hole = self._clamp(self._hole)
        self._hole = hole
        self.size_label.setText(f"{hole.width()} × {hole.height()}")
        bar_w = max(320, self.toolbar.sizeHint().width())
        x = hole.x()
        y = hole.y() - BAR_H - 6
        if y < 0:
            y = min(self.height() - BAR_H, hole.bottom() + 6)
        if x + bar_w > self.width():
            x = max(0, self.width() - bar_w)
        self.toolbar.setGeometry(x, y, bar_w, BAR_H)
        self.update()
        self.regionChanged.emit()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        dim = QColor(0, 0, 0, 110)
        hole = self._clamp(self._hole)
        painter.fillRect(QRect(0, 0, self.width(), hole.y()), dim)
        painter.fillRect(QRect(0, hole.bottom() + 1, self.width(), self.height() - hole.bottom()), dim)
        painter.fillRect(QRect(0, hole.y(), hole.x(), hole.height()), dim)
        painter.fillRect(
            QRect(hole.right() + 1, hole.y(), self.width() - hole.right(), hole.height()),
            dim,
        )
        painter.setPen(QPen(QColor("#3d8fd1"), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(hole.adjusted(1, 1, -1, -1))

    def _hit(self, pos: QPoint) -> str:
        hole = self._clamp(self._hole)
        near_left = abs(pos.x() - hole.left()) <= HANDLE
        near_right = abs(pos.x() - hole.right()) <= HANDLE
        near_top = abs(pos.y() - hole.top()) <= HANDLE
        near_bottom = abs(pos.y() - hole.bottom()) <= HANDLE
        inside_x = hole.left() - HANDLE <= pos.x() <= hole.right() + HANDLE
        inside_y = hole.top() - HANDLE <= pos.y() <= hole.bottom() + HANDLE
        if near_top and near_left:
            return "nw"
        if near_top and near_right:
            return "ne"
        if near_bottom and near_left:
            return "sw"
        if near_bottom and near_right:
            return "se"
        if near_left and inside_y:
            return "w"
        if near_right and inside_y:
            return "e"
        if near_top and inside_x:
            return "n"
        if near_bottom and inside_x:
            return "s"
        if hole.contains(pos):
            return "move"
        return ""

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self.toolbar.geometry().contains(event.position().toPoint()):
            return
        mode = self._hit(event.position().toPoint())
        if not mode:
            return
        self._drag_mode = mode
        self._drag_origin = event.position().toPoint()
        self._hole_start = QRect(self._clamp(self._hole))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if not self._drag_mode:
            mode = self._hit(pos)
            cursors = {
                "n": Qt.CursorShape.SizeVerCursor,
                "s": Qt.CursorShape.SizeVerCursor,
                "e": Qt.CursorShape.SizeHorCursor,
                "w": Qt.CursorShape.SizeHorCursor,
                "ne": Qt.CursorShape.SizeBDiagCursor,
                "sw": Qt.CursorShape.SizeBDiagCursor,
                "nw": Qt.CursorShape.SizeFDiagCursor,
                "se": Qt.CursorShape.SizeFDiagCursor,
                "move": Qt.CursorShape.SizeAllCursor,
            }
            self.setCursor(cursors.get(mode, Qt.CursorShape.ArrowCursor))
            return
        delta = pos - self._drag_origin
        hole = QRect(self._hole_start)
        mode = self._drag_mode
        if mode == "move":
            hole.translate(delta)
        if "n" in mode:
            hole.setTop(self._hole_start.top() + delta.y())
        if "s" in mode:
            hole.setBottom(self._hole_start.bottom() + delta.y())
        if "w" in mode:
            hole.setLeft(self._hole_start.left() + delta.x())
        if "e" in mode:
            hole.setRight(self._hole_start.right() + delta.x())
        self._hole = self._clamp(hole)
        self._refresh_chrome()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag_mode:
            self._drag_mode = ""
            self.regionCommitted.emit()
        super().mouseReleaseEvent(event)
