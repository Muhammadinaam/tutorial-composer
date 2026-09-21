from __future__ import annotations

import sys

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from app.engine.capture import guess_system_audio, list_dshow_devices
from app.engine.settings import load_settings, save_settings
from app.ui.camera_window import camera_devices


class RecordPage(QWidget):
    startRequested = Signal()
    selectAreaRequested = Signal()
    fullScreenRequested = Signal()
    monitorChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._audio_devices: list[str] = []
        self._filling = False

        hint = QLabel(
            "Pick a monitor, then Select area to resize the capture frame. "
            "Start recording to minimize this window. A camera window stays on top "
            "and is captured with the desktop. F10 stops."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)

        self.monitor_combo = QComboBox()
        self.select_area_btn = QPushButton("Select area")
        self.full_screen_btn = QPushButton("Use full screen")
        self.region_label = QLabel("Full screen")
        self.region_label.setObjectName("hintLabel")

        monitor_row = QHBoxLayout()
        monitor_row.addWidget(self.monitor_combo, 1)
        monitor_row.addWidget(self.select_area_btn)
        monitor_row.addWidget(self.full_screen_btn)

        screen_box = QGroupBox("Screen")
        screen_form = QFormLayout(screen_box)
        screen_form.addRow("Monitor", monitor_row)
        screen_form.addRow("Region", self.region_label)

        self.camera_on = QCheckBox("Show camera window")
        self.camera_combo = QComboBox()
        self.shape_combo = QComboBox()
        self.shape_combo.addItem("Circle", "circle")
        self.shape_combo.addItem("Square", "square")
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(160, 400)
        self.size_slider.setValue(240)
        self.size_label = QLabel("240 px")
        size_row = QHBoxLayout()
        size_row.addWidget(self.size_slider, 1)
        size_row.addWidget(self.size_label)

        camera_box = QGroupBox("Camera")
        camera_form = QFormLayout(camera_box)
        camera_form.addRow(self.camera_on)
        camera_form.addRow("Device", self.camera_combo)
        camera_form.addRow("Shape", self.shape_combo)
        camera_form.addRow("Size", size_row)

        self.mic_on = QCheckBox("Microphone")
        self.mic_combo = QComboBox()
        self.system_on = QCheckBox("System audio")
        self.system_combo = QComboBox()
        self.audio_hint = QLabel()
        self.audio_hint.setObjectName("hintLabel")
        self.audio_hint.setWordWrap(True)

        audio_box = QGroupBox("Audio")
        audio_form = QFormLayout(audio_box)
        audio_form.addRow(self.mic_on, self.mic_combo)
        audio_form.addRow(self.system_on, self.system_combo)
        audio_form.addRow(self.audio_hint)

        self.start_btn = QPushButton("Start recording")
        self.start_btn.setObjectName("accentButton")
        self.start_btn.setMinimumHeight(32)
        self.platform_hint = QLabel()
        self.platform_hint.setObjectName("warningLabel")
        self.platform_hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(hint)
        layout.addWidget(screen_box)
        layout.addWidget(camera_box)
        layout.addWidget(audio_box)
        layout.addWidget(self.start_btn)
        layout.addWidget(self.platform_hint)
        layout.addStretch()

        self.select_area_btn.clicked.connect(lambda: self.selectAreaRequested.emit())
        self.full_screen_btn.clicked.connect(lambda: self.fullScreenRequested.emit())
        self.start_btn.clicked.connect(lambda: self.startRequested.emit())
        self.monitor_combo.currentIndexChanged.connect(self._monitor_changed)
        self.size_slider.valueChanged.connect(self._size_changed)
        self.camera_on.toggled.connect(self._sync_enabled)
        self.mic_on.toggled.connect(self._sync_enabled)
        self.system_on.toggled.connect(self._sync_enabled)
        for widget in (
            self.camera_combo,
            self.shape_combo,
            self.mic_combo,
            self.system_combo,
        ):
            widget.currentIndexChanged.connect(self._persist)
        self.camera_on.toggled.connect(self._persist)
        self.mic_on.toggled.connect(self._persist)
        self.system_on.toggled.connect(self._persist)
        self.size_slider.valueChanged.connect(self._persist)

        if sys.platform != "win32":
            self.platform_hint.setText("Screen recording is currently available on Windows.")
            self.start_btn.setEnabled(False)
        self.reload_devices()
        self.load_prefs()
        self._sync_enabled()

    def reload_devices(self) -> None:
        self._filling = True
        self.monitor_combo.clear()
        screens = QGuiApplication.screens()
        primary = QGuiApplication.primaryScreen()
        for index, screen in enumerate(screens):
            geo = screen.geometry()
            label = f"{screen.name()}  {geo.width()}×{geo.height()}"
            if screen is primary:
                label += "  (primary)"
            self.monitor_combo.addItem(label, index)

        self.camera_combo.clear()
        cams = camera_devices()
        if not cams:
            self.camera_combo.addItem("No camera found", "")
            self.camera_on.setChecked(False)
            self.camera_on.setEnabled(False)
        else:
            self.camera_on.setEnabled(True)
            for cam in cams:
                self.camera_combo.addItem(cam.description(), cam.description())

        videos, audios = list_dshow_devices()
        self._audio_devices = audios
        self.mic_combo.clear()
        self.system_combo.clear()
        if not audios:
            self.mic_combo.addItem("No audio device found", "")
            self.system_combo.addItem("No loopback device found", "")
            self.mic_on.setChecked(False)
            self.system_on.setChecked(False)
            self.audio_hint.setText("FFmpeg did not list DirectShow audio devices.")
        else:
            for name in audios:
                self.mic_combo.addItem(name, name)
                self.system_combo.addItem(name, name)
            loopback = guess_system_audio(audios)
            if loopback:
                self.audio_hint.setText(f"Suggested system audio: {loopback}")
            else:
                self.audio_hint.setText(
                    "No Stereo Mix / loopback device was found. Enable Stereo Mix "
                    "in Windows sound settings, or pick a virtual cable."
                )
        self._filling = False
        _ = videos

    def load_prefs(self) -> None:
        data = load_settings()
        self._filling = True
        monitor = int(data.get("record_monitor", 0) or 0)
        if 0 <= monitor < self.monitor_combo.count():
            self.monitor_combo.setCurrentIndex(monitor)
        self.camera_on.setChecked(bool(data.get("record_camera_enabled", True)) and self.camera_on.isEnabled())
        self._select_combo(self.camera_combo, str(data.get("record_camera_name", "")))
        shape = str(data.get("record_camera_shape", "circle"))
        index = self.shape_combo.findData(shape)
        if index >= 0:
            self.shape_combo.setCurrentIndex(index)
        self.size_slider.setValue(int(data.get("record_camera_size", 240) or 240))
        self.mic_on.setChecked(
            bool(data.get("record_mic_enabled", True)) and bool(self.mic_combo.currentData())
        )
        self._select_combo(self.mic_combo, str(data.get("record_mic_name", "")))
        saved_sys = str(data.get("record_system_audio_name", ""))
        if not saved_sys:
            guessed = guess_system_audio(self._audio_devices)
            if guessed:
                saved_sys = guessed
        self._select_combo(self.system_combo, saved_sys)
        sys_on = bool(data.get("record_system_audio_enabled", False))
        if sys_on and not self.system_combo.currentData():
            sys_on = False
        self.system_on.setChecked(sys_on)
        self._filling = False
        self._size_changed(self.size_slider.value())

    def persist(self, extra: dict | None = None) -> None:
        if self._filling:
            return
        data = load_settings()
        data.update(
            {
                "record_monitor": self.monitor_index(),
                "record_camera_enabled": self.camera_on.isChecked(),
                "record_camera_name": self.camera_combo.currentData() or "",
                "record_camera_shape": self.shape_combo.currentData() or "circle",
                "record_camera_size": self.size_slider.value(),
                "record_mic_enabled": self.mic_on.isChecked(),
                "record_mic_name": self.mic_combo.currentData() or "",
                "record_system_audio_enabled": self.system_on.isChecked(),
                "record_system_audio_name": self.system_combo.currentData() or "",
            }
        )
        if extra:
            data.update(extra)
        save_settings(data)

    def set_region_label(self, rect: QRect | None, full: bool = False) -> None:
        if full or rect is None:
            self.region_label.setText("Full screen")
            return
        self.region_label.setText(f"{rect.width()} × {rect.height()}  at {rect.x()}, {rect.y()}")

    def monitor_index(self) -> int:
        data = self.monitor_combo.currentData()
        return int(data) if data is not None else 0

    def camera_enabled(self) -> bool:
        return self.camera_on.isChecked() and bool(self.camera_combo.currentData())

    def camera_name(self) -> str:
        return str(self.camera_combo.currentData() or "")

    def camera_shape(self) -> str:
        return str(self.shape_combo.currentData() or "circle")

    def camera_size(self) -> int:
        return int(self.size_slider.value())

    def mic_name(self) -> str | None:
        if not self.mic_on.isChecked():
            return None
        name = self.mic_combo.currentData()
        return str(name) if name else None

    def system_audio_name(self) -> str | None:
        if not self.system_on.isChecked():
            return None
        name = self.system_combo.currentData()
        return str(name) if name else None

    def _select_combo(self, combo: QComboBox, value: str) -> None:
        if not value:
            return
        index = combo.findData(value)
        if index < 0:
            index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _monitor_changed(self) -> None:
        if self._filling:
            return
        self.monitorChanged.emit(self.monitor_index())
        self.persist()

    def _size_changed(self, value: int) -> None:
        self.size_label.setText(f"{value} px")

    def _sync_enabled(self) -> None:
        self.camera_combo.setEnabled(self.camera_on.isChecked())
        self.shape_combo.setEnabled(self.camera_on.isChecked())
        self.size_slider.setEnabled(self.camera_on.isChecked())
        self.mic_combo.setEnabled(self.mic_on.isChecked())
        self.system_combo.setEnabled(self.system_on.isChecked())

    def _persist(self, *_args) -> None:
        self.persist()
