"""
D.I.A.B.L.O. — areas in a picture, found by their pixels rather than by a model.

B.4. The vision model understands what an area is and cannot say where it is.
Asked for the welfare-loss triangle on the subsidy fixture it described it
correctly nine times in nine ("the grey triangle just left of where the two
lines cross") and put its box on the triangle zero times in nine. Most of that
was a coordinate space nobody had told us about; the rest is the model.

So geometry comes from here, and the model is only asked to choose. A filled
area in a chart or a diagram is a run of one colour, and a run of one colour
is something pixels can find exactly: the triangle's rectangle comes out of
the picture, not out of a sentence.

    load_rgb(path)                  the picture, as an array
    candidates(rgb, min_edge)       the filled areas in it, biggest first
    sheet(path, found, out)         the picture with each area lettered, for
                                    a model to choose from

Nothing here calls a model, knows what a subsidy is, or knows where anything
is on the screen. Boxes are in the picture's own pixels; turning them into
screen points is the existing mapper's job.
"""

from dataclasses import dataclass, replace
from typing import List, Optional, Sequence, Tuple

import numpy as np

# Letters for the sheet. More areas than this and the question is unfair.
LETTERS = "ABCDEFGHJK"

# A colour has to cover at least this share of the picture to be a fill
# worth following. Anti-aliasing, text and grain are all below it.
MIN_SHARE = 0.002
# How far a pixel may be from a fill's colour and still belong to it, after
# smoothing. Wide enough for grain and a grid line drawn through the fill,
# narrow enough that violet and grey stay apart.
TOLERANCE = 30.0
# A region fills a fair part of its own rectangle; a diagonal line or a curve
# does not. A right triangle fills half.
MIN_FILL = 0.22
# The card a figure sits on is not an area of the figure.
MAX_SHARE_OF_PICTURE = 0.45
# Nor is a window's title bar, which is a solid strip 37 times wider than it
# is tall. The longest bar in a bar chart is nowhere near this.
MAX_ELONGATION = 12.0


@dataclass(frozen=True)
class Region:
    """One filled area, in the picture's own pixels."""

    left: int
    top: int
    right: int
    bottom: int
    pixels: int
    colour: Tuple[int, int, int]
    # The area's own edge, as a closed polygon in the same pixels (B.5). Its
    # box above is derived from the mask this came from; this is the mask,
    # kept. Empty when it could not be traced, and the box is then all there is.
    outline: Tuple[Tuple[float, float], ...] = ()

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def fill(self) -> float:
        area = self.width * self.height
        return self.pixels / area if area else 0.0

    def fractions(self, width: int, height: int) -> Tuple[float, float, float, float]:
        """The box as fractions of the picture, which is what the mapper takes."""
        return (self.left / width, self.top / height,
                self.right / width, self.bottom / height)


def load_rgb(path: str) -> Optional[np.ndarray]:
    """An image file as an H x W x 3 uint8 array, or None."""
    try:
        from AppKit import NSBitmapImageRep
        from Foundation import NSData
        rep = NSBitmapImageRep.imageRepWithData_(NSData.dataWithContentsOfFile_(path))
        if rep is None:
            return None
        width, height = int(rep.pixelsWide()), int(rep.pixelsHigh())
        # Bytes per pixel from bitsPerPixel, not from the sample count: an RGB
        # image is often stored padded to four bytes a pixel, and reading it
        # three at a time shears every row a little further than the last.
        stride = int(rep.bitsPerPixel()) // 8
        row = int(rep.bytesPerRow())
        if int(rep.bitsPerSample()) != 8 or int(rep.samplesPerPixel()) < 3 \
                or stride < 3 or rep.isPlanar():
            return None
        data = np.frombuffer(bytes(rep.bitmapData()[: row * height]), dtype=np.uint8)
        data = data.reshape(height, row)[:, : width * stride]
        return data.reshape(height, width, stride)[:, :, :3].copy()
    except Exception:
        return None


def save_rgb(rgb: np.ndarray, path: str) -> bool:
    """An H x W x 3 uint8 array to a PNG at `path`. False if it could not be written."""
    try:
        from AppKit import NSBitmapImageRep, NSPNGFileType
        height, width = int(rgb.shape[0]), int(rgb.shape[1])
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, width, height, 8, 3, False, False, "NSDeviceRGBColorSpace",
            width * 3, 24)
        rep.bitmapData()[: width * height * 3] = np.ascontiguousarray(
            rgb[:, :, :3], dtype=np.uint8).tobytes()
        data = rep.representationUsingType_properties_(NSPNGFileType, {})
        return bool(data is not None and data.writeToFile_atomically_(path, True))
    except Exception:
        return False


def _is_paper(colour) -> bool:
    """
    Very light and almost colourless: the page, a card, a margin.

    Measured on the real price-floor page: without this, its off-white card,
    a margin strip and a header band were offered as areas beside the three
    the figure actually has.
    """
    high, low = float(max(colour)), float(min(colour))
    return high > 216 and (high - low) / max(high, 1.0) < 0.06


def _smooth(rgb: np.ndarray) -> np.ndarray:
    """
    Grain and hairlines out, edges kept.

    A median, not a mean. A mean blurs a two-pixel line drawn across an area
    into a wide band of in-between colour, and that band cut the welfare-loss
    triangle in two. A median removes the line outright, because it is a
    minority in every window it passes through, and leaves the area's edge
    where it was.
    """
    from scipy import ndimage
    return ndimage.median_filter(rgb, size=(5, 5, 1)).astype(np.float32)


def _fill_colours(smooth: np.ndarray, limit: int = 12) -> List[np.ndarray]:
    """
    The colours that fill real areas: peaks of a coarse histogram.

    Near-white is left out, because in a document it is paper, and so is the
    single most common colour, because that is whatever the figure sits on.
    """
    flat = smooth.reshape(-1, 3)
    bins = (flat // 16).astype(np.int32)
    keys = bins[:, 0] * 256 + bins[:, 1] * 16 + bins[:, 2]
    counts = np.bincount(keys, minlength=4096)
    floor = max(1, int(MIN_SHARE * flat.shape[0]))
    order = np.argsort(counts)[::-1]
    background = order[0]
    colours = []
    for key in order:
        if counts[key] < floor or len(colours) >= limit:
            break
        if key == background:
            continue
        members = flat[keys == key]
        colour = members.mean(axis=0)
        if colour.min() > 232 or _is_paper(colour):
            continue
        colours.append(colour)
    return colours


def candidates(rgb: np.ndarray, min_edge: float = 24.0,
               limit: int = len(LETTERS)) -> List[Region]:
    """
    Filled areas in the picture, largest first, with no two describing the same one.

    `min_edge` is in the picture's pixels: the smallest a side may be and
    still be an area rather than a line of type or a legend swatch.
    """
    if rgb is None or rgb.ndim != 3 or rgb.shape[0] < 8 or rgb.shape[1] < 8:
        return []
    from scipy import ndimage
    height, width = rgb.shape[:2]
    smooth = _smooth(rgb)
    found: List[Region] = []
    masks = {}
    for colour in _fill_colours(smooth):
        near = np.linalg.norm(smooth - colour, axis=2) < TOLERANCE
        near = ndimage.binary_closing(near, iterations=2)
        near = ndimage.binary_fill_holes(near)
        labels, count = ndimage.label(near)
        if not count:
            continue
        sizes = ndimage.sum(near, labels, index=np.arange(1, count + 1))
        boxes = ndimage.find_objects(labels)
        for index, box in enumerate(boxes):
            if box is None:
                continue
            top, bottom = box[0].start, box[0].stop
            left, right = box[1].start, box[1].stop
            region = Region(left, top, right, bottom, int(sizes[index]),
                            tuple(int(c) for c in colour))
            masks[region] = labels[box] == index + 1
            if region.width < min_edge or region.height < min_edge:
                continue
            if region.fill < MIN_FILL:
                continue
            if max(region.width, region.height) > \
                    MAX_ELONGATION * min(region.width, region.height):
                continue
            if region.width * region.height > MAX_SHARE_OF_PICTURE * width * height:
                continue
            found.append(region)
    kept = _distinct(sorted(found, key=lambda r: -r.pixels))[:limit]
    # Traced only for what is kept: at most ten areas, each from the mask it
    # was found in. Nothing is looked at again to draw it.
    return [replace(region, outline=_outline(masks[region], region.left, region.top))
            for region in kept]


# ─── An area's edge (B.5) ────────────────────────────────────────────────────
#
# B.4 found the welfare-loss triangle from its mask and then kept only the
# mask's bounding box, so the triangle was shown as a rectangle half of which
# was empty. The mask is traced instead: its outer boundary, followed pixel by
# pixel, then reduced to the few corners that matter.

# How far the simplified edge may stray from the traced one, in pixels. Small
# enough to keep a triangle's corners, large enough to drop the staircase.
SIMPLIFY = 2.0

_AROUND = ((0, -1), (-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1))


def _trace(mask: np.ndarray) -> List[Tuple[int, int]]:
    """The outer boundary of one connected mask, clockwise, as (row, col)."""
    padded = np.pad(mask, 1)
    rows, cols = np.nonzero(padded)
    if not len(rows):
        return []
    start = (int(rows[0]), int(cols[0]))       # topmost, then leftmost
    boundary = [start]
    current, came_from = start, 0              # entered from the west
    limit = 4 * int(mask.sum()) + 16
    for _ in range(limit):
        for turn in range(8):
            d = (came_from + 1 + turn) % 8
            nr, nc = current[0] + _AROUND[d][0], current[1] + _AROUND[d][1]
            if padded[nr, nc]:
                # Look next from the neighbour just before this one.
                came_from = (d + 4) % 8
                current = (nr, nc)
                break
        else:
            break                              # a single isolated pixel
        if current == start:
            break
        boundary.append(current)
    return [(r - 1, c - 1) for r, c in boundary]


def _simplify(points: List[Tuple[float, float]], tolerance: float) -> List[Tuple[float, float]]:
    """Douglas-Peucker on an open run of points."""
    if len(points) < 3:
        return list(points)
    stack, keep = [(0, len(points) - 1)], {0, len(points) - 1}
    arr = np.asarray(points, dtype=float)
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        a, b = arr[first], arr[last]
        seg = b - a
        run = arr[first + 1:last]
        length = float(np.hypot(*seg))
        if length == 0.0:
            dist = np.hypot(*(run - a).T)
        else:
            dist = np.abs(seg[0] * (run[:, 1] - a[1]) - seg[1] * (run[:, 0] - a[0])) / length
        worst = int(np.argmax(dist))
        if dist[worst] > tolerance:
            middle = first + 1 + worst
            keep.add(middle)
            stack += [(first, middle), (middle, last)]
    return [points[i] for i in sorted(keep)]


def _outline(mask: np.ndarray, left: int, top: int) -> Tuple[Tuple[float, float], ...]:
    """
    A closed polygon for one mask, in the picture's pixels.

    Closed curves are split at the two points furthest apart and each half
    simplified, which keeps both ends of a triangle's longest side. Each point
    is moved half a pixel out from the centre, because boundary pixels' centres
    sit half a pixel inside the edge they belong to.
    """
    ring = _trace(mask)
    if len(ring) < 3:
        return ()
    points = [(c + 0.5, r + 0.5) for r, c in ring]
    arr = np.asarray(points)
    far = int(np.argmax(np.hypot(*(arr - arr[0]).T)))
    first = _simplify(points[: far + 1], SIMPLIFY)
    second = _simplify(points[far:] + [points[0]], SIMPLIFY)
    polygon = first[:-1] + second[:-1]
    if len(polygon) < 3:
        return ()
    cx = sum(p[0] for p in polygon) / len(polygon)
    cy = sum(p[1] for p in polygon) / len(polygon)
    out = []
    for x, y in polygon:
        dx, dy = x - cx, y - cy
        norm = (dx * dx + dy * dy) ** 0.5 or 1.0
        out.append((round(left + x + 0.5 * dx / norm, 2), round(top + y + 0.5 * dy / norm, 2)))
    return tuple(out)


def _overlap(a: Region, b: Region) -> float:
    ix = max(0, min(a.right, b.right) - max(a.left, b.left))
    iy = max(0, min(a.bottom, b.bottom) - max(a.top, b.top))
    inter = ix * iy
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union else 0.0


def _inside(small: Region, big: Region) -> float:
    """How much of `small`'s box lies inside `big`'s."""
    ix = max(0, min(small.right, big.right) - max(small.left, big.left))
    iy = max(0, min(small.bottom, big.bottom) - max(small.top, big.top))
    area = small.width * small.height
    return ix * iy / area if area else 0.0


def _near_colour(a: Region, b: Region, limit: float = 45.0) -> bool:
    return sum((x - y) ** 2 for x, y in zip(a.colour, b.colour)) ** 0.5 < limit


def _distinct(regions: Sequence[Region]) -> List[Region]:
    """
    One candidate per area. Largest first, so a piece of an area — a slightly
    darker patch under a grid line, found by a second colour peak — meets the
    whole area first and is dropped. On the real price-floor page that piece
    was offered alongside the whole consumer surplus, and it was the one
    chosen.
    """
    kept: List[Region] = []
    for region in regions:
        if any(_overlap(region, other) >= 0.8 for other in kept):
            continue
        if any(_inside(region, other) >= 0.8 and _near_colour(region, other)
               for other in kept):
            continue
        kept.append(region)
    return kept


def sheet(path: str, found: Sequence[Region], out: str) -> Optional[str]:
    """
    The picture with each area outlined and lettered, saved to `out`.

    What a model is shown when it is asked to choose. It is never asked where
    anything is: every box on this sheet came from the pixels.
    """
    try:
        from AppKit import (NSBezierPath, NSBitmapImageRep, NSColor, NSFont,
                            NSFontAttributeName, NSForegroundColorAttributeName,
                            NSGraphicsContext, NSMakeRect, NSPNGFileType, NSString)
        from Foundation import NSData
        rep = NSBitmapImageRep.imageRepWithData_(NSData.dataWithContentsOfFile_(path))
        if rep is None:
            return None
        width, height = int(rep.pixelsWide()), int(rep.pixelsHigh())
        canvas = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, width, height, 8, 4, True, False, "NSDeviceRGBColorSpace", 0, 0)
        context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(canvas)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(context)
        rep.drawInRect_(NSMakeRect(0, 0, width, height))
        palette = [(0.9, 0.1, 0.1), (0.1, 0.4, 0.95), (0.05, 0.6, 0.2), (0.85, 0.45, 0.0),
                   (0.6, 0.1, 0.8), (0.0, 0.6, 0.65), (0.8, 0.0, 0.5), (0.35, 0.35, 0.35),
                   (0.55, 0.4, 0.1), (0.1, 0.1, 0.1)]
        size = max(14.0, min(width, height) / 30.0)
        for index, region in enumerate(found[: len(LETTERS)]):
            r, g, b = palette[index % len(palette)]
            colour = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)
            y = height - region.bottom          # AppKit draws from the bottom
            outline = NSBezierPath.bezierPathWithRect_(
                NSMakeRect(region.left, y, region.width, region.height))
            outline.setLineWidth_(3.0)
            colour.set()
            outline.stroke()
            tag = NSMakeRect(region.left + 3, height - region.top - size * 1.5,
                             size * 1.4, size * 1.35)
            NSColor.whiteColor().set()
            NSBezierPath.bezierPathWithRect_(tag).fill()
            colour.set()
            NSBezierPath.bezierPathWithRect_(tag).stroke()
            NSString.stringWithString_(LETTERS[index]).drawAtPoint_withAttributes_(
                (tag.origin.x + size * 0.3, tag.origin.y + size * 0.1),
                {NSFontAttributeName: NSFont.boldSystemFontOfSize_(size),
                 NSForegroundColorAttributeName: colour})
        NSGraphicsContext.restoreGraphicsState()
        data = canvas.representationUsingType_properties_(NSPNGFileType, {})
        if data is None or not data.writeToFile_atomically_(out, True):
            return None
        return out
    except Exception:
        return None
