from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.engine.ffmpeg import ffmpeg_path, start_process, stop_process
from app.engine.settings import recordings_dir


LOOPBACK_HINTS = (
    "stereo mix",
    "what u hear",
    "loopback",
    "cable output",
    "vb-audio",
    "virtual cable",
    "speakers (loopback)",
    "wave out mix",
)


@dataclass
class CaptureRegion:
    x: int
    y: int
    width: int
    height: int
    screen_index: int = 0
    screen_x: int = 0
    screen_y: int = 0


@dataclass
class CaptureRequest:
    dest: Path
    region: CaptureRegion
    mic: str | None = None
    system_audio: str | None = None
    fps: int = 30


def even(value: int) -> int:
    value = max(2, int(value))
    return value if value % 2 == 0 else value - 1


def _hide_window_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


@lru_cache(maxsize=1)
def has_ddagrab() -> bool:
    try:
        proc = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-h", "filter=ddagrab"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_hide_window_kwargs(),
        )
        text = f"{proc.stdout or ''}{proc.stderr or ''}".lower()
        return "ddagrab" in text and "unknown filter" not in text
    except Exception:
        return False


def list_dshow_devices() -> tuple[list[str], list[str]]:
    if sys.platform != "win32":
        return [], []
    try:
        proc = subprocess.run(
            [
                ffmpeg_path(),
                "-hide_banner",
                "-list_devices",
                "true",
                "-f",
                "dshow",
                "-i",
                "dummy",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_hide_window_kwargs(),
        )
    except Exception:
        return [], []
    text = proc.stderr or proc.stdout or ""
    videos: list[str] = []
    audios: list[str] = []
    section = ""
    for line in text.splitlines():
        low = line.lower()
        if "directshow video devices" in low:
            section = "video"
            continue
        if "directshow audio devices" in low:
            section = "audio"
            continue
        if "alternative name" in low:
            continue
        match = re.search(r'"([^"]+)"', line)
        if not match or not section:
            continue
        name = match.group(1).strip()
        if not name:
            continue
        if section == "video" and name not in videos:
            videos.append(name)
        elif section == "audio" and name not in audios:
            audios.append(name)
    return videos, audios


def guess_system_audio(audio_devices: list[str]) -> str | None:
    for name in audio_devices:
        low = name.lower()
        if any(hint in low for hint in LOOPBACK_HINTS):
            return name
    return None


def new_recording_path() -> Path:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return recordings_dir() / f"recording_{stamp}.mp4"


def _gdigrab_input(region: CaptureRegion, fps: int) -> list[str]:
    width = even(region.width)
    height = even(region.height)
    return [
        "-f",
        "gdigrab",
        "-framerate",
        str(fps),
        "-offset_x",
        str(int(region.x)),
        "-offset_y",
        str(int(region.y)),
        "-video_size",
        f"{width}x{height}",
        "-draw_mouse",
        "1",
        "-i",
        "desktop",
    ]


def _ddagrab_input(region: CaptureRegion, fps: int) -> list[str]:
    width = even(region.width)
    height = even(region.height)
    offset_x = max(0, int(region.x - region.screen_x))
    offset_y = max(0, int(region.y - region.screen_y))
    filt = (
        f"ddagrab=output_idx={max(0, region.screen_index)}:framerate={fps}:"
        f"draw_mouse=1:offset_x={offset_x}:offset_y={offset_y}:"
        f"video_size={width}x{height},hwdownload,format=bgra"
    )
    return ["-f", "lavfi", "-i", filt]


def _quote_dshow(name: str) -> str:
    cleaned = name.replace('"', "")
    return f"audio={cleaned}"


def _audio_inputs(mic: str | None, system_audio: str | None) -> tuple[list[str], int]:
    args: list[str] = []
    count = 0
    if mic:
        args.extend(["-f", "dshow", "-i", _quote_dshow(mic)])
        count += 1
    if system_audio and system_audio != mic:
        args.extend(["-f", "dshow", "-i", _quote_dshow(system_audio)])
        count += 1
    return args, count


def _build_args(request: CaptureRequest, *, use_ddagrab: bool) -> list[str]:
    video = (
        _ddagrab_input(request.region, request.fps)
        if use_ddagrab
        else _gdigrab_input(request.region, request.fps)
    )
    audio_args, audio_count = _audio_inputs(request.mic, request.system_audio)
    args = [*video, *audio_args]
    if audio_count == 2:
        args.extend(
            [
                "-filter_complex",
                "[1:a][2:a]amix=inputs=2:duration=longest:dropout_transition=2,aresample=44100[a]",
                "-map",
                "0:v",
                "-map",
                "[a]",
            ]
        )
    elif audio_count == 1:
        args.extend(["-map", "0:v", "-map", "1:a"])
    else:
        args.extend(["-map", "0:v", "-an"])
    args.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(request.fps),
        ]
    )
    if audio_count:
        args.extend(["-c:a", "aac", "-b:a", "160k"])
    args.append(str(request.dest))
    return args


class ScreenRecorder:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.request: CaptureRequest | None = None
        self._used_ddagrab = False

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, request: CaptureRequest) -> None:
        if self.running:
            raise RuntimeError("A recording is already in progress.")
        if sys.platform != "win32":
            raise RuntimeError("Screen recording is currently available on Windows.")
        request.dest.parent.mkdir(parents=True, exist_ok=True)
        self.request = request
        use_dda = has_ddagrab()
        self._used_ddagrab = use_dda
        args = _build_args(request, use_ddagrab=use_dda)
        self.process = start_process(args)

    def try_gdigrab_fallback(self) -> bool:
        if not self.process or not self.request or not self._used_ddagrab:
            return False
        if self.process.poll() is None:
            return False
        self._used_ddagrab = False
        args = _build_args(self.request, use_ddagrab=False)
        self.process = start_process(args)
        return True

    def early_error(self) -> str | None:
        if not self.process or self.process.poll() is None:
            return None
        stderr = stop_process(self.process, timeout=2)
        self.process = None
        text = (stderr or "").strip()
        return text[-2000:] if text else "FFmpeg exited before the recording started."

    def stop(self) -> Path:
        if not self.request:
            raise RuntimeError("No recording was started.")
        dest = self.request.dest
        if self.process:
            stop_process(self.process)
            self.process = None
        self.request = None
        if not dest.is_file() or dest.stat().st_size < 1000:
            raise RuntimeError("Recording file was not written. Check FFmpeg and the selected devices.")
        return dest
