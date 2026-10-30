#!/usr/bin/env python3
"""The Abstract Signal: Diablo's mark, one form with many states.

What is checked: every visual state is the same two strokes in a different
shape; the front never crosses the source (which is when the mark turns into
a letter or a symbol); idle is still; the thinking sweep stays bounded and
settles; the mark faces where it goes and what it means, the short way round;
reduced motion keeps the meaning without the movement; the real renderer and
icon draw in light and dark; and no face is left anywhere.
"""

import math
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

from AppKit import (NSApplication, NSAppearance, NSBitmapImageRep,  # noqa: E402
                    NSGraphicsContext)
from Foundation import NSAffineTransform  # noqa: E402

NSApplication.sharedApplication()
import app_shell  # noqa: E402
from skills import make_icon, motion  # noqa: E402
from skills.motion import Presence, Visual  # noqa: E402

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
HZ = 120.0


def run_for(p, seconds):
    for _ in range(int(round(seconds * HZ))):
        p.step(1.0 / HZ)


def draws(view, appearance):
    """Draw the real view into an offscreen bitmap. Returns a count of inked samples."""
    size = int(app_shell.CANVAS * 2)
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0)
    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    NSAppearance.setCurrentAppearance_(NSAppearance.appearanceNamed_(appearance))
    view.setAppearance_(NSAppearance.appearanceNamed_(appearance))
    t = NSAffineTransform.transform()
    t.scaleBy_(2.0)
    t.concat()
    view.drawRect_(view.bounds())
    NSGraphicsContext.restoreGraphicsState()
    return sum(1 for y in range(0, size, 3) for x in range(0, size, 3)
               if rep.colorAtX_y_(x, y).alphaComponent() > 0.5)


def run():
    r = Results()

    r.head("One form, many states")
    with guard(r, "every visual state is the same mark in a different shape"):
        missing = [v.value for v in Visual if v not in motion.SIGNAL]
        r.check("every visual state has a geometry", not missing, ", ".join(missing))
        shapes = {v: motion.SIGNAL[v] for v in (
            Visual.IDLE, Visual.LISTENING, Visual.THINKING, Visual.SPEAKING,
            Visual.WAITING, Visual.INTERRUPTED)}
        r.check("the states people must tell apart have different shapes",
                len(set(shapes.values())) == len(shapes))

    with guard(r, "the front never crosses the source"):
        worst, where = 0.0, ""
        for visual, (_radius, span, _lum) in motion.SIGNAL.items():
            sweep = motion.SWEEP.get(visual, (0.0, 0.0))[0]
            scan = 14.0 if visual is Visual.READING else 0.0
            cue = 20.0 if visual is Visual.SUCCESS else 0.0
            reach = span + sweep + scan + cue          # furthest the front's end turns
            if reach > worst:
                worst, where = reach, visual.value
        r.check("the front's ends stay on its own side (under 90 degrees)",
                worst < 85.0, f"widest reach {worst:.0f} deg ({where})")

    with guard(r, "idle is still"):
        p = Presence()
        run_for(p, 2.0)
        look = p.look()
        r.check("at rest nothing needs a frame", not p.busy())
        r.check("and the mark is not moving",
                look.turn == 0.0 and look.rhythm == 0.0 and look.lean == 0.0 and look.side == 1.0)

    with guard(r, "thinking sweeps, bounded, and settles"):
        p = Presence()
        p.set_runtime("processing")
        turns = []
        for _ in range(int(3 * HZ)):
            p.step(1 / HZ)
            turns.append(p.look().turn)
        amplitude = motion.SWEEP[Visual.THINKING][0]
        r.check("the front sweeps while thinking", max(turns) - min(turns) > amplitude,
                f"range {max(turns) - min(turns):.0f} deg")
        r.check("never further than its bound", max(abs(t) for t in turns) <= amplitude + 1e-6)
        p.set_runtime("idle")
        run_for(p, 1.5)
        r.check("and comes back to rest when thinking ends",
                abs(p.look().turn) < 0.01 and not p.busy())
        p.set_runtime("preparing")
        run_for(p, 1.0)
        r.check("preparing holds still", abs(p.look().turn) < 0.01)

    with guard(r, "speaking steps outward, only while speaking"):
        p = Presence()
        p.set_runtime("responding")
        peaks = []
        for _ in range(int(2 * HZ)):
            p.step(1 / HZ)
            peaks.append(p.look().rhythm)
        r.check("an outward rhythm while speaking", max(peaks) > 0.3, f"peak {max(peaks):.2f}")
        p.set_runtime("idle")
        run_for(p, 1.0)
        r.check("none after", p.look().rhythm == 0.0)

    r.head("Facing: a side and a lean, never a free rotation")
    with guard(r, "it faces where it goes, and what it means"):
        p = Presence(home=(1400.0, 60.0))
        p.move_to((200.0, 60.0))                        # straight left
        run_for(p, 0.4)
        r.check("travelling left, it has turned round to face left",
                p.look().side < -0.9, f"side {p.look().side:.2f}")
        p = Presence(home=(100.0, 60.0))
        p.approach_target((600.0, 400.0), facing=(600.0, 520.0))    # target straight above
        run_for(p, 3.0)
        look = p.look()
        r.check("a target straight above: it leans up as far as it may, no further",
                abs(look.lean - motion.LEAN_LIMIT) < 0.5 and look.side > 0.9,
                f"side {look.side:.2f}, lean {look.lean:.1f}")
        p.return_home()
        run_for(p, 3.0)
        r.check("home again, it faces forward, upright",
                abs(p.look().lean) < 0.5 and p.look().side > 0.99)

    with guard(r, "it never turns far enough to become a face"):
        worst, where = 0.0, ""
        for angle in range(0, 360, 45):
            p = Presence(home=(700.0, 500.0))
            dx, dy = 300 * math.cos(math.radians(angle)), 300 * math.sin(math.radians(angle))
            p.approach_target((700.0 + dx * 0.5, 500.0 + dy * 0.5),
                              facing=(700.0 + dx, 500.0 + dy))
            for _ in range(int(3 * HZ)):
                p.step(1 / HZ)
                if abs(p.look().lean) > worst:
                    worst, where = abs(p.look().lean), f"target at {angle} deg"
        r.check("across targets in all eight directions the lean stays within its limit",
                worst <= motion.LEAN_LIMIT + 1e-6, f"worst {worst:.1f} deg ({where})")
        r.check("a target straight below leans down on its side, not upside down",
                motion.face(0.0, -100.0) == (1.0, -motion.LEAN_LIMIT))
        r.check("a target behind it turns it round rather than over",
                motion.face(-100.0, 10.0)[0] == -1.0
                and abs(motion.face(-100.0, 10.0)[1]) < motion.LEAN_LIMIT)

    r.head("Reduced motion")
    with guard(r, "the meaning without the movement"):
        p = Presence(reduced=True)
        p.set_runtime("processing")
        run_for(p, 2.0)
        r.check("no sweep", abs(p.look().turn) < 1e-6)
        r.check("thinking is still its own shape",
                abs(p.look().span - motion.SIGNAL[Visual.THINKING][1]) < 0.5)
        p.set_runtime("responding")
        run_for(p, 1.0)
        r.check("no speaking rhythm", p.look().rhythm == 0.0)

    r.head("Size: present, not dominant")
    with guard(r, "one physical size, and everything spatial follows it"):
        from skills import guidance
        r.check("the disc is 37 points", motion.ORB == 37.0, f"{motion.ORB}")
        r.check("the shell draws the size motion owns",
                app_shell.ORB == motion.ORB
                and app_shell.CANVAS == motion.ORB + 2 * app_shell.HALO)
        r.check("where he stands is derived from the disc, not typed in",
                motion.STAND_RADIUS == motion.ORB / 2 + motion.STAND_ROOM)
        r.check("the stand room covers the disc at its largest (the press)",
                motion.ORB / 2 * motion.Scale.PRESS < motion.STAND_RADIUS)
        r.check("pointing, the target's ring is the larger thing on screen",
                2 * guidance.RING > motion.ORB * motion.Scale.ACTIVE,
                f"ring {2 * guidance.RING:.0f} pt, orb {motion.ORB * motion.Scale.ACTIVE:.1f} pt")

    with guard(r, "the mark keeps its proportions at this size"):
        size = app_shell.ORB * app_shell.MARK_FILL
        weight = make_icon.weight_for(size)
        # Interrupted collapses the front onto the source on purpose: that is its meaning.
        narrowest = min(radius for visual, (radius, _s, _l) in motion.SIGNAL.items()
                        if visual is not Visual.INTERRUPTED) - 0.03   # listening at full voice
        gap = (narrowest - weight) * size
        r.check("source and front stay two strokes (at least 2.5 pt apart)",
                gap >= 2.5, f"{gap:.2f} pt")
        widest = max(radius for radius, _s, _l in motion.SIGNAL.values()) + 0.05 + 0.05
        reach = (widest + make_icon.SOURCE_X + weight / 2) * size
        r.check("the mark never reaches the rim (a quarter of the radius stays quiet)",
                app_shell.ORB / 2 - reach >= app_shell.ORB / 8,
                f"{app_shell.ORB / 2 - reach:.1f} pt of quiet")
        r.check("no heavier stroke to make up for the smaller disc",
                weight <= 0.105, f"{weight}")

    r.head("The real renderer, in both appearances")
    with guard(r, "the orb draws every state, light and dark"):
        problems = []
        for appearance in ("NSAppearanceNameAqua", "NSAppearanceNameDarkAqua"):
            for state in ("idle", "listening", "processing", "preparing", "responding",
                          "error", "cold"):
                view = app_shell.OrbView.alloc().initWithFrame_(
                    ((0, 0), (app_shell.CANVAS, app_shell.CANVAS)))
                view.update_({"state": state})
                run_for(view.presence, 0.5)
                if draws(view, appearance) < 50:
                    problems.append(f"{state}/{appearance[12:]}")
        r.check("something is drawn in every case", not problems, ", ".join(problems))

    with guard(r, "the state is never only in the mark"):
        view = app_shell.OrbView.alloc().initWithFrame_(
            ((0, 0), (app_shell.CANVAS, app_shell.CANVAS)))
        view.update_({"state": "listening"})
        r.check("the accessibility label says the state",
                "listening" in (view.accessibilityLabel() or ""), view.accessibilityLabel())
        r.check("and it is announced as a button", view.accessibilityRole() == "AXButton")

    with guard(r, "the icon and the menu bar glyph"):
        folder = tempfile.mkdtemp(prefix="diablo-icon-test-")
        try:
            out = os.path.join(folder, "icon.png")
            ok = all(os.path.getsize(make_icon.render(size, out)) > 0
                     for size in (16, 32, 128, 1024))
        finally:
            shutil.rmtree(folder, ignore_errors=True)   # it used to stay behind
        r.check("the app icon renders at every size asked for", ok)
        r.check("the menu bar glyph is a template image, so the bar tints it",
                app_shell._status_icon().isTemplate())

    r.head("No face left anywhere")
    with guard(r, "native"):
        r.check("the character drawing is gone", not hasattr(make_icon, "draw_head"))
        shell = open(app_shell.__file__).read()
        r.check("nothing in the shell draws a face or a mouth",
                "draw_head" not in shell and "_mouth_openness" not in shell)
        overlay = open(os.path.join(HOME, "skills", "overlay.py")).read()
        r.check("the guidance marker draws the mark",
                "draw_signal" in overlay and "draw_head" not in overlay)
        r.check("pointing is not an error: the overlay has no red",
                "ACCENT" not in overlay and "systemRedColor" not in overlay
                and "colorWithCalibratedRed" not in overlay)
        r.check("its strokes carry a keyline, so they hold on light and dark content",
                "keyed_stroke(ring" in overlay and "keyed_stroke(piece" in overlay
                and "keyed_stroke(outline" in overlay)
    with guard(r, "the window"):
        mascot = open(os.path.join(HOME, "app", "mascot.js")).read()
        r.check("the panel's mark is two strokes, no face",
                "<line" in mascot and "<path" in mascot
                and not any(w in mascot for w in ("ellipse", "MOUTHS", "glove", "iris")))
        r.check("and keeps its export", "export function mascot" in mascot)

    return r.report("signal")


if __name__ == "__main__":
    sys.exit(run())
