"""
D.I.A.B.L.O. — where to stand when you are pointing at something.

The rule
--------
The application is the canvas. Diablo borrows a corner of it and gives it back.
So the thing being explained is never the thing being covered:

        [Diablo]  Click Export to save a copy
             \\
              \\
          ( Export )        <- ring on the target, target still readable

A marker drawn ON the target hides the target. That was the previous
behaviour, and it is a strange failure to look at: the assistant obscures the
button at the exact moment it tells you to press it.

Why this is a module and not four lines in the drawing code
-----------------------------------------------------------
Choosing a side is arithmetic over display geometry, window bounds and the
cursor, and arithmetic can be tested. The previous version hard-coded "58
points below, or above if that falls off the bottom" — which is a placement
rule that knows about one edge of one display and nothing else. On a target
near the right edge of a 1512-wide Retina panel sitting at x=-1512 it has
nothing useful to say.

Everything here is in Quartz space, top-left origin, the canonical space
display.py established. A display to the left has a negative x and needs no
special case anywhere in this file.
"""

import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import config  # noqa: E402
from skills import display as _display  # noqa: E402

# The ring drawn on the target. Big enough to find, small enough that a button
# is still legible through the middle of it.
RING = config.env_float("DIABLO_GUIDE_RING", 26.0, low=8.0, high=120.0)

# Diablo's marker: the small disc he occupies while guiding.
MARKER = config.env_float("DIABLO_GUIDE_MARKER", 17.0, low=6.0, high=64.0)

# Centre-to-centre between the target and the marker. Far enough that the ring
# and the marker never touch, near enough that the connector reads as one
# gesture rather than two objects.
STANDOFF = config.env_float("DIABLO_GUIDE_STANDOFF", 86.0, low=40.0, high=400.0)

# Keep everything this far inside a display edge.
MARGIN = 14.0

# Eight directions, as (dx, dy) in Quartz space — y grows downward, so
# "above" is negative y. Ordered by preference, and the order is a design
# decision rather than an accident:
#
#   above      the brief's own diagram, and it puts the label where a caption
#              belongs — over the thing, reading downward into it
#   right      next best for a left-to-right reader: the eye is already there
#   left       for targets near a right edge
#   below      last of the cardinals; a label under a control competes with
#              whatever the control sits above
#   diagonals  only when no cardinal fits
_DIRECTIONS: List[Tuple[str, float, float]] = [
    ("above", 0.0, -1.0),
    ("right", 1.0, 0.0),
    ("left", -1.0, 0.0),
    ("below", 0.0, 1.0),
    ("above right", 0.7071, -0.7071),
    ("above left", -0.7071, -0.7071),
    ("below right", 0.7071, 0.7071),
    ("below left", -0.7071, 0.7071),
]

_SPOKEN = {
    "above": "just above it", "below": "just below it",
    "left": "to the left of it", "right": "to the right of it",
    "above right": "above and right of it", "above left": "above and left of it",
    "below right": "below and right of it", "below left": "below and left of it",
}


@dataclass
class Placement:
    """Where the ring goes, where Diablo stands, and what joins them."""

    target_x: float
    target_y: float
    marker_x: float
    marker_y: float
    side: str
    # The label box, top-left origin, in Quartz points.
    label_x: float
    label_y: float
    label_width: float
    label_height: float
    display_index: int = 0
    display_scale: float = 1.0
    confident: bool = True
    why: str = ""
    # What is being referred to, as half-sizes: a control is a ring of RING,
    # a region of content (a table, a graph, a paragraph) is its own box.
    ring_w: float = RING
    ring_h: float = RING
    # How big whoever stands at the marker is: the overlay's small marker,
    # or the orb itself when it has walked over.
    marker_r: float = MARKER

    @property
    def is_region(self) -> bool:
        return self.ring_w != RING or self.ring_h != RING

    def bounds(self) -> Tuple[float, float, float, float]:
        """
        The rectangle the overlay window has to cover, in Quartz points.

        Everything drawn — ring, connector, marker, label — has to be inside
        one window, because they are one gesture and a second window would
        composite separately and slide against the first.
        """
        left = min(self.target_x - self.ring_w, self.marker_x - self.marker_r, self.label_x)
        top = min(self.target_y - self.ring_h, self.marker_y - self.marker_r, self.label_y)
        right = max(self.target_x + self.ring_w, self.marker_x + self.marker_r,
                    self.label_x + self.label_width)
        bottom = max(self.target_y + self.ring_h, self.marker_y + self.marker_r,
                     self.label_y + self.label_height)
        pad = 24.0          # room for the ring's outer pulse
        return (left - pad, top - pad,
                (right - left) + pad * 2, (bottom - top) + pad * 2)

    def describe(self) -> str:
        """Where Diablo is, in words, for the spoken half of the guidance."""
        return _SPOKEN.get(self.side, "next to it")


def outline(points, pad: float) -> List[Tuple[float, float]]:
    """
    An area's own outline, moved out by `pad` points (B.5).

    Each corner moves away from the middle, so the line sits just outside
    the shape instead of on it and never hides the edge it is showing.
    """
    points = [(float(x), float(y)) for x, y in (points or [])]
    if not points:
        return []
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    out = []
    for x, y in points:
        dx, dy = x - cx, y - cy
        norm = (dx * dx + dy * dy) ** 0.5
        if norm == 0.0:
            out.append((x, y))
        else:
            out.append((x + pad * dx / norm, y + pad * dy / norm))
    return out


def label_size(text: str, font_size: float = 13.0) -> Tuple[float, float]:
    """
    Roughly how big a caption will be, without asking AppKit.

    An estimate on purpose: the placement search runs eight candidates and
    measuring text eight times through NSString costs more than the error is
    worth. The estimate runs slightly wide, which is the safe direction — a
    label that fits the reserved box is fine, one that overflows it is not.
    """
    if not text:
        return (0.0, 0.0)
    longest = max((len(line) for line in text.split("\n")), default=0)
    lines = text.count("\n") + 1
    return (min(longest * font_size * 0.58, 320.0) + 22.0,
            lines * (font_size + 5.0) + 12.0)


def _overlaps(a: Tuple[float, float, float, float],
              b: Tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def _inside(box: Tuple[float, float, float, float], screen) -> bool:
    x, y, w, h = box
    return (x >= screen.x + MARGIN and y >= screen.y + MARGIN
            and x + w <= screen.right - MARGIN and y + h <= screen.bottom - MARGIN)


def place(target_x: float, target_y: float, label: str = "",
          window: Optional[Dict[str, Any]] = None,
          cursor: Optional[Tuple[float, float]] = None,
          step: str = "",
          extent: Optional[Tuple[float, float]] = None,
          marker: Optional[float] = None) -> Optional[Placement]:
    """
    Choose where Diablo stands to point at a target.

    Returns None only when the target is not on any display, which is a real
    answer: there is nothing to point at and the caller should say so rather
    than draw a marker into a gap between monitors.

    `extent` is the half-width and half-height of what is being referred to,
    when it is a region of content rather than a control; the stand point
    then keeps the same gap from the region's edge that it keeps from a
    control's ring. `marker` is the radius of whoever stands there. Both
    default to the overlay's own sizes, which gives exactly the old answer.
    """
    half_w, half_h = extent if extent else (RING, RING)
    radius = MARKER if marker is None else float(marker)
    gap = STANDOFF - RING               # ring edge to marker centre
    screen = _display.for_point(target_x, target_y)
    if screen is None:
        return None

    # The caption carries the step counter above it, so reserve that line.
    width, height = label_size(label)
    if step and label:
        height += 15.0

    if cursor is None:
        cursor = _display.cursor()

    # The target's ring, plus clearance. Nothing may be drawn inside this.
    forbidden = (target_x - half_w - 8, target_y - half_h - 8,
                 (half_w + 8) * 2, (half_h + 8) * 2)

    best, best_score = None, float("-inf")
    for index, (side, dx, dy) in enumerate(_DIRECTIONS):
        off_x, off_y = abs(dx) * (half_w + gap), abs(dy) * (half_h + gap)
        if dx and dy:
            # A diagonal only has to clear the target on one axis. For the
            # small marker it always does; for something bigger standing
            # there (the orb) it can fall a point short, so it is stretched
            # along the diagonal just far enough, and only then.
            need = 8.0 + radius + 1.0
            if off_x < half_w + need and off_y < half_h + need:
                stretch = min((half_w + need) / off_x, (half_h + need) / off_y)
                off_x, off_y = off_x * stretch, off_y * stretch
        mx = target_x + (off_x if dx > 0 else -off_x)
        my = target_y + (off_y if dy > 0 else -off_y)

        # The label hangs off the marker, on the far side from the target, so
        # the text leads away from the thing rather than back across it.
        if dy < -0.3:                       # Diablo is above: label above him
            lx, ly = mx - width / 2, my - radius - 6 - height
        elif dy > 0.3:                      # below: label below him
            lx, ly = mx - width / 2, my + radius + 6
        elif dx > 0:                        # right: label to his right
            lx, ly = mx + radius + 8, my - height / 2
        else:                               # left: label to his left
            lx, ly = mx - radius - 8 - width, my - height / 2

        marker_box = (mx - radius, my - radius, radius * 2, radius * 2)
        label_box = (lx, ly, width, height)

        if not _inside(marker_box, screen):
            continue
        if width and not _inside(label_box, screen):
            continue
        if _overlaps(marker_box, forbidden) or (width and _overlaps(label_box, forbidden)):
            continue

        # Preference order, scaled so it never outweighs a hard constraint.
        score = 100.0 - index * 6.0

        # Staying inside the window being explained looks deliberate; spilling
        # onto the desktop next to it looks like a stray tooltip.
        if window and window.get("width"):
            frame = (float(window["x"]), float(window["y"]),
                     float(window["width"]), float(window["height"]))
            if _inside_rect(marker_box, frame):
                score += 24.0
            if width and not _inside_rect(label_box, frame):
                score -= 10.0

        # Never park on the pointer. The user's cursor is theirs, and a marker
        # under it reads as something they are about to click.
        if cursor:
            near = (cursor[0] - 26, cursor[1] - 26, 52, 52)
            if _overlaps(marker_box, near) or (width and _overlaps(label_box, near)):
                score -= 45.0

        # Prefer room to breathe: how far the whole arrangement sits from the
        # nearest display edge, so a marker does not hug a bezel when the
        # middle of the screen was available.
        whole = _union(marker_box, label_box) if width else marker_box
        score += min(_edge_distance(whole, screen), 120.0) * 0.08

        if score > best_score:
            best, best_score = Placement(
                target_x=target_x, target_y=target_y, marker_x=mx, marker_y=my,
                side=side, label_x=lx, label_y=ly,
                label_width=width, label_height=height,
                display_index=screen.index, display_scale=screen.scale,
                why=f"{side}, score {score:.0f}",
                ring_w=half_w, ring_h=half_h, marker_r=radius), score

    if best is not None:
        return best

    # Nowhere clear. Rather than covering the target, stand as close as the
    # display allows and say the placement is compromised — the caller can
    # shorten the label or drop it entirely.
    return Placement(
        target_x=target_x, target_y=target_y,
        marker_x=min(max(target_x, screen.x + MARGIN + radius), screen.right - MARGIN - radius),
        marker_y=min(max(target_y - (half_h + gap), screen.y + MARGIN + radius),
                     screen.bottom - MARGIN - radius),
        side="above", label_x=0.0, label_y=0.0, label_width=0.0, label_height=0.0,
        display_index=screen.index, display_scale=screen.scale,
        confident=False,
        why="no side had room for the caption; marker only",
        ring_w=half_w, ring_h=half_h, marker_r=radius)


def _inside_rect(box, frame) -> bool:
    bx, by, bw, bh = box
    fx, fy, fw, fh = frame
    return bx >= fx and by >= fy and bx + bw <= fx + fw and by + bh <= fy + fh


def _union(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left, top = min(ax, bx), min(ay, by)
    return (left, top, max(ax + aw, bx + bw) - left, max(ay + ah, by + bh) - top)


def _edge_distance(box, screen) -> float:
    x, y, w, h = box
    return min(x - screen.x, y - screen.y, screen.right - (x + w), screen.bottom - (y + h))


if __name__ == "__main__":
    from skills import screen_context
    windows = screen_context.list_windows()
    window = windows[0] if windows else None
    if window:
        print(f"target window: {window['app']} at ({window['x']:.0f},{window['y']:.0f}) "
              f"{window['width']}x{window['height']}\n")
        spots = {
            "centre": (window["x"] + window["width"] / 2, window["y"] + window["height"] / 2),
            "top-left": (window["x"] + 40, window["y"] + 30),
            "top-right": (window["x"] + window["width"] - 40, window["y"] + 30),
            "bottom-right": (window["x"] + window["width"] - 40,
                             window["y"] + window["height"] - 30),
        }
        for name, (x, y) in spots.items():
            found = place(x, y, "Click Export", window)
            print(f"  {name:14} -> {found.side:12} marker ({found.marker_x:8.0f},"
                  f"{found.marker_y:7.0f})  {found.why}")
