#!/usr/bin/env python3
"""Doing something: what Diablo is about to do, and whether it worked.

What is checked: the backend's own task and action states map onto one
user-facing phase without a second state machine; the action preview says
what is about to happen in the screen's words; a permission request is a
question with a consequence, never a risk class or a confidence number;
acting and checking are separate states, and "done" is only ever said after
the backend verified it; an ambiguous target asks instead of guessing;
blocked is not dressed up as an error; cancelling clears the target, the
permission and the surface; an event from a superseded task cannot move him
or claim success; a multi-step task shows only the step it is on; DO borrows
GUI-6's one target rather than drawing a second; and nothing here can
execute anything.
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
from skills import computer_use, doing, lesson, motion  # noqa: E402
from skills.do_engine import ActionState, TaskState  # noqa: E402
from skills.motion import Presence  # noqa: E402

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
HERE = os.path.dirname(os.path.abspath(__file__))
HZ = 120.0


class Drawn:
    """A guide that records instead of drawing."""

    def __init__(self):
        self.shown, self.hidden, self.closed, self.prepared = [], 0, 0, []

    def hide(self):
        self.hidden += 1

    def close(self):
        self.closed += 1

    def prepare(self, x, y, label="", w=0.0, h=0.0, watch=False, step=""):
        self.prepared.append((x, y, label, w, h, watch, step))

    def show(self, x, y, label="", w=0.0, h=0.0, step="", on_light=False):
        self.shown.append({"x": x, "y": y, "label": label, "step": step})

    def ready(self, timeout=5.0):
        return True

    def wait_for_click(self, *a, **k):
        return True


def stand_in(home=(1461.0, 51.0), reduced=False):
    shell = types.SimpleNamespace()
    shell.presence = Presence(home=home, reduced=reduced)
    shell.presence.set_reduced(reduced)
    shell.wake = lambda: None
    shell.guide = Drawn()
    shell.lesson = lesson.Lesson()
    shell.doing = doing.Doing()
    for name in ("guide_begin", "guide_step", "guide_waiting", "guide_verified",
                 "guide_unverified", "guide_finish", "clear_target", "no_target",
                 "approach", "focus_content", "_under_surface",
                 "do_begin", "do_target", "do_ask", "do_granted", "do_denied",
                 "do_executing", "do_verifying", "do_verified", "do_failed",
                 "do_blocked", "do_finish"):
        setattr(shell, name, types.MethodType(getattr(app_shell.Shell, name), shell))
    return shell


def settle(shell, seconds=4.0):
    for _ in range(int(seconds * HZ)):
        shell.presence.step(1.0 / HZ)


def run():
    r = Results()
    CLICK = doing.Intent(verb="Click", target="Save")

    # ── the mapping, not a second state machine ─────────────────────────
    r.head("The backend's states, in the user's words")
    with guard(r, "every task state has one phase"):
        for state in TaskState:
            phase = doing.phase_for(state)
            r.check("  %-20s -> %-12s" % (state.value, phase),
                    phase in doing.PHASES, phase)
        r.check("and the phases are fewer than the states",
                len(doing.PHASES) < len(TaskState) + len(ActionState),
                "the interface simplifies; it does not mirror")

    with guard(r, "the distinctions that matter"):
        r.check("targeting reads as finding the place",
                doing.phase_for(TaskState.LOCATING) == doing.TARGETING)
        r.check("awaiting permission reads as asking",
                doing.phase_for(TaskState.AWAITING_PERMISSION) == doing.ASKING)
        r.check("executing is its own phase",
                doing.phase_for(TaskState.EXECUTING) == doing.EXECUTING)
        r.check("verifying is NOT executing",
                doing.phase_for(TaskState.VERIFYING) == doing.VERIFYING
                and doing.VERIFYING != doing.EXECUTING,
                "clicking and checking are different things to watch")
        r.check("blocked is not failed",
                doing.phase_for(TaskState.BLOCKED) == doing.BLOCKED
                and doing.phase_for(TaskState.FAILED) == doing.FAILED)

    # ── B. the action preview ───────────────────────────────────────────
    r.head("What he is about to do")
    with guard(r, "the preview"):
        r.check("it is the verb and the thing", CLICK.preview == "Click Save")
        r.check("a setting reads naturally",
                doing.Intent(verb="Choose", target="Dark Mode").preview
                == "Choose Dark Mode")
        r.check("with nothing named it stays honest",
                doing.Intent(verb="Click").preview == "Click")
        r.check("it never carries a coordinate",
                not any(ch.isdigit() for ch in CLICK.preview))
        r.check("and it is short", len(CLICK.preview) <= 40)

    # ── A / risk / permission wording ───────────────────────────────────
    r.head("Asking, without the machinery")
    with guard(r, "the question"):
        low = doing.consent_for(computer_use.LOW, CLICK)
        med = doing.consent_for(computer_use.MEDIUM,
                                doing.Intent(verb="Change", target="the theme"))
        high = doing.consent_for(computer_use.HIGH,
                                 doing.Intent(verb="Delete", target="the file"))
        for name, line in [("low", low), ("medium", med), ("high", high)]:
            r.check("  %-6s: %s" % (name, line),
                    bool(line) and line.endswith("?")
                    and not any(w in line.lower() for w in
                                ("risk", "confidence", "enum", "actionrecord",
                                 "0.", "click(")),
                    "no risk class, no decimals, no internals")
        r.check("a heavier action says what it costs",
                "undo" in high.lower() or "permanent" in high.lower()
                or "cannot" in high.lower(),
                "the consequence, not the category")
        r.check("the question names the thing", "Save" in low)

    with guard(r, "and the states of consent"):
        d = doing.Doing()
        d.begin("t1", CLICK)
        r.check("nothing is pending before he asks", d.consent is None)
        d.asking(doing.consent_for(computer_use.MEDIUM, CLICK))
        r.check("asking is a phase", d.phase == doing.ASKING)
        r.check("  with a question to show", bool(d.consent))
        r.check("  and it is not a fault", not doing.is_fault(d.phase))
        r.check("granting moves on", d.granted() and d.phase == doing.READY)
        d2 = doing.Doing()
        d2.begin("t2", CLICK)
        d2.asking(doing.consent_for(computer_use.LOW, CLICK))
        r.check("denying stops it", d2.denied() and d2.phase == doing.CANCELLED)
        r.check("  and a denial is not an error", not doing.is_fault(d2.phase))

    # ── D / E / F. act, then check, then say ────────────────────────────
    r.head("Acting and checking are different")
    with guard(r, "the order"):
        d = doing.Doing()
        d.begin("t3", CLICK)
        d.targeting()
        r.check("he finds the place first", d.phase == doing.TARGETING)
        d.asking(doing.consent_for(computer_use.LOW, CLICK))
        d.granted()
        r.check("done cannot be skipped to", not d.verified(),
                "done is only said after checking")
        d.executing()
        r.check("executing", d.phase == doing.EXECUTING)
        r.check("still cannot claim done from executing", not d.verified(),
                "the OS event being sent is not the result happening")
        d.verifying()
        r.check("checking is visible", d.phase == doing.VERIFYING)
        r.check("now it can be verified", d.verified())
        r.check("  and that is done", d.phase == doing.DONE)

    with guard(r, "a check that fails"):
        d = doing.Doing()
        d.begin("t4", CLICK)
        d.asking(doing.consent_for(computer_use.LOW, CLICK))
        d.granted()
        d.executing()
        d.verifying()
        r.check("it can fail instead", d.failed("I couldn't verify that."))
        r.check("  and that is a fault", doing.is_fault(d.phase))
        r.check("  which never reads as success", "done" not in d.line.lower())

    # ── I. blocked, and K. ambiguous ────────────────────────────────────
    r.head("Refusing, and not guessing")
    with guard(r, "blocked"):
        d = doing.Doing()
        d.begin("t5", CLICK)
        d.blocked("I can't safely identify the target.")
        r.check("blocked says why", "safely" in d.line)
        r.check("  and is not dressed up as an error",
                not doing.is_fault(d.phase), "a refusal is not a crash")

    with guard(r, "ambiguity"):
        d = doing.Doing()
        d.begin("t6", CLICK)
        d.ambiguous("I found two possible Save buttons. Which one?")
        r.check("it asks rather than picks", d.phase == doing.AMBIGUOUS)
        r.check("  showing the question", "two possible" in d.line)
        r.check("  and never a score or a coordinate",
                not any(ch.isdigit() for ch in d.line))
        r.check("there is no target while it is ambiguous", d.target is None,
                "ambiguity must never resolve to a visual guess")

    # ── J. waiting, and 9. recovery ─────────────────────────────────────
    r.head("Waiting, and noticing that things moved")
    with guard(r, "waiting"):
        d = doing.Doing()
        d.begin("t7", CLICK)
        d.waiting()
        r.check("waiting is stable and plain", d.phase == doing.WAITING)
        r.check("  and says so",
                "you" in d.line.lower() or "go ahead" in d.line.lower())

    with guard(r, "recovery"):
        d = doing.Doing()
        d.begin("t8", CLICK)
        d.asking(doing.consent_for(computer_use.LOW, CLICK))
        d.granted()
        d.executing()
        d.verifying()
        r.check("a moved target is noticed", d.recovering("target_moved"))
        r.check("  and said as continuation", d.phase == doing.RECOVERING)
        r.check("  in words, not a code",
                "target_moved" not in d.line and len(d.line) > 8, d.line)
        for code in ("target_disappeared", "window_changed", "app_changed",
                     "action_registered_but_no_effect", "modal_appeared"):
            said = doing.recovery_words(code)
            r.check("  %-34s -> %s" % (code, said),
                    bool(said) and code not in said and said[0].isupper())

    # ── L / M. stale events ─────────────────────────────────────────────
    r.head("A task that has moved on")
    with guard(r, "stale task and action events"):
        d = doing.Doing()
        d.begin("old", CLICK, seq=5)
        d.begin("new", CLICK, seq=9)
        r.check("a newer task replaces the old", d.task_id == "new")
        r.check("an event from the old task is ignored",
                not d.executing(task_id="old"),
                "a stale task must not move him or claim anything")
        r.check("  and the phase is untouched", d.phase != doing.EXECUTING)
        r.check("an out-of-order event is ignored", not d.executing(seq=3))
        r.check("the current task's event is taken", d.executing(seq=11))

    with guard(r, "a finished task ignores what follows"):
        d = doing.Doing()
        d.begin("t9", CLICK)
        d.asking(doing.consent_for(computer_use.LOW, CLICK))
        d.granted()
        d.executing()
        d.verifying()
        d.verified()
        r.check("nothing more is accepted", not d.executing())
        r.check("and there is no live target", d.target is None)

    # ── N. multi-step ───────────────────────────────────────────────────
    r.head("One step at a time")
    with guard(r, "the counter"):
        d = doing.Doing()
        d.begin("t10", doing.Intent(verb="Open", target="Settings"),
                index=1, total=4)
        r.check("the step is named", "Step 1 of 4" in d.line, d.line)
        d.begin("t10", doing.Intent(verb="Choose", target="Dark Mode"),
                index=2, total=4, seq=20)
        r.check("the next step replaces it", "Step 2 of 4" in d.line, d.line)
        r.check("  and names the new intent", "Dark Mode" in d.line)
        one = doing.Doing()
        one.begin("t12", CLICK, index=1, total=1)
        r.check("a single-step task carries no counter",
                "Step" not in one.line, one.line)

    # ── C / P. the target, borrowed from GUI-6 ──────────────────────────
    r.head("One target on screen, still")
    with guard(r, "the shell"):
        shell = stand_in()
        shell.do_begin("t13", CLICK, index=1, total=1)
        shell.do_target(x=420.0, y=300.0, label="Save")
        r.check("he leaves home for the target",
                shell.presence.phase == motion.MOVING)
        r.check("nothing is drawn while travelling", shell.guide.shown == [])
        settle(shell)
        r.check("the indication follows the settle", len(shell.guide.shown) == 1)
        r.check("and it is the lesson's one target, not a second mark",
                shell.lesson.current is not None,
                "DO borrows GUI-6's indicator rather than drawing its own")

    with guard(r, "no duplicate target"):
        shell = stand_in()
        shell.do_begin("t14", CLICK)
        shell.do_target(x=420.0, y=300.0, label="Save")
        settle(shell)
        before = len(shell.guide.shown)
        shell.do_target(x=900.0, y=600.0, label="Save As")
        settle(shell)
        r.check("a new target replaces the old",
                len(shell.guide.shown) == before + 1)
        r.check("  and only one is current",
                shell.lesson.current.label == "Save As")

    # ── H / O. cancelling and coming home ───────────────────────────────
    r.head("Stopping clears everything")
    with guard(r, "cancel"):
        shell = stand_in()
        shell.do_begin("t15", CLICK)
        shell.do_target(x=420.0, y=300.0, label="Save")
        settle(shell)
        shell.do_ask(doing.consent_for(computer_use.MEDIUM, CLICK))
        shell.do_finish(doing.CANCELLED)
        r.check("the indication is closed", shell.guide.closed >= 1)
        r.check("no permission is left pending", shell.doing.consent is None)
        r.check("the lesson is over too", not shell.lesson.live,
                "no stale target ring")
        settle(shell, 6.0)
        r.check("and he is home", shell.presence.phase == motion.HOME)

    # ── safety ──────────────────────────────────────────────────────────
    r.head("Showing, never doing")
    with guard(r, "the boundary"):
        r.check("computer use is off", computer_use.ENABLED is False)
        banned = ("CGEventPost", "_post_click", "kCGEventLeftMouseDown",
                  "CGEventCreateMouseEvent", "CGEventCreateKeyboardEvent")
        for name in ["skills/doing.py", "app_shell.py", "diablo_app.py",
                     "app/app.js", "app/surface.js"]:
            text = open(os.path.join(HOME, name)).read()
            r.check("%s posts no events" % name,
                    not any(w in text for w in banned))
        # Parsed, not grepped: a docstring line beginning "from a superseded
        # task…" is prose, and a word search reads it as an import.
        import ast
        tree = ast.parse(open(os.path.join(HOME, "skills/doing.py")).read())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add((node.module or "").split(".")[0])
        r.check("doing.py only decides what is true",
                imports <= {"dataclasses", "typing", "skills"},
                "imports: %s" % sorted(imports))
        bridge = open(os.path.join(HOME, "diablo_app.py")).read()
        r.check("no production do_engine.run is wired",
                "do_engine.run(" not in bridge,
                "the GUI renders DO state; it does not start DO")

    # ── the page ────────────────────────────────────────────────────────
    r.head("The surface's own rules")
    with guard(r, "node"):
        proc = subprocess.run(
            ["node", os.path.join(HERE, "js", "doing_states.mjs")],
            capture_output=True, text=True, env=dict(os.environ, DIABLO_HOME=HOME))
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("ok "):
                r.check(stripped[3:], True)
            elif stripped.startswith("FAIL "):
                r.check(stripped[5:], False)
        if proc.returncode not in (0, 1):
            r.check("the node suite ran", False, (proc.stderr or "")[-200:])

    return r.report("doing")


if __name__ == "__main__":
    sys.exit(run())
