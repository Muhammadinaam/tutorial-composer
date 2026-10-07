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
    input_of: dict[str, int] = {}
    scale = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps:.3f},format=yuv420p"
    )

    def real_input(clip: Clip) -> int:
        existing = input_of.get(clip.id)
        if existing is not None:
            return existing
        slot = len(input_of)
        input_of[clip.id] = slot
        inputs.extend(["-i", clip.path])
        return slot

    def nearest_picture(start: int, step: int) -> tuple[Clip, int] | None:
        index = start
        while 0 <= index < len(clips):
            candidate = clips[index]
            if not candidate.is_pause and candidate.path:
                return candidate, index
            index += step
        return None

    for index, clip in enumerate(clips):
        if clip.is_pause:
            held = nearest_picture(index - 1, -1) or nearest_picture(index + 1, 1)
            if held is None:
                raise ValueError("A pause needs a video or image frame to hold.")
            source, _source_index = held
            slot = real_input(source)
            duration = max(0.2, clip.used)
            if source.is_image:
                filters.append(
                    f"[{slot}:v]loop=-1:size=1,trim=duration={duration:.3f},"
                    f"setpts=PTS-STARTPTS,{scale}[v{index}]"
                )
            else:
                end = max(0.04, float(source.out_point if held[1] < index else source.in_point + 0.04))
                start = max(0.0, end - 0.04)
                pad = max(0.0, duration - (end - start))
                filters.append(
                    f"[{slot}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
                    f"tpad=stop_mode=clone:stop_duration={pad:.3f},{scale}[v{index}]"
                )
            concat_v.append(f"[v{index}]")
            if not mute:
                filters.append(
                    f"aevalsrc=0|0:s=44100:d={duration:.3f},aformat=channel_layouts=stereo[a{index}]"
                )
                concat_a.append(f"[a{index}]")
            continue
        slot = real_input(clip)
        start = max(0.0, clip.in_point)
        end = max(start + 0.04, clip.out_point)
        duration = max(0.04, end - start)
        if clip.is_image:
            filters.append(
                f"[{slot}:v]loop=-1:size=1,trim=duration={duration:.3f},"
                f"setpts=PTS-STARTPTS,{scale}[v{index}]"
            )
        else:
            filters.append(
                f"[{slot}:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
                f"{scale}[v{index}]"
            )
        concat_v.append(f"[v{index}]")
        if not mute:
            info = media_info(clip.path)
            if info["has_audio"] and not clip.is_image:
                filters.append(
                    f"[{slot}:a]atrim=start={start:.3f}:end={end:.3f},"
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
    filter_graph = ";".join(filters)
    cmd = [
        "-i",
        str(source),
        "-filter_complex",
        filter_graph,
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
        if blur.is_highlight():
            filters.append(
                f"[{current}]drawbox=x={x}:y={y}:w={w}:h={h}:color=red:t=4:"
                f"enable='between(t\\,{start:.3f}\\,{end:.3f})'[v{placed}]"
            )
        else:
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
    cmd = [
        "-i",
        str(source),
        "-filter_complex",
        script_body.replace("\n", ""),
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


def srt_timestamp(seconds: float) -> str:
    millis = int(round(max(0.0, float(seconds)) * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def caption_entries(plan: TimelinePlan, rate: float = 1.0) -> list[tuple[float, float, str]]:
    scale = clamp_speed(rate)
    entries: list[tuple[float, float, str]] = []
    for cue in plan.cues:
        text = (cue.text or "").replace("\r", "").strip()
        if not text:
            continue
        start = max(0.0, cue.output_time / scale)
        end = max(start + 0.2, (cue.output_time + max(0.2, cue.tts_duration)) / scale)
        entries.append((start, end, text))
    return entries


def format_srt(entries: list[tuple[float, float, str]]) -> str:
    blocks = []
    for index, (start, end, text) in enumerate(entries, start=1):
        blocks.append(f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n")
    return "\n".join(blocks)


def write_srt(path: str | Path, plan: TimelinePlan, rate: float = 1.0) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(format_srt(caption_entries(plan, rate)), encoding="utf-8")
    return dest


def _filter_path(path: Path) -> str:
    text = path.resolve().as_posix()
    return text.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")


def apply_captions(source: Path, srt: Path, dest: Path, on_progress=None) -> Path:
    info = media_info(source)
    duration = max(float(info["duration"] or 0.0), 0.1)
    style = (
        "FontName=Arial\\,FontSize=22\\,PrimaryColour=&H00FFFFFF\\,"
        "OutlineColour=&H00000000\\,BorderStyle=1\\,Outline=2\\,Shadow=0\\,Alignment=2"
    )
    filt = (
        f"[0:v]subtitles=filename='{_filter_path(srt)}':charenc=UTF-8:"
        f"force_style='{style}'[vout]"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "-i",
        str(source),
        "-filter_complex",
        filt,
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
    filter_graph = ";".join(filters)
    cmd = [
        "-i",
        str(source),
        "-filter_complex",
        filter_graph,
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

    filter_graph = ";".join(filters)
    run(
        [
            *inputs,
            "-filter_complex",
            filter_graph,
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
        on_progress=hook("Inserting freeze frames", 0.44, 0.58),
    )
    picture = held
    caption_srt = work / "captions.srt"
    write_srt(caption_srt, plan, rate=1.0)
    if getattr(project, "burn_subtitles", True) and caption_srt.read_text(encoding="utf-8").strip():
        emit("Burning subtitles…", percent_start + span * 0.58)
        captioned = work / "captioned.mp4"
        apply_captions(
            picture,
            caption_srt,
            captioned,
            on_progress=hook("Burning subtitles", 0.58, 0.68),
        )
        picture = captioned
    items = [
        (cue.output_time, path)
        for cue, path in zip(plan.cues, tts_paths)
        if path and Path(path).is_file()
    ]
    rate = clamp_speed(getattr(project, "speed", 1.0))
    mix_dest = work / "mixed.mp4" if abs(rate - 1.0) >= 0.005 else dest
    emit("Encoding final MP4…", percent_start + span * 0.68)
    mix_end = 0.86 if mix_dest != dest else 1.0
    mix_tts(
        picture,
        items,
        mix_dest,
        keep_original_audio=not project.mute_original,
        music_path=project.music_path,
        music_volume=project.music_volume,
        voice_volume=getattr(project, "voice_volume", 1.0),
        on_progress=hook("Encoding final MP4", 0.68, mix_end),
    )
    if mix_dest != dest:
        emit("Applying speed…", percent_start + span * mix_end)
        apply_speed(
            mix_dest,
            dest,
            rate,
            on_progress=hook("Applying speed", mix_end, 1.0),
        )
    write_srt(dest.with_suffix(".srt"), plan, rate=rate)
    return dest
