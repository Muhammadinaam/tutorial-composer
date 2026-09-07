from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "tutorial-composer"

DEFAULTS = {
    "tts_provider": "edge-tts",
    "openai_api_key": "",
    "elevenlabs_api_key": "",
    "voice": "en-US-JennyNeural",
    "lang": "en",
}


def settings_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".config" / APP_NAME


def settings_path() -> Path:
    return settings_dir() / "settings.json"


def cache_dir() -> Path:
    path = settings_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_settings() -> dict:
    path = settings_path()
    data = dict(DEFAULTS)
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                data.update({k: stored.get(k, data[k]) for k in DEFAULTS})
        except (OSError, json.JSONDecodeError):
            pass
    return data


def save_settings(data: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULTS)
    merged.update({k: data.get(k, merged[k]) for k in DEFAULTS})
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
