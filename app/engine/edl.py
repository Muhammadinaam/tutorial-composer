from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.engine.ffmpeg import media_info
from app.engine.project import IMAGE_EXTS, Clip, Cue, new_id

DEFAULT_IMAGE_SECONDS = 5.0
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}


def is_image_path(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


def is_audio_path(path: str) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTS


def probe_audio(path: str) -> tuple[str, float]:
    info = media_info(path)
    duration = float(info.get("duration") or 0.0)
    if duration <= 0 and not info.get("has_audio"):
        raise ValueError(f"Not a usable audio file: {path}")
    return path, max(duration, 0.1)


def joined_duration(clips: list[Clip]) -> float:
    return sum(c.used for c in clips)


def probe_clip(path: str) -> Clip:
    if is_image_path(path):
        try:
            info = media_info(path)
        except Exception:
            info = {"width": 0, "height": 0, "duration": 0, "has_video": True}
        seconds = DEFAULT_IMAGE_SECONDS
        if not info.get("width") and not info.get("has_video"):
            raise ValueError(f"Not a usable image file: {path}")
        return Clip(
            path=path,
            in_point=0.0,
            out_point=seconds,
            duration=seconds,
            kind="image",
        )
    info = media_info(path)
    duration = float(info["duration"])
    if not info["has_video"] or duration <= 0:
        raise ValueError(f"Not a usable video file: {path}")
    return Clip(path=path, in_point=0.0, out_point=duration, duration=duration, kind="video")


def map_joined_to_clip(clips: list[Clip], joined_time: float) -> tuple[Clip, float, int] | None:
    if not clips:
        return None
    acc = 0.0
    last_index = len(clips) - 1
    for index, clip in enumerate(clips):
        clip_end = acc + clip.used
        if joined_time < clip_end - 0.0005 or index == last_index:
            local = clip.in_point + max(0.0, joined_time - acc)
            local = min(clip.out_point, local)
            return clip, local, index
        acc = clip_end
    last = clips[-1]
    return last, last.out_point, last_index


def clip_joined_start(clips: list[Clip], index: int) -> float:
    return sum(c.used for c in clips[:index])


def split_clip(clips: list[Clip], index: int, at_local_time: float) -> list[Clip]:
    clip = clips[index]
    if at_local_time <= clip.in_point + 0.05 or at_local_time >= clip.out_point - 0.05:
        return clips
    left = replace(clip, id=new_id(), out_point=at_local_time)
    right = replace(clip, id=new_id(), in_point=at_local_time)
    return clips[:index] + [left, right] + clips[index + 1 :]


def move_clip(clips: list[Clip], index: int, delta: int) -> list[Clip]:
    dest = index + delta
    if dest < 0 or dest >= len(clips):
        return clips
    updated = list(clips)
    updated[index], updated[dest] = updated[dest], updated[index]
    return updated


def delete_joined_range(clips: list[Clip], start: float, end: float) -> list[Clip]:
    start, end = (min(start, end), max(start, end))
    if end - start < 0.05 or not clips:
        return clips
    result: list[Clip] = []
    cursor = 0.0
    for clip in clips:
        clip_start = cursor
        clip_end = cursor + clip.used
        cursor = clip_end
        if clip_end <= start + 0.001 or clip_start >= end - 0.001:
            result.append(clip)
            continue
        if clip_start >= start - 0.001 and clip_end <= end + 0.001:
            continue
        if start > clip_start + 0.05:
            left_out = clip.in_point + (min(start, clip_end) - clip_start)
            result.append(replace(clip, id=new_id(), out_point=left_out))
        if end < clip_end - 0.05:
            right_in = clip.in_point + (max(end, clip_start) - clip_start)
            result.append(replace(clip, id=new_id(), in_point=right_in))
    return result


def ripple_cues(cues: list[Cue], start: float, end: float) -> list[Cue]:
    lo, hi = (min(start, end), max(start, end))
    gap = hi - lo
    if gap < 0.05:
        return list(cues)
    result: list[Cue] = []
    for cue in cues:
        if cue.video_time >= hi - 0.001:
            result.append(
                Cue(
                    video_time=max(0.0, cue.video_time - gap),
                    text=cue.text,
                    should_video_stop=cue.should_video_stop,
                )
            )
        elif cue.video_time < lo + 0.001:
            result.append(cue)
    return result


def trim_clip(clip: Clip, new_in: float | None = None, new_out: float | None = None) -> None:
    if new_in is not None:
        clip.in_point = max(0.0, min(new_in, clip.out_point - 0.08))
    if new_out is not None:
        limit = 600.0 if clip.is_image else max(clip.duration, clip.out_point)
        if clip.is_image:
            clip.out_point = max(clip.in_point + 0.08, new_out)
            clip.duration = max(clip.duration, clip.out_point)
        else:
            clip.out_point = min(limit, max(clip.in_point + 0.08, new_out))


def target_video_size(clips: list[Clip]) -> tuple[int, int, float]:
    for clip in clips:
        try:
            info = media_info(clip.path)
        except Exception:
            continue
        if info["width"] and info["height"]:
            fps = 30.0 if clip.is_image else float(info["fps"] or 30.0)
            return int(info["width"]), int(info["height"]), fps
    return 1920, 1080, 30.0
