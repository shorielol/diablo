#!/usr/bin/env python3
"""Teaching: one step at a time, and only one.

What is checked: a step moves him, settles him, and only then indicates —
never all at once; there is exactly one current target, and presenting the
next one clears the last before he leaves; a step planned but never found on
screen sends him nowhere; a lesson that has ended ignores what arrives after
it, and an event from an overtaken step draws nothing; waiting, verified and
not-verified are told apart and success is never announced for a step nobody
was asked to take; cancelling clears the indication and takes him home;
placement comes from the one spatial system; reduced motion keeps the
indication; nothing here posts a click or a key; and the page's own rules for
the caption (app/surface.js, in node).
"""

import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

from AppKit import NSApplication  # noqa: E402

NSApplication.sharedApplication()
import app_shell  # noqa: E402
from skills import lesson, motion  # noqa: E402
from skills.motion import Presence  # noqa: E402

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
HERE = os.path.dirname(os.path.abspath(__file__))
HZ = 120.0


class Drawn:
    """A guide that records instead of drawing, and never starts a process."""

    def __init__(self):
        self.shown = []
        self.hidden = 0
        self.closed = 0
        self.prepared = []
        self.watch = False

    def hide(self):
        self.hidden += 1

    def close(self):
        self.closed += 1

    def prepare(self, x, y, label="", w=0.0, h=0.0, watch=False, step=""):
        self.prepared.append((x, y, label, w, h, watch, step))
        self.watch = watch

    def show(self, x, y, label="", w=0.0, h=0.0, step="", on_light=False):
        self.shown.append({"x": x, "y": y, "label": label, "w": w, "h": h,
                           "step": step, "on_light": on_light})

    def ready(self, timeout=5.0):
        return True

    def wait_for_click(self, x, y, radius=80.0, timeout=30.0, should_stop=None):
        return True


def stand_in(home=(1461.0, 51.0), reduced=False):
    """A Shell with a presence, a recording guide and a real Lesson."""
    shell = types.SimpleNamespace()
    shell.presence = Presence(home=home, reduced=reduced)
    shell.presence.set_reduced(reduced)
    shell.wake = lambda: None
    shell.guide = Drawn()
    shell.lesson = lesson.Lesson()
    for name in ("guide_begin", "guide_step", "guide_waiting", "guide_verified",
                 "guide_unverified", "guide_finish", "clear_target", "no_target",
                 "approach", "focus_content", "_under_surface"):
        setattr(shell, name, types.MethodType(getattr(app_shell.Shell, name), shell))
    return shell


def settle(shell, seconds=4.0):
    for _ in range(int(seconds * HZ)):
        shell.presence.step(1.0 / HZ)


def run():
    r = Results()

    # ── the session's own rules ─────────────────────────────────────────
    r.head("One current target, and only one")
    with guard(r, "presenting"):
        book = lesson.Lesson()
        r.check("a fresh lesson has nothing current", book.current is None)
        book.begin(3, "Preview")
        first = lesson.Step(index=1, total=3, label="Export", x=400.0, y=300.0)
        r.check("a step becomes current", book.present(first) and book.current is first)
        second = lesson.Step(index=2, total=3, label="PDF", x=500.0, y=340.0)
        book.present(second)
        r.check("the next step replaces it", book.current is second)
        r.check("  and the first is no longer current", book.current is not first,
                "two rings at once is what makes a walkthrough unfollowable")

    with guard(r, "endings"):
        book = lesson.Lesson()
        book.begin(2)
        book.present(lesson.Step(index=1, total=2, x=10.0, y=10.0))
        book.finish(lesson.DONE)
        r.check("a finished lesson has no current target", book.current is None)
        r.check("  and refuses a later step",
                not book.present(lesson.Step(index=2, total=2, x=20.0, y=20.0)),
                "a step arriving after the end belongs to a lesson nobody is in")
        r.check("  and cannot be finished twice", not book.finish(lesson.DONE))

    with guard(r, "stale events are dropped"):
        book = lesson.Lesson()
        book.begin(2, seq=10)
        r.check("a newer event is taken",
                book.present(lesson.Step(index=1, total=2, x=1.0, y=1.0), seq=11))
        r.check("an older one is not",
                not book.present(lesson.Step(index=9, total=2, x=9.0, y=9.0), seq=5))
        r.check("  and the target is unchanged", book.current.index == 1)

    # ── never celebrate what was not done ───────────────────────────────
    r.head("Verified means verified")
    with guard(r, "the gate"):
        book = lesson.Lesson()
        book.begin(1)
        book.present(lesson.Step(index=1, total=1, x=5.0, y=5.0))
        r.check("a step nobody was asked to take cannot be verified",
                not book.verified(),
                "this is 'do not falsely celebrate success', as code")
        r.check("asking first is what makes it possible", book.waiting())
        r.check("  and then it can", book.verified())
        r.check("  leaving the lesson in verified", book.phase == lesson.VERIFIED)

    with guard(r, "and not-verified says so"):
        book = lesson.Lesson()
        book.begin(1)
        book.present(lesson.Step(index=1, total=1, x=5.0, y=5.0))
        book.waiting()
        r.check("nothing changed is a real outcome",
                book.unverified("That didn't seem to change anything."))
        r.check("  it is drawn as a fault", lesson.is_fault(book.phase))
        r.check("  and it never reads as success",
                "done" not in book.line.lower() and book.phase != lesson.VERIFIED)

    # ── the words ───────────────────────────────────────────────────────
    r.head("What the surface says")
    with guard(r, "captions"):
        step = lesson.Step(index=2, total=4, label="Export")
        r.check("guiding names the step and the thing",
                lesson.line_for(lesson.GUIDING, step) == "Step 2 of 4 · This is Export")
        r.check("waiting hands over",
                lesson.line_for(lesson.WAITING, step) == "Step 2 of 4 · Go ahead")
        r.check("verified is short",
                lesson.line_for(lesson.VERIFIED, step) == "Step 2 of 4 · Done")
        r.check("a single step carries no counter",
                lesson.line_for(lesson.GUIDING,
                                lesson.Step(index=1, total=1, label="Settings"))
                == "This is Settings")
        r.check("completion is plain", lesson.line_for(lesson.DONE) == "That's the lot.")
        r.check("cancelling is plain", lesson.line_for(lesson.CANCELLED) == "Stopped.")
        for phase in lesson.PHASES:
            r.check("  %-10s stays one short line" % phase,
                    len(lesson.line_for(phase, step)) <= 48,
                    "no giant instruction panels")

    # ── move, settle, then indicate ─────────────────────────────────────
    r.head("Move, settle, then indicate")
    with guard(r, "the order"):
        shell = stand_in()
        shell.guide_begin(2, "Preview")
        shell.guide_step(x=500.0, y=400.0, label="Export", index=1, total=2)
        r.check("he leaves home", shell.presence.phase == motion.MOVING)
        r.check("nothing is drawn while he travels", shell.guide.shown == [],
                "the ring appearing before he arrives is three beats in one")
        settle(shell)
        r.check("the indication comes after the settle", len(shell.guide.shown) == 1)
        r.check("  on the target he was given",
                shell.guide.shown[0]["x"] == 500.0 and shell.guide.shown[0]["y"] == 400.0)
        r.check("  carrying the step counter", shell.guide.shown[0]["step"] == "1 of 2")
        r.check("he is guiding, not merely stopped", shell.presence.guiding)
        r.check("the overlay was asked to watch for the click",
                bool(shell.guide.prepared) and shell.guide.prepared[-1][5] is True,
                "one overlay draws and watches, so there is one mark")

    with guard(r, "the hold"):
        shell = stand_in()
        shell.guide_begin(1)
        shell.guide_step(x=500.0, y=400.0, label="Export", index=1, total=1)
        settle(shell)
        shell.guide_waiting()
        r.check("waiting is a state, not a sleep", shell.presence.awaiting)
        settle(shell, 2.0)
        r.check("  and he is still there", shell.presence.phase == motion.HOLDING)
        r.check("  still indicating", len(shell.guide.shown) == 1)

    # ── step to step ────────────────────────────────────────────────────
    r.head("From one step to the next")
    with guard(r, "the transition"):
        shell = stand_in()
        shell.guide_begin(2)
        shell.guide_step(x=400.0, y=300.0, label="File", index=1, total=2)
        settle(shell)
        cleared = shell.guide.hidden
        shell.guide_step(x=900.0, y=600.0, label="Export", index=2, total=2)
        r.check("the old indication is cleared before he moves",
                shell.guide.hidden > cleared,
                "target A must not remain while target B appears")
        r.check("  and he travels again", shell.presence.phase == motion.MOVING)
        settle(shell)
        r.check("the new step is the only one shown", len(shell.guide.shown) == 2)
        r.check("  and it is the second target", shell.guide.shown[-1]["x"] == 900.0)
        r.check("  with its own counter", shell.guide.shown[-1]["step"] == "2 of 2")

    with guard(r, "a step overtaken mid-flight draws nothing"):
        shell = stand_in()
        shell.guide_begin(2)
        shell.guide_step(x=400.0, y=300.0, label="File", index=1, total=2)
        shell.guide_step(x=900.0, y=600.0, label="Export", index=2, total=2)
        settle(shell)
        r.check("only the surviving step is indicated", len(shell.guide.shown) == 1,
                "the overtaken arrival must not draw its ring on the way past")
        r.check("  and it is the newer one", shell.guide.shown[0]["x"] == 900.0)

    # ── nothing to point at ─────────────────────────────────────────────
    r.head("When there is nothing to show")
    with guard(r, "a step that was never found"):
        shell = stand_in()
        shell.guide_begin(2)
        before = shell.presence.phase
        shell.guide_step(x=0.0, y=0.0, label="Preferences", index=1, total=2,
                         found=False)
        r.check("he does not travel", shell.presence.phase == before,
                "a planned step that was never located is not a place")
        r.check("and nothing is drawn", shell.guide.shown == [])
        r.check("the step is still current, so the surface can say so",
                shell.lesson.current is not None)

    with guard(r, "a target on no display"):
        shell = stand_in()
        shell.guide_begin(1)
        shell.guide_step(x=99000.0, y=99000.0, label="Nowhere", index=1, total=1)
        settle(shell)
        r.check("nothing is indicated off every screen", shell.guide.shown == [])

    # ── cancelling ──────────────────────────────────────────────────────
    r.head("Stopping")
    with guard(r, "cancel"):
        shell = stand_in()
        shell.guide_begin(3)
        shell.guide_step(x=500.0, y=400.0, label="Export", index=1, total=3)
        settle(shell)
        shell.guide_finish(lesson.CANCELLED)
        r.check("the indication is closed", shell.guide.closed >= 1)
        r.check("the lesson is over", not shell.lesson.live)
        r.check("he heads home",
                shell.presence.phase in (motion.RETURNING, motion.HOME))
        settle(shell, 6.0)
        r.check("  and gets there", shell.presence.phase == motion.HOME)
        r.check("a step arriving after the stop is ignored",
                not shell.lesson.present(lesson.Step(index=2, total=3, x=1.0, y=1.0)),
                "no stale guidance animation should restart")

    with guard(r, "anything that clears the target ends the lesson"):
        shell = stand_in()
        shell.guide_begin(2)
        shell.guide_step(x=500.0, y=400.0, label="Export", index=1, total=2)
        settle(shell)
        shell.clear_target()
        r.check("the lesson does not outlive its indication", not shell.lesson.live)

    # ── displays ────────────────────────────────────────────────────────
    r.head("Wherever the target is")
    with guard(r, "placement uses the one spatial system"):
        from skills import guidance
        stand = motion.stand_for(900.0, 500.0, "Export")
        r.check("a target on a display has somewhere to stand", stand is not None)
        if stand:
            r.check("  and he is not on top of it",
                    abs(stand[0] - 900.0) + abs(stand[1] - 500.0) > motion.STAND_RADIUS,
                    "the thing being explained is never the thing being covered")
        r.check("a target on no display has nowhere",
                motion.stand_for(99000.0, 99000.0, "") is None,
                "reported rather than invented")
        r.check("the placement comes from guidance.place",
                guidance.place(900.0, 500.0, "Export") is not None,
                "no second coordinate mapper")

    # ── reduced motion ──────────────────────────────────────────────────
    r.head("With reduced motion")
    with guard(r, "the indication survives"):
        shell = stand_in(reduced=True)
        shell.guide_begin(1)
        shell.guide_step(x=500.0, y=400.0, label="Export", index=1, total=1)
        settle(shell, 6.0)
        r.check("he still arrives", shell.presence.phase == motion.HOLDING)
        r.check("and the target is still indicated", len(shell.guide.shown) == 1,
                "less travel, same relationship")
        shell.guide_waiting()
        r.check("and waiting still reads as waiting", shell.presence.awaiting)

    # ── the backend's own lifecycle ─────────────────────────────────────
    r.head("Driven by the walkthrough, not by a clock")
    with guard(r, "the tutor reports what it does"):
        source = open(os.path.join(HOME, "skills", "tutor.py")).read()
        r.check("the lifecycle has an audience", "SINK" in source and "_tell(" in source)
        for event in ("STARTED", "PRESENTED", "WAITING", "VERIFIED",
                      "NOT_VERIFIED", "CANCELLED", "COMPLETED"):
            r.check("  %s is reported" % event, "_tell(%s" % event in source)
        r.check("the end is reported however it ends",
                "finally:" in source and "_tell(COMPLETED" in source)

    with guard(r, "the shell routes it"):
        shell_source = open(os.path.join(HOME, "app_shell.py")).read()
        for event, method in [("lesson_begin", "guide_begin"),
                              ("lesson_step", "guide_step"),
                              ("lesson_waiting", "guide_waiting"),
                              ("lesson_verified", "guide_verified"),
                              ("lesson_unverified", "guide_unverified"),
                              ("lesson_finish", "guide_finish")]:
            r.check("  %-18s -> %s" % (event, method),
                    '"%s"' % event in shell_source
                    and "self.%s(" % method in shell_source)
        r.check("no sleep paces the guidance", "time.sleep" not in shell_source,
                "an explicit guidance state rather than arbitrary sleeping")

    with guard(r, "one mark on screen"):
        bridge = open(os.path.join(HOME, "diablo_app.py")).read()
        r.check("the tutor's marker is drawn by the shell",
                "class ShellMarker" in bridge and "tutor.Marker = ShellMarker" in bridge,
                "two overlays would put two marks on one target")
        r.check("and its clicks come from the shell's overlay",
                "shell.guide.wait_for_click" in bridge)

    # ── safety ──────────────────────────────────────────────────────────
    r.head("Showing, never doing")
    with guard(r, "the whole build"):
        from skills import computer_use
        r.check("computer use is still off", computer_use.ENABLED is False)
        banned = ("CGEventPost", "_post_click", "kCGEventLeftMouseDown",
                  "CGEventCreateMouseEvent", "CGEventCreateKeyboardEvent")
        for name in ["skills/lesson.py", "app_shell.py", "diablo_app.py",
                     "app/app.js", "app/surface.js"]:
            text = open(os.path.join(HOME, name)).read()
            r.check("%s posts no events" % name,
                    not any(word in text for word in banned))
        # By what it imports, not by what it says: the docstring mentions
        # AppKit precisely to promise there is none, so a word search fails
        # on the sentence making the promise.
        imports = set()
        for line in open(os.path.join(HOME, "skills/lesson.py")):
            line = line.strip()
            if line.startswith(("import ", "from ")):
                imports.add(line.split()[1].split(".")[0])
        r.check("the lesson module only decides what is true",
                imports <= {"dataclasses", "typing"},
                "it holds which step is current; it does not draw, move or "
                "act — imports: %s" % (sorted(imports) or "none"))

    # ── the page ────────────────────────────────────────────────────────
    r.head("The surface's own caption rules")
    with guard(r, "node"):
        proc = subprocess.run(
            ["node", os.path.join(HERE, "js", "lesson_steps.mjs")],
            capture_output=True, text=True, env=dict(os.environ, DIABLO_HOME=HOME))
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("ok "):
                r.check(stripped[3:], True)
            elif stripped.startswith("FAIL "):
                r.check(stripped[5:], False)
        if proc.returncode not in (0, 1):
            r.check("the node suite ran", False, (proc.stderr or "")[-200:])

    return r.report("lesson")


if __name__ == "__main__":
    sys.exit(run())
