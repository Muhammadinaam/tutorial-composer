from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QScrollArea, QWidget

from app.engine.edl import clip_joined_start, joined_duration, trim_clip
from app.engine.project import Clip
from app.engine.script import format_timestamp


class TimelineCanvas(QWidget):
    clipSelected = Signal(int)
    playheadMoved = Signal(float)
    playheadReleased = Signal(float)
    clipsTrimmed = Signal()
    musicSelected = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.clips: list[Clip] = []
        self.cue_spans: list[tuple[float, float, str]] = []
        self.selected = -1
        self.music_selected = False
        self.playhead = 0.0
        self.pps = 80.0
        self.music_name = ""
        self.music_duration = 0.0
        self.extra_end = 0.0
        self.mark_in = None
        self.mark_out = None
        self.left_gutter = 40
        self.ruler_h = 16
        self.track_h = 34
        self.track_gap = 4
        self._drag = None

    def set_state(
        self,
        clips: list[Clip],
        selected: int,
        playhead: float,
        cue_spans: list[tuple[float, float, str]] | None = None,
        music_name: str = "",
        music_duration: float = 0.0,
        music_selected: bool = False,
        extra_end: float = 0.0,
        mark_in: float | None = None,
        mark_out: float | None = None,
    ) -> None:
        self.clips = clips
        self.selected = selected
        self.playhead = max(0.0, playhead)
        self.cue_spans = cue_spans or []
        self.music_name = music_name
        self.music_duration = music_duration
        self.music_selected = music_selected
        self.extra_end = extra_end
        self.mark_in = mark_in
        self.mark_out = mark_out
        self._refresh_size()
        self.update()

    def set_playhead(self, seconds: float) -> None:
        self.playhead = max(0.0, seconds)
        self.update()

    def set_zoom(self, pps: float) -> None:
        self.pps = max(8.0, min(240.0, pps))
        self._refresh_size()
        self.update()

    def total_time(self) -> float:
        last_cue = 0.0
        for start, length, _text in self.cue_spans:
            last_cue = max(last_cue, start + length)
        return max(joined_duration(self.clips), last_cue, self.extra_end, 4.0)

    def playhead_x(self) -> int:
        return self._time_to_x(self.playhead)

    def _refresh_size(self) -> None:
        width = self.left_gutter + int(self.total_time() * self.pps) + 80
        height = self.ruler_h + 3 * (self.track_h + self.track_gap) + 10
        self.setMinimumSize(QSize(width, height))
        self.resize(width, height)

    def _time_to_x(self, seconds: float) -> int:
        return int(self.left_gutter + seconds * self.pps)

    def _x_to_time(self, x: int) -> float:
        return max(0.0, (x - self.left_gutter) / self.pps)

    def _track_rect(self, row: int) -> QRect:
        y = self.ruler_h + 4 + row * (self.track_h + self.track_gap)
        width = max(10, self.width() - self.left_gutter - 8)
        return QRect(self.left_gutter, y, width, self.track_h)

    def _clip_rect(self, index: int) -> QRect:
        start = clip_joined_start(self.clips, index)
        width = max(8, int(self.clips[index].used * self.pps))
        track = self._track_rect(0)
        return QRect(self._time_to_x(start), track.y(), width, track.height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#17181b"))

        labels = ["V1", "VO", "M1"]
        painter.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        for row, label in enumerate(labels):
            track = self._track_rect(row)
            painter.setPen(QColor("#8b9098"))
            painter.drawText(QRect(2, track.y(), 36, track.height()), Qt.AlignmentFlag.AlignCenter, label)

        total = self.total_time()
        step = 1.0
        if self.pps < 20:
            step = 10.0
        elif self.pps < 40:
            step = 5.0
        elif self.pps < 70:
            step = 2.0
        t = 0.0
        while t <= total + 0.01:
            x = self._time_to_x(t)
            painter.setPen(QPen(QColor("#3a3c42"), 1))
            painter.drawLine(x, 2, x, self.ruler_h)
            painter.setPen(QColor("#9aa0a8"))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(x + 3, 13, format_timestamp(t))
            t += step

        for row in range(3):
            track = self._track_rect(row)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#222328"))
            painter.drawRoundedRect(track, 6, 6)

        for index, clip in enumerate(self.clips):
            rect = self._clip_rect(index)
            selected = index == self.selected and not self.music_selected
            if clip.is_image:
                fill = QColor("#c4922a") if selected else QColor("#8a6a2d")
            else:
                fill = QColor("#3d8fd1") if selected else QColor("#2c5f8a")
            self._draw_block(painter, rect, fill, selected, f"{'IMG' if clip.is_image else 'VID'}  {clip.name}", format_timestamp(clip.used))

        vo = self._track_rect(1)
        if not self.cue_spans:
            painter.setPen(QColor("#6b7078"))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(vo.adjusted(10, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, "Narration cues appear here")
        for start, length, text in self.cue_spans:
            rect = QRect(self._time_to_x(start), vo.y(), max(10, int(length * self.pps)), vo.height())
            self._draw_block(painter, rect, QColor("#2e7d5b"), False, text, format_timestamp(length))

        music = self._track_rect(2)
        if self.music_name:
            span = joined_duration(self.clips) or self.music_duration or 4.0
            rect = QRect(self._time_to_x(0), music.y(), max(10, int(span * self.pps)), music.height())
            self._draw_block(
                painter,
                rect,
                QColor("#8a3d7a") if self.music_selected else QColor("#5c2d54"),
                self.music_selected,
                f"MUSIC  {self.music_name}",
                "loops to video length",
            )
        else:
            painter.setPen(QColor("#6b7078"))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(music.adjusted(10, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, "Add music for a soundtrack")

        if self.mark_in is not None:
            mx = self._time_to_x(self.mark_in)
            painter.setPen(QPen(QColor("#f4d35e"), 2, Qt.PenStyle.DashLine))
            painter.drawLine(mx, self.ruler_h, mx, self.height())
        if self.mark_out is not None:
            mx = self._time_to_x(self.mark_out)
            painter.setPen(QPen(QColor("#f4d35e"), 2, Qt.PenStyle.DashLine))
            painter.drawLine(mx, self.ruler_h, mx, self.height())

        x = self._time_to_x(self.playhead)
        painter.setPen(QPen(QColor("#ff5c5c"), 2))
        painter.drawLine(x, 0, x, self.height())
        painter.setBrush(QColor("#ff5c5c"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon([QPoint(x - 5, 0), QPoint(x + 5, 0), QPoint(x, 8)])

    def _draw_block(self, painter: QPainter, rect: QRect, fill: QColor, selected: bool, title: str, subtitle: str) -> None:
        painter.setBrush(fill)
        painter.setPen(QPen(QColor("#f2f2f2") if selected else QColor("#1b1c1f"), 1))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)
        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
        painter.drawText(rect.adjusted(5, 2, -5, -12), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, title)
        painter.setFont(QFont("Segoe UI", 7))
        painter.setPen(QColor("#e8e8e8"))
        painter.drawText(rect.adjusted(5, 14, -5, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, subtitle)

    def _hit_clip(self, pos) -> tuple[int, str]:
        for index in range(len(self.clips)):
            rect = self._clip_rect(index)
            if not rect.contains(pos):
                continue
            if pos.x() <= rect.left() + 8:
                return index, "left"
            if pos.x() >= rect.right() - 8:
                return index, "right"
            return index, "body"
        return -1, ""

    def _hit_music(self, pos) -> bool:
        return self.music_name != "" and self._track_rect(2).contains(pos)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        time = self._x_to_time(int(event.position().x()))
        index, zone = self._hit_clip(pos)
        if index >= 0:
            self.selected = index
            self.music_selected = False
            self.clipSelected.emit(index)
            clip = self.clips[index]
            if zone in {"left", "right"}:
                self._drag = {
                    "mode": zone,
                    "index": index,
                    "in": clip.in_point,
                    "out": clip.out_point,
                    "time": time,
                }
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self._drag = {"mode": "playhead"}
                self.playhead = time
                self.playheadMoved.emit(self.playhead)
        elif self._hit_music(pos):
            self.music_selected = True
            self.selected = -1
            self.musicSelected.emit()
            self._drag = {"mode": "playhead"}
            self.playhead = time
            self.playheadMoved.emit(self.playhead)
        else:
            self.music_selected = False
            self._drag = {"mode": "playhead"}
            self.playhead = time
            self.playheadMoved.emit(self.playhead)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag and self._drag.get("mode") == "playhead":
            time = self._x_to_time(int(event.position().x()))
            self.playhead = time
            self.playheadMoved.emit(time)
            self.update()
            return
        if self._drag:
            time = self._x_to_time(int(event.position().x()))
            index = self._drag["index"]
            clip = self.clips[index]
            start = clip_joined_start(self.clips, index)
            if self._drag["mode"] == "left":
                trim_clip(clip, new_in=self._drag["in"] + (time - self._drag["time"]))
            else:
                trim_clip(clip, new_out=max(0.1, time - start + clip.in_point))
            self._refresh_size()
            self.clipsTrimmed.emit()
            self.update()
            return
        index, zone = self._hit_clip(event.position().toPoint())
        self.setCursor(Qt.CursorShape.SizeHorCursor if zone in {"left", "right"} else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        was_playhead = bool(self._drag and self._drag.get("mode") == "playhead")
        self._drag = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if was_playhead:
            self.playheadReleased.emit(self.playhead)


class TimelineWidget(QScrollArea):
    clipSelected = Signal(int)
    playheadMoved = Signal(float)
    playheadReleased = Signal(float)
    clipsTrimmed = Signal()
    musicSelected = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(148)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.follow_playhead = True
        self.canvas = TimelineCanvas()
        self.setWidget(self.canvas)
        self.canvas.clipSelected.connect(self.clipSelected)
        self.canvas.playheadMoved.connect(self.playheadMoved)
        self.canvas.playheadReleased.connect(self.playheadReleased)
        self.canvas.clipsTrimmed.connect(self.clipsTrimmed)
        self.canvas.musicSelected.connect(self.musicSelected)

    def set_state(self, *args, **kwargs) -> None:
        self.canvas.set_state(*args, **kwargs)

    def set_playhead(self, seconds: float) -> None:
        self.canvas.set_playhead(seconds)
        if self.follow_playhead:
            self.ensure_playhead_visible()

    def set_zoom(self, pps: float) -> None:
        self.canvas.set_zoom(pps)

    def zoom_by(self, factor: float) -> None:
        self.set_zoom(self.canvas.pps * factor)
        self.ensure_playhead_visible()

    def fit_zoom(self) -> None:
        total = self.canvas.total_time()
        usable = max(160, self.viewport().width() - self.canvas.left_gutter - 24)
        self.set_zoom(usable / total)

    @property
    def pps(self) -> float:
        return self.canvas.pps

    def ensure_playhead_visible(self) -> None:
        x = self.canvas.playhead_x()
        bar = self.horizontalScrollBar()
        left = bar.value()
        right = left + self.viewport().width()
        margin = 48
        if x < left + margin:
            bar.setValue(max(0, x - margin))
        elif x > right - margin:
            bar.setValue(x - self.viewport().width() + margin)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
            event.accept()
            return
        super().wheelEvent(event)
