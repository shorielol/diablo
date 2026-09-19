"""
Diablo's mark, drawn rather than exported — and the app icon built from it.

The mark is two strokes: a short vertical SOURCE, and one arc, the FRONT,
facing out from it. Something that is there, and the signal leaving it. It is
drawn here once, and everything that shows Diablo draws this function — the
floating presence, the guidance marker, the menu bar glyph and the app icon —
so there is one mark and not four that drift apart.

It is one form with many states, not a set of icons. State changes the
geometry: how far the front stands from the source, how long it is, which way
the whole mark faces, where the front has travelled round to, and how bright
it is. At rest the front faces right and the mark is still.

Two things about Mac icons are worth being explicit about:

  The corner is not a circle. Apple's icon shape is a squircle — a superellipse
  — and a plain rounded rectangle sits next to it looking slightly swollen at
  the corners. It is built here from the actual curve.

  Small sizes are not the large one shrunk. Below 32 points the strokes get
  heavier and the front stands a little further off, so the two parts stay two
  parts instead of closing into a blot.

Run it directly to write ~/.diablo/assets/Diablo.icns.
"""

import math
import os
import shutil
import subprocess
import sys
import tempfile

from AppKit import (
    NSBezierPath, NSBitmapImageRep, NSColor, NSGraphicsContext, NSImage,
    NSPNGFileType,
)
from Foundation import NSAffineTransform, NSMakeRect, NSPoint

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
ASSETS = os.path.join(HOME, "assets")

# The icon tile: graphite, not black and not coloured. The mark carries the
# identity; the tile only has to sit quietly among other Mac icons.
INK = (0.090, 0.090, 0.102)      # the tile, bottom
INK_TOP = (0.176, 0.176, 0.192)  # its top edge

# ── The mark's geometry, in its own unit square centred on (0, 0) ──────────
#
# Every number that defines the mark is here. The source sits left of centre
# and the front's centre of curvature is the source, so rotating the whole
# mark about (0, 0) turns it about its visual middle rather than swinging it.
SOURCE_X = -0.16      # where the source stands
SOURCE_HALF = 0.22    # half its length
FRONT_RADIUS = 0.30   # distance from source to front, at rest
FRONT_SPAN = 52.0     # half the front's arc, in degrees, at rest
WEIGHT = 0.09         # stroke, as a fraction of the mark's size


def weight_for(size: float) -> float:
    """Heavier strokes at small sizes, so the mark survives 16 points."""
    if size <= 18:
        return 0.125
    if size <= 32:
        return 0.105
    return WEIGHT

# Every icon size Finder, the Dock, Spotlight and the About box ask for.
SIZES = [
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
]


def _colour(rgb, alpha=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, alpha)


def _squircle(x, y, side, n=5.0, steps=240):
    """
    Apple's icon outline: a superellipse, |u|^n + |v|^n = 1.

    The corner is not a circle. n=4 is a rounded rectangle you can still see
    the circle in; n=5 is close to the macOS mask, and a plain rounded rect
    sits next to real icons in the Dock looking swollen at the corners.
    """
    a = b = side / 2.0
    cx, cy = x + a, y + b
    path = NSBezierPath.bezierPath()
    for i in range(steps):
        t = 2.0 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        px = cx + a * math.copysign(abs(ct) ** (2.0 / n), ct)
        py = cy + b * math.copysign(abs(st) ** (2.0 / n), st)
        (path.moveToPoint_ if i == 0 else path.lineToPoint_)(NSPoint(px, py))
    path.closePath()
    return path


# ─── The mark ────────────────────────────────────────────────────────────────

def draw_signal(cx: float, cy: float, size: float, ink,
                radius: float = FRONT_RADIUS, span: float = FRONT_SPAN,
                heading: float = 0.0, turn: float = 0.0, weight: float = None,
                front_ink=None, shift: float = 0.0, side: float = 1.0) -> None:
    """
    The mark, into whatever context is current, centred on (cx, cy).

    `size` is the side of the mark's square in points. `radius` and `span`
    are the front's distance from the source and half its length. `side` is
    +1 facing right, -1 mirrored to face left, and anything between while it
    turns round (the mark narrows to its source and opens out the other way).
    `heading` leans the mark (degrees, anticlockwise, in its own frame; kept
    small by the caller so the mark never turns into something else); `turn`
    carries the front round the source without moving the source — how
    thinking travels. `shift` slides the mark sideways along its own axis,
    for the one small shake an error gets. Everything is in the mark's own
    units so it scales without changing shape.
    """
    weight = weight_for(size) if weight is None else weight
    context = NSGraphicsContext.currentContext()
    context.saveGraphicsState()
    frame = NSAffineTransform.transform()
    frame.translateXBy_yBy_(cx, cy)
    frame.scaleXBy_yBy_(side if abs(side) > 0.02 else 0.02, 1.0)
    frame.rotateByDegrees_(heading)
    frame.scaleBy_(size)
    frame.translateXBy_yBy_(shift, 0.0)
    frame.concat()

    source = NSBezierPath.bezierPath()
    source.moveToPoint_(NSPoint(SOURCE_X, -SOURCE_HALF))
    source.lineToPoint_(NSPoint(SOURCE_X, SOURCE_HALF))
    source.setLineWidth_(weight)
    source.setLineCapStyle_(1)                       # round
    ink.set()
    source.stroke()

    if span > 0.5 and radius > 0.01:
        front = NSBezierPath.bezierPath()
        front.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSPoint(SOURCE_X, 0.0), radius, turn - span, turn + span)
        front.setLineWidth_(weight)
        front.setLineCapStyle_(1)
        (front_ink or ink).set()
        front.stroke()
    context.restoreGraphicsState()


# macOS 26 composites app icons itself: it takes whatever the .icns contains,
# masks it to the system shape and draws the tile and shadow around it. An icon
# that also draws its own rounded tile and margin ends up nested inside a second
# one, which is what the first build of this looked like — a small dark square
# floating inside a large grey square. Before 26 nothing composites anything and
# an icon that does not draw its own shape is a hard-edged rectangle.
#
# So the shape is drawn or not drawn depending on which of those is true, and
# the check is the OS version rather than a guess.
def _system_draws_the_tile() -> bool:
    try:
        import platform
        return int(platform.mac_ver()[0].split(".")[0]) >= 26
    except Exception:
        return False


TILED = _system_draws_the_tile()


def _draw(size: int, tiled: bool = None) -> None:
    """One icon, drawn into whatever context is current."""
    tiled = TILED if tiled is None else tiled

    if tiled:
        # Edge to edge. The system mask does the corners, and artwork that
        # stops short of them leaves a visible gap inside its own tile.
        inset, side = 0.0, float(size)
        shape = NSBezierPath.bezierPathWithRect_(NSMakeRect(0, 0, size, size))
    else:
        # Apple's older grid: the artwork takes about 80% of the canvas and
        # leaves the margin every other icon in the Dock also leaves.
        inset = size * 0.098
        side = size - inset * 2
        shape = _squircle(inset, inset, side)

    context = NSGraphicsContext.currentContext()

    # The tile: a short vertical lift rather than a flat fill, which is what
    # stops a dark icon reading as a hole in the Dock. No colour wash: the
    # mark is the only thing in the icon that is anything.
    context.saveGraphicsState()
    shape.addClip()
    from AppKit import NSGradient
    NSGradient.alloc().initWithStartingColor_endingColor_(
        _colour(INK), _colour(INK_TOP)).drawInRect_angle_(
            NSMakeRect(0, 0, size, size), 90.0)
    context.restoreGraphicsState()

    # The mark, at the optical centre and a little over half the tile: big
    # enough to own the icon, small enough to leave it air.
    draw_signal(size / 2.0, size / 2.0, side * (0.60 if tiled else 0.64),
                _colour((0.96, 0.96, 0.97), 1.0),
                weight=weight_for(size))

    # A hairline on the edge, when we own the edge. White and barely there:
    # the crimson stroke the first version used came out bright pink at 1024
    # and was the loudest thing in the icon.
    if not tiled and size >= 32:
        _colour((1, 1, 1), 0.10).set()
        shape.setLineWidth_(max(1.0, size * 0.004))
        shape.stroke()


def render(size: int, path: str) -> str:
    """One PNG, at exactly `size` points and one pixel per point."""
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0)
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)
    _draw(size)
    NSGraphicsContext.currentContext().flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(NSPNGFileType, {})
    data.writeToFile_atomically_(path, True)
    return path


def build(destination: str = "") -> str:
    """Write Diablo.icns and return where it went."""
    destination = destination or os.path.join(ASSETS, "Diablo.icns")
    os.makedirs(os.path.dirname(destination), exist_ok=True)

    staging = tempfile.mkdtemp(prefix="diablo-icon-")
    iconset = os.path.join(staging, "Diablo.iconset")
    os.makedirs(iconset)
    try:
        for size, name in SIZES:
            render(size, os.path.join(iconset, name))
        # iconutil ships with macOS; there is nothing to install.
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", destination],
                       check=True, capture_output=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return destination


if __name__ == "__main__":
    if "--png" in sys.argv:
        # For looking at it without a Dock in the way.
        out = os.path.join(ASSETS, "icon-1024.png")
        os.makedirs(ASSETS, exist_ok=True)
        print(render(1024, out))
    else:
        path = build()
        print(f"{path}  ({os.path.getsize(path) / 1024:.0f} KB)")
