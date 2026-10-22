from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.engine.project import BlurRegion
from app.engine.zoom import zoom_crop


class PreviewCanvas(QWidget):
    """Letterboxed preview that can blur rectangles on the current frame."""

    gestureStarted = Signal()
    gestureFinished = Signal()
    blurDrawn = Signal(float, float, float, float)
    blurSelected = Signal(str)
    blurRectEdited = Signal(str, float, float, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewCanvas")
        self.setMinimumHeight(140)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._mode = "empty"
        self._image: QImage | None = None
        self._frame_dirty = False
        self._placeholder = "Add a video or image to the timeline"
        self._regions: list[BlurRegion] = []
        self._selected_id = ""
        self._playhead = 0.0
        self._draw_mode = False
        self._draw_kind = "blur"
        self._drag: dict | None = None

    @property
    def mode(self) -> str:
        return self._mode

    def show_placeholder(self, text: str) -> None:
        self._mode = "empty"
        self._image = None
        self._placeholder = text
        self.update()

    def show_image(self, path: str, fallback: str = "") -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self.show_placeholder(fallback or "Image")
            return
        self._mode = "still"
        self._image = pixmap.toImage()
        self._placeholder = ""
        self.update()

    def show_video(self, *, clear: bool = False) -> None:
        if clear or self._mode != "video":
            self._image = None
        self._mode = "video"
        self._placeholder = ""
        self.update()

    def frame_pending(self) -> bool:
        """True while a frame is waiting to be painted. Later frames can be dropped."""
        return self._frame_dirty

    def set_frame(self, image: QImage) -> None:
        if image.isNull():
            return
        self._mode = "video"
        self._placeholder = ""
        self._image = image
        self._frame_dirty = True
        self.update()

    def set_draw_mode(self, enabled: bool, kind: str = "blur") -> None:
        self._draw_mode = bool(enabled)
        if enabled:
            if kind == "highlight":
                self._draw_kind = "highlight"
            elif kind == "zoom":
                self._draw_kind = "zoom"
            else:
                self._draw_kind = "blur"
        if self._drag is None:
            self.setCursor(Qt.CursorShape.CrossCursor if self._draw_mode else Qt.CursorShape.ArrowCursor)
        self.update()

    def set_regions(self, regions: list[BlurRegion], selected_id: str, playhead: float) -> None:
        self._regions = list(regions)
        self._selected_id = selected_id or ""
        self._playhead = max(0.0, float(playhead))
        self.update()

    def _frame_size(self) -> tuple[int, int]:
        if self._image is not None and not self._image.isNull():
            return max(1, self._image.width()), max(1, self._image.height())
        return 16, 9

    def _content_rect(self) -> QRect:
        fw, fh = self._frame_size()
        avail = self.rect()
        if avail.width() < 2 or avail.height() < 2:
            return QRect()
        scale = min(avail.width() / fw, avail.height() / fh)
        width = max(1, int(fw * scale))
        height = max(1, int(fh * scale))
        x = avail.x() + (avail.width() - width) // 2
        y = avail.y() + (avail.height() - height) // 2
        return QRect(x, y, width, height)

    def _covers(self, blur: BlurRegion) -> bool:
        return float(blur.start) - 0.001 <= self._playhead <= float(blur.end) + 0.001

    def _zoom_edit(self) -> bool:
        """The Zoom tool shows the whole frame so the box can be drawn and resized."""
        return self._draw_mode and self._draw_kind == "zoom"

    def _crop_pixels(self) -> tuple[float, float, float, float]:
        width, height = self._frame_size()
        if self._image is None or self._image.isNull() or self._zoom_edit():
            return 0.0, 0.0, float(width), float(height)
        return zoom_crop(
            [region for region in self._regions if region.is_zoom()],
            self._playhead,
            width,
            height,
        )

    def _view_norm(self, blur: BlurRegion) -> tuple[float, float, float, float]:
        crop_x, crop_y, crop_w, crop_h = self._crop_pixels()
        width, height = self._frame_size()
        if crop_w < 1 or crop_h < 1:
            return blur.x, blur.y, blur.w, blur.h
        return (
            (blur.x * width - crop_x) / crop_w,
            (blur.y * height - crop_y) / crop_h,
            (blur.w * width) / crop_w,
            (blur.h * height) / crop_h,
        )

    def _widget_rect(self, blur: BlurRegion) -> QRect:
        content = self._content_rect()
        x, y, w, h = self._view_norm(blur)
        return QRect(
            int(content.x() + x * content.width()),
            int(content.y() + y * content.height()),
            max(1, int(w * content.width())),
            max(1, int(h * content.height())),
        )

    def _to_norm(self, pos: QPoint, *, clamp: bool) -> tuple[float, float] | None:
        content = self._content_rect()
        if content.width() < 2 or content.height() < 2:
            return None
        vx = (pos.x() - content.x()) / content.width()
        vy = (pos.y() - content.y()) / content.height()
        if not clamp and (vx < -0.02 or vy < -0.02 or vx > 1.02 or vy > 1.02):
            return None
        vx = max(0.0, min(1.0, vx))
        vy = max(0.0, min(1.0, vy))
        crop_x, crop_y, crop_w, crop_h = self._crop_pixels()
        width, height = self._frame_size()
        sx = (crop_x + vx * crop_w) / width
        sy = (crop_y + vy * crop_h) / height
        return max(0.0, min(1.0, sx)), max(0.0, min(1.0, sy))

    def _hit_enabled(self, blur: BlurRegion) -> bool:
        if blur.is_zoom():
            return self._zoom_edit()
        return True

    def _blur_by_id(self, blur_id: str) -> BlurRegion | None:
        for blur in self._regions:
            if blur.id == blur_id:
                return blur
        return None

    def _zone_on(self, blur: BlurRegion, pos: QPoint, handles: bool) -> str:
        rect = self._widget_rect(blur)
        if handles:
            size = 10
            corners = {
                "nw": QRect(rect.left() - 4, rect.top() - 4, size, size),
                "ne": QRect(rect.right() - 6, rect.top() - 4, size, size),
                "sw": QRect(rect.left() - 4, rect.bottom() - 6, size, size),
                "se": QRect(rect.right() - 6, rect.bottom() - 6, size, size),
            }
            for name, handle in corners.items():
                if handle.contains(pos):
                    return name
        if rect.contains(pos):
            return "body"
        return ""

    def _region_at(self, pos: QPoint) -> tuple[BlurRegion, str] | None:
        selected = self._blur_by_id(self._selected_id)
        if selected is not None and self._hit_enabled(selected):
            zone = self._zone_on(selected, pos, handles=True)
            if zone:
                return selected, zone
        for blur in reversed(self._regions):
            if blur is selected or not self._covers(blur) or not self._hit_enabled(blur):
                continue
            zone = self._zone_on(blur, pos, handles=False)
            if zone:
                return blur, zone
        return None

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        self._frame_dirty = False
        painter.fillRect(self.rect(), QColor("#111214"))
        content = self._content_rect()
        if content.width() > 1 and content.height() > 1:
            painter.save()
            painter.setClipRect(content)
        if self._image is not None and not self._image.isNull() and content.width() > 1:
            crop_x, crop_y, crop_w, crop_h = self._crop_pixels()
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(QRectF(content), self._image, QRectF(crop_x, crop_y, crop_w, crop_h))
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            for blur in self._regions:
                if self._covers(blur) and not blur.is_highlight() and not blur.is_zoom():
                    self._paint_blur(painter, blur)
        elif self._placeholder:
            painter.setPen(QColor("#8b9098"))
            painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder)
        for blur in self._regions:
            selected = blur.id == self._selected_id
            if blur.is_zoom() and not self._zoom_edit():
                continue
            if not selected and not self._covers(blur):
                continue
            rect = self._widget_rect(blur)
            highlight = blur.is_highlight()
            zoom = blur.is_zoom()
            if selected and not self._covers(blur):
                if zoom:
                    painter.fillRect(rect, QColor(40, 208, 216, 40))
                elif highlight:
                    painter.fillRect(rect, QColor(255, 59, 48, 40))
                else:
                    painter.fillRect(rect, QColor(160, 90, 220, 40))
            if highlight:
                pen = QPen(QColor("#ffffff") if selected else QColor("#ff3b30"), 4)
            elif zoom:
                pen = QPen(QColor("#ffffff") if selected else QColor("#3ad0d8"), 3)
            else:
                pen = QPen(QColor("#ffffff") if selected else QColor("#d2b4ff"), 2 if selected else 1)
            if selected and not self._covers(blur):
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            if selected:
                painter.setBrush(QColor("#ffffff"))
                painter.setPen(QPen(QColor("#1b1c1f"), 1))
                for handle in self._handle_rects(rect):
                    painter.drawRect(handle)
        if self._drag and self._drag.get("mode") == "draw":
            x0, y0 = self._drag["x0"], self._drag["y0"]
            x1, y1 = self._drag["x1"], self._drag["y1"]
            rect = self._widget_rect(
                BlurRegion(
                    start=0,
                    end=1,
                    x=min(x0, x1),
                    y=min(y0, y1),
                    w=max(0.0, abs(x1 - x0)),
                    h=max(0.0, abs(y1 - y0)),
                    id="draft",
                )
            )
            if self._draw_kind == "highlight":
                painter.fillRect(rect, QColor(255, 59, 48, 40))
                painter.setPen(QPen(QColor("#ff3b30"), 3, Qt.PenStyle.DashLine))
            elif self._draw_kind == "zoom":
                painter.fillRect(rect, QColor(40, 208, 216, 50))
                painter.setPen(QPen(QColor("#3ad0d8"), 3, Qt.PenStyle.DashLine))
            else:
                painter.fillRect(rect, QColor(160, 90, 220, 70))
                painter.setPen(QPen(QColor("#ffffff"), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
        if content.width() > 1 and content.height() > 1:
            painter.restore()

    def _handle_rects(self, rect: QRect) -> list[QRect]:
        size = 8
        return [
            QRect(rect.left() - 3, rect.top() - 3, size, size),
            QRect(rect.right() - 5, rect.top() - 3, size, size),
            QRect(rect.left() - 3, rect.bottom() - 5, size, size),
            QRect(rect.right() - 5, rect.bottom() - 5, size, size),
        ]

    def _paint_blur(self, painter: QPainter, blur: BlurRegion) -> None:
        image = self._image
        if image is None or image.isNull():
            return
        sx = int(blur.x * image.width())
        sy = int(blur.y * image.height())
        sw = max(1, int(blur.w * image.width()))
        sh = max(1, int(blur.h * image.height()))
        sx = max(0, min(image.width() - 1, sx))
        sy = max(0, min(image.height() - 1, sy))
        sw = max(1, min(image.width() - sx, sw))
        sh = max(1, min(image.height() - sy, sh))
        patch = image.copy(sx, sy, sw, sh)
        small = patch.scaled(
            max(2, sw // 14),
            max(2, sh // 14),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        blurred = small.scaled(
            sw,
            sh,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawImage(self._widget_rect(blur), blurred)

    def _cursor_for(self, zone: str) -> Qt.CursorShape:
        if zone in {"nw", "se"}:
            return Qt.CursorShape.SizeFDiagCursor
        if zone in {"ne", "sw"}:
            return Qt.CursorShape.SizeBDiagCursor
        if zone == "body":
            return Qt.CursorShape.SizeAllCursor
        if self._draw_mode:
            return Qt.CursorShape.CrossCursor
        return Qt.CursorShape.ArrowCursor

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        norm = self._to_norm(pos, clamp=False)
        if norm is None:
            return
        hit = self._region_at(pos)
        if hit:
            blur, zone = hit
            self._selected_id = blur.id
            self.blurSelected.emit(blur.id)
            self.gestureStarted.emit()
            if zone == "body":
                self._drag = {
                    "mode": "move",
                    "id": blur.id,
                    "nx": norm[0],
                    "ny": norm[1],
                    "x": blur.x,
                    "y": blur.y,
                    "w": blur.w,
                    "h": blur.h,
                }
            else:
                anchors = {
                    "nw": (blur.x + blur.w, blur.y + blur.h),
                    "ne": (blur.x, blur.y + blur.h),
                    "sw": (blur.x + blur.w, blur.y),
                    "se": (blur.x, blur.y),
                }
                ax, ay = anchors[zone]
                self._drag = {"mode": "resize", "id": blur.id, "ax": ax, "ay": ay}
            self.setCursor(self._cursor_for(zone))
            self.update()
            return
        if self._draw_mode:
            self.gestureStarted.emit()
            self._drag = {"mode": "draw", "x0": norm[0], "y0": norm[1], "x1": norm[0], "y1": norm[1]}
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()
            return
        if self._selected_id:
            self._selected_id = ""
            self.blurSelected.emit("")
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if not self._drag:
            hit = self._region_at(pos)
            self.setCursor(self._cursor_for(hit[1] if hit else ""))
            return
        norm = self._to_norm(pos, clamp=True)
        if norm is None:
            return
        mode = self._drag["mode"]
        if mode == "draw":
            self._drag["x1"], self._drag["y1"] = norm
            self.update()
            return
        blur = self._blur_by_id(self._drag["id"])
        if blur is None:
            return
        if mode == "move":
            blur.x = self._drag["x"] + (norm[0] - self._drag["nx"])
            blur.y = self._drag["y"] + (norm[1] - self._drag["ny"])
            blur.w = self._drag["w"]
            blur.h = self._drag["h"]
        else:
            x0, y0 = self._drag["ax"], self._drag["ay"]
            blur.x = min(x0, norm[0])
            blur.y = min(y0, norm[1])
            blur.w = abs(norm[0] - x0)
            blur.h = abs(norm[1] - y0)
        blur.clamp()
        self.blurRectEdited.emit(blur.id, blur.x, blur.y, blur.w, blur.h)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        drag = self._drag
        self._drag = None
        self.setCursor(Qt.CursorShape.CrossCursor if self._draw_mode else Qt.CursorShape.ArrowCursor)
        if not drag:
            self.update()
            return
        if drag.get("mode") != "draw":
            self.gestureFinished.emit()
            self.update()
            return
        x0, y0 = drag["x0"], drag["y0"]
        x1, y1 = drag["x1"], drag["y1"]
        width = abs(x1 - x0)
        height = abs(y1 - y0)
        self.update()
        if width >= 0.02 and height >= 0.02:
            region = BlurRegion(start=0.0, end=1.0, x=min(x0, x1), y=min(y0, y1), w=width, h=height)
            region.clamp()
            self.blurDrawn.emit(region.x, region.y, region.w, region.h)
        self.gestureFinished.emit()
