"""图片墙窗口：选文件或文件夹，自动铺满。"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QRectF,
    QRunnable,
    QSettings,
    QSize,
    QThreadPool,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QKeySequence,
    QNativeGestureEvent,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStatusBar,
    QToolBar,
    QWidget,
)

from photo_wall.decode import load_frame, probe_size
from photo_wall.layout import compute_layout
from photo_wall.library import collect_images, file_dialog_filter
from photo_wall.paint import (
    BACKGROUND,
    contain_rect,
    fit_decode_size,
    paint_transformed,
)
from photo_wall.viewport import (
    MAX_ZOOM,
    ZOOM_STEP,
    clamp_pan,
    content_bounds,
    zoom_about_center,
    zoom_factor_from_magnification,
)

_STYLE = """
QMainWindow, QToolBar, QStatusBar { background: #161616; color: #ececec; }
QToolBar { border: none; border-bottom: 1px solid #2a2a2a; spacing: 8px; padding: 6px 10px; }
QToolBar QToolButton {
    color: #f3f3f3;
    background: #2c2c2c;
    border: none;
    border-radius: 7px;
    padding: 6px 12px;
}
QToolBar QToolButton:hover { background: #3c3c3c; }
QToolBar QToolButton:pressed { background: #4a4a4a; }
QToolBar QToolButton:checked { background: #3d5a80; color: white; }
QStatusBar { border-top: 1px solid #2a2a2a; }
QStatusBar::item { border: none; }
QLabel#count { color: #c8c8c8; padding: 0 8px; }
"""


@dataclass
class Photo:
    path: str
    width: int
    height: int


class _Signals(QObject):
    scan_progress = Signal(int, int, int)
    scan_done = Signal(int, object, int)
    decoded = Signal(int, int, int, QImage)


class _ScanJob(QRunnable):
    def __init__(self, paths: list[str], generation: int, signals: _Signals):
        super().__init__()
        self.paths = paths
        self.generation = generation
        self.signals = signals

    def run(self) -> None:
        total = len(self.paths)
        sizes: list[tuple[int, int] | None] = [None] * total
        if total:
            workers = min(8, total, max(1, os.cpu_count() or 4))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_probe_one, path): index for index, path in enumerate(self.paths)
                }
                done = 0
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        sizes[index] = future.result()
                    except Exception:
                        sizes[index] = None
                    done += 1
                    if done == total or done % 24 == 0:
                        self.signals.scan_progress.emit(self.generation, done, total)
        photos: list[tuple[str, int, int]] = []
        failed = 0
        for path, size in zip(self.paths, sizes):
            if size is None:
                failed += 1
            else:
                photos.append((path, size[0], size[1]))
        self.signals.scan_done.emit(self.generation, photos, failed)


class _DecodeJob(QRunnable):
    def __init__(
        self,
        generation: int,
        index: int,
        token: int,
        path: str,
        out_w: int,
        out_h: int,
        signals: _Signals,
    ):
        super().__init__()
        self.generation = generation
        self.index = index
        self.token = token
        self.path = path
        self.out_w = out_w
        self.out_h = out_h
        self.signals = signals

    def run(self) -> None:
        image = QImage()
        try:
            loaded = load_frame(self.path, self.out_w, self.out_h)
            if loaded is not None:
                image = loaded
        except Exception:
            image = QImage()
        self.signals.decoded.emit(self.generation, self.index, self.token, image)


def _probe_one(path: str) -> tuple[int, int] | None:
    try:
        return probe_size(path)
    except Exception:
        return None


class WallView(QWidget):
    statusChanged = Signal(str)
    countChanged = Signal(int)
    filesDropped = Signal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAutoFillBackground(True)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), BACKGROUND)
        self.setPalette(palette)
        self.setMinimumSize(QSize(480, 320))
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)

        self.photos: list[Photo] = []
        self.tiles = []
        self.frames: dict[int, QImage] = {}
        self._pixmaps: dict[int, QPixmap] = {}
        self.failed_probe = 0
        self.failed_decode = 0
        self.focus_index: int | None = None
        self.focus_zoom = 1.0
        self.focus_pan_x = 0.0
        self.focus_pan_y = 0.0
        self.crop = 0.0
        self.filled = False
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._press_pos = None
        self._pan_origin = (0.0, 0.0)
        self._panning = False
        self._zoom_settled = True
        self._native_zoom_at = 0.0
        self._generation = 0
        self._layout_key = None
        self._cache: dict = {}
        self._prepared: dict = {}
        self._tokens: dict[int, int] = {}
        self._inflight: set[int] = set()
        self._inflight_size: dict[int, tuple[int, int]] = {}
        self._give_up: set[int] = set()
        self._decode_pending = False
        self._loading = False
        self._loading_text = ""
        self._drag_over = False
        self.include_subfolders = False
        self._signals = _Signals(self)
        self._signals.scan_progress.connect(self._on_scan_progress)
        self._signals.scan_done.connect(self._on_scan_done)
        self._signals.decoded.connect(self._on_decoded)

        self.caption = QLabel(self)
        self.caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.caption.setStyleSheet(
            "QLabel { background: rgba(0,0,0,175); color: white;"
            " padding: 5px 9px; border-radius: 7px; }"
        )
        self.caption.hide()

        self._decode_timer = QTimer(self)
        self._decode_timer.setSingleShot(True)
        self._decode_timer.setInterval(40)
        self._decode_timer.timeout.connect(self._on_interaction_settled)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(16)
        self._refresh_timer.timeout.connect(self._flush_refresh)
        self._refresh_status = False

    def set_files(self, paths: list[str]) -> None:
        self._generation += 1
        self.photos = []
        self.tiles = []
        self.frames.clear()
        self._pixmaps.clear()
        self._prepared.clear()
        self._cache.clear()
        self._tokens.clear()
        self._inflight.clear()
        self._inflight_size.clear()
        self._give_up.clear()
        self._decode_pending = False
        self.failed_probe = 0
        self.failed_decode = 0
        self.focus_index = None
        self.focus_zoom = 1.0
        self.focus_pan_x = 0.0
        self.focus_pan_y = 0.0
        self.crop = 0.0
        self.filled = False
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._press_pos = None
        self._panning = False
        self._zoom_settled = True
        self._layout_key = None
        self.caption.hide()
        if self.mouseGrabber() is self:
            self.releaseMouse()
        self._update_cursor()
        self._loading = True
        self._loading_text = "正在读取图片…"
        self.countChanged.emit(0)
        if not paths:
            self._loading = False
            self._emit_status()
            self.update()
            return
        self._emit_status()
        self.update()
        job = _ScanJob(list(paths), self._generation, self._signals)
        QThreadPool.globalInstance().start(job)

    def zoom_in(self) -> None:
        if self.focus_index is not None:
            self._set_focus_zoom(self.focus_zoom * ZOOM_STEP)
            return
        if not self.tiles:
            return
        self._set_zoom(self.zoom * ZOOM_STEP)

    def zoom_out(self) -> None:
        if self.focus_index is not None:
            self._set_focus_zoom(self.focus_zoom / ZOOM_STEP)
            return
        if not self.tiles:
            return
        self._set_zoom(self.zoom / ZOOM_STEP)

    def reset_zoom(self) -> None:
        if self.focus_index is not None:
            self.focus_zoom = 1.0
            self.focus_pan_x = 0.0
            self.focus_pan_y = 0.0
            self._press_pos = None
            self._panning = False
            if self.mouseGrabber() is self:
                self.releaseMouse()
            self._update_cursor()
            self._request_decodes()
            self._emit_status()
            self.update()
            return
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._press_pos = None
        self._panning = False
        if self.mouseGrabber() is self:
            self.releaseMouse()
        self._prepared.clear()
        self._zoom_settled = True
        self._update_cursor()
        self._layout_key = None
        self._relayout(debounce_decode=False)
        self._emit_status()
        self.update()

    def clear_focus(self) -> None:
        if self.focus_index is None:
            return
        self.focus_index = None
        self.focus_zoom = 1.0
        self.focus_pan_x = 0.0
        self.focus_pan_y = 0.0
        self._update_cursor()
        self._emit_status()
        self.update()

    def step_focus(self, delta: int) -> None:
        if self.focus_index is None or not self.photos:
            return
        self.focus_index = (self.focus_index + delta) % len(self.photos)
        self.focus_zoom = 1.0
        self.focus_pan_x = 0.0
        self.focus_pan_y = 0.0
        self._request_decodes()
        self._emit_status()
        self.update()

    def is_settled(self) -> bool:
        if self._loading or not self.photos:
            return False
        return len(self.frames) + self.failed_decode >= len(self.photos) and not self._inflight

    def shutdown(self) -> None:
        self._generation += 1

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout(debounce_decode=True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._relayout(debounce_decode=False)

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.NativeGesture and isinstance(event, QNativeGestureEvent):
            if self._handle_native_gesture(event):
                return True
        if event.type() == QEvent.Type.DevicePixelRatioChange and self.photos:
            self._prepared.clear()
            if self.zoom <= 1.001:
                self._layout_key = None
                self._relayout(debounce_decode=False)
            else:
                self._zoom_settled = False
                self._poke_decode()
                self.update()
        return super().event(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            self._paint(painter, event)
        finally:
            painter.end()

    def _paint(self, painter: QPainter, event) -> None:
        painter.setClipRect(event.rect())
        if self.focus_index is not None and self.photos:
            self._paint_focus(painter)
        elif self.tiles:
            painter.fillRect(event.rect(), BACKGROUND)
            paint_transformed(
                painter,
                self.tiles,
                self.frames,
                self.devicePixelRatioF(),
                self.width(),
                self.height(),
                self.zoom,
                self.pan_x,
                self.pan_y,
                None,
                pixmaps=self._pixmaps,
            )
        else:
            painter.fillRect(self.rect(), BACKGROUND)
            if self._loading:
                self._paint_message(painter, "正在读取", [self._loading_text or "正在读取图片…"])
            else:
                self._paint_message(
                    painter,
                    "把图片铺满窗口",
                    [
                        "拖入图片或文件夹，或使用上方的按钮",
                        "选择文件夹后会询问要不要包含子文件夹",
                        "JPG、PNG、WEBP、GIF、TIFF、BMP、HEIC",
                        "⌘O 选择图片     ⌘⇧O 选择文件夹",
                        "双指或 ⌘+ / ⌘− 朝画面中心放大，拖动查看",
                    ],
                )
        if self._drag_over:
            pen = QPen(QColor("#8eb4ff"))
            pen.setWidth(3)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(3, 3, -3, -3), 10, 10)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._can_pan():
            self._press_pos = event.position()
            if self.focus_index is not None:
                self._pan_origin = (self.focus_pan_x, self.focus_pan_y)
            else:
                self._pan_origin = (self.pan_x, self.pan_y)
            self._panning = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press_pos is not None:
            delta = event.position() - self._press_pos
            if not self._panning and abs(delta.x()) + abs(delta.y()) < 4:
                return
            if not self._panning:
                self._panning = True
                self.grabMouse()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self.focus_index is not None:
                self.focus_pan_x = self._pan_origin[0] + delta.x()
                self.focus_pan_y = self._pan_origin[1] + delta.y()
                self._clamp_focus_pan()
            else:
                self.pan_x = self._pan_origin[0] + delta.x()
                self.pan_y = self._pan_origin[1] + delta.y()
                self._clamp_pan()
            self.caption.hide()
            self._poke_decode()
            self.update()
            return
        if self.focus_index is not None:
            self.caption.hide()
            if self.focus_zoom > 1.001:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.unsetCursor()
            return
        if not self.tiles:
            self.caption.hide()
            return
        if self.zoom > 1.001:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        index = self._tile_at(event.position().toPoint())
        if index is None:
            self.caption.hide()
            if self.zoom <= 1.001:
                self.unsetCursor()
            return
        photo = self.photos[index]
        self.caption.setText(f"{os.path.basename(photo.path)}    {photo.width}×{photo.height}")
        self.caption.adjustSize()
        point = event.position().toPoint() + QPoint(14, 18)
        if point.x() + self.caption.width() > self.width() - 8:
            point.setX(max(8, self.width() - self.caption.width() - 8))
        if point.y() + self.caption.height() > self.height() - 8:
            point.setY(max(8, point.y() - self.caption.height() - 28))
        self.caption.move(point)
        self.caption.show()
        if self.zoom <= 1.001:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            self._press_pos = None
            was_panning = self._panning
            self._panning = False
            if self.mouseGrabber() is self:
                self.releaseMouse()
            if was_panning:
                self._update_cursor()
                self._poke_decode()
                return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        if self.focus_index is not None:
            self._wheel_focus(event)
            return
        if not self.tiles:
            event.ignore()
            return
        if time.monotonic() - self._native_zoom_at < 0.12:
            event.accept()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                self._set_zoom(self.zoom * (1.12 if delta > 0 else 1 / 1.12))
            event.accept()
            return
        if self.zoom > 1.001:
            pixel = event.pixelDelta()
            if not pixel.isNull():
                self.pan_x += pixel.x()
                self.pan_y += pixel.y()
            else:
                angle = event.angleDelta()
                self.pan_x += angle.x() * 0.4
                self.pan_y += angle.y() * 0.4
            self._clamp_pan()
            self._poke_decode()
            self.update()
            event.accept()
            return
        event.ignore()

    def leaveEvent(self, event) -> None:
        if self._panning:
            return
        self.caption.hide()
        self.unsetCursor()
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._panning:
            return
        if self.focus_index is not None:
            self.clear_focus()
            return
        index = self._tile_at(event.position().toPoint())
        if index is None:
            return
        self.focus_index = index
        self.focus_zoom = 1.0
        self.focus_pan_x = 0.0
        self.focus_pan_y = 0.0
        self.caption.hide()
        self._request_decodes()
        self._emit_status()
        self.update()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._drag_over = True
            self.update()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._drag_over = False
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._drag_over = False
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.filesDropped.emit(paths)
            event.acceptProposedAction()
        self.update()

    def _relayout(self, *, debounce_decode: bool) -> None:
        if not self.photos:
            return
        width, height = self.width(), self.height()
        if width < 2 or height < 2:
            return
        if self.zoom > 1.001:
            self._clamp_pan()
            self._emit_status()
            if debounce_decode:
                self._poke_decode()
            else:
                self._request_decodes()
            self.update()
            return
        key = (width, height, round(self.devicePixelRatioF() * 1000), self._generation)
        if key == self._layout_key:
            return
        self._layout_key = key
        self._prepared.clear()
        result = compute_layout(
            [photo.width / photo.height for photo in self.photos],
            width,
            height,
            self._cache,
        )
        self.tiles = result.tiles
        self.crop = result.crop
        self.filled = result.filled
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._emit_status()
        if debounce_decode:
            self._poke_decode()
        else:
            self._request_decodes()
        self.update()

    def _request_decodes(self) -> None:
        if not self.photos or not self.tiles:
            return
        dpr = self.devicePixelRatioF() or 1.0
        budget = max(1, QThreadPool.globalInstance().maxThreadCount())
        zoomed = self.zoom > 1.05
        margin = 280 if zoomed else 8
        view_cx = self.width() / 2
        view_cy = self.height() / 2
        fresh: list[tuple] = []
        upgrade: list[tuple] = []
        for tile in self.tiles:
            index = tile.index
            if index in self._give_up or tile.w <= 0 or tile.h <= 0 or index >= len(self.photos):
                continue
            sx, sy, sw, sh = self._screen_rect(tile)
            on_screen = not (
                sx + sw < -margin
                or sy + sh < -margin
                or sx > self.width() + margin
                or sy > self.height() + margin
            )
            if self.focus_index != index and not on_screen:
                continue
            photo = self.photos[index]
            scale = 1.35 if zoomed and self.focus_index != index else 1.0
            want_w, want_h = fit_decode_size(
                photo.width,
                photo.height,
                max(1, int(round(sw * scale))),
                max(1, int(round(sh * scale))),
                dpr,
            )
            if self.focus_index == index:
                _bx, _by, bw, bh = contain_rect(
                    photo.width, photo.height, self.width(), self.height(), dpr
                )
                headroom = 1.35 if self.focus_zoom > 1.05 else 1.0
                fit_w, fit_h = fit_decode_size(
                    photo.width,
                    photo.height,
                    max(1, int(round(bw * self.focus_zoom * headroom))),
                    max(1, int(round(bh * self.focus_zoom * headroom))),
                    dpr,
                )
                if fit_w * fit_h > want_w * want_h:
                    want_w, want_h = fit_w, fit_h
            if _frame_is_enough(self.frames.get(index), want_w, want_h, photo.width, photo.height):
                continue
            if self.focus_index == index:
                priority = (-1.0, 0.0, index)
            elif zoomed:
                dx = sx + sw / 2 - view_cx
                dy = sy + sh / 2 - view_cy
                priority = (dx * dx + dy * dy, float(tile.y), index)
            else:
                priority = (float(tile.y), float(tile.x), index)
            if index in self._inflight:
                have_w, have_h = self._inflight_size.get(index, (0, 0))
                if have_w * have_h >= want_w * want_h * 0.9:
                    continue
                if index not in self.frames:
                    continue
                upgrade.append((priority, index, want_w, want_h))
            else:
                fresh.append((priority, index, want_w, want_h))
        if not fresh and not upgrade:
            return
        fresh.sort()
        upgrade.sort()
        room = budget - len(self._inflight)
        for _priority, index, want_w, want_h in fresh:
            if room <= 0:
                break
            self._start_decode(index, want_w, want_h)
            room -= 1
        for _priority, index, want_w, want_h in upgrade[:4]:
            self._start_decode(index, want_w, want_h)

    def _start_decode(self, index: int, want_w: int, want_h: int) -> None:
        photo = self.photos[index]
        token = self._tokens.get(index, 0) + 1
        self._tokens[index] = token
        self._inflight.add(index)
        self._inflight_size[index] = (want_w, want_h)
        QThreadPool.globalInstance().start(
            _DecodeJob(
                self._generation,
                index,
                token,
                photo.path,
                want_w,
                want_h,
                self._signals,
            )
        )

    def _on_scan_progress(self, generation: int, done: int, total: int) -> None:
        if generation != self._generation:
            return
        self._loading_text = f"正在读取 {done}/{total}"
        self._emit_status()
        self.update()

    def _on_scan_done(self, generation: int, photos: list, failed: int) -> None:
        if generation != self._generation:
            return
        self._loading = False
        self.failed_probe = failed
        self.photos = [Photo(path, width, height) for path, width, height in photos]
        self.countChanged.emit(len(self.photos))
        self._layout_key = None
        if self.photos:
            self._relayout(debounce_decode=False)
        else:
            self._emit_status()
            self.update()

    def _on_decoded(self, generation: int, index: int, token: int, image: QImage) -> None:
        if generation != self._generation or token != self._tokens.get(index):
            return
        self._inflight.discard(index)
        self._inflight_size.pop(index, None)
        if image.isNull():
            self._give_up.add(index)
            if index not in self.frames:
                self.failed_decode += 1
        else:
            self.frames[index] = image
            self._pixmaps[index] = QPixmap.fromImage(image)
            self._prepared.pop(index, None)
        self._request_decodes()
        self._schedule_refresh(status=True)

    def _poke_decode(self) -> None:
        self._decode_pending = True
        if not self._decode_timer.isActive():
            self._decode_timer.start(40)

    def _on_interaction_settled(self) -> None:
        again = self._decode_pending
        self._decode_pending = False
        self._zoom_settled = not again
        self._request_decodes()
        if again:
            self._decode_timer.start(40)

    def _schedule_refresh(self, *, status: bool = False) -> None:
        self._refresh_status = self._refresh_status or status
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _flush_refresh(self) -> None:
        if self._refresh_status:
            self._emit_status()
        self._refresh_status = False
        self.update()

    def _set_zoom(self, new_zoom: float) -> None:
        new_zoom = min(MAX_ZOOM, max(1.0, new_zoom))
        if new_zoom <= 1.0:
            if self.zoom > 1.0 or abs(self.pan_x) > 0.01 or abs(self.pan_y) > 0.01:
                self.reset_zoom()
            return
        if abs(new_zoom - self.zoom) < 1e-4:
            return
        self.pan_x, self.pan_y = zoom_about_center(
            self.pan_x,
            self.pan_y,
            max(self.zoom, 1e-6),
            self.width(),
            self.height(),
            new_zoom,
        )
        self.zoom = new_zoom
        self._clamp_pan()
        self._zoom_settled = False
        self._poke_decode()
        self._update_cursor()
        self._schedule_refresh(status=True)
        self.update()

    def _set_focus_zoom(self, new_zoom: float) -> None:
        new_zoom = min(MAX_ZOOM, max(1.0, new_zoom))
        if new_zoom <= 1.0:
            self.focus_zoom = 1.0
            self.focus_pan_x = 0.0
            self.focus_pan_y = 0.0
        elif abs(new_zoom - self.focus_zoom) < 1e-4:
            return
        else:
            self.focus_pan_x, self.focus_pan_y = zoom_about_center(
                self.focus_pan_x,
                self.focus_pan_y,
                max(self.focus_zoom, 1e-6),
                self.width(),
                self.height(),
                new_zoom,
            )
            self.focus_zoom = new_zoom
            self._clamp_focus_pan()
        self._poke_decode()
        self._update_cursor()
        self._schedule_refresh(status=True)
        self.update()

    def _clamp_focus_pan(self) -> None:
        if self.focus_index is None:
            return
        bx, by, bw, bh = self._focus_fit()
        zoom = self.focus_zoom
        view_w = float(self.width())
        view_h = float(self.height())
        content_w = bw * zoom
        content_h = bh * zoom
        if content_w <= view_w:
            self.focus_pan_x = (view_w - content_w) / 2 - bx * zoom
        else:
            self.focus_pan_x = min(self.focus_pan_x, -bx * zoom)
            self.focus_pan_x = max(self.focus_pan_x, view_w - (bx + bw) * zoom)
        if content_h <= view_h:
            self.focus_pan_y = (view_h - content_h) / 2 - by * zoom
        else:
            self.focus_pan_y = min(self.focus_pan_y, -by * zoom)
            self.focus_pan_y = max(self.focus_pan_y, view_h - (by + bh) * zoom)

    def _focus_fit(self) -> tuple[float, float, float, float]:
        photo = self.photos[self.focus_index]
        return contain_rect(
            photo.width,
            photo.height,
            self.width(),
            self.height(),
            self.devicePixelRatioF() or 1.0,
        )

    def _can_pan(self) -> bool:
        if self.focus_index is not None:
            return self.focus_zoom > 1.001
        return bool(self.tiles) and self.zoom > 1.001

    def _wheel_focus(self, event) -> None:
        if time.monotonic() - self._native_zoom_at < 0.12:
            event.accept()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                self._set_focus_zoom(self.focus_zoom * (1.12 if delta > 0 else 1 / 1.12))
            event.accept()
            return
        if self.focus_zoom > 1.001:
            pixel = event.pixelDelta()
            if not pixel.isNull():
                self.focus_pan_x += pixel.x()
                self.focus_pan_y += pixel.y()
            else:
                angle = event.angleDelta()
                self.focus_pan_x += angle.x() * 0.4
                self.focus_pan_y += angle.y() * 0.4
            self._clamp_focus_pan()
            self._poke_decode()
            self.update()
            event.accept()
            return
        event.ignore()

    def _clamp_pan(self) -> None:
        self.pan_x, self.pan_y = clamp_pan(
            self.pan_x,
            self.pan_y,
            self.zoom,
            self.width(),
            self.height(),
            content_bounds(self.tiles),
        )

    def _screen_rect(self, tile) -> tuple[float, float, float, float]:
        return (
            tile.x * self.zoom + self.pan_x,
            tile.y * self.zoom + self.pan_y,
            tile.w * self.zoom,
            tile.h * self.zoom,
        )

    def _tile_on_screen(self, tile, margin: float) -> bool:
        sx, sy, sw, sh = self._screen_rect(tile)
        if sw <= 0 or sh <= 0:
            return False
        return not (
            sx + sw < -margin
            or sy + sh < -margin
            or sx > self.width() + margin
            or sy > self.height() + margin
        )

    def _handle_native_gesture(self, event: QNativeGestureEvent) -> bool:
        if event.gestureType() != Qt.NativeGestureType.ZoomNativeGesture:
            return False
        event.accept()
        if self.focus_index is not None:
            factor = zoom_factor_from_magnification(float(event.value()))
            if factor is not None:
                self._native_zoom_at = time.monotonic()
                self._set_focus_zoom(self.focus_zoom * factor)
            return True
        if not self.tiles:
            return True
        factor = zoom_factor_from_magnification(float(event.value()))
        if factor is None:
            return True
        self._native_zoom_at = time.monotonic()
        self._set_zoom(self.zoom * factor)
        return True

    def _update_cursor(self) -> None:
        if self._panning:
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self.focus_index is not None and self.focus_zoom > 1.001:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif self.zoom > 1.001 and self.focus_index is None and self.tiles:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()

    def _tile_at(self, point: QPoint) -> int | None:
        zoom = self.zoom or 1.0
        x = (point.x() - self.pan_x) / zoom
        y = (point.y() - self.pan_y) / zoom
        for tile in self.tiles:
            if tile.x <= x < tile.x + tile.w and tile.y <= y < tile.y + tile.h:
                return tile.index
        return None

    def _emit_status(self) -> None:
        if self._loading:
            text = self._loading_text or "正在读取图片…"
        elif self.focus_index is not None and self.photos:
            photo = self.photos[self.focus_index]
            if self.focus_zoom > 1.001:
                text = (
                    f"{os.path.basename(photo.path)}    {photo.width}×{photo.height}"
                    f"    {round(self.focus_zoom * 100)}%    拖动查看    Esc 返回"
                )
            else:
                text = (
                    f"{os.path.basename(photo.path)}    {photo.width}×{photo.height}"
                    "    左右方向键切换    双指或 ⌘+ 放大    Esc 返回"
                )
        elif not self.photos:
            text = "拖入图片或文件夹" if not self.failed_probe else "没有可以显示的图片"
        else:
            count = len(self.photos)
            loaded = len(self.frames)
            zoomed = self.zoom > 1.001
            if loaded < count:
                text = f"正在载入 {loaded}/{count}"
                if zoomed:
                    text += f" · {round(self.zoom * 100)}%"
            elif zoomed:
                text = f"{count} 张 · 完整显示 · {round(self.zoom * 100)}% · 拖动查看"
            elif self.filled:
                text = f"{count} 张 · 完整显示 · 已铺满窗口"
            else:
                text = f"{count} 张 · 完整显示 · 已尽量放大"
            failed = self.failed_probe + self.failed_decode
            if failed:
                text += f" · {failed} 张未能打开"
        self.statusChanged.emit(text)

    def _paint_focus(self, painter: QPainter) -> None:
        painter.fillRect(self.rect(), BACKGROUND)
        photo = self.photos[self.focus_index]
        frame = self.frames.get(self.focus_index)
        if frame is None or frame.isNull():
            painter.setPen(QColor("#e8e8e8"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "正在打开原图…")
            return
        bx, by, bw, bh = self._focus_fit()
        x = bx * self.focus_zoom + self.focus_pan_x
        y = by * self.focus_zoom + self.focus_pan_y
        width = bw * self.focus_zoom
        height = bh * self.focus_zoom
        dpr = self.devicePixelRatioF() or 1.0
        pixel_w = max(1, int(round(width * dpr)))
        pixel_h = max(1, int(round(height * dpr)))
        pixmap = self._pixmaps.get(self.focus_index)
        source_w = pixmap.width() if pixmap is not None and not pixmap.isNull() else frame.width()
        source_h = pixmap.height() if pixmap is not None and not pixmap.isNull() else frame.height()
        sharp = (
            source_w > 0
            and source_h > 0
            and abs(source_w / pixel_w - 1.0) <= 0.08
            and abs(source_h / pixel_h - 1.0) <= 0.08
        )
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, not sharp)
        if pixmap is not None and not pixmap.isNull():
            painter.drawPixmap(
                QRectF(x, y, width, height),
                pixmap,
                QRectF(0, 0, pixmap.width(), pixmap.height()),
            )
        else:
            painter.drawImage(QRectF(x, y, width, height), frame)
        if self.focus_zoom > 1.001:
            hint = (
                f"{os.path.basename(photo.path)}    {round(self.focus_zoom * 100)}%"
                "    拖动查看    Esc 或双击返回"
            )
        else:
            hint = (
                f"{os.path.basename(photo.path)}    {photo.width}×{photo.height}"
                "    双指或 ⌘+ 放大    Esc 或双击返回"
            )
        self._paint_hint(painter, hint)

    def _paint_hint(self, painter: QPainter, text: str) -> None:
        font = painter.font()
        font.setPixelSize(13)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        width = metrics.horizontalAdvance(text) + 22
        height = metrics.height() + 12
        x = max(8, (self.width() - width) // 2)
        y = self.height() - height - 16
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawRoundedRect(x, y, width, height, 8, 8)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(QRect(x, y, width, height), Qt.AlignmentFlag.AlignCenter, text)

    def _paint_message(self, painter: QPainter, title: str, lines: list[str]) -> None:
        title_font = QFont(painter.font())
        title_font.setPixelSize(28)
        title_font.setWeight(QFont.Weight.DemiBold)
        body_font = QFont(painter.font())
        body_font.setPixelSize(14)
        title_metrics = QFontMetrics(title_font)
        body_metrics = QFontMetrics(body_font)
        line_height = body_metrics.height() + 8
        block_height = title_metrics.height() + 22 + line_height * len(lines)
        top = max(16, (self.height() - block_height) // 2)
        painter.setPen(QColor("#f5f5f5"))
        painter.setFont(title_font)
        painter.drawText(
            QRect(24, top, self.width() - 48, title_metrics.height() + 4),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            title,
        )
        painter.setPen(QColor("#bdbdbd"))
        painter.setFont(body_font)
        y = top + title_metrics.height() + 22
        for line in lines:
            painter.drawText(
                QRect(24, y, self.width() - 48, line_height),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                line,
            )
            y += line_height


def _frame_is_enough(
    image: QImage | None,
    want_w: int,
    want_h: int,
    src_w: int,
    src_h: int,
) -> bool:
    if image is None or image.isNull():
        return False
    if image.width() >= src_w - 1 and image.height() >= src_h - 1:
        return True
    return image.width() >= want_w - 1 and image.height() >= want_h - 1


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(_STYLE)
        self.setWindowTitle("图片预览")
        self.wall = WallView()
        self.setCentralWidget(self.wall)
        self.wall.statusChanged.connect(self._set_status)
        self.wall.countChanged.connect(self._set_count)
        self.wall.filesDropped.connect(lambda paths: self.load_paths(paths, sort=True))
        self._settings = QSettings("photoproject", "PhotoWall")
        self._last_paths: list[str] = []
        self._last_sort = True
        self._build_actions()
        self._build_toolbar()
        self._build_menu()
        status = QStatusBar()
        status.setSizeGripEnabled(False)
        self.setStatusBar(status)
        self._set_status("拖入图片或文件夹")
        self.resize(1280, 820)
        self.setMinimumSize(860, 560)

    def load_paths(self, paths: list[str], *, sort: bool, recursive: bool | None = None) -> None:
        if recursive is None:
            if any(os.path.isdir(path) for path in paths):
                recursive = self._ask_include_subfolders()
                if recursive is None:
                    return
            else:
                recursive = False
        self.wall.include_subfolders = recursive
        self._last_paths = list(paths)
        self._last_sort = sort
        files = collect_images(paths, sort=sort, recursive=recursive)
        if not files:
            if recursive:
                detail = "子文件夹里也没有找到可以显示的图片。"
            else:
                detail = "这一层没有可以显示的图片。"
            QMessageBox.information(self, "没有图片", detail)
            return
        self._remember(paths[0])
        self._set_title(files)
        self.wall.set_files(files)

    def choose_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片",
            self._start_dir(),
            file_dialog_filter(),
        )
        if files:
            self.load_paths(files, sort=False)

    def choose_folder(self) -> None:
        recursive = self._ask_include_subfolders()
        if recursive is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择文件夹",
            self._start_dir(),
            QFileDialog.Option.ShowDirsOnly,
        )
        if folder:
            self.load_paths([folder], sort=True, recursive=recursive)

    def _ask_include_subfolders(self) -> bool | None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("包含子文件夹")
        box.setText("要一并打开子文件夹里的照片吗？")
        yes = box.addButton("包含", QMessageBox.ButtonRole.YesRole)
        no = box.addButton("只看这一层", QMessageBox.ButtonRole.NoRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)
        box.exec()
        clicked = box.clickedButton()
        if clicked is yes:
            return True
        if clicked is no:
            return False
        return None

    def clear_wall(self) -> None:
        self.wall.set_files([])
        self.wall._loading = False
        self.setWindowTitle("图片预览")
        self._set_count(0)
        self._set_status("拖入图片或文件夹")
        self.wall.update()

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def handle_escape(self) -> None:
        if self.wall.focus_index is not None:
            if self.wall.focus_zoom > 1.001:
                self.wall.reset_zoom()
                return
            self.wall.clear_focus()
            return
        if self.wall.zoom > 1.001:
            self.wall.reset_zoom()
            return
        if self.isFullScreen():
            self.showNormal()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            fullscreen = self.isFullScreen()
            self.full_action.setText("退出全屏" if fullscreen else "进入全屏")
            self.toolbar.setVisible(not fullscreen)
            self.statusBar().setVisible(not fullscreen)

    def closeEvent(self, event) -> None:
        self.wall.shutdown()
        pool = QThreadPool.globalInstance()
        pool.clear()
        pool.waitForDone(2000)
        super().closeEvent(event)

    def _build_actions(self) -> None:
        self.open_files_action = QAction("选择图片…", self)
        self.open_files_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_files_action.triggered.connect(self.choose_files)

        self.open_folder_action = QAction("选择文件夹…", self)
        self.open_folder_action.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.open_folder_action.triggered.connect(self.choose_folder)

        self.clear_action = QAction("清空", self)
        self.clear_action.setShortcut(QKeySequence("Ctrl+Backspace"))
        self.clear_action.triggered.connect(self.clear_wall)

        self.zoom_in_action = QAction("放大", self)
        self.zoom_in_action.setShortcuts(
            [QKeySequence.StandardKey.ZoomIn, QKeySequence("Ctrl+=")]
        )
        self.zoom_in_action.setToolTip("朝画面中心放大")
        self.zoom_in_action.triggered.connect(self.wall.zoom_in)

        self.zoom_out_action = QAction("缩小", self)
        self.zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self.zoom_out_action.setToolTip("朝画面中心缩小")
        self.zoom_out_action.triggered.connect(self.wall.zoom_out)

        self.zoom_reset_action = QAction("重置缩放", self)
        self.zoom_reset_action.setShortcut(QKeySequence("Ctrl+0"))
        self.zoom_reset_action.setToolTip("回到完整照片墙")
        self.zoom_reset_action.triggered.connect(self.wall.reset_zoom)

        self.full_action = QAction("进入全屏", self)
        self.full_action.setShortcut(QKeySequence.StandardKey.FullScreen)
        self.full_action.triggered.connect(self.toggle_fullscreen)

        self.quit_action = QAction("退出", self)
        self.quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        self.quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.quit_action.triggered.connect(self.close)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.handle_escape)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, lambda: self.wall.step_focus(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, lambda: self.wall.step_focus(1))

    def _build_toolbar(self) -> None:
        self.toolbar = QToolBar()
        self.toolbar.setMovable(False)
        self.toolbar.setFloatable(False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toolbar.addAction(self.open_files_action)
        self.toolbar.addAction(self.open_folder_action)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.toolbar.addWidget(spacer)
        self.count_label = QLabel("未选择")
        self.count_label.setObjectName("count")
        self.toolbar.addWidget(self.count_label)
        self.toolbar.addAction(self.clear_action)
        self.toolbar.addAction(self.full_action)
        self.addToolBar(self.toolbar)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        file_menu.addAction(self.open_files_action)
        file_menu.addAction(self.open_folder_action)
        file_menu.addSeparator()
        file_menu.addAction(self.clear_action)
        file_menu.addSeparator()
        file_menu.addAction(self.quit_action)
        view_menu = self.menuBar().addMenu("视图")
        view_menu.addAction(self.zoom_in_action)
        view_menu.addAction(self.zoom_out_action)
        view_menu.addAction(self.zoom_reset_action)
        view_menu.addSeparator()
        view_menu.addAction(self.full_action)

    def _set_status(self, text: str) -> None:
        self.statusBar().showMessage(text)

    def _set_count(self, count: int) -> None:
        self.count_label.setText("未选择" if count <= 0 else f"{count} 张")

    def _set_title(self, files: list[str]) -> None:
        parents = {os.path.dirname(path) for path in files}
        if len(parents) == 1:
            name = os.path.basename(next(iter(parents))) or "图片"
            self.setWindowTitle(f"图片预览 — {name}")
        else:
            self.setWindowTitle(f"图片预览 — {len(files)} 张")

    def _start_dir(self) -> str:
        last = self._settings.value("lastDir", "")
        if isinstance(last, str) and os.path.isdir(last):
            return last
        pictures = os.path.expanduser("~/Pictures")
        if os.path.isdir(pictures):
            return pictures
        return os.path.expanduser("~")

    def _remember(self, path: str) -> None:
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if folder:
            self._settings.setValue("lastDir", folder)
