from __future__ import annotations

from dataclasses import dataclass

from app.engine.project import Cue


@dataclass
class PlannedCue:
    video_time: float
    text: str
    tts_duration: float
    gap: float
    hold: float
    output_time: float
    should_video_stop: bool = False


@dataclass
class Hold:
    at_source: float
    duration: float


@dataclass
class TimelinePlan:
    cues: list[PlannedCue]
    holds: list[Hold]
    source_duration: float
    output_duration: float

    @property
    def total_hold(self) -> float:
        return sum(h.duration for h in self.holds)


def build_timeline(
    cues: list[Cue],
    durations: list[float],
    source_duration: float,
) -> TimelinePlan:
    if len(durations) != len(cues):
        raise ValueError("Each cue needs a speech duration.")
    planned: list[PlannedCue] = []
    holds: list[Hold] = []
    extra = 0.0
    source_duration = max(0.0, float(source_duration))

    for index, cue in enumerate(cues):
        next_time = (
            cues[index + 1].video_time if index + 1 < len(cues) else source_duration
        )
        gap = max(0.0, next_time - cue.video_time)
        speech = max(0.0, float(durations[index]))
        stop = bool(getattr(cue, "should_video_stop", False))
        if stop:
            hold = speech
            hold_at = cue.video_time
        else:
            hold = max(0.0, speech - gap)
            hold_at = min(max(cue.video_time, next_time), source_duration)
        output_time = cue.video_time + extra
        planned.append(
            PlannedCue(
                video_time=cue.video_time,
                text=cue.text,
                tts_duration=speech,
                gap=gap,
                hold=hold,
                output_time=output_time,
                should_video_stop=stop,
            )
        )
        if hold > 0.001:
            holds.append(
                Hold(
                    at_source=min(max(0.0, hold_at), source_duration),
                    duration=hold,
                )
            )
            extra += hold

    last_speech_end = 0.0
    if planned:
        last = planned[-1]
        last_speech_end = last.output_time + last.tts_duration
    output_duration = max(source_duration + extra, last_speech_end)
    return TimelinePlan(
        cues=planned,
        holds=holds,
        source_duration=source_duration,
        output_duration=output_duration,
    )
