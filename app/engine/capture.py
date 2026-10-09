from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from app.engine.ffmpeg import (
    _decode_subprocess,
    ffmpeg_candidates,
    ffmpeg_path,
    media_info,
    run,
    start_process,
    stop_process,
)
from app.engine.loopback import LoopbackRecorder, is_speaker_loopback
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


_DSHOW_QUOTED = re.compile(r'"([^"]+)"(.*)$')


def list_dshow_devices() -> tuple[list[str], list[str], str]:
    """Return video names, audio names, and an error string.

    An empty error means a DirectShow listing ran. FFmpeg 7 prints
    ``"Name" (audio)`` on one line. Older builds print a section header,
    then the quoted name. Some Windows builds write that list as UTF-16.
    """
    if sys.platform != "win32":
        return [], [], ""
    try:
        primary = ffmpeg_path()
    except Exception as exc:
        return [], [], str(exc)
    paths: list[str] = []
    for path in (primary, *ffmpeg_candidates()):
        if path not in paths:
            paths.append(path)
    errors: list[str] = []
    ran = False
    for path in paths:
        videos, audios, error, ok = _list_dshow_from(path)
        if videos or audios:
            return videos, audios, ""
        if ok:
            ran = True
        elif error:
            errors.append(error)
    if ran:
        return [], [], ""
    if errors:
        return [], [], errors[0]
    return [], [], "FFmpeg did not list DirectShow devices."


def _list_dshow_from(path: str) -> tuple[list[str], list[str], str, bool]:
    try:
        proc = subprocess.run(
            [
                path,
                "-hide_banner",
                "-list_devices",
                "true",
                "-f",
                "dshow",
                "-i",
                "dummy",
            ],
            capture_output=True,
            **_hide_window_kwargs(),
        )
    except Exception as exc:
        return [], [], str(exc), False
    text = _decode_subprocess(proc.stderr or b"")
    if proc.stdout:
        text = f"{text}\n{_decode_subprocess(proc.stdout)}"
    videos, audios = _parse_dshow_listing(text)
    if videos or audios:
        return videos, audios, "", True
    low = text.lower()
    if "unknown input format" in low or "not recognized" in low or "no such filter" in low:
        detail = " ".join(text.split())
        return [], [], (detail[-500:] if detail else "FFmpeg could not list DirectShow devices."), False
    if not text.strip():
        return [], [], "FFmpeg did not list DirectShow devices.", False
    return [], [], "", True


def _parse_dshow_listing(text: str) -> tuple[list[str], list[str]]:
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
        match = _DSHOW_QUOTED.search(line)
        if not match:
            continue
        name, suffix = match.group(1), match.group(2)
        kinds = _media_kinds(suffix)
        if not kinds and section:
            kinds = [section]
        for kind in kinds:
            _append_device(videos, audios, name, kind)
    return videos, audios


def _media_kinds(suffix: str) -> list[str]:
    kinds: list[str] = []
    for blob in re.findall(r"\(([^)]*)\)", suffix):
        low = blob.lower()
        for kind in ("video", "audio"):
            if re.search(rf"\b{kind}\b", low) and kind not in kinds:
                kinds.append(kind)
        if "none" in low and not kinds:
            kinds.append("none")
    return kinds


def _append_device(videos: list[str], audios: list[str], name: str, kind: str) -> None:
    cleaned = name.strip()
    if not cleaned or kind == "none":
        return
    if kind == "video" and cleaned not in videos:
        videos.append(cleaned)
    elif kind == "audio" and cleaned not in audios:
        audios.append(cleaned)


def guess_system_audio(audio_devices: list[str]) -> str | None:
    for name in audio_devices:
        low = name.lower()
        if any(hint in low for hint in LOOPBACK_HINTS):
            return name
    return None


def new_recording_path(directory: str | Path | None = None) -> Path:
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = Path(directory) if directory else recordings_dir()
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / f"recording_{stamp}.mp4"
    index = 2
    while candidate.exists():
        candidate = folder / f"recording_{stamp}_{index}.mp4"
        index += 1
    return candidate


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


def _dshow_request(request: CaptureRequest) -> CaptureRequest:
    if is_speaker_loopback(request.system_audio):
        return replace(request, system_audio=None)
    return request


def _audio_inputs(mic: str | None, system_audio: str | None) -> tuple[list[str], int]:
    args: list[str] = []
    count = 0
    if mic and not is_speaker_loopback(mic):
        args.extend(["-f", "dshow", "-i", _quote_dshow(mic)])
        count += 1
    if system_audio and system_audio != mic and not is_speaker_loopback(system_audio):
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
    # Fragments keep the file playable when FFmpeg is killed before it can
    # write a normal moov atom (device loss, or a hard stop).
    args.extend(
        [
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-flush_packets",
            "1",
        ]
    )
    args.append(str(request.dest))
    return args


def _trim_partial_atom(path: Path) -> None:
    """Drop a trailing fragment that was cut off when FFmpeg was killed."""
    total = path.stat().st_size
    last = 0
    with path.open("rb") as handle:
        while True:
            offset = handle.tell()
            header = handle.read(8)
            if len(header) < 8:
                break
            size = int.from_bytes(header[:4], "big")
            header_len = 8
            if size == 1:
                extra = handle.read(8)
                if len(extra) < 8:
                    break
                size = int.from_bytes(extra, "big")
                header_len = 16
            if size < header_len or offset + size > total:
                break
            handle.seek(offset + size)
            last = offset + size
    if 0 < last < total:
        with path.open("rb+") as handle:
            handle.truncate(last)


def _prepare_recording(path: Path) -> None:
    """Turn a capture, including one that was killed, into a normal MP4."""
    if not path.is_file() or path.stat().st_size < 1000:
        return
    _trim_partial_atom(path)
    fixed = path.with_name(f"{path.stem}.fast{path.suffix}")
    try:
        run(
            [
                "-i",
                str(path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(fixed),
            ]
        )
        if fixed.is_file() and fixed.stat().st_size > 1000:
            fixed.replace(path)
    except Exception:
        pass
    finally:
        if fixed.exists():
            fixed.unlink(missing_ok=True)


class ScreenRecorder:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.request: CaptureRequest | None = None
        self._used_ddagrab = False
        self._loopback: LoopbackRecorder | None = None

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
        if is_speaker_loopback(request.system_audio):
            self._loopback = LoopbackRecorder(request.dest.with_suffix(".loopback.wav"))
            try:
                self._loopback.start()
            except Exception:
                self._loopback = None
                self.request = None
                raise
        try:
            args = _build_args(_dshow_request(request), use_ddagrab=use_dda)
            self.process = start_process(args)
        except Exception:
            self._cancel_loopback()
            self.request = None
            raise

    def try_gdigrab_fallback(self) -> bool:
        if not self.process or not self.request or not self._used_ddagrab:
            return False
        if self.process.poll() is None:
            return False
        failed = self.process
        self._used_ddagrab = False
        args = _build_args(_dshow_request(self.request), use_ddagrab=False)
        self.process = start_process(args)
        stop_process(failed, timeout=1)
        return True

    def early_error(self) -> str | None:
        if not self.process or self.process.poll() is None:
            return None
        stderr = stop_process(self.process, timeout=2)
        self.process = None
        dest = self.request.dest if self.request else None
        self._cancel_loopback()
        if dest and dest.is_file():
            _prepare_recording(dest)
        text = (stderr or "").strip()
        return text[-2000:] if text else "FFmpeg exited before the recording started."

    def stop(self) -> Path:
        if not self.request:
            raise RuntimeError("No recording was started.")
        dest = self.request.dest
        loopback = self._loopback
        self._loopback = None
        if self.process:
            stop_process(self.process)
            self.process = None
        self.request = None
        wav: Path | None = None
        try:
            if loopback:
                wav = loopback.stop()
            _prepare_recording(dest)
            if not dest.is_file() or dest.stat().st_size < 1000:
                raise RuntimeError(
                    "Recording file was not written. Check FFmpeg and the selected devices."
                )
            try:
                info = media_info(dest)
            except Exception as exc:
                raise RuntimeError(str(exc)) from exc
            if not info.get("has_video") or float(info.get("duration") or 0) <= 0:
                raise RuntimeError(
                    "This recording is incomplete. It was cut off before it finished saving."
                )
            if wav:
                _mix_loopback(dest, wav)
        finally:
            if wav:
                wav.unlink(missing_ok=True)
        return dest

    def _cancel_loopback(self) -> None:
        recorder = self._loopback
        self._loopback = None
        if recorder:
            recorder.cancel()


def _mix_loopback(video: Path, wav: Path) -> None:
    info = media_info(video)
    mixed = video.with_name(f"{video.stem}.mix{video.suffix}")
    try:
        if info.get("has_audio"):
            graph = (
                "[0:a]aresample=44100[m];"
                "[1:a]aresample=44100[s];"
                "[m][s]amix=inputs=2:duration=longest:dropout_transition=2,apad[a]"
            )
        else:
            graph = "[1:a]aresample=44100,apad[a]"
        run(
            [
                "-i",
                str(video),
                "-i",
                str(wav),
                "-filter_complex",
                graph,
                "-map",
                "0:v",
                "-map",
                "[a]",
                "-shortest",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                str(mixed),
            ]
        )
        mixed.replace(video)
    finally:
        if mixed.exists():
            mixed.unlink(missing_ok=True)
