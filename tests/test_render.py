import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from photo_wall.decode import load_frame, probe_size
from photo_wall.paint import BACKGROUND, render_wall
from photo_wall.window import MainWindow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESOURCE = os.path.join(ROOT, "resource")


def _solid(width: int, height: int, color: QColor) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(color)
    return image


def _pixel(image: QImage, x: int, y: int) -> tuple[int, int, int]:
    color = image.pixelColor(x, y)
    return color.red(), color.green(), color.blue()


def _count_background(image: QImage) -> int:
    target = (BACKGROUND.red(), BACKGROUND.green(), BACKGROUND.blue())
    count = 0
    for y in range(image.height()):
        for x in range(image.width()):
            if _pixel(image, x, y) == target:
                count += 1
    return count


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_four_squares_have_no_gaps_or_wrong_colors(self):
        colors = [
            QColor("#e23d3d"),
            QColor("#2f9e44"),
            QColor("#1c7ed6"),
            QColor("#f08c00"),
        ]
        frames = [_solid(80, 80, color) for color in colors]
        canvas, tiles = render_wall(frames, [1, 1, 1, 1], 200, 200, dpr=1)
        self.assertEqual(canvas.width(), 200)
        self.assertEqual(_count_background(canvas), 0)
        for tile, color in zip(sorted(tiles, key=lambda item: item.index), colors):
            red, green, blue = _pixel(
                canvas,
                tile.x + tile.w // 2,
                tile.y + tile.h // 2,
            )
            self.assertEqual((red, green, blue), (color.red(), color.green(), color.blue()))

    def test_tiles_keep_the_outer_edge_of_each_photo(self):
        frames = []
        aspects = []
        for width, height in ((360, 240), (240, 400), (480, 200), (280, 280)):
            image = _solid(width, height, QColor("#ff2d2d"))
            painter = QPainter(image)
            margin_x = width // 5
            margin_y = height // 5
            painter.fillRect(
                margin_x,
                margin_y,
                width - 2 * margin_x,
                height - 2 * margin_y,
                QColor("#245bff"),
            )
            painter.end()
            frames.append(image)
            aspects.append(width / height)
        canvas, tiles = render_wall(frames, aspects, 1100, 720, dpr=1)
        for tile in tiles:
            edge = _pixel(canvas, tile.x + 2, tile.y + tile.h // 2)
            center = _pixel(canvas, tile.x + tile.w // 2, tile.y + tile.h // 2)
            self.assertGreater(edge[0], 180, tile)
            self.assertLess(edge[2], 80, tile)
            self.assertGreater(center[2], 180, tile)
            self.assertLess(center[0], 80, tile)

    def test_hidpi_canvas_uses_device_pixels(self):
        color = QColor("#d9480f")
        canvas, tiles = render_wall([_solid(40, 80, color)], [0.5], 300, 200, dpr=2)
        self.assertEqual((canvas.width(), canvas.height()), (600, 400))
        self.assertEqual(len(tiles), 1)
        sample = _pixel(canvas, canvas.width() // 2, canvas.height() // 2)
        self.assertEqual(sample, (color.red(), color.green(), color.blue()))

    def test_real_photos_decode_at_display_size(self):
        if not os.path.isdir(RESOURCE):
            self.skipTest("local photos are not part of the repository")
        paths = [
            os.path.join(RESOURCE, name)
            for name in os.listdir(RESOURCE)
            if name.lower().endswith(".png")
        ]
        if len(paths) < 2:
            self.skipTest("local photos are not part of the repository")
        frames = []
        aspects = []
        for path in sorted(paths):
            width, height = probe_size(path)
            self.assertGreater(width, 0)
            image = load_frame(path, 640, 640)
            self.assertIsNotNone(image)
            self.assertFalse(image.isNull())
            self.assertLessEqual(max(image.width(), image.height()), 640)
            frames.append(image)
            aspects.append(width / height)
        canvas, tiles = render_wall(frames, aspects, 900, 600, dpr=2)
        self.assertEqual(len(tiles), len(frames))
        self.assertEqual((canvas.width(), canvas.height()), (1800, 1200))
        preview = os.path.join(ROOT, ".preview")
        os.makedirs(preview, exist_ok=True)
        self.assertTrue(canvas.save(os.path.join(preview, "resource_wall.png")))

    def test_window_loads_a_folder(self):
        if not os.path.isdir(RESOURCE):
            self.skipTest("local photos are not part of the repository")
        window = MainWindow()
        window.resize(1100, 760)
        window.show()
        window.load_paths([RESOURCE], sort=True, recursive=False)
        pool = QThreadPool.globalInstance()
        deadline = time.time() + 15
        while time.time() < deadline and not window.wall.is_settled():
            self.app.processEvents()
            if not pool.activeThreadCount() and window.wall.photos and not window.wall._inflight:
                self.app.processEvents()
                break
            time.sleep(0.02)
        pool.waitForDone(8000)
        self.app.processEvents()
        self.assertGreaterEqual(len(window.photos if False else window.wall.photos), 2)
        self.assertGreaterEqual(len(window.wall.frames), 2)
        self.assertTrue(window.wall.tiles)
        area = sum(tile.w * tile.h for tile in window.wall.tiles)
        self.assertGreater(area, window.wall.width() * window.wall.height() * 0.5)
        window.close()


class SharpnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_downscale_keeps_a_hard_edge(self):
        source = QImage(400, 200, QImage.Format.Format_RGB32)
        source.fill(QColor("#111111"))
        painter = QPainter(source)
        painter.fillRect(0, 0, 200, 200, QColor("#f8f9fa"))
        painter.end()
        from photo_wall.paint import fit_image

        covered = fit_image(source, 200, 100)
        left = _pixel(covered, 40, 50)
        right = _pixel(covered, 160, 50)
        self.assertGreater(left[0], 200)
        self.assertLess(right[0], 40)


if __name__ == "__main__":
    unittest.main()
