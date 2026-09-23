#!/usr/bin/env python3
"""The click the user actually made.

The defect this suite exists for: a step's sentence is spoken before the
wait for the click begins, and the wait used to start by emptying the click
queue. Someone who clicked while Diablo was still talking — which is what
anyone does, because the ring is already on the target — had that click
thrown away, then waited out the whole timeout while a stale ring sat on a
control they had already pressed.

So: a click that arrives after the target is armed counts, whenever it
arrives. A click from before it was armed does not. Acting is not the same
as succeeding, and only a verified result advances the lesson.
"""

import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

from AppKit import NSApplication  # noqa: E402

NSApplication.sharedApplication()
import app_shell  # noqa: E402
from skills import lesson, motion  # noqa: E402
from skills.motion import Presence  # noqa: E402

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
HZ = 120.0
TARGET = (400.0, 300.0)


def a_guide():
    """A Guide with no overlay process: just its click plumbing."""
    guide = app_shell.Guide()
    guide.watch = True
    return guide


def click_at(guide, x, y, at=None):
    """What the reader thread puts on the queue when the overlay reports one."""
    guide.clicks.put({"x": x, "y": y, "at": time.time() if at is None else at})


class Drawn:
    def __init__(self):
        self.shown, self.hidden, self.closed = [], 0, 0

    def hide(self):
        self.hidden += 1

    def close(self):
        self.closed += 1

    def prepare(self, *a, **k):
        pass

    def show(self, x, y, label="", w=0.0, h=0.0, step="", on_light=False):
        self.shown.append({"x": x, "y": y, "step": step})

    def arm(self, *a, **k):
        pass

    def ready(self, timeout=5.0):
        return True

    def wait_for_click(self, *a, **k):
        return True


def stand_in(reduced=False):
    shell = types.SimpleNamespace()
    shell.presence = Presence(home=(1461.0, 51.0), reduced=reduced)
    shell.presence.set_reduced(reduced)
    shell.wake = lambda: None
    shell.guide = Drawn()
    shell.lesson = lesson.Lesson()
    for name in ("guide_begin", "guide_step", "guide_waiting", "guide_acted",
                 "guide_verified", "guide_unverified", "guide_finish",
                 "clear_target", "no_target", "approach", "focus_content",
                 "_under_surface"):
        setattr(shell, name, types.MethodType(getattr(app_shell.Shell, name), shell))
    return shell


def settle(shell, seconds=4.0):
    for _ in range(int(seconds * HZ)):
        shell.presence.step(1.0 / HZ)


def run():
    r = Results()

    # ── the defect itself ───────────────────────────────────────────────
    r.head("A click made while he is still talking")
    with guard(r, "the click that arrives before the wait"):
        guide = a_guide()
        guide.arm(*TARGET)                    # the ring goes up
        click_at(guide, *TARGET)              # the user presses it, at once
        started = time.time()
        got = guide.wait_for_click(*TARGET, radius=80.0, timeout=2.0)
        took = time.time() - started
        r.check("the click still counts", got,
                "the wait used to empty the queue before looking at it")
        r.check("  and it is seen at once", took < 0.30, "%.2fs" % took)

    with guard(r, "a click from before the target was armed"):
        guide = a_guide()
        click_at(guide, *TARGET, at=time.time() - 5.0)   # an older press
        guide.arm(*TARGET)
        got = guide.wait_for_click(*TARGET, radius=80.0, timeout=0.6)
        r.check("an older click does not count", not got,
                "it belonged to whatever was on screen before this step")

    with guard(r, "a click somewhere else"):
        guide = a_guide()
        guide.arm(*TARGET)
        click_at(guide, 1200.0, 900.0)
        got = guide.wait_for_click(*TARGET, radius=80.0, timeout=0.6)
        r.check("a click off the target is not the step", not got)

    with guard(r, "no click at all"):
        guide = a_guide()
        guide.arm(*TARGET)
        started = time.time()
        got = guide.wait_for_click(*TARGET, radius=80.0, timeout=0.5)
        r.check("nothing happening is not a click", not got)
        r.check("  and it waits the time it was given",
                0.4 <= time.time() - started < 1.2)

    with guard(r, "a click after a few seconds"):
        guide = a_guide()
        guide.arm(*TARGET)
        threading.Timer(0.4, lambda: click_at(guide, *TARGET)).start()
        started = time.time()
        got = guide.wait_for_click(*TARGET, radius=80.0, timeout=3.0)
        r.check("a slower user still counts", got)
        r.check("  and it is seen soon after", time.time() - started < 1.2)

    with guard(r, "stopping while waiting"):
        guide = a_guide()
        guide.arm(*TARGET)
        stop = {"now": False}
        threading.Timer(0.2, lambda: stop.__setitem__("now", True)).start()
        got = guide.wait_for_click(*TARGET, radius=80.0, timeout=3.0,
                                   should_stop=lambda: stop["now"])
        r.check("cancelling ends the wait", not got)

    with guard(r, "re-arming for the next step"):
        guide = a_guide()
        guide.arm(*TARGET)
        click_at(guide, *TARGET)
        guide.wait_for_click(*TARGET, radius=80.0, timeout=1.0)
        guide.arm(900.0, 600.0)
        got = guide.wait_for_click(900.0, 600.0, radius=80.0, timeout=0.5)
        r.check("the last step's click does not satisfy this one", not got,
                "arming is what makes a press belong to a step")

    # ── acting is not succeeding ────────────────────────────────────────
    r.head("Acting is not succeeding")
    with guard(r, "the phase between them"):
        book = lesson.Lesson()
        book.begin(2)
        book.present(lesson.Step(index=1, total=2, label="File", x=1.0, y=1.0))
        book.waiting()
        r.check("a press is its own state", book.acted())
        r.check("  and it is not verified", book.phase == lesson.ACTED)
        r.check("  it says he is checking", "check" in book.line.lower(), book.line)
        r.check("verifying still has to happen", book.verified())
        r.check("  and only then is it done", book.phase == lesson.VERIFIED)

    with guard(r, "a press that changed nothing"):
        book = lesson.Lesson()
        book.begin(1)
        book.present(lesson.Step(index=1, total=1, label="File", x=1.0, y=1.0))
        book.waiting()
        book.acted()
        r.check("it can still come to nothing",
                book.unverified("That didn't seem to change anything."))
        r.check("  which is not success", book.phase != lesson.VERIFIED)
        r.check("  and never reads as done", "done" not in book.line.lower())

    with guard(r, "the order cannot be skipped"):
        book = lesson.Lesson()
        book.begin(1)
        book.present(lesson.Step(index=1, total=1, x=1.0, y=1.0))
        r.check("no press before he has asked", not book.acted(),
                "acted only means anything once the user was asked to act")

    # ── the lesson does not advance on its own ──────────────────────────
    r.head("Only a verified result advances it")
    with guard(r, "waiting stays waiting"):
        shell = stand_in()
        shell.guide_begin(2)
        shell.guide_step(x=TARGET[0], y=TARGET[1], label="File", index=1, total=2)
        settle(shell)
        shell.guide_waiting()
        r.check("he is waiting", shell.presence.awaiting)
        settle(shell, 5.0)
        r.check("  and five seconds later he still is", shell.presence.awaiting,
                "no timer may advance a step")
        r.check("  with the same one target", len(shell.guide.shown) == 1)

    with guard(r, "a stale press cannot advance a newer step"):
        book = lesson.Lesson()
        book.begin(2, seq=10)
        book.present(lesson.Step(index=1, total=2, x=1.0, y=1.0), seq=11)
        book.waiting(seq=12)
        r.check("a press numbered behind the step is dropped",
                not book.acted(seq=4))
        r.check("  and the step is untouched", book.phase == lesson.WAITING)

    with guard(r, "cancelling during the wait clears everything"):
        shell = stand_in()
        shell.guide_begin(2)
        shell.guide_step(x=TARGET[0], y=TARGET[1], label="File", index=1, total=2)
        settle(shell)
        shell.guide_waiting()
        shell.guide_finish(lesson.CANCELLED)
        r.check("the indication is closed", shell.guide.closed >= 1)
        r.check("the lesson is over", not shell.lesson.live)
        r.check("nothing is still current", shell.lesson.current is None)
        settle(shell, 6.0)
        r.check("and he is home", shell.presence.phase == motion.HOME)

    with guard(r, "reduced motion changes none of it"):
        shell = stand_in(reduced=True)
        shell.guide_begin(1)
        shell.guide_step(x=TARGET[0], y=TARGET[1], label="File", index=1, total=1)
        settle(shell, 6.0)
        shell.guide_waiting()
        r.check("he still waits", shell.presence.awaiting)
        shell.guide_acted()
        r.check("  and a press still registers", shell.lesson.phase == lesson.ACTED)

    # ── safety ──────────────────────────────────────────────────────────
    r.head("Watching, never clicking")
    with guard(r, "the observer only observes"):
        from skills import computer_use
        r.check("computer use is off", computer_use.ENABLED is False)
        banned = ("CGEventPost", "CGEventCreateMouseEvent",
                  "CGEventCreateKeyboardEvent", "_post_click")
        for name in ["app_shell.py", "skills/lesson.py", "skills/tutor.py"]:
            text = open(os.path.join(HOME, name)).read()
            r.check("%s posts no events" % name,
                    not any(w in text for w in banned))
        overlay = open(os.path.join(HOME, "skills", "overlay.py")).read()
        r.check("the overlay's monitor is a listener, not a sender",
                "addGlobalMonitorForEventsMatchingMask" in overlay
                and "CGEventPost" not in overlay)
        r.check("  and its window never takes the click",
                "setIgnoresMouseEvents_(True)" in overlay,
                "the press reaches the application underneath as it would anyway")

    return r.report("acting")


if __name__ == "__main__":
    sys.exit(run())
