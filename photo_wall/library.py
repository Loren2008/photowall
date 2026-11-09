"""从用户选中的文件或文件夹里收集图片。"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
}

_NUMBER_SPLIT = re.compile(r"(\d+)")


def file_dialog_filter() -> str:
    patterns = " ".join("*" + extension for extension in sorted(IMAGE_EXTENSIONS))
    return f"图片 ({patterns})"


def natural_key(path: str) -> list:
    name = os.path.basename(path).casefold()
    return _natural_name_key(name)


def natural_path_key(path: str) -> list:
    return [_natural_name_key(part.casefold()) for part in os.path.normpath(path).split(os.sep)]


def _natural_name_key(name: str) -> list:
    parts = _NUMBER_SPLIT.split(name)
    key: list = []
    for index, part in enumerate(parts):
        if index % 2:
            key.append(int(part))
        else:
            key.append(part)
    return key


def is_image_file(path: str) -> bool:
    name = os.path.basename(path)
    if not name or name.startswith(".") or name.startswith("._"):
        return False
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def images_in_folder(folder: str, *, recursive: bool = False) -> list[str]:
    """读取文件夹中的图片。recursive 为真时会进入所有子文件夹。"""

    if recursive:
        return _images_recursive(folder)
    found: list[str] = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False) and is_image_file(entry.name):
                found.append(entry.path)
        except OSError:
            continue
    found.sort(key=natural_path_key)
    return found


def _images_recursive(folder: str) -> list[str]:
    found: list[str] = []
    seen_dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        real_dir = os.path.realpath(dirpath)
        if real_dir in seen_dirs:
            dirnames.clear()
            continue
        seen_dirs.add(real_dir)
        dirnames[:] = [name for name in dirnames if _is_visible_dir(name)]
        for name in filenames:
            if is_image_file(name):
                found.append(os.path.join(dirpath, name))
    found.sort(key=natural_path_key)
    return found


def _is_visible_dir(name: str) -> bool:
    return bool(name) and not name.startswith(".") and not name.startswith("._")


def collect_images(paths: Iterable[str], *, sort: bool = True, recursive: bool = False) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = os.path.abspath(os.path.expanduser(raw))
        if os.path.isdir(path):
            candidates = images_in_folder(path, recursive=recursive)
        elif os.path.isfile(path) and is_image_file(path):
            candidates = [path]
        else:
            candidates = []
        for candidate in candidates:
            marker = os.path.normcase(os.path.normpath(candidate))
            if marker in seen:
                continue
            seen.add(marker)
            found.append(candidate)
    if sort:
        found.sort(key=natural_path_key)
    return found
