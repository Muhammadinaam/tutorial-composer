from __future__ import annotations

from pathlib import Path
from time import monotonic

from PySide6.QtCore import QUrl, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeyEvent, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from app.engine.edl import (
    clip_joined_start,
    delete_joined_range,
    joined_duration,
    ripple_cues,
    map_joined_to_clip,
    probe_audio,
    probe_clip,
    split_clip,
)
from app.engine.export import export_project
from app.engine.ffmpeg import ffmpeg_available
from app.engine.project import Cue, Narration, Project, load_project, save_project
from app.engine.script import (
    estimate_speech_seconds,
    format_timestamp,
    parse_timestamp,
)
from app.engine.settings import cache_dir, load_settings, save_settings
from app.engine.timeline import TimelinePlan, build_timeline
from app.engine.translate import LANGUAGE_NAMES, translate_cues
from app.engine.tts import synthesize, synthesize_with_duration
from app.engine.voices import (
    fallback_edge_voices,
    languages_from_voices,
    list_voices,
    matching_voice,
)
from app.ui.settings_dialog import SettingsDialog
from app.ui.timeline_widget import TimelineWidget
from app.ui.workers import TaskWorker


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tutorial Composer")
        self.resize(1180, 760)
        self.project = Project()
        self.voices = []
        self.tts_paths: list[Path] = []
        self.tts_durations: list[float] = []
        self.plan: TimelinePlan | None = None
        self.worker: TaskWorker | None = None
        self.selected_index = -1
        self.playhead = 0.0
        self._playing = False
        self._seeking = False
        self._slider_dragging = False
        self._scrubbing = False
        self._last_live_seek = 0.0
        self._seek_target_ms = None
        self._narration_index = -1
        self._narration_pending = None
        self._narration_finished = -1
        self._narration_gen = 0
        self._narration_token = 0
        self._audio_hold = False
        self._hold_at = 0.0
        self._hold_started = 0.0
        self._joining = False
        self._music_selected = False
        self.tts_store: dict[str, tuple] = {}
        self._loading_table = False
        self._switching_lang = False

        self._preview_player = QMediaPlayer(self)
        self._preview_audio = QAudioOutput(self)
        self._preview_player.setAudioOutput(self._preview_audio)

        self.narration_player = QMediaPlayer(self)
        self.narration_audio = QAudioOutput(self)
        self.narration_audio.setVolume(1.0)
        self.narration_player.setAudioOutput(self.narration_audio)
        self._preview_audio.setVolume(1.0)

        self.music_player = QMediaPlayer(self)
        self.music_audio = QAudioOutput(self)
        self.music_player.setAudioOutput(self.music_audio)

        self._image_clock = QTimer(self)
        self._image_clock.setInterval(33)
        self._image_clock.timeout.connect(self._tick_image)
        self._seek_unlock = QTimer(self)
        self._seek_unlock.setSingleShot(True)
        self._seek_unlock.timeout.connect(self._unlock_seek)
        self._narration_retry = QTimer(self)
        self._narration_retry.setSingleShot(True)
        self._narration_retry.timeout.connect(self._retry_narration)

        self._build_menu()
        self._build_ui()
        self._bind()
        self._refresh_ffmpeg_status()
        self.voices = fallback_edge_voices()
        self.reload_voices(background=True)
        self._apply_mute()
        self.refresh_all()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        for title, slot, shortcut in (
            ("New", self.new_project, "Ctrl+N"),
            ("Open...", self.open_project, "Ctrl+O"),
            ("Save", self.save_project_dialog, "Ctrl+S"),
            ("Save As...", self.save_project_as, "Ctrl+Shift+S"),
        ):
            action = QAction(title, self)
            action.setShortcut(shortcut)
            action.triggered.connect(slot)
            file_menu.addAction(action)
        file_menu.addSeparator()
        export_action = QAction("Export MP4...", self)
        export_action.setShortcut("Ctrl+E")
        export_action.triggered.connect(self.export_video)
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        quit_action = QAction("Exit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        settings_menu = self.menuBar().addMenu("App")
        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self.open_settings)
        settings_menu.addAction(settings_action)

    def _panel(self) -> QWidget:
        frame = QWidget()
        frame.setObjectName("panel")
        return frame

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        title = QLabel("Tutorial Composer")
        title.setObjectName("titleLabel")
        self.ffmpeg_label = QLabel()
        self.ffmpeg_label.setObjectName("hintLabel")
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.ffmpeg_label)
        settings_btn = QPushButton("Settings")
        settings_btn.clicked.connect(self.open_settings)
        header.addWidget(settings_btn)
        outer.addLayout(header)

        vertical = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(vertical, 1)

        top = QSplitter(Qt.Orientation.Horizontal)
        preview = self._panel()
        preview_box = QVBoxLayout(preview)
        preview_box.setContentsMargins(8, 8, 8, 8)
        preview_box.setSpacing(4)
        self.preview_stack = QStackedWidget()
        self.video_widget = QVideoWidget()
        self.image_label = QLabel("Add a video or image to the timeline")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setObjectName("hintLabel")
        self.image_label.setMinimumHeight(140)
        self.preview_stack.addWidget(self.video_widget)
        self.preview_stack.addWidget(self.image_label)
        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        self.player.setVideoOutput(self.video_widget)
        preview_box.addWidget(self.preview_stack, 1)

        controls = QHBoxLayout()
        self.play_btn = QPushButton("Play")
        self.time_label = QLabel("0:00 / 0:00")
        self.mute_box = QCheckBox("Mute original audio")
        self.mute_box.setChecked(True)
        self.mute_box.setToolTip("Silence the recording's own sound while you preview.")
        controls.addWidget(self.play_btn)
        controls.addWidget(self.time_label)
        controls.addStretch()
        controls.addWidget(self.mute_box)
        preview_box.addLayout(controls)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        preview_box.addWidget(self.slider)
        top.addWidget(preview)

        side = QWidget()
        side_box = QVBoxLayout(side)
        side_box.setContentsMargins(0, 0, 0, 0)
        side_box.setSpacing(6)

        script_panel = self._panel()
        script_box = QVBoxLayout(script_panel)
        script_box.setContentsMargins(8, 6, 8, 6)
        script_box.setSpacing(4)
        voice_row = QHBoxLayout()
        voice_row.setSpacing(4)
        self.lang_combo = QComboBox()
        self.voice_combo = QComboBox()
        self.preview_voice_btn = QPushButton("Hear")
        self.preview_voice_btn.setToolTip("Play a short sample of this voice")
        voice_row.addWidget(self.lang_combo, 1)
        voice_row.addWidget(self.voice_combo, 2)
        voice_row.addWidget(self.preview_voice_btn)
        script_box.addLayout(voice_row)
        tab_row = QHBoxLayout()
        tab_row.setSpacing(4)
        self.lang_tabs = QTabBar()
        self.lang_tabs.setExpanding(False)
        self.lang_tabs.setDocumentMode(True)
        self.lang_tabs.setTabsClosable(True)
        self.lang_tabs.setDrawBase(False)
        add_lang_btn = QPushButton("+ Lang")
        add_lang_btn.setToolTip("Add another language tab (English, Urdu, …)")
        add_lang_btn.clicked.connect(self.add_language_tab)
        tab_row.addWidget(self.lang_tabs, 1)
        tab_row.addWidget(add_lang_btn)
        script_box.addLayout(tab_row)
        self.cue_table = QTableWidget(0, 2)
        self.cue_table.setHorizontalHeaderLabels(["Time", "Text"])
        self.cue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.cue_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.cue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cue_table.verticalHeader().setVisible(False)
        self.cue_table.verticalHeader().setDefaultSectionSize(22)
        self.cue_table.setShowGrid(False)
        script_box.addWidget(self.cue_table, 1)
        cue_btns = QHBoxLayout()
        cue_btns.setSpacing(4)
        self.add_cue_btn = QPushButton("Add at playhead")
        self.remove_cue_btn = QPushButton("Remove")
        cue_btns.addWidget(self.add_cue_btn)
        cue_btns.addWidget(self.remove_cue_btn)
        script_box.addLayout(cue_btns)
        self.warning_label = QLabel()
        self.warning_label.setObjectName("warningLabel")
        self.warning_label.setWordWrap(True)
        script_box.addWidget(self.warning_label)
        script_btns = QHBoxLayout()
        script_btns.setSpacing(4)
        self.generate_btn = QPushButton("Generate voices")
        self.generate_btn.setObjectName("accentButton")
        self.translate_btn = QPushButton("Translate")
        script_btns.addWidget(self.generate_btn)
        script_btns.addWidget(self.translate_btn)
        script_box.addLayout(script_btns)
        side_box.addWidget(script_panel, 1)
        top.addWidget(side)
        top.setStretchFactor(0, 3)
        top.setStretchFactor(1, 2)
        vertical.addWidget(top)

        timeline_panel = self._panel()
        tl_box = QVBoxLayout(timeline_panel)
        tl_box.setContentsMargins(8, 6, 8, 6)
        tl_box.setSpacing(4)
        tools = QHBoxLayout()
        tools.setSpacing(4)
        self.add_video_btn = QPushButton("Add video")
        self.add_image_btn = QPushButton("Add image")
        self.add_music_btn = QPushButton("Add music")
        self.remove_music_btn = QPushButton("Remove music")
        self.split_btn = QPushButton("Split")
        self.split_btn.setToolTip("Cut the selected clip at the red playhead (S)")
        self.mark_in_btn = QPushButton("Mark In")
        self.mark_out_btn = QPushButton("Mark Out")
        self.delete_range_btn = QPushButton("Delete In→Out")
        self.delete_range_btn.setToolTip("Ripple-delete the middle between yellow marks.")
        self.delete_btn = QPushButton("Delete clip")
        self.delete_btn.setToolTip("Remove the selected video/image piece, or selected music.")
        for btn in (
            self.add_video_btn,
            self.add_image_btn,
            self.add_music_btn,
            self.remove_music_btn,
            self.split_btn,
            self.mark_in_btn,
            self.mark_out_btn,
            self.delete_range_btn,
            self.delete_btn,
        ):
            tools.addWidget(btn)
        tools.addStretch()
        self.zoom_out_btn = QPushButton("−")
        self.zoom_in_btn = QPushButton("+")
        self.fit_btn = QPushButton("Fit")
        self.follow_box = QCheckBox("Follow playhead")
        self.follow_box.setChecked(True)
        tools.addWidget(self.zoom_out_btn)
        tools.addWidget(self.zoom_in_btn)
        tools.addWidget(self.fit_btn)
        tools.addWidget(self.follow_box)
        tl_box.addLayout(tools)

        mix_row = QHBoxLayout()
        mix_row.setSpacing(6)
        mix_row.addWidget(QLabel("Voice"))
        self.voice_volume = QSlider(Qt.Orientation.Horizontal)
        self.voice_volume.setRange(0, 100)
        self.voice_volume.setValue(100)
        self.voice_volume.setMaximumWidth(140)
        self.voice_volume.setToolTip("Narration loudness. Tutorials default to full volume.")
        self.voice_vol_label = QLabel("100%")
        mix_row.addWidget(self.voice_volume)
        mix_row.addWidget(self.voice_vol_label)
        mix_row.addSpacing(12)
        mix_row.addWidget(QLabel("Music"))
        self.music_volume = QSlider(Qt.Orientation.Horizontal)
        self.music_volume.setRange(0, 100)
        self.music_volume.setValue(20)
        self.music_volume.setMaximumWidth(140)
        self.music_volume.setToolTip("Background soundtrack level")
        self.music_vol_label = QLabel("20%")
        self.music_name_label = QLabel("No soundtrack")
        self.music_name_label.setObjectName("hintLabel")
        mix_row.addWidget(self.music_volume)
        mix_row.addWidget(self.music_vol_label)
        mix_row.addWidget(self.music_name_label, 1)
        self.selected_label = QLabel("No clip selected")
        self.selected_label.setObjectName("hintLabel")
        mix_row.addWidget(self.selected_label)
        tl_box.addLayout(mix_row)
        self.timeline = TimelineWidget()
        self.timeline.setToolTip(
            "Mark In / Mark Out / Delete In→Out to cut the middle. "
            "Or Split twice and Delete clip. Drag the red line to scrub."
        )
        tl_box.addWidget(self.timeline)
        vertical.addWidget(timeline_panel)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 1)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(8)
        self.export_btn = QPushButton("Export MP4")
        self.export_btn.setObjectName("accentButton")
        self.status_label = QLabel("Add a video to the timeline to begin.")
        self.status_label.setObjectName("hintLabel")
        bottom.addWidget(self.export_btn)
        bottom.addWidget(self.status_label, 1)
        outer.addLayout(bottom)
        self.statusBar().showMessage("Ready")

    def _bind(self) -> None:
        self.play_btn.clicked.connect(self.toggle_play)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_play_state)
        self.player.mediaStatusChanged.connect(self._video_status)
        self.slider.sliderPressed.connect(self._slider_pressed)
        self.slider.sliderReleased.connect(self._seek_from_slider)
        self.slider.sliderMoved.connect(self._slider_scrub)
        self.slider.valueChanged.connect(self._slider_value)
        self.narration_player.mediaStatusChanged.connect(self._narration_status)
        self.narration_player.playbackStateChanged.connect(self._narration_play_state)
        self.narration_player.errorOccurred.connect(self._narration_error)
        self.add_video_btn.clicked.connect(self.add_videos)
        self.add_image_btn.clicked.connect(self.add_images)
        self.add_music_btn.clicked.connect(self.add_music)
        self.remove_music_btn.clicked.connect(self.remove_music)
        self.delete_btn.clicked.connect(self.remove_clip)
        self.split_btn.clicked.connect(self.split_at_playhead)
        self.mark_in_btn.clicked.connect(self.mark_in)
        self.mark_out_btn.clicked.connect(self.mark_out)
        self.delete_range_btn.clicked.connect(self.delete_marked_range)
        self.add_cue_btn.clicked.connect(self.add_cue_at_playhead)
        self.remove_cue_btn.clicked.connect(self.remove_selected_cue)
        self.cue_table.itemChanged.connect(self._table_changed)
        self.lang_tabs.currentChanged.connect(self._lang_tab_changed)
        self.lang_tabs.tabCloseRequested.connect(self._close_lang_tab)
        self.zoom_in_btn.clicked.connect(lambda: self.timeline.zoom_by(1.25))
        self.zoom_out_btn.clicked.connect(lambda: self.timeline.zoom_by(0.8))
        self.fit_btn.clicked.connect(self.timeline.fit_zoom)
        self.follow_box.toggled.connect(self._follow_changed)
        self.voice_volume.valueChanged.connect(self._voice_volume_changed)
        self.music_volume.valueChanged.connect(self._music_volume_changed)
        self.timeline.musicSelected.connect(self._music_track_selected)
        self.mute_box.toggled.connect(self._mute_changed)
        self.lang_combo.currentIndexChanged.connect(self._lang_changed)
        self.voice_combo.currentIndexChanged.connect(self._voice_changed)
        self.preview_voice_btn.clicked.connect(self.preview_voice)
        self.generate_btn.clicked.connect(self.generate_voices)
        self.translate_btn.clicked.connect(self.translate_script)
        self.export_btn.clicked.connect(self.export_video)
        self.timeline.clipSelected.connect(self._timeline_clip_selected)
        self.timeline.playheadMoved.connect(self._timeline_seek)
        self.timeline.playheadReleased.connect(self._timeline_seek_done)
        self.timeline.clipsTrimmed.connect(self._after_trim)
        self._warn_timer = QTimer(self)
        self._warn_timer.setSingleShot(True)
        self._warn_timer.setInterval(400)
        self._warn_timer.timeout.connect(self.refresh_warnings)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.cue_table.hasFocus() or self.voice_combo.hasFocus() or self.lang_combo.hasFocus():
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key.Key_I:
            self.mark_in()
            event.accept()
            return
        if key == Qt.Key.Key_O:
            self.mark_out()
            event.accept()
            return
        if key == Qt.Key.Key_Space:
            self.toggle_play()
            event.accept()
            return
        if key == Qt.Key.Key_S:
            self.split_at_playhead()
            event.accept()
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.remove_clip()
            event.accept()
            return
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.timeline.zoom_by(1.25)
            event.accept()
            return
        if key == Qt.Key.Key_Minus:
            self.timeline.zoom_by(0.8)
            event.accept()
            return
        super().keyPressEvent(event)

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _start_worker(self, fn, on_ok) -> None:
        if self._busy():
            self.statusBar().showMessage("Please wait for the current task to finish.")
            return
        self.worker = TaskWorker(fn, self)
        self.worker.progress.connect(self._set_status)
        self.worker.succeeded.connect(on_ok)
        self.worker.failed.connect(self._task_failed)
        self.worker.finished.connect(self._worker_done)
        self.worker.start()

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.statusBar().showMessage(text)

    def _task_failed(self, message: str) -> None:
        self._set_status("Something went wrong.")
        QMessageBox.critical(self, "Error", message)

    def _worker_done(self) -> None:
        self.generate_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self.translate_btn.setEnabled(True)
        self.preview_voice_btn.setEnabled(True)

    def _refresh_ffmpeg_status(self) -> None:
        ok, detail = ffmpeg_available()
        self.ffmpeg_label.setText("FFmpeg ready" if ok else detail)

    def settings(self) -> dict:
        return load_settings()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec():
            self.reload_voices(background=True)
            self._set_status("Settings saved.")

    def reload_voices(self, background: bool = False) -> None:
        data = self.settings()
        provider = data.get("tts_provider", "edge-tts")
        key = data.get("elevenlabs_api_key", "") if provider == "elevenlabs" else ""
        if background:
            if provider == "edge-tts":
                self.voices = fallback_edge_voices()
            self._fill_languages()
            self._fill_voices()

            def work(_progress):
                return list_voices(provider, key)

            self._start_worker(work, self._voices_loaded)
            return
        try:
            self.voices = list_voices(provider, key)
        except Exception as exc:
            self.voices = fallback_edge_voices()
            self.statusBar().showMessage(f"Could not load {provider} voices: {exc}")
        self._fill_languages()
        self._fill_voices()

    def _voices_loaded(self, voices) -> None:
        if voices:
            self.voices = voices
            self._fill_languages()
            self._fill_voices()
            self._set_status(f"Loaded {len(voices)} voices.")

    def _fill_languages(self) -> None:
        self.lang_combo.blockSignals(True)
        self.lang_combo.clear()
        current = self.project.lang or self.settings().get("lang", "en")
        for code, name in languages_from_voices(self.voices):
            self.lang_combo.addItem(name, code)
        index = self.lang_combo.findData(current)
        self.lang_combo.setCurrentIndex(max(0, index))
        self.lang_combo.blockSignals(False)

    def _fill_voices(self) -> None:
        lang = self.lang_combo.currentData() or "en"
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        filtered = [v for v in self.voices if v.lang == lang] or self.voices
        current = self.project.voice
        selected = 0
        for i, voice in enumerate(filtered):
            self.voice_combo.addItem(voice.label, voice.id)
            if voice.id == current:
                selected = i
        if filtered and current not in {v.id for v in filtered}:
            match = matching_voice(filtered, lang)
            if match:
                selected = next(i for i, v in enumerate(filtered) if v.id == match.id)
                self.project.voice = match.id
                self.project.current().voice = match.id
        self.voice_combo.setCurrentIndex(selected)
        self.voice_combo.blockSignals(False)

    def _lang_changed(self) -> None:
        if self._switching_lang:
            return
        code = self.lang_combo.currentData() or "en"
        if code != self.project.lang:
            self._apply_language(code)

    def _voice_changed(self) -> None:
        voice_id = self.voice_combo.currentData()
        if not voice_id or self._switching_lang:
            return
        current = self.project.current()
        if current.voice != voice_id:
            current.voice = voice_id
            self.project.voice = voice_id
            self._clear_tts(self.project.lang)
            self._persist_voice_prefs()
            self.refresh_warnings()

    def _persist_voice_prefs(self) -> None:
        data = self.settings()
        data["voice"] = self.project.voice
        data["lang"] = self.project.lang
        save_settings(data)

    def current_clip_index(self) -> int:
        return self.selected_index

    def current_clip(self):
        index = self.selected_index
        if 0 <= index < len(self.project.clips):
            return self.project.clips[index]
        return None

    def _spoken_cues(self) -> list[Cue]:
        return [c for c in self.project.current().cues if c.text.strip()]

    def _cue_spans(self) -> list[tuple[float, float, str]]:
        if self.plan and self.plan.cues:
            return [
                (c.video_time, max(0.4, c.tts_duration), c.text[:42] or "Cue")
                for c in self.plan.cues
            ]
        cues = self._spoken_cues()
        spans = []
        durations = (
            self.tts_durations
            if len(self.tts_durations) == len(cues)
            else [estimate_speech_seconds(c.text) for c in cues]
        )
        for cue, duration in zip(cues, durations):
            spans.append((cue.video_time, max(0.4, duration), cue.text[:42] or "Cue"))
        return spans

    def _restore_tts(self) -> None:
        stored = self.tts_store.get(self.project.lang)
        if stored:
            self.tts_paths, self.tts_durations, self.plan = stored
        else:
            self.tts_paths, self.tts_durations, self.plan = [], [], None

    def _save_tts(self) -> None:
        if self.plan and self.tts_paths:
            self.tts_store[self.project.lang] = (
                list(self.tts_paths),
                list(self.tts_durations),
                self.plan,
            )

    def _clear_tts(self, lang: str) -> None:
        self.tts_store.pop(lang, None)
        if lang == self.project.lang:
            self.tts_paths = []
            self.tts_durations = []
            self.plan = None
            self.narration_player.stop()
            self._narration_index = -1
            self._narration_finished = -1
            self._narration_pending = None
            self._narration_gen += 1
            self._audio_hold = False

    def _follow_changed(self, checked: bool) -> None:
        self.timeline.follow_playhead = checked

    def _voice_gain(self) -> float:
        return max(0.0, min(1.0, float(getattr(self.project, "voice_volume", 1.0))))

    def _apply_voice_volume(self) -> None:
        gain = self._voice_gain()
        self.narration_audio.setMuted(False)
        self.narration_audio.setVolume(gain)
        self._preview_audio.setMuted(False)
        self._preview_audio.setVolume(gain)

    def _voice_volume_changed(self, value: int) -> None:
        self.project.voice_volume = value / 100.0
        self.voice_vol_label.setText(f"{value}%")
        self._apply_voice_volume()

    def _music_volume_changed(self, value: int) -> None:
        self.project.music_volume = value / 100.0
        self.music_vol_label.setText(f"{value}%")
        self.music_audio.setVolume(self.project.music_volume)

    def _refresh_music_ui(self) -> None:
        if self.project.music_path:
            self.music_name_label.setText(Path(self.project.music_path).name)
        else:
            self.music_name_label.setText("No soundtrack")
        self.music_volume.blockSignals(True)
        self.music_volume.setValue(int(round(self.project.music_volume * 100)))
        self.music_volume.blockSignals(False)
        self.music_vol_label.setText(f"{self.music_volume.value()}%")
        self.music_audio.setVolume(self.project.music_volume)
        self.voice_volume.blockSignals(True)
        self.voice_volume.setValue(int(round(self._voice_gain() * 100)))
        self.voice_volume.blockSignals(False)
        self.voice_vol_label.setText(f"{self.voice_volume.value()}%")
        self._apply_voice_volume()

    def refresh_all(self) -> None:
        self.mute_box.setChecked(self.project.mute_original)
        self._apply_mute()
        self._rebuild_lang_tabs()
        self._load_cues_into_table()
        self._switching_lang = True
        self._fill_languages()
        index = self.lang_combo.findData(self.project.lang)
        if index >= 0:
            self.lang_combo.setCurrentIndex(index)
        self._fill_voices()
        self._switching_lang = False
        if self.project.clips and self.selected_index < 0:
            self.selected_index = 0
        self._restore_tts()
        self._refresh_music_ui()
        self.refresh_timeline()
        self.refresh_warnings()
        self._refresh_transport()
        self._show_current_media(force=True)

    def refresh_timeline(self) -> None:
        if self.project.clips:
            self.selected_index = min(max(self.selected_index, 0), len(self.project.clips) - 1)
        else:
            self.selected_index = -1
        self.timeline.set_state(
            self.project.clips,
            self.selected_index,
            self.playhead,
            cue_spans=self._cue_spans(),
            music_name=Path(self.project.music_path).name if self.project.music_path else "",
            music_duration=self.project.music_duration,
            music_selected=self._music_selected,
            extra_end=self._cue_end(),
            mark_in=self.project.mark_in,
            mark_out=self.project.mark_out,
        )
        if self._music_selected and self.project.music_path:
            self.selected_label.setText(f"Music: {Path(self.project.music_path).name}")
        else:
            clip = self.current_clip()
            if clip:
                kind = "Image" if clip.is_image else "Video"
                self.selected_label.setText(f"{kind}: {clip.name}")
            else:
                self.selected_label.setText("No clip selected")

    def _timeline_span(self) -> float:
        return max(joined_duration(self.project.clips), self._cue_end(), 0.0)

    def _cue_end(self) -> float:
        end = 0.0
        for start, length, _text in self._cue_spans():
            end = max(end, start + length)
        return end

    def _refresh_transport(self) -> None:
        total = max(joined_duration(self.project.clips), 0.0)
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, int(total * 1000)))
        self.slider.setValue(int(min(self.playhead, total) * 1000))
        self.slider.blockSignals(False)
        self.time_label.setText(f"{format_timestamp(self.playhead)} / {format_timestamp(total)}")
        self.timeline.set_playhead(self.playhead)

    def _apply_mute(self) -> None:
        muted = bool(self.project.mute_original)
        self.audio_out.setMuted(muted)
        self.audio_out.setVolume(0.0 if muted else 1.0)

    def _mute_changed(self, checked: bool) -> None:
        self.project.mute_original = checked
        self._apply_mute()
        self._set_status("Original audio muted." if checked else "Original audio on.")

    def _timeline_clip_selected(self, index: int) -> None:
        self._music_selected = False
        self.selected_index = index
        clip = self.current_clip()
        if clip:
            kind = "Image" if clip.is_image else "Video"
            self.selected_label.setText(f"{kind}: {clip.name}")

    def _music_track_selected(self) -> None:
        self._music_selected = True
        self.selected_index = -1
        if self.project.music_path:
            self.selected_label.setText(f"Music: {Path(self.project.music_path).name}")
        self.refresh_timeline()

    def _timeline_seek(self, seconds: float) -> None:
        if not self._scrubbing:
            self._reset_narration_player()
            self.music_player.pause()
        self._audio_hold = False
        self._scrubbing = True
        self._seek_joined(seconds, keep_playing=False, live=True)

    def _timeline_seek_done(self, seconds: float) -> None:
        self._finish_user_seek(seconds)

    def _after_trim(self) -> None:
        self.refresh_warnings()
        self._refresh_transport()
        self.refresh_timeline()

    def _seek_joined(self, seconds: float, keep_playing: bool = False, live: bool = False) -> None:
        total = joined_duration(self.project.clips)
        self.playhead = min(max(0.0, seconds), max(0.0, total))
        mapped = map_joined_to_clip(self.project.clips, self.playhead)
        if mapped:
            _, _, index = mapped
            self.selected_index = index
        following = self.timeline.follow_playhead
        if live:
            self.timeline.follow_playhead = False
            now = monotonic()
            skip_media = (now - self._last_live_seek) < 0.04
            if not skip_media:
                self._last_live_seek = now
        else:
            skip_media = False
        self._refresh_transport()
        if not live:
            self.refresh_timeline()
        if not skip_media:
            self._show_current_media(force=not live)
        if not live:
            self._sync_narration(force=True)
            self._sync_music(force=True)
        if live:
            self.timeline.follow_playhead = following
        if keep_playing and not self._audio_hold:
            self._start_clock_if_needed()
            if self._playing:
                clip = self.current_clip()
                if clip and not clip.is_image:
                    self.player.play()
        elif not live and not self._playing:
            self.player.pause()
            self.narration_player.pause()
            self.music_player.pause()
            self._image_clock.stop()

    def _unlock_seek(self) -> None:
        self._seeking = False
        self._seek_target_ms = None

    def _begin_seek(self, local_ms: int, lock_ms: int = 400) -> None:
        self._seeking = True
        self._seek_target_ms = local_ms
        self._seek_unlock.start(max(80, lock_ms))

    def _show_current_media(self, force: bool = False, playback_join: bool = False) -> None:
        if not self.project.clips:
            self.player.stop()
            self.preview_stack.setCurrentWidget(self.image_label)
            self.image_label.setText("Add a video or image to the timeline")
            self.image_label.setPixmap(QPixmap())
            return
        index = self.selected_index
        if index < 0 or index >= len(self.project.clips):
            mapped = map_joined_to_clip(self.project.clips, self.playhead)
            if not mapped:
                return
            _, _, index = mapped
        clip = self.project.clips[index]
        start = clip_joined_start(self.project.clips, index)
        local = clip.in_point + max(0.0, self.playhead - start)
        local = min(clip.out_point, local)
        self.selected_index = index
        if clip.is_image:
            self.player.pause()
            pixmap = QPixmap(clip.path)
            if not pixmap.isNull():
                self.image_label.setPixmap(
                    pixmap.scaled(
                        self.image_label.size(),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                self.image_label.setText("")
            else:
                self.image_label.setText(clip.name)
            self.preview_stack.setCurrentWidget(self.image_label)
            return
        self.preview_stack.setCurrentWidget(self.video_widget)
        url = QUrl.fromLocalFile(str(Path(clip.path).resolve()))
        target_ms = int(local * 1000)
        if self.player.source() != url:
            self._begin_seek(target_ms, 250 if playback_join else 400)
            self.player.setSource(url)
            self.player.setPosition(target_ms)
        elif force or abs(self.player.position() - target_ms) > 80:
            if not playback_join:
                self._begin_seek(target_ms)
            self.player.setPosition(target_ms)
        self._apply_mute()
        if self._playing and not self._scrubbing and not self._audio_hold:
            self.player.play()

    def add_videos(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Add videos to V1",
            "",
            "Video files (*.mp4 *.mov *.mkv *.avi *.webm *.m4v);;All files (*.*)",
        )
        self._add_media(files)

    def add_images(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Add images to V1",
            "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*.*)",
        )
        self._add_media(files)

    def add_music(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Add soundtrack",
            "",
            "Audio (*.mp3 *.wav *.m4a *.aac *.flac *.ogg);;All files (*.*)",
        )
        if not path:
            return
        try:
            _, duration = probe_audio(path)
        except Exception as exc:
            QMessageBox.warning(self, "Music", str(exc))
            return
        self.project.music_path = path
        self.project.music_duration = duration
        self._music_selected = True
        self._refresh_music_ui()
        self.refresh_timeline()
        self._sync_music()
        self._set_status(f"Soundtrack: {Path(path).name}")

    def remove_music(self) -> None:
        self.project.music_path = None
        self.project.music_duration = 0.0
        self._music_selected = False
        self.music_player.stop()
        self._refresh_music_ui()
        self.refresh_timeline()
        self._set_status("Soundtrack removed.")

    def _add_media(self, files: list[str]) -> None:
        if not files:
            return
        errors = []
        for path in files:
            try:
                self.project.clips.append(probe_clip(path))
            except Exception as exc:
                errors.append(f"{Path(path).name}: {exc}")
        if self.selected_index < 0 and self.project.clips:
            self.selected_index = 0
        self.refresh_timeline()
        self.refresh_warnings()
        self._refresh_transport()
        self._show_current_media(force=True)
        if errors:
            QMessageBox.warning(self, "Some files were skipped", "\n".join(errors))
        elif files:
            self._set_status(f"Added {len(files)} clip(s) on V1.")

    def remove_clip(self) -> None:
        if self._music_selected:
            self.remove_music()
            return
        index = self.selected_index
        if index < 0 or index >= len(self.project.clips):
            return
        del self.project.clips[index]
        if not self.project.clips:
            self.selected_index = -1
            self.playhead = 0.0
        else:
            self.selected_index = min(index, len(self.project.clips) - 1)
            self.playhead = min(self.playhead, joined_duration(self.project.clips))
        self.refresh_timeline()
        self.refresh_warnings()
        self._refresh_transport()
        self._show_current_media(force=True)

    def split_at_playhead(self) -> None:
        mapped = map_joined_to_clip(self.project.clips, self.playhead)
        if not mapped:
            return
        clip, local, index = mapped
        before = len(self.project.clips)
        self.project.clips = split_clip(self.project.clips, index, local)
        if len(self.project.clips) == before:
            self._set_status("Move the red line onto a clip, then Split.")
            return
        self.selected_index = index + 1
        self.refresh_timeline()
        self.refresh_warnings()
        self._set_status("Split. Select the piece you do not want and press Delete.")

    def toggle_play(self) -> None:
        if not self.project.clips:
            return
        if self._playing:
            self._pause_all()
            return
        if self.playhead >= joined_duration(self.project.clips) - 0.05:
            self.playhead = 0.0
        mapped = map_joined_to_clip(self.project.clips, self.playhead)
        if mapped:
            self.selected_index = mapped[2]
        self._playing = True
        self.play_btn.setText("Pause")
        self._apply_mute()
        self._show_current_media(force=False)
        self._sync_narration(force=True)
        self._sync_music(force=True)
        if not self._audio_hold:
            self._resume_transport()

    def _pause_all(self) -> None:
        self._playing = False
        self._audio_hold = False
        self._joining = False
        self.play_btn.setText("Play")
        self.player.pause()
        self.narration_player.pause()
        self.music_player.pause()
        self._image_clock.stop()
        self._narration_retry.stop()

    def _start_clock_if_needed(self) -> None:
        clip = self.current_clip()
        if self._playing and clip and clip.is_image:
            self._image_clock.start()
        else:
            self._image_clock.stop()

    def _tick_image(self) -> None:
        if not self._playing or self._audio_hold:
            return
        self.playhead += 0.033
        total = joined_duration(self.project.clips)
        if self.playhead >= total:
            self.playhead = total
            self._pause_all()
            self._refresh_transport()
            return
        mapped = map_joined_to_clip(self.project.clips, self.playhead)
        if mapped and mapped[2] != self.selected_index:
            self.selected_index = mapped[2]
            self._show_current_media(force=True)
            self._start_clock_if_needed()
        self._refresh_transport()
        self._sync_narration()
        self._sync_music()

    def _on_play_state(self, state) -> None:
        if not self._playing:
            self.play_btn.setText("Play")
            return
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._joining = False
            return
        if self._audio_hold or self._scrubbing or self._joining or self._seeking:
            return
        QTimer.singleShot(0, self._continue_playback)

    def _video_status(self, status) -> None:
        if (
            status == QMediaPlayer.MediaStatus.EndOfMedia
            and self._playing
            and not self._audio_hold
            and not self._scrubbing
        ):
            QTimer.singleShot(0, self._play_next_clip)

    def _play_next_clip(self) -> None:
        if not self._playing or self._audio_hold or self._scrubbing:
            return
        nxt = self.selected_index + 1
        if nxt >= len(self.project.clips):
            self.playhead = joined_duration(self.project.clips)
            self._pause_all()
            self._refresh_transport()
            return
        self.selected_index = nxt
        self.playhead = clip_joined_start(self.project.clips, nxt)
        self._joining = True
        self._show_current_media(force=True, playback_join=True)
        self._start_clock_if_needed()
        self.refresh_timeline()
        self._refresh_transport()
        self._sync_narration()
        self._sync_music()
        clip = self.current_clip()
        if clip and not clip.is_image and not self._audio_hold:
            self.player.play()
        QTimer.singleShot(80, self._clear_joining)

    def _clear_joining(self) -> None:
        self._joining = False
        if self._playing and not self._audio_hold and not self._scrubbing:
            clip = self.current_clip()
            if clip and not clip.is_image:
                if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                    self.player.play()

    def _continue_playback(self) -> None:
        if not self._playing or self._audio_hold or self._scrubbing or self._joining:
            return
        clip = self.current_clip()
        if not clip:
            self._pause_all()
            return
        local = self.player.position() / 1000.0
        if not clip.is_image and local >= clip.out_point - 0.05:
            self._play_next_clip()
            return
        if clip.is_image:
            self._start_clock_if_needed()
            return
        self.player.play()

    def _on_position(self, position: int) -> None:
        if self._slider_dragging or self._scrubbing or self._audio_hold or self._joining:
            return
        if self._seeking:
            target = self._seek_target_ms
            if target is not None and abs(position - target) > 220:
                return
            self._unlock_seek()
            self._seek_unlock.stop()
        if not self.project.clips:
            return
        clip = self.current_clip()
        if not clip or clip.is_image:
            return
        local = position / 1000.0
        if local < clip.in_point - 0.05:
            return
        if local >= clip.out_point - 0.03:
            self._play_next_clip()
            return
        self.playhead = clip_joined_start(self.project.clips, self.selected_index) + (
            local - clip.in_point
        )
        self._refresh_transport()
        self._sync_narration()
        self._sync_music()

    def _slider_pressed(self) -> None:
        self._slider_dragging = True
        self._scrubbing = True
        self._reset_narration_player()
        self.music_player.pause()
        self._audio_hold = False

    def _slider_scrub(self, value: int) -> None:
        self._audio_hold = False
        self._scrubbing = True
        self._seek_joined(value / 1000.0, keep_playing=False, live=True)

    def _slider_value(self, value: int) -> None:
        if self._slider_dragging:
            self._slider_scrub(value)

    def _seek_from_slider(self) -> None:
        self._finish_user_seek(self.slider.value() / 1000.0)

    NARRATION_LEAD = 0.08
    _LOADED = {
        QMediaPlayer.MediaStatus.LoadedMedia,
        QMediaPlayer.MediaStatus.BufferedMedia,
        QMediaPlayer.MediaStatus.BufferingMedia,
    }

    def _cue_index_at(self, moment: float, lead: float = 0.0) -> int | None:
        if not self.plan or len(self.tts_paths) != len(self.plan.cues):
            return None
        for index, cue in enumerate(self.plan.cues):
            if index == self._narration_finished:
                continue
            start = cue.video_time
            end = start + max(0.05, cue.tts_duration)
            if start - lead <= moment < end:
                return index
        return None

    def _ensure_playing(self, player: QMediaPlayer) -> None:
        if player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
            player.setPosition(0)
        if player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            player.play()

    def _narration_url(self, index: int) -> QUrl | None:
        if index < 0 or index >= len(self.tts_paths):
            return None
        path = Path(self.tts_paths[index])
        if not path.is_file():
            return None
        return QUrl.fromLocalFile(str(path.resolve()))

    def _reset_narration_player(self) -> None:
        self._narration_gen += 1
        self._narration_retry.stop()
        self._narration_pending = None
        self._narration_index = -1
        self._narration_finished = -1
        self.narration_player.stop()
        self.narration_player.setSource(QUrl())

    def _finish_user_seek(self, seconds: float) -> None:
        self._slider_dragging = False
        self._scrubbing = False
        self._audio_hold = False
        self._reset_narration_player()
        total = joined_duration(self.project.clips)
        self.playhead = min(max(0.0, seconds), max(0.0, total))
        mapped = map_joined_to_clip(self.project.clips, self.playhead)
        if mapped:
            self.selected_index = mapped[2]
        self._refresh_transport()
        self.refresh_timeline()
        self._show_current_media(force=True)
        if self._playing:
            self.player.pause()
            self.music_player.pause()
            self._sync_narration(force=True)
            if not self._audio_hold:
                self._resume_transport()
        else:
            self.player.pause()
            self._sync_narration(force=True)

    def _hold_transport(self, at: float) -> None:
        self._audio_hold = True
        self._hold_at = max(0.0, at)
        self._hold_started = monotonic()
        self.playhead = self._hold_at
        self.player.pause()
        self.music_player.pause()
        self._image_clock.stop()
        self._refresh_transport()

    def _resume_transport(self) -> None:
        if self._scrubbing:
            return
        self._audio_hold = False
        if not self._playing:
            return
        self._show_current_media(force=False)
        self._start_clock_if_needed()
        clip = self.current_clip()
        if clip and not clip.is_image:
            self.player.play()
        self._sync_music(force=True)

    def _stop_narration(self) -> None:
        self._narration_retry.stop()
        if self.narration_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.narration_player.pause()
        self._narration_index = -1
        self._narration_pending = None
        if self._audio_hold and not self._scrubbing:
            self._resume_transport()

    def _needs_reload(self, url: QUrl) -> bool:
        status = self.narration_player.mediaStatus()
        return (
            self.narration_player.source() != url
            or status == QMediaPlayer.MediaStatus.EndOfMedia
            or status == QMediaPlayer.MediaStatus.NoMedia
            or status == QMediaPlayer.MediaStatus.InvalidMedia
        )

    def _load_narration(self, url: QUrl) -> None:
        self.narration_player.stop()
        self.narration_player.setSource(QUrl())
        self.narration_player.setSource(url)

    def _start_narration(self, index: int, offset: float, play: bool) -> None:
        url = self._narration_url(index)
        if url is None:
            return
        self._narration_index = index
        self._narration_token = self._narration_gen
        self._narration_pending = (offset, play)
        if self._needs_reload(url):
            self._load_narration(url)
            return
        self._apply_narration_pending()

    def _sync_narration(self, force: bool = False) -> None:
        self.narration_audio.setMuted(False)
        self._apply_voice_volume()
        if not self.plan or len(self.tts_paths) != len(self.plan.cues):
            self._stop_narration()
            return
        if force:
            self._narration_finished = -1
        due = self._cue_index_at(self.playhead, lead=self.NARRATION_LEAD)
        if due == self._narration_finished:
            due = None
        playing = self.narration_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if not force and self._playing and self._narration_index >= 0 and playing:
            if due is None or due == self._narration_index:
                return
        if due is None:
            if playing and not force and self._playing:
                return
            self._stop_narration()
            return
        if not force and due == self._narration_index:
            if playing or self._narration_pending:
                return
            self._retry_narration()
            return
        if not self._playing:
            if force:
                self._start_narration(
                    due, max(0.0, self.playhead - self.plan.cues[due].video_time), False
                )
            return
        offset = max(0.0, self.playhead - self.plan.cues[due].video_time)
        start_at = offset if force and offset > 0.05 else 0.0
        if self.narration_player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self._hold_transport(self.playhead)
        self._start_narration(due, start_at, True)
        self._narration_retry.start(120)

    def _retry_narration(self) -> None:
        if self._scrubbing or getattr(self, "_narration_token", -1) != self._narration_gen:
            return
        if not self._playing or self._narration_index < 0:
            return
        if self.narration_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._resume_transport()
            return
        if self._audio_hold and monotonic() - self._hold_started > 4.0:
            self._set_status("Voice file did not start. Check Generate voices, then Play again.")
            self._resume_transport()
            return
        if self._narration_pending:
            self._apply_narration_pending()
            return
        url = self._narration_url(self._narration_index)
        if url is None:
            return
        if self._needs_reload(url):
            self._narration_pending = (0.0, True)
            self._load_narration(url)
            return
        self.narration_player.setPosition(0)
        self.narration_player.play()

    def _apply_narration_pending(self) -> None:
        if not self._narration_pending:
            return
        if getattr(self, "_narration_token", self._narration_gen) != self._narration_gen:
            self._narration_pending = None
            return
        offset, should_play = self._narration_pending
        self._narration_pending = None
        if self.narration_player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia:
            self.narration_player.setPosition(0)
        elif offset > 0.02:
            self.narration_player.setPosition(int(offset * 1000))
        if should_play and self._playing and not self._scrubbing:
            self.narration_player.play()
            if self.narration_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self._resume_transport()
            else:
                self._narration_retry.start(80)
        else:
            self.narration_player.pause()

    def _narration_play_state(self, state) -> None:
        if getattr(self, "_narration_token", -1) != self._narration_gen:
            return
        if state == QMediaPlayer.PlaybackState.PlayingState and not self._scrubbing:
            self._resume_transport()

    def _narration_status(self, status) -> None:
        if getattr(self, "_narration_token", self._narration_gen) != self._narration_gen:
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._narration_index >= 0:
                self._narration_finished = self._narration_index
            self._narration_index = -1
            self._narration_pending = None
            if self._audio_hold:
                self._resume_transport()
            return
        if status in self._LOADED:
            self._apply_narration_pending()

    def _narration_error(self, *_args) -> None:
        if getattr(self, "_narration_token", -1) != self._narration_gen:
            return
        if self._playing and self._narration_index >= 0:
            self._set_status("Retrying voice playback…")
            self._narration_retry.start(150)

    def _sync_music(self, force: bool = False) -> None:
        path = self.project.music_path
        if not path or not Path(path).is_file() or self.project.music_volume <= 0:
            if self.music_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.music_player.pause()
            return
        url = QUrl.fromLocalFile(str(Path(path).resolve()))
        if self.music_player.source() != url:
            self.music_player.setSource(url)
            force = True
        length = self.project.music_duration or 0.0
        offset = self.playhead % length if length > 0.05 else self.playhead
        drifted = abs(self.music_player.position() / 1000.0 - offset) > 1.5
        if force or drifted:
            self.music_player.setPosition(int(offset * 1000))
        self.music_audio.setVolume(self.project.music_volume)
        if self._playing:
            self._ensure_playing(self.music_player)
        elif self.music_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.music_player.pause()

    def _parsed_cues(self) -> list[Cue]:
        return self._spoken_cues()

    def _cues_from_table(self) -> list[Cue]:
        cues = []
        for row in range(self.cue_table.rowCount()):
            time_item = self.cue_table.item(row, 0)
            text_item = self.cue_table.item(row, 1)
            raw_time = time_item.text().strip() if time_item else ""
            text = text_item.text().strip() if text_item else ""
            if not raw_time and not text:
                continue
            try:
                moment = parse_timestamp(raw_time or "0")
            except ValueError:
                continue
            cues.append(Cue(video_time=moment, text=text))
        cues.sort(key=lambda c: c.video_time)
        return cues

    def _load_cues_into_table(self) -> None:
        self._loading_table = True
        cues = self.project.current().cues
        self.cue_table.setRowCount(0)
        for cue in cues:
            self._append_cue_row(format_timestamp(cue.video_time), cue.text)
        self._loading_table = False

    def _append_cue_row(self, time_text: str, body: str) -> None:
        row = self.cue_table.rowCount()
        self.cue_table.insertRow(row)
        self.cue_table.setItem(row, 0, QTableWidgetItem(time_text))
        self.cue_table.setItem(row, 1, QTableWidgetItem(body))

    def _table_changed(self, *_args) -> None:
        if self._loading_table:
            return
        cues = self._cues_from_table()
        spoken = [c for c in cues if c.text.strip()]
        current = self.project.current()
        before = [(c.video_time, c.text) for c in current.cues if c.text.strip()]
        after = [(c.video_time, c.text) for c in spoken]
        current.cues = spoken
        self.project.cues = spoken
        if before != after:
            self._clear_tts(self.project.lang)
        self._warn_timer.start()
        self.refresh_timeline()

    def add_cue_at_playhead(self) -> None:
        self._loading_table = True
        self._append_cue_row(format_timestamp(self.playhead), "")
        self._loading_table = False
        self._table_changed()
        self.cue_table.setCurrentCell(self.cue_table.rowCount() - 1, 1)
        self.cue_table.editItem(self.cue_table.item(self.cue_table.rowCount() - 1, 1))

    def remove_selected_cue(self) -> None:
        row = self.cue_table.currentRow()
        if row < 0:
            return
        self.cue_table.removeRow(row)
        self._table_changed()

    def _rebuild_lang_tabs(self) -> None:
        self.lang_tabs.blockSignals(True)
        while self.lang_tabs.count():
            self.lang_tabs.removeTab(0)
        for code in self.project.narrations:
            self.lang_tabs.addTab(LANGUAGE_NAMES.get(code, code.upper()))
            self.lang_tabs.setTabData(self.lang_tabs.count() - 1, code)
        index = next(
            (
                i
                for i in range(self.lang_tabs.count())
                if self.lang_tabs.tabData(i) == self.project.lang
            ),
            0,
        )
        self.lang_tabs.setCurrentIndex(index)
        closable = self.lang_tabs.count() > 1
        for i in range(self.lang_tabs.count()):
            button = self.lang_tabs.tabButton(i, QTabBar.ButtonPosition.RightSide)
            if button:
                button.setVisible(closable)
        self.lang_tabs.blockSignals(False)

    def _lang_tab_changed(self, index: int) -> None:
        if index < 0 or self._switching_lang:
            return
        code = self.lang_tabs.tabData(index)
        if code and code != self.project.lang:
            self._apply_language(code)

    def _apply_language(self, code: str, save_current: bool = True) -> None:
        self._switching_lang = True
        if save_current and self.project.lang in self.project.narrations:
            self.project.current().cues = [c for c in self._cues_from_table() if c.text.strip()]
            self._save_tts()
        self.project.lang = code
        if code not in self.project.narrations:
            match = matching_voice(self.voices, code)
            self.project.narrations[code] = Narration(
                lang=code,
                voice=match.id if match else self.project.voice,
            )
        current = self.project.current()
        self.project.voice = current.voice
        self._restore_tts()
        self._load_cues_into_table()
        combo = self.lang_combo.findData(code)
        if combo >= 0:
            self.lang_combo.setCurrentIndex(combo)
        self._fill_voices()
        voice_index = self.voice_combo.findData(current.voice)
        if voice_index >= 0:
            self.voice_combo.setCurrentIndex(voice_index)
        self._rebuild_lang_tabs()
        self._switching_lang = False
        self.refresh_timeline()
        self.refresh_warnings()
        self._persist_voice_prefs()

    def add_language_tab(self) -> None:
        names = [f"{name} ({code})" for code, name in LANGUAGE_NAMES.items() if code not in self.project.narrations]
        if not names:
            QMessageBox.information(self, "Language", "All listed languages already have a tab.")
            return
        choice, ok = QInputDialog.getItem(self, "Add language", "Tutorial language:", names, 0, False)
        if not ok or not choice:
            return
        code = choice.rsplit("(", 1)[-1].rstrip(")")
        self._apply_language(code)
        self._set_status(f"Added {LANGUAGE_NAMES.get(code, code)} tab. Generate voices for this language before export.")

    def _close_lang_tab(self, index: int) -> None:
        if self.lang_tabs.count() <= 1:
            return
        code = self.lang_tabs.tabData(index)
        if not code:
            return
        name = LANGUAGE_NAMES.get(code, code)
        answer = QMessageBox.question(
            self,
            "Remove language",
            f"Remove the {name} tab and its narration?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        leftover = next(c for c in self.project.narrations if c != code)
        self.project.narrations.pop(code, None)
        self._clear_tts(code)
        if self.project.lang == code:
            self._apply_language(leftover, save_current=False)
        else:
            self._rebuild_lang_tabs()

    def mark_in(self) -> None:
        self.project.mark_in = self.playhead
        self.refresh_timeline()
        self._set_status(f"In mark at {format_timestamp(self.playhead)}")

    def mark_out(self) -> None:
        self.project.mark_out = self.playhead
        self.refresh_timeline()
        self._set_status(f"Out mark at {format_timestamp(self.playhead)}")

    def delete_marked_range(self) -> None:
        start = self.project.mark_in
        end = self.project.mark_out
        if start is None or end is None:
            QMessageBox.information(
                self,
                "Delete range",
                "Drag the red line to the start of the unwanted part and click Mark In. "
                "Drag to the end and click Mark Out. Then Delete In→Out.",
            )
            return
        before = len(self.project.clips)
        lo, hi = min(start, end), max(start, end)
        self.project.clips = delete_joined_range(self.project.clips, lo, hi)
        for narration in self.project.narrations.values():
            narration.cues = ripple_cues(narration.cues, lo, hi)
        self.project.sync_from_current()
        self.project.mark_in = None
        self.project.mark_out = None
        self.playhead = lo
        self._load_cues_into_table()
        spoken = self._spoken_cues()
        if self.plan and len(self.tts_durations) == len(spoken) == len(self.plan.cues):
            self.plan = build_timeline(spoken, self.tts_durations, joined_duration(self.project.clips))
            self._save_tts()
        else:
            self._clear_tts(self.project.lang)
        self.refresh_timeline()
        self.refresh_warnings()
        self._refresh_transport()
        self._show_current_media(force=True)
        if len(self.project.clips) == before and before:
            self._set_status("Nothing was inside those marks.")
        else:
            self._set_status("Removed the marked range. Later video and narration slid left.")

    def refresh_warnings(self) -> None:
        cues = self._parsed_cues()
        source = joined_duration(self.project.clips)
        if not cues:
            self.warning_label.setText("")
            return
        late = [c for c in cues if c.video_time > source + 0.05]
        durations = (
            self.tts_durations
            if len(self.tts_durations) == len(cues)
            else [estimate_speech_seconds(c.text) for c in cues]
        )
        plan = build_timeline(cues, durations, source)
        bits = []
        if late:
            bits.append(
                "Some times are after the video ends — those lines will not play. "
                f"Video is {format_timestamp(source)}."
            )
        holds = [c for c in plan.cues if c.hold > 0.05]
        if holds:
            bits.extend(
                f"{format_timestamp(c.video_time)}: speech {c.tts_duration:.1f}s > gap {c.gap:.1f}s → hold {c.hold:.1f}s"
                for c in holds
            )
        elif not late:
            kind = "measured" if len(self.tts_durations) == len(cues) else "estimated"
            bits.append(f"No holds ({kind}). Speech fits the gaps.")
        if self.tts_paths and len(self.tts_paths) == len(cues):
            bits.append(f"{len(self.tts_paths)} voice clip(s) ready on the VO track.")
        self.warning_label.setText("\n".join(bits))

    def preview_voice(self) -> None:
        voice = self.voice_combo.currentData()
        if not voice:
            return
        data = self.settings()

        def work(progress):
            progress("Generating voice preview...")
            return synthesize(
                "This is a preview of the selected voice for your tutorial.",
                provider=data.get("tts_provider", "edge-tts"),
                voice=voice,
                openai_key=data.get("openai_api_key", ""),
                elevenlabs_key=data.get("elevenlabs_api_key", ""),
            )

        self.preview_voice_btn.setEnabled(False)
        self._start_worker(work, self._play_preview_file)

    def _play_preview_file(self, path) -> None:
        self.preview_voice_btn.setEnabled(True)
        self._apply_voice_volume()
        self._preview_player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self._preview_player.play()
        self._set_status("Playing voice preview.")

    def generate_voices(self) -> None:
        self.project.current().cues = self._cues_from_table()
        self.project.sync_from_current()
        cues = self._spoken_cues()
        if not cues:
            QMessageBox.information(self, "Script", "Add at least one line with text in the table.")
            return
        if not self.project.clips:
            QMessageBox.information(self, "Timeline", "Add a video first.")
            return
        self.project.current().cues = cues
        self.project.sync_from_current()
        self._load_cues_into_table()
        data = self.settings()
        voice = self.project.current().voice or self.project.voice
        source = joined_duration(self.project.clips)
        late = [c for c in cues if c.video_time > source + 0.05]
        if late:
            QMessageBox.warning(
                self,
                "Times past the video",
                f"{len(late)} line(s) start after the video ends ({format_timestamp(source)}). "
                "Those will not play. Change the Time column so each line sits on the video.",
            )

        def work(progress):
            paths = []
            durations = []
            for i, cue in enumerate(cues, start=1):
                progress(f"Speaking cue {i} of {len(cues)}...")
                path, duration = synthesize_with_duration(
                    cue.text,
                    provider=data.get("tts_provider", "edge-tts"),
                    voice=voice,
                    openai_key=data.get("openai_api_key", ""),
                    elevenlabs_key=data.get("elevenlabs_api_key", ""),
                )
                paths.append(path)
                durations.append(duration)
            plan = build_timeline(cues, durations, source)
            return paths, durations, plan

        self.generate_btn.setEnabled(False)
        self._start_worker(work, self._voices_ready)

    def _voices_ready(self, result) -> None:
        self.tts_paths, self.tts_durations, self.plan = result
        self._save_tts()
        self.generate_btn.setEnabled(True)
        self._load_cues_into_table()
        self.refresh_warnings()
        self.refresh_timeline()
        self._sync_narration()
        source = joined_duration(self.project.clips)
        late = [c for c in self.plan.cues if c.video_time > source + 0.05] if self.plan else []
        extra = ""
        if late:
            extra = f" {len(late)} line(s) start after the video — change those times."
        elif self.plan and self.plan.cues:
            extra = (
                f" First line is at {format_timestamp(self.plan.cues[0].video_time)}. "
                "Scrub the red line onto that time, then press Play."
            )
        vo = len(self.plan.cues) if self.plan else 0
        self._set_status(
            f"Generated {len(self.tts_paths)} WAV voice clip(s) — {vo} block(s) on VO for "
            f"{LANGUAGE_NAMES.get(self.project.lang, self.project.lang)}.{extra}"
        )

    def translate_script(self) -> None:
        self.project.current().cues = self._cues_from_table()
        cues = [c for c in self._parsed_cues() if c.text.strip()]
        key = self.settings().get("openai_api_key", "")
        if not key:
            QMessageBox.information(
                self,
                "Translate",
                "Add an OpenAI API key in Settings to auto-translate, "
                "or edit the script text yourself.",
            )
            return
        if not cues:
            QMessageBox.information(self, "Translate", "Write a script first.")
            return
        options = [f"{name} ({code})" for code, name in LANGUAGE_NAMES.items() if code != self.project.lang]
        choice, ok = QInputDialog.getItem(
            self, "Translate", "Create or update this language tab:", options, 0, False
        )
        if not ok or not choice:
            return
        target = choice.rsplit("(", 1)[-1].rstrip(")")
        language = LANGUAGE_NAMES.get(target, target)

        def work(progress):
            progress(f"Translating script to {language}...")
            return target, translate_cues(cues, target, key)

        self.translate_btn.setEnabled(False)
        self._start_worker(work, self._translated)

    def _translated(self, result) -> None:
        self.translate_btn.setEnabled(True)
        target, cues = result
        match = matching_voice(self.voices, target)
        self.project.narrations[target] = Narration(
            lang=target,
            voice=match.id if match else self.project.voice,
            cues=cues,
        )
        self._clear_tts(target)
        self._apply_language(target)
        self._set_status(
            f"Translated into {LANGUAGE_NAMES.get(target, target)}. Generate voices on this tab, then export."
        )

    def export_video(self) -> None:
        ok, detail = ffmpeg_available()
        if not ok:
            QMessageBox.warning(self, "FFmpeg required", detail)
            return
        if not self.project.clips:
            QMessageBox.information(self, "Export", "Add at least one clip.")
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, "Export MP4", "tutorial.mp4", "MP4 video (*.mp4)"
        )
        if not dest:
            return
        if not dest.lower().endswith(".mp4"):
            dest += ".mp4"
        self.project.current().cues = self._cues_from_table()
        cues = [c for c in self._parsed_cues() if c.text.strip()]

        data = self.settings()
        voice = self.project.current().voice or self.project.voice
        source = joined_duration(self.project.clips)
        existing_paths = list(self.tts_paths)
        existing_durs = list(self.tts_durations)

        def work(progress):
            paths = existing_paths
            durations = existing_durs
            if cues and (len(paths) != len(cues) or len(durations) != len(cues)):
                paths, durations = [], []
                for i, cue in enumerate(cues, start=1):
                    progress(f"Speaking cue {i} of {len(cues)}...")
                    path, duration = synthesize_with_duration(
                        cue.text,
                        provider=data.get("tts_provider", "edge-tts"),
                        voice=voice,
                        openai_key=data.get("openai_api_key", ""),
                        elevenlabs_key=data.get("elevenlabs_api_key", ""),
                    )
                    paths.append(path)
                    durations.append(duration)
            plan = build_timeline(cues, durations, source) if cues else build_timeline([], [], source)
            progress("Rendering video with FFmpeg...")
            export_project(self.project, plan, paths, dest, cache_dir() / "export")
            return dest, paths, durations, plan

        self.export_btn.setEnabled(False)
        self._start_worker(work, self._export_done)

    def _export_done(self, result) -> None:
        dest, paths, durations, plan = result
        self.tts_paths, self.tts_durations, self.plan = paths, durations, plan
        self.export_btn.setEnabled(True)
        self.refresh_warnings()
        self._set_status(f"Exported {dest}")
        QMessageBox.information(self, "Export complete", f"Saved:\n{dest}")

    def new_project(self) -> None:
        self.project = Project(
            voice=self.voice_combo.currentData() or "en-US-JennyNeural",
            lang=self.lang_combo.currentData() or "en",
        )
        self.tts_store = {}
        self.tts_paths = []
        self.tts_durations = []
        self.plan = None
        self.playhead = 0.0
        self.selected_index = -1
        self._music_selected = False
        self._pause_all()
        self.player.stop()
        self.music_player.stop()
        self.refresh_all()
        self._set_status("New project.")

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", "Tutorial project (*.json)"
        )
        if not path:
            return
        try:
            self.project = load_project(path)
        except Exception as exc:
            QMessageBox.critical(self, "Open failed", str(exc))
            return
        self.tts_paths = []
        self.tts_durations = []
        self.plan = None
        self.playhead = 0.0
        self.selected_index = 0 if self.project.clips else -1
        self._music_selected = False
        self.refresh_all()
        self._set_status(f"Opened {Path(path).name}")

    def save_project_dialog(self) -> None:
        if self.project.path:
            save_project(self.project)
            self._set_status("Project saved.")
            return
        self.save_project_as()

    def save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project", "tutorial.json", "Tutorial project (*.json)"
        )
        if not path:
            return
        save_project(self.project, path)
        self._set_status("Project saved.")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait(2000)
        self._pause_all()
        self.player.stop()
        self._preview_player.stop()
        self.narration_player.stop()
        self.music_player.stop()
        event.accept()
