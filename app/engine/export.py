from __future__ import annotations

import shutil
from pathlib import Path

from app.engine.edl import joined_duration, target_video_size
from app.engine.ffmpeg import media_info, run
from app.engine.project import BlurRegion, Clip, Project, clamp_speed
from app.engine.timeline import Hold, TimelinePlan


YOUTUBE_ARGS = [
    "-c:v",
    "libx264",
    "-preset",
    "medium",
    "-crf",
    "18",
    "-pix_fmt",
    "yuv420p",
    "-c:a",
    "aac",
    "-b:a",
    "192k",
    "-movflags",
    "+faststart",
]


def _even(value: int) -> int:
    return value if value % 2 == 0 else value + 1


def concat_clips(clips: list[Clip], dest: Path, mute: bool = True, on_progress=None) -> Path:
    if not clips:
        raise ValueError("Add at least one video clip before exporting.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    width, height, fps = target_video_size(clips)
    width, height = _even(width), _even(height)
    fps = fps if fps > 1 else 30.0

    inputs: list[str] = []
    filters: list[str] = []
    concat_v: list[str] = []
    concat_a: list[str] = []

    for index, clip in enumerate(clips):
        inputs.extend(["-i", clip.path])
        start = max(0.0, clip.in_point)
        end = max(start + 0.04, clip.out_point)
        duration = max(0.04, end - start)
        scale = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps:.3f},format=yuv420p"
        )
        if clip.is_image:
            filters.append(
                f"[{index}:v]loop=-1:size=1,trim=duration={duration:.3f},"
                f"setpts=PTS-STARTPTS,{scale}[v{index}]"
            )
        else:
            filters.append(
                f"[{index}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
                f"{scale}[v{index}]"
            )
        concat_v.append(f"[v{index}]")
        if not mute:
            info = media_info(clip.path)
            if info["has_audio"] and not clip.is_image:
                filters.append(
                    f"[{index}:a]atrim=start={start:.3f}:end={end:.3f},"
                    f"asetpts=PTS-STARTPTS,aresample=44100,aformat=channel_layouts=stereo[a{index}]"
                )
            else:
                filters.append(
                    f"aevalsrc=0|0:s=44100:d={duration:.3f},aformat=channel_layouts=stereo[a{index}]"
                )
            concat_a.append(f"[a{index}]")

    n = len(clips)
    duration = joined_duration(clips)
    if mute:
        filters.append(f"{''.join(concat_v)}concat=n={n}:v=1:a=0[vout]")
        filter_graph = ";".join(filters)
        run(
            [
                *inputs,
                "-filter_complex",
                filter_graph,
                "-map",
                "[vout]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                str(dest),
                "-y",
            ],
            on_progress=on_progress,
            duration=duration,
        )
    else:
        filters.append(f"{''.join(concat_v)}{''.join(concat_a)}concat=n={n}:v=1:a=1[vout][aout]")
        filter_graph = ";".join(filters)
        run(
            [
                *inputs,
                "-filter_complex",
                filter_graph,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(dest),
                "-y",
            ],
            on_progress=on_progress,
            duration=duration,
        )
    return dest


def apply_holds(source: Path, holds: list[Hold], dest: Path, on_progress=None) -> Path:
    info = media_info(source)
    duration = float(info["duration"])
    ordered = sorted((h for h in holds if h.duration > 0.02), key=lambda h: h.at_source)
    if not ordered:
        if source.resolve() != dest.resolve():
            shutil.copy2(source, dest)
        if on_progress:
            on_progress(1.0)
        return dest

    play_ranges: list[tuple[float, float, float]] = []
    cursor = 0.0
    for hold in ordered:
        at = min(max(0.0, hold.at_source), duration)
        if at > cursor + 0.01:
            play_ranges.append((cursor, at, hold.duration))
        elif play_ranges:
            start, end, extra = play_ranges[-1]
            play_ranges[-1] = (start, end, extra + hold.duration)
        else:
            play_ranges.append((0.0, min(0.05, duration), hold.duration))
        cursor = at
    if cursor < duration - 0.01:
        play_ranges.append((cursor, duration, 0.0))
    if not play_ranges:
        play_ranges.append((0.0, duration, 0.0))

    has_audio = bool(info["has_audio"])
    fps = float(info["fps"] or 30.0)
    count = len(play_ranges)
    filters = []
    v_split = "".join(f"[vs{i}]" for i in range(count))
    filters.append(f"[0:v]split={count}{v_split}")
    if has_audio:
        a_split = "".join(f"[as{i}]" for i in range(count))
        filters.append(f"[0:a]asplit={count}{a_split}")
    vlabels = []
    alabels = []
    for index, (start, end, pad) in enumerate(play_ranges):
        piece = (
            f"[vs{index}]trim=start={start:.3f}:end={max(start + 0.04, end):.3f},"
            f"setpts=PTS-STARTPTS,fps={fps:.3f}"
        )
        if pad > 0.02:
            piece += f",tpad=stop_mode=clone:stop_duration={pad:.3f}"
        piece += f"[v{index}]"
        filters.append(piece)
        vlabels.append(f"[v{index}]")
        if has_audio:
            audio = (
                f"[as{index}]atrim=start={start:.3f}:end={max(start + 0.04, end):.3f},"
                f"asetpts=PTS-STARTPTS"
            )
            if pad > 0.02:
                audio += f",apad=pad_dur={pad:.3f}"
            audio += f"[a{index}]"
            filters.append(audio)
            alabels.append(f"[a{index}]")

    if has_audio:
        filters.append(
            f"{''.join(vlabels)}{''.join(alabels)}concat=n={len(vlabels)}:v=1:a=1[vout][aout]"
        )
    else:
        filters.append(f"{''.join(vlabels)}concat=n={len(vlabels)}:v=1:a=0[vout]")
    script = dest.with_suffix(".hold.ffscript")
    script.write_text(";\n".join(filters), encoding="utf-8")
    cmd = [
        "-i",
        str(source),
        "-filter_complex_script",
        str(script),
        "-map",
        "[vout]",
    ]
    if has_audio:
        cmd.extend(["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.append("-an")
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
            "-y",
        ]
    )
    out_duration = duration + sum(hold.duration for hold in ordered)
    run(cmd, on_progress=on_progress, duration=out_duration)
    return dest


def blur_pixels(blur: BlurRegion, width: int, height: int) -> tuple[int, int, int, int] | None:
    if width < 16 or height < 16:
        return None
    x = int(round(float(blur.x) * width))
    y = int(round(float(blur.y) * height))
    w = int(round(float(blur.w) * width))
    h = int(round(float(blur.h) * height))
    if x % 2:
        x -= 1
    if y % 2:
        y -= 1
    x = max(0, x)
    y = max(0, y)
    if w % 2:
        w -= 1
    if h % 2:
        h -= 1
    w = max(8, w)
    h = max(8, h)
    if x + w > width:
        w = width - x
        if w % 2:
            w -= 1
    if y + h > height:
        h = height - y
        if h % 2:
            h -= 1
    if w < 8 or h < 8:
        return None
    return x, y, w, h


def blur_filter_script(blurs: list[BlurRegion], width: int, height: int, duration: float) -> str:
    filters: list[str] = []
    current = "0:v"
    placed = 0
    for blur in blurs:
        if blur.end - blur.start < 0.05 or blur.w <= 0.01 or blur.h <= 0.01:
            continue
        pixels = blur_pixels(blur, width, height)
        if not pixels:
            continue
        x, y, w, h = pixels
        luma = max(1, min(20, w // 2 - 1, h // 2 - 1))
        # Chroma is subsampled, so its radius has to stay within about a quarter of the crop.
        chroma = max(1, min(luma, w // 4 - 1, h // 4 - 1))
        start = max(0.0, min(duration, float(blur.start)))
        end = max(start + 0.04, min(duration, float(blur.end)))
        filters.append(f"[{current}]split=2[base{placed}][src{placed}]")
        filters.append(
            f"[src{placed}]crop={w}:{h}:{x}:{y},boxblur={luma}:1:{chroma}:1[blur{placed}]"
        )
        filters.append(
            f"[base{placed}][blur{placed}]overlay={x}:{y}:enable='between(t\\,{start:.3f}\\,{end:.3f})'[v{placed}]"
        )
        current = f"v{placed}"
        placed += 1
    if not filters:
        return ""
    filters[-1] = filters[-1].rsplit("[", 1)[0] + "[vout]"
    return ";\n".join(filters)


def atempo_chain(rate: float) -> str:
    factors: list[float] = []
    remaining = float(rate)
    while remaining < 0.5 - 1e-6:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0 + 1e-6:
        factors.append(2.0)
        remaining /= 2.0
    factors.append(remaining)
    return ",".join(f"atempo={factor:.5f}" for factor in factors)


def apply_blurs(source: Path, blurs: list[BlurRegion], dest: Path, on_progress=None) -> Path:
    info = media_info(source)
    width = int(info["width"] or 0)
    height = int(info["height"] or 0)
    duration = max(float(info["duration"] or 0.0), 0.1)
    script_body = blur_filter_script(blurs, width, height, duration)
    if not script_body:
        if source.resolve() != dest.resolve():
            shutil.copy2(source, dest)
        if on_progress:
            on_progress(1.0)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    script = dest.with_suffix(".blur.ffscript")
    script.write_text(script_body, encoding="utf-8")
    cmd = [
        "-i",
        str(source),
        "-filter_complex_script",
        str(script),
        "-map",
        "[vout]",
    ]
    if info["has_audio"]:
        cmd.extend(["-map", "0:a", "-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.append("-an")
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
            "-y",
        ]
    )
    run(cmd, on_progress=on_progress, duration=duration)
    return dest


def apply_speed(source: Path, dest: Path, rate: float, on_progress=None) -> Path:
    rate = clamp_speed(rate)
    if abs(rate - 1.0) < 0.005:
        if source.resolve() != dest.resolve():
            shutil.copy2(source, dest)
        if on_progress:
            on_progress(1.0)
        return dest
    info = media_info(source)
    duration = max(float(info["duration"] or 0.0), 0.1)
    out_duration = max(0.1, duration / rate)
    filters = [f"[0:v]setpts=PTS/{rate:.5f}[vout]"]
    if info["has_audio"]:
        filters.append(f"[0:a]{atempo_chain(rate)}[aout]")
    script = dest.with_suffix(".speed.ffscript")
    script.write_text(";\n".join(filters), encoding="utf-8")
    cmd = [
        "-i",
        str(source),
        "-filter_complex_script",
        str(script),
        "-map",
        "[vout]",
    ]
    if info["has_audio"]:
        cmd.extend(["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.append("-an")
    cmd.extend(
        [
            "-t",
            f"{out_duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
            "-y",
        ]
    )
    run(cmd, on_progress=on_progress, duration=out_duration)
    return dest


def mix_tts(
    video: Path,
    tts_items: list[tuple[float, Path]],
    dest: Path,
    keep_original_audio: bool = False,
    music_path: str | None = None,
    music_volume: float = 0.2,
    voice_volume: float = 1.0,
    on_progress=None,
) -> Path:
    info = media_info(video)
    duration = max(float(info["duration"]), 0.1)
    inputs = ["-i", str(video)]
    audio_labels = []
    filters = []
    next_index = 1

    if keep_original_audio and info["has_audio"]:
        filters.append("[0:a]aresample=44100,aformat=channel_layouts=stereo[basea]")
        audio_labels.append("[basea]")
    else:
        inputs.extend(
            [
                "-f",
                "lavfi",
                "-t",
                f"{duration:.3f}",
                "-i",
                "anullsrc=r=44100:cl=stereo",
            ]
        )
        audio_labels.append("[1:a]")
        next_index = 2

    if music_path and Path(music_path).is_file() and music_volume > 0.001:
        inputs.extend(["-stream_loop", "-1", "-i", str(music_path)])
        label = f"m{next_index}"
        vol = max(0.0, min(2.0, float(music_volume)))
        filters.append(
            f"[{next_index}:a]aresample=44100,aformat=channel_layouts=stereo,"
            f"atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,volume={vol:.3f}[{label}]"
        )
        audio_labels.append(f"[{label}]")
        next_index += 1

    gain = max(0.0, min(4.0, float(voice_volume) * 2.8))
    for delay_sec, path in tts_items:
        inputs.extend(["-i", str(path)])
        delay_ms = max(0, int(round(delay_sec * 1000)))
        label = f"t{next_index}"
        filters.append(
            f"[{next_index}:a]aresample=44100,aformat=channel_layouts=stereo,"
            f"volume={gain:.3f},alimiter=limit=0.99,adelay={delay_ms}:all=1[{label}]"
        )
        audio_labels.append(f"[{label}]")
        next_index += 1

    mix_count = len(audio_labels)
    if mix_count == 1:
        filters.append(f"{audio_labels[0]}anull[aout]")
    else:
        filters.append(
            f"{''.join(audio_labels)}amix=inputs={mix_count}:duration=first:"
            f"dropout_transition=0:normalize=0[aout]"
        )

    script = dest.with_suffix(".mix.ffscript")
    script.write_text(";\n".join(filters), encoding="utf-8")
    run(
        [
            *inputs,
            "-filter_complex_script",
            str(script),
            "-map",
            "0:v",
            "-map",
            "[aout]",
            "-t",
            f"{duration:.3f}",
            *YOUTUBE_ARGS,
            str(dest),
            "-y",
        ],
        on_progress=on_progress,
        duration=duration,
    )
    return dest


def export_project(
    project: Project,
    plan: TimelinePlan,
    tts_paths: list[Path],
    dest: str | Path,
    work_dir: str | Path,
    progress=None,
    percent_start: int = 0,
    percent_end: int = 99,
) -> Path:
    dest = Path(dest)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    span = max(1, int(percent_end) - int(percent_start))

    def emit(message: str, percent: float) -> None:
        if not progress:
            return
        value = max(0, min(100, int(round(percent))))
        try:
            progress(message, value)
        except TypeError:
            progress(message)

    def hook(label: str, start: float, end: float):
        def _inner(fraction: float) -> None:
            frac = min(1.0, max(0.0, float(fraction)))
            emit(f"{label}…", percent_start + span * (start + (end - start) * frac))

        return _inner

    emit("Joining clips…", percent_start)
    joined = work / "joined.mp4"
    concat_clips(
        project.clips,
        joined,
        mute=project.mute_original,
        on_progress=hook("Joining clips", 0.0, 0.28),
    )
    picture = joined
    blurs = list(getattr(project, "blurs", []) or [])
    if blurs:
        emit("Blurring regions…", percent_start + span * 0.28)
        picture = work / "blurred.mp4"
        apply_blurs(
            joined,
            blurs,
            picture,
            on_progress=hook("Blurring regions", 0.28, 0.44),
        )
    emit("Inserting freeze frames…", percent_start + span * 0.44)
    held = work / "held.mp4"
    apply_holds(
        picture,
        plan.holds,
        held,
        on_progress=hook("Inserting freeze frames", 0.44, 0.62),
    )
    items = [
        (cue.output_time, path)
        for cue, path in zip(plan.cues, tts_paths)
        if path and Path(path).is_file()
    ]
    rate = clamp_speed(getattr(project, "speed", 1.0))
    mix_dest = work / "mixed.mp4" if abs(rate - 1.0) >= 0.005 else dest
    emit("Encoding final MP4…", percent_start + span * 0.62)
    mix_end = 0.86 if mix_dest != dest else 1.0
    mix_tts(
        held,
        items,
        mix_dest,
        keep_original_audio=not project.mute_original,
        music_path=project.music_path,
        music_volume=project.music_volume,
        voice_volume=getattr(project, "voice_volume", 1.0),
        on_progress=hook("Encoding final MP4", 0.62, mix_end),
    )
    if mix_dest != dest:
        emit("Applying speed…", percent_start + span * mix_end)
        apply_speed(
            mix_dest,
            dest,
            rate,
            on_progress=hook("Applying speed", mix_end, 1.0),
        )
    return dest
