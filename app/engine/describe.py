from __future__ import annotations

import base64
import json
import tempfile
from collections.abc import Callable
from pathlib import Path

from app.engine.edl import joined_duration, map_joined_to_clip
from app.engine.ffmpeg import run
from app.engine.project import Clip, Cue
from app.engine.script import parse_timestamp
from app.engine.translate import LANGUAGE_NAMES

MAX_FRAMES = 16
MIN_INTERVAL = 2.0
FRAME_WIDTH = 768
# Mean absolute difference on a tiny grayscale frame. Below this, the picture
# has not changed enough to spend another image on the request.
SIMILAR_THRESHOLD = 8.0

Progress = Callable[..., None]


def describe_timeline(
    clips: list[Clip],
    lang: str,
    api_key: str,
    on_progress: Progress | None = None,
) -> list[Cue]:
    if not api_key:
        raise ValueError(
            "Add an OpenAI API key in Settings to write narration from the video."
        )
    if not clips:
        raise ValueError("Add a video first.")
    duration = joined_duration(clips)
    if duration <= 0:
        raise ValueError("The video has no length to describe.")

    _emit(on_progress, "Reading the video…", 5)
    with tempfile.TemporaryDirectory(prefix="describe-") as folder:
        frames = _sample_frames(clips, duration, Path(folder), on_progress)
    if not frames:
        raise ValueError("Could not read any frames from the video.")

    _emit(on_progress, "Writing narration…", 70)
    cues = _ask_for_cues(frames, duration, lang, api_key)
    _emit(on_progress, "Narration ready.", 100)
    return cues


def _sample_frames(
    clips: list[Clip],
    duration: float,
    folder: Path,
    on_progress: Progress | None,
) -> list[tuple[float, bytes]]:
    times = _sample_times(duration)
    kept: list[tuple[float, bytes]] = []
    previous: bytes | None = None
    seen_images: set[str] = set()
    total = len(times)
    for index, moment in enumerate(times):
        _emit(
            on_progress,
            f"Reading frame {index + 1} of {total}…",
            5 + int(60 * index / max(1, total)),
        )
        mapped = map_joined_to_clip(clips, moment)
        if mapped is None:
            continue
        clip, local, _clip_index = mapped
        if clip.is_image and clip.id in seen_images:
            continue
        dest = folder / f"frame-{index:02d}.jpg"
        try:
            _extract_jpeg(clip, local, dest)
        except (RuntimeError, OSError):
            continue
        if not dest.is_file() or dest.stat().st_size <= 0:
            continue
        fingerprint = _fingerprint(dest)
        if previous is not None and _similar(previous, fingerprint):
            dest.unlink(missing_ok=True)
            continue
        if clip.is_image:
            seen_images.add(clip.id)
        previous = fingerprint or previous
        kept.append((moment, dest.read_bytes()))
        if len(kept) >= MAX_FRAMES:
            break
    return kept


def _sample_times(duration: float) -> list[float]:
    count = min(MAX_FRAMES, max(1, int(duration / MIN_INTERVAL)))
    if count == 1:
        return [min(duration * 0.5, max(0.0, duration - 0.05))]
    span = max(0.0, duration - 0.05)
    return [i * span / (count - 1) for i in range(count)]


def _extract_jpeg(clip: Clip, local_time: float, dest: Path) -> None:
    args = ["-i", clip.path]
    if not clip.is_image:
        args.extend(["-ss", f"{max(0.0, local_time):.3f}"])
    args.extend(
        [
            "-frames:v",
            "1",
            "-vf",
            f"scale={FRAME_WIDTH}:-2",
            "-q:v",
            "8",
            str(dest),
        ]
    )
    run(args)


def _fingerprint(path: Path) -> bytes:
    raw = path.with_suffix(".raw")
    try:
        run(
            [
                "-i",
                str(path),
                "-vf",
                "scale=32:18,format=gray",
                "-f",
                "rawvideo",
                str(raw),
            ]
        )
        if raw.is_file():
            return raw.read_bytes()
    except (RuntimeError, OSError):
        return b""
    finally:
        raw.unlink(missing_ok=True)
    return b""


def _similar(left: bytes, right: bytes) -> bool:
    if not left or not right or len(left) != len(right):
        return False
    total = sum(abs(a - b) for a, b in zip(left, right))
    return (total / len(left)) < SIMILAR_THRESHOLD


def _ask_for_cues(
    frames: list[tuple[float, bytes]],
    duration: float,
    lang: str,
    api_key: str,
) -> list[Cue]:
    from openai import OpenAI

    language = LANGUAGE_NAMES.get(lang, lang)
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"Video length: {duration:.2f} seconds. "
                f"Write the narration in {language}. "
                "Each image is labeled with the time it was taken."
            ),
        }
    ]
    for moment, data in frames:
        content.append({"type": "text", "text": f"Frame at {moment:.2f} seconds."})
        encoded = base64.b64encode(data).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{encoded}",
                    "detail": "low",
                },
            }
        )

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        max_tokens=1500,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You write spoken tutorial narration from screenshots of a "
                    "screen recording. Return JSON: "
                    '{"cues":[{"time":0.0,"text":"..."}]}. '
                    "time is seconds from the start of the video. "
                    "Write one short spoken line for each real on-screen change. "
                    "Skip frames where nothing meaningful happened. "
                    "Use a calm tutorial tone. Do not describe the recording itself. "
                    f"Write in {language}. "
                    f"Every time must be between 0 and {duration:.2f}."
                ),
            },
            {"role": "user", "content": content},
        ],
    )
    raw = response.choices[0].message.content or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("The model did not return narration that could be read.") from exc
    cues = _cues_from_payload(payload, duration)
    if not cues:
        raise ValueError("The model did not return any narration lines.")
    return cues


def _cues_from_payload(payload: object, duration: float) -> list[Cue]:
    items = _payload_items(payload)
    cues: list[Cue] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("line") or "").strip()
        if not text:
            continue
        moment = _cue_time(item.get("time", item.get("video_time", item.get("start"))), duration)
        if moment is None:
            continue
        cues.append(Cue(video_time=moment, text=text, should_video_stop=False))
    cues.sort(key=lambda cue: cue.video_time)
    return cues


def _payload_items(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("cues", "items", "lines", "narration"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _cue_time(value: object, duration: float) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        moment = float(value)
    else:
        try:
            moment = parse_timestamp(str(value))
        except ValueError:
            return None
    if moment != moment:
        return None
    return max(0.0, min(float(duration), moment))


def _emit(on_progress: Progress | None, message: str, percent: float | None = None) -> None:
    if on_progress is None:
        return
    if percent is None:
        on_progress(message)
    else:
        on_progress(message, percent)
