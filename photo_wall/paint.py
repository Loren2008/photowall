"""把已经排好的瓦片画成没有缝的画面。"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from photo_wall.layout import Tile, compute_layout

BACKGROUND = QColor(17, 17, 17)
PLACEHOLDER = QColor(36, 36, 36)


def fit_decode_size(
    src_w: int,
    src_h: int,
    cell_w: int,
    cell_h: int,
    dpr: float,
) -> tuple[int, int]:
    """整张图解码到能放进这一格的像素，不放大超过原图，也不丢掉画面。"""

    if src_w <= 0 or src_h <= 0:
        return 1, 1
    target_w = max(1, int(math.ceil(cell_w * dpr)))
    target_h = max(1, int(math.ceil(cell_h * dpr)))
    scale = min(1.0, target_w / src_w, target_h / src_h)
    return max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))


def contain_decode_size(
    src_w: int,
    src_h: int,
    view_w: int,
    view_h: int,
    dpr: float,
) -> tuple[int, int]:
    """单张查看时的解码尺寸：完整放进窗口，且不放大超过原图。"""

    if src_w <= 0 or src_h <= 0:
        return 1, 1
    scale = min(1.0, (view_w * dpr) / src_w, (view_h * dpr) / src_h)
    return max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))


def contain_rect(
    src_w: int,
    src_h: int,
    view_w: int,
    view_h: int,
    dpr: float,
) -> tuple[float, float, float, float]:
    if src_w <= 0 or src_h <= 0 or view_w <= 0 or view_h <= 0:
        return 0.0, 0.0, 0.0, 0.0
    dpr = dpr or 1.0
    scale = min(1.0, (view_w * dpr) / src_w, (view_h * dpr) / src_h)
    draw_w = src_w / dpr * scale
    draw_h = src_h / dpr * scale
    return (view_w - draw_w) / 2, (view_h - draw_h) / 2, draw_w, draw_h


def fit_image(source: QImage, dst_w: int, dst_h: int) -> QImage:
    """把整张图等比放进目标像素，不裁掉任何一边。"""

    dst_w = max(1, dst_w)
    dst_h = max(1, dst_h)
    output = QImage(dst_w, dst_h, QImage.Format.Format_RGB32)
    output.fill(BACKGROUND)
    if source.isNull() or source.width() <= 0 or source.height() <= 0:
        return output
    scale = min(dst_w / source.width(), dst_h / source.height())
    draw_w = source.width() * scale
    draw_h = source.height() * scale
    # 取整最多差 1 像素时贴齐格子，避免图和格之间出现细缝。画面仍然完整。
    if dst_w - draw_w <= 1.01 and dst_h - draw_h <= 1.01:
        draw_w = float(dst_w)
        draw_h = float(dst_h)
    x = (dst_w - draw_w) / 2
    y = (dst_h - draw_h) / 2
    painter = QPainter(output)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawImage(QRectF(x, y, draw_w, draw_h), source)
    painter.end()
    return output


def paint_layout(
    painter: QPainter,
    tiles: list[Tile],
    frames: dict[int, QImage],
    dpr: float,
    view_w: int,
    view_h: int,
    cache: dict | None = None,
) -> None:
    """在逻辑坐标系里把瓦片贴满。相邻瓦片重叠 1 个物理像素，避免出现细缝。"""

    paint_transformed(painter, tiles, frames, dpr, view_w, view_h, 1.0, 0.0, 0.0, cache, draft=False)


def paint_transformed(
    painter: QPainter,
    tiles: list[Tile],
    frames: dict[int, QImage],
    dpr: float,
    view_w: int,
    view_h: int,
    zoom: float,
    pan_x: float,
    pan_y: float,
    cache: dict | None = None,
    draft: bool = False,
    pixmaps: dict | None = None,
) -> None:
    """按缩放和平移把瓦片画到屏幕上。zoom 为 1、pan 为 0 时与铺满窗口相同。"""

    dpr = dpr or 1.0
    zoom = zoom or 1.0
    pad = 1.0 / dpr
    smooth: bool | None = None
    for tile in tiles:
        if tile.w <= 0 or tile.h <= 0:
            continue
        sx = tile.x * zoom + pan_x
        sy = tile.y * zoom + pan_y
        sw = tile.w * zoom
        sh = tile.h * zoom
        if sw <= 0 or sh <= 0:
            continue
        if sx + sw < 0 or sy + sh < 0 or sx > view_w or sy > view_h:
            continue
        dest_w = sw
        dest_h = sh
        if sx + sw < view_w:
            dest_w += pad
        if sy + sh < view_h:
            dest_h += pad
        pixmap = None if pixmaps is None else pixmaps.get(tile.index)
        if pixmap is not None and not pixmap.isNull():
            pixel_w = max(1, int(round(sw * dpr)))
            pixel_h = max(1, int(round(sh * dpr)))
            use_smooth = _needs_filter(pixmap.width(), pixmap.height(), pixel_w, pixel_h)
            if use_smooth != smooth:
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, use_smooth)
                smooth = use_smooth
            painter.drawPixmap(
                QRectF(sx, sy, dest_w, dest_h),
                pixmap,
                QRectF(0, 0, pixmap.width(), pixmap.height()),
            )
            continue
        frame = frames.get(tile.index)
        if frame is None or frame.isNull():
            painter.fillRect(QRectF(sx, sy, sw, sh), PLACEHOLDER)
            continue
        pixel_w = max(1, int(round(sw * dpr)))
        pixel_h = max(1, int(round(sh * dpr)))
        # 缩放过程中直接画已有的图。停稳并且图已经够大时，再做成屏幕像素大小，避免发糊。
        if draft or frame.width() + 1 < pixel_w or frame.height() + 1 < pixel_h:
            if smooth is not True:
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                smooth = True
            painter.drawImage(QRectF(sx, sy, dest_w, dest_h), frame)
            continue
        image = _cached_tile(cache, tile.index, frame, pixel_w, pixel_h)
        if smooth is not False:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            smooth = False
        painter.drawImage(QRectF(sx, sy, dest_w, dest_h), image)


def _needs_filter(src_w: int, src_h: int, dst_w: int, dst_h: int) -> bool:
    """接近屏幕像素大小时直接贴上，又快又清楚。差得多才做平滑缩放。"""

    if src_w <= 0 or src_h <= 0:
        return True
    return abs(dst_w / src_w - 1.0) > 0.08 or abs(dst_h / src_h - 1.0) > 0.08


def render_wall(
    frames: list[QImage],
    aspects: list[float],
    width: int,
    height: int,
    dpr: float = 1.0,
) -> tuple[QImage, list[Tile]]:
    result = compute_layout(aspects, width, height)
    canvas = QImage(
        max(1, int(round(width * dpr))),
        max(1, int(round(height * dpr))),
        QImage.Format.Format_RGB32,
    )
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(BACKGROUND)
    painter = QPainter(canvas)
    paint_layout(
        painter,
        result.tiles,
        {index: image for index, image in enumerate(frames)},
        dpr,
        width,
        height,
    )
    painter.end()
    return canvas, result.tiles


def _cached_tile(
    cache: dict | None,
    index: int,
    frame: QImage,
    pixel_w: int,
    pixel_h: int,
) -> QImage:
    if cache is not None:
        cached = cache.get(index)
        if cached is not None and cached[0] == (pixel_w, pixel_h):
            return cached[1]
    image = fit_image(frame, pixel_w, pixel_h)
    if cache is not None:
        cache[index] = ((pixel_w, pixel_h), image)
    return image
