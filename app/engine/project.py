from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
PROJECT_FILENAME = "project.json"
MEDIA_DIRNAME = "media"


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

    @property
    def is_pause(self) -> bool:
        return self.kind == "pause"


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


@dataclass
class Pause:
    at: float
    duration: float = 1.5
    id: str = field(default_factory=new_id)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Pause:
        pause = cls(
            at=float(data.get("at", 0.0)),
            duration=float(data.get("duration", 1.5)),
            id=str(data.get("id") or new_id()),
        )
        pause.clamp()
        return pause

    def clamp(self) -> None:
        self.at = max(0.0, float(self.at))
        self.duration = max(0.2, min(30.0, float(self.duration)))


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
    kind: str = "blur"

    def is_highlight(self) -> bool:
        return self.kind == "highlight"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> BlurRegion:
        kind = str(data.get("kind") or "blur")
        if kind not in {"blur", "highlight"}:
            kind = "blur"
        region = cls(
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            x=float(data.get("x", 0.0)),
            y=float(data.get("y", 0.0)),
            w=float(data.get("w", 0.2)),
            h=float(data.get("h", 0.2)),
            id=str(data.get("id") or new_id()),
            kind=kind,
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
    pauses: list[Pause] = field(default_factory=list)
    burn_subtitles: bool = True
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
            "pauses": [pause.to_dict() for pause in self.pauses],
            "burn_subtitles": self.burn_subtitles,
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
        legacy_pauses = []
        for raw in data.get("pauses") or []:
            if isinstance(raw, dict):
                legacy_pauses.append(Pause.from_dict(raw))
        if legacy_pauses and clips:
            from app.engine.edl import insert_pause

            for old in sorted(legacy_pauses, key=lambda item: -float(item.at)):
                clips = insert_pause(clips, old.at, old.duration)
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
            pauses=[],
            burn_subtitles=bool(data.get("burn_subtitles", True)),
            speed=clamp_speed(data.get("speed", 1.0)),
            path=path,
        )


def media_dir_for(project_json: str | Path) -> Path:
    return Path(project_json).parent / MEDIA_DIRNAME


def is_project_bundle(path: str | Path | None) -> bool:
    if not path:
        return False
    return Path(path).name.lower() == PROJECT_FILENAME


def bundle_json_path(dest: str | Path) -> Path:
    """Return the project.json path for a folder, a bundle file, or a legacy json file."""
    dest = Path(dest)
    if dest.exists() and dest.is_dir():
        return dest / PROJECT_FILENAME
    if dest.suffix.lower() == ".json":
        if dest.name.lower() == PROJECT_FILENAME:
            return dest
        return dest.parent / dest.stem / PROJECT_FILENAME
    return dest / PROJECT_FILENAME


def _path_key(path: Path) -> str:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    return os.path.normcase(os.path.normpath(str(resolved)))


def _is_under(path: Path, parent: Path) -> bool:
    child = _path_key(path)
    base = _path_key(parent)
    if child == base:
        return True
    prefix = base if base.endswith(os.sep) else base + os.sep
    return child.startswith(prefix)


def _same_file(left: Path, right: Path) -> bool:
    return _path_key(left) == _path_key(right)


def _dest_for(media: Path, source: Path) -> Path:
    def usable(candidate: Path) -> bool:
        return (not candidate.exists()) or _same_file(candidate, source)

    candidate = media / source.name
    if usable(candidate):
        return candidate
    stem = source.stem
    suffix = source.suffix
    number = 2
    while True:
        candidate = media / f"{stem}_{number}{suffix}"
        if usable(candidate):
            return candidate
        number += 1


def _store_media(
    stored: str,
    media: Path,
    placed: dict[str, Path],
    missing: list[str],
    created: list[Path],
) -> str:
    source = Path(stored)
    if not stored or not source.is_file():
        missing.append(source.name or stored or "unknown")
        return stored
    key = _path_key(source)
    existing = placed.get(key)
    if existing is not None:
        return str(existing)
    if _is_under(source, media):
        resolved = source.resolve()
        placed[key] = resolved
        return str(resolved)
    dest = _dest_for(media, source)
    if not dest.exists():
        shutil.copy2(source, dest)
        created.append(dest)
    resolved = dest.resolve()
    placed[key] = resolved
    return str(resolved)


def collect_media(project: Project, root: str | Path) -> list[str]:
    """Copy clips and the soundtrack into ``root/media`` and point the project at those copies.

    Files already in that media folder stay where they are. Missing files are left unchanged.
    Returns the display names of missing files.
    """
    media = Path(root) / MEDIA_DIRNAME
    media.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    placed: dict[str, Path] = {}
    created: list[Path] = []
    clip_paths = [clip.path for clip in project.clips]
    music_path = project.music_path
    try:
        for clip in project.clips:
            if clip.is_pause:
                clip.path = ""
                continue
            clip.path = _store_media(clip.path, media, placed, missing, created)
        if project.music_path:
            project.music_path = _store_media(project.music_path, media, placed, missing, created)
    except Exception:
        for clip, old in zip(project.clips, clip_paths):
            clip.path = old
        project.music_path = music_path
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    return missing


def _resolve_stored_path(stored: str, root: Path) -> str:
    path = Path(stored)
    if path.is_absolute():
        return str(path)
    return str((root / path).resolve())


def _relative_media_path(stored: str, root: Path) -> str:
    path = Path(stored)
    if not path.is_absolute():
        return path.as_posix()
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return str(path)
    if not _is_under(resolved, base):
        return str(path)
    return Path(os.path.relpath(resolved, base)).as_posix()


def _purge_cached_recording(original: str | None, current: str | None) -> None:
    if not original or not current:
        return
    source = Path(original)
    if _same_file(source, Path(current)):
        return
    from app.engine.settings import recordings_dir

    try:
        cache = recordings_dir().resolve()
    except OSError:
        return
    if not source.is_file() or not _is_under(source, cache):
        return
    try:
        source.unlink()
    except OSError:
        pass


def load_project(path: str | Path) -> Project:
    json_path = Path(path)
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    project = Project.from_dict(raw, path=str(json_path))
    root = json_path.parent
    for clip in project.clips:
        if clip.path:
            clip.path = _resolve_stored_path(clip.path, root)
    if project.music_path:
        project.music_path = _resolve_stored_path(project.music_path, root)
    return project


def save_project(project: Project, path: str | Path | None = None) -> tuple[Path, list[str]]:
    """Write a project folder and return ``(project.json, missing file names)``.

    ``path`` may be a project folder, ``project.json``, or a legacy standalone json file.
    A legacy file is saved into a sibling folder named after that file. The old file is left in place.
    In-memory clip and music paths stay absolute. The json stores paths relative to the folder.
    Recordings copied out of the app cache are deleted after the json is written.
    """
    chosen = path if path is not None else project.path
    if not chosen:
        raise ValueError("No path given to save the project.")
    dest = Path(chosen)
    json_path = bundle_json_path(dest)
    root = json_path.parent
    root.mkdir(parents=True, exist_ok=True)
    originals = [clip.path for clip in project.clips]
    music_original = project.music_path
    missing = collect_media(project, root)
    payload = project.to_dict()
    for raw, clip in zip(payload["clips"], project.clips):
        raw["path"] = "" if clip.is_pause else _relative_media_path(clip.path, root)
    if payload.get("music_path"):
        payload["music_path"] = _relative_media_path(str(payload["music_path"]), root)
    try:
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        for clip, old in zip(project.clips, originals):
            clip.path = old
        project.music_path = music_original
        raise
    project.path = str(json_path.resolve())
    for original, clip in zip(originals, project.clips):
        _purge_cached_recording(original, clip.path)
    _purge_cached_recording(music_original, project.music_path)
    return json_path.resolve(), missing
