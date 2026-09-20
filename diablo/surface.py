"""
Where Diablo's conversation surface goes, and how big it is.

Three presentation levels, one object:

  PRESENCE     the orb at home, and nothing else
  INTERACTION  a compact surface for one exchange: what you asked, what he
               said, the line you type into. As tall as its content.
  WORK         a larger, temporary surface for long answers, several things
               running at once, the trace, health and settings. Resizable.

The surface is anchored to the orb's home, not placed beside it: its
bottom-right corner sits just above him, and it grows up and to the left
from that corner and shrinks back into it. So the corner nearest him never
moves, and the surface reads as him opening up rather than a window arriving.

Pure geometry, in AppKit points (origin bottom-left). No AppKit here: the
shell passes in the screen's visible frame and the orb's home, and applies
what comes back. The web page decides the level and reports its content's
height; this decides what that means on this screen.
"""

from typing import Optional, Tuple

Rect = Tuple[float, float, float, float]     # x, y, width, height
Size = Tuple[float, float]
Point = Tuple[float, float]

PRESENCE = "presence"
INTERACTION = "interaction"
WORK = "work"
LEVELS = (PRESENCE, INTERACTION, WORK)

# The compact surface. Wide enough for a sentence and a reply at a
# comfortable measure; as tall as what is in it, between the command line
# alone and the point where it stops being a quick exchange. app/surface.js
# holds the same numbers and escalates to WORK past the ceiling; the tests
# check the two agree.
INTERACTION_WIDTH = 380.0
INTERACTION_MIN_HEIGHT = 58.0
INTERACTION_MAX_HEIGHT = 440.0

# The work surface: opened at NORMAL, resizable between MIN and a ceiling
# that is a fraction of the screen, because it is a tool occupying part of
# the workspace, never the whole desktop.
WORK_NORMAL = (560.0, 620.0)
WORK_MIN = (440.0, 380.0)
WORK_MAX_FRACTION = (0.62, 0.88)

# Between the surface and the orb's window, and from the screen's edges —
# the orb's own inset, so the two share a right edge.
GAP = 10.0
INSET = 18.0


def accept(seq: int, last: int) -> bool:
    """A report from the page is used only if it is newer than the last one used."""
    return int(seq) > int(last)


def anchor_for(home: Point, canvas: float) -> Point:
    """The surface's bottom-right corner: just above the orb, sharing its right edge."""
    return (home[0] + canvas / 2.0, home[1] + canvas / 2.0 + GAP)


def work_bounds(visible: Rect, anchor: Point) -> Tuple[Size, Size]:
    """
    The smallest and largest the work surface may be on this screen, from
    this anchor. The largest is a fraction of the screen, and never taller
    than the room above the orb: it must not cover him.
    """
    vx, vy, vw, vh = visible
    room_w = anchor[0] - (vx + INSET)
    room_h = (vy + vh - INSET) - anchor[1]
    max_w = max(WORK_MIN[0], min(vw * WORK_MAX_FRACTION[0], room_w))
    max_h = max(WORK_MIN[1], min(vh * WORK_MAX_FRACTION[1], room_h))
    return WORK_MIN, (max_w, max_h)


def clamp(size: Size, low: Size, high: Size) -> Size:
    return (min(max(size[0], low[0]), high[0]), min(max(size[1], low[1]), high[1]))


def size_for(level: str, content_height: float, visible: Rect, anchor: Point,
             chosen: Optional[Size] = None) -> Size:
    """
    How big the surface is at a level.

    Both surfaces follow their content: an answer takes the room it needs
    and no more. INTERACTION is bounded by its floor and ceiling, WORK by
    what this screen allows, and a size the person dragged the work surface
    to wins over both. PRESENCE has no surface.
    """
    if level == INTERACTION:
        room = (visible[1] + visible[3] - INSET) - anchor[1]
        ceiling = max(INTERACTION_MIN_HEIGHT, min(INTERACTION_MAX_HEIGHT, room))
        height = min(max(float(content_height or 0.0), INTERACTION_MIN_HEIGHT), ceiling)
        return (INTERACTION_WIDTH, round(height))
    if level == WORK:
        low, high = work_bounds(visible, anchor)
        if chosen:
            w, h = chosen
        else:
            w = WORK_NORMAL[0]
            h = float(content_height) if content_height else WORK_NORMAL[1]
        w, h = clamp((w, h), low, high)
        return (round(w), round(h))
    return (0.0, 0.0)


def frame_for(size: Size, anchor: Point, visible: Rect) -> Rect:
    """
    The window frame for a size: grown up and left from the anchor, then
    kept on the screen. The anchor corner only moves if the screen forces it.
    """
    vx, vy, vw, vh = visible
    # Never larger than the screen it is on, whatever it was asked for.
    w = min(size[0], vw - 2 * INSET)
    h = min(size[1], vh - 2 * INSET)
    x = anchor[0] - w
    y = anchor[1]
    x = min(max(x, vx + INSET), vx + vw - INSET - w)
    if y + h > vy + vh - INSET:
        y = max(vy + INSET, vy + vh - INSET - h)
    return (x, y, w, h)
