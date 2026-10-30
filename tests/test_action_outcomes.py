#!/usr/bin/env python3
"""
What an action came to: confirmed, unverifiable, suspected no-op, or refused.

computer_use used to report a click as a boolean. "Something on screen
changed" counted as verified, a click that changed nothing was clicked again
automatically, a click with no capture behind it went ahead, and refusals were
sentences that the DO engine had to search for words. Each of those is pinned
here against the real click(), act_on(), type_text() and do_engine.run(), with
every edge of the machine replaced:

  - the event poster is a stub that counts, and CGEventPost is a tripwire;
  - capture, comparison, perception and the window list are doubles;
  - the master switch is turned on inside each check and restored after it.

Nothing is clicked, typed or captured.
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

BASE = tempfile.mkdtemp(prefix="diablo-outcomes-")
os.environ["DIABLO_TRACE"] = os.path.join(BASE, "trace.jsonl")

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import ax, computer_use as cu  # noqa: E402
from skills import display, grounding_gate, perception, screen, screen_context  # noqa: E402
from skills import do_engine as engine  # noqa: E402
from skills.do_engine import Goal, Step, TaskState  # noqa: E402

WINDOW = {"id": 7, "app": "Fixture", "title": "Toggle", "x": 0.0, "y": 0.0,
          "width": 800.0, "height": 600.0}
VAULT = dict(WINDOW, app="1Password", sensitive=True)


# ─── Doubles ─────────────────────────────────────────────────────────────────

class Frame:
    """A capture that holds no pixels."""

    def __init__(self, ok=True, fingerprint=(10, 10, 10, 10), stale=False):
        self.ok = ok
        self.problem = "" if ok else "that window captured as solid black"
        self.fingerprint = list(fingerprint) if ok else []
        self.app = WINDOW["app"]
        self.captured_at = 0.0
        self.x, self.y, self.width, self.height = 0.0, 0.0, 800.0, 600.0
        self.frame_id = "F"
        self._stale = stale

    def is_stale(self, _limit):
        return self._stale

    def release(self):
        pass


class Change:
    def __init__(self, same=False, appeared=()):
        self.same = same
        self.appeared = list(appeared)

    def describe(self):
        if self.same:
            return "Nothing I can read has changed."
        if self.appeared:
            return "New on screen: " + ", ".join(self.appeared)
        return "The window moved."


class Candidate:
    def __init__(self, label="Dark mode", confidence=0.97, enabled=True):
        self.label = label
        self.confidence = confidence
        self.enabled = enabled
        self.sources = ["accessibility"]
        self.evidence = [f"the application calls it switch “{label}”"]
        self.centre = (120.0, 80.0)
        self.role = "switch"

    def describe(self):
        return f"switch “{self.label}”"

    def why(self):
        return self.evidence[0]


class Reading:
    def __init__(self, best=None, ambiguous=False):
        self.best = best
        self.found = best is not None
        self.ambiguous = ambiguous
        self.candidates = [best] if best else []

    def question(self):
        return "I can see two things that could be it. Which one?"


# Controls the stand-in screen shows, keyed by centre. T() puts one there.
ON_SCREEN = {}


def T(x, y, label, named=True):
    """
    A target as grounding_gate would issue it: a 20-point control centred on
    (x, y), validated against the frame these tests use. The control is put
    on the stand-in screen too, so the revalidation before the click finds it.
    """
    ON_SCREEN[(x, y)] = ax.Element(role="AXButton", title=label, enabled=True,
                                   x=x - 10, y=y - 10, width=20, height=20)
    return grounding_gate._issue(
        role="AXButton", label=label, x=x - 10, y=y - 10, width=20, height=20, pid=1,
        window_id=WINDOW["id"], window_bounds=(0.0, 0.0, 800.0, 600.0), frame_id="F",
        validated_at=time.time(), source_model="test", named=named)


class Machine:
    """Every edge computer_use touches, replaced, and put back afterwards."""

    def __init__(self, change=None, reading=None, fresh=None, captures=None,
                 post_ok=True, delivered=None, on_top=None, focus="field", fields=(),
                 on_post=None):
        self.change = change if change is not None else Change(same=True)
        self.reading = reading if reading is not None else Reading(Candidate())
        self.fresh = fresh if fresh is not None else self.reading
        self.captures = list(captures or [])       # frames to hand out, in order
        # Events the poster reports delivered: both halves of a click, none,
        # or (for a partial delivery) one.
        self.delivered = delivered if delivered is not None else (2 if post_ok else 0)
        self.clicks = []
        self.cgevents = []
        self.reads = 0
        # What act_on's last look finds at the point. None: the control
        # perception chose, as it chose it. A REJECT_* verdict: that refusal.
        self.on_top = on_top
        # What has keyboard focus: a text field unless a test says otherwise.
        self.focus = (ax.Element(role="AXTextField", description="Quantity", focused=True,
                                 x=300, y=100, width=120, height=24)
                      if focus == "field" else focus)
        # The window's published controls, for the DO engine's field lookup.
        self.fields = list(fields)
        # What a click does to the stand-in screen, for checks that read a
        # control's own state afterwards.
        self.on_post = on_post
        self._undo = []

    def capture(self, *_a, **_k):
        return self.captures.pop(0) if self.captures else Frame()

    def read(self, *_a, **_k):
        self.reads += 1
        return self.reading if self.reads == 1 else self.fresh

    def post(self, x, y):
        self.clicks.append((x, y))
        if self.on_post:
            self.on_post(x, y)
        return self.delivered

    def hit(self, x, y):
        """What accessibility says is under a point: a control T() put there."""
        for element in ON_SCREEN.values():
            if (element.x <= x < element.x + element.width
                    and element.y <= y < element.y + element.height):
                return ax.Hit(element=element, pid=1, window_frame=(0.0, 0.0, 800.0, 600.0))
        return ax.Hit(pid=1, window_frame=(0.0, 0.0, 800.0, 600.0))

    def last_look(self, x, y, label, role, window, **_k):
        verdict = self.on_top or grounding_gate.VALIDATED
        return grounding_gate.Validation(verdict, "the stand-in screen says so", (x, y),
                                         found=self.hit(x, y).element)

    def patch(self, owner, name, value):
        self._undo.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def __enter__(self):
        self.patch(cu, "ENABLED", True)
        self.patch(cu, "_post_click", self.post)
        self.patch(cu, "_GRANT", cu.Grant())
        self.patch(cu.time, "sleep", lambda _s: None)
        self.patch(screen, "capture_this_window", self.capture)
        self.patch(screen, "capture_display", self.capture)
        self.patch(screen, "compare", lambda b, a, w=None: self.change)
        self.patch(display, "on_any_display", lambda x, y: x >= 0 and y >= 0)
        self.patch(display, "for_point", lambda x, y: None)
        self.patch(screen_context, "list_windows", lambda *a, **k: [WINDOW])
        self.patch(screen_context, "frontmost_window", lambda: WINDOW)
        self.patch(screen_context, "window_by_id", lambda i: WINDOW)
        self.patch(perception, "read", self.read)
        self.patch(ax, "hit_test", self.hit)
        self.patch(ax, "_pid_for_window", lambda window: 1)
        self.patch(ax, "focused_element", lambda pid: self.focus)
        self.patch(ax, "elements_for_window", lambda *a, **k: list(self.fields))
        self.patch(grounding_gate, "check_point", self.last_look)
        try:
            import Quartz
            self.patch(Quartz, "CGEventPost",
                       lambda *a, **k: self.cgevents.append(a))
        except ImportError:
            pass
        return self

    def __exit__(self, *exc):
        for owner, name, value in reversed(self._undo):
            setattr(owner, name, value)
        ON_SCREEN.clear()


def yes(_question):
    return True


def no(_question):
    return False


def run_task(machine, planner, goal_holds):
    """One DO task against the machine, with the goal decided by the test."""
    saved = (Goal.verified, engine._active)
    Goal.verified = lambda self, window=None: goal_holds()
    engine._active = None
    try:
        return engine.run("turn on dark mode", Goal("text_on_screen", "Dark"),
                          window=WINDOW, confirm=yes, planner=planner)
    finally:
        Goal.verified = saved[0]
        engine._active = None


# ─── The checks ──────────────────────────────────────────────────────────────

def run():
    r = Results()
    switched_off_before = cu.ENABLED is False

    r.head("A click that changes nothing is reported, not repeated")
    with guard(r, "a toggle through act_on"):
        with Machine(change=Change(same=True)) as m:
            record = cu.act_on("turn on dark mode", task="t", window=WINDOW, confirm=yes)
        r.check("exactly one click was attempted", len(m.clicks) == 1, str(m.clicks))
        r.check("the outcome is a suspected no-op", record.outcome == cu.SUSPECTED_NOOP,
                record.outcome)
        r.check("it says it has not clicked again",
                "not clicked it again" in record.spoken, record.spoken)
        r.check("nothing is marked retried", record.retried is False)
        r.check("no automatic retry is configured", cu.MAX_RETRIES == 0,
                str(cu.MAX_RETRIES))

    with guard(r, "a toggle through the DO loop"):
        with Machine(change=Change(same=True)) as m:
            task = run_task(m, lambda t, w: Step(verb="click", target="Dark mode",
                                                 expect="Dark", why="toggle"),
                            goal_holds=lambda: False)
        r.check("the planner naming it again does not click it again",
                len(m.clicks) == 1, f"{len(m.clicks)} clicks")
        r.check("the task stops rather than looping", task.state is TaskState.BLOCKED,
                task.state.value)
        r.check("and says why in plain words",
                "will not press it again" in task.summary(), task.summary())

    with guard(r, "a different control is still allowed after a no-op"):
        with Machine(change=Change(same=True)) as m:
            # Each request resolves to its own control, as perception would.
            perception.read = lambda intent, window, **k: Reading(Candidate(intent))
            names = iter(["Dark mode", "Appearance"])
            task = run_task(m, lambda t, w: Step(verb="click",
                                                 target=next(names, "Appearance"),
                                                 why="try"),
                            goal_holds=lambda: len(m.clicks) >= 2)
        r.check("a revised plan may try something else", len(m.clicks) == 2,
                f"{len(m.clicks)} clicks, ended {task.state.value}")

    r.head("Only the expected result confirms anything")
    with guard(r, "a generic change"):
        with Machine(change=Change(same=False, appeared=["Notification: 1 new"])):
            record = cu.act_on("turn on dark mode", window=WINDOW, confirm=yes,
                               expect="Dark mode is on")
        r.check("a change without the expected words is not confirmed",
                record.outcome == cu.UNVERIFIABLE, record.outcome)
        r.check("and is not reported as verified", record.verified is not True,
                str(record.verified))
        r.check("nor as a failure", record.verified is not False, str(record.verified))
        with Machine(change=Change(same=False, appeared=["Something"])):
            record = cu.act_on("turn on dark mode", window=WINDOW, confirm=yes)
        r.check("with no expectation at all, a change is unverifiable",
                record.outcome == cu.UNVERIFIABLE, record.outcome)
        r.check("and the explanation does not claim success",
                "could not confirm" in record.explain(), record.explain())

    with guard(r, "an unverifiable step is not a success in the DO loop"):
        with Machine(change=Change(same=False, appeared=["Something"])) as m:
            task = run_task(m, lambda t, w: Step(verb="click", target="Dark mode",
                                                 why="toggle"),
                            goal_holds=lambda: len(m.clicks) >= 1)
        r.check("the step is recorded as unverified",
                task.steps and task.steps[0].state is engine.ActionState.UNVERIFIED,
                task.steps[0].state.value if task.steps else "no steps")
        r.check("and completion came from the goal check", task.state is TaskState.COMPLETED,
                task.state.value)

    with guard(r, "the expected result"):
        with Machine(change=Change(same=False, appeared=["Dark mode is on"])):
            record = cu.act_on("turn on dark mode", window=WINDOW, confirm=yes,
                               expect="dark mode is on")
        r.check("the expected words appearing confirms it",
                record.outcome == cu.CONFIRMED and record.verified is True,
                f"{record.outcome} {record.verified}")

    with guard(r, "a capture that fails afterwards"):
        with Machine(change=Change(same=False, appeared=["Dark mode is on"]),
                     captures=[Frame(), Frame(), Frame(ok=False)]):
            record = cu.act_on("turn on dark mode", window=WINDOW, confirm=yes,
                               expect="Dark mode is on")
        r.check("an unreadable after-frame is unverifiable, never confirmed",
                record.outcome == cu.UNVERIFIABLE, record.outcome)

    with guard(r, "click() follows the same rule"):
        with Machine(change=Change(same=False, appeared=["Saved"])):
            result = cu.click(T(50, 50, "Continue"), frame=Frame(), confirm=yes)
        r.check("a change with no expectation is unverifiable",
                result.ok and result.outcome == cu.UNVERIFIABLE and result.verified is None,
                f"{result.outcome} {result.verified}")
        with Machine(change=Change(same=False, appeared=["Saved"])):
            result = cu.click(T(50, 50, "Continue"), frame=Frame(), confirm=yes, expect="Saved")
        r.check("the expected words confirm it",
                result.outcome == cu.CONFIRMED and result.verified is True,
                f"{result.outcome} {result.verified}")
        with Machine(change=Change(same=True)) as m:
            result = cu.click(T(50, 50, "Dark mode"), frame=Frame(), confirm=yes)
        r.check("no change is a suspected no-op, clicked once",
                result.outcome == cu.SUSPECTED_NOOP and len(m.clicks) == 1,
                f"{result.outcome}, {len(m.clicks)} clicks")

    with guard(r, "typing is sent, not verified"):
        with Machine() as m:
            result = cu.type_text("hello", task="t", confirm=yes)
            record = engine._as_record(result, Step(verb="type", value="hello"),
                                       engine.Task(intent="x",
                                                   goal=Goal("text_on_screen", "x")))
        r.check("typed text is unverifiable", result.outcome == cu.UNVERIFIABLE,
                result.outcome)
        r.check("and the DO record does not call it verified", record.verified is None,
                str(record.verified))
        # type_text posts through Quartz directly, so the tripwire is what
        # received them: key down and key up for the one chunk, and nothing
        # reached the real event tap.
        r.check("the keystrokes went to the tripwire, not the system",
                len(m.cgevents) == 2, f"{len(m.cgevents)} events intercepted")

    r.head("Every refusal has a code")
    exercised = set()

    def refused(label, result, code):
        exercised.add(code)
        outcome = getattr(result, "outcome", "")
        got = getattr(result, "refusal", "")
        sent = getattr(result, "ok", None) or getattr(result, "action", "")
        r.check(f"{label}: {code}", outcome == cu.REFUSED and got == code and not sent,
                f"{outcome} / {got!r}")

    with guard(r, "click() gates"):
        refused("switched off", cu.click(T(50, 50, "x"), frame=Frame(), confirm=yes),
                cu.SWITCHED_OFF)
        with Machine() as m:
            refused("off every display", cu.click(T(-5, -5, "x"), frame=Frame(), confirm=yes),
                    cu.OFF_DISPLAY)
            refused("no frame", cu.click(T(50, 50, "x"), confirm=yes), cu.FRAME_MISSING)
            refused("an unreadable frame", cu.click(T(50, 50, "x"), frame=Frame(ok=False),
                                                     confirm=yes), cu.FRAME_INVALID)
            refused("nobody to ask", cu.click(T(50, 50, "Continue"), frame=Frame()),
                    cu.NO_CONFIRMATION)
            refused("the user said no", cu.click(T(50, 50, "Continue"), frame=Frame(),
                                                  confirm=no), cu.DECLINED)
            refused("destructive, nobody to ask", cu.click(T(50, 50, "Delete all"),
                                                           frame=Frame()), cu.NO_CONFIRMATION)
            refused("destructive, declined", cu.click(T(50, 50, "Delete all"),
                                                      frame=Frame(), confirm=no), cu.DECLINED)
            refused("a bare point, never validated",
                    cu.click((50, 50), frame=Frame(), confirm=yes), cu.NOT_VALIDATED)
        r.check("none of those sent a click", not m.clicks, str(m.clicks))
        with Machine(on_top=grounding_gate.REJECT_WRONG_WINDOW) as m:
            refused("act_on: another window is on top of the control",
                    cu.act_on("turn on dark mode", window=WINDOW, confirm=yes),
                    cu.WRONG_WINDOW)
        r.check("and that sent no click either", not m.clicks, str(m.clicks))
        with Machine() as m:
            screen_context.window_by_id = lambda i: VAULT
            refused("a password manager", cu.click(T(50, 50, "x"), frame=Frame(),
                                                   confirm=yes), cu.SENSITIVE_WINDOW)

    with guard(r, "a missing or stale frame cannot authorise a click"):
        with Machine() as m:
            cu.click(T(50, 50, "x"), confirm=yes)
        r.check("a click with no frame never reaches the event poster", not m.clicks)
        with Machine(captures=[Frame(ok=False)]) as m:
            refused("stale, and the fresh look failed",
                    cu.click(T(50, 50, "x"), frame=Frame(stale=True), confirm=yes),
                    cu.FRAME_INVALID)
        r.check("no click after a failed fresh look", not m.clicks, str(m.clicks))
        with Machine(captures=[Frame(fingerprint=(250, 250, 250, 250))]) as m:
            refused("stale, and the screen moved",
                    cu.click(T(50, 50, "x"), frame=Frame(stale=True), confirm=yes),
                    cu.SCREEN_CHANGED)
        r.check("no click after the screen moved", not m.clicks, str(m.clicks))
        with Machine(captures=[Frame(ok=False)]) as m:
            refused("no readable before-frame",
                    cu.click(T(50, 50, "x"), frame=Frame(), confirm=yes), cu.FRAME_INVALID)
        r.check("no click without a baseline to verify against", not m.clicks)
        with Machine(post_ok=False):
            refused("the event could not be posted",
                    cu.click(T(50, 50, "x"), frame=Frame(), confirm=yes), cu.INPUT_UNAVAILABLE)

    with guard(r, "act_on() gates"):
        with Machine():
            screen_context.frontmost_window = lambda: None
            refused("no window", cu.act_on("x", confirm=yes), cu.NO_WINDOW)
        with Machine():
            refused("a password manager", cu.act_on("x", window=VAULT, confirm=yes),
                    cu.SENSITIVE_WINDOW)
        with Machine(captures=[Frame(ok=False)]):
            refused("a window that captures black",
                    cu.act_on("x", window=WINDOW, confirm=yes), cu.FRAME_INVALID)
        with Machine(reading=Reading(None)):
            refused("nothing found", cu.act_on("x", window=WINDOW, confirm=yes),
                    cu.TARGET_NOT_FOUND)
        with Machine(reading=Reading(Candidate(enabled=False))):
            refused("a disabled control", cu.act_on("x", window=WINDOW, confirm=yes),
                    cu.TARGET_DISABLED)
        with Machine(reading=Reading(Candidate(), ambiguous=True)):
            refused("two candidates, nobody to ask", cu.act_on("x", window=WINDOW),
                    cu.TARGET_UNCERTAIN)
        with Machine(reading=Reading(Candidate(), ambiguous=True)):
            refused("two candidates, user said no",
                    cu.act_on("x", window=WINDOW, confirm=no), cu.DECLINED)
        with Machine():
            refused("permission declined", cu.act_on("x", window=WINDOW, confirm=no),
                    cu.DECLINED)
        with Machine(fresh=Reading(None)):
            refused("gone by the time it was approved",
                    cu.act_on("x", window=WINDOW, confirm=yes), cu.TARGET_GONE)
        with Machine(captures=[Frame(), Frame(ok=False)]):
            refused("no readable before-frame",
                    cu.act_on("x", window=WINDOW, confirm=yes), cu.FRAME_INVALID)
        with Machine(post_ok=False):
            refused("the event could not be posted",
                    cu.act_on("x", window=WINDOW, confirm=yes), cu.INPUT_UNAVAILABLE)

    with guard(r, "type_text() gates"):
        with Machine():
            refused("nothing to type", cu.type_text("", confirm=yes), cu.NOTHING_TO_TYPE)
            refused("nobody to ask", cu.type_text("hi"), cu.NO_CONFIRMATION)
            screen_context.frontmost_window = lambda: VAULT
            refused("a password manager in front", cu.type_text("hi", confirm=yes),
                    cu.SENSITIVE_WINDOW)
        asked = []

        def counting_yes(question):
            asked.append(question)
            return True
        with Machine(focus=ax.Element(role="AXButton", title="Delete", focused=True,
                                      x=10, y=10, width=60, height=24)) as m:
            refused("a button has focus, so a space would press it",
                    cu.type_text("a b", confirm=counting_yes), cu.NO_FIELD)
        r.check("no keystroke reached anything", not m.cgevents, f"{len(m.cgevents)} events")
        r.check("and nobody was asked about typing into nothing", not asked, str(asked))
        with Machine(focus=None) as m:
            refused("focus cannot be read", cu.type_text("hi", confirm=yes), cu.NO_FIELD)
        r.check("no keystroke when focus is unknown", not m.cgevents,
                f"{len(m.cgevents)} events")

    r.head("A click that changes no words, proven by the control's own state")
    import dataclasses

    def at(centre):
        c = Candidate("Quantity")
        c.centre = centre
        return Reading(c)
    with guard(r, "a click that focuses a text field"):
        ON_SCREEN[(360, 112)] = ax.Element(role="AXTextField", description="Quantity",
                                           enabled=True, x=300, y=100, width=120, height=24)
        with Machine(change=Change(same=True), reading=at((360.0, 112.0))):
            record = cu.act_on("Quantity", window=WINDOW, confirm=yes)
        r.check("is confirmed by the focus, not called a no-op",
                record.outcome == cu.CONFIRMED and "focus" in record.observed,
                f"{record.outcome}: {record.observed}")
    with guard(r, "the same click, with the focus elsewhere"):
        ON_SCREEN[(360, 112)] = ax.Element(role="AXTextField", description="Quantity",
                                           enabled=True, x=300, y=100, width=120, height=24)
        with Machine(change=Change(same=True), reading=at((360.0, 112.0)), focus=None):
            record = cu.act_on("Quantity", window=WINDOW, confirm=yes)
        r.check("is still a suspected no-op", record.outcome == cu.SUSPECTED_NOOP,
                record.outcome)

    def tick(x, y):
        for key, element in list(ON_SCREEN.items()):
            if element.role == "AXCheckBox":
                ON_SCREEN[key] = dataclasses.replace(element, state=1.0 - element.state)
    with guard(r, "a checkbox that changed"):
        ON_SCREEN[(300, 292)] = ax.Element(role="AXCheckBox", title="Priority", enabled=True,
                                           state=0.0, x=220, y=280, width=160, height=24)
        with Machine(change=Change(same=True), reading=at((300.0, 292.0)), on_post=tick):
            record = cu.act_on("Priority", window=WINDOW, confirm=yes)
        r.check("is confirmed by its value", record.outcome == cu.CONFIRMED
                and "now on" in record.observed, f"{record.outcome}: {record.observed}")
    with guard(r, "a checkbox that did not change"):
        ON_SCREEN[(300, 292)] = ax.Element(role="AXCheckBox", title="Priority", enabled=True,
                                           state=0.0, x=220, y=280, width=160, height=24)
        with Machine(change=Change(same=True), reading=at((300.0, 292.0))):
            record = cu.act_on("Priority", window=WINDOW, confirm=yes)
        r.check("is still a suspected no-op", record.outcome == cu.SUSPECTED_NOOP,
                record.outcome)

    r.head("A checkbox is pressed on its box, not in the empty part of its frame")
    with guard(r, "a wide checkbox"):
        wide = Candidate("Priority")
        wide.role = "AXCheckBox"
        wide.x, wide.y, wide.width, wide.height = 220.0, 280.0, 160.0, 24.0
        wide.centre = (300.0, 292.0)
        with Machine(change=Change(same=False, appeared=["x"]), reading=Reading(wide)) as m:
            cu.act_on("Priority", window=WINDOW, confirm=yes)
        r.check("pressed at the box, inside the control", m.clicks == [(232.0, 292.0)],
                str(m.clicks))
    with guard(r, "a button is still pressed at its centre"):
        button = Candidate("Save")
        button.role = "AXButton"
        button.x, button.y, button.width, button.height = 220.0, 280.0, 160.0, 24.0
        button.centre = (300.0, 292.0)
        with Machine(change=Change(same=False, appeared=["x"]), reading=Reading(button)) as m:
            cu.act_on("Save", window=WINDOW, confirm=yes)
        r.check("at the centre", m.clicks == [(300.0, 292.0)], str(m.clicks))

    r.head("Typing is read back from the field")
    with guard(r, "the field holds exactly what was typed"):
        with Machine(focus=ax.Element(role="AXTextField", description="Quantity",
                                      value="7", focused=True)):
            typed = cu.type_text("7", confirm=yes)
        r.check("confirmed", typed.outcome == cu.CONFIRMED, typed.outcome)
    with guard(r, "the field holds more than was typed"):
        with Machine(focus=ax.Element(role="AXTextField", description="Quantity",
                                      value="33", focused=True)):
            typed = cu.type_text("3", confirm=yes)
        r.check("not confirmed, and what it reads is said",
                typed.outcome == cu.UNVERIFIABLE and "33" in typed.spoken, typed.spoken)

    r.head("The planner sees what fields hold and whether boxes are ticked")
    with guard(r, "control lines"):
        field = ax.Element(role="AXTextField", description="Quantity", value="3")
        empty = ax.Element(role="AXTextField", description="Quantity")
        box = ax.Element(role="AXCheckBox", title="Priority", state=1.0)
        r.check("a filled field says what it contains",
                "contains “3”" in engine._control_line(field), engine._control_line(field))
        r.check("an empty one says so", "(empty)" in engine._control_line(empty),
                engine._control_line(empty))
        r.check("a ticked box says on", engine._control_line(box).endswith("(on)"),
                engine._control_line(box))

    with guard(r, "the planner's copy of a control line"):
        for said, bare in (('CheckBox "Priority" (off)', 'CheckBox "Priority"'),
                           ("Quantity (contains “3”)", "Quantity"),
                           ("Save (sent, not confirmed) (failed)", "Save"),
                           ("Export (PDF)", "Export (PDF)")):
            r.check(f"{said!r} is read as {bare!r}", engine._bare_target(said) == bare,
                    engine._bare_target(said))

    r.head("A goal with two parts holds only when both do")
    with guard(r, "both texts"):
        saved = engine._read_window_text
        try:
            goal = Goal("text_on_screen", "Saved order 10484: quantity 9", "",
                        ("Saved order 10481: quantity 3",))
            engine._read_window_text = lambda w: "Saved order 10484: quantity 9, priority off"
            r.check("the last part alone is not done", not goal.verified(WINDOW))
            engine._read_window_text = lambda w: ("Saved order 10481: quantity 3, priority off\n"
                                                  "Saved order 10484: quantity 9, priority off")
            r.check("both parts are", goal.verified(WINDOW))
            r.check("and it says both", "10481" in goal.describe()
                    and "10484" in goal.describe(), goal.describe())
        finally:
            engine._read_window_text = saved

    r.head("Pressed without proof: refused again until the screen moves on")
    changed = Change(same=False, appeared=["Order 10484 open."])

    def by_name(intent, window=None, *_a, **_k):
        c = Candidate(intent)
        c.centre = {"Save": (100.0, 300.0), "Order 10484": (60.0, 120.0)}.get(intent,
                                                                             (10.0, 10.0))
        return Reading(c)

    def clicks(*targets):
        steps = iter([Step(verb="click", target=t, why="next") for t in targets])
        return lambda t, w: next(steps, Step(verb="done", why="finished"))
    with guard(r, "Save, another order, then Save again"):
        with Machine(change=changed) as m:
            perception.read = by_name
            task = run_task(m, clicks("Save", "Order 10484", "Save"), goal_holds=lambda: False)
        r.check("all three were pressed", len(m.clicks) == 3, str(m.clicks))
    with guard(r, "Save twice with nothing between"):
        with Machine(change=changed) as m:
            perception.read = by_name
            task = run_task(m, clicks("Save", "Save"), goal_holds=lambda: False)
        r.check("the second is refused", len(m.clicks) == 1
                and task.actions[-1].refusal == cu.ALREADY_TRIED,
                f"{m.clicks}, {task.actions[-1].refusal}")

    r.head("A field named for typing is clicked first, through the gates")
    quantity = ax.Element(role="AXTextField", description="Quantity", focused=False,
                          enabled=True, x=300, y=100, width=120, height=24)

    def typing_step(target):
        steps = iter([Step(verb="type", target=target, value="7", why="set it")])
        return lambda t, w: next(steps, Step(verb="done", why="finished"))
    with guard(r, "type into a named field that is not focused"):
        done = iter([False, True])
        with Machine(fields=[quantity]) as m:
            task = run_task(m, typing_step("Quantity"), goal_holds=lambda: next(done, True))
        r.check("the field was clicked once, by act_on", len(m.clicks) == 1, str(m.clicks))
        r.check("then the text was typed", len(m.cgevents) == 2, f"{len(m.cgevents)} events")
        r.check("both are in the task's record",
                [a.action for a in task.actions] == ["click", "type"],
                str([a.action for a in task.actions]))
    with guard(r, "a target that names a button is not clicked to type"):
        done = iter([False, True])
        save = ax.Element(role="AXButton", title="Save", enabled=True,
                          x=10, y=10, width=60, height=24)
        with Machine(fields=[save]) as m:
            run_task(m, typing_step("Save"), goal_holds=lambda: next(done, True))
        r.check("nothing was clicked", not m.clicks, str(m.clicks))
    with guard(r, "two fields with the name: no guess"):
        done = iter([False, True])
        twin = ax.Element(role="AXTextField", description="Quantity", focused=False,
                          enabled=True, x=300, y=200, width=120, height=24)
        with Machine(fields=[quantity, twin]) as m:
            run_task(m, typing_step("Quantity"), goal_holds=lambda: next(done, True))
        r.check("neither was clicked", not m.clicks, str(m.clicks))

    r.head("The DO engine reads codes, not sentences")
    with guard(r, "classification"):
        step = Step(verb="click", target="Save", expect="Saved")
        for code, wanted in ((cu.DECLINED, engine.PERMISSION_FAILURE),
                             (cu.NO_CONFIRMATION, engine.PERMISSION_FAILURE),
                             (cu.SENSITIVE_WINDOW, engine.PERMISSION_FAILURE),
                             (cu.INPUT_UNAVAILABLE, engine.PERMISSION_FAILURE),
                             (cu.SWITCHED_OFF, engine.PERMISSION_FAILURE),
                             (cu.TARGET_UNCERTAIN, engine.AMBIGUITY),
                             (cu.TARGET_GONE, engine.TARGET_GONE)):
            record = cu.ActionRecord(verdict="refuse")
            record.settle(cu.REFUSED, "Left it alone.", code)
            got = engine.classify(record, WINDOW, WINDOW, step)
            r.check(f"{code} -> {wanted}", got == wanted, got)
        legacy = cu.ActionRecord(verdict="refuse",
                                 spoken="I am not allowed to control the computer.")
        r.check("a record with no code still classifies by its wording",
                engine.classify(legacy, WINDOW, WINDOW, step) == engine.PERMISSION_FAILURE)

    with guard(r, "a declined permission stops the task instead of asking again"):
        with Machine() as m:
            asked = []

            def deny(question):
                asked.append(question)
                return False

            saved = (Goal.verified, engine._active)
            Goal.verified = lambda self, window=None: False
            try:
                task = engine.run("turn on dark mode", Goal("text_on_screen", "Dark"),
                                  window=WINDOW, confirm=deny,
                                  planner=lambda t, w: Step(verb="click",
                                                            target="Dark mode", why="go"))
            finally:
                Goal.verified = saved[0]
                engine._active = None
        r.check("the user is asked once", len(asked) == 1, f"asked {len(asked)} times")
        r.check("and the task stops", task.state is TaskState.BLOCKED, task.state.value)
        r.check("without clicking", not m.clicks)

    with guard(r, "the old boolean follows the outcome"):
        r.check("confirmed is True", cu.legacy_verified(cu.CONFIRMED) is True)
        r.check("a suspected no-op is False", cu.legacy_verified(cu.SUSPECTED_NOOP) is False)
        r.check("unverifiable is None", cu.legacy_verified(cu.UNVERIFIABLE) is None)
        r.check("refused is None", cu.legacy_verified(cu.REFUSED) is None)

    # ── Found by an independent review of the first version of this change ──
    r.head("The same control is never pressed twice without proof")
    with guard(r, "a click that changes the wrong thing is not repeated"):
        # Not a no-op: something changed, just not what was expected. The
        # first version let this repeat on the same toggle up to MAX_STEPS.
        with Machine(change=Change(same=False, appeared=["Off"])) as m:
            task = run_task(m, lambda t, w: Step(verb="click", target="Dark mode",
                                                 expect="Dark", why="toggle"),
                            goal_holds=lambda: False)
        r.check("one click on the toggle, not twelve", len(m.clicks) == 1,
                f"{len(m.clicks)} clicks, ended {task.state.value}")
        r.check("and the task stops", task.state is TaskState.BLOCKED, task.state.value)

    with guard(r, "rewording does not get round it"):
        words = iter(["Dark mode", "dark mode switch", "the Dark Mode toggle"])
        with Machine(change=Change(same=True)) as m:
            task = run_task(m, lambda t, w: Step(verb="click",
                                                 target=next(words, "Dark mode"),
                                                 why="toggle"),
                            goal_holds=lambda: False)
        r.check("the resolved control is what is remembered, not the words",
                len(m.clicks) == 1, f"{len(m.clicks)} clicks")

    with guard(r, "act_on refuses a control it is told to avoid"):
        avoid = [(Candidate().describe(), 120.0, 80.0)]
        with Machine() as m:
            record = cu.act_on("x", window=WINDOW, confirm=yes, avoid=avoid)
        refused("refused before anything is sent", record, cu.ALREADY_TRIED)
        r.check("and nothing was clicked", not m.clicks, f"{len(m.clicks)} clicks")
        with Machine() as m:
            record = cu.act_on("x", window=WINDOW, confirm=yes,
                               avoid=[(Candidate().describe(), 600.0, 500.0)])
        r.check("a control of the same name elsewhere is a different control",
                len(m.clicks) == 1, f"{record.outcome}/{record.refusal}")

    r.head("The control approved is the control pressed")
    with guard(r, "the fresh look finds something else"):
        with Machine(reading=Reading(Candidate("Dark mode")),
                     fresh=Reading(Candidate("Delete account"))) as m:
            record = cu.act_on("x", window=WINDOW, confirm=yes)
        refused("a different control is refused", record, cu.TARGET_CHANGED)
        r.check("and not clicked", not m.clicks, f"{len(m.clicks)} clicks")
    with guard(r, "the fresh look is ambiguous or the control is disabled now"):
        with Machine(fresh=Reading(Candidate(), ambiguous=True)) as m:
            record = cu.act_on("x", window=WINDOW, confirm=yes)
        r.check("ambiguous now is refused", record.refusal == cu.TARGET_UNCERTAIN
                and not m.clicks, record.refusal)
        with Machine(fresh=Reading(Candidate(enabled=False))) as m:
            record = cu.act_on("x", window=WINDOW, confirm=yes)
        r.check("disabled now is refused", record.refusal == cu.TARGET_DISABLED
                and not m.clicks, record.refusal)

    r.head("Saying exactly what happened")
    with guard(r, "half a click"):
        with Machine(delivered=1) as m:
            record = cu.act_on("x", window=WINDOW, confirm=yes)
        r.check("a partly delivered click is partial, not refused",
                record.outcome == cu.PARTIAL and record.action == "click",
                f"{record.outcome} action={record.action!r}")
        with Machine(delivered=1) as m:
            task = run_task(m, lambda t, w: Step(verb="click", target="Dark mode",
                                                 why="go"), goal_holds=lambda: False)
        r.check("and the task stops there", task.state is TaskState.BLOCKED
                and len(m.clicks) == 1, f"{task.state.value}, {len(m.clicks)} clicks")
    with guard(r, "typing that stops halfway"):
        with Machine() as m:
            import Quartz
            sent = []

            def flaky(*a, **k):
                if len(sent) >= 2:
                    raise RuntimeError("event tap went away")
                sent.append(a)
            Quartz.CGEventPost = flaky
            result = cu.type_text("x" * 45, confirm=yes)
        r.check("typing that was cut off is partial",
                result.outcome == cu.PARTIAL and result.ok, result.outcome)
    with guard(r, "a failed after-look is not described as a change"):
        with Machine(change=Change(same=False, appeared=["Dark mode is on"]),
                     captures=[Frame(), Frame(), Frame(ok=False)]):
            record = cu.act_on("x", window=WINDOW, confirm=yes, expect="Dark mode is on")
        r.check("it says it could not look", "could not look" in record.spoken
                and "changed" not in record.spoken, record.spoken)
    with guard(r, "the expected words must be words"):
        with Machine(change=Change(same=False, appeared=["Notification: 1 new"])):
            record = cu.act_on("x", window=WINDOW, confirm=yes, expect="on")
        r.check("'on' inside 'Notification' confirms nothing",
                record.outcome == cu.UNVERIFIABLE, record.outcome)
        with Machine(change=Change(same=False, appeared=["Dark mode: On"])):
            record = cu.act_on("x", window=WINDOW, confirm=yes, expect="on")
        r.check("'On' as a word still confirms", record.outcome == cu.CONFIRMED,
                record.outcome)

    r.head("Typing and keys are recorded as what they were")
    with guard(r, "the record of a typing step"):
        task_ = engine.Task(intent="x", goal=Goal("text_on_screen", "x"))
        refused_typing = engine._as_record(cu.type_text("hi"), Step(verb="type", value="hi"),
                                           task_)
        r.check("refused typing did not act", refused_typing.action == ""
                and "did not act" in refused_typing.explain(), refused_typing.explain())
        with Machine():
            typed = engine._as_record(cu.type_text("hi", confirm=yes),
                                      Step(verb="type", value="hi"), task_)
        r.check("typing is described as typing", "typed" in typed.explain()
                and "clicked" not in typed.explain(), typed.explain())
    with guard(r, "a key press is not typed out as its name"):
        with Machine() as m:
            task = run_task(m, lambda t, w: Step(verb="press", value="Return", why="submit"),
                            goal_holds=lambda: False)
        r.check("nothing is typed", not m.cgevents, f"{len(m.cgevents)} events")
        r.check("and the task says it cannot press keys", task.state is TaskState.BLOCKED
                and "key" in task.summary().lower(), task.summary())

    r.head("A capture binds the click it was taken for")
    with guard(r, "the point must be inside its frame"):
        with Machine() as m:
            result = cu.click(T(950, 50, "x"), frame=Frame(), confirm=yes)
        refused("a point outside the capture is refused", result, cu.FRAME_MISMATCH)
        r.check("and not clicked", not m.clicks)
    with guard(r, "one frame, one click"):
        frame = Frame()
        with Machine(change=Change(same=False, appeared=["Saved"])) as m:
            first = cu.click(T(50, 50, "Continue"), frame=frame, confirm=yes, expect="Saved")
            second = cu.click(T(50, 50, "Continue"), frame=frame, confirm=yes, expect="Saved")
        r.check("the first click uses the frame", first.outcome == cu.CONFIRMED, first.outcome)
        refused("the second is refused", second, cu.FRAME_USED)
        r.check("one click in all", len(m.clicks) == 1, f"{len(m.clicks)} clicks")
    with guard(r, "a no withdraws a standing grant"):
        with Machine():
            cu._GRANT = cu.Grant(level=cu.ALLOW_FOR_TASK, task="t", granted_at=time.time())
            cu.click(T(50, 50, "Delete all"), frame=Frame(), task="t", confirm=no)
            after_destructive = cu.state()["level"]
            cu._GRANT = cu.Grant(level=cu.ALLOW_FOR_TASK, task="t", granted_at=time.time())
        with Machine(reading=Reading(Candidate(), ambiguous=True)):
            cu._GRANT = cu.Grant(level=cu.ALLOW_FOR_TASK, task="t", granted_at=time.time())
            cu.act_on("x", task="t", window=WINDOW, confirm=no)
            after_ask = cu.state()["level"]
        r.check("declining a destructive click revokes it", after_destructive == cu.DENY,
                after_destructive)
        r.check("declining an uncertain target revokes it", after_ask == cu.DENY, after_ask)

    r.head("A yes is for one click, and only that one")
    with guard(r, "an approval that was not used does not carry over"):
        # Found by the second review: the user says yes to "Dark mode", the
        # fresh look finds something else and the click is refused — and the
        # unspent yes then covered the next click on anything, in any task.
        with Machine(reading=Reading(Candidate("Dark mode")),
                     fresh=Reading(Candidate("Airplane mode"))) as m:
            cu.act_on("dark mode", window=WINDOW, confirm=yes)
            left = cu.state()["level"]
            asked = []
            m.reads = 0
            m.reading = m.fresh = Reading(Candidate("Airplane mode"))
            follow = cu.act_on("airplane mode", window=WINDOW,
                               confirm=lambda q: asked.append(q) or False)
        r.check("the refused click leaves no standing yes", left == cu.DENY, left)
        r.check("the next control is asked about again", len(asked) == 1
                and follow.refusal == cu.DECLINED and not m.clicks,
                f"asked {len(asked)}, {follow.refusal}, {len(m.clicks)} clicks")
    with guard(r, "a control already tried is refused before anyone is asked"):
        asked = []
        with Machine() as m:
            record = cu.act_on("x", window=WINDOW,
                               confirm=lambda q: asked.append(q) or True,
                               avoid=[(Candidate().describe(), 120.0, 80.0)])
        refused("refused without a question", record, cu.ALREADY_TRIED)
        r.check("nobody was asked", not asked, str(asked))

    with guard(r, "the codes are a closed set"):
        r.check("every code produced is a declared refusal", exercised <= set(cu.REFUSALS),
                str(exercised - set(cu.REFUSALS)))
        r.check("and every declared refusal was produced by a real gate",
                set(cu.REFUSALS) <= exercised, str(set(cu.REFUSALS) - exercised))

    # ── Found by the fourth review ───────────────────────────────────────────
    r.head("A yes does not outlive what it was for")
    with guard(r, "click(): a refusal after the yes drops it"):
        with Machine(captures=[Frame(ok=False)]) as m:
            result = cu.click(T(120.0, 80.0, "Dark mode"), frame=Frame(stale=True),
                              confirm=yes)
            left = cu.state()["level"]
        r.check("refused on the unreadable fresh look", result.refusal == cu.FRAME_INVALID,
                result.refusal)
        r.check("and the unspent yes is gone", left == cu.DENY, left)
    with guard(r, "type_text(): a refusal after the yes drops it"):
        with Machine() as m:
            import Quartz

            def broken(*a, **k):
                raise RuntimeError("no accessibility")
            m.patch(Quartz, "CGEventPost", broken)
            result = cu.type_text("hello", confirm=yes)
            left = cu.state()["level"]
        r.check("refused as input unavailable", result.refusal == cu.INPUT_UNAVAILABLE,
                result.refusal)
        r.check("and the unspent yes is gone", left == cu.DENY, left)
    with guard(r, "act_on(): an exception after the yes drops it"):
        with Machine() as m:
            def fails(*a, **k):
                m.reads += 1
                if m.reads > 1:
                    raise RuntimeError("perception fell over")
                return m.reading
            m.patch(perception, "read", fails)
            raised = False
            try:
                cu.act_on("dark mode", task="task A", window=WINDOW, confirm=yes)
            except RuntimeError:
                raised = True
            left = cu.state()["level"]
            m.reads = 0
            m.patch(perception, "read", m.read)
            follow = cu.act_on("airplane mode", task="task B", window=WINDOW, confirm=None)
        r.check("the failure is not hidden", raised)
        r.check("the yes it was given does not survive it", left == cu.DENY, left)
        r.check("and cannot press anything for a caller nobody can ask",
                follow.refusal == cu.NO_CONFIRMATION and not m.clicks,
                f"{follow.refusal}, {len(m.clicks)} clicks")
    with guard(r, "a one-time yes belongs to its task"):
        grant = cu.Grant(level=cu.ALLOW_ONCE, task="task A", granted_at=time.time())
        r.check("it covers that task", grant.covers("task A"))
        r.check("and no other", not grant.covers("task B"))

    r.head("An exception after the click is still an outcome")
    with guard(r, "the after-look cannot be compared"):
        with Machine() as m:
            def broken_compare(*a, **k):
                raise RuntimeError("OCR fell over")
            m.patch(screen, "compare", broken_compare)
            record = cu.act_on("dark mode", window=WINDOW, confirm=yes, expect="Dark")
        r.check("act_on still settles one outcome", record.outcome == cu.UNVERIFIABLE,
                f"{record.outcome}/{record.refusal}")
        r.check("having clicked exactly once", len(m.clicks) == 1, f"{len(m.clicks)}")
    with guard(r, "a task that hits an exception ends"):
        def exploding(task, window):
            raise RuntimeError("planner fell over")
        with Machine() as m:
            task = run_task(m, exploding, goal_holds=lambda: False)
            saved = (Goal.verified, engine._active)
            Goal.verified = lambda self, window=None: False
            engine._active = task
            try:
                follow = engine.run("again", Goal("text_on_screen", "Dark"), window=WINDOW,
                                    confirm=yes, planner=lambda t, w: Step(verb="done"))
            finally:
                Goal.verified, engine._active = saved
        r.check("it ends as failed, not still running", task.state is TaskState.FAILED,
                task.state.value)
        r.check("and the next task is not told one is already running",
                "already in the middle" not in (follow.spoken or ""), follow.spoken)
    with guard(r, "a task that ends takes its standing grant with it"):
        with Machine(change=Change(same=False, appeared=["Dark"])) as m:
            run_task(m, lambda t, w: Step(verb="click", target="Dark mode", expect="Dark",
                                          why="toggle"), goal_holds=lambda: bool(m.clicks))
            left = cu.state()["level"]
        r.check("no grant is left when the task is over", left == cu.DENY, left)

    r.head("Seeing the words is not seeing it happen")
    with guard(r, "the words, saying it failed"):
        for appeared, expect in (("Message not sent", "sent"),
                                 ("Dark mode could not be enabled", "Dark mode"),
                                 ("Saving failed", "Saving"),
                                 ("Unable to connect to Wi-Fi", "Wi-Fi")):
            change = screen.ScreenChange(same=False, appeared=[appeared])
            r.check(f"“{appeared}” does not confirm “{expect}”",
                    cu._judge(change, expect) != cu.CONFIRMED, cu._judge(change, expect))
    with guard(r, "the words, saying it failed, as macOS writes them"):
        # macOS writes its apostrophes curly (U+2019); the first failure-word
        # list only knew the straight one, so these all confirmed.
        for appeared, expect in (("The document “Report” couldn’t be saved.", "saved"),
                                 ("Message wasn’t sent", "sent"),
                                 ("Dark mode can’t be turned on", "on")):
            change = screen.ScreenChange(same=False, appeared=[appeared])
            r.check(f"“{appeared}” does not confirm “{expect}”",
                    cu._judge(change, expect) != cu.CONFIRMED, cu._judge(change, expect))
    with guard(r, "the words, saying it has not happened yet"):
        for appeared, expect in (("Sent — tap to retry", "sent"),
                                 ("Will be sent when online", "sent"),
                                 ("Saved changes pending", "saved")):
            change = screen.ScreenChange(same=False, appeared=[appeared])
            r.check(f"“{appeared}” does not confirm “{expect}”",
                    cu._judge(change, expect) != cu.CONFIRMED, cu._judge(change, expect))
    with guard(r, "a line that only re-rendered"):
        change = screen.ScreenChange(same=False, appeared=["Last saved 3 minutes ago"],
                                     vanished=["Last saved 2 minutes ago"])
        r.check("a timestamp ticking over is not the save",
                cu._judge(change, "saved") != cu.CONFIRMED, cu._judge(change, "saved"))
    with guard(r, "the words, saying it worked"):
        change = screen.ScreenChange(same=False, appeared=["Message sent"])
        r.check("“Message sent” confirms “sent”", cu._judge(change, "sent") == cu.CONFIRMED)
        change = screen.ScreenChange(same=False, appeared=["Last saved just now"],
                                     vanished=["Unsaved changes"])
        r.check("and a new line saying it saved confirms it",
                cu._judge(change, "saved") == cu.CONFIRMED)
    with guard(r, "the task's own goal"):
        saved_read = engine._read_window_text
        try:
            engine._read_window_text = lambda window: "Notifications\nDone editing"
            on = Goal("text_on_screen", "on").verified(WINDOW)
            engine._read_window_text = lambda window: "Dark mode could not be enabled"
            failed_line = Goal("text_on_screen", "Dark mode").verified(WINDOW)
            engine._read_window_text = lambda window: "Appearance\nDark mode"
            real = Goal("text_on_screen", "Dark mode").verified(WINDOW)
        finally:
            engine._read_window_text = saved_read
        r.check("“on” is not found inside “Notifications”", on is False)
        r.check("a line saying it failed does not complete the goal", failed_line is False)
        r.check("the words themselves still do", real is True)

    r.head("The same control, however it is found again")
    with guard(r, "found again by another source, or a little off"):
        avoid = [(Candidate().describe(), 120.0, 80.0)]

        class Seen(Candidate):
            def __init__(self, label="Dark mode", role="text", centre=(120.0, 80.0)):
                super().__init__(label)
                self.role, self.centre = role, centre

            def describe(self):
                return f"{self.role} “{self.label}”"
        with Machine(reading=Reading(Seen())) as m:
            by_ocr = cu.act_on("dark mode", window=WINDOW, confirm=yes, avoid=avoid)
        r.check("the same toggle read off the screen as text is still refused",
                by_ocr.refusal == cu.ALREADY_TRIED and not m.clicks, by_ocr.refusal)
        with Machine(reading=Reading(Seen(role="switch", centre=(150.0, 80.0)))) as m:
            nudged = cu.act_on("dark mode", window=WINDOW, confirm=yes, avoid=avoid)
        r.check("thirty points off is still the same control",
                nudged.refusal == cu.ALREADY_TRIED and not m.clicks, nudged.refusal)
        with Machine(reading=Reading(Seen(label="Wi-Fi", role="switch"))) as m:
            other = cu.act_on("wi-fi", window=WINDOW, confirm=yes, avoid=avoid)
        r.check("a different control in the same place is not",
                len(m.clicks) == 1, f"{other.outcome}/{other.refusal}")
    with guard(r, "the same name, typed differently"):
        # Apple writes Wi‑Fi with a non-breaking hyphen (U+2011); OCR reads a
        # plain one, and often keeps a trailing colon.
        tried = [("switch “Wi‑Fi”", 120.0, 80.0)]
        r.check("Wi‑Fi (U+2011) and Wi-Fi are the same control",
                cu._same_control("text “Wi-Fi”", 150.0, 80.0, tried))
        r.check("and so is “Wi-Fi:” read off the screen",
                cu._same_control("text “Wi-Fi:”", 150.0, 80.0, tried))
        r.check("but Bluetooth is not",
                not cu._same_control("text “Bluetooth”", 150.0, 80.0, tried))
    with guard(r, "it moves onto one already tried between the two looks"):
        far, near = Candidate(), Candidate()
        far.centre = (600.0, 500.0)
        with Machine(reading=Reading(far), fresh=Reading(near)) as m:
            moved = cu.act_on("dark mode", window=WINDOW, confirm=yes,
                              avoid=[(near.describe(), 120.0, 80.0)])
        r.check("refused after the fresh look", moved.refusal == cu.ALREADY_TRIED
                and not m.clicks, moved.refusal)

    r.head("A refusal after a yes is not a question")
    with guard(r, "an uncertain target, approved, then changed"):
        asked = []
        with Machine(reading=Reading(Candidate(confidence=0.5)),
                     fresh=Reading(Candidate("Delete account"))) as m:
            record = cu.act_on("dark mode", window=WINDOW,
                               confirm=lambda q: asked.append(q) or True)
        r.check("refused as changed", record.refusal == cu.TARGET_CHANGED, record.refusal)
        named = engine.classify(record, WINDOW, WINDOW, Step(verb="click", target="Dark mode"))
        r.check("and read as the screen having changed, not as ambiguity",
                named != engine.AMBIGUITY, named)
        r.check("the user was asked once, not twice", len(asked) == 1, str(asked))

    r.head("The safety floor")
    with guard(r, "nothing real happened"):
        r.check("computer use was off before this suite", switched_off_before)
        r.check("and is off after it", cu.ENABLED is False)
        r.check("no standing grant is left behind", cu.state()["level"] == cu.DENY)

    return r.report("action outcomes")


if __name__ == "__main__":
    try:
        sys.exit(run())
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
