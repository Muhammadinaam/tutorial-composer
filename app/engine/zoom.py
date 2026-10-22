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


def zoom_filter_script(regions: list[BlurRegion], width: int, height: int) -> str:
    """FFmpeg filtergraph from ``[0:v]`` to ``[vout]``, or "" when there is no zoom.

    Commas inside the crop expressions are escaped so the filtergraph parser
    keeps each expression intact.
    """
    zooms = sorted(
        (item for item in regions if item.is_zoom() and float(item.end) > float(item.start)),
        key=lambda item: (float(item.start), item.id),
    )
    if not zooms or width < 2 or height < 2:
        return ""
    x_expr = "0"
    y_expr = "0"
    w_expr = "in_w"
    h_expr = "in_h"
    for region in zooms:
        amount = _ease_expr(region)
        target_x, target_y, target_w, target_h = target_crop(region, width, height)
        x_expr = f"if(gt({amount},0),{_lerp_expr(amount, '0', f'{target_x:.4f}')},{x_expr})"
        y_expr = f"if(gt({amount},0),{_lerp_expr(amount, '0', f'{target_y:.4f}')},{y_expr})"
        w_expr = f"if(gt({amount},0),{_lerp_expr(amount, 'in_w', f'{target_w:.4f}')},{w_expr})"
        h_expr = f"if(gt({amount},0),{_lerp_expr(amount, 'in_h', f'{target_h:.4f}')},{h_expr})"
    x_expr = f"max(0,min(in_w-out_w,{x_expr}))"
    y_expr = f"max(0,min(in_h-out_h,{y_expr}))"
    w_expr = f"min(in_w,max(2,{w_expr}))"
    h_expr = f"min(in_h,max(2,{h_expr}))"

    def escaped(expr: str) -> str:
        return expr.replace(",", "\\,")

    return (
        "[0:v]format=gbrp,"
        f"crop=w='{escaped(w_expr)}':h='{escaped(h_expr)}':x='{escaped(x_expr)}':y='{escaped(y_expr)}',"
        f"scale={int(width)}:{int(height)}:flags=lanczos,format=yuv420p[vout]"
    )
