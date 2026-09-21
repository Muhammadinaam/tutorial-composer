from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent, QRegion
from PySide6.QtMultimedia import QCamera, QCameraDevice, QMediaCaptureSession, QMediaDevices
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


def camera_devices() -> list[QCameraDevice]:
    return list(QMediaDevices.videoInputs())


def camera_by_name(name: str) -> QCameraDevice | None:
    devices = camera_devices()
    if not name:
        return devices[0] if devices else None
    for device in devices:
        if device.description() == name:
            return device
    lowered = name.lower()
    for device in devices:
        if lowered in device.description().lower():
            return device
    return devices[0] if devices else None


class CameraWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cameraWindow")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._shape = "circle"
        self._size = 240
        self._drag: QPoint | None = None
        self._camera: QCamera | None = None
        self._session = QMediaCaptureSession(self)
        self._device_name = ""
        self._closed = False

        self.video = QVideoWidget(self)
        self.video.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.placeholder = QLabel("Camera")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setObjectName("hintLabel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video)
        self._session.setVideoOutput(self.video)
        self._apply_size()

    def configure(self, shape: str, size: int) -> None:
        self._shape = "circle" if shape == "circle" else "square"
        self._size = max(160, min(420, int(size)))
        self._apply_size()
        self._apply_mask()

    def _apply_size(self) -> None:
        self.setFixedSize(self._size, self._size)

    def _apply_mask(self) -> None:
        if self._shape == "circle":
            self.setMask(QRegion(self.rect(), QRegion.RegionType.Ellipse))
        else:
            self.clearMask()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_mask()

    def open_camera(self, device_name: str = "") -> bool:
        device = camera_by_name(device_name)
        if device is None:
            return False
        self.close_camera()
        self._device_name = device.description()
        self._closed = False
        self._camera = QCamera(device, self)
        self._session.setCamera(self._camera)
        self._camera.start()
        self.show()
        return True

    def hide_preview(self) -> None:
        self.hide()

    def show_preview(self) -> bool:
        if self._closed or self._camera is None:
            return self.open_camera(self._device_name)
        self.show()
        self.raise_()
        return True

    def close_camera(self) -> None:
        self._closed = True
        if self._camera is not None:
            try:
                self._camera.stop()
            except Exception:
                pass
            self._session.setCamera(None)
            self._camera.deleteLater()
            self._camera = None
        self.hide()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag = None
        super().mouseReleaseEvent(event)
