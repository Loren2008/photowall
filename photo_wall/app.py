"""启动图片预览窗口。"""

from __future__ import annotations

import argparse
import os
import sys

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication

from photo_wall.decode import configure_reader_limits
from photo_wall.window import MainWindow


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    parser = argparse.ArgumentParser(description="把多张图片无缝铺满窗口")
    parser.add_argument("paths", nargs="*", help="图片文件或文件夹")
    args = parser.parse_args(argv[1:])

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(argv[:1])
    app.setStyle("Fusion")
    font = QFont()
    font.setFamilies(["PingFang SC", "Helvetica Neue", "Arial"])
    font.setPointSize(13)
    app.setFont(font)
    app.setApplicationName("图片预览")
    app.setApplicationDisplayName("图片预览")
    app.setOrganizationName("photoproject")
    configure_reader_limits()
    # 读图主要在等磁盘，线程再多只会互相抢。8 个足够喂饱解码。
    QThreadPool.globalInstance().setMaxThreadCount(min(8, max(4, os.cpu_count() or 4)))

    window = MainWindow()
    window.show()
    if args.paths:
        window.load_paths(args.paths, sort=True)
    return app.exec()

