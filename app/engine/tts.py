from __future__ import annotations

import hashlib
import asyncio
from pathlib import Path

from app.engine.ffmpeg import audio_duration, run
from app.engine.settings import cache_dir

TTS_VOLUME = "+50%"
TTS_PREP_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-35dB:start_silence=0.02:detection=peak,"
    "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,"
    "volume=2.3,alimiter=limit=0.96,asetpts=PTS-STARTPTS"
)


def cache_path(provider: str, voice: str, text: str, suffix: str) -> Path:
    digest = hashlib.sha256(f"{provider}|{voice}|{text}|wav1".encode("utf-8")).hexdigest()[:20]
    return cache_dir() / f"{provider}_{digest}{suffix}"


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def synthesize_edge(text: str, voice: str, dest: Path) -> Path:
    import edge_tts

    async def _save() -> None:
        communicate = edge_tts.Communicate(text, voice, volume=TTS_VOLUME)
        await communicate.save(str(dest))

    _run_async(_save())
    return dest


def synthesize_openai(text: str, voice: str, api_key: str, dest: Path) -> Path:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=text,
    )
    if hasattr(response, "stream_to_file"):
        response.stream_to_file(str(dest))
    else:
        dest.write_bytes(response.read())
    return dest


def synthesize_elevenlabs(text: str, voice: str, api_key: str, dest: Path) -> Path:
    import httpx

    response = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        headers={
            "xi-api-key": api_key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        },
        json={"text": text, "model_id": "eleven_multilingual_v2"},
        timeout=120.0,
    )
    response.raise_for_status()
    dest.write_bytes(response.content)
    return dest


def _to_wav(src: Path, dest: Path) -> None:
    run(
        [
            "-i",
            str(src),
            "-af",
            TTS_PREP_FILTER,
            "-c:a",
            "pcm_s16le",
            "-ar",
            "44100",
            "-ac",
            "2",
            str(dest),
        ]
    )


def synthesize(
    text: str,
    *,
    provider: str,
    voice: str,
    openai_key: str = "",
    elevenlabs_key: str = "",
) -> Path:
    text = text.strip()
    if not text:
        raise ValueError("Cannot speak empty text.")
    dest = cache_path(provider, voice, text, ".wav")
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_suffix(".src.mp3")
    if provider == "openai":
        if not openai_key:
            raise ValueError("Add an OpenAI API key in Settings to use this voice.")
        synthesize_openai(text, voice, openai_key, raw)
    elif provider == "elevenlabs":
        if not elevenlabs_key:
            raise ValueError("Add an ElevenLabs API key in Settings to use this voice.")
        synthesize_elevenlabs(text, voice, elevenlabs_key, raw)
    else:
        synthesize_edge(text, voice, raw)
    temp_wav = dest.with_suffix(".part.wav")
    try:
        _to_wav(raw, temp_wav)
        temp_wav.replace(dest)
    finally:
        if raw.exists():
            raw.unlink(missing_ok=True)
        if temp_wav.exists() and temp_wav != dest:
            temp_wav.unlink(missing_ok=True)
    return dest


def synthesize_with_duration(
    text: str,
    *,
    provider: str,
    voice: str,
    openai_key: str = "",
    elevenlabs_key: str = "",
) -> tuple[Path, float]:
    path = synthesize(
        text,
        provider=provider,
        voice=voice,
        openai_key=openai_key,
        elevenlabs_key=elevenlabs_key,
    )
    return path, audio_duration(path)
