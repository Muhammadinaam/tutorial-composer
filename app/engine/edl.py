from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.engine.ffmpeg import media_info
from app.engine.project import IMAGE_EXTS, BlurRegion, Clip, Cue, Pause, new_id

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


def make_pause(duration: float = 1.5) -> Clip:
    seconds = max(0.2, min(30.0, float(duration)))
    return Clip(path="", in_point=0.0, out_point=seconds, duration=seconds, kind="pause")


def insert_pause(clips: list[Clip], at: float, duration: float = 1.5) -> list[Clip]:
    """Split the clip under ``at`` and place a freeze between the two pieces.

    Narration times are left alone. Only the picture track grows.
    """
    if not clips:
        return clips
    total = joined_duration(clips)
    at = min(max(0.0, float(at)), total)
    mapped = map_joined_to_clip(clips, at)
    if mapped is None:
        return clips
    clip, _local, index = mapped
    if clip.is_pause:
        clip.out_point = max(0.2, clip.out_point + float(duration))
        clip.duration = clip.out_point
        clip.in_point = 0.0
        return clips
    pause = make_pause(duration)
    start = clip_joined_start(clips, index)
    into = at - start
    if into <= 0.05:
        return clips[:index] + [pause] + clips[index:]
    if into >= clip.used - 0.05:
        return clips[: index + 1] + [pause] + clips[index + 1 :]
    pieces = split_clip(clips, index, clip.in_point + into)
    if len(pieces) == len(clips):
        return clips[: index + 1] + [pause] + clips[index + 1 :]
    return pieces[: index + 1] + [pause] + pieces[index + 1 :]


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


def reorder_clip(clips: list[Clip], source: int, dest: int) -> list[Clip]:
    """Place the clip at ``source`` so it lands at ``dest`` in the new list."""
    if not clips or not (0 <= source < len(clips)):
        return clips
    dest = max(0, min(int(dest), len(clips) - 1))
    if source == dest:
        return clips
    updated = list(clips)
    clip = updated.pop(source)
    updated.insert(dest, clip)
    return updated


def remap_joined_time(old_clips: list[Clip], new_clips: list[Clip], moment: float) -> float:
    """Keep a timeline time on the same clip after that clip moves."""
    if not old_clips:
        return moment
    if moment > joined_duration(old_clips) + 0.001:
        return moment
    acc = 0.0
    owner_id = None
    offset = 0.0
    last = len(old_clips) - 1
    for index, clip in enumerate(old_clips):
        clip_end = acc + clip.used
        if moment < clip_end - 0.0005 or index == last:
            owner_id = clip.id
            offset = max(0.0, moment - acc)
            break
        acc = clip_end
    if owner_id is None:
        return moment
    acc = 0.0
    for clip in new_clips:
        if clip.id == owner_id:
            return acc + min(max(0.0, offset), clip.used)
        acc += clip.used
    return moment


def remap_cues(old_clips: list[Clip], new_clips: list[Clip], cues: list[Cue]) -> list[Cue]:
    moved = [
        Cue(
            video_time=remap_joined_time(old_clips, new_clips, cue.video_time),
            text=cue.text,
            should_video_stop=cue.should_video_stop,
        )
        for cue in cues
    ]
    moved.sort(key=lambda cue: cue.video_time)
    return moved


def remap_pauses(old_clips: list[Clip], new_clips: list[Clip], pauses: list[Pause]) -> list[Pause]:
    total = joined_duration(new_clips)
    moved: list[Pause] = []
    for pause in pauses:
        at = remap_joined_time(old_clips, new_clips, pause.at)
        if total > 0:
            at = min(at, total)
        updated = replace(pause, at=at)
        updated.clamp()
        moved.append(updated)
    return moved


def remap_blurs(
    old_clips: list[Clip], new_clips: list[Clip], blurs: list[BlurRegion]
) -> list[BlurRegion]:
    total = joined_duration(new_clips)
    moved: list[BlurRegion] = []
    for blur in blurs:
        duration = max(0.08, float(blur.end) - float(blur.start))
        midpoint = (float(blur.start) + float(blur.end)) / 2.0
        center = remap_joined_time(old_clips, new_clips, midpoint)
        start = center - duration / 2.0
        end = start + duration
        if start < 0:
            end -= start
            start = 0.0
        if total > 0 and end > total:
            start = max(0.0, start - (end - total))
            end = total
        updated = replace(blur, start=start, end=max(start + 0.08, end))
        updated.clamp()
        moved.append(updated)
    return moved


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


def ripple_pauses(pauses: list[Pause], start: float, end: float) -> list[Pause]:
    lo, hi = (min(start, end), max(start, end))
    gap = hi - lo
    if gap < 0.05:
        return list(pauses)
    result: list[Pause] = []
    for pause in pauses:
        if pause.at >= hi - 0.001:
            updated = replace(pause, at=max(0.0, pause.at - gap))
            updated.clamp()
            result.append(updated)
        elif pause.at < lo + 0.001:
            result.append(pause)
    return result


def ripple_blurs(blurs: list[BlurRegion], start: float, end: float) -> list[BlurRegion]:
    lo, hi = (min(start, end), max(start, end))
    gap = hi - lo
    if gap < 0.05:
        return list(blurs)
    result: list[BlurRegion] = []
    for blur in blurs:
        b0, b1 = float(blur.start), float(blur.end)
        if b1 <= lo + 0.001:
            result.append(blur)
            continue
        if b0 >= hi - 0.001:
            shifted = replace(blur, start=max(0.0, b0 - gap), end=max(0.08, b1 - gap))
            shifted.clamp()
            result.append(shifted)
            continue
        kept_start = b0 if b0 < lo - 0.001 else None
        kept_end = b1 - gap if b1 > hi + 0.001 else None
        if kept_start is not None and kept_end is not None:
            merged = replace(blur, start=kept_start, end=kept_end)
            merged.clamp()
            result.append(merged)
            continue
        if kept_start is not None and lo - kept_start >= 0.05:
            trimmed = replace(blur, start=kept_start, end=lo)
            trimmed.clamp()
            result.append(trimmed)
            continue
        if kept_end is not None and kept_end - lo >= 0.05:
            trimmed = replace(blur, start=lo, end=kept_end)
            trimmed.clamp()
            result.append(trimmed)
    return result


def trim_clip(clip: Clip, new_in: float | None = None, new_out: float | None = None) -> None:
    if clip.is_pause:
        if new_out is not None:
            clip.out_point = max(0.2, min(30.0, float(new_out)))
            clip.in_point = 0.0
            clip.duration = clip.out_point
        return
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
        if clip.is_pause or not clip.path:
            continue
        try:
            info = media_info(clip.path)
        except Exception:
            continue
        if info["width"] and info["height"]:
            fps = 30.0 if clip.is_image else float(info["fps"] or 30.0)
            return int(info["width"]), int(info["height"]), fps
    return 1920, 1080, 30.0
