"""Measure voice-preview startup delay without the app UI.

Prints a timestamp at play(), then keeps doing extra work so you can hear
whether the click is late relative to the log.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "cache" / "bench_click.wav"


def make_click_wav(path: Path, seconds: float = 1.6, rate: int = 44100) -> Path:
    import array
    import math

    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(rate * seconds)
    samples = array.array("h")
    for i in range(n):
        t = i / rate
        if t < 0.012:
            value = int(32000 * (1.0 - t / 0.012))
        else:
            value = int(9000 * math.sin(2 * math.pi * 440 * t) * max(0.0, 1.0 - t / seconds))
        samples.append(max(-32767, min(32767, value)))
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return path


def extra_work(label: str, started: float, until: float) -> None:
    n = 0
    while time.perf_counter() < until:
        n += 1
        elapsed = time.perf_counter() - started
        print(f"  [{elapsed:6.3f}s] extra work {label} #{n}", flush=True)
        time.sleep(0.12)


def bench_winsound(path: Path) -> None:
    import winsound

    print("\n=== winsound.PlaySound (Windows, async) ===", flush=True)
    t0 = time.perf_counter()
    print(f"  [{0.000:6.3f}s] PLAY — you should hear the click now", flush=True)
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
    print(f"  [{time.perf_counter() - t0:6.3f}s] PlaySound returned", flush=True)
    extra_work("winsound", t0, t0 + 1.4)
    winsound.PlaySound(None, winsound.SND_PURGE)


def bench_qaudiosink(path: Path) -> None:
    from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QIODevice, QTimer
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    pcm = QByteArray(path.read_bytes())
    with wave.open(str(path), "rb") as handle:
        fmt = QAudioFormat()
        fmt.setSampleRate(handle.getframerate())
        fmt.setChannelCount(handle.getnchannels())
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        frames = handle.readframes(handle.getnframes())

    buf = QBuffer()
    buf.setData(QByteArray(frames))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    sink = QAudioSink(fmt)

    t0 = {"t": 0.0}

    def on_state(state) -> None:
        print(f"  [{time.perf_counter() - t0['t']:6.3f}s] QAudioSink state={state}", flush=True)

    sink.stateChanged.connect(on_state)
    print("\n=== QAudioSink (current app path) ===", flush=True)
    t0["t"] = time.perf_counter()
    print(f"  [{0.000:6.3f}s] PLAY — you should hear the click now", flush=True)
    sink.start(buf)
    print(f"  [{time.perf_counter() - t0['t']:6.3f}s] sink.start returned", flush=True)

    def tick() -> None:
        elapsed = time.perf_counter() - t0["t"]
        print(f"  [{elapsed:6.3f}s] extra work qaudio event-loop", flush=True)
        if elapsed >= 1.4:
            sink.stop()
            app.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(120)
    app.exec()
    del pcm


def bench_qmediaplayer(path: Path) -> None:
    from PySide6.QtCore import QCoreApplication, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    player = QMediaPlayer()
    audio = QAudioOutput()
    player.setAudioOutput(audio)
    player.setSource(QUrl.fromLocalFile(str(path.resolve())))
    t0 = {"t": 0.0}

    def on_pos(pos: int) -> None:
        print(f"  [{time.perf_counter() - t0['t']:6.3f}s] QMediaPlayer position={pos}ms", flush=True)

    def on_state(state) -> None:
        print(f"  [{time.perf_counter() - t0['t']:6.3f}s] QMediaPlayer state={state}", flush=True)

    player.positionChanged.connect(on_pos)
    player.playbackStateChanged.connect(on_state)
    print("\n=== QMediaPlayer FFmpeg ===", flush=True)
    t0["t"] = time.perf_counter()
    print(f"  [{0.000:6.3f}s] PLAY — you should hear the click now", flush=True)
    player.play()
    print(f"  [{time.perf_counter() - t0['t']:6.3f}s] player.play returned", flush=True)

    def tick() -> None:
        elapsed = time.perf_counter() - t0["t"]
        print(f"  [{elapsed:6.3f}s] extra work qmediaplayer event-loop", flush=True)
        if elapsed >= 1.8:
            player.stop()
            app.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(120)
    app.exec()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="all", choices=("all", "winsound", "qaudio", "qmedia"))
    parser.add_argument("--wav", type=Path, default=None)
    args = parser.parse_args()
    wav = args.wav if args.wav else make_click_wav(OUT)
    print(f"Using {wav} ({wav.stat().st_size} bytes)", flush=True)
    print("Listen for a sharp click at the start of the tone.", flush=True)
    backends = [args.backend] if args.backend != "all" else ["winsound", "qaudio", "qmedia"]
    for name in backends:
        if name == "winsound":
            bench_winsound(wav)
        elif name == "qaudio":
            bench_qaudiosink(wav)
        else:
            bench_qmediaplayer(wav)
        time.sleep(0.3)
    print("\nDone.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
