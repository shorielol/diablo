"""
The app icon, drawn rather than exported.

There is no design file to keep in sync and no pipeline to install, because the
icon is the same character the pill draws and the same character the window
draws, described once here and rendered at every size macOS asks for.

Two things about Mac icons are worth being explicit about, because getting
either wrong is what makes an icon look like it came from somewhere else:

  The corner is not a circle. Apple's icon shape is a squircle — a superellipse
  — and a plain rounded rectangle sits next to it in the Dock looking slightly
  swollen at the corners. It is built here from the actual curve.

  Small sizes are not the large one shrunk. At sixteen points a grin is three
  grey pixels and two irises are one. Below 64 the face drops the mouth and the
  highlight and keeps only what still reads: the head, the hair, the streak and
  two lit eyes.

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

# The same values the window uses, so the three drawings of this character
# cannot drift apart.
INK = (0.055, 0.055, 0.067)      # #0E0E11  the squircle
INK_TOP = (0.094, 0.094, 0.110)  # #18181C  its top edge
HAIR = (0.082, 0.082, 0.102)
HAIR_LO = (0.043, 0.043, 0.055)
GUM = (0.557, 0.082, 0.149)
SKIN = (0.937, 0.878, 0.824)
CRIMSON = (0.878, 0.137, 0.235)  # #E0233C
GOLD = (0.949, 0.761, 0.188)     # #F2C230

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


# ─── Drawing in the mascot's own coordinates ─────────────────────────────────
#
# The character is defined once, in app/mascot.js, on a 120x120 grid with y
# running downwards like every other SVG. Re-deriving it in AppKit coordinates
# by hand is how three drawings of one face end up being three different faces,
# so instead the grid comes here intact: one transform flips and scales it, and
# every number below is the number that is in the SVG.

def _q(path, qx, qy, x, y):
    """
    A quadratic segment, which NSBezierPath does not have.

    Exact, not approximated: a quadratic is the cubic whose controls sit two
    thirds of the way from each end towards the single control point.
    """
    p0 = path.currentPoint()
    c1 = NSPoint(p0.x + 2.0 / 3.0 * (qx - p0.x), p0.y + 2.0 / 3.0 * (qy - p0.y))
    c2 = NSPoint(x + 2.0 / 3.0 * (qx - x), y + 2.0 / 3.0 * (qy - y))
    path.curveToPoint_controlPoint1_controlPoint2_(NSPoint(x, y), c1, c2)


def _ellipse(cx, cy, rx, ry):
    return NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(cx - rx, cy - ry, rx * 2, ry * 2))


def draw_head(detail: bool = True, mouth: float = 0.0,
              accent=None, eye=None) -> None:
    """
    The character, in the mascot's own 120-unit coordinates.

    Public because the icon is not the only thing that draws him. The orb on
    screen draws the same face from the same numbers, which is the only way
    three renderings of one character stay one character.

    `mouth` is how far open, 0 to 1. At 0 it is the fixed grin the icon uses;
    above about 0.05 it opens, which is what the orb animates while he speaks.
    """
    accent = accent or CRIMSON
    eye = eye or GOLD
    # Face
    _colour(SKIN).set()
    _ellipse(60, 54, 32, 31).fill()

    NSGraphicsContext.currentContext().saveGraphicsState()
    _ellipse(60, 54, 32, 31).addClip()

    # Hair as ONE filled mass with a scalloped fringe. Built from separate
    # sweeps it leaves a pale band across the brow that reads as a bandage.
    hair = NSBezierPath.bezierPath()
    hair.moveToPoint_(NSPoint(22, 14))
    hair.lineToPoint_(NSPoint(98, 14))
    hair.lineToPoint_(NSPoint(98, 44))
    _q(hair, 90, 60, 81, 47)
    _q(hair, 71, 62, 61, 48)
    _q(hair, 51, 62, 42, 47)
    _q(hair, 32, 60, 22, 46)
    hair.closePath()
    _colour(HAIR).set()
    hair.fill()
    # Stroked as well as filled. The fringe and the side panels are separate
    # paths that meet along the temple, and at icon scale the seam between
    # them opened into a pale sliver of forehead that looks like dust on the
    # lens. Half a unit of stroke closes it without changing the silhouette.
    hair.setLineWidth_(1.0)
    hair.stroke()

    # Side panels, so the hair frames the face instead of capping it. They
    # start at the crown rather than at y=30 like the SVG: at icon scale the
    # gap between the panel and the fringe above it showed up as a speck of
    # background on the temple, which at 1024 points looks like dust on the
    # lens.
    _colour(HAIR).set()
    NSBezierPath.bezierPathWithRect_(NSMakeRect(22, 14, 12, 72)).fill()
    NSBezierPath.bezierPathWithRect_(NSMakeRect(86, 14, 12, 72)).fill()
    _colour(HAIR_LO).set()
    NSBezierPath.bezierPathWithRect_(NSMakeRect(22, 14, 8, 72)).fill()

    # The streaks, inside the hair and stopping at the fringe. Crimson is the
    # one that identifies him; the gold one keeps the silhouette from being
    # symmetrical, which is what makes a face look drawn rather than generated.
    streak = NSBezierPath.bezierPath()
    streak.moveToPoint_(NSPoint(45, 16))
    _q(streak, 41, 30, 44.5, 47)
    _q(streak, 50.5, 33, 50, 16)
    streak.closePath()
    _colour(accent).set()
    streak.fill()

    if detail:
        second = NSBezierPath.bezierPath()
        second.moveToPoint_(NSPoint(64, 15))
        _q(second, 67, 29, 62.5, 45)
        _q(second, 57, 31, 59, 15)
        second.closePath()
        _colour(eye, 0.92).set()
        second.fill()

    NSGraphicsContext.currentContext().restoreGraphicsState()

    # Eyes: wider than tall. Taller than wide reads as goggles.
    _colour(HAIR_LO).set()
    _ellipse(47, 59, 10, 8.6).fill()
    _ellipse(73, 59, 10, 8.6).fill()

    # The upper lid, angled inward. This is the whole expression, and dropping
    # it is what turned the first attempt into a smiley face.
    _colour(HAIR).set()
    for left in (True, False):
        lid = NSBezierPath.bezierPath()
        if left:
            lid.moveToPoint_(NSPoint(36, 54))
            _q(lid, 47, 47, 58, 55)
            lid.lineToPoint_(NSPoint(58, 50))
            _q(lid, 47, 42, 36, 49)
        else:
            lid.moveToPoint_(NSPoint(84, 54))
            _q(lid, 73, 47, 62, 55)
            lid.lineToPoint_(NSPoint(62, 50))
            _q(lid, 73, 42, 84, 49)
        lid.closePath()
        lid.fill()

    _colour(eye).set()
    _ellipse(47, 60, 3.2, 5.6).fill()
    _ellipse(73, 60, 3.2, 5.6).fill()
    if detail:
        _colour(HAIR_LO).set()
        _ellipse(47, 60, 1.1, 4.2).fill()
        _ellipse(73, 60, 1.1, 4.2).fill()
        _colour((1, 1, 1), 0.95).set()
        _ellipse(49.6, 56.4, 1.6, 1.6).fill()
        _ellipse(75.6, 56.4, 1.6, 1.6).fill()

    # The mouth.
    #
    # Open and closed are two different shapes rather than one shape stretched:
    # a grin pulled downwards reads as a grimace, and a mouth that only ever
    # hinges along the top edge looks like a hand puppet. Open is a rounded
    # well with the upper teeth across the top of it.
    if mouth > 0.05:
        depth = 4.0 + 13.0 * min(1.0, mouth)
        width = 17.0 + 3.0 * min(1.0, mouth)
        well = NSBezierPath.bezierPath()
        well.moveToPoint_(NSPoint(60 - width, 70))
        _q(well, 60, 70 + depth * 1.5, 60 + width, 70)
        _q(well, 60, 66, 60 - width, 70)
        well.closePath()
        _colour(GUM).set()
        well.fill()
        upper = NSBezierPath.bezierPath()
        upper.moveToPoint_(NSPoint(60 - width, 70))
        _q(upper, 60, 66, 60 + width, 70)
        _q(upper, 60, 74.5, 60 - width, 70)
        upper.closePath()
        _colour((1, 1, 1), 1.0).set()
        upper.fill()
    elif detail:
        # The grin: wide, toothy, pleased with itself.
        gum = NSBezierPath.bezierPath()
        gum.moveToPoint_(NSPoint(43, 70))
        _q(gum, 60, 88, 77, 70)
        _q(gum, 60, 77, 43, 70)
        gum.closePath()
        _colour(GUM).set()
        gum.fill()

        teeth = NSBezierPath.bezierPath()
        teeth.moveToPoint_(NSPoint(43, 70))
        _q(teeth, 60, 87, 77, 70)
        teeth.closePath()
        _colour((1, 1, 1), 1.0).set()
        teeth.fill()
    else:
        # At 32 points and below the gum is two red pixels under the teeth and
        # the whole mouth turns into a smudge. A filled wedge survives.
        wedge = NSBezierPath.bezierPath()
        wedge.moveToPoint_(NSPoint(44, 70))
        _q(wedge, 60, 86, 76, 70)
        wedge.closePath()
        _colour((1, 1, 1), 0.95).set()
        wedge.fill()


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

    # Body. A short vertical lift rather than a flat fill, which is what stops
    # a dark icon reading as a hole punched in the Dock.
    context.saveGraphicsState()
    shape.addClip()
    from AppKit import NSGradient
    NSGradient.alloc().initWithStartingColor_endingColor_(
        _colour(INK), _colour(INK_TOP)).drawInRect_angle_(
            NSMakeRect(0, 0, size, size), 90.0)

    # A crimson wash behind the head. Tight, and low enough in the frame to
    # sit under the chin rather than around the whole tile — the first version
    # ran the gradient across the full rect at 20% and turned the background
    # maroon, which is a different icon entirely.
    glow = side * 1.30
    NSGradient.alloc().initWithStartingColor_endingColor_(
        _colour(CRIMSON, 0.15), _colour(CRIMSON, 0.0)).drawInRect_relativeCenterPosition_(
            NSMakeRect(size / 2 - glow / 2, size * 0.52 - glow / 2, glow, glow),
            (0.0, 0.0))

    # The character. Everything from here is written on the mascot's 120-unit
    # grid; this transform is the only place the two coordinate systems meet.
    #
    # Framed as a portrait rather than a figure on a background: the face is
    # placed at the optical centre and scaled until the hair runs off the top,
    # which is what stops an icon reading as a sticker floating in a box.
    scale = side * (1.62 if tiled else 1.86) / 120.0
    flip = NSAffineTransform.transform()
    flip.translateXBy_yBy_(size / 2.0 - 60 * scale, size * 0.50 + 56 * scale)
    flip.scaleXBy_yBy_(scale, -scale)
    context.saveGraphicsState()
    flip.concat()
    draw_head(detail=size >= 64)
    context.restoreGraphicsState()

    context.restoreGraphicsState()

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
