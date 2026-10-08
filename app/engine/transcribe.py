from __future__ import annotations

import uuid
from pathlib import Path

from app.engine.ffmpeg import media_info, run
from app.engine.project import Clip, Cue
from app.engine.settings import cache_dir
from app.engine.translate import LANGUAGE_NAMES


def transcribe_timeline(clips: list[Clip], lang: str, api_key: str) -> list[Cue]:
    if not api_key:
        raise ValueError("Add an OpenAI API key in Settings to draft the script from audio.")
    if not clips:
        raise ValueError("Add a video first.")
    language = LANGUAGE_NAMES.get(lang, lang)
    dest = cache_dir() / f"speech_{uuid.uuid4().hex[:12]}.mp3"
    try:
        _extract_timeline_audio(clips, dest)
        if dest.stat().st_size > 25 * 1024 * 1024:
            raise ValueError("The recording audio is too long to transcribe in one request.")
        return _transcribe_file(dest, lang, api_key, language)
    finally:
        dest.unlink(missing_ok=True)


def _extract_timeline_audio(clips: list[Clip], dest: Path) -> None:
    pieces: list[tuple[str, Clip | float]] = []
    any_audio = False
    for clip in clips:
        if clip.is_pause or clip.is_image or not clip.path:
            pieces.append(("silence", max(0.04, clip.used)))
            continue
        info = media_info(clip.path)
        if info.get("has_audio"):
            any_audio = True
            pieces.append(("audio", clip))
        else:
            pieces.append(("silence", max(0.04, clip.used)))
    if not any_audio:
        raise ValueError(
            "This video has no audio to listen to. "
            "Record with the microphone or system audio turned on."
        )

    inputs: list[str] = []
    input_index: dict[str, int] = {}
    for clip in clips:
        if clip.is_pause or clip.is_image or not clip.path or clip.path in input_index:
            continue
        input_index[clip.path] = len(input_index)
        inputs.extend(["-i", clip.path])

    filters: list[str] = []
    labels: list[str] = []
    for index, (kind, payload) in enumerate(pieces):
        label = f"[a{index}]"
        if kind == "silence":
            duration = max(0.04, float(payload))
            filters.append(
                "anullsrc=channel_layout=mono:sample_rate=16000,"
                f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS{label}"
            )
        else:
            clip = payload
            assert isinstance(clip, Clip)
            slot = input_index[clip.path]
            start = max(0.0, clip.in_point)
            end = max(start + 0.04, clip.out_point)
            filters.append(
                f"[{slot}:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
                f"aresample=16000,aformat=channel_layouts=mono{label}"
            )
        labels.append(label)
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[aout]")
    run(
        [
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[aout]",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(dest),
        ]
    )
    if not dest.is_file() or dest.stat().st_size < 1000:
        raise ValueError("Could not read audio from the timeline.")


def _transcribe_file(path: Path, lang: str, api_key: str, language_name: str) -> list[Cue]:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    with path.open("rb") as handle:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=handle,
            language=lang or "en",
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )
    cues = [
        Cue(video_time=start, text=text)
        for start, text in _segments(result)
    ]
    if not cues:
        raise ValueError(f"No speech was found for {language_name}.")
    return cues


def _segments(result) -> list[tuple[float, str]]:
    segments = getattr(result, "segments", None)
    if segments is None and isinstance(result, dict):
        segments = result.get("segments")
    found: list[tuple[float, str]] = []
    for segment in segments or []:
        if isinstance(segment, dict):
            start = float(segment.get("start") or 0.0)
            text = str(segment.get("text") or "").strip()
        else:
            start = float(getattr(segment, "start", 0.0) or 0.0)
            text = str(getattr(segment, "text", "") or "").strip()
        if text:
            found.append((max(0.0, start), text))
    if found:
        return found
    text = getattr(result, "text", None)
    if text is None and isinstance(result, dict):
        text = result.get("text")
    cleaned = str(text or "").strip()
    if cleaned:
        return [(0.0, cleaned)]
    return []
