from __future__ import annotations

import sys
import threading
import wave
from pathlib import Path


SPEAKER_LOOPBACK = "__speaker_loopback__"
_OPEN_ERROR = "Could not open the default speakers for system audio."


def is_speaker_loopback(name: str | None) -> bool:
    return name == SPEAKER_LOOPBACK


def speaker_loopback_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        _default_loopback_mic()
    except Exception:
        return False
    return True


def _ensure_com() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    # WASAPI belongs to the calling thread. The capture thread has to
    # initialize COM itself; importing soundcard on the UI thread does not.
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0) & 0xFFFFFFFF
    if hr not in (0, 1, 0x80010106):
        raise RuntimeError(f"Could not initialize audio (COM {hr:#x}).")


def _default_loopback_mic():
    import soundcard as sc

    _ensure_com()
    speaker = sc.default_speaker()
    if speaker is None:
        raise RuntimeError(_OPEN_ERROR)
    return sc.get_microphone(id=str(speaker.name), include_loopback=True)


class LoopbackRecorder:
    """Capture the default playback device while a screen recording is running."""

    def __init__(self, dest: Path) -> None:
        self.dest = Path(dest)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    def start(self) -> None:
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run, name="speaker-loopback", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(4.0):
            self.cancel()
            raise RuntimeError(self._error or _OPEN_ERROR)
        if self._error:
            message = self._error
            self.cancel()
            raise RuntimeError(message)

    def stop(self) -> Path:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._error:
            raise RuntimeError(self._error)
        if not self.dest.is_file() or self.dest.stat().st_size < 1000:
            raise RuntimeError("System audio was not captured from the default speakers.")
        return self.dest

    def cancel(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=3)
        try:
            self.dest.unlink(missing_ok=True)
        except OSError:
            pass

    def _run(self) -> None:
        try:
            import numpy as np

            mic = _default_loopback_mic()
            rate = 48000
            with mic.recorder(samplerate=rate) as recorder:
                first = recorder.record(numframes=rate // 10)
                channels = int(first.shape[1]) if getattr(first, "ndim", 1) > 1 else 1
                self.dest.parent.mkdir(parents=True, exist_ok=True)
                with wave.open(str(self.dest), "wb") as handle:
                    handle.setnchannels(max(1, channels))
                    handle.setsampwidth(2)
                    handle.setframerate(rate)
                    self._write(handle, first, np)
                    self._ready.set()
                    while not self._stop.is_set():
                        chunk = recorder.record(numframes=rate // 5)
                        self._write(handle, chunk, np)
        except Exception as exc:
            self._error = f"{_OPEN_ERROR} {exc}"
            self._ready.set()

    @staticmethod
    def _write(handle: wave.Wave_write, data, np) -> None:
        clipped = np.clip(data, -1.0, 1.0)
        frames = (clipped * 32767.0).astype(np.int16)
        handle.writeframes(frames.tobytes())
