import math
import random
import unittest

from photo_wall.layout import Tile, _spread, compute_layout


def _assert_aspects(tiles, aspects):
    ordered = sorted(tiles, key=lambda tile: tile.index)
    for tile, aspect in zip(ordered, aspects):
        if tile.w < 8 or tile.h < 8:
            continue
        cell = tile.w / tile.h
        error = abs(cell - aspect) / aspect
        if error >= 0.06:
            raise AssertionError(f"tile {tile.index} aspect {cell:.3f} vs image {aspect:.3f}")


def _assert_solid_block(tiles, width, height):
    left = min(tile.x for tile in tiles)
    top = min(tile.y for tile in tiles)
    right = max(tile.x + tile.w for tile in tiles)
    bottom = max(tile.y + tile.h for tile in tiles)
    if left < 0 or top < 0 or right > width or bottom > height:
        raise AssertionError("block outside viewport")
    shifted = [
        Tile(tile.index, tile.x - left, tile.y - top, tile.w, tile.h) for tile in tiles
    ]
    gaps, _ = _covered(shifted, right - left, bottom - top)
    if gaps:
        raise AssertionError(f"gap inside the block: {gaps}")


def _covered(tiles, width, height):
    buffer = bytearray(width * height)
    indexes = []
    for tile in tiles:
        if tile.w <= 0 or tile.h <= 0:
            continue
        if tile.x < 0 or tile.y < 0 or tile.x + tile.w > width or tile.y + tile.h > height:
            raise AssertionError(f"tile outside viewport: {tile}")
        indexes.append(tile.index)
        for y in range(tile.y, tile.y + tile.h):
            start = y * width + tile.x
            segment = buffer[start : start + tile.w]
            if any(segment):
                raise AssertionError(f"overlap at {tile}")
            buffer[start : start + tile.w] = b"\x01" * tile.w
    gaps = buffer.count(0)
    return gaps, indexes


class LayoutTests(unittest.TestCase):
    def test_empty(self):
        result = compute_layout([], 800, 600)
        self.assertEqual(result.tiles, [])

    def test_single_image_is_fully_visible_and_centered(self):
        result = compute_layout([2.0], 400, 200)
        self.assertEqual(result.crop, 0.0)
        tile = result.tiles[0]
        self.assertEqual((tile.x, tile.y, tile.w, tile.h), (0, 0, 400, 200))

        portrait = compute_layout([0.5], 400, 200)
        tile = portrait.tiles[0]
        self.assertEqual(tile.h, 200)
        self.assertLess(tile.w, 400)
        self.assertGreater(tile.x, 0)
        self.assertEqual(portrait.crop, 0.0)

    def test_two_squares_fill_a_wide_frame_without_crop(self):
        result = compute_layout([1.0, 1.0], 200, 100)
        gaps, indexes = _covered(result.tiles, 200, 100)
        self.assertEqual(gaps, 0)
        self.assertEqual(sorted(indexes), [0, 1])
        self.assertLess(result.crop, 0.01)

    def test_landscape_photos_stay_complete(self):
        aspects = [1.5] * 8
        result = compute_layout(aspects, 1600, 900)
        self.assertEqual(result.crop, 0.0)
        self.assertEqual(sorted(tile.index for tile in result.tiles), list(range(8)))
        _assert_solid_block(result.tiles, 1600, 900)
        _assert_aspects(result.tiles, aspects)
        area = sum(tile.w * tile.h for tile in result.tiles)
        self.assertGreater(area, 0.7 * 1600 * 900)

    def test_photos_run_left_to_right_in_rows(self):
        aspects = [0.62] * 24
        result = compute_layout(aspects, 1600, 900)
        rows: dict[int, list] = {}
        for tile in result.tiles:
            rows.setdefault(tile.y, []).append(tile)
        self.assertGreater(len(rows), 1)
        order = []
        for y in sorted(rows):
            row = sorted(rows[y], key=lambda tile: tile.x)
            self.assertGreater(len(row), 1)
            order.extend(tile.index for tile in row)
        self.assertEqual(order, list(range(len(aspects))))

    def test_random_photo_sets_have_no_gaps(self):
        rng = random.Random(7)
        for count in (2, 3, 5, 8, 12, 17):
            aspects = [rng.uniform(0.6, 1.9) for _ in range(count)]
            for width, height in ((1280, 800), (800, 1200), (900, 900)):
                result = compute_layout(aspects, width, height)
                indexes = [tile.index for tile in result.tiles]
                self.assertEqual(sorted(indexes), list(range(count)))
                self.assertEqual(result.crop, 0.0)
                _assert_solid_block(result.tiles, width, height)
                _assert_aspects(result.tiles, aspects)
                if result.filled:
                    gaps, _ = _covered(result.tiles, width, height)
                    self.assertEqual(gaps, 0)

    def test_harsh_pair_stays_complete_and_touching(self):
        aspects = [1.021, 0.796]
        result = compute_layout(aspects, 900, 900)
        self.assertFalse(result.filled)
        self.assertEqual(result.crop, 0.0)
        _assert_solid_block(result.tiles, 900, 900)
        _assert_aspects(result.tiles, aspects)
        area = sum(tile.w * tile.h for tile in result.tiles)
        self.assertGreater(area, 0.5 * 900 * 900)

    def test_spread_preserves_the_total_and_proportions(self):
        parts = _spread([1, 2, 3], 100)
        self.assertEqual(sum(parts), 100)
        self.assertEqual(parts, [17, 33, 50])

    def test_many_images_stay_complete_and_touching(self):
        aspects = [1.2, 0.8, 1.5, 1.0] * 15
        result = compute_layout(aspects, 640, 400)
        self.assertEqual(result.crop, 0.0)
        self.assertEqual(sorted(tile.index for tile in result.tiles), list(range(len(aspects))))
        _assert_solid_block(result.tiles, 640, 400)
        _assert_aspects(result.tiles, aspects)

    def test_repeated_layout_is_stable(self):
        aspects = [1.4, 0.7, 1.1, 1.8, 0.9]
        cache = {}
        first = compute_layout(aspects, 1000, 700, cache)
        second = compute_layout(aspects, 1000, 700, cache)
        self.assertEqual(first, second)

    def test_ideal_pack_reports_almost_no_crop(self):
        result = compute_layout([1, 1, 1, 1], 200, 200)
        self.assertLess(result.crop, 0.001)
        self.assertTrue(all(tile.w == 100 and tile.h == 100 for tile in result.tiles))

    def test_layout_of_two_hundred_images_stays_fast(self):
        import time

        aspects = [1.4 if index % 2 == 0 else 0.75 for index in range(200)]
        cache = {}
        started = time.perf_counter()
        result = compute_layout(aspects, 1920, 1080, cache)
        compute_layout(aspects, 1100, 800, cache)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.5)
        self.assertEqual(len(result.tiles), 200)
        self.assertTrue(math.isfinite(result.crop))


if __name__ == "__main__":
    unittest.main()
