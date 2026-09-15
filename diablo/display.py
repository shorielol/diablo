"""
D.I.A.B.L.O. — the displays, and the arithmetic between them.

Why this module exists
----------------------
Every spatial thing Diablo does — pointing at a button, drawing a marker,
capturing a region, deciding which screen a window is on — is arithmetic over
display geometry. That arithmetic was previously spread across three modules
and, where it existed at all, assumed one display.

It assumed wrongly. Measured on this machine:

    display 3   origin (    0, 0)   1920 x 1080   scale 1.0   main
    display 1   origin (-1512, 0)   1512 x  982   scale 2.0

Two displays, two different scale factors, and the second one at a negative x.
`screencapture -x out.png` on that setup writes exactly one file containing
exactly the main display, and reports success. Anything built on it silently
lost half the desktop.

The three coordinate spaces
---------------------------
These get confused constantly, so they are named once here and converted only
through this module:

    QUARTZ      origin top-left of the MAIN display, y grows downward.
                CGWindowList bounds and CGDisplayBounds speak this.
                A display to the left has negative x. This is Diablo's
                canonical space — every public coordinate is Quartz.

    APPKIT      origin bottom-left of the MAIN display, y grows upward.
                NSWindow and NSScreen speak this. The overlay draws in it.

    IMAGE       pixels within one capture, origin top-left. Differs from
                Quartz by the display's scale factor, which is why a Retina
                window 1512 points wide produces a 3024 pixel image.

The conversion between Quartz and AppKit is one subtraction against the main
display's height, and it holds for every display regardless of arrangement:

    appkit_y = main_height - quartz_y

Verified against the live arrangement above. The laptop's top edge is Quartz
y=0; main height is 1080; NSScreen reports its frame origin y=98 with height
982, so its top edge is at AppKit y=1080. The subtraction agrees.
"""

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass(frozen=True)
class Display:
    """One physical screen, in Quartz space."""

    id: int
    x: float          # Quartz origin, top-left
    y: float
    width: float      # points
    height: float
    scale: float      # pixels per point: 1.0 ordinary, 2.0 Retina
    is_main: bool
    index: int        # 1-based, what `screencapture -D` wants

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def pixel_width(self) -> int:
        return int(self.width * self.scale)

    @property
    def pixel_height(self) -> int:
        return int(self.height * self.scale)

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def describe(self) -> str:
        """How a person would refer to this screen out loud."""
        if self.is_main:
            where = "main"
        elif self.x < 0:
            where = "left"
        elif self.x > 0:
            where = "right"
        elif self.y < 0:
            where = "upper"
        else:
            where = "second"
        retina = ", Retina" if self.scale >= 2.0 else ""
        return f"{where} display ({int(self.width)}x{int(self.height)}{retina})"


def displays() -> List[Display]:
    """
    Every active display, main first.

    Main first is not cosmetic: `screencapture -D` numbers displays from 1 with
    the main display at 1, so the list index and the flag have to agree or
    every per-display capture lands on the wrong screen.
    """
    try:
        import Quartz
    except ImportError:
        return []
    try:
        err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
        if err or not count:
            return []
    except Exception:
        return []

    # Scale comes from NSScreen, not Quartz.
    #
    # CGDisplayPixelsWide looked like the obvious source and is wrong: on this
    # machine's Retina panel it returns 1512, the same as the point width, so
    # the computed scale was 1.0 while the display is genuinely 2.0 and its
    # window captures come back at 3024 pixels. NSScreen.backingScaleFactor
    # reports 2.0 correctly. Keyed by NSScreenNumber, which is the same
    # CGDirectDisplayID Quartz enumerates.
    scales = {}
    try:
        from AppKit import NSScreen
        for screen in NSScreen.screens():
            number = screen.deviceDescription().get("NSScreenNumber")
            if number is not None:
                scales[int(number)] = float(screen.backingScaleFactor())
    except Exception:
        pass

    found = []
    for display_id in list(ids)[:count]:
        try:
            bounds = Quartz.CGDisplayBounds(display_id)
            width = float(bounds.size.width)
            scale = scales.get(int(display_id), 1.0)
            found.append({
                "id": int(display_id),
                "x": float(bounds.origin.x),
                "y": float(bounds.origin.y),
                "width": width,
                "height": float(bounds.size.height),
                "scale": round(scale, 3),
                "is_main": bool(Quartz.CGDisplayIsMain(display_id)),
            })
        except Exception:
            continue

    found.sort(key=lambda d: (not d["is_main"], d["x"], d["y"]))
    return [Display(index=i + 1, **d) for i, d in enumerate(found)]


def main_display() -> Optional[Display]:
    for display in displays():
        if display.is_main:
            return display
    found = displays()
    return found[0] if found else None


def main_height() -> float:
    """The main display's height, which is the whole Quartz/AppKit conversion."""
    main = main_display()
    return main.height if main else 0.0


def for_point(x: float, y: float) -> Optional[Display]:
    """Which display a Quartz point falls on, or None if it falls in a gap."""
    for display in displays():
        if display.contains(x, y):
            return display
    return None


def for_window(window: Dict[str, Any]) -> Optional[Display]:
    """
    Which display a window is on, by where most of it sits.

    A window straddling two displays belongs to the one showing more of it,
    which is also the one macOS puts its menu bar on. Centre-point matching
    would flip on a window pushed slightly over an edge; area does not.
    """
    if not window:
        return None
    wx, wy = float(window.get("x", 0)), float(window.get("y", 0))
    ww, wh = float(window.get("width", 0)), float(window.get("height", 0))

    best, best_area = None, 0.0
    for display in displays():
        overlap_w = max(0.0, min(wx + ww, display.right) - max(wx, display.x))
        overlap_h = max(0.0, min(wy + wh, display.bottom) - max(wy, display.y))
        area = overlap_w * overlap_h
        if area > best_area:
            best, best_area = display, area
    if best is not None:
        return best
    # No overlap at all: the window is offscreen or the arrangement changed
    # under us. Fall back to the nearest display by centre distance rather
    # than returning None, because a caller with a coordinate needs a screen.
    centre = (wx + ww / 2.0, wy + wh / 2.0)
    nearest, shortest = None, float("inf")
    for display in displays():
        dx = max(display.x - centre[0], 0, centre[0] - display.right)
        dy = max(display.y - centre[1], 0, centre[1] - display.bottom)
        distance = dx * dx + dy * dy
        if distance < shortest:
            nearest, shortest = display, distance
    return nearest


def desktop_bounds() -> Tuple[float, float, float, float]:
    """The rectangle enclosing every display, in Quartz space."""
    found = displays()
    if not found:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(d.x for d in found), min(d.y for d in found),
            max(d.right for d in found), max(d.bottom for d in found))


def on_any_display(x: float, y: float) -> bool:
    return for_point(x, y) is not None


# ─── Between the coordinate spaces ───────────────────────────────────────────

def quartz_to_appkit(x: float, y: float) -> Tuple[float, float]:
    """Quartz (top-left of main) to AppKit (bottom-left of main)."""
    return (x, main_height() - y)


def appkit_to_quartz(x: float, y: float) -> Tuple[float, float]:
    """The same subtraction, which is its own inverse."""
    return (x, main_height() - y)


def image_to_quartz(u: float, v: float, window: Dict[str, Any]) -> Tuple[float, float]:
    """
    A normalised point inside a window capture to a point on the desktop.

    `u` and `v` run 0..1 across and down the capture. Because they are
    normalised, the display's scale factor has already cancelled out — this is
    why a Retina capture needs no special case, and why enlarging an image
    before OCR moves nothing.
    """
    return (float(window.get("x", 0)) + u * float(window.get("width", 0)),
            float(window.get("y", 0)) + v * float(window.get("height", 0)))


def clamp_to_display(x: float, y: float) -> Tuple[float, float]:
    """
    Pull a coordinate back onto the nearest display.

    A vision model asked for a coordinate will sometimes return one outside
    every screen. Drawing a marker there puts it nowhere; clamping puts it
    somewhere wrong but visible, which is worse. So callers should check
    `on_any_display` and refuse — this exists for the case where a point is a
    pixel or two over an edge because of rounding, not for rescuing a guess.
    """
    display = for_point(x, y)
    if display:
        return (x, y)
    display = for_window({"x": x, "y": y, "width": 1, "height": 1})
    if not display:
        return (x, y)
    return (min(max(x, display.x), display.right - 1),
            min(max(y, display.y), display.bottom - 1))


# ─── The cursor ──────────────────────────────────────────────────────────────

def cursor() -> Optional[Tuple[float, float]]:
    """
    Where the pointer is, in Quartz space.

    NSEvent reports it in AppKit space, so it comes back through the same
    single subtraction as everything else rather than growing a second
    convention.
    """
    try:
        from AppKit import NSEvent
    except ImportError:
        return None
    try:
        point = NSEvent.mouseLocation()
        return appkit_to_quartz(float(point.x), float(point.y))
    except Exception:
        return None


def cursor_display() -> Optional[Display]:
    spot = cursor()
    return for_point(*spot) if spot else None


# ─── Capturing a whole display, or part of one ───────────────────────────────

def _temp(prefix: str) -> str:
    return os.path.join(tempfile.gettempdir(),
                        f"diablo_{prefix}_{int(time.time() * 1000)}.png")


def capture_display(display: Display) -> Optional[str]:
    """One display, whole, by its `screencapture -D` index."""
    path = _temp(f"d{display.index}")
    try:
        proc = subprocess.run(
            ["screencapture", "-x", "-o", f"-D{display.index}", path],
            capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    return path


def capture_region(x: float, y: float, width: float, height: float) -> Optional[str]:
    """
    A rectangle of the desktop, in Quartz points.

    `screencapture -R` takes its rect in the same top-left global space that
    CGWindowList reports, so a region on a display at negative x needs no
    special case — which is the whole reason Quartz is the canonical space.
    """
    if width <= 0 or height <= 0:
        return None
    path = _temp("region")
    rect = f"-R{x:.0f},{y:.0f},{width:.0f},{height:.0f}"
    try:
        proc = subprocess.run(["screencapture", "-x", "-o", rect, path],
                              capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    return path


def capture_everything() -> List[Tuple[Display, str]]:
    """
    Every display, one capture each.

    Deliberately not one image. `screencapture -x out.png` on a two-display
    machine writes one file holding the main display only, returns 0, and says
    nothing — the failure this module was written to end. Separate captures
    also keep each display's own scale factor intact, which a stitched image
    could not.
    """
    results = []
    for display in displays():
        path = capture_display(display)
        if path:
            results.append((display, path))
    return results


def describe_arrangement() -> str:
    """The display setup in one sentence, for a person."""
    found = displays()
    if not found:
        return "I cannot see any displays."
    if len(found) == 1:
        one = found[0]
        return (f"One display, {int(one.width)} by {int(one.height)}"
                f"{', Retina' if one.scale >= 2 else ''}.")
    parts = [d.describe() for d in found]
    return f"{len(found)} displays: " + ", and the ".join(
        [parts[0]] + [p for p in parts[1:]]) + "."


if __name__ == "__main__":
    for d in displays():
        print(f"  -D{d.index}  id={d.id:<10} origin=({d.x:.0f},{d.y:.0f}) "
              f"{d.width:.0f}x{d.height:.0f} @{d.scale}x "
              f"{'MAIN' if d.is_main else '    '}  {d.describe()}")
    print()
    print("  desktop bounds:", desktop_bounds())
    print("  cursor:", cursor(), "on", (cursor_display() or Display(0,0,0,0,0,1,False,0)).describe())
    print("  arrangement:", describe_arrangement())
