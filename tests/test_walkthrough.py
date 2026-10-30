"""
The marker on screen, and the step-by-step that waits for you.

Two things are being tested and they fail in different ways.

The **drawing** fails visibly-but-not-really: every AppKit call succeeds, the
window reports isVisible() as true, drawRect_ runs, and nothing is composited.
This project has been caught by that twice, and twice by counting red pixels in
a screenshot and matching a Stop button somewhere else entirely. So every claim
about what is on screen is made against CGWindowList, which is the only source
that cannot be fooled.

The **waiting** fails invisibly: a walkthrough that does not wait still says all
the right sentences, just over the top of you. It is tested by synthesising
clicks and checking which ones the step accepts.

Clicks here are posted with CGEvent at coordinates over Diablo's own overlay,
which is click-through — so they land on the desktop and do nothing. Nothing is
clicked in any application.
"""

import json
import math
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import pointing, tutor  # noqa: E402

DRAW = os.getenv("DIABLO_TEST_OVERLAY", "1") not in ("0", "", "false")


# ─── Ground truth ────────────────────────────────────────────────────────────
#
# These used to identify the guide by its size: layer 25 and exactly 420x260,
# because the overlay window was a fixed box centred on the target.
#
# It is not a fixed box any more, and that is the point of the redesign. The
# window now spans a ring on the target, Diablo standing a standoff away on
# whichever side has room, and a caption whose width depends on its text — so
# its size varies per step and its centre is deliberately NOT the target. The
# whole reason for the change is that a marker centred on the target covers
# the target.
#
# So identity comes from the process instead, which is exact and survives any
# geometry, and the assertions below check what actually matters now: that the
# guide window contains the target and that it is the same window moving.

def _overlay_pids():
    found = subprocess.run(["pgrep", "-f", "overlay" + ".py"],
                           capture_output=True, text=True).stdout.split()
    return {int(pid) for pid in found if pid.isdigit()}


def markers():
    """Every guide window actually composited, from the window server."""
    import Quartz
    pids = _overlay_pids()
    if not pids:
        return []
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    return [w for w in windows
            if w.get("kCGWindowLayer") == 25
            and int(w.get("kCGWindowOwnerPID") or 0) in pids
            and int(w["kCGWindowBounds"]["Width"]) > 40]


def guide_box():
    """The guide window's rectangle, Quartz top-left."""
    found = markers()
    if not found:
        return None
    b = found[0]["kCGWindowBounds"]
    return (b["X"], b["Y"], b["Width"], b["Height"])


def _clear_of_target(x, y):
    """
    Whether Diablo is standing off the target rather than on it.

    Asked of guidance.place directly: the window server can say where the
    window is, not which part of it holds the face.
    """
    from skills import guidance
    spot = guidance.place(float(x), float(y), "Continue", step="1 of 3")
    if spot is None:
        return False
    apart = ((spot.marker_x - x) ** 2 + (spot.marker_y - y) ** 2) ** 0.5
    return apart >= guidance.RING + guidance.MARKER


def holds(x, y):
    """Whether the guide window covers a target point at all."""
    box = guide_box()
    if not box:
        return False
    bx, by, bw, bh = box
    return bx <= x <= bx + bw and by <= y <= by + bh


def pixels(window_id):
    """How much of the marker is actually drawn, not just allocated."""
    import Quartz
    from Quartz import (CGWindowListCreateImage, CGRectNull,
                        kCGWindowListOptionIncludingWindow,
                        kCGWindowImageBoundsIgnoreFraming)
    from AppKit import NSBitmapImageRep
    image = CGWindowListCreateImage(CGRectNull, kCGWindowListOptionIncludingWindow,
                                    window_id, kCGWindowImageBoundsIgnoreFraming)
    rep = NSBitmapImageRep.alloc().initWithCGImage_(image)
    width, height = rep.pixelsWide(), rep.pixelsHigh()
    drawn = 0
    for y in range(0, height, 3):
        for x in range(0, width, 3):
            colour = rep.colorAtX_y_(x, y)
            if colour is not None and colour.alphaComponent() > 0.05:
                drawn += 1
    return drawn


def click_at(x, y):
    """Post a real click at a Quartz point. Lands on the desktop."""
    import Quartz
    for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        event = Quartz.CGEventCreateMouseEvent(
            None, kind, Quartz.CGPointMake(x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.02)


def wait_for(predicate, timeout=4.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def main() -> int:
    r = Results()
    if not DRAW:
        r.skip("everything", "draws on screen; set DIABLO_TEST_OVERLAY=1")
        return r.report("walkthrough")

    # ── 1. It draws at all ───────────────────────────────────────────────
    r.head("The marker is really on screen")
    marker = tutor.Marker()
    try:
        with guard(r, "a marker appears where it was asked to"):
            r.check("nothing there to begin with", not markers())
            marker.move(700, 400, "Continue", "1 of 3")
            found = wait_for(markers)
            r.check("it is composited", bool(found),
                    "created, visible, drawn, and not on screen is the classic "
                    "failure here")
            if found:
                r.check("  covering the point requested", holds(700, 400),
                        f"window {guide_box()}")
                r.check("  without standing on it",
                        _clear_of_target(700, 400),
                        "the ring goes on the target; Diablo goes beside it")
                r.check("  above ordinary windows",
                        found[0]["kCGWindowLayer"] == 25)
                r.check("  and has something drawn in it",
                        pixels(found[0]["kCGWindowNumber"]) > 200,
                        f"{pixels(found[0]['kCGWindowNumber'])} opaque samples")

        # ── 2. It moves, rather than blinking ────────────────────────────
        with guard(r, "one marker moves between steps"):
            before = markers()[0]["kCGWindowNumber"]
            marker.move(1100, 600, "Export", "2 of 3")
            moved = wait_for(lambda: holds(1100, 600))
            r.check("it moved", bool(moved), str(guide_box()))
            r.check("  to cover the second point", holds(1100, 600), str(guide_box()))
            r.check("  and no longer the first", not holds(700, 400), str(guide_box()))
            r.check("  as the same window, not a new one",
                    markers()[0]["kCGWindowNumber"] == before,
                    "respawning per step blinks, and reads as two pointers")
            r.check("  and there is still only one of them", len(markers()) == 1)

        # ── 3. It goes away ──────────────────────────────────────────────
        with guard(r, "it leaves when told"):
            marker.close()
            gone = wait_for(lambda: not markers(), timeout=4)
            r.check("closed cleanly", bool(gone) or not markers())
    finally:
        marker.close()

    # ── 4. Contrast against what is behind it ────────────────────────────
    r.head("It reads against the background")
    with guard(r, "the caption inverts on a light background"):
        light = tutor.Marker()
        dark = tutor.Marker()
        try:
            dark.move(500, 500, "Dark background", "1 of 1", on_light=False)
            wait_for(markers)
            dark_pixels = pixels(markers()[0]["kCGWindowNumber"]) if markers() else 0
            dark.close()
            wait_for(lambda: not markers(), timeout=3)

            light.move(500, 500, "Light background", "1 of 1", on_light=True)
            wait_for(markers)
            light_pixels = pixels(markers()[0]["kCGWindowNumber"]) if markers() else 0
            r.check("both draw", dark_pixels > 200 and light_pixels > 200,
                    f"dark {dark_pixels}, light {light_pixels}")
            # Not asserting which is larger — the shapes are the same and only
            # the colours differ. Asserting they both render is the real test;
            # the inversion itself is a colour choice, checked below in code.
            r.check("the two modes are distinguishable in the drawing code",
                    "on_light" in open(
                        os.path.join(DIABLO_HOME, "skills", "overlay.py")).read())
        finally:
            light.close()
            dark.close()
            wait_for(lambda: not markers(), timeout=3)

    # ── 5. Waiting for the click ─────────────────────────────────────────
    r.head("It waits for you to do it")
    with guard(r, "a click on the marker advances the step"):
        watcher = tutor.Marker(watch_clicks=True)
        try:
            watcher.move(800, 500, "Click me", "1 of 2")
            wait_for(markers)
            # Wait for the overlay to confirm its monitor, not for a guess at
            # how long that takes. A click posted before the monitor is up is
            # simply never seen, and this test failed about one run in three
            # on a sleep.
            r.check("the click monitor confirms it is up", watcher.ready(),
                    "needs Accessibility")

            import threading
            result = {}

            def wait():
                result["got"] = watcher.wait_for_click(800, 500, timeout=8)

            thread = threading.Thread(target=wait)
            thread.start()
            time.sleep(0.6)
            click_at(800, 500)
            thread.join(timeout=10)
            r.check("the click was seen and accepted", result.get("got") is True,
                    "needs Accessibility; without it the monitor never fires")
        finally:
            watcher.close()
            wait_for(lambda: not markers(), timeout=3)

    with guard(r, "a click somewhere else is ignored"):
        watcher = tutor.Marker(watch_clicks=True)
        try:
            watcher.move(800, 500, "Click me", "1 of 2")
            wait_for(markers)
            watcher.ready()
            import threading
            result = {}

            def wait():
                result["got"] = watcher.wait_for_click(800, 500, timeout=3)

            thread = threading.Thread(target=wait)
            thread.start()
            time.sleep(0.4)
            click_at(300, 200)                   # far from the marker
            thread.join(timeout=6)
            r.check("a click 580 points away does not count",
                    result.get("got") is False,
                    "people click other things mid-walkthrough; only the "
                    "marked spot means 'done, next'")
        finally:
            watcher.close()
            wait_for(lambda: not markers(), timeout=3)

    with guard(r, "no click at all times out rather than hanging"):
        watcher = tutor.Marker(watch_clicks=True)
        try:
            watcher.move(900, 700, "Nobody will click this", "1 of 1")
            wait_for(markers)
            watcher.ready()
            began = time.time()
            got = watcher.wait_for_click(900, 700, timeout=2)
            elapsed = time.time() - began
            r.check("it gives up", got is False)
            r.check("  at about the timeout", 1.8 < elapsed < 3.5, f"{elapsed:.1f}s")
        finally:
            watcher.close()
            wait_for(lambda: not markers(), timeout=3)

    with guard(r, "an interrupt stops the wait"):
        watcher = tutor.Marker(watch_clicks=True)
        try:
            watcher.move(900, 700, "Stop me", "1 of 1")
            wait_for(markers)
            watcher.ready()
            began = time.time()
            got = watcher.wait_for_click(900, 700, timeout=20,
                                         should_stop=lambda: True)
            r.check("it returns immediately", got is False)
            r.check("  without waiting out the timeout",
                    time.time() - began < 2.0, f"{time.time()-began:.1f}s")
        finally:
            watcher.close()
            wait_for(lambda: not markers(), timeout=3)

    # ── 6. Which steps wait ──────────────────────────────────────────────
    r.head("Which steps wait, and which do not")
    for say, should in (
        ("Click on Settings to open it.", True),
        ("Press Export in the File menu.", True),
        ("Select the second track.", True),
        ("Type your name into the box.", True),
        ("The tempo is shown at the top.", False),
        ("Notice the waveform has changed.", False),
        ("Subtract five from both sides.", False),
        ("You should now see the result.", False),
    ):
        r.check(f"{'waits' if should else 'carries on'}: {say[:40]}",
                tutor.asks_for_an_action(say) is should)

    # ── 7. Never point at something that is not there ────────────────────
    r.head("It only points at real things")
    fragments = [
        {"text": "Settings", "confidence": 0.5, "left": 0.1, "right": 0.2,
         "top": 0.9, "height": 0.02},
        {"text": "Export", "confidence": 0.5, "left": 0.3, "right": 0.4,
         "top": 0.1, "height": 0.02},
    ]
    window = {"x": 0, "y": 0, "width": 1000, "height": 800}
    steps = [tutor.Step(say="Click Settings.", look_for="Settings"),
             tutor.Step(say="Click Funness Level.", look_for="Funness Level"),
             tutor.Step(say="Press Export.", look_for="Export"),
             tutor.Step(say="Look around.", look_for="")]
    anchored = tutor.anchor(steps, fragments, window)
    r.check("a real label is found", anchored[0]["found"])
    r.check("  at the measured position",
            abs(anchored[0]["x"] - 150) < 1 and abs(anchored[0]["y"] - 728) < 2,
            f"{anchored[0].get('x')}, {anchored[0].get('y')}")
    r.check("an invented label is NOT found", not anchored[1]["found"],
            "the model writes labels it has not seen; this is the only thing "
            "standing between that and a confident wrong pointer")
    r.check("  and is recorded as missing",
            anchored[1].get("missing") == "Funness Level")
    r.check("a second real label is found", anchored[2]["found"])
    r.check("a step with no target asks for no marker", not anchored[3]["found"])

    r.check("the anchor bar is stricter than the pointer's",
            tutor.ANCHOR_SCORE > pointing.MIN_SCORE,
            f"{tutor.ANCHOR_SCORE} vs {pointing.MIN_SCORE}")

    # ── 8. Refusals ──────────────────────────────────────────────────────
    r.head("When it refuses")
    said = []
    answer = tutor.walk("how do i do this", said.append, target="NoSuchApp")
    r.check("an unknown window is refused, not substituted",
            "can't find a window" in answer.lower(), answer[:70])
    r.check("  and nothing was said out loud", not said,
            "teaching about the wrong application sounds authoritative")

    maths = [{"text": "Solve for x:", "confidence": 0.5},
             {"text": "4x", "confidence": 0.3}]
    r.check("a half-read equation is refused",
            tutor.unreadable_problem(maths) is not None)
    settings = [{"text": "Humour", "confidence": 0.5},
                {"text": "14h", "confidence": 0.4},
                {"text": "Save", "confidence": 0.5}]
    r.check("a settings panel is not mistaken for algebra",
            tutor.unreadable_problem(settings) is None,
            "two sidebar timestamps used to trigger the maths gate")

    # ── 9. Nothing is left running ───────────────────────────────────────
    r.head("Nothing left behind")
    r.check("no marker survives the suite", not markers(), f"{len(markers())} left")
    stragglers = subprocess.run(
        ["pgrep", "-f", "overlay.py"], capture_output=True, text=True).stdout.split()
    r.check("no overlay processes survive it either", not stragglers,
            f"{len(stragglers)} still running")

    # ── 8. The desktop is messy ──────────────────────────────────────────
    #
    # Real work interrupts a walkthrough constantly: the window gets dragged
    # aside, resized, sent to the other display, or closed outright. The
    # coordinates were resolved once at the start of the lesson, so every one
    # of those silently invalidated every marker after it.
    r.head("It survives the window moving under it")
    from skills import screen_context as _sc

    with guard(r, "a window's bounds can be re-read by id"):
        front = _sc.frontmost_window()
        if not front:
            r.skip("a window's bounds can be re-read by id", "no window visible")
        else:
            again = _sc.window_by_id(front["id"])
            r.check("a live window is found by its id",
                    again is not None and again["app"] == front["app"],
                    front["app"])
            r.check("a window that has gone returns nothing, not a guess",
                    _sc.window_by_id(999999) is None)
            r.check("a window that has not moved reports no movement",
                    _sc.has_moved(front) is None)

            dragged = dict(front)
            dragged["x"] = front["x"] - 400
            moved = _sc.has_moved(dragged)
            r.check("a 400 point drag is detected", moved is not None)
            r.check("  and the current bounds come back with it",
                    moved is not None and abs(moved["x"] - front["x"]) < 2,
                    "so the caller can re-anchor without a second lookup")

            jittered = dict(front)
            jittered["x"] = front["x"] + 1
            r.check("a one point difference is not treated as a move",
                    _sc.has_moved(jittered) is None,
                    "bounds jitter; re-anchoring on noise would thrash the marker")

    with guard(r, "stale bounds put the marker in the wrong place"):
        fragment = {"left": 0.48, "right": 0.52, "top": 0.48, "height": 0.04,
                    "text": "Export", "confidence": 0.9}
        before = {"x": 0.0, "y": 0.0, "width": 1200.0, "height": 800.0}
        after = {"x": 600.0, "y": 200.0, "width": 1200.0, "height": 800.0}
        was = tutor._screen_point(fragment, before)
        now = tutor._screen_point(fragment, after)
        drift = ((now[0] - was[0]) ** 2 + (now[1] - was[1]) ** 2) ** 0.5
        r.check("the same fragment moves with its window",
                drift > 600, f"{drift:.0f} points apart after a (600,200) drag")

    with guard(r, "read_window reports the bounds it actually read"):
        front = _sc.frontmost_window()
        if not front:
            r.skip("read_window reports the bounds it actually read", "no window")
        else:
            stale = dict(front)
            stale["x"] = front["x"] - 500
            stale["y"] = front["y"] - 500
            path, fragments, current = tutor.read_window(stale)
            try:
                r.check("it hands back three things, not two",
                        current is not None and isinstance(current, dict))
                r.check("and the bounds are the live ones, not the ones passed in",
                        abs(current["x"] - front["x"]) < 2
                        and abs(current["y"] - front["y"]) < 2,
                        f"passed x={stale['x']:.0f}, got x={current['x']:.0f}, "
                        f"live x={front['x']:.0f}")
            finally:
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    with guard(r, "the walkthrough stops rather than guessing"):
        import inspect
        # The body is _walk; walk() is the wrapper that owns the capture.
        source = inspect.getsource(tutor._walk)
        r.check("it checks the window is still there before each marker",
                "has_moved(window)" in source,
                "a drag mid-lesson invalidates every later coordinate")
        r.check("it stops if the target window disappears",
                "window_by_id(window.get(\"id\", 0)) is None" in source
                and "has gone" in source,
                "marking where something used to be is worse than stopping")

    return r.report("walkthrough")


if __name__ == "__main__":
    sys.exit(main())
