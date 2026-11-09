import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from photo_wall.layout import Tile
from photo_wall.viewport import (
    clamp_pan,
    content_bounds,
    zoom_about_center,
    zoom_factor_from_magnification,
)
from photo_wall.window import MainWindow, Photo, WallView


class ViewportMathTests(unittest.TestCase):
    def test_zoom_keeps_the_point_under_the_center(self):
        pan_x, pan_y = zoom_about_center(0, 0, 1, 800, 600, 2)
        self.assertEqual((pan_x, pan_y), (-400, -300))
        self.assertEqual((400 - pan_x) / 2, 400)
        self.assertEqual((300 - pan_y) / 2, 300)

    def test_zoom_keeps_center_after_a_pan(self):
        pan_x, pan_y = zoom_about_center(-100, -40, 2, 200, 100, 4)
        self.assertEqual(pan_x, -300)
        self.assertEqual(pan_y, -130)
        self.assertEqual((100 - pan_x) / 4, 100)
        self.assertEqual((50 - pan_y) / 4, 45)

    def test_smaller_than_the_view_stays_centered(self):
        pan_x, pan_y = clamp_pan(50, 50, 1, 200, 200, (0, 0, 100, 80))
        self.assertEqual(pan_x, 50)
        self.assertEqual(pan_y, 60)

    def test_larger_than_the_view_cannot_leave_a_gap(self):
        pan_x, pan_y = clamp_pan(30, 40, 1, 200, 100, (0, 0, 400, 200))
        self.assertEqual((pan_x, pan_y), (0, 0))
        pan_x, pan_y = clamp_pan(-999, -999, 1, 200, 100, (0, 0, 400, 200))
        self.assertEqual((pan_x, pan_y), (-200, -100))

    def test_letterboxed_wall_at_zoom_one_has_no_pan(self):
        bounds = content_bounds([Tile(0, 100, 50, 800, 700)])
        pan_x, pan_y = clamp_pan(0, 0, 1, 1000, 800, bounds)
        self.assertEqual(pan_x, 0)
        self.assertEqual(pan_y, 0)

    def test_pinch_magnification_is_a_relative_delta(self):
        self.assertIsNone(zoom_factor_from_magnification(0))
        self.assertAlmostEqual(zoom_factor_from_magnification(0.02), 1.02)
        self.assertAlmostEqual(zoom_factor_from_magnification(-0.1), 0.9)
        self.assertAlmostEqual(zoom_factor_from_magnification(1.1), 1.1)


class WallZoomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_zoom_in_anchors_the_center_and_zoom_out_resets(self):
        view = WallView()
        view.resize(800, 600)
        view.tiles = [Tile(0, 0, 0, 800, 600)]
        view.zoom_in()
        self.assertAlmostEqual(view.zoom, 1.25)
        self.assertAlmostEqual((400 - view.pan_x) / view.zoom, 400)
        self.assertAlmostEqual((300 - view.pan_y) / view.zoom, 300)
        view.zoom_out()
        self.assertEqual(view.zoom, 1.0)
        self.assertEqual(view.pan_x, 0.0)
        self.assertEqual(view.pan_y, 0.0)
        view.close()

    def test_resize_while_zoomed_does_not_reflow(self):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "a.png")
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("#ff0000"))
        self.assertTrue(image.save(path))
        view = WallView()
        view.resize(400, 300)
        view.photos = [Photo(path, 8, 8)]
        view._relayout(debounce_decode=False)
        placed = (view.tiles[0].x, view.tiles[0].y, view.tiles[0].w, view.tiles[0].h)
        view.zoom_in()
        view.resize(640, 440)
        view._decode_timer.stop()
        self.assertEqual(
            (view.tiles[0].x, view.tiles[0].y, view.tiles[0].w, view.tiles[0].h),
            placed,
        )
        self.assertGreater(view.zoom, 1.2)
        view.shutdown()
        self.app.processEvents()
        QThreadPool.globalInstance().waitForDone(2000)
        self.app.processEvents()
        view.close()

    def test_only_a_few_photos_decode_at_once(self):
        folder = tempfile.mkdtemp()
        paths = []
        for index in range(12):
            path = os.path.join(folder, f"{index}.png")
            image = QImage(16, 16, QImage.Format.Format_RGB32)
            image.fill(QColor("#3399ff"))
            self.assertTrue(image.save(path))
            paths.append(path)
        pool = QThreadPool.globalInstance()
        previous = pool.maxThreadCount()
        pool.setMaxThreadCount(2)
        view = WallView()
        try:
            view.resize(800, 500)
            view.photos = [Photo(path, 16, 16) for path in paths]
            view._relayout(debounce_decode=False)
            self.assertGreater(len(view._inflight), 0)
            self.assertLessEqual(len(view._inflight), 2)
        finally:
            view.shutdown()
            self.app.processEvents()
            pool.waitForDone(3000)
            self.app.processEvents()
            pool.setMaxThreadCount(previous)
            view.close()

    def test_folder_button_asks_instead_of_using_a_menu(self):
        window = MainWindow()
        button = window.toolbar.widgetForAction(window.open_folder_action)
        self.assertIsNone(button.menu())
        texts = []
        for action in window.menuBar().actions():
            menu = action.menu()
            if menu is not None and menu.title() == "文件":
                texts = [child.text() for child in menu.actions() if child.text()]
        self.assertIn("选择文件夹…", texts)
        self.assertNotIn("包含子文件夹", texts)
        window.close()

    def test_focused_photo_zooms_around_the_view_center(self):
        view = WallView()
        view.resize(800, 600)
        view.photos = [Photo("x", 400, 300)]
        view.focus_index = 0
        view.zoom_in()
        self.assertAlmostEqual(view.focus_zoom, 1.25)
        self.assertEqual(view.zoom, 1.0)
        bx, by, bw, bh = view._focus_fit()
        x = bx * view.focus_zoom + view.focus_pan_x
        y = by * view.focus_zoom + view.focus_pan_y
        self.assertAlmostEqual(x + bw * view.focus_zoom / 2, 400)
        self.assertAlmostEqual(y + bh * view.focus_zoom / 2, 300)
        view.zoom_out()
        self.assertEqual(view.focus_zoom, 1.0)
        self.assertEqual(view.focus_pan_x, 0.0)
        view.close()


if __name__ == "__main__":
    unittest.main()
