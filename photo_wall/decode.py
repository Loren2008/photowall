"""读取图片尺寸和像素。Qt 打不开的格式交给 macOS 的 sips。"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
import threading

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPainter

_LOCK = threading.Lock()
_CONVERTED: dict[str, str] = {}
_TMPDIR: str | None = None
# 这些后缀不用再猜文件格式，少读一遍文件头。
_KNOWN_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
}


def configure_reader_limits() -> None:
    QImageReader.setAllocationLimit(1024)


def probe_size(path: str) -> tuple[int, int] | None:
    size = _qt_size(path)
    if size is not None:
        return size
    return _sips_size(path)


def load_frame(path: str, out_w: int, out_h: int) -> QImage | None:
    """解码整张图，最长边不超过显示所需，且不会比原图更大。"""

    image = _read(path, out_w, out_h)
    if image is None:
        converted = _convert(path)
        if converted:
            image = _read(converted, out_w, out_h)
    if image is None or image.isNull():
        return None
    return _flatten(image)


def _open_reader(path: str) -> QImageReader:
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    reader.setAllocationLimit(1024)
    if os.path.splitext(path)[1].lower() not in _KNOWN_SUFFIXES:
        reader.setDecideFormatFromContent(True)
    return reader


def _qt_size(path: str) -> tuple[int, int] | None:
    reader = _open_reader(path)
    if not reader.canRead():
        return None
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        return size.width(), size.height()
    return None


def _read(path: str, out_w: int, out_h: int) -> QImage | None:
    reader = _open_reader(path)
    if not reader.canRead():
        return None
    source = reader.size()
    out_w = max(1, int(out_w))
    out_h = max(1, int(out_h))
    if source.isValid() and source.width() > out_w and source.height() > out_h:
        reader.setScaledSize(QSize(out_w, out_h))
    image = reader.read()
    if image.isNull():
        return None
    if source.isValid() and source.width() > 0 and source.height() > 0:
        expected = source.width() / source.height()
        actual = image.width() / max(1, image.height())
        if expected > 0 and abs(actual - expected) / expected > 0.04:
            retry = _open_reader(path)
            image = retry.read()
            if image.isNull():
                return None
    if image.width() > out_w + 1 or image.height() > out_h + 1:
        image = image.scaled(
            out_w,
            out_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image


def _flatten(image: QImage) -> QImage:
    if image.format() == QImage.Format.Format_RGB32 and not image.hasAlphaChannel():
        return image
    flat = QImage(image.size(), QImage.Format.Format_RGB32)
    flat.fill(Qt.GlobalColor.black)
    painter = QPainter(flat)
    painter.drawImage(0, 0, image)
    painter.end()
    return flat


def _sips_size(path: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", "-g", "orientation", path],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    width = height = None
    orientation = 1
    for line in result.stdout.splitlines():
        lower = line.lower()
        if "pixelwidth" in lower:
            width = _last_int(line)
        elif "pixelheight" in lower:
            height = _last_int(line)
        elif "orientation" in lower:
            number = _last_int(line)
            if number:
                orientation = number
    if not width or not height:
        return None
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    return width, height


def _convert(path: str) -> str | None:
    with _LOCK:
        cached = _CONVERTED.get(path)
        if cached and os.path.exists(cached):
            return cached
        dest = os.path.join(_temp_dir(), f"{len(_CONVERTED)}.jpg")
        try:
            result = subprocess.run(
                [
                    "sips",
                    "-s",
                    "format",
                    "jpeg",
                    "-s",
                    "formatOptions",
                    "100",
                    path,
                    "--out",
                    dest,
                ],
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            return None
        if result.returncode != 0 or not os.path.exists(dest):
            return None
        _CONVERTED[path] = dest
        return dest


def _temp_dir() -> str:
    global _TMPDIR
    if _TMPDIR is None:
        _TMPDIR = tempfile.mkdtemp(prefix="photowall-")
        atexit.register(lambda: shutil.rmtree(_TMPDIR, ignore_errors=True))
    return _TMPDIR


def _last_int(line: str) -> int | None:
    token = line.split(":")[-1].strip()
    digits = "".join(character for character in token if character.isdigit())
    if not digits:
        return None
    return int(digits)
