from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _looks_image(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


@dataclass
class Clip:
    path: str
    in_point: float = 0.0
    out_point: float = 0.0
    duration: float = 0.0
    id: str = field(default_factory=new_id)
    kind: str = "video"

    @property
    def used(self) -> float:
        return max(0.0, float(self.out_point) - float(self.in_point))

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def is_image(self) -> bool:
        return self.kind == "image"


@dataclass
class Cue:
    video_time: float
    text: str
    should_video_stop: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> Cue:
        return cls(
            video_time=float(data.get("video_time", 0.0)),
            text=str(data.get("text", "")),
            should_video_stop=bool(data.get("should_video_stop", False)),
        )


def clamp_speed(value: float) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        rate = 1.0
    if rate != rate:  # NaN
        rate = 1.0
    return max(0.25, min(2.0, rate))


@dataclass
class BlurRegion:
    start: float
    end: float
    x: float
    y: float
    w: float
    h: float
    id: str = field(default_factory=new_id)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> BlurRegion:
        region = cls(
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            x=float(data.get("x", 0.0)),
            y=float(data.get("y", 0.0)),
            w=float(data.get("w", 0.2)),
            h=float(data.get("h", 0.2)),
            id=str(data.get("id") or new_id()),
        )
        region.clamp()
        return region

    def clamp(self) -> None:
        self.w = max(0.02, min(1.0, float(self.w)))
        self.h = max(0.02, min(1.0, float(self.h)))
        self.x = max(0.0, min(1.0 - self.w, float(self.x)))
        self.y = max(0.0, min(1.0 - self.h, float(self.y)))
        self.start = max(0.0, float(self.start))
        self.end = max(self.start + 0.08, float(self.end))


@dataclass
class Narration:
    lang: str = "en"
    voice: str = "en-US-JennyNeural"
    cues: list[Cue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lang": self.lang,
            "voice": self.voice,
            "cues": [asdict(c) for c in self.cues],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Narration:
        cues = [Cue.from_dict(c) if isinstance(c, dict) else c for c in data.get("cues", [])]
        return cls(
            lang=data.get("lang", "en"),
            voice=data.get("voice", "en-US-JennyNeural"),
            cues=cues,
        )


@dataclass
class Project:
    clips: list[Clip] = field(default_factory=list)
    mute_original: bool = True
    script_text: str = ""
    cues: list[Cue] = field(default_factory=list)
    voice: str = "en-US-JennyNeural"
    lang: str = "en"
    narrations: dict[str, Narration] = field(default_factory=dict)
    music_path: str | None = None
    music_volume: float = 0.2
    voice_volume: float = 1.0
    music_duration: float = 0.0
    mark_in: float | None = None
    mark_out: float | None = None
    blurs: list[BlurRegion] = field(default_factory=list)
    speed: float = 1.0
    path: str | None = None

    def __post_init__(self) -> None:
        if not self.narrations:
            self.narrations[self.lang] = Narration(
                lang=self.lang, voice=self.voice, cues=list(self.cues)
            )
        elif self.lang not in self.narrations:
            self.narrations[self.lang] = Narration(lang=self.lang, voice=self.voice)

    def current(self) -> Narration:
        if self.lang not in self.narrations:
            self.narrations[self.lang] = Narration(lang=self.lang, voice=self.voice)
        return self.narrations[self.lang]

    def sync_from_current(self) -> None:
        current = self.current()
        self.voice = current.voice
        self.cues = list(current.cues)
        self.script_text = ""

    def to_dict(self) -> dict:
        self.sync_from_current()
        return {
            "clips": [asdict(c) for c in self.clips],
            "mute_original": self.mute_original,
            "script_text": self.script_text,
            "cues": [asdict(c) for c in self.cues],
            "voice": self.voice,
            "lang": self.lang,
            "narrations": {code: item.to_dict() for code, item in self.narrations.items()},
            "music_path": self.music_path,
            "music_volume": self.music_volume,
            "voice_volume": self.voice_volume,
            "music_duration": self.music_duration,
            "mark_in": self.mark_in,
            "mark_out": self.mark_out,
            "blurs": [blur.to_dict() for blur in self.blurs],
            "speed": clamp_speed(self.speed),
        }

    @classmethod
    def from_dict(cls, data: dict, path: str | None = None) -> Project:
        clips = []
        for raw in data.get("clips", []):
            item = dict(raw)
            item.setdefault("kind", "image" if _looks_image(item.get("path", "")) else "video")
            clips.append(Clip(**item))
        cues = [Cue.from_dict(c) if isinstance(c, dict) else c for c in data.get("cues", [])]
        narrations = {}
        raw_nars = data.get("narrations") or {}
        for code, payload in raw_nars.items():
            narrations[code] = Narration.from_dict(payload)
        lang = data.get("lang", "en")
        voice = data.get("voice", "en-US-JennyNeural")
        if not cues and data.get("script_text"):
            from app.engine.script import parse_script

            try:
                cues = parse_script(data["script_text"])
            except ValueError:
                cues = []
        if not narrations:
            narrations[lang] = Narration(lang=lang, voice=voice, cues=cues)
        elif lang in narrations and not narrations[lang].cues and cues:
            narrations[lang].cues = list(cues)
        blurs = []
        for raw in data.get("blurs") or []:
            if isinstance(raw, dict):
                blurs.append(BlurRegion.from_dict(raw))
        return cls(
            clips=clips,
            mute_original=bool(data.get("mute_original", True)),
            script_text=data.get("script_text", ""),
            cues=cues or list(narrations.get(lang, Narration()).cues),
            voice=voice,
            lang=lang,
            narrations=narrations,
            music_path=data.get("music_path") or None,
            music_volume=float(data.get("music_volume", 0.2)),
            voice_volume=float(data.get("voice_volume", 1.0)),
            music_duration=float(data.get("music_duration", 0.0)),
            mark_in=data.get("mark_in"),
            mark_out=data.get("mark_out"),
            blurs=blurs,
            speed=clamp_speed(data.get("speed", 1.0)),
            path=path,
        )


def load_project(path: str | Path) -> Project:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Project.from_dict(raw, path=str(path))


def save_project(project: Project, path: str | Path | None = None) -> Path:
    dest = Path(path or project.path or "")
    if not dest:
        raise ValueError("No path given to save the project.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(project.to_dict(), indent=2), encoding="utf-8")
    project.path = str(dest)
    return dest
