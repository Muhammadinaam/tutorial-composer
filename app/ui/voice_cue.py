from __future__ import annotations

import array
import wave
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink


def _audio_is(value, expected) -> bool:
    # QAudioSink reports QtAudio.State, which is a different enum type from
    # QAudio.State even when the names and numeric values match.
    return getattr(value, "value", value) == getattr(expected, "value", expected)


def _stretch(pcm: bytes, channels: int, rate: float) -> bytes:
    """Resample so playback at the device rate matches ``rate`` (1 = original)."""
    if abs(rate - 1.0) < 0.01 or not pcm or channels <= 0:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % (channels * 2))])
    frames = len(samples) // channels
    if frames <= 1:
        return pcm
    out_frames = max(1, int(frames / rate))
    out = array.array("h")
    last = frames - 1
    for index in range(out_frames):
        src = min(float(last), index * rate)
        left = int(src)
        right = min(last, left + 1)
        mix = src - left
        for channel in range(channels):
            a = samples[left * channels + channel]
            b = samples[right * channels + channel]
            out.append(int(a + (b - a) * mix))
    return out.tobytes()


@dataclass
class CachedVoice:
    pcm: bytes
    sample_rate: int
    channels: int
    duration: float
    sped: dict[int, bytes] = field(default_factory=dict)

    @property
    def frame_bytes(self) -> int:
        return self.channels * 2

    def at_rate(self, rate: float) -> bytes:
        key = int(round(rate * 1000))
        if key == 1000:
            return self.pcm
        cached = self.sped.get(key)
        if cached is None:
            cached = _stretch(self.pcm, self.channels, rate)
            self.sped[key] = cached
        return cached

    def slice_at(self, origin: float, rate: float) -> bytes:
        origin = min(max(0.0, origin), self.duration)
        audio = self.at_rate(rate)
        start = int(origin * self.sample_rate / max(0.25, rate))
        return audio[start * self.frame_bytes :]


def load_wav(path: Path) -> CachedVoice:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        pcm = handle.readframes(frames)
    if width != 2 or channels < 1 or rate <= 0 or not pcm:
        raise ValueError(f"Voice file must be 16-bit PCM: {path}")
    usable = len(pcm) - (len(pcm) % (channels * 2))
    pcm = pcm[:usable]
    duration = (len(pcm) // (channels * 2)) / float(rate)
    return CachedVoice(pcm=pcm, sample_rate=rate, channels=channels, duration=duration)


class VoiceCuePlayer(QObject):
    """Play generated narration from a decoded PCM cache.

    The whole clip stays in memory, so preview can start at any sample.
    Pausing in the middle of a line and pressing Play continues from that
    moment instead of the first word.
    """

    ready = Signal()
    failed = Signal()
    playingChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache: dict[tuple, CachedVoice] = {}
        self._source = QUrl()
        self._clip: CachedVoice | None = None
        self._sink: QAudioSink | None = None
        self._buffer: QBuffer | None = None
        self._fmt_key: tuple[int, int] | None = None
        self._heard = False
        self._ignore_end = False
        self._gain = 1.0
        self._rate = 1.0
        self._origin = 0.0
        self._position = 0.0
        self._duration = 0.0

    def source(self) -> QUrl:
        return self._source

    def isReady(self) -> bool:
        return self._clip is not None

    def isPlaying(self) -> bool:
        return (
            self._heard
            and self._sink is not None
            and _audio_is(self._sink.state(), QAudio.State.ActiveState)
        )

    def duration(self) -> float:
        return self._duration

    def position_seconds(self) -> float:
        if self._heard and self._sink is not None:
            elapsed = self._sink.processedUSecs() / 1_000_000.0
            return min(self._duration, self._origin + elapsed * self._rate)
        return self._position

    def setPlaybackRate(self, rate: float) -> None:
        rate = max(0.25, min(2.0, float(rate)))
        if abs(rate - self._rate) < 0.001:
            self._rate = rate
            return
        restart = self.position_seconds() if self._heard else None
        self._rate = rate
        if restart is not None and self._clip is not None:
            self.play(restart)

    def setVolume(self, gain: float) -> None:
        self._gain = max(0.0, min(1.0, float(gain)))
        if self._sink is not None:
            self._sink.setVolume(self._gain)

    def preload(self, paths) -> None:
        for path in paths:
            try:
                self._clip_for(Path(path))
            except (OSError, ValueError, wave.Error):
                continue

    def setSource(self, url: QUrl) -> None:
        self.stop()
        self._source = url
        self._clip = None
        self._duration = 0.0
        path = url.toLocalFile() if url.isLocalFile() else ""
        if not path:
            self.failed.emit()
            return
        try:
            self._clip = self._clip_for(Path(path))
        except (OSError, ValueError, wave.Error):
            self._clip = None
            self.failed.emit()
            return
        self._duration = self._clip.duration
        self.ready.emit()

    def play(self, offset: float = 0.0) -> None:
        clip = self._clip
        if clip is None:
            self.failed.emit()
            return
        origin = min(max(0.0, float(offset)), clip.duration)
        payload = clip.slice_at(origin, self._rate)
        self._origin = origin
        self._position = origin
        self._ignore_end = True
        self._halt()
        self._heard = False
        if len(payload) < clip.frame_bytes:
            self._position = clip.duration
            self._ignore_end = False
            self.playingChanged.emit()
            return
        self._ensure_sink(clip.sample_rate, clip.channels)
        self._buffer = QBuffer(self)
        self._buffer.setData(QByteArray(payload))
        self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink.start(self._buffer)
        self._ignore_end = False
        if _audio_is(self._sink.state(), QAudio.State.ActiveState):
            self._mark_heard()
        elif not _audio_is(self._sink.error(), QAudio.Error.NoError):
            self.failed.emit()

    def stop(self) -> None:
        was = self._heard
        if was:
            self._position = self.position_seconds()
        self._ignore_end = True
        self._halt()
        self._ignore_end = False
        self._heard = False
        if was:
            self.playingChanged.emit()

    def _clip_for(self, path: Path) -> CachedVoice:
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(key)
        if cached is None:
            cached = load_wav(path)
            self._cache[key] = cached
        return cached

    def _ensure_sink(self, sample_rate: int, channels: int) -> None:
        key = (sample_rate, channels)
        if self._sink is not None and self._fmt_key == key:
            self._sink.setVolume(self._gain)
            return
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
            self._sink = None
        fmt = QAudioFormat()
        fmt.setSampleRate(sample_rate)
        fmt.setChannelCount(channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self._sink = QAudioSink(fmt, self)
        self._sink.setVolume(self._gain)
        self._sink.stateChanged.connect(self._on_state)
        self._fmt_key = key

    def _halt(self) -> None:
        if self._sink is not None and not _audio_is(self._sink.state(), QAudio.State.StoppedState):
            self._sink.stop()
        if self._buffer is not None:
            self._buffer.close()
            self._buffer.deleteLater()
            self._buffer = None

    def _on_state(self, state) -> None:
        if self._ignore_end:
            return
        if _audio_is(state, QAudio.State.ActiveState):
            self._mark_heard()
            return
        if _audio_is(state, QAudio.State.IdleState) and self._heard:
            self._position = self._duration
            self._heard = False
            self.playingChanged.emit()
            return
        if (
            _audio_is(state, QAudio.State.StoppedState)
            and self._sink is not None
            and not _audio_is(self._sink.error(), QAudio.Error.NoError)
        ):
            self._heard = False
            self.failed.emit()

    def _mark_heard(self) -> None:
        if self._heard:
            return
        self._heard = True
        self.playingChanged.emit()
