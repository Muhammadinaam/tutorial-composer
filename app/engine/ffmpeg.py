from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
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


def _ffmpeg_search_paths() -> list[str]:
    ordered: list[str] = []

    def add(path: str | None) -> None:
        if not path:
            return
        resolved = str(Path(path))
        if resolved not in ordered and Path(resolved).is_file():
            ordered.append(resolved)

    # The imageio build includes DirectShow. A ffmpeg earlier on PATH often
    # does not, which is why one PC lists microphones and another does not.
    if sys.platform == "win32":
        add(_bundled_ffmpeg())
    add(shutil.which("ffmpeg"))
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    for folder in _project_tools():
        add(str(folder / exe))
    if sys.platform == "win32":
        add(str(Path(r"C:\ffmpeg\bin") / exe))
        add(
            str(
                Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "ffmpeg"
                / "bin"
                / exe
            )
        )
        local = os.environ.get("LOCALAPPDATA")
        if local:
            add(str(Path(local) / "Microsoft" / "WinGet" / "Links" / exe))
    if sys.platform != "win32":
        add(_bundled_ffmpeg())
    return ordered


def ffmpeg_candidates() -> list[str]:
    return _ffmpeg_search_paths()


def _ffmpeg_formats(path: str) -> str:
    try:
        proc = subprocess.run(
            [path, "-hide_banner", "-formats"],
            capture_output=True,
            **_hide_window_kwargs(),
        )
    except Exception:
        return ""
    return _decode_subprocess(proc.stdout or b"") + _decode_subprocess(proc.stderr or b"")


def ffmpeg_has_dshow(path: str) -> bool:
    return " dshow " in _ffmpeg_formats(path).lower()


@lru_cache(maxsize=1)
def _chosen_ffmpeg() -> str | None:
    candidates = _ffmpeg_search_paths()
    if sys.platform == "win32":
        for path in candidates:
            if ffmpeg_has_dshow(path):
                return path
    return candidates[0] if candidates else None


def find_tool(name: str) -> str | None:
    if name == "ffmpeg":
        return _chosen_ffmpeg()
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
    return None


def ffmpeg_path() -> str:
    path = find_tool("ffmpeg")
    if not path:
        raise FileNotFoundError(
            "FFmpeg was not found. Install FFmpeg or keep imageio-ffmpeg installed."
        )
    return path


def _decode_subprocess(data: bytes) -> str:
    """Read FFmpeg output from a pipe.

    Some Windows builds write the device list as UTF-16 when there is no
    console. Decoding those bytes as UTF-8 drops every device name.
    """
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace").replace("\x00", "")
    sample = data[:400]
    if sample.count(0) > max(4, len(sample) // 4):
        encoding = "utf-16-le" if data[:2] != b"\xfe\xff" else "utf-16-be"
        return data.decode(encoding, errors="replace").replace("\x00", "")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if sys.platform == "win32":
            return data.decode("mbcs", errors="replace")
        return data.decode("utf-8", errors="replace")


def ffprobe_path() -> str | None:
    return find_tool("ffprobe")


def ffmpeg_available() -> tuple[bool, str]:
    ff = find_tool("ffmpeg")
    if ff:
        return True, ff
    return False, "FFmpeg is missing. Install it or pip install imageio-ffmpeg."


def _progress_seconds(line: str) -> float | None:
    text = line.strip()
    if text.startswith("out_time_us="):
        raw = text.split("=", 1)[1]
        if raw.isdigit():
            return int(raw) / 1_000_000.0
        return None
    if text.startswith("out_time_ms="):
        raw = text.split("=", 1)[1]
        if raw.isdigit():
            return int(raw) / 1000.0
        return None
    if text.startswith("out_time="):
        raw = text.split("=", 1)[1].strip()
        if not raw or raw == "N/A":
            return None
        parts = raw.split(":")
        if len(parts) != 3:
            return None
        try:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return None
    return None


def start_process(args: list[str]) -> subprocess.Popen:
    cmd = [ffmpeg_path(), "-hide_banner", "-y", *args]
    kwargs = _hide_window_kwargs()
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        **kwargs,
    )


def stop_process(process: subprocess.Popen, timeout: float = 12.0) -> str:
    if process.poll() is None:
        try:
            if process.stdin:
                process.stdin.write(b"q")
                process.stdin.flush()
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass
        try:
            _stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            _stdout, stderr = process.communicate(timeout=3)
        detail = stderr or b""
        if isinstance(detail, bytes):
            return detail.decode("utf-8", errors="replace")
        return str(detail)
    try:
        _stdout, stderr = process.communicate(timeout=1)
    except Exception:
        stderr = b""
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="replace")
    return str(stderr or "")


def run(
    args: list[str],
    *,
    tool: str = "ffmpeg",
    on_progress: Callable[[float], None] | None = None,
    duration: float | None = None,
) -> str:
    if tool == "ffprobe":
        binary = ffprobe_path()
        if not binary:
            raise FileNotFoundError("ffprobe is not available.")
        cmd = [binary, *args]
        on_progress = None
    elif on_progress is None:
        cmd = [ffmpeg_path(), "-hide_banner", "-y", *args]
    else:
        cmd = [
            ffmpeg_path(),
            "-hide_banner",
            "-nostats",
            "-progress",
            "pipe:1",
            "-y",
            *args,
        ]

    if on_progress is None:
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
        return proc.stdout or ""

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_hide_window_kwargs(),
    )
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        if process.stderr:
            stderr_chunks.append(process.stderr.read() or "")

    reader = threading.Thread(target=_drain_stderr, daemon=True)
    reader.start()
    last_pct = -1
    total = max(0.0, float(duration or 0.0))
    try:
        assert process.stdout is not None
        for line in process.stdout:
            seconds = _progress_seconds(line)
            if seconds is not None and total > 0.05:
                frac = min(1.0, max(0.0, seconds / total))
                pct = int(frac * 100)
                if pct != last_pct:
                    last_pct = pct
                    on_progress(frac)
            elif line.strip() == "progress=end" and total > 0.05:
                on_progress(1.0)
    finally:
        if process.stdout:
            process.stdout.close()
        process.wait()
        reader.join(timeout=8)
    if process.returncode != 0:
        detail = "".join(stderr_chunks).strip() or "Unknown FFmpeg error"
        raise RuntimeError(detail[-4000:])
    if last_pct < 100:
        on_progress(1.0)
    return ""


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
