from __future__ import annotations

import hashlib
import asyncio
import threading
import time
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.engine.ffmpeg import audio_duration, run
from app.engine.settings import cache_dir

TTS_VOLUME = "+100%"
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
# teacher1 marks the warm delivery and the milder level. Older loud5 files stay unused.
CACHE_TAG = "teacher1"
TEACHER_INSTRUCTIONS = (
    "Speak as a warm, patient teacher recording a software tutorial. "
    "Use a calm, unhurried pace and a friendly, encouraging tone with gentle inflection. "
    "Sound welcoming and conversational. Never stern, rushed, or interrogative."
)
TTS_PREP_FILTER = (
    "silenceremove=start_periods=1:start_threshold=-35dB:start_silence=0.02:detection=peak,"
    "aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,"
    "volume=1.4,alimiter=limit=0.95:attack=10:release=150,asetpts=PTS-STARTPTS"
)
_OPENAI_WORKERS = 3
_VOICE_PREFIXES = ("openai_", "edge-tts_", "elevenlabs_", "voice_")
_VOICE_SUFFIXES = (".src.mp3", ".src.wav", ".part.wav")

# Pause between live Edge requests so a long script does not trip Bing's connection limit.
_REQUEST_GAP = 0.4
_next_request_at = 0.0
_pace_lock = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def cache_path(provider: str, voice: str, text: str, suffix: str) -> Path:
    # Speaker (voice) and exact text are the identity.
    # The OpenAI model is part of the key so a repeat of the same line is reused
    # and an older tts-1 file is not played as gpt-4o-mini-tts.
    if provider == "openai":
        identity = f"{provider}|{OPENAI_TTS_MODEL}|{voice}|{text}|{CACHE_TAG}"
    else:
        identity = f"{provider}|{voice}|{text}|{CACHE_TAG}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
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


def clear_voice_cache() -> int:
    """Delete generated narration in the app cache. Recordings are left in place."""
    folder = cache_dir()
    removed = 0
    try:
        entries = list(folder.iterdir())
    except OSError:
        return 0
    for path in entries:
        if not path.is_file():
            continue
        name = path.name
        if not (name.startswith(_VOICE_PREFIXES) or name.endswith(_VOICE_SUFFIXES)):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def wav_duration(path: str | Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
        if rate > 0 and frames > 0:
            return frames / float(rate)
    except (OSError, wave.Error):
        pass
    return audio_duration(path)


def _pace_network(provider: str) -> None:
    if provider != "edge-tts":
        return
    global _next_request_at
    with _pace_lock:
        now = time.monotonic()
        wait = _next_request_at - now
        if wait > 0:
            time.sleep(wait)
        _next_request_at = time.monotonic() + _REQUEST_GAP


def _path_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


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


def _openai_speech(client, text: str, voice: str):
    kwargs = {
        "model": OPENAI_TTS_MODEL,
        "voice": voice,
        "input": text,
        "response_format": "wav",
        "instructions": TEACHER_INSTRUCTIONS,
    }
    try:
        return client.audio.speech.create(**kwargs)
    except TypeError:
        kwargs.pop("instructions")
        kwargs["extra_body"] = {"instructions": TEACHER_INSTRUCTIONS}
        return client.audio.speech.create(**kwargs)


def synthesize_openai(text: str, voice: str, api_key: str, dest: Path) -> Path:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = _openai_speech(client, text, voice)
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
    with _path_lock(dest):
        if _usable(dest):
            return dest
        raw_name = ".src.wav" if provider == "openai" else ".src.mp3"
        raw = dest.with_name(dest.stem + raw_name)
        temp_wav = dest.with_name(dest.stem + ".part.wav")
        attempts = 4
        for attempt in range(attempts):
            try:
                raw.unlink(missing_ok=True)
                _pace_network(provider)
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
    return path, wav_duration(path)


def synthesize_lines(
    texts: list[str],
    *,
    provider: str,
    voice: str,
    openai_key: str = "",
    elevenlabs_key: str = "",
    on_progress: Callable[[int, int], None] | None = None,
) -> list[tuple[Path, float]]:
    """Speak every line. OpenAI runs a few at a time. Edge stays one at a time."""
    total = len(texts)
    results: list[tuple[Path, float] | None] = [None] * total
    if total == 0:
        return []

    def one(index: int, text: str) -> tuple[int, Path, float]:
        path, duration = synthesize_with_duration(
            text,
            provider=provider,
            voice=voice,
            openai_key=openai_key,
            elevenlabs_key=elevenlabs_key,
        )
        return index, path, duration

    done = 0
    lock = threading.Lock()

    def finish(index: int, path: Path, duration: float) -> None:
        nonlocal done
        with lock:
            results[index] = (path, duration)
            done += 1
            current = done
            if on_progress:
                on_progress(current, total)

    if provider == "openai" and total > 1:
        workers = min(_OPENAI_WORKERS, total)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, index, text) for index, text in enumerate(texts)]
            for future in as_completed(futures):
                index, path, duration = future.result()
                finish(index, path, duration)
    else:
        for index, text in enumerate(texts):
            _, path, duration = one(index, text)
            finish(index, path, duration)

    if any(item is None for item in results):
        raise RuntimeError("Voice generation did not finish every line.")
    return [item for item in results if item is not None]
