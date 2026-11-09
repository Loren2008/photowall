"""照片墙的缩放和平移。放大始终对着画面中心。"""

from __future__ import annotations

MIN_ZOOM = 1.0
MAX_ZOOM = 48.0
ZOOM_STEP = 1.25


def zoom_about_center(
    pan_x: float,
    pan_y: float,
    zoom: float,
    view_w: float,
    view_h: float,
    new_zoom: float,
) -> tuple[float, float]:
    """改变缩放时，让屏幕中心底下的那一点留在原地。"""

    if zoom <= 0:
        return pan_x, pan_y
    center_x = view_w / 2
    center_y = view_h / 2
    world_x = (center_x - pan_x) / zoom
    world_y = (center_y - pan_y) / zoom
    return center_x - world_x * new_zoom, center_y - world_y * new_zoom


def content_bounds(tiles) -> tuple[float, float, float, float]:
    if not tiles:
        return 0.0, 0.0, 0.0, 0.0
    min_x = min(tile.x for tile in tiles)
    min_y = min(tile.y for tile in tiles)
    max_x = max(tile.x + tile.w for tile in tiles)
    max_y = max(tile.y + tile.h for tile in tiles)
    return float(min_x), float(min_y), float(max_x), float(max_y)


def clamp_pan(
    pan_x: float,
    pan_y: float,
    zoom: float,
    view_w: float,
    view_h: float,
    bounds: tuple[float, float, float, float],
) -> tuple[float, float]:
    """放大后不把整面墙拖出画面；缩到比窗口小时则居中。"""

    if zoom <= 0 or view_w <= 0 or view_h <= 0:
        return pan_x, pan_y
    min_x, min_y, max_x, max_y = bounds
    left = min_x * zoom
    top = min_y * zoom
    right = max_x * zoom
    bottom = max_y * zoom
    content_w = right - left
    content_h = bottom - top
    if content_w <= view_w:
        pan_x = (view_w - content_w) / 2 - left
    else:
        pan_x = min(pan_x, -left)
        pan_x = max(pan_x, view_w - right)
    if content_h <= view_h:
        pan_y = (view_h - content_h) / 2 - top
    else:
        pan_y = min(pan_y, -top)
        pan_y = max(pan_y, view_h - bottom)
    return pan_x, pan_y


def zoom_factor_from_magnification(value: float) -> float | None:
    """触控板捏合的 magnification 是相对变化量，0.02 表示放大 2%。"""

    if abs(value) < 1e-4:
        return None
    if -0.85 <= value <= 0.85:
        factor = 1.0 + value
    elif value > 0.85:
        factor = value
    else:
        return None
    if factor <= 0:
        return None
    return max(0.5, min(1.5, factor))
