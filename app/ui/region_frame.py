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
# Keep overlay windows this far from recorded pixels. gdigrab/ddagrab paint
# WDA_EXCLUDEFROMCAPTURE windows as solid black, so the controls must sit
# fully outside the capture rect instead of being "excluded".
GAP = 8
MIN_CAPTURE = 80


def _overlay_flags(*extra: Qt.WindowType) -> Qt.WindowType:
    flags = (
        Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint
        | Qt.WindowType.Tool
    )
    no_shadow = getattr(Qt.WindowType, "NoDropShadowWindowHint", None)
    if no_shadow is not None:
        flags |= no_shadow
    for flag in extra:
        flags |= flag
    return flags


def _disable_shadow(widget: QWidget) -> None:
    """Stop the DWM frame from extending into the recorded area."""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi
        disabled = ctypes.c_int(1)  # DWMNCRP_DISABLED
        dwm.DwmSetWindowAttribute(hwnd, 2, ctypes.byref(disabled), ctypes.sizeof(disabled))
        square = ctypes.c_int(1)  # DWMWCP_DONOTROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(square), ctypes.sizeof(square))
    except Exception:
        pass


class OutlineStrip(QWidget):
    """Thin red edge drawn just outside the captured rectangle."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("recordingOutline")
        flags = _overlay_flags(Qt.WindowType.WindowTransparentForInput)
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
        _disable_shadow(self)
        QTimer.singleShot(0, lambda widget=self: _disable_shadow(widget))


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
        self.setWindowFlags(_overlay_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
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
        _disable_shadow(self)
        QTimer.singleShot(0, lambda widget=self: _disable_shadow(widget))

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

    def place_outside(self, hole: QRect, bounds: QRect) -> QRect:
        """Park the bar fully outside `hole`.

        Returns the rectangle that should actually be recorded. When the bar
        cannot fit on screen without covering `hole` (full screen), that
        rectangle is trimmed so the bar sits in the margin and is not captured.
        """
        width = min(max(420, self.sizeHint().width()), max(160, bounds.width()))
        self.setFixedSize(width, BAR_H)
        hole = QRect(hole)

        def clamp_x(preferred: int) -> int:
            return max(bounds.left(), min(preferred, bounds.right() - width + 1))

        def clamp_y(preferred: int) -> int:
            return max(bounds.top(), min(preferred, bounds.bottom() - BAR_H + 1))

        blocked = hole.adjusted(-GAP, -GAP, GAP, GAP)
        x = clamp_x(hole.left())
        y = clamp_y(hole.top())
        candidates = (
            QRect(x, hole.top() - BAR_H - GAP, width, BAR_H),
            QRect(x, hole.bottom() + 1 + GAP, width, BAR_H),
            QRect(hole.left() - width - GAP, y, width, BAR_H),
            QRect(hole.right() + 1 + GAP, y, width, BAR_H),
        )
        for rect in candidates:
            if bounds.contains(rect) and not rect.intersects(blocked):
                self.setGeometry(rect)
                return hole

        # Full-screen (and other tight regions) have no free margin. Sit on the
        # bottom edge and leave that band out of the recording so the panel
        # does not cover the top of the desktop, and does not turn black.
        carved = self._carve(hole, bounds, width, top=False)
        if carved is None:
            carved = self._carve(hole, bounds, width, top=True)
        if carved is not None:
            return carved

        bar = QRect(clamp_x(bounds.left()), bounds.top(), width, BAR_H)
        self.setGeometry(bar)
        trimmed = QRect(hole)
        trimmed.setTop(bar.bottom() + 1 + GAP)
        if trimmed.height() >= MIN_CAPTURE and not bar.intersects(trimmed):
            return trimmed
        return hole

    def _carve(self, hole: QRect, bounds: QRect, width: int, *, top: bool) -> QRect | None:
        if hole.height() <= BAR_H + GAP + MIN_CAPTURE:
            return None
        x = max(bounds.left(), min(hole.left(), bounds.right() - width + 1))
        new_hole = QRect(hole)
        if top:
            bar = QRect(x, max(bounds.top(), hole.top()), width, BAR_H)
            new_hole.setTop(bar.bottom() + 1 + GAP)
        else:
            bar_y = min(hole.bottom(), bounds.bottom()) - BAR_H + 1
            bar = QRect(x, max(bounds.top(), bar_y), width, BAR_H)
            new_hole.setBottom(bar.top() - GAP - 1)
        if new_hole.width() < MIN_W or new_hole.height() < MIN_CAPTURE:
            return None
        if not bounds.contains(bar) or bar.intersects(new_hole):
            return None
        self.setGeometry(bar)
        return new_hole

    def separate_from(self, hole: QRect, bounds: QRect) -> QRect:
        """After show, keep the real window frame (including any border) out of `hole`."""
        frame = self.frameGeometry()
        if not frame.intersects(hole):
            return hole
        geo = QRect(self.geometry())
        overlap = frame.intersected(hole)
        vertical = abs(frame.center().y() - hole.center().y()) >= abs(frame.center().x() - hole.center().x())
        if vertical:
            if frame.center().y() <= hole.center().y():
                geo.moveTop(geo.top() - overlap.height() - GAP)
            else:
                geo.moveTop(geo.top() + overlap.height() + GAP)
        elif frame.center().x() <= hole.center().x():
            geo.moveLeft(geo.left() - overlap.width() - GAP)
        else:
            geo.moveLeft(geo.left() + overlap.width() + GAP)
        if bounds.contains(geo):
            self.setGeometry(geo)
            frame = self.frameGeometry()
            if not frame.intersects(hole):
                return hole
        trimmed = QRect(hole)
        frame = self.frameGeometry()
        vertical = abs(frame.center().y() - hole.center().y()) >= abs(frame.center().x() - hole.center().x())
        if vertical and frame.center().y() <= hole.center().y():
            trimmed.setTop(frame.bottom() + 3)
        elif vertical:
            trimmed.setBottom(frame.top() - 3)
        elif frame.center().x() <= hole.center().x():
            trimmed.setLeft(frame.right() + 3)
        else:
            trimmed.setRight(frame.left() - 3)
        if (
            trimmed.width() >= MIN_W
            and trimmed.height() >= MIN_CAPTURE
            and not frame.intersects(trimmed)
        ):
            return trimmed
        return hole

    def _tick(self) -> None:
        self._elapsed += 1
        minutes, seconds = divmod(self._elapsed, 60)
        self.time_label.setText(f"REC  {minutes}:{seconds:02d}")


class RecordingOutline:
    """Red frame around the capture rect. Strips stay outside the recorded pixels."""

    THICK = 4

    def __init__(self) -> None:
        self._strips = [OutlineStrip() for _ in range(4)]
        self._hole = QRect()
        self._bounds: QRect | None = None

    def show_around(self, hole: QRect, bounds: QRect | None = None) -> None:
        t = self.THICK
        gap = 2
        self._hole = QRect(hole)
        self._bounds = QRect(bounds) if bounds is not None else None
        geos = (
            QRect(hole.left() - t, hole.top() - t - gap, hole.width() + t * 2, t),
            QRect(hole.right() + 1 + gap, hole.top(), t, hole.height()),
            QRect(hole.left() - t, hole.bottom() + 1 + gap, hole.width() + t * 2, t),
            QRect(hole.left() - t - gap, hole.top(), t, hole.height()),
        )
        for strip, geo in zip(self._strips, geos):
            self._place_strip(strip, geo)
        QTimer.singleShot(0, self._hide_overlapping)

    def _place_strip(self, strip: OutlineStrip, geo: QRect) -> None:
        hole = self._hole
        bounds = self._bounds
        if geo.intersects(hole) or (bounds is not None and not bounds.contains(geo)):
            strip.hide()
            return
        strip.setGeometry(geo)
        strip.show()
        strip.raise_()
        actual = strip.frameGeometry()
        if actual.intersects(hole) or (bounds is not None and not bounds.contains(actual)):
            strip.hide()

    def _hide_overlapping(self) -> None:
        hole = getattr(self, "_hole", None)
        if hole is None:
            return
        bounds = self._bounds
        for strip in self._strips:
            if not strip.isVisible():
                continue
            actual = strip.frameGeometry()
            if actual.intersects(hole) or (bounds is not None and not bounds.contains(actual)):
                strip.hide()

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
