#!/usr/bin/env python3
"""
B.5 — show the thing's own shape, not a box round it.

B.4 found the welfare-loss triangle exactly, from its pixels, and then threw
the shape away: regions.candidates built the triangle's mask, kept its
bounding box, and everything after that — the candidate, pointing, the
bridge, the shell, the guide, the overlay — carried x, y, w, h. The overlay
drew a rounded rectangle half of which was empty background.

Nothing here finds anything new. The contour comes from the mask B.4 already
built, travels as one optional field beside the box everyone already passes,
and is drawn by the overlay that already draws the outline. A caller that
sends only a box gets exactly what it got before.

No screen, no model: every picture is synthetic and every shape is known.
"""

import json
import os
import shutil
import sys
import threading
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402
import test_grounding_geometry as geo  # noqa: E402  (the synthetic diagrams)

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

import app_shell  # noqa: E402
from skills import (computer_use, display, grounding, guidance, lesson,  # noqa: E402
                    perception, pointing, regions, screen_context, target, teaching)


def polygon_mask(points, shape):
    """Rasterise a closed polygon (x, y pixel coordinates) to a boolean mask."""
    h, w = shape
    yy, xx = np.mgrid[:h, :w]
    px, py = xx + 0.5, yy + 0.5
    inside = np.zeros(shape, bool)
    pts = list(points)
    for i in range(len(pts)):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % len(pts)]
        crosses = ((y1 > py) != (y2 > py))
        with np.errstate(divide="ignore", invalid="ignore"):
            at = (x2 - x1) * (py - y1) / (y2 - y1) + x1
        inside ^= crosses & (px < at)
    return inside


def iou(a, b):
    return float((a & b).sum()) / float((a | b).sum() or 1)


def found_for(img, colour_close_to):
    """The candidate whose colour is nearest the one asked for."""
    found = regions.candidates(img, min_edge=24)
    return min(found, key=lambda r: sum((a - b) ** 2 for a, b in
                                        zip(r.colour, colour_close_to))), found


def run() -> int:
    r = Results()

    # ── A, C, D, K. The contour survives, and it is the shape ───────────────
    r.head("A-D  the area keeps its outline")
    with guard(r, "the welfare-loss triangle"):
        img = geo.diagram(grain=8)
        welfare, _ = found_for(img, (169, 169, 171))
        true_mask = polygon_mask([(313, 281), (372, 281), (372, 362)], img.shape[:2])
        r.check("the area carries an outline, not only a box",
                len(welfare.outline) >= 3, str(welfare.outline))
        r.check("  a handful of points, not a pixel staircase",
                3 <= len(welfare.outline) <= 10, f"{len(welfare.outline)} points")
        drawn = polygon_mask(welfare.outline, img.shape[:2])
        r.check("  that covers the triangle (IoU >= 0.85)",
                iou(drawn, true_mask) >= 0.85, f"{iou(drawn, true_mask):.3f}")
        box = np.zeros(img.shape[:2], bool)
        box[welfare.top:welfare.bottom, welfare.left:welfare.right] = True
        r.check("  and is far closer to it than the box was",
                iou(drawn, true_mask) > iou(box, true_mask) + 0.3,
                f"shape {iou(drawn, true_mask):.2f} vs box {iou(box, true_mask):.2f}")
        for corner in ((313, 281), (372, 281), (372, 362)):
            near = min(((x - corner[0]) ** 2 + (y - corner[1]) ** 2) ** .5
                       for x, y in welfare.outline)
            r.check(f"  a vertex within 4px of corner {corner}", near <= 4.0, f"{near:.1f}px")
    with guard(r, "consumer and producer surplus"):
        img = geo.diagram(grain=8)
        for colour, corners in (((183, 206, 234), [(45, 138), (300, 324), (45, 324)]),
                                ((178, 219, 200), [(45, 506), (300, 324), (45, 324)])):
            region, _ = found_for(img, colour)
            score = iou(polygon_mask(region.outline, img.shape[:2]),
                        polygon_mask(corners, img.shape[:2]))
            r.check(f"triangle from {corners[0]}: outline IoU {score:.2f} >= 0.85",
                    score >= 0.85)
    with guard(r, "an irregular, concave area"):
        img = np.full((700, 900, 3), 255, np.uint8)         # the L is not the page
        img[60:340, 60:160] = (140, 110, 200)              # an L: tall stroke ...
        img[240:340, 60:420] = (140, 110, 200)             # ... and a long foot
        region, _ = found_for(img, (140, 110, 200))
        truth = np.zeros(img.shape[:2], bool)
        truth[60:340, 60:160] = True
        truth[240:340, 60:420] = True
        drawn = polygon_mask(region.outline, img.shape[:2])
        box = np.zeros(img.shape[:2], bool)
        box[region.top:region.bottom, region.left:region.right] = True
        r.check("the outline follows the notch (IoU >= 0.9)", iou(drawn, truth) >= 0.9,
                f"{iou(drawn, truth):.3f}")
        r.check("  where a box would add the empty corner",
                iou(box, truth) < 0.65 and iou(drawn, truth) > iou(box, truth) + 0.25,
                f"box {iou(box, truth):.2f}")
    with guard(r, "simplification is stable"):
        a, _ = found_for(geo.diagram(grain=8, seed=1), (169, 169, 171))
        b, _ = found_for(geo.diagram(grain=8, seed=2), (169, 169, 171))
        same, _ = found_for(geo.diagram(grain=8, seed=1), (169, 169, 171))
        r.check("the same picture gives the same outline", a.outline == same.outline)
        spread = max(min(((x - u) ** 2 + (y - v) ** 2) ** .5 for u, v in b.outline)
                     for x, y in a.outline)
        r.check("different grain moves no vertex more than 3px", spread <= 3.0,
                f"{spread:.1f}px")

    # ── B, F, G, H, I, J. What is drawn, chosen from the geometry ──────────
    r.head("B-J  the presentation follows the geometry, not the words")
    with guard(r, "teaching.outline_for"):
        tri = ((0.0, 0.0), (60.0, 0.0), (60.0, 80.0))
        r.check("an area with a real shape: its outline",
                teaching.outline_for(target.REGION, 60.0, 80.0, tri) == tri)
        bar = ((0.0, 0.0), (60.0, 0.0), (60.0, 200.0), (0.0, 200.0))
        r.check("a bar is its rectangle: the existing outline, no polygon",
                teaching.outline_for(target.REGION, 60.0, 200.0, bar) == ())
        r.check("no outline: the box, as before (fallback)",
                teaching.outline_for(target.REGION, 60.0, 80.0, None) == ())
        r.check("a crossing-sized place is a point, whatever its outline",
                teaching.outline_for(target.REGION, 40.0, 40.0,
                                     ((0.0, 0.0), (40.0, 0.0), (40.0, 40.0))) == ())
        r.check("a control keeps its ring",
                teaching.outline_for(target.CONTROL, 60.0, 80.0, tri) == ())
        r.check("text keeps its treatment",
                teaching.outline_for(target.TEXT, 60.0, 80.0, tri) == ())
        r.check("broken geometry is not drawn",
                teaching.outline_for(target.REGION, 60.0, 80.0,
                                     ((0.0, 0.0), (float("nan"), 1.0), (3.0, 4.0))) == ()
                and teaching.outline_for(target.REGION, 60.0, 80.0, ((0.0, 0.0),)) == ())
        r.check("an outline that strays outside its own box is not drawn",
                teaching.outline_for(target.REGION, 60.0, 80.0,
                                     ((0.0, 0.0), (300.0, 0.0), (60.0, 80.0))) == ())
    with guard(r, "thin things have no outline to invent"):
        img = np.full((400, 600, 3), 255, np.uint8)
        for i in range(500):
            img[int(50 + i * 0.6): int(50 + i * 0.6) + 3, 50 + i] = (200, 40, 40)
        r.check("a curve is not an area, so no contour is made for it",
                regions.candidates(img, min_edge=24) == [])

    # ── L. Padding ─────────────────────────────────────────────────────────
    r.head("L  a small margin outward, not a box")
    with guard(r, "guidance.outline"):
        tri = [(100.0, 100.0), (160.0, 100.0), (160.0, 180.0)]
        out = guidance.outline(tri, 3.0)
        cx = sum(p[0] for p in tri) / 3.0
        cy = sum(p[1] for p in tri) / 3.0
        grew = [((o[0] - cx) ** 2 + (o[1] - cy) ** 2) ** .5
                - ((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** .5 for p, o in zip(tri, out)]
        r.check("every vertex moves out by the margin",
                all(abs(g - 3.0) < 1e-6 for g in grew), str(grew))
        r.check("  and it is still a triangle", len(out) == 3)
        r.check("an empty outline stays empty", guidance.outline([], 3.0) == [])

    # ── The chain: grounding -> pointing -> bridge -> shell -> guide ────────
    r.head("the outline travels the existing chain, and only when there is one")
    shape = ((440.0, 567.0), (499.0, 567.0), (499.0, 651.0))
    with guard(r, "grounding keeps it"):
        saved = (grounding._crop_for, grounding._anchor_for, target.scope_for,
                 display.capture_region, screen_context._for_vision,
                 screen_context.read_image)
        source = geo.save_png(geo.diagram(grain=8), "shapes.png")
        counter = [0]

        def capture(*a):
            counter[0] += 1
            fresh = os.path.join(geo.TMP, f"shape_{counter[0]}.png")
            shutil.copy(source, fresh)
            return fresh
        crop = (100.0, 200.0, 852.0, 632.0)
        display.capture_region = capture
        screen_context._for_vision = lambda p: (p, False)
        screen_context.read_image = lambda *a, **k: json.dumps({"choice": "C"})
        grounding._crop_for = lambda scope, window, anchor, cursor: crop
        grounding._anchor_for = lambda wanted, window: None
        target.scope_for = lambda wanted: (target.WINDOW,)
        try:
            found = grounding._by_looking(
                target.read_question("welfare loss region", stated="region"),
                {"id": 1, "app": "X", "x": 100.0, "y": 172.0, "width": 852.0,
                 "height": 660.0, "sensitive": False})
        finally:
            (grounding._crop_for, grounding._anchor_for, target.scope_for,
             display.capture_region, screen_context._for_vision,
             screen_context.read_image) = saved
        c = found.candidate
        r.check("the chosen area arrives with its outline, in screen points",
                c is not None and bool(c.outline) and len(c.outline) >= 3, str(c))
        if c is not None and c.outline:
            r.check("  every point inside the area's own box",
                    all(c.x - 1 <= x <= c.x + c.width + 1 and c.y - 1 <= y <= c.y + c.height + 1
                        for x, y in c.outline))
            r.check("  and mapped by the existing mapper (crop origin + pixels)",
                    any(abs(x - (100.0 + 372.0)) <= 4 and abs(y - (200.0 + 362.0)) <= 4
                        for x, y in c.outline), str(c.outline))
    with guard(r, "pointing sends it; a box-only answer sends none"):
        sent = []
        saved = (grounding.ground, pointing.show)

        def show(x, y, label="", seconds=3.0, marker=True, width=0.0, height=0.0, **extra):
            sent.append({"w": width, "h": height, **extra})
            return ""
        wanted = target.read_question("welfare loss region", stated="region")
        common = dict(label="welfare loss", role="region", x=440.0, y=567.0, width=59.0,
                      height=84.0, confidence=0.85, sources=[target.REGION_SOURCE],
                      source_confidence={target.REGION_SOURCE: 0.85})
        withshape = perception.Candidate(outline=list(shape), **common)
        boxonly = perception.Candidate(**common)
        pointing.show = show
        answer = ""
        try:
            for cand in (withshape, boxonly):
                grounding.ground = lambda q, window=None, stated_kind="", c=cand: \
                    grounding.Ground(target=wanted, candidate=c, scope=target.WINDOW)
                answer = pointing.point_at("welfare loss region", kind="region")
        finally:
            grounding.ground, pointing.show = saved
        r.check("the shape goes with the box", len(sent) == 2
                and tuple(map(tuple, sent[0].get("shape", ()))) == shape
                and sent[0]["w"] == 59.0, str(sent))
        r.check("  and a box alone is sent exactly as before, with no shape key",
                len(sent) == 2 and "shape" not in sent[1], str(sent))
        r.check("nothing about outlines is said out loud",
                not any(w in answer.lower() for w in ("polygon", "outline", "contour",
                                                     "shape")), answer)

    # ── M, N, O, P, Q. The shell: one target, stale, cancel, reduced ────────
    r.head("M-Q  the shell draws the current step's shape, and only that")

    class Drawn:
        def __init__(self):
            self.shown, self.hid, self.closed = [], 0, 0

        def prepare(self, *a, **k):
            pass

        def show(self, x, y, label="", w=0.0, h=0.0, step="", on_light=False,
                 bare=False, shape=()):
            self.shown.append({"x": x, "y": y, "w": w, "h": h, "bare": bare,
                               "shape": tuple(map(tuple, shape or ()))})

        def hide(self):
            self.hid += 1

        def close(self):
            self.closed += 1

        def ready(self, timeout=5.0):
            return True

        def wait_for_click(self, *a, **k):
            return True

    def stand_in(reduced=False):
        from skills.motion import Presence
        shell = types.SimpleNamespace()
        shell.presence = Presence(home=(1461.0, 51.0), reduced=reduced)
        shell.presence.set_reduced(reduced)
        shell.wake = lambda: None
        shell.guide = Drawn()
        shell.lesson = lesson.Lesson()
        shell.cursor = teaching.CursorMotion(reduced=reduced)
        shell._watch_escape = lambda on: None
        shell._arrival = None
        for name in ("teach_begin", "teach_step", "teach_finish", "_release_arrival",
                     "_escape_pressed", "clear_target", "no_target", "_under_surface",
                     "approach"):
            setattr(shell, name, types.MethodType(getattr(app_shell.Shell, name), shell))
        return shell

    def settle(shell, seconds=4.0):
        for _ in range(int(seconds * 120)):
            shell.presence.step(1.0 / 120)
            shell.cursor.step(1.0 / 120)

    for reduced in (False, True):
        with guard(r, f"teach steps ({'reduced motion' if reduced else 'motion'})"):
            shell = stand_in(reduced)
            shell.teach_begin(2, seq=1)
            shell.teach_step(470.0, 609.0, 59.0, 84.0, label="welfare loss", index=1,
                             total=2, mode=teaching.REGION, seq=2,
                             arrived=threading.Event(), shape=shape)
            settle(shell)
            shown = shell.guide.shown
            r.check("the region's own shape is drawn when the cursor arrives",
                    bool(shown) and shown[-1]["shape"] == shape and shown[-1]["bare"],
                    str(shown))
            r.check("  once, with no box drawn beside it", len(shown) == 1, str(shown))
            hid = shell.guide.hid
            shell.teach_step(700.0, 300.0, 40.0, 40.0, label="equilibrium", index=2,
                             total=2, mode=teaching.POINT, seq=3,
                             arrived=threading.Event())
            r.check("the next step hides it before anything moves",
                    shell.guide.hid > hid)
            settle(shell)
            r.check("  and a point step draws no shape",
                    all(s["shape"] == () for s in shell.guide.shown[1:]),
                    str(shell.guide.shown))
            before = len(shell.guide.shown)
            shell.teach_step(470.0, 609.0, 59.0, 84.0, label="welfare loss", index=1,
                             total=2, mode=teaching.REGION, seq=2,
                             arrived=threading.Event(), shape=shape)
            settle(shell)
            r.check("a stale step draws nothing", len(shell.guide.shown) == before)
            # clear_target closes the overlay process outright, which takes
            # the shape with it; nothing temporary outlives it.
            shell.clear_target()
            r.check("cancelling clears it", shell.guide.closed >= 1)
        with guard(r, f"a single answer through approach ({'reduced' if reduced else 'motion'})"):
            shell = stand_in(reduced)
            shell.approach(470.0, 609.0, "welfare loss", seconds=0.1, w=59.0, h=84.0,
                           shape=shape)
            settle(shell, 6.0)
            r.check("the orb's own answer draws the shape too",
                    any(s["shape"] == shape for s in shell.guide.shown),
                    str(shell.guide.shown))
            shell = stand_in(reduced)
            shell.approach(470.0, 609.0, "Save", seconds=0.1)
            settle(shell, 6.0)
            r.check("  and a point answer is exactly what it was",
                    bool(shell.guide.shown)
                    and all(s["shape"] == () and s["w"] == 0.0 for s in shell.guide.shown),
                    str(shell.guide.shown))

    with guard(r, "the guide message"):
        messages = []
        guide = app_shell.Guide.__new__(app_shell.Guide)
        guide._send = lambda m: messages.append(m) or True
        guide.prepare = lambda *a, **k: None
        guide.arm = lambda *a, **k: None
        app_shell.Guide.show(guide, 470.0, 609.0, "welfare loss", 59.0, 84.0, shape=shape)
        app_shell.Guide.show(guide, 300.0, 200.0, "Save")
        r.check("the overlay is sent the points",
                messages and messages[0].get("shape") == [list(p) for p in shape],
                str(messages[:1]))
        r.check("  and a box-only message is unchanged",
                len(messages) == 2 and "shape" not in messages[1], str(messages[1:]))

    with guard(r, "Q  words, not geometry"):
        r.check("the step is still announced by name",
                teaching.announce("welfare loss", 3, 4) == "Step 3 of 4: welfare loss"
                and teaching.announce("welfare loss", 1, 1) == "Looking here: welfare loss")

    # ── W. The path carries it ─────────────────────────────────────────────
    r.head("W  a path step carries its shape")
    with guard(r, "waypoint"):
        cand = perception.Candidate(label="welfare loss", role="region", x=440.0, y=567.0,
                                    width=59.0, height=84.0, confidence=0.85,
                                    sources=[target.REGION_SOURCE], outline=list(shape))
        place = teaching.waypoint(cand, target.REGION, say="This is what is lost.")
        r.check("the waypoint keeps the outline",
                place is not None and place.shape == shape, str(place))
        small = perception.Candidate(label="equilibrium", role="region", x=680.0, y=400.0,
                                     width=40.0, height=40.0, confidence=0.85,
                                     sources=[target.REGION_SOURCE],
                                     outline=[(680.0, 400.0), (720.0, 400.0), (700.0, 440.0)])
        place = teaching.waypoint(small, target.REGION)
        r.check("a crossing-sized step is a point, with no shape",
                place is not None and place.mode == teaching.POINT and place.shape == ())
        # The live bridge, installed the way test_teaching installs it: every
        # voice_bridge hook present, notify recorded, nothing drawn.
        import diablo_app

        class Anything:
            def __getattr__(self, name):
                return lambda *a, **k: None

        sent = []
        saved = (app_shell.notify, pointing.show, teaching.CHANNEL)
        app_shell.notify = lambda event, **data: sent.append((event, data)) or True
        try:
            diablo_app._bridge_presence(Anything())
            pointing.show(470.0, 609.0, "welfare loss", 3.0, width=59.0, height=84.0,
                          shape=[list(p) for p in shape])
            pointing.show(300.0, 200.0, "Save", 3.0)
            channel = teaching.CHANNEL() if isinstance(teaching.CHANNEL, type) \
                else teaching.CHANNEL
            channel.step(teaching.waypoint(cand, target.REGION, say="x"), 1, 1)
        finally:
            app_shell.notify, pointing.show, teaching.CHANNEL = saved
        targets = [d for e, d in sent if e == "target"]
        r.check("a single answer's shape reaches the shell",
                len(targets) == 2
                and tuple(map(tuple, targets[0].get("shape", ()))) == shape,
                str(targets[:1]))
        r.check("  and a point answer reaches it exactly as before",
                len(targets) == 2 and "shape" not in targets[1], str(targets[1:]))
        step = [d for e, d in sent if e == "teach_step"]
        r.check("a path step's shape reaches the shell",
                bool(step) and tuple(map(tuple, step[-1].get("shape", ()))) == shape,
                str(step[-1] if step else sent))

    # ── V. Actions and controls ────────────────────────────────────────────
    r.head("V  controls are not turned into polygons")
    with guard(r, "the New Note control"):
        control = perception.Candidate(label="New Note", role="AXButton", x=819.0, y=337.0,
                                       width=36.0, height=32.0, confidence=1.0,
                                       sources=["accessibility"])
        r.check("an accessibility control has no outline",
                getattr(control, "outline", None) in (None, [], ()))
        r.check("  and would not be given one", teaching.outline_for(
            target.CONTROL, 36.0, 32.0, ((0.0, 0.0), (36.0, 0.0), (18.0, 32.0))) == ())

    # ── R, S. Safety ────────────────────────────────────────────────────────
    r.head("R-S  shapes are pictures")
    with guard(r, "source"):
        banned = ("CGEventPost", "CGEventCreateMouseEvent", "CGEventCreateKeyboardEvent",
                  "CGWarpMouseCursorPosition", "setMouseLocation", "_post_click")
        overlay_source = open(pointing.OVERLAY).read()
        for module in (regions, guidance, teaching, pointing, grounding):
            r.check(f"{module.__name__}: no synthetic input",
                    not any(b in open(module.__file__).read() for b in banned))
        r.check("overlay: no synthetic input", not any(b in overlay_source for b in banned))
        r.check("app_shell: no synthetic input",
                not any(b in open(app_shell.__file__).read() for b in banned))
        r.check("the overlay window still ignores the mouse",
                "setIgnoresMouseEvents_(True)" in overlay_source)
        r.check("computer use is off", computer_use.ENABLED is False)

    return r.report("shapes")


if __name__ == "__main__":
    sys.exit(run())
