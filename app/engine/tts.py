from __future__ import annotations

import hashlib
import asyncio
import time
from pathlib import Path

from app.engine.ffmpeg import audio_duration, run
from app.engine.settings import cache_dir

TTS_VOLUME = "+100%"
TTS_PREP_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-35dB:start_silence=0.02:detection=peak,"
    "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,"
    "volume=4.5,alimiter=limit=0.99,asetpts=PTS-STARTPTS"
)


# Pause between live requests so a long script does not trip Bing's connection limit.
_REQUEST_GAP = 0.4
_next_request_at = 0.0


def cache_path(provider: str, voice: str, text: str, suffix: str) -> Path:
    # Speaker (voice) and text are the identity. loud5 marks the post-process chain.
    digest = hashlib.sha256(f"{provider}|{voice}|{text}|loud5".encode("utf-8")).hexdigest()[:20]
    return cache_dir() / f"{provider}_{digest}{suffix}"


def _usable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 1000
    except OSError:
        return False


def is_cached(text: str, *, provider: str, voice: str) -> bool:
    text = text.strip()
    if not text:
        return False
    return _usable(cache_path(provider, voice, text, ".wav"))


def _pace_network() -> None:
    global _next_request_at
    now = time.monotonic()
    if now < _next_request_at:
        time.sleep(_next_request_at - now)
    _next_request_at = time.monotonic() + _REQUEST_GAP


def _transient_voice_error(exc: BaseException) -> bool:
    if isinstance(exc, (ValueError, FileNotFoundError)):
        return False
    text = f"{type(exc).__name__} {exc}".lower()
    markers = (
        "443",
        "429",
        "403",
        "timeout",
        "timed out",
        "connection",
        "websocket",
        "no audio was received",
        "ssl",
        "reset",
        "network",
        "temporarily",
        "unavailable",
        "semaphore",
        "bing",
    )
    return any(marker in text for marker in markers)


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


def _write_source(
    text: str,
    dest: Path,
    *,
    provider: str,
    voice: str,
    openai_key: str,
    elevenlabs_key: str,
) -> None:
    if provider == "openai":
        if not openai_key:
            raise ValueError("Add an OpenAI API key in Settings to use this voice.")
        synthesize_openai(text, voice, openai_key, dest)
    elif provider == "elevenlabs":
        if not elevenlabs_key:
            raise ValueError("Add an ElevenLabs API key in Settings to use this voice.")
        synthesize_elevenlabs(text, voice, elevenlabs_key, dest)
    else:
        synthesize_edge(text, voice, dest)


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
    if _usable(dest):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest.with_suffix(".src.mp3")
    temp_wav = dest.with_suffix(".part.wav")
    attempts = 4
    for attempt in range(attempts):
        try:
            raw.unlink(missing_ok=True)
            _pace_network()
            _write_source(
                text,
                raw,
                provider=provider,
                voice=voice,
                openai_key=openai_key,
                elevenlabs_key=elevenlabs_key,
            )
            break
        except Exception as exc:
            raw.unlink(missing_ok=True)
            if not _transient_voice_error(exc) or attempt + 1 == attempts:
                if _transient_voice_error(exc):
                    raise RuntimeError(
                        f"{exc} — the voice service rejected the connection. "
                        "Lines that already generated are saved and will be reused "
                        "when you generate again with the same speaker and text."
                    ) from exc
                raise
            time.sleep(1.2 * (2**attempt))
    try:
        _to_wav(raw, temp_wav)
        temp_wav.replace(dest)
    finally:
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
