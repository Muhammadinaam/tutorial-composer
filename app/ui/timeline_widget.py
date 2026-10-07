from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QScrollArea, QWidget

from app.engine.edl import clip_joined_start, joined_duration, trim_clip
from app.engine.project import BlurRegion, Clip, Pause
from app.engine.script import format_timestamp


class TimelineCanvas(QWidget):
    clipSelected = Signal(int)
    playheadMoved = Signal(float)
    playheadReleased = Signal(float)
    clipsTrimmed = Signal()
    musicSelected = Signal()
    blurSelected = Signal(str)
    blurEdited = Signal()
    pauseSelected = Signal(str)
    pauseEdited = Signal()
    editStarted = Signal()
    editFinished = Signal()
    clipReordered = Signal(str, int)
    cueSelected = Signal(int)
    cueDragged = Signal(int, float)

    _DRAG_SLOP = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.clips: list[Clip] = []
        self.cue_spans: list[tuple] = []
        self.selected = -1
        self.music_selected = False
        self.playhead = 0.0
        self.pps = 80.0
        self.music_name = ""
        self.music_duration = 0.0
        self.extra_end = 0.0
        self.mark_in = None
        self.mark_out = None
        self.blurs: list[BlurRegion] = []
        self.selected_blur = ""
        self.pauses: list[Pause] = []
        self.selected_pause = ""
        self.selected_cue = -1
        self._edit_open = False
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
        cue_spans: list[tuple] | None = None,
        music_name: str = "",
        music_duration: float = 0.0,
        music_selected: bool = False,
        extra_end: float = 0.0,
        mark_in: float | None = None,
        mark_out: float | None = None,
        blurs: list[BlurRegion] | None = None,
        selected_blur: str = "",
        selected_cue: int = -1,
        pauses: list[Pause] | None = None,
        selected_pause: str = "",
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
        self.blurs = list(blurs or [])
        self.selected_blur = selected_blur or ""
        self.selected_cue = selected_cue
        self.pauses = list(pauses or [])
        self.selected_pause = selected_pause or ""
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
        for start, length, *_rest in self.cue_spans:
            last_cue = max(last_cue, start + length)
        pause_end = 0.0
        for pause in self.pauses:
            pause_end = max(pause_end, float(pause.at) + float(pause.duration))
        return max(joined_duration(self.clips), last_cue, self.extra_end, pause_end, 4.0)

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
            selected = (
                index == self.selected
                and not self.music_selected
                and not self.selected_blur
                and not self.selected_pause
            )
            if clip.is_pause:
                fill = QColor("#e0a000") if selected else QColor("#a87810")
                title = "PAUSE"
            elif clip.is_image:
                fill = QColor("#c4922a") if selected else QColor("#8a6a2d")
                title = f"IMG  {clip.name}"
            else:
                fill = QColor("#3d8fd1") if selected else QColor("#2c5f8a")
                title = f"VID  {clip.name}"
            self._draw_block(painter, rect, fill, selected, title, format_timestamp(clip.used))

        for blur in self.blurs:
            rect = self._blur_bar_rect(blur)
            selected = blur.id == self.selected_blur
            if blur.is_highlight():
                painter.setBrush(QColor(230, 70, 60, 220) if selected else QColor(180, 40, 36, 190))
                painter.setPen(QPen(QColor("#ffffff") if selected else QColor("#3a1010"), 1))
                label = "Box"
            else:
                painter.setBrush(QColor(176, 112, 230, 210) if selected else QColor(120, 70, 180, 170))
                painter.setPen(QPen(QColor("#ffffff") if selected else QColor("#2a1238"), 1))
                label = "Blur"
            painter.drawRoundedRect(rect, 3, 3)
            if rect.width() > 36:
                painter.setPen(QColor("#ffffff"))
                painter.setFont(QFont("Segoe UI", 7, QFont.Weight.DemiBold))
                painter.drawText(rect.adjusted(4, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, label)

        vo = self._track_rect(1)
        if not self.cue_spans:
            painter.setPen(QColor("#6b7078"))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(vo.adjusted(10, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, "Narration cues appear here")
        for index, span in enumerate(self.cue_spans):
            start, length, text, *rest = span
            ready = rest[0] if rest else True
            rect = self._cue_rect(index)
            if ready:
                fill = QColor("#3eaf7a") if index == self.selected_cue else QColor("#2e7d5b")
                subtitle = format_timestamp(length)
            else:
                fill = QColor("#c4922a") if index == self.selected_cue else QColor("#8a6a2d")
                subtitle = "No voice — Generate"
            self._draw_block(painter, rect, fill, index == self.selected_cue, text, subtitle)

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

    def _pause_bar_rect(self, pause: Pause) -> QRect:
        track = self._track_rect(0)
        x = self._time_to_x(pause.at)
        width = max(8, int(pause.duration * self.pps))
        return QRect(x, track.y() + 1, width, 12)

    def _hit_pause(self, pos) -> tuple[Pause, str] | None:
        for pause in reversed(self.pauses):
            rect = self._pause_bar_rect(pause)
            if not rect.contains(pos):
                continue
            edge = 6 if rect.width() > 18 else 0
            if edge and pos.x() >= rect.right() - edge:
                return pause, "right"
            return pause, "body"
        return None

    def _apply_pause_drag(self, time: float) -> None:
        pause = next((item for item in self.pauses if item.id == self._drag["id"]), None)
        if pause is None:
            return
        limit = joined_duration(self.clips)
        origin = self._drag["time"]
        start = float(self._drag["at"])
        duration = float(self._drag["duration"])
        if self._drag["mode"] == "pause-right":
            pause.at = start
            pause.duration = duration + (time - origin)
        else:
            pause.at = start + (time - origin)
            pause.duration = duration
        if limit > 0:
            pause.at = min(pause.at, limit)
        pause.clamp()
        self._refresh_size()
        self.update()
        self.pauseEdited.emit()

    def _clear_pause_selection(self) -> None:
        if not self.selected_pause:
            return
        self.selected_pause = ""
        self.pauseSelected.emit("")

    def _blur_bar_rect(self, blur: BlurRegion) -> QRect:
        track = self._track_rect(0)
        x = self._time_to_x(blur.start)
        width = max(8, int((blur.end - blur.start) * self.pps))
        height = 14
        return QRect(x, track.bottom() - height - 1, width, height)

    def _hit_blur(self, pos) -> tuple[BlurRegion, str] | None:
        for blur in reversed(self.blurs):
            rect = self._blur_bar_rect(blur)
            if not rect.contains(pos):
                continue
            edge = 6 if rect.width() > 18 else 0
            if edge and pos.x() <= rect.left() + edge:
                return blur, "left"
            if edge and pos.x() >= rect.right() - edge:
                return blur, "right"
            return blur, "body"
        return None

    def _apply_blur_drag(self, time: float) -> None:
        blur = next((item for item in self.blurs if item.id == self._drag["id"]), None)
        if blur is None:
            return
        limit = joined_duration(self.clips)
        if limit <= 0.1:
            return
        mode = self._drag["mode"]
        origin = self._drag["time"]
        start = float(self._drag["start"])
        end = float(self._drag["end"])
        minimum = 0.08
        if mode == "blur-left":
            blur.start = min(end - minimum, max(0.0, start + (time - origin)))
            blur.end = end
        elif mode == "blur-right":
            blur.end = max(start + minimum, min(limit, end + (time - origin)))
            blur.start = start
        else:
            duration = max(minimum, end - start)
            moved = start + (time - origin)
            moved = max(0.0, min(max(0.0, limit - duration), moved))
            blur.start = moved
            blur.end = moved + duration
        self.update()
        self.blurEdited.emit()

    def _cue_rect(self, index: int) -> QRect:
        start, length, *_rest = self.cue_spans[index]
        track = self._track_rect(1)
        return QRect(self._time_to_x(start), track.y(), max(10, int(length * self.pps)), track.height())

    def _hit_cue(self, pos) -> int:
        for index in range(len(self.cue_spans) - 1, -1, -1):
            if self._cue_rect(index).contains(pos):
                return index
        return -1

    def _clip_index(self, clip_id: str) -> int:
        for index, clip in enumerate(self.clips):
            if clip.id == clip_id:
                return index
        return -1

    def _reorder_dest(self, x: int, source: int) -> int:
        dest = 0
        for index in range(len(self.clips)):
            if index == source:
                continue
            if x > self._clip_rect(index).center().x():
                dest += 1
        return dest

    def _open_edit(self) -> None:
        if not self._edit_open:
            self._edit_open = True
            self.editStarted.emit()

    def _hit_clip(self, pos) -> tuple[int, str]:
        for index in range(len(self.clips)):
            rect = self._clip_rect(index)
            if not rect.contains(pos):
                continue
            clip = self.clips[index]
            if pos.x() <= rect.left() + 8 and not clip.is_pause:
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
        pause_hit = self._hit_pause(pos)
        if pause_hit:
            pause, zone = pause_hit
            self.selected_pause = pause.id
            self.selected_blur = ""
            self.selected_cue = -1
            self.music_selected = False
            self.blurSelected.emit("")
            self.pauseSelected.emit(pause.id)
            self._open_edit()
            self._drag = {
                "mode": f"pause-{zone}",
                "id": pause.id,
                "at": pause.at,
                "duration": pause.duration,
                "time": time,
            }
            self.setCursor(
                Qt.CursorShape.SizeHorCursor if zone == "right" else Qt.CursorShape.SizeAllCursor
            )
            self.update()
            return
        self._clear_pause_selection()
        blur_hit = self._hit_blur(pos)
        if blur_hit:
            blur, zone = blur_hit
            self.selected_blur = blur.id
            self.selected_cue = -1
            self.music_selected = False
            self.blurSelected.emit(blur.id)
            self._open_edit()
            self._drag = {
                "mode": f"blur-{zone}",
                "id": blur.id,
                "start": blur.start,
                "end": blur.end,
                "time": time,
            }
            self.setCursor(
                Qt.CursorShape.SizeHorCursor if zone in {"left", "right"} else Qt.CursorShape.SizeAllCursor
            )
            self.update()
            return
        if self.selected_blur:
            self.selected_blur = ""
            self.blurSelected.emit("")
        index, zone = self._hit_clip(pos)
        if index >= 0:
            self.selected = index
            self.selected_cue = -1
            self.music_selected = False
            clip = self.clips[index]
            self.clipSelected.emit(index)
            if zone in {"left", "right"}:
                self._open_edit()
                self._drag = {
                    "mode": zone,
                    "index": index,
                    "in": clip.in_point,
                    "out": clip.out_point,
                    "time": time,
                }
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif not clip.is_pause:
                self._drag = {
                    "mode": "clip-pending",
                    "id": clip.id,
                    "press_x": pos.x(),
                }
            self.update()
            return
        cue_index = self._hit_cue(pos)
        if cue_index >= 0:
            self.selected_cue = cue_index
            self.selected = -1
            self.music_selected = False
            start = float(self.cue_spans[cue_index][0])
            self._drag = {
                "mode": "cue-pending",
                "index": cue_index,
                "anchor": start,
                "press_time": time,
                "press_x": pos.x(),
            }
            self.cueSelected.emit(cue_index)
            self.update()
            return
        self.selected_cue = -1
        if self._hit_music(pos):
            self.music_selected = True
            self.selected = -1
            self.musicSelected.emit()
        else:
            self.music_selected = False
        self._drag = {"mode": "playhead"}
        self.playhead = time
        self.playheadMoved.emit(self.playhead)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        x = pos.x()
        time = self._x_to_time(x)
        drag = self._drag
        if drag and str(drag.get("mode", "")).startswith("pause"):
            self._apply_pause_drag(time)
            return
        if drag and str(drag.get("mode", "")).startswith("blur"):
            self._apply_blur_drag(time)
            return
        if drag and drag.get("mode") == "playhead":
            self.playhead = time
            self.playheadMoved.emit(time)
            self.update()
            return
        if drag and drag.get("mode") in {"left", "right"}:
            index = drag["index"]
            if not (0 <= index < len(self.clips)):
                return
            clip = self.clips[index]
            start = clip_joined_start(self.clips, index)
            if drag["mode"] == "left":
                trim_clip(clip, new_in=drag["in"] + (time - drag["time"]))
            elif clip.is_pause:
                trim_clip(clip, new_out=max(0.2, time - start))
            else:
                trim_clip(clip, new_out=max(0.1, time - start + clip.in_point))
            self._refresh_size()
            self.clipsTrimmed.emit()
            self.update()
            return
        if drag and drag.get("mode") == "clip-pending":
            if abs(x - int(drag["press_x"])) < self._DRAG_SLOP:
                return
            drag["mode"] = "reorder"
            self._open_edit()
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        if drag and drag.get("mode") == "reorder":
            index = self._clip_index(str(drag.get("id", "")))
            if index < 0:
                return
            dest = self._reorder_dest(x, index)
            if dest != index:
                self.clipReordered.emit(str(drag["id"]), dest)
            return
        if drag and drag.get("mode") == "cue-pending":
            if abs(x - int(drag["press_x"])) < self._DRAG_SLOP:
                return
            drag["mode"] = "cue"
            self._open_edit()
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        if drag and drag.get("mode") == "cue":
            new_time = max(0.0, float(drag["anchor"]) + (time - float(drag["press_time"])))
            index = int(drag["index"])
            if 0 <= index < len(self.cue_spans):
                _start, length, text, *rest = self.cue_spans[index]
                ready = rest[0] if rest else True
                self.cue_spans[index] = (new_time, length, text, ready)
                self._refresh_size()
                self.update()
                self.cueDragged.emit(index, new_time)
            return
        pause_hit = self._hit_pause(pos)
        if pause_hit:
            zone = pause_hit[1]
            self.setCursor(
                Qt.CursorShape.SizeHorCursor if zone == "right" else Qt.CursorShape.SizeAllCursor
            )
            return
        blur_hit = self._hit_blur(pos)
        if blur_hit:
            zone = blur_hit[1]
            self.setCursor(
                Qt.CursorShape.SizeHorCursor if zone in {"left", "right"} else Qt.CursorShape.SizeAllCursor
            )
            return
        index, zone = self._hit_clip(pos)
        if zone in {"left", "right"}:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif zone == "body" or self._hit_cue(pos) >= 0:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        was_playhead = bool(self._drag and self._drag.get("mode") == "playhead")
        was_blur = bool(self._drag and str(self._drag.get("mode", "")).startswith("blur"))
        was_pause = bool(self._drag and str(self._drag.get("mode", "")).startswith("pause"))
        was_edit = self._edit_open
        self._drag = None
        self._edit_open = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if was_blur:
            self.blurEdited.emit()
        if was_pause:
            self.pauseEdited.emit()
        if was_playhead:
            self.playheadReleased.emit(self.playhead)
        if was_edit:
            self.editFinished.emit()


class TimelineWidget(QScrollArea):
    clipSelected = Signal(int)
    playheadMoved = Signal(float)
    playheadReleased = Signal(float)
    clipsTrimmed = Signal()
    musicSelected = Signal()
    blurSelected = Signal(str)
    blurEdited = Signal()
    pauseSelected = Signal(str)
    pauseEdited = Signal()
    editStarted = Signal()
    editFinished = Signal()
    clipReordered = Signal(str, int)
    cueSelected = Signal(int)
    cueDragged = Signal(int, float)

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
        self.canvas.blurSelected.connect(self.blurSelected)
        self.canvas.blurEdited.connect(self.blurEdited)
        self.canvas.pauseSelected.connect(self.pauseSelected)
        self.canvas.pauseEdited.connect(self.pauseEdited)
        self.canvas.editStarted.connect(self.editStarted)
        self.canvas.editFinished.connect(self.editFinished)
        self.canvas.clipReordered.connect(self.clipReordered)
        self.canvas.cueSelected.connect(self.cueSelected)
        self.canvas.cueDragged.connect(self.cueDragged)

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
