from __future__ import annotations

from dataclasses import dataclass

FALLBACK_EDGE_VOICES = [
    ("en-US-JennyNeural", "Jenny", "en", "Female", "English (US)"),
    ("en-US-GuyNeural", "Guy", "en", "Male", "English (US)"),
    ("en-US-AriaNeural", "Aria", "en", "Female", "English (US)"),
    ("en-US-DavisNeural", "Davis", "en", "Male", "English (US)"),
    ("en-GB-SoniaNeural", "Sonia", "en", "Female", "English (UK)"),
    ("en-GB-RyanNeural", "Ryan", "en", "Male", "English (UK)"),
    ("en-AU-NatashaNeural", "Natasha", "en", "Female", "English (Australia)"),
    ("en-IN-NeerjaNeural", "Neerja", "en", "Female", "English (India)"),
    ("es-ES-ElviraNeural", "Elvira", "es", "Female", "Spanish (Spain)"),
    ("es-MX-DaliaNeural", "Dalia", "es", "Female", "Spanish (Mexico)"),
    ("fr-FR-DeniseNeural", "Denise", "fr", "Female", "French"),
    ("de-DE-KatjaNeural", "Katja", "de", "Female", "German"),
    ("hi-IN-SwaraNeural", "Swara", "hi", "Female", "Hindi"),
    ("hi-IN-MadhurNeural", "Madhur", "hi", "Male", "Hindi"),
    ("ur-PK-UzmaNeural", "Uzma", "ur", "Female", "Urdu"),
    ("ur-PK-AsadNeural", "Asad", "ur", "Male", "Urdu"),
    ("ar-SA-ZariyahNeural", "Zariyah", "ar", "Female", "Arabic"),
    ("pt-BR-FranciscaNeural", "Francisca", "pt", "Female", "Portuguese (Brazil)"),
    ("ja-JP-NanamiNeural", "Nanami", "ja", "Female", "Japanese"),
    ("zh-CN-XiaoxiaoNeural", "Xiaoxiao", "zh", "Female", "Chinese"),
]

OPENAI_VOICES = [
    ("alloy", "Alloy", "en", "Neutral", "OpenAI"),
    ("ash", "Ash", "en", "Male", "OpenAI"),
    ("coral", "Coral", "en", "Female", "OpenAI"),
    ("echo", "Echo", "en", "Male", "OpenAI"),
    ("fable", "Fable", "en", "Neutral", "OpenAI"),
    ("nova", "Nova", "en", "Female", "OpenAI"),
    ("onyx", "Onyx", "en", "Male", "OpenAI"),
    ("sage", "Sage", "en", "Neutral", "OpenAI"),
    ("shimmer", "Shimmer", "en", "Female", "OpenAI"),
]


@dataclass
class Voice:
    id: str
    name: str
    lang: str
    gender: str
    locale_label: str
    provider: str

    @property
    def label(self) -> str:
        gender = f" · {self.gender}" if self.gender else ""
        return f"{self.name}{gender} · {self.locale_label}"


def _edge_locale_label(locale: str) -> str:
    names = {
        "en-US": "English (US)",
        "en-GB": "English (UK)",
        "en-AU": "English (Australia)",
        "en-IN": "English (India)",
        "es-ES": "Spanish (Spain)",
        "es-MX": "Spanish (Mexico)",
        "fr-FR": "French",
        "de-DE": "German",
        "hi-IN": "Hindi",
        "ur-PK": "Urdu",
        "ar-SA": "Arabic",
        "pt-BR": "Portuguese (Brazil)",
        "ja-JP": "Japanese",
        "zh-CN": "Chinese",
    }
    return names.get(locale, locale)


def _lang_from_locale(locale: str) -> str:
    return (locale or "en").split("-")[0].lower()


def fallback_edge_voices() -> list[Voice]:
    return [
        Voice(vid, name, lang, gender, label, "edge-tts")
        for vid, name, lang, gender, label in FALLBACK_EDGE_VOICES
    ]


def list_edge_voices() -> list[Voice]:
    try:
        import asyncio

        import edge_tts

        async def _load() -> list:
            return await edge_tts.list_voices()

        try:
            raw = asyncio.run(_load())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                raw = loop.run_until_complete(_load())
            finally:
                loop.close()
        voices = []
        for item in raw:
            locale = item.get("Locale", "")
            short_name = item.get("ShortName", "")
            if not short_name:
                continue
            friendly = item.get("FriendlyName") or short_name.split("-")[-1].replace(
                "Neural", ""
            )
            voices.append(
                Voice(
                    id=short_name,
                    name=friendly.replace("Microsoft ", "").split(" - ")[0].strip(),
                    lang=_lang_from_locale(locale),
                    gender=item.get("Gender", ""),
                    locale_label=_edge_locale_label(locale),
                    provider="edge-tts",
                )
            )
        voices.sort(key=lambda v: (v.locale_label, v.name))
        return voices or fallback_edge_voices()
    except Exception:
        return fallback_edge_voices()


def list_openai_voices() -> list[Voice]:
    return [
        Voice(vid, name, lang, gender, label, "openai")
        for vid, name, lang, gender, label in OPENAI_VOICES
    ]


def list_elevenlabs_voices(api_key: str) -> list[Voice]:
    if not api_key:
        return []
    import httpx

    response = httpx.get(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": api_key},
        timeout=30.0,
    )
    response.raise_for_status()
    voices = []
    for item in response.json().get("voices", []):
        labels = item.get("labels") or {}
        voices.append(
            Voice(
                id=item.get("voice_id", ""),
                name=item.get("name", "Voice"),
                lang=(labels.get("language") or "en")[:2].lower(),
                gender=(labels.get("gender") or "").title(),
                locale_label="ElevenLabs",
                provider="elevenlabs",
            )
        )
    return [v for v in voices if v.id]


def list_voices(provider: str, api_key: str = "") -> list[Voice]:
    if provider == "openai":
        return list_openai_voices()
    if provider == "elevenlabs":
        return list_elevenlabs_voices(api_key)
    return list_edge_voices()


def languages_from_voices(voices: list[Voice]) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for voice in voices:
        seen.setdefault(voice.lang, voice.locale_label.split("(")[0].strip())
    extras = {
        "en": "English",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "hi": "Hindi",
        "ur": "Urdu",
        "ar": "Arabic",
        "pt": "Portuguese",
        "ja": "Japanese",
        "zh": "Chinese",
    }
    for code, name in extras.items():
        seen.setdefault(code, name)
    return sorted(seen.items(), key=lambda item: item[1])


def matching_voice(voices: list[Voice], lang: str, current_id: str | None = None) -> Voice | None:
    if current_id:
        for voice in voices:
            if voice.id == current_id:
                return voice
    for voice in voices:
        if voice.lang == lang:
            return voice
    return voices[0] if voices else None
