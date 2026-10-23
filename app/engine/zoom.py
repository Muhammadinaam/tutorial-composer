"""Animated zoom crops shared by the preview and the exporter.

A zoom region names the rectangle that must stay on screen. The crop keeps the
frame's aspect ratio, stays inside the picture, and never goes past 4x.
"""

from __future__ import annotations

from app.engine.project import BlurRegion

EASE_SECONDS = 0.4
MAX_ZOOM = 4.0


def smoothstep(unit: float) -> float:
    unit = max(0.0, min(1.0, float(unit)))
    return unit * unit * (3.0 - 2.0 * unit)


def ease_seconds(region: BlurRegion) -> float:
    span = max(0.0, float(region.end) - float(region.start))
    return min(EASE_SECONDS, span / 2.0)


def ease_amount(region: BlurRegion, time: float) -> float:
    """0 outside the region, 1 while held, and a smoothstep on the way in and out."""
    start = float(region.start)
    end = float(region.end)
    moment = float(time)
    if moment < start or moment > end:
        return 0.0
    ease = ease_seconds(region)
    if ease < 1e-4:
        return 1.0
    if moment < start + ease:
        return smoothstep((moment - start) / ease)
    if moment > end - ease:
        return smoothstep((end - moment) / ease)
    return 1.0


def target_crop(region: BlurRegion, width: int, height: int) -> tuple[float, float, float, float]:
    """Pixel crop that contains ``region`` without stretching the frame."""
    frame_w = float(width)
    frame_h = float(height)
    if frame_w < 2 or frame_h < 2:
        return 0.0, 0.0, max(frame_w, 1.0), max(frame_h, 1.0)
    aspect = frame_w / frame_h
    rect_w = max(1.0, float(region.w) * frame_w)
    rect_h = max(1.0, float(region.h) * frame_h)
    if rect_w / rect_h >= aspect:
        crop_w = rect_w
        crop_h = rect_w / aspect
    else:
        crop_h = rect_h
        crop_w = rect_h * aspect
    scale = max(1.0, (frame_w / MAX_ZOOM) / crop_w, (frame_h / MAX_ZOOM) / crop_h)
    crop_w *= scale
    crop_h *= scale
    if crop_w > frame_w or crop_h > frame_h:
        crop_w = frame_w
        crop_h = frame_h
    center_x = (float(region.x) + float(region.w) / 2.0) * frame_w
    center_y = (float(region.y) + float(region.h) / 2.0) * frame_h
    crop_x = min(max(0.0, center_x - crop_w / 2.0), frame_w - crop_w)
    crop_y = min(max(0.0, center_y - crop_h / 2.0), frame_h - crop_h)
    return crop_x, crop_y, crop_w, crop_h


def zoom_crop(
    regions: list[BlurRegion], time: float, width: int, height: int
) -> tuple[float, float, float, float]:
    """Crop in pixels at ``time``. The full frame is returned when nothing is active.

    Overlapping zooms do not blend. The one that started later wins, and its
    own ease still runs from the full frame to its target.
    """
    frame_w = float(width)
    frame_h = float(height)
    full = (0.0, 0.0, max(frame_w, 1.0), max(frame_h, 1.0))
    winner: BlurRegion | None = None
    amount = 0.0
    for region in sorted(
        (item for item in regions if item.is_zoom()),
        key=lambda item: (float(item.start), item.id),
    ):
        eased = ease_amount(region, time)
        if eased > 1e-6:
            winner = region
            amount = eased
    if winner is None or frame_w < 2 or frame_h < 2:
        return full
    target_x, target_y, target_w, target_h = target_crop(winner, width, height)
    crop_w = min(frame_w, max(1.0, (1.0 - amount) * frame_w + amount * target_w))
    crop_h = min(frame_h, max(1.0, (1.0 - amount) * frame_h + amount * target_h))
    crop_x = min(max(0.0, amount * target_x), frame_w - crop_w)
    crop_y = min(max(0.0, amount * target_y), frame_h - crop_h)
    return crop_x, crop_y, crop_w, crop_h


def _smoothstep_expr(unit: str) -> str:
    return f"({unit})*({unit})*(3-2*({unit}))"


def _ease_expr(region: BlurRegion) -> str:
    start = float(region.start)
    end = float(region.end)
    ease = ease_seconds(region)
    if ease < 1e-4:
        return f"between(t,{start:.4f},{end:.4f})"
    rise = _smoothstep_expr(f"(t-{start:.4f})/{ease:.4f}")
    fall = _smoothstep_expr(f"({end:.4f}-t)/{ease:.4f}")
    return (
        f"if(lt(t,{start:.4f}),0,"
        f"if(gt(t,{end:.4f}),0,"
        f"if(lt(t,{start + ease:.4f}),{rise},"
        f"if(gt(t,{end - ease:.4f}),{fall},1))))"
    )


def _lerp_expr(amount: str, start: str, end: str) -> str:
    return f"((1-({amount}))*({start})+({amount})*({end}))"


def _animated_crop_exprs(
    zooms: list[BlurRegion], width: int, height: int
) -> tuple[str, str, str, str]:
    """Source-pixel crop expressions that follow :func:`zoom_crop`.

    The numbers are constants. ``in_w`` must not appear here: the same text is
    evaluated both while scaling the original frame and while cropping the
    enlarged one, and ``in_w`` means a different picture in each filter.
    """
    frame_w = f"{float(width):.6f}"
    frame_h = f"{float(height):.6f}"
    cx = "0"
    cy = "0"
    cw = frame_w
    ch = frame_h
    for region in zooms:
        amount = _ease_expr(region)
        target_x, target_y, target_w, target_h = target_crop(region, width, height)
        next_cw = f"min({frame_w},max(1,{_lerp_expr(amount, frame_w, f'{target_w:.6f}')}))"
        next_ch = f"min({frame_h},max(1,{_lerp_expr(amount, frame_h, f'{target_h:.6f}')}))"
        next_cx = (
            f"max(0,min({frame_w}-({next_cw}),{_lerp_expr(amount, '0', f'{target_x:.6f}')}))"
        )
        next_cy = (
            f"max(0,min({frame_h}-({next_ch}),{_lerp_expr(amount, '0', f'{target_y:.6f}')}))"
        )
        active = f"gt({amount},0.000001)"
        cx = f"if({active},{next_cx},{cx})"
        cy = f"if({active},{next_cy},{cy})"
        cw = f"if({active},{next_cw},{cw})"
        ch = f"if({active},{next_ch},{ch})"
    return cx, cy, cw, ch


def _escape_filter_expr(expr: str) -> str:
    """Keep commas inside a filtergraph expression from splitting filters."""
    return expr.replace("\\", "\\\\").replace(",", "\\,")


def _rate_arg(fps: float) -> str:
    rate = float(fps or 0.0)
    if rate < 1:
        rate = 30.0
    nearest = round(rate)
    if abs(rate - nearest) < 0.015:
        return str(int(nearest))
    return f"{rate:.5f}"


def zoom_filter_script(
    regions: list[BlurRegion], width: int, height: int, fps: float = 30.0
) -> str:
    """FFmpeg filtergraph from ``[0:v]`` to ``[vout]``, or "" when there is no zoom.

    Crop remembers the size of its first input and will not look past it, and
    its width and height cannot change over time. The frame is scaled on every
    frame until the preview's crop fills the output, laid on a fixed black
    canvas, and the output window is cut from that canvas. One scale factor is
    used on both axes so the framed area stays on the same point the preview
    shows.
    """
    zooms = sorted(
        (item for item in regions if item.is_zoom() and float(item.end) > float(item.start)),
        key=lambda item: (float(item.start), item.id),
    )
    if not zooms or width < 2 or height < 2:
        return ""
    cx, cy, cw, _ch = _animated_crop_exprs(zooms, width, height)
    out_w = int(width)
    out_h = int(height)
    # The strongest zoom is the largest picture we have to hold. A few extra
    # pixels cover rounding, and even sizes stay valid for yuv420p later.
    tightest = min(target_crop(region, out_w, out_h)[2] for region in zooms)
    tightest = max(1.0, tightest)
    max_w = int(out_w * float(out_w) / tightest + 8)
    max_h = int(out_w * float(out_h) / tightest + 8)
    if max_w % 2:
        max_w += 1
    if max_h % 2:
        max_h += 1
    # One factor, out_w/cw, enlarges both axes. in_w and in_h are the source frame.
    scale_w = f"min({max_w},max({out_w},floor(({out_w})*in_w/({cw})+0.5)))"
    scale_h = f"min({max_h},max({out_h},floor(({out_w})*in_h/({cw})+0.5)))"
    # in_w/in_h here are the fixed canvas, which does not change size.
    out_x = f"max(0,min(in_w-{out_w},floor(({cx})*({out_w})/({cw})+0.5)))"
    out_y = f"max(0,min(in_h-{out_h},floor(({cy})*({out_w})/({cw})+0.5)))"
    rate = _rate_arg(fps)
    return (
        f"color=c=black:s={max_w}x{max_h}:r={rate},format=gbrp[bg];"
        "[0:v]format=gbrp,setsar=1,"
        f"scale=w='{_escape_filter_expr(scale_w)}':h='{_escape_filter_expr(scale_h)}'"
        ":eval=frame:flags=lanczos+accurate_rnd[fg];"
        "[bg][fg]overlay=0:0:shortest=1:format=auto,"
        f"crop=w={out_w}:h={out_h}:x='{_escape_filter_expr(out_x)}':y='{_escape_filter_expr(out_y)}'"
        ":exact=1,setsar=1,format=yuv420p[vout]"
    )
