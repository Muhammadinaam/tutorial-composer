from __future__ import annotations

import re

from app.engine.project import Cue

TIME_LINE = re.compile(
    r"^\s*(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\s*[,|\-–—]\s*(.+?)\s*$"
)
SECONDS_LINE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[,|\-–—]\s*(.+?)\s*$")


def parse_timestamp(value: str) -> float:
    value = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    parts = value.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise ValueError(f"Could not parse timestamp: {value}")


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:06.3f}".rstrip("0").rstrip(".")
    return f"{minutes}:{secs:06.3f}".rstrip("0").rstrip(".")


def parse_script(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = TIME_LINE.match(line)
        if match:
            hours, minutes, seconds, body = match.groups()
            total = int(minutes) * 60 + float(seconds)
            if hours:
                total += int(hours) * 3600
            if body:
                cues.append(Cue(video_time=total, text=body))
            continue
        match = SECONDS_LINE.match(line)
        if match:
            cues.append(Cue(video_time=float(match.group(1)), text=match.group(2)))
            continue
        raise ValueError(
            f'Could not parse line: "{line}". Use "1:12, Your narration text".'
        )
    cues.sort(key=lambda c: c.video_time)
    return cues


def script_from_cues(cues: list[Cue]) -> str:
    lines = []
    for cue in cues:
        lines.append(f"{format_timestamp(cue.video_time)}, {cue.text}")
    return "\n".join(lines)


def estimate_speech_seconds(text: str) -> float:
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    if not words:
        return 0.0
    return max(0.8, len(words) / 2.4)
