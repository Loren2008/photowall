"""把不同比例的图片排进窗口。

每张图都完整保留，只做等比缩放，并且一律横排：
从左到右排成一行，排满再换下一行。图片彼此贴紧；
窗口比例对不上时，只在外侧留空，不裁切画面。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Tile:
    index: int
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class LayoutResult:
    """tiles 里每张图的格子都按原比例，画面完整。

    filled 表示这组图片刚好铺满窗口。铺不满时图片仍然贴在一起，空白只在外侧。
    """

    tiles: list[Tile]
    crop: float
    filled: bool


_MIN_ASPECT = 1.0 / 40.0
_MAX_ASPECT = 40.0


def compute_layout(
    aspects: list[float],
    width: int,
    height: int,
    cache: dict | None = None,
) -> LayoutResult:
    """按原比例排列。返回的每张图都是完整的，不会被裁切。"""

    if width <= 0 or height <= 0 or not aspects:
        return LayoutResult([], 0.0, False)

    clean = [_clamp_aspect(a) for a in aspects]
    if len(clean) == 1:
        return _contain_one(clean[0], width, height)

    horizontal = _best_strips(clean, width, height, cache, "row")
    tiles = _contain_tiles(_prefix(clean), clean, horizontal.ranges, width, height)
    filled = _covers(tiles, width, height)
    return LayoutResult(tiles, 0.0, filled)


def _clamp_aspect(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        return 1.0
    return min(_MAX_ASPECT, max(_MIN_ASPECT, float(value)))


def _contain_one(aspect: float, width: int, height: int) -> LayoutResult:
    if aspect >= width / height:
        tile_w = width
        tile_h = max(1, min(height, int(round(width / aspect))))
    else:
        tile_h = height
        tile_w = max(1, min(width, int(round(height * aspect))))
    tile_w = min(tile_w, width)
    tile_h = min(tile_h, height)
    x = (width - tile_w) // 2
    y = (height - tile_h) // 2
    filled = tile_w == width and tile_h == height
    return LayoutResult([Tile(0, x, y, tile_w, tile_h)], 0.0, filled)


@dataclass(frozen=True)
class _Candidate:
    tiles: list[Tile]
    crop: float
    penalty: float
    ranges: tuple[tuple[int, int], ...]


def _best_strips(
    aspects: list[float],
    width: int,
    height: int,
    cache: dict | None,
    orientation: str,
) -> _Candidate:
    prefix = _prefix(aspects)
    total = prefix[-1]
    target_inv = height / width
    counts = _candidate_counts(len(aspects), total, width, height)
    best: _Candidate | None = None
    for rows in counts:
        balanced = _balanced_ranges(prefix, rows, width, cache, (orientation, rows))
        tuned = _hill_climb(prefix, balanced, target_inv, width)
        seen: set[tuple[tuple[int, int], ...]] = set()
        for ranges in (balanced, tuned):
            key = tuple(ranges)
            if key in seen:
                continue
            seen.add(key)
            candidate = _materialize(prefix, aspects, ranges, width, height, target_inv)
            if best is None or _better(candidate, best):
                best = candidate
    assert best is not None
    return best


def _better(candidate: _Candidate, current: _Candidate) -> bool:
    return (candidate.crop + candidate.penalty) < (current.crop + current.penalty) - 1e-12


def _prefix(aspects: list[float]) -> list[float]:
    prefix = [0.0]
    for aspect in aspects:
        prefix.append(prefix[-1] + aspect)
    return prefix


def _candidate_counts(count: int, total_aspect: float, along: int, across: int) -> list[int]:
    if count <= 1 or along <= 0 or across <= 0:
        return [1]
    max_rows = min(count, across)
    min_rows = max(1, math.ceil(count / along))
    if min_rows > max_rows:
        return [max_rows]
    ideal = math.sqrt(max(total_aspect, 1e-9) * across / along)
    center = int(round(ideal))
    low = max(min_rows, center - 4)
    high = min(max_rows, max(center + 4, low))
    return list(range(low, high + 1))


def _balanced_ranges(
    prefix: list[float],
    rows: int,
    max_items: int,
    cache: dict | None,
    cache_key: tuple,
) -> list[tuple[int, int]]:
    if cache is not None and cache_key in cache:
        return list(cache[cache_key])

    count = len(prefix) - 1
    rows = max(1, min(rows, count))
    max_items = max(1, max_items)
    target = prefix[-1] / rows
    infinity = 1e300
    costs = [[infinity] * (count + 1) for _ in range(rows + 1)]
    previous = [[-1] * (count + 1) for _ in range(rows + 1)]
    costs[0][0] = 0.0

    for row in range(1, rows + 1):
        row_cost = costs[row]
        prior = costs[row - 1]
        row_prev = previous[row]
        for end in range(row, count + 1):
            end_sum = prefix[end]
            best = infinity
            best_start = -1
            start_low = max(row - 1, end - max_items)
            for start in range(start_low, end):
                prior_cost = prior[start]
                if prior_cost >= infinity:
                    continue
                span = end_sum - prefix[start]
                if span <= 0:
                    continue
                delta = span - target
                cost = prior_cost + delta * delta
                if cost < best:
                    best = cost
                    best_start = start
            row_cost[end] = best
            row_prev[end] = best_start

    if previous[rows][count] < 0:
        ranges = _even_ranges(count, rows, max_items)
    else:
        bounds = [count]
        cursor = count
        for row in range(rows, 0, -1):
            cursor = previous[row][cursor]
            if cursor < 0:
                ranges = _even_ranges(count, rows, max_items)
                break
            bounds.append(cursor)
        else:
            bounds.reverse()
            ranges = list(zip(bounds[:-1], bounds[1:]))

    if cache is not None:
        cache[cache_key] = list(ranges)
    return ranges


def _even_ranges(count: int, rows: int, max_items: int) -> list[tuple[int, int]]:
    rows = max(1, min(rows, count))
    max_items = max(1, max_items)
    base, extra = divmod(count, rows)
    sizes = [base + 1] * extra + [base] * (rows - extra)
    if any(size < 1 or size > max_items for size in sizes):
        ranges = []
        cursor = 0
        while cursor < count:
            end = min(count, cursor + max_items)
            ranges.append((cursor, end))
            cursor = end
        return ranges
    ranges = []
    cursor = 0
    for size in sizes:
        ranges.append((cursor, cursor + size))
        cursor += size
    return ranges


def _hill_climb(
    prefix: list[float],
    ranges: list[tuple[int, int]],
    target_inv: float,
    max_items: int,
) -> list[tuple[int, int]]:
    """边界每次挪一张图，让各条自然厚度之和更接近窗口。"""

    current = [tuple(pair) for pair in ranges]
    if len(current) <= 1:
        return current
    limit = max(8, (len(prefix) - 1) * 3)
    for _ in range(limit):
        error = abs(_inverse_sum(prefix, current) - target_inv)
        if error < 1e-9:
            break
        best_ranges = None
        best_error = error
        for boundary in range(1, len(current)):
            left_start, left_end = current[boundary - 1]
            right_start, right_end = current[boundary]
            options = []
            if left_end - 1 > left_start and (right_end - (left_end - 1)) <= max_items:
                options.append((left_start, left_end - 1, left_end - 1, right_end))
            if left_end + 1 < right_end and (left_end + 1 - left_start) <= max_items:
                options.append((left_start, left_end + 1, left_end + 1, right_end))
            for new_left_start, new_left_end, new_right_start, new_right_end in options:
                trial = list(current)
                trial[boundary - 1] = (new_left_start, new_left_end)
                trial[boundary] = (new_right_start, new_right_end)
                trial_error = abs(_inverse_sum(prefix, trial) - target_inv)
                if trial_error + 1e-12 < best_error:
                    best_error = trial_error
                    best_ranges = trial
        if best_ranges is None:
            break
        current = best_ranges
    return current


def _inverse_sum(prefix: list[float], ranges: list[tuple[int, int]]) -> float:
    total = 0.0
    for start, end in ranges:
        span = prefix[end] - prefix[start]
        total += 1.0 / span
    return total


def _materialize(
    prefix: list[float],
    aspects: list[float],
    ranges: list[tuple[int, int]],
    width: int,
    height: int,
    target_inv: float,
) -> _Candidate:
    inverse = _inverse_sum(prefix, ranges)
    crop = _crop_ratio(inverse, target_inv)
    natural = [width / (prefix[end] - prefix[start]) for start, end in ranges]
    thicknesses = _spread(natural, height)
    tiles: list[Tile] = []
    y = 0
    area_floor = 1.0
    area_ceil = 0.0
    for (start, end), thickness in zip(ranges, thicknesses):
        widths = _spread(aspects[start:end], width)
        x = 0
        for offset, tile_w in enumerate(widths):
            tiles.append(Tile(start + offset, x, y, tile_w, thickness))
            if tile_w > 0 and thickness > 0 and width > 0 and height > 0:
                fraction = (tile_w * thickness) / (width * height)
                area_floor = min(area_floor, fraction)
                area_ceil = max(area_ceil, fraction)
            x += tile_w
        y += thickness
    penalty = _penalty(thicknesses, height, area_floor, area_ceil, len(aspects))
    return _Candidate(tiles, crop, penalty, tuple(ranges))


def _crop_ratio(inverse: float, target_inv: float) -> float:
    if inverse <= 0 or target_inv <= 0:
        return 1.0
    return 1.0 - (min(inverse, target_inv) / max(inverse, target_inv))


def _penalty(
    thicknesses: list[int],
    height: int,
    smallest_area: float,
    largest_area: float,
    count: int,
) -> float:
    if not thicknesses or height <= 0 or count <= 0:
        return 1.0
    mean = height / len(thicknesses)
    if mean <= 0:
        return 1.0
    variance = sum((value - mean) ** 2 for value in thicknesses) / len(thicknesses)
    uneven = min(2.0, math.sqrt(variance) / mean)
    penalty = 0.03 * uneven
    if smallest_area > 0 and largest_area > 0:
        imbalance = largest_area / smallest_area
        if imbalance > 2.5:
            # 每张都要能看清，不用一张巨大、其余缩成细条来换一点点裁切。
            penalty += (imbalance - 2.5) * 0.06
    fair_share = 1.0 / count
    if fair_share > 0 and smallest_area < fair_share * 0.42:
        penalty += 0.45 * (1 - smallest_area / (fair_share * 0.42))
    return penalty


def _contain_tiles(
    prefix: list[float],
    aspects: list[float],
    ranges: tuple[tuple[int, int], ...] | list[tuple[int, int]],
    width: int,
    height: int,
) -> list[Tile]:
    """按原比例排成实心图块，再整体缩放到窗口里能放下的最大尺寸。"""

    natural = [width / (prefix[end] - prefix[start]) for start, end in ranges]
    total = math.fsum(natural)
    if total <= 0:
        return []
    if total <= height:
        mosaic_w = width
        mosaic_h = min(height, max(1, int(round(total))))
    else:
        mosaic_w = min(width, max(1, int(round(width * height / total))))
        mosaic_h = height
    origin_x = (width - mosaic_w) // 2
    origin_y = (height - mosaic_h) // 2
    thicknesses = _spread(natural, mosaic_h)
    tiles: list[Tile] = []
    y = origin_y
    for (start, end), thickness in zip(ranges, thicknesses):
        widths = _spread(aspects[start:end], mosaic_w)
        x = origin_x
        for offset, tile_w in enumerate(widths):
            tiles.append(Tile(start + offset, x, y, tile_w, thickness))
            x += tile_w
        y += thickness
    return tiles


def _covers(tiles: list[Tile], width: int, height: int) -> bool:
    if width <= 0 or height <= 0 or not tiles:
        return False
    area = 0
    for tile in tiles:
        if tile.w <= 0 or tile.h <= 0:
            return False
        if tile.x < 0 or tile.y < 0 or tile.x + tile.w > width or tile.y + tile.h > height:
            return False
        area += tile.w * tile.h
    return area == width * height


def _spread(weights: list[float], total: int) -> list[int]:
    """按权重把 total 个像素分成整数份，总和恰好等于 total。"""

    count = len(weights)
    if count == 0:
        return []
    if total <= 0:
        return [0] * count
    if count == 1:
        return [total]

    safe = [weight if weight > 1e-9 else 1e-9 for weight in weights]
    weight_sum = math.fsum(safe)
    raw = [total * weight / weight_sum for weight in safe]
    parts = [int(math.floor(value)) for value in raw]
    leftover = total - sum(parts)
    if leftover > 0:
        order = sorted(range(count), key=lambda index: raw[index] - parts[index], reverse=True)
        for index in order[:leftover]:
            parts[index] += 1
    elif leftover < 0:
        order = sorted(range(count), key=lambda index: parts[index], reverse=True)
        cursor = 0
        while leftover < 0 and cursor < count * 2:
            donor = order[cursor % count]
            if parts[donor] > 0:
                parts[donor] -= 1
                leftover += 1
            cursor += 1

    if total >= count:
        guard = 0
        while any(part <= 0 for part in parts) and guard < count * count:
            empty = next(index for index, part in enumerate(parts) if part <= 0)
            donor = max(range(count), key=lambda index: parts[index])
            if parts[donor] <= 1:
                break
            parts[donor] -= 1
            parts[empty] += 1
            guard += 1
    return parts
