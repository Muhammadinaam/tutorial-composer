from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.engine.ffmpeg import media_info, probe, run
from app.engine.project import Clip, Cue
from app.engine.settings import cache_dir
from app.engine.translate import LANGUAGE_NAMES


# One request only covers a short slice. Longer audio is sent in pieces so the
# later part of the video is not dropped.
CHUNK_SECONDS = 25.0


def transcribe_timeline(clips: list[Clip], lang: str, api_key: str) -> list[Cue]:
    if not api_key:
        raise ValueError("Add an OpenAI API key in Settings to draft the script from audio.")
    if not clips:
        raise ValueError("Add a video first.")
    language = LANGUAGE_NAMES.get(lang, lang)
    dest = cache_dir() / f"speech_{uuid.uuid4().hex[:12]}.mp3"
    pieces: list[Path] = []
    try:
        _extract_timeline_audio(clips, dest)
        duration = float(media_info(dest).get("duration") or 0.0)
        if duration <= 0:
            raise ValueError("Could not read audio from the timeline.")
        cues: list[Cue] = []
        offset = 0.0
        index = 0
        while offset < duration - 0.05:
            length = min(CHUNK_SECONDS, duration - offset)
            if duration <= CHUNK_SECONDS + 0.05:
                part_path = dest
            else:
                part_path = dest.with_name(f"{dest.stem}_{index}{dest.suffix}")
                pieces.append(part_path)
                _cut_audio(dest, part_path, offset, length)
            for cue in _transcribe_file(part_path, lang, api_key, length):
                cue.video_time += offset
                cues.append(cue)
            offset += length
            index += 1
            if part_path is dest:
                break
        if not cues:
            raise ValueError(f"No speech was found for {language}.")
        return cues
    finally:
        dest.unlink(missing_ok=True)
        for piece in pieces:
            piece.unlink(missing_ok=True)


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
            # A clip that already runs to the end of the file should include the
            # whole audio track, even when the stored duration stops early.
            if clip.out_point >= float(clip.duration) - 0.08:
                end = max(end, _audio_end(clip.path))
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


def _audio_end(path: str) -> float:
    try:
        data = probe(path)
    except Exception:
        return 0.0
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not audio:
        return 0.0
    start = float(audio.get("start_time") or 0.0)
    if audio.get("duration"):
        return max(0.0, start + float(audio["duration"]))
    return float((data.get("format") or {}).get("duration") or 0.0)


def _cut_audio(src: Path, dest: Path, start: float, length: float) -> None:
    run(
        [
            "-i",
            str(src),
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{max(0.04, length):.3f}",
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


_SENTENCE_BREAK = re.compile(r"(?<=[.!?۔])\s+|\n+")


def _transcribe_file(path: Path, lang: str, api_key: str, duration: float) -> list[Cue]:
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
    return [
        Cue(video_time=start, text=text)
        for start, text in _voice_lines(result, duration)
    ]


def _voice_lines(result, duration: float) -> list[tuple[float, str]]:
    spans = _segments(result, duration)
    lines: list[tuple[float, str]] = []
    for start, end, text in spans:
        lines.extend(_split_sentences(start, end, text))
    return lines


def _segments(result, duration: float) -> list[tuple[float, float, str]]:
    segments = getattr(result, "segments", None)
    if segments is None and isinstance(result, dict):
        segments = result.get("segments")
    found: list[tuple[float, float, str]] = []
    for segment in segments or []:
        if isinstance(segment, dict):
            start = float(segment.get("start") or 0.0)
            end = float(segment.get("end") or start)
            text = str(segment.get("text") or "").strip()
        else:
            start = float(getattr(segment, "start", 0.0) or 0.0)
            end = float(getattr(segment, "end", start) or start)
            text = str(getattr(segment, "text", "") or "").strip()
        if text:
            found.append((max(0.0, start), max(start, end), text))
    if found:
        return found
    text = getattr(result, "text", None)
    if text is None and isinstance(result, dict):
        text = result.get("text")
    cleaned = str(text or "").strip()
    if cleaned:
        return [(0.0, max(0.0, float(duration)), cleaned)]
    return []


def _split_sentences(start: float, end: float, text: str) -> list[tuple[float, str]]:
    parts = [part.strip() for part in _SENTENCE_BREAK.split(text) if part.strip()]
    if len(parts) <= 1:
        return [(start, text.strip())]
    span = max(0.0, end - start)
    weights = [max(1, len(part)) for part in parts]
    total = float(sum(weights))
    cursor = start
    lines: list[tuple[float, str]] = []
    for part, weight in zip(parts, weights):
        lines.append((cursor, part))
        cursor += span * weight / total
    return lines
