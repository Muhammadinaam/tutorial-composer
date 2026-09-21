from __future__ import annotations

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class VoiceCuePlayer(QObject):
    """Preview voice using QMediaPlayer.

    isPlaying() is true only after samples have started, so the timeline can
    wait at the cue until speech is actually audible.
    """

    ready = Signal()
    failed = Signal()
    playingChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._audio.setVolume(1.0)
        self._player.setAudioOutput(self._audio)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.positionChanged.connect(self._on_position)
        self._player.errorOccurred.connect(self._on_error)
        self._heard = False
        self._seen_start = False
        self._gain = 1.0

    def source(self) -> QUrl:
        return self._player.source()

    def isReady(self) -> bool:
        status = self._player.mediaStatus()
        return status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
            QMediaPlayer.MediaStatus.BufferingMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
        )

    def isPlaying(self) -> bool:
        return (
            self._heard
            and self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            and self._player.position() > 40
        )

    def setVolume(self, gain: float) -> None:
        self._gain = max(0.0, min(1.0, float(gain)))
        self._audio.setMuted(False)
        self._audio.setVolume(self._gain)

    def setSource(self, url: QUrl) -> None:
        self.stop()
        if self._player.source() == url and self.isReady():
            self.ready.emit()
            return
        self._player.setSource(url)

    def play(self) -> None:
        self._heard = False
        self._seen_start = False
        self._player.setPosition(0)
        self._player.play()

    def stop(self) -> None:
        was = self._heard
        self._heard = False
        self._seen_start = False
        if self._player.playbackState() != QMediaPlayer.PlaybackState.StoppedState:
            self._player.pause()
        if was:
            self.playingChanged.emit()

    def _on_status(self, status) -> None:
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self.ready.emit()
            return
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.failed.emit()
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._heard = False
            self._seen_start = False
            self.playingChanged.emit()

    def _on_position(self, position: int) -> None:
        if self._heard:
            return
        if self._player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return
        if not self._seen_start:
            if position <= 80:
                self._seen_start = True
            elif position < 250:
                self._mark_heard()
            return
        if position > 40:
            self._mark_heard()

    def _mark_heard(self) -> None:
        if self._heard:
            return
        self._heard = True
        self._seen_start = True
        self.playingChanged.emit()

    def _on_error(self, *_args) -> None:
        self._heard = False
        self.failed.emit()
