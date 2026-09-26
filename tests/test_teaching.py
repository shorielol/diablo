"""
Teaching: choosing how to show something, not only where it is.

Everything Diablo pointed at used to be shown the same way. The orb walked
over, a ring appeared, and for a button that is exactly right. For a shaded
region of a diagram the ring said less than the region's own outline did, and
for "show me how the subsidy creates welfare loss" there was no way to show a
relationship at all: the answer is three places in order, and one ring can
only ever be one place.

This suite covers the four ways of showing something that now exist, and the
rules that keep them honest:

  POINT    a precise place: the existing ring, unchanged
  REGION   an area: the existing outline, unchanged
  CURSOR   a small pointer Diablo draws and moves; never the real one
  PATH     several grounded places, one at a time, each explained

The two rules everything else depends on:

  NOTHING IS INVENTED   every place the cursor goes came from perception, a
                        grounded target or a fixture's published geometry.
  THE MOUSE IS YOURS    nothing here moves the real cursor or posts an event.

Deterministic throughout. The shell's handlers run against a stand-in with a
real Presence, a real Lesson and the real cursor motion, stepped at 120 Hz,
so ordering and staleness are tested without AppKit.
"""

import math
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

import app_shell  # noqa: E402
from skills import (computer_use, display, lesson, motion, perception,  # noqa: E402
                    target, teaching)
from skills.motion import Presence  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HZ = 120

# ─── A subsidy diagram, as perception would report it (Quartz, top-left) ─────
#
# Hand-set rather than read from a window, because this suite tests what
# happens to geometry once it exists. The live fixture publishes the same kind
# of rectangles and is exercised by the visual validation instead.

FIGURE = (500.0, 240.0, 520.0, 440.0)
PLACES = {
    "original equilibrium": (680.0, 400.0, 40.0, 40.0),
    "subsidised equilibrium": (742.0, 452.0, 40.0, 40.0),
    "subsidy wedge": (748.0, 380.0, 28.0, 92.0),
    "welfare loss": (700.0, 410.0, 64.0, 70.0),
    "consumer surplus": (510.0, 260.0, 190.0, 160.0),
    "producer surplus": (510.0, 430.0, 190.0, 150.0),
}
LEGEND = {"welfare loss": (1060.0, 300.0, 110.0, 18.0)}


def region(name, source="region", label=None):
    """A grounded region candidate, the way grounding returns one."""
    x, y, w, h = PLACES[name]
    return perception.Candidate(label=label or name, role="region", x=x, y=y,
                                width=w, height=h, confidence=0.84,
                                sources=[source], source_confidence={source: 0.84})


def control(name, x, y, w=60.0, h=22.0):
    return perception.Candidate(label=name, role="AXButton", x=x, y=y, width=w,
                                height=h, confidence=0.97, sources=["accessibility"])


def caption(name):
    """The printed label, as OCR finds it. The trap."""
    x, y, w, h = LEGEND[name]
    return perception.Candidate(label=name.capitalize(), role="text", x=x, y=y,
                                width=w, height=h, confidence=0.97, sources=["ocr"])


# ─── A stand-in shell ────────────────────────────────────────────────────────

class Drawn:
    """The overlay, as a list of what it was asked to draw."""

    def __init__(self):
        self.shown, self.hid, self.closed = [], 0, 0

    def prepare(self, *a, **k):
        pass

    def show(self, x, y, label="", w=0.0, h=0.0, step="", on_light=False,
             bare=False):
        self.shown.append({"x": x, "y": y, "label": label, "w": w, "h": h,
                           "step": step, "bare": bare})

    def hide(self):
        self.hid += 1

    def close(self):
        self.closed += 1

    def ready(self, timeout=5.0):
        return True

    def wait_for_click(self, x, y, radius=80.0, timeout=30.0, should_stop=None):
        return True


def stand_in(reduced=False):
    shell = types.SimpleNamespace()
    shell.presence = Presence(home=(1461.0, 51.0), reduced=reduced)
    shell.presence.set_reduced(reduced)
    shell.wake = lambda: None
    shell.guide = Drawn()
    shell.lesson = lesson.Lesson()
    shell.cursor = teaching.CursorMotion(reduced=reduced)
    shell.escape = []
    shell._watch_escape = lambda on: shell.escape.append(bool(on))
    shell._arrival = None
    for name in ("teach_begin", "teach_step", "teach_finish", "_release_arrival",
                 "_escape_pressed", "clear_target", "no_target", "guide_begin",
                 "guide_step", "guide_finish", "guide_waiting", "guide_acted",
                 "guide_verified", "approach", "_under_surface"):
        setattr(shell, name, types.MethodType(getattr(app_shell.Shell, name), shell))
    return shell


def run_frames(shell, seconds=3.0):
    for _ in range(int(seconds * HZ)):
        shell.presence.step(1.0 / HZ)
        shell.cursor.step(1.0 / HZ)


def centre(box):
    x, y, w, h = box
    return (x + w / 2.0, y + h / 2.0)


class Channel:
    """What run_path talks to, recorded. Arrivals are immediate."""

    def __init__(self, stop_after=None):
        self.events = []
        self.stop_after = stop_after
        self.stopped = False

    def begin(self, total):
        self.events.append(("begin", total))

    def step(self, waypoint, index, total):
        self.events.append(("step", index, waypoint.label, waypoint.mode))
        if self.stop_after is not None and index >= self.stop_after:
            self.stopped = True
        done = threading.Event()
        done.set()
        return done

    def await_action(self, waypoint, index, total):
        self.events.append(("await", index))
        return teaching.VERIFIED

    def finish(self, why):
        self.events.append(("finish", why))

    def cancelled(self):
        return self.stopped

    def wait(self, seconds):
        self.events.append(("dwell", round(seconds, 2)))
        return self.stopped


def run():
    r = Results()
    before = display.cursor()

    # ── E. Choosing how to show it ──────────────────────────────────────────
    r.head("E  What kind of thing it is decides how it is shown")
    with guard(r, "the policy"):
        pick = teaching.choose
        r.check("a control is a point", pick(target.CONTROL, (60, 22)).mode == teaching.POINT)
        r.check("a piece of text is a point", pick(target.TEXT, (300, 18)).mode == teaching.POINT)
        r.check("an area is a region", pick(target.REGION, (84, 144)).mode == teaching.REGION)
        r.check("a whole figure is a region", pick(target.FIGURE, (520, 440)).mode == teaching.REGION)
        r.check("the thing under the pointer is shown with the teaching cursor",
                pick(target.OBJECT, (80, 80)).mode == teaching.CURSOR)
        r.check("several grounded places in order are a path",
                pick(target.REGION, (84, 144), waypoints=3).mode == teaching.PATH)
        r.check("the whole screen is not shown at all",
                pick(target.SCREEN, None).mode == "",
                "SCREEN has never been pointable and still is not")

    with guard(r, "size is not a reason to shrink a region"):
        r.check("a very large region stays a region",
                pick(target.REGION, (900, 600)).mode == teaching.REGION,
                "downgrading a big area to a dot would lose the area")
        r.check("an area no bigger than a crossing is shown as the place it is",
                pick(target.REGION, (40, 40)).mode == teaching.POINT,
                f"both edges within {teaching.POINT_LIKE:.0f}pt")

    with guard(r, "movement words are a hint, not an order"):
        one = pick(target.REGION, (84, 144), question="show me how the subsidy works",
                   waypoints=1)
        r.check("one grounded place is shown as that place, however it was asked",
                one.mode == teaching.REGION, one.reason)
        r.check("the policy says it noticed",
                "one" in one.reason.lower() or "single" in one.reason.lower(), one.reason)
        r.check("the signal is recognised", teaching.asks_for_movement(
            "walk me through how the subsidy creates welfare loss"))
        r.check("and ordinary questions do not trip it",
                not teaching.asks_for_movement("where is welfare loss"))
        r.check("a control stays a point even when asked to 'show me how'",
                pick(target.CONTROL, (60, 22), question="show me how to save",
                     waypoints=1).mode == teaching.POINT)

    with guard(r, "every choice can be explained"):
        for kind, size, steps in ((target.CONTROL, (60, 22), 1),
                                  (target.REGION, (84, 144), 1),
                                  (target.OBJECT, (80, 80), 1),
                                  (target.REGION, (84, 144), 3)):
            if not pick(kind, size, waypoints=steps).reason:
                r.bad("every choice carries a reason", kind)
                break
        else:
            r.ok("every choice carries a reason", "4 of 4")

    # ── F G H. The economics diagram ────────────────────────────────────────
    r.head("F G H  The subsidy diagram")
    with guard(r, "F  welfare loss is a region, not its label"):
        asked = target.read_question("where is welfare loss")
        good = teaching.waypoint(region("welfare loss"), asked.kind, say="")
        r.check("the region becomes a waypoint", good is not None)
        r.check("  shown as a region", good is not None and good.mode == teaching.REGION)
        r.check("the printed label does not",
                teaching.waypoint(caption("welfare loss"), asked.kind) is None,
                "the one rule from Phase B, still holding for the new modes")

    with guard(r, "G  'point out equilibrium' is a point"):
        asked = target.read_question("point out equilibrium")
        spot = teaching.waypoint(region("original equilibrium"), asked.kind)
        r.check("the crossing is shown as the place it is",
                spot is not None and spot.mode == teaching.POINT,
                spot.mode if spot else "refused")

    with guard(r, "E  'what does this triangle mean' is an area"):
        asked = target.read_question("what does this triangle mean")
        spot = teaching.waypoint(region("welfare loss"), asked.kind)
        r.check("the triangle is outlined, not reduced to a dot",
                spot is not None and spot.mode in (teaching.REGION, teaching.CURSOR),
                spot.mode if spot else "refused")

    with guard(r, "H  the relationship is a path"):
        steps = [teaching.waypoint(region(n), target.REGION, say=s) for n, s in (
            ("original equilibrium", "Before the subsidy, the market cleared here."),
            ("subsidised equilibrium", "The subsidy shifts supply, so it clears here."),
            ("welfare loss", "These extra units cost more than they are worth."))]
        path = teaching.TeachingPath.of(steps)
        r.check("three grounded places make a three-step path", path.total == 3)
        r.check("  presented as a path",
                teaching.choose(target.REGION, None, waypoints=path.total).mode
                == teaching.PATH)
        r.check("  and each step keeps its own way of being shown",
                [s.mode for s in path.steps]
                == [teaching.POINT, teaching.POINT, teaching.REGION],
                str([s.mode for s in path.steps]))

    # ── W. Nothing is invented ──────────────────────────────────────────────
    r.head("W  A path cannot be made of invented places")
    with guard(r, "waypoints"):
        invented = perception.Candidate(label="welfare loss", role="region",
                                        x=700, y=410, width=64, height=70,
                                        confidence=0.9, sources=["model"])
        r.check("a rectangle a model wrote down is refused",
                teaching.waypoint(invented, target.REGION) is None)
        nowhere = perception.Candidate(label="x", role="region", x=float("nan"),
                                       y=1, width=40, height=40, sources=["region"])
        r.check("a NaN is refused", teaching.waypoint(nowhere, target.REGION) is None)
        r.check("nothing is refused", teaching.waypoint(None, target.REGION) is None)
        r.check("a fixture's published geometry is accepted",
                teaching.waypoint(region("welfare loss", source="fixture"),
                                  target.REGION) is not None)

    with guard(r, "a hand-built waypoint cannot sneak into a path"):
        forged = teaching.Waypoint(label="welfare loss", say="", x=700, y=410,
                                   w=64, h=70, kind=target.REGION,
                                   mode=teaching.REGION, sources=("model",))
        real = teaching.waypoint(region("welfare loss"), target.REGION)
        path = teaching.TeachingPath.of([forged, real, None])
        r.check("only the grounded step survives", path.total == 1,
                f"{path.total} step(s)")

    # ── C N. The teaching cursor's motion ───────────────────────────────────
    r.head("C  The teaching cursor moves like the rest of Diablo")
    with guard(r, "at rest it is nowhere"):
        cursor = teaching.CursorMotion()
        r.check("a new cursor is hidden", not cursor.visible)
        r.check("  and asks for no frames", not cursor.busy(),
                "no continuous idle animation")

    with guard(r, "travel"):
        cursor = teaching.CursorMotion(origin=(1400.0, 60.0))
        arrived = []
        cursor.travel((600.0, 500.0), lambda: arrived.append(time.perf_counter()))
        r.check("it appears where Diablo is", cursor.visible
                and math.hypot(cursor.look().position[0] - 1400.0,
                               cursor.look().position[1] - 60.0) < 1.0)
        trail, frames = [], 0
        while cursor.busy() and frames < HZ * 4:
            cursor.step(1.0 / HZ)
            trail.append(cursor.look().position)
            frames += 1
        r.check("it arrives", len(arrived) == 1, f"{len(arrived)} arrival(s)")
        end = cursor.look().position
        r.check("exactly on the spot", math.hypot(end[0] - 600.0, end[1] - 500.0) < 0.5,
                f"{end[0]:.2f},{end[1]:.2f}")
        overshoot = any(p[0] < 600.0 - 0.5 or p[1] > 500.0 + 0.5 for p in trail)
        r.check("without overshooting", not overshoot,
                "critically damped, the same springs as the orb")
        took = frames / HZ
        r.check("in about the time the orb would take for the same trip",
                took <= motion.Timing.MOVE_MAX + motion.Timing.SETTLE + 0.25,
                f"{took:.2f}s for {math.hypot(800, 440):.0f}pt")
        r.check("and then it is still", not cursor.busy(),
                "the arrival cue plays once and stops")

    with guard(r, "elapsed time, not frame count"):
        fast, slow = teaching.CursorMotion(origin=(0.0, 0.0)), teaching.CursorMotion(origin=(0.0, 0.0))
        fast.travel((500.0, 300.0))
        slow.travel((500.0, 300.0))
        for _ in range(int(0.3 * 120)):
            fast.step(1.0 / 120)
        for _ in range(int(0.3 * 60)):
            slow.step(1.0 / 60)
        a, b = fast.look().position, slow.look().position
        r.check("120 Hz and 60 Hz are in the same place after the same time",
                math.hypot(a[0] - b[0], a[1] - b[1]) < 1.0,
                f"{math.hypot(a[0] - b[0], a[1] - b[1]):.3f}pt apart")

    with guard(r, "a new destination replaces the old one"):
        cursor = teaching.CursorMotion(origin=(0.0, 0.0))
        old, new = [], []
        cursor.travel((800.0, 600.0), lambda: old.append(1))
        for _ in range(10):
            cursor.step(1.0 / HZ)
        cursor.travel((200.0, 150.0), lambda: new.append(1))
        for _ in range(HZ * 3):
            cursor.step(1.0 / HZ)
        r.check("the superseded arrival never fires", old == [])
        r.check("the new one fires once", new == [1])

    with guard(r, "hiding"):
        cursor = teaching.CursorMotion(origin=(0.0, 0.0))
        late = []
        cursor.travel((300.0, 300.0), lambda: late.append(1))
        cursor.step(1.0 / HZ)
        cursor.hide()
        for _ in range(HZ):
            cursor.step(1.0 / HZ)
        r.check("it fades away", not cursor.visible and cursor.look().alpha < 0.01)
        r.check("and a trip it was on does not arrive afterwards", late == [])
        r.check("and it stops asking for frames", not cursor.busy())

    with guard(r, "a deliberate hold"):
        cursor = teaching.CursorMotion(origin=(0.0, 0.0))
        cursor.travel((100.0, 100.0))
        for _ in range(HZ * 2):
            cursor.step(1.0 / HZ)
        quiet, ended = [False], []
        cursor.hold(1.0, lambda: quiet[0], lambda: ended.append(1))
        for _ in range(int(HZ * 1.5)):
            cursor.step(1.0 / HZ)
        r.check("it lasts at least its minimum and while Diablo is still talking",
                ended == [])
        quiet[0] = True
        cursor.step(1.0 / HZ)
        r.check("and ends when he goes quiet", ended == [1])

    # ── N. Reduced motion ───────────────────────────────────────────────────
    r.head("N  Reduced motion: the same information, no travel")
    with guard(r, "fade, jump, fade"):
        cursor = teaching.CursorMotion(origin=(1400.0, 60.0), reduced=True)
        arrived, travelled = [], []
        cursor.travel((600.0, 500.0), lambda: arrived.append(1))
        for _ in range(HZ * 2):
            cursor.step(1.0 / HZ)
            p = cursor.look().position
            travelled.append(p)
        middle = [p for p in travelled
                  if 20.0 < math.hypot(p[0] - 600.0, p[1] - 500.0) < 790.0]
        r.check("it never passes through the space in between", middle == [],
                f"{len(middle)} frames in transit")
        r.check("it still arrives, and still says so", arrived == [1])
        r.check("  exactly on the spot",
                math.hypot(cursor.look().position[0] - 600.0,
                           cursor.look().position[1] - 500.0) < 0.5)
        r.check("  with no arrival flourish", cursor.look().cue is None)

    # ── J I K. One target, in order, and nothing stale ──────────────────────
    r.head("J I K  One current target, and late events change nothing")
    with guard(r, "a region step"):
        shell = stand_in()
        shell.teach_begin(total=3, seq=10)
        r.check("Escape is listened for while teaching", shell.escape[-1:] == [True])
        box = PLACES["welfare loss"]
        cx, cy = centre(box)
        done = threading.Event()
        shell.teach_step(x=cx, y=cy, w=box[2], h=box[3], label="welfare loss",
                         index=1, total=3, mode=teaching.REGION, seq=11,
                         arrived=done)
        r.check("the cursor is on its way", shell.cursor.visible)
        r.check("  and nothing is outlined before it gets there", shell.guide.shown == [])
        run_frames(shell, 2.5)
        r.check("on arrival the region is outlined", len(shell.guide.shown) == 1)
        if shell.guide.shown:
            got = shell.guide.shown[-1]
            r.check("  centred on the region, at its size",
                    abs(got["x"] - cx) < 1e-6 and abs(got["y"] - cy) < 1e-6
                    and got["w"] == box[2] and got["h"] == box[3], str(got))
            r.check("  as an outline alone, with no line to an orb that is not there",
                    got["bare"] is True,
                    "found by the visual check: the connector pointed at empty space")
        r.check("and the waiting side is told", done.is_set())
        r.check("the orb stays home: one instrument at a time",
                shell.presence.phase == motion.HOME)

    with guard(r, "I  the next step replaces it"):
        before_hides = shell.guide.hid
        spot = PLACES["original equilibrium"]
        sx, sy = centre(spot)
        second = threading.Event()
        shell.teach_step(x=sx, y=sy, w=0.0, h=0.0, label="equilibrium", index=2,
                         total=3, mode=teaching.POINT, seq=12, arrived=second)
        r.check("the old outline goes before anything moves",
                shell.guide.hid > before_hides)
        shown_before = len(shell.guide.shown)
        run_frames(shell, 2.5)
        r.check("a point is shown by the cursor alone, with no ring beside it",
                len(shell.guide.shown) == shown_before,
                "one instrument, not a cursor and a ring on the same spot")
        r.check("the cursor rests on the crossing",
                math.hypot(shell.cursor.look().position[0]
                           - app_shell._appkit(sx, sy)[0],
                           shell.cursor.look().position[1]
                           - app_shell._appkit(sx, sy)[1]) < 1.0
                if app_shell._appkit(sx, sy) else False)
        r.check("the lesson has one current step, the new one",
                shell.lesson.current is not None and shell.lesson.current.index == 2)

    with guard(r, "K  a stale step"):
        stale = threading.Event()
        shell.teach_step(x=100.0, y=100.0, w=0.0, h=0.0, label="old", index=1,
                         total=3, mode=teaching.POINT, seq=5, arrived=stale)
        r.check("an older step is ignored", shell.lesson.current.index == 2)
        r.check("  but whoever was waiting on it is let go", stale.is_set(),
                "a dropped event must never leave a thread waiting forever")
        run_frames(shell, 1.0)
        r.check("  and the cursor did not go there",
                math.hypot(shell.cursor.look().position[0]
                           - app_shell._appkit(sx, sy)[0],
                           shell.cursor.look().position[1]
                           - app_shell._appkit(sx, sy)[1]) < 1.0)

    with guard(r, "K  a stale arrival"):
        shell = stand_in()
        shell.teach_begin(total=2, seq=1)
        a = PLACES["welfare loss"]
        shell.teach_step(x=centre(a)[0], y=centre(a)[1], w=a[2], h=a[3],
                         label="first", index=1, total=2, mode=teaching.REGION, seq=2)
        for _ in range(5):
            shell.cursor.step(1.0 / HZ)
        b = PLACES["consumer surplus"]
        shell.teach_step(x=centre(b)[0], y=centre(b)[1], w=b[2], h=b[3],
                         label="second", index=2, total=2, mode=teaching.REGION, seq=3)
        run_frames(shell, 3.0)
        labels = [s["label"] for s in shell.guide.shown]
        r.check("the step that was overtaken in flight draws nothing",
                labels == ["second"], str(labels))

    # ── Y L M. Finishing, cancelling, Escape ────────────────────────────────
    r.head("Y L M  Every way out clears everything")
    with guard(r, "Y  finishing"):
        shell.teach_finish(why=lesson.DONE, seq=4)
        run_frames(shell, 1.0)
        r.check("the cursor is gone", not shell.cursor.visible)
        r.check("the outline is gone", shell.guide.closed >= 1)
        r.check("the lesson is over", not shell.lesson.live)
        r.check("Escape is no longer listened for", shell.escape[-1:] == [False])
        r.check("and he is home", shell.presence.phase == motion.HOME)

    with guard(r, "L  cancelling part-way"):
        shell = stand_in()
        shell.teach_begin(total=3, seq=1)
        waiting = threading.Event()
        shell.teach_step(x=700.0, y=450.0, w=64.0, h=70.0, label="welfare loss",
                         index=1, total=3, mode=teaching.REGION, seq=2,
                         arrived=waiting)
        shell.cursor.step(1.0 / HZ)
        shell.clear_target()
        run_frames(shell, 1.0)
        r.check("the cursor is cleared", not shell.cursor.visible)
        r.check("nothing is outlined", shell.guide.shown == [])
        r.check("the lesson is cancelled", shell.lesson.phase == lesson.CANCELLED)
        r.check("the thread waiting on that step is released", waiting.is_set())
        r.check("Escape stops being watched", shell.escape[-1:] == [False])

    with guard(r, "M  Escape"):
        shell = stand_in()
        shell.teach_begin(total=2, seq=1)
        shell.teach_step(x=700.0, y=450.0, w=0.0, h=0.0, label="here", index=1,
                         total=2, mode=teaching.POINT, seq=2)
        shell._escape_pressed()
        run_frames(shell, 1.0)
        r.check("Escape ends the lesson", not shell.lesson.live)
        r.check("  and takes the cursor with it", not shell.cursor.visible)

    with guard(r, "a new lesson takes over"):
        shell = stand_in()
        shell.teach_begin(total=2, seq=1)
        shell.teach_step(x=700.0, y=450.0, w=0.0, h=0.0, label="here", index=1,
                         total=2, mode=teaching.POINT, seq=2)
        shell.guide_begin(total=3, app="Preview", seq=3)
        run_frames(shell, 1.0)
        r.check("a walkthrough starting clears the teaching cursor",
                not shell.cursor.visible)

    with guard(r, "a newer request"):
        shell = stand_in()
        shell.teach_begin(total=2, seq=1)
        original = app_shell._current_generation
        app_shell._current_generation = lambda g: False
        try:
            gone = threading.Event()
            shell.teach_step(x=700.0, y=450.0, w=0.0, h=0.0, label="here",
                             index=1, total=2, mode=teaching.POINT, seq=2,
                             generation=41, arrived=gone)
        finally:
            app_shell._current_generation = original
        r.check("a step from an interaction that has been replaced is dropped",
                not shell.lesson.live and not shell.cursor.visible)
        r.check("  and releases its waiter", gone.is_set())

    # ── U V. The target moves or disappears ─────────────────────────────────
    r.head("U V  When the target is not where it was")
    with guard(r, "U  a place on no display"):
        shell = stand_in()
        shell.teach_begin(total=1, seq=1)
        original = app_shell._appkit
        app_shell._appkit = lambda x, y: None
        try:
            lost = threading.Event()
            shell.teach_step(x=-9000.0, y=-9000.0, w=0.0, h=0.0, label="gone",
                             index=1, total=1, mode=teaching.POINT, seq=2,
                             arrived=lost)
        finally:
            app_shell._appkit = original
        r.check("nothing travels to a place that is not on any screen",
                not shell.cursor.visible)
        r.check("the waiter is released rather than left hanging", lost.is_set())

    with guard(r, "V  re-anchored after the screen changed"):
        shell = stand_in()
        shell.teach_begin(total=2, seq=1)
        shell.teach_step(x=600.0, y=400.0, w=0.0, h=0.0, label="here", index=1,
                         total=2, mode=teaching.POINT, seq=2)
        run_frames(shell, 2.0)
        shell.teach_step(x=900.0, y=300.0, w=0.0, h=0.0, label="here", index=1,
                         total=2, mode=teaching.POINT, seq=3)
        run_frames(shell, 2.0)
        want = app_shell._appkit(900.0, 300.0)
        got = shell.cursor.look().position
        r.check("the same step, found somewhere new, is followed there",
                want is not None and math.hypot(got[0] - want[0], got[1] - want[1]) < 1.0)

    with guard(r, "single target, held while he explains"):
        shell = stand_in()
        shell.teach_begin(total=1, seq=1)
        shell.presence.set_runtime("speaking")
        shell.teach_step(x=700.0, y=450.0, w=80.0, h=80.0, label="this", index=1,
                         total=1, mode=teaching.CURSOR, seq=2, hold=True)
        run_frames(shell, 5.0)
        r.check("while he is talking it stays", shell.cursor.visible
                and shell.lesson.live)
        shell.presence.set_runtime("idle")
        run_frames(shell, 1.5)
        r.check("when he stops it clears itself", not shell.cursor.visible
                and not shell.lesson.live)

    # ── R S T. What already worked still works ──────────────────────────────
    r.head("R S T  Point and region guidance are unchanged")
    with guard(r, "R  a control"):
        shell = stand_in()
        shell.approach(x=640.0, y=300.0, label="File", seconds=1.0)
        run_frames(shell, 2.5)
        r.check("the orb still walks to a control and rings it",
                shell.guide.shown and shell.guide.shown[-1]["w"] == 0.0
                and shell.presence.phase != motion.HOME)
        r.check("  with its connector and caption, exactly as before",
                shell.guide.shown and shell.guide.shown[-1]["bare"] is False)
        r.check("  and no teaching cursor appears", not shell.cursor.visible)

    with guard(r, "S  a region"):
        shell = stand_in()
        cx, cy = centre(PLACES["welfare loss"])
        shell.approach(x=cx, y=cy, label="welfare loss", seconds=1.0,
                       w=64.0, h=70.0)
        run_frames(shell, 2.5)
        r.check("the region outline is drawn at the centre it was given",
                shell.guide.shown and shell.guide.shown[-1]["w"] == 64.0
                and abs(shell.guide.shown[-1]["x"] - cx) < 1e-6)

    with guard(r, "a pointing target takes over from a teaching cursor"):
        shell = stand_in()
        shell.teach_begin(total=2, seq=1)
        shell.teach_step(x=700.0, y=450.0, w=0.0, h=0.0, label="here", index=1,
                         total=2, mode=teaching.POINT, seq=2)
        shell.cursor.step(1.0 / HZ)
        shell.approach(x=640.0, y=300.0, label="File", seconds=1.0)
        run_frames(shell, 1.0)
        r.check("there is never a cursor and a ring at once", not shell.cursor.visible)

    # ── The path, driven ────────────────────────────────────────────────────
    r.head("I X Y  A path, from first step to last")
    steps = [teaching.waypoint(region(n), target.REGION, say=s) for n, s in (
        ("original equilibrium", "Before the subsidy, the market cleared here."),
        ("subsidised equilibrium", "With the subsidy, supply shifts and it clears here."),
        ("welfare loss", "These extra units cost more to make than they are worth."))]
    path = teaching.TeachingPath.of(steps)

    with guard(r, "move, settle, explain, next"):
        channel, said = Channel(), []

        def speak(text):
            said.append(("speak", text))
            channel.events.append(("speak", text))

        result = teaching.run_path(path, channel, speak=speak)
        kinds = [e[0] for e in channel.events]
        r.check("it begins, shows each step, and finishes",
                kinds == ["begin", "step", "speak", "step", "speak", "step",
                          "speak", "finish"], str(kinds))
        r.check("each sentence is spoken only after its step has been shown",
                all(kinds[i - 1] == "step" for i, k in enumerate(kinds) if k == "speak"))
        r.check("it ends as done", channel.events[-1] == ("finish", lesson.DONE)
                and result.done)
        r.check("every step was shown", result.shown == 3)

    with guard(r, "no voice: each step still stays up long enough to read"):
        channel = Channel()
        teaching.run_path(path, channel, speak=None)
        dwells = [e for e in channel.events if e[0] == "dwell"]
        r.check("it waits instead of speaking",
                len(dwells) == 3 and all(d[1] >= motion.Timing.GUIDE_HOLD for d in dwells))

    with guard(r, "L  stopped part-way"):
        channel, said = Channel(stop_after=1), []
        result = teaching.run_path(path, channel, speak=lambda t: said.append(t))
        r.check("nothing after the stop is shown",
                [e for e in channel.events if e[0] == "step"] == [channel.events[1]])
        r.check("  or said", said == [])
        r.check("  and it ends cancelled",
                channel.events[-1] == ("finish", lesson.CANCELLED) and not result.done)

    with guard(r, "X  a step that needs the user to do something"):
        act = teaching.waypoint(region("welfare loss"), target.REGION,
                                say="Click the shaded triangle.", act=True)
        mixed = teaching.TeachingPath.of([steps[0], act])
        channel = Channel()
        teaching.run_path(mixed, channel, speak=lambda t: None)
        kinds = [e[0] for e in channel.events]
        r.check("it waits for the user on that step, through the existing loop",
                "await" in kinds and kinds.index("await") > kinds.index("step", 2))
        r.check("and only then finishes", kinds[-1] == "finish")

    # ── The whole thing, through point_at ───────────────────────────────────
    r.head("§32  'Show me how the subsidy creates welfare loss'")
    from skills import grounding, pointing

    class Found:
        def __init__(self, candidate, kind):
            self.candidate = candidate
            self.found = candidate is not None
            self.target = target.Target(semantic="", kind=kind)
            self.note = "" if candidate is not None else "not found"

    def fake_ground(question, window=None, stated_kind=""):
        wanted = target.read_question(question, stated=stated_kind)
        for name in PLACES:
            if name in question.lower():
                return Found(region(name), wanted.kind)
        return Found(None, wanted.kind)

    def through_point_at(step_list, channel):
        originals = (grounding.ground, teaching.CHANNEL)
        grounding.ground = fake_ground
        teaching.CHANNEL = channel
        spoken = []
        try:
            answer = pointing.point_at(
                "how the subsidy creates welfare loss", steps=step_list,
                speak=lambda t: spoken.append(t))
        finally:
            grounding.ground, teaching.CHANNEL = originals
        return answer, spoken

    with guard(r, "end to end"):
        channel = Channel()
        answer, spoken = through_point_at([
            {"what": "the original equilibrium", "say": "It started here."},
            {"what": "the subsidised equilibrium", "say": "The subsidy moves it here."},
            {"what": "the welfare loss region", "say": "And this is what is lost."}],
            channel)
        shown = [e for e in channel.events if e[0] == "step"]
        r.check("three places were grounded and shown in order",
                [s[2] for s in shown] == ["original equilibrium",
                                          "subsidised equilibrium", "welfare loss"],
                str([s[2] for s in shown]))
        r.check("each was spoken", spoken == ["It started here.",
                                              "The subsidy moves it here.",
                                              "And this is what is lost."])
        r.check("it finished", channel.events[-1] == ("finish", lesson.DONE))
        r.check("the reply does not narrate coordinates or modes",
                not any(w in answer.lower() for w in
                        ("path", "cursor", "presentation", "mode", "px", "pt")),
                answer)

    with guard(r, "a step that cannot be found is dropped, not invented"):
        channel = Channel()
        answer, spoken = through_point_at([
            {"what": "the original equilibrium", "say": "It started here."},
            {"what": "the unicorn", "say": "This does not exist."},
            {"what": "the welfare loss region", "say": "And this is what is lost."}],
            channel)
        shown = [e[2] for e in channel.events if e[0] == "step"]
        r.check("only the grounded places are shown", shown == ["original equilibrium",
                                                               "welfare loss"], str(shown))
        r.check("the missing one is not spoken over a place it is not",
                "This does not exist." not in spoken)

    with guard(r, "fewer than two found: the safest valid thing instead"):
        channel = Channel()
        answer, spoken = through_point_at([
            {"what": "the welfare loss region", "say": "This is what is lost."},
            {"what": "the unicorn", "say": "Nope."}], channel)
        shown = [e for e in channel.events if e[0] == "step"]
        r.check("one grounded place is shown on its own, not as a one-step path",
                len(shown) == 1 and shown[0][3] == teaching.REGION, str(shown))
        channel = Channel()
        answer, spoken = through_point_at([
            {"what": "the unicorn", "say": "Nope."},
            {"what": "the griffin", "say": "Nope."}], channel)
        r.check("nothing found: nothing shown, and it says so",
                [e for e in channel.events if e[0] == "step"] == []
                and answer and "find" in answer.lower(), answer)

    with guard(r, "the decision layer can ask for a path"):
        from skills import agent_tools
        spec = agent_tools.TOOLS.get("point_at", {}).get("schema")
        params = str(spec)
        r.check("point_at accepts steps", "steps" in params)
        r.check("  each step says what, and what to say there",
                "what" in params and "say" in params)

    # ── §19. Not only economics ─────────────────────────────────────────────
    r.head("§19  The same mechanism on other kinds of picture")
    cases = {
        "bar chart": [("Q1 bar", (520, 400, 60, 180)), ("Q4 bar", (760, 260, 60, 320))],
        "line chart": [("January", (520, 480, 30, 30)), ("peak", (700, 300, 30, 30)),
                       ("trough", (880, 520, 30, 30))],
        "process diagram": [("Order placed", (520, 300, 140, 60)),
                            ("Payment", (700, 300, 140, 60)),
                            ("Shipped", (880, 300, 140, 60))],
        "map-like visual": [("north region", (520, 240, 300, 160)),
                            ("river", (560, 420, 400, 40))],
        "labelled figure": [("the heart", (600, 300, 120, 110))],
        "unlabelled region": [("the shaded band", (520, 360, 420, 70))],
        "multiple related regions": [("inflow", (520, 260, 160, 120)),
                                     ("stock", (700, 260, 160, 120)),
                                     ("outflow", (880, 260, 160, 120))],
    }
    with guard(r, "each builds a valid presentation"):
        for name, places in cases.items():
            points = []
            for label, (x, y, w, h) in places:
                cand = perception.Candidate(label=label, role="region", x=x, y=y,
                                            width=w, height=h, confidence=0.8,
                                            sources=["fixture"])
                points.append(teaching.waypoint(cand, target.REGION, say=label))
            path = teaching.TeachingPath.of(points)
            mode = teaching.choose(target.REGION, (places[0][1][2], places[0][1][3]),
                                   waypoints=path.total).mode
            want = teaching.PATH if len(places) > 1 else teaching.REGION
            r.check(f"{name}: {len(places)} place(s) -> {want}",
                    path.total == len(places) and mode == want,
                    f"{path.total} valid, shown as {mode}")
        ui = [teaching.waypoint(control("File", 12, 4), target.CONTROL),
              teaching.waypoint(control("Export…", 30, 120, 90), target.CONTROL),
              teaching.waypoint(control("Save", 400, 500), target.CONTROL)]
        flow = teaching.TeachingPath.of(ui)
        r.check("UI workflow: three controls, each a point, in order",
                flow.total == 3 and all(s.mode == teaching.POINT for s in flow.steps))

    # ── O. What is said about it ────────────────────────────────────────────
    r.head("O  Accessibility: the step is always said in words")
    with guard(r, "announcements"):
        r.check("a path step", teaching.announce("equilibrium", 2, 4)
                == "Step 2 of 4: equilibrium", teaching.announce("equilibrium", 2, 4))
        r.check("a single thing", teaching.announce("welfare loss", 1, 1)
                == "Looking here: welfare loss")
        r.check("nothing internal leaks into them",
                not any(w in (teaching.announce("x", 1, 3) + teaching.announce("y", 1, 1)).lower()
                        for w in ("path", "cursor", "region", "presentation", "mode")))
        source = open(os.path.join(ROOT, "app_shell.py")).read()
        r.check("the drawn cursor has an accessibility label",
                "setAccessibilityLabel_" in source[source.find("class CursorView"):
                                                   source.find("class TeachingCursor")])

    # ── Performance ─────────────────────────────────────────────────────────
    r.head("§28  What it costs")
    with guard(r, "per frame"):
        cursor = teaching.CursorMotion(origin=(0.0, 0.0))
        cursor.travel((1200.0, 700.0))
        began = time.perf_counter()
        frames = 0
        while cursor.busy() and frames < HZ * 4:
            cursor.step(1.0 / HZ)
            cursor.look()
            frames += 1
        cost = (time.perf_counter() - began) / max(frames, 1) * 1e6
        r.check("a moving frame costs well under the 8.3ms a 120 Hz frame allows",
                cost < 200.0, f"{cost:.1f}µs per frame over {frames} frames")
        idle = 0
        for _ in range(HZ):
            if cursor.busy():
                idle += 1
            cursor.step(1.0 / HZ)
        r.check("at rest it asks for no frames at all", idle == 0)

    # ── P Q. The real cursor ────────────────────────────────────────────────
    r.head("P Q  The mouse is yours")
    with guard(r, "Q  no input anywhere in this feature"):
        forbidden = ("CGEventPost", "CGEventCreateMouseEvent",
                     "CGEventCreateKeyboardEvent", "CGWarpMouseCursorPosition",
                     "setMouseLocation", "CGDisplayMoveCursorToPoint",
                     "CGAssociateMouseAndMouseCursorPosition")
        files = [os.path.join(ROOT, "diablo", "teaching.py"),
                 os.path.join(ROOT, "app_shell.py"), os.path.join(ROOT, "diablo_app.py"),
                 os.path.join(ROOT, "diablo", "pointing.py"),
                 os.path.join(ROOT, "diablo", "overlay.py")]
        hits = []
        for path_ in files:
            text = open(path_).read()
            hits += [f"{os.path.basename(path_)}:{sym}" for sym in forbidden if sym in text]
        r.check("no API that moves the pointer or posts an event", hits == [], str(hits))
        r.check("computer use is still disabled", computer_use.ENABLED is False)
        cls = open(os.path.join(ROOT, "app_shell.py")).read()
        body = cls[cls.find("class TeachingCursor"):cls.find("class TeachingCursor") + 4000]
        r.check("the drawn cursor lets every click through",
                "setIgnoresMouseEvents_(True)" in body)

    with guard(r, "P  the real pointer did not move"):
        after = display.cursor()
        r.check("the pointer is where it was before all four ways were exercised",
                before is None or after is None
                or math.hypot(before[0] - after[0], before[1] - after[1]) < 0.5,
                f"before {before}, after {after}")

    # ── The bridge ──────────────────────────────────────────────────────────
    r.head("The live bridge carries a region's extent")
    with guard(r, "region answers reach the shell"):
        import diablo_app
        from skills import pointing as p
        sent = []

        class Anything:
            """voice_bridge, as far as the bridge needs one: every hook exists."""

            def __getattr__(self, name):
                return lambda *a, **k: None

        stub = Anything()
        saved = {name: getattr(app_shell, name) for name in ("notify",)}
        saved_show = p.show
        app_shell.notify = lambda event, **data: sent.append((event, data)) or True
        try:
            diablo_app._bridge_presence(stub)
            ok = p.show(700.0, 445.0, "welfare loss", 3.0, width=64.0, height=70.0)
        except TypeError as e:
            ok = f"TypeError: {e}"
        finally:
            app_shell.notify = saved["notify"]
        r.check("pointing.show accepts a width and height once bridged", ok is True,
                str(ok))
        targets = [d for e, d in sent if e == "target"]
        r.check("  and passes them to the shell",
                targets and targets[-1].get("w") == 64.0 and targets[-1].get("h") == 70.0,
                str(targets[-1] if targets else sent[-3:]))
        r.check("the teaching channel is installed", teaching.CHANNEL is not None)
        p.show = saved_show

    with guard(r, "the channel judges staleness only once the shell has caught up"):
        sent = []
        fake = types.SimpleNamespace(lesson=lesson.Lesson())
        saved_notify, saved_shell = app_shell.notify, app_shell._SHELL
        app_shell.notify = lambda event, **data: sent.append((event, data)) or True
        app_shell._SHELL = fake
        try:
            channel = teaching.CHANNEL()
            channel.begin(2)
            # The previous lesson, closed, and numbered before this one began —
            # the only order the shared counter allows.
            fake.lesson = lesson.Lesson()
            fake.lesson.begin(2)
            fake.lesson.seq = channel.first - 1
            fake.lesson.finish(lesson.DONE)
            r.check("before the shell has seen this lesson begin, it is not cancelled",
                    not channel.cancelled(),
                    "notify is asynchronous; the closed lesson is the last one's")
            fake.lesson.begin(2, seq=channel.first)
            r.check("once it has, a live lesson is not cancelled", not channel.cancelled())
            place = teaching.waypoint(region("welfare loss"), target.REGION, say="x")
            channel.step(place, 1, 2)
            step = [d for e, d in sent if e == "teach_step"][-1]
            r.check("a step is sent as its centre, extent and mode",
                    (step["x"], step["y"]) == centre(PLACES["welfare loss"])
                    and step["w"] == 64.0 and step["mode"] == teaching.REGION, str(step))
            r.check("numbered after the lesson began, from the one shared counter",
                    step["seq"] > channel.first)
            fake.lesson.present(lesson.Step(1, 2, x=1.0, y=1.0), seq=step["seq"])
            fake.lesson.finish(lesson.CANCELLED)
            r.check("cleared by Escape or anything else: cancelled", channel.cancelled())
            fake.lesson.begin(1, seq=channel.sent + 5)
            r.check("overtaken by a newer lesson: cancelled", channel.cancelled())
        finally:
            app_shell.notify, app_shell._SHELL = saved_notify, saved_shell

    return r.report("teaching")


if __name__ == "__main__":
    sys.exit(run())
