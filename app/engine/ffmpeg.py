from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


def _hide_window_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def _project_tools() -> list[Path]:
    root = Path(__file__).resolve().parents[2]
    exe = ".exe" if sys.platform == "win32" else ""
    return [
        root / "tools" / "ffmpeg" / "bin",
        root / "tools" / "ffmpeg",
    ]


@lru_cache(maxsize=1)
def _bundled_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    exe = f"{name}.exe" if sys.platform == "win32" else name
    extra = []
    for folder in _project_tools():
        extra.append(folder / exe)
    if sys.platform == "win32":
        extra.extend(
            [
                Path(r"C:\ffmpeg\bin") / exe,
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "ffmpeg"
                / "bin"
                / exe,
            ]
        )
    for path in extra:
        if path.is_file():
            return str(path)
    if name == "ffmpeg":
        bundled = _bundled_ffmpeg()
        if bundled and Path(bundled).is_file():
            return bundled
    return None


def ffmpeg_path() -> str:
    path = find_tool("ffmpeg")
    if not path:
        raise FileNotFoundError(
            "FFmpeg was not found. Install FFmpeg or keep imageio-ffmpeg installed."
        )
    return path


def ffprobe_path() -> str | None:
    return find_tool("ffprobe")


def ffmpeg_available() -> tuple[bool, str]:
    ff = find_tool("ffmpeg")
    if ff:
        return True, ff
    return False, "FFmpeg is missing. Install it or pip install imageio-ffmpeg."


def run(args: list[str], *, tool: str = "ffmpeg") -> str:
    if tool == "ffprobe":
        binary = ffprobe_path()
        if not binary:
            raise FileNotFoundError("ffprobe is not available.")
        cmd = [binary, *args]
    else:
        cmd = [ffmpeg_path(), "-hide_banner", "-y", *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hide_window_kwargs(),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Unknown FFmpeg error").strip()
        raise RuntimeError(detail[-4000:])
    return proc.stdout


def _parse_ffmpeg_info(path: str | Path) -> dict:
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hide_window_kwargs(),
    )
    text = proc.stderr or proc.stdout or ""
    duration = 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if match:
        duration = (
            int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
        )
    width = height = 0
    fps = 30.0
    size = re.search(r"(\d{2,5})x(\d{2,5})", text)
    if size:
        width, height = int(size.group(1)), int(size.group(2))
    rate = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if rate:
        fps = float(rate.group(1))
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps or 30.0,
        "has_video": "Video:" in text,
        "has_audio": "Audio:" in text,
    }


def probe(path: str | Path) -> dict:
    if not ffprobe_path():
        return {}
    raw = run(
        [
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        tool="ffprobe",
    )
    return json.loads(raw or "{}")


def media_info(path: str | Path) -> dict:
    data = probe(path)
    if not data:
        return _parse_ffmpeg_info(path)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = 0.0
    if data.get("format", {}).get("duration"):
        duration = float(data["format"]["duration"])
    elif video and video.get("duration"):
        duration = float(video["duration"])
    width = int(video["width"]) if video and video.get("width") else 0
    height = int(video["height"]) if video and video.get("height") else 0
    fps = 30.0
    if video and video.get("avg_frame_rate") and video["avg_frame_rate"] != "0/0":
        num, _, den = video["avg_frame_rate"].partition("/")
        try:
            fps = float(num) / float(den or "1")
        except ValueError:
            fps = 30.0
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps or 30.0,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def audio_duration(path: str | Path) -> float:
    return float(media_info(path)["duration"])
