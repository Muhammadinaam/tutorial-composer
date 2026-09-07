from __future__ import annotations

import shutil
from pathlib import Path

from app.engine.edl import target_video_size
from app.engine.ffmpeg import ffmpeg_path, media_info, run
from app.engine.project import Clip, Project
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


def concat_clips(clips: list[Clip], dest: Path, mute: bool = True) -> Path:
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
            ]
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
            ]
        )
    return dest


def apply_holds(source: Path, holds: list[Hold], dest: Path) -> Path:
    info = media_info(source)
    duration = float(info["duration"])
    ordered = sorted((h for h in holds if h.duration > 0.02), key=lambda h: h.at_source)
    if not ordered:
        if source.resolve() != dest.resolve():
            shutil.copy2(source, dest)
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
    run(cmd)
    return dest


def mix_tts(
    video: Path,
    tts_items: list[tuple[float, Path]],
    dest: Path,
    keep_original_audio: bool = False,
    music_path: str | None = None,
    music_volume: float = 0.2,
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

    for delay_sec, path in tts_items:
        inputs.extend(["-i", str(path)])
        delay_ms = max(0, int(round(delay_sec * 1000)))
        label = f"t{next_index}"
        filters.append(
            f"[{next_index}:a]aresample=44100,aformat=channel_layouts=stereo,"
            f"volume=1.6,adelay={delay_ms}:all=1[{label}]"
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
        ]
    )
    return dest


def export_project(
    project: Project,
    plan: TimelinePlan,
    tts_paths: list[Path],
    dest: str | Path,
    work_dir: str | Path,
) -> Path:
    dest = Path(dest)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    joined = work / "joined.mp4"
    concat_clips(project.clips, joined, mute=project.mute_original)
    held = work / "held.mp4"
    apply_holds(joined, plan.holds, held)
    items = [
        (cue.output_time, path)
        for cue, path in zip(plan.cues, tts_paths)
        if path and Path(path).is_file()
    ]
    mix_tts(
        held,
        items,
        dest,
        keep_original_audio=not project.mute_original,
        music_path=project.music_path,
        music_volume=project.music_volume,
    )
    return dest
