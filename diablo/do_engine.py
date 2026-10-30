"""
D.I.A.B.L.O. — taking responsibility for an outcome.

The difference this module exists to make
-----------------------------------------
`computer_use.act_on` performs one action and checks it. That is DO 1.0, and
it is a good foundation, but it cannot be handed a goal. Ask it to create a
folder and it has nowhere to put the fact that the folder does not exist yet.

This holds the goal. It observes, decides on ONE next step, performs it,
verifies it, and then looks again before deciding anything else — because a
plan made three screens ago is a description of a screen that is gone.

    observe -> plan one step -> locate -> decide -> permission
            -> act -> verify -> classify -> update -> observe ...

Never:

    plan eight clicks -> perform eight clicks -> report success

Short horizon on purpose
------------------------
The planner is asked for the next meaningful step, never for a script. A
twenty-step plan is stale by step three, and the failure it produces is the
worst kind: every step "succeeds" and the task ends somewhere nobody asked
for. Re-planning after each verified change costs one model call and buys the
ability to notice that a dialog appeared.

Goal verification is separate from action verification
------------------------------------------------------
A click can succeed and the task still not be done. "Done" is only said when
the goal itself is observed to hold, which is why Goal is a thing that can be
checked against a screen rather than a sentence in a plan.
"""

import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import config  # noqa: E402


# ─── States ──────────────────────────────────────────────────────────────────

class TaskState(Enum):
    NEW = "new"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    OBSERVING = "observing"
    LOCATING = "locating"
    AWAITING_PERMISSION = "awaiting_permission"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


OVER = {TaskState.COMPLETED, TaskState.BLOCKED, TaskState.FAILED,
        TaskState.CANCELLED}


class ActionState(Enum):
    """One step's own lifecycle. Deliberately not the task's."""

    PLANNED = "planned"
    TARGETING = "targeting"
    AUTHORIZED = "authorized"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    UNVERIFIED = "unverified"      # sent, and nothing proves what it did
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


# What went wrong, so recovery can be chosen rather than guessed.
TARGET_MOVED = "target_moved"
TARGET_GONE = "target_disappeared"
WINDOW_CHANGED = "window_changed"
APP_CHANGED = "app_changed"
MODAL_APPEARED = "modal_appeared"
NO_EFFECT = "action_registered_but_no_effect"
UNEXPECTED_STATE = "unexpected_state"
AMBIGUITY = "ambiguity"
PERMISSION_FAILURE = "permission_failure"
TIMED_OUT = "timeout"
UNKNOWN = "unknown"
# The next step would press a control already pressed without effect.
REPEATED = "would_repeat_an_unproven_action"

def _refusal_class(code: str) -> Optional[str]:
    """
    computer_use's refusal code, read as a fact, or None for codes with no
    special handling.

    Matching the spoken sentence was the only way to tell these apart before,
    and anything it missed fell through to UNEXPECTED_STATE — which re-plans,
    so a "no" from the user, an absent confirmation or a password manager was
    answered by asking again. Built from computer_use's own constants so the
    codes have one definition.
    """
    from skills import computer_use as cu
    return {
        cu.SWITCHED_OFF: PERMISSION_FAILURE,
        cu.NO_CONFIRMATION: PERMISSION_FAILURE,
        cu.DECLINED: PERMISSION_FAILURE,
        cu.SENSITIVE_WINDOW: PERMISSION_FAILURE,
        cu.INPUT_UNAVAILABLE: PERMISSION_FAILURE,
        cu.TARGET_UNCERTAIN: AMBIGUITY,
        cu.TARGET_GONE: TARGET_GONE,
        cu.ALREADY_TRIED: REPEATED,
    }.get(code)

# Every retry re-observes. A second attempt from the same stale picture is
# just the first mistake again.
MAX_RECOVERIES = config.env_int("DIABLO_TASK_RECOVERIES", 3, low=0, high=8)
MAX_STEPS = config.env_int("DIABLO_TASK_STEPS", 12, low=1, high=40)
# Consecutive waits before giving up. Waiting does not change the screen, so
# a planner stuck on "wait" makes no progress however long it is given.
MAX_WAITS = config.env_int("DIABLO_TASK_WAITS", 3, low=1, high=20)
RUNTIME_LIMIT = config.env_float("DIABLO_TASK_RUNTIME", 600.0, low=10.0, high=3600.0)

# A task nobody has touched for this long is not the task the next sentence
# is about.
CONTEXT_EXPIRY = config.env_float("DIABLO_TASK_CONTEXT_EXPIRY", 300.0,
                                  low=10.0, high=7200.0)


# ─── What success means ──────────────────────────────────────────────────────

def _read_window_text(window: Dict[str, Any]) -> str:
    """
    Everything readable in one specific window, by its id.

    With a fallback to capturing the screen rectangle the window occupies,
    and the fallback is not belt and braces — it is load-bearing. Measured:
    `screencapture -l <id>` on an application window returned a perfectly
    valid image containing the title bar and a blank body, while capturing the
    same rectangle off the screen showed the buttons and the text. The
    blank-frame gate does not catch it either, because a dark title bar above
    a white body has plenty of contrast.

    A goal checked against a capture like that is always false, and the task
    it belongs to never finishes.
    """
    from skills import screen as _screen
    from skills import screen_context as _sc

    frame = _screen.capture_this_window(window)
    text = ""
    try:
        if frame.ok:
            text = _sc.ocr_image(frame.path, window) or ""
    except Exception:
        text = ""
    finally:
        frame.release()

    # The title bar alone is not a reading of a window.
    if len((text or "").strip()) > len((window.get("title") or "")) + 24:
        return text

    from skills import display as _display
    path = _display.capture_region(window.get("x", 0), window.get("y", 0),
                                   window.get("width", 0), window.get("height", 0))
    if not path:
        return text
    try:
        return _sc.ocr_image(path, window) or text
    except Exception:
        return text
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@dataclass
class Goal:
    """
    What has to be true for the task to be over.

    Checked against the screen, not against the plan. A step that reports
    success moves the task along; only this ends it.
    """

    kind: str = "text_on_screen"      # text_on_screen | text_absent
                                      # | window_exists | file_exists
    value: str = ""
    where: str = ""                   # window or folder, depending on kind
    # More text that must be on screen too. A two-part request with a goal
    # naming only its last part was reported done with the first part never
    # carried out: the engine checked exactly what it was given.
    also: Tuple[str, ...] = ()

    def describe(self) -> str:
        if self.kind == "file_exists":
            return f"{self.value} exists in {self.where or 'the folder'}"
        if self.kind == "window_exists":
            return f"a window for {self.value} is open"
        if self.kind == "text_absent":
            return f"“{self.value}” is gone from the screen"
        if self.also:
            texts = " and ".join(f"“{t}”" for t in (self.value, *self.also))
            return f"{texts} are on the screen"
        return f"“{self.value}” is on the screen"

    def verified(self, window: Optional[Dict[str, Any]] = None) -> bool:
        """Look, and say whether it holds. Never infer this from a click."""
        from skills import screen_context as _sc
        try:
            if self.kind == "file_exists":
                folder = os.path.expanduser(self.where or "~/Desktop")
                return os.path.exists(os.path.join(folder, self.value))
            if self.kind == "window_exists":
                return _sc.find_window(self.value) is not None
            if self.kind in ("text_on_screen", "text_absent"):
                target = window or _sc.frontmost_window()
                if not target:
                    return self.kind == "text_absent"
                # Read THIS window, not a window whose app happens to share a
                # name. look() resolves by name, and two windows owned by the
                # same interpreter are both "Python" — the goal was being
                # checked against the wrong one.
                text = _read_window_text(target)
                # Whole words on one line, and not a line saying it failed:
                # a substring check found "on" inside "Notifications" and
                # completed the goal on "Dark mode could not be enabled".
                from skills import computer_use as _cu
                lines = text.splitlines() or [text]
                if self.kind == "text_on_screen":
                    return all(any(_cu._evidence(line, want) for line in lines)
                               for want in (self.value, *self.also))
                return not any(_cu._says(line, self.value) for line in lines)
        except Exception:
            return False
        return False


@dataclass
class Step:
    """One thing to do next. Never a script."""

    verb: str = "click"               # click | type | press | wait
    target: str = ""                  # said the way a person would say it
    value: str = ""                   # what to type, or which key
    expect: str = ""                  # what should appear if it worked
    why: str = ""
    state: ActionState = ActionState.PLANNED

    def describe(self) -> str:
        if self.verb == "type":
            return f"type “{self.value[:40]}”"
        if self.verb == "press":
            return f"press {self.value}"
        if self.verb == "wait":
            return "wait for the screen to settle"
        return f"click {self.target}"


@dataclass
class Moment:
    """One line of the task's timeline."""

    at: float
    kind: str
    detail: str = ""

    def line(self, start: float) -> str:
        return f"  +{self.at - start:6.2f}s  {self.kind:20} {self.detail}"


@dataclass
class Task:
    """A goal, the state it is in, and everything that has happened to it."""

    intent: str = ""
    goal: Goal = field(default_factory=Goal)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    app: str = ""
    state: TaskState = TaskState.NEW
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    deadline: float = 0.0

    steps: List[Step] = field(default_factory=list)
    actions: List[Any] = field(default_factory=list)      # ActionRecords
    timeline: List[Moment] = field(default_factory=list)
    recoveries: int = 0
    result: str = ""
    spoken: str = ""
    question: str = ""

    # Bumped whenever the task is cancelled or superseded. Anything holding
    # an older number is working from a world that has moved on.
    generation: int = 0

    def note(self, kind: str, detail: str = "") -> None:
        self.timeline.append(Moment(time.time(), kind, detail))
        self.updated_at = time.time()

    def to(self, state: TaskState, detail: str = "") -> None:
        if self.state is state:
            return
        self.state = state
        self.note(state.value, detail)

    @property
    def over(self) -> bool:
        return self.state in OVER

    @property
    def runtime(self) -> float:
        return time.time() - self.created_at

    def expired(self) -> bool:
        return self.deadline > 0 and time.time() > self.deadline

    def stale_context(self) -> bool:
        return (time.time() - self.updated_at) > CONTEXT_EXPIRY

    def trace(self) -> str:
        return "\n".join(m.line(self.created_at) for m in self.timeline)

    def summary(self) -> str:
        """What was asked, what happened, and what it means. For a person."""
        if self.state is TaskState.COMPLETED:
            return self.spoken or f"Done — {self.goal.describe()}."
        if self.state is TaskState.CANCELLED:
            return "Stopped."
        if self.state is TaskState.WAITING_FOR_USER:
            return self.question or "I need you to decide something."
        if self.state is TaskState.BLOCKED:
            return self.spoken or "I have stopped because I could not do that safely."
        return self.spoken or "I could not finish that."


# ─── Planning one step at a time ─────────────────────────────────────────────

# `target` and `value` are required, and that is not tidiness.
#
# They used to be optional, and constrained decoding took the invitation: on a
# 59-goal benchmark the planner returned `click` with an empty target in 21 of
# 59 decisions — 36% of everything it said, and a click that names nothing
# cannot be carried out by anything downstream.
#
# Making them required, and saying so in the prompt, was measured as one
# change against the same corpus and the same model:
#
#     advancement rate      44.1% -> 74.6%
#     first step            48.9% -> 82.2%
#     later steps           28.6% -> 50.0%
#     empty-target decisions 33.9% -> 0.0%
#
# Repeated three times over the whole corpus: 46.9% against 74.0%. The 4B was
# not the bottleneck. Its schema was.
#
# `why` comes first. A constrained reply is written in the schema's order, so
# with `verb` first the model chose the action before it had said anything
# about the screen. Measured on qwen3.5:4b, 2026-10-05: on "Open Safari" with
# Safari already in front it answered verb "wait" with the reason "the goal
# is already satisfied. No further action is needed" — the reasoning was right
# and the verb, committed before it, was not. Nine decisions in 118 went that
# way.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "why": {"type": "string"},
        "verb": {"type": "string", "enum": ["click", "type", "press", "wait", "done"]},
        "target": {"type": "string"},
        "value": {"type": "string"},
        "expect": {"type": "string"},
    },
    "required": ["why", "verb", "target", "value"],
}

PLAN_SYSTEM = """You decide the single next action in someone else's application.

You are given a goal and a description of what is on screen right now. Reply
with ONE action, never a sequence — you will be asked again after it has been
carried out and the screen has been looked at afresh.

- First check whether what is on screen already satisfies the goal. If it
  does, the action is "done", even if nothing has been done yet.
- `target` is a control's name copied from the Controls list, as written there:
  Search, Save, Reload. Not a description of it, and never a coordinate. A
  click with an empty target cannot be carried out by anything.
- `type` sends text to whichever control already has keyboard focus. To type
  into a field that is not focused, click it first; type on the next turn.
  `type` and `press` put the text or the key in `value`.
- Use "wait" only when the screen says it is loading or busy.
- If the screen shows something you did not expect, deal with that first. An
  unexpected dialog is usually the next thing to handle, not an obstacle to
  push past.
- `expect` is the text you would expect to appear if the action worked, or an
  empty string if you do not know.

Fill in every field, using an empty string where one does not apply."""


# The notes _control_line adds after a control's name. The planner copies
# whole lines, so `CheckBox "Priority" (off)` came back as the target and
# matched nothing; then it grew "(failed) (on) (expected)". Only these exact
# notes are removed, so a control really called "Export (PDF)" keeps its name.
_NOTES = re.compile(r"(\s*\((?:on|off|empty|contains “[^”]*”|confirmed|failed|"
                    r"sent, not confirmed)\))+\s*$")


def _bare_target(target: str) -> str:
    return _NOTES.sub("", target).strip()


def _control_line(element: Any) -> str:
    """
    A control as the planner sees it, with its state when it has one.

    Without it a filled field and an empty one read the same, so the planner
    typed "3" into a field that already said 3, and "33" was saved.
    """
    from skills import ax
    line = element.describe()
    if element.role in ax.EDITABLE_ROLES and element.value and element.value != element.label:
        return f"{line} (contains “{element.value[:30]}”)"
    if element.role in ax.EDITABLE_ROLES:
        return f"{line} (empty)" if not element.value else line
    if element.role in ax.TOGGLE_ROLES and element.state is not None:
        return f"{line} ({'on' if element.state else 'off'})"
    return line


# How a step ended, as the planner is told it.
_ENDED = {ActionState.SUCCEEDED: "confirmed", ActionState.UNVERIFIED: "sent, not confirmed",
          ActionState.FAILED: "failed"}


def _screen_summary(window: Optional[Dict[str, Any]], limit: int = 900) -> str:
    """A compact account of the current screen for the planner."""
    from skills import screen_context as _sc
    if not window:
        return "No window is visible."
    parts = [f"Application: {window.get('app','?')}",
             f"Window: {window.get('title','')[:80]}"]
    try:
        # THIS window, by id, not a window whose app happens to share a name.
        # look() resolves by app name and two windows owned by the same
        # interpreter are both "Python", so the planner was being shown a
        # different window's text — or, measured on the controls fixture,
        # "Error: I could not find a window matching 'Python'". _read_window_text
        # was written for exactly this when the goal check hit it; the planner's
        # own account never adopted it, and the benchmark stubbed this function
        # out entirely, so nothing caught it.
        text = _read_window_text(window) or ""
        if text.strip():
            parts.append("On screen:\n" + text[:limit])
    except Exception:
        pass
    try:
        from skills import ax
        controls = [_control_line(e) for e in ax.elements_for_window(window)[:24]]
        if controls:
            parts.append("Controls: " + "; ".join(controls))
    except Exception:
        pass
    return "\n".join(parts)


# How many alternatives to show the planner when arbitration could not choose.
BRIEF_ALTERNATIVES = 3


def perception_brief(intent: str, window: Optional[Dict[str, Any]]) -> str:
    """
    What arbitration concluded about the thing the planner is trying to reach.

    `_screen_summary` hands the planner raw material: OCR text and an
    unranked list of every actionable control in the window. That is what the
    planner benchmark stubbed, and it is the reason the 74.6% figure describes
    a planner working from a perfect reading rather than a real one. It does
    not consult `perception.read`, so nothing the last three builds produced —
    fusion, the separation rule, the five verdicts, source provenance — ever
    reached the planner.

    This is the adapter. It asks the real production pipeline the same
    question the planner is about to answer, and states the arbitrated result
    in the terms the planner already understands: a control named the way a
    person would say it. Built from `Reading`, `Candidate` and `Verdict`, not
    from prose about them.

    Returns an empty string when there is nothing useful to add, so the prompt
    is unchanged in that case rather than padded.
    """
    if not intent or not intent.strip() or not window:
        return ""
    try:
        from skills import perception
    except Exception:
        return ""
    try:
        reading = perception.read(intent, window)
    except Exception:
        return ""

    verdict = getattr(reading, "verdict", "")
    best = getattr(reading, "best", None)

    if verdict == perception.Verdict.INVALID_OBSERVATION:
        # The distinction Build 1.5 preserved, carried to the one consumer
        # whose next move depends on it. "I could not see" must not reach the
        # planner as "there is nothing there", because the right response to
        # the first is to wait and look again.
        why = ", ".join(getattr(reading, "unreadable", []) or []) or "unknown"
        return (f"Perception: THE SCREEN COULD NOT BE READ ({why}). "
                f"Do not assume the control is absent. Waiting and looking "
                f"again is usually right.")

    if best is None:
        return (f"Perception: looked for {intent!r} and found no control "
                f"matching it. The screen was readable.")

    sources = "+".join(sorted(set(getattr(best, "sources", []) or []))) or "unknown"
    line = (f"Perception: {verdict.upper()} — {best.label or 'an unlabelled control'!r} "
            f"({best.role or 'control'}), confidence {best.confidence:.2f} "
            f"from {sources}.")
    if getattr(best, "enabled", None) is False:
        line += " It reports itself as DISABLED; clicking it will do nothing."

    if verdict == perception.Verdict.AMBIGUOUS:
        others = [c.label or "an unlabelled control"
                  for c in getattr(reading, "candidates", [])[1:BRIEF_ALTERNATIVES + 1]]
        line += (f" Arbitration could not separate it from "
                 f"{', '.join(repr(o) for o in others)}. "
                 f"Naming one of these is a guess.")
    elif verdict == perception.Verdict.LOW_CONFIDENCE:
        line += " The evidence is weak; treat the name as provisional."
    return line


def model_planner(task: "Task", window: Optional[Dict[str, Any]]) -> Step:
    """
    Ask the local model for the next step. The default planner.

    Injectable so tests do not measure a sampled model: a suite whose result
    depends on what a 4B felt like saying is measuring the 4B.
    """
    from skills import local_brain
    done = "; ".join(f"{s.describe()} ({_ENDED[s.state]})" if s.state in _ENDED
                     else s.describe() for s in task.steps[-4:]) or "nothing yet"
    # NOT wired to perception_brief. Measured on the target fixture with a goal
    # a click can actually satisfy ("FINISHED" on screen, reached by pressing
    # Mark Done), six planner calls per arm:
    #
    #     without the brief   6/6  click "Mark Done"     correct
    #     with the brief      5/6  wait                  wrong
    #
    # Handing the planner the arbitrated result as an extra prompt line made it
    # hesitate on the case it should act on. The adapter is kept and tested
    # because it demonstrably carries the structured result; wiring it in as
    # additional prose is what failed, and tuning that wording until the 4B
    # likes it is planner work, not an integration.
    prompt = (f"Goal: {task.goal.describe()}\n"
              f"The user asked: {task.intent!r}\n"
              f"Already done: {done}\n\n"
              f"{_screen_summary(window)}\n\nWhat is the single next action?")
    try:
        reply = local_brain.chat_json(
            [{"role": "system", "content": PLAN_SYSTEM},
             {"role": "user", "content": prompt}],
            schema=PLAN_SCHEMA, temperature=0.1, num_predict=200)
    except Exception:
        return Step(verb="wait", why="the planner did not answer")
    return Step(verb=str(reply.get("verb", "wait")),
                target=_bare_target(str(reply.get("target", "")))[:80],
                value=str(reply.get("value", ""))[:200],
                expect=str(reply.get("expect", ""))[:80],
                why=str(reply.get("why", ""))[:160])


# ─── Classifying what actually happened ──────────────────────────────────────

def classify(record, before_window, after_window, step: Step) -> str:
    """
    Why the step did not do what it was supposed to.

    Naming the failure is what makes recovery a decision rather than a retry.
    "It did not work, try again" is how one wrong coordinate becomes four.
    """
    from skills import screen_context as _sc

    if record is None:
        return UNKNOWN
    named = _refusal_class(getattr(record, "refusal", ""))
    if named:
        return named
    # "ask" is a question only while nothing was sent. An approved uncertain
    # click that then changed nothing used to be put to the user as a
    # question with a statement for its text.
    if record.verdict == "ask" and getattr(record, "outcome", "") in ("", "refused"):
        return AMBIGUITY
    if record.verdict == "refuse":
        if "not allowed" in (record.spoken or "").lower():
            return PERMISSION_FAILURE
        if "not now" in (record.spoken or "").lower() or "gone" in (record.spoken or "").lower():
            return TARGET_GONE
        return UNEXPECTED_STATE

    # Judged from the two windows it was handed, never by reaching for the
    # live screen. A classifier that fetches its own evidence cannot be tested
    # and answers a different question each time it is called — asked about a
    # click that changed nothing, it reported APP_CHANGED because something
    # unrelated happened to be frontmost.
    if after_window is None:
        return WINDOW_CHANGED
    if before_window and after_window.get("app") != before_window.get("app"):
        return APP_CHANGED
    if before_window and _sc.has_moved(before_window) is not None:
        return WINDOW_CHANGED

    # Unverifiable reaches here only when something specific was expected and
    # not seen: judged from what was observed, as a failed check always was.
    if record.verified is False or getattr(record, "outcome", "") == "unverifiable":
        observed = (record.observed or "").lower()
        if "nothing" in observed or not observed:
            return NO_EFFECT
        # Something changed, just not what was asked for. Very often that is
        # a dialog, and a dialog is the next thing to deal with rather than a
        # failure to undo.
        if step.expect and step.expect.lower() not in observed:
            return MODAL_APPEARED if "new on screen" in observed else UNEXPECTED_STATE
        return UNEXPECTED_STATE
    return UNKNOWN


# ─── One task at a time ──────────────────────────────────────────────────────

_active: Optional[Task] = None


def active() -> Optional[Task]:
    """The task currently allowed to touch the desktop, if any."""
    if _active is not None and not _active.over and not _active.stale_context():
        return _active
    return None


def cancel(reason: str = "the user asked me to stop") -> bool:
    """
    Stop now. Everything already in flight becomes stale.

    Bumping the generation is the part that matters: a verification that
    returns after this must not be able to mark a cancelled task complete.
    """
    task = _active
    if task is None or task.over:
        return False
    task.generation += 1
    task.to(TaskState.CANCELLED, reason)
    task.spoken = "Stopped."
    from skills import computer_use
    computer_use.revoke()           # a stopped task leaves nothing clickable
    return True


def run(intent: str, goal: Goal, window: Optional[Dict[str, Any]] = None,
        planner: Optional[Callable[["Task", Optional[dict]], Step]] = None,
        confirm: Optional[Callable[[str], bool]] = None,
        say: Optional[Callable[[str], None]] = None,
        runtime: Optional[float] = None) -> Task:
    """
    Carry out a task: observe, decide one step, do it, check, look again.

    Returns the Task however it ends, including the ways it ends without
    finishing. A task that stopped to ask a question is not a failure and
    does not pretend to be a success.
    """
    global _active
    from skills import computer_use
    from skills import screen_context as _sc

    running = active()
    if running is not None:
        waiting = Task(intent=intent, goal=goal)
        waiting.to(TaskState.BLOCKED, f"task {running.id} is already running")
        waiting.spoken = ("I am already in the middle of something. "
                          "Tell me to stop if you would rather I did this instead.")
        return waiting

    task = Task(intent=intent, goal=goal)
    task.deadline = time.time() + (runtime or RUNTIME_LIMIT)
    _active = task
    mine = task.generation
    plan = planner or model_planner
    waits = 0
    # Controls (description, x, y) pressed in this task without being shown to
    # work. computer_use refuses to press one of them again.
    tried: List[Tuple[str, float, float]] = []
    speak = say or (lambda _line: None)

    task.to(TaskState.UNDERSTANDING, intent)
    task.note("goal", goal.describe())

    try:
        while True:
            # Cancelled or superseded while we were away.
            if task.generation != mine or task.over:
                return task
            if task.expired():
                task.to(TaskState.FAILED, f"ran for {task.runtime:.0f}s")
                task.spoken = ("I ran out of time on that one and stopped "
                               "rather than carrying on unsupervised.")
                return task
            if len(task.steps) >= MAX_STEPS:
                task.to(TaskState.BLOCKED, f"{MAX_STEPS} steps")
                task.spoken = ("That is taking more steps than I expected. "
                               "I have stopped so you can look.")
                return task

            # ── Observe. Never trust the last screen. ──
            task.to(TaskState.OBSERVING)
            here = (_sc.window_by_id(window["id"]) if window and window.get("id")
                    else None) or _sc.frontmost_window()
            if here is None:
                task.to(TaskState.BLOCKED, "no window")
                task.spoken = "I cannot see a window to work in."
                return task
            if task.app and here.get("app") != task.app:
                task.note(APP_CHANGED, f"{task.app} -> {here.get('app')}")
            task.app = here.get("app", "")
            window = here

            # ── Is it already done? The goal decides, not the steps. ──
            task.to(TaskState.VERIFYING, "checking the goal")
            if goal.verified(here):
                task.to(TaskState.COMPLETED, goal.describe())
                task.spoken = f"Done — {goal.describe()}."
                return task

            # ── One step. ──
            task.to(TaskState.PLANNING)
            step = plan(task, here)
            step.state = ActionState.PLANNED
            task.note("next", f"{step.describe()} — {step.why}"[:110])

            if step.verb == "done":
                # The planner thinks so; the goal does not. Believe the goal.
                task.to(TaskState.BLOCKED, "planner said done, goal says otherwise")
                task.spoken = ("I think I have done everything I can, but I "
                               "cannot confirm it worked.")
                return task
            if step.verb == "wait":
                # Bounded, because waiting is not free. A planner that keeps
                # saying "wait" used to run until the runtime limit — ninety
                # seconds of doing nothing and then reporting a timeout, when
                # the honest answer was available after the third wait.
                waits += 1
                task.note("wait", f"{step.why[:70]} ({waits})")
                if waits > MAX_WAITS:
                    task.to(TaskState.BLOCKED, f"{waits} waits with no progress")
                    task.spoken = ("The screen stopped changing and I ran out "
                                   "of things to try.")
                    return task
                time.sleep(0.8)
                continue
            waits = 0

            if step.verb in ("type", "press") and not step.value:
                task.to(TaskState.BLOCKED, "nothing to type")
                task.spoken = "I am not sure what to enter there."
                return task

            if step.verb == "press":
                # There is no key-press action. A press used to go to
                # type_text, which typed the key's NAME — "Return" as six
                # letters, once per step until the step ceiling.
                task.to(TaskState.BLOCKED, "key presses are not supported")
                task.spoken = (f"I can't press keys yet, so I stopped there. "
                               f"You'll need to press {step.value} yourself.")
                return task

            speak(_progress(step))

            # ── Act. Everything below here is the existing DO 1.0 loop. ──
            task.steps.append(step)
            step.state = ActionState.EXECUTING
            before = dict(here)

            if step.verb == "click":
                record = computer_use.act_on(
                    step.target or intent, task=task.id, expect=step.expect,
                    window=here, confirm=confirm, avoid=tried)
            else:
                # "type 3" with the target "Quantity": the planner names the
                # field and, measured, often forgets to click it first. When
                # the name is one editable field, focus it through the same
                # gated click as any other, then type.
                focus = None
                field_name = _field_named(here, step.target)
                if field_name:
                    focus = computer_use.act_on(
                        field_name, task=task.id, expect="", window=here,
                        confirm=confirm, avoid=tried)
                    task.note("focus", f"click {field_name} -> "
                                       f"{getattr(focus, 'outcome', '') or 'done'}")
                if focus is not None and getattr(focus, "outcome", "") == computer_use.REFUSED:
                    record = focus
                else:
                    if focus is not None:
                        task.actions.append(focus)
                    record = computer_use.type_text(step.value, task=task.id,
                                                    confirm=confirm)
                    record = _as_record(record, step, task)
            task.actions.append(record)
            outcome = getattr(record, "outcome", "")
            task.note("action", f"{step.describe()} -> "
                                f"{outcome or record.verdict or 'done'}")
            # A click that moved the screen on starts a new state, and what was
            # pressed in the old one may need pressing again: Save for the
            # second order, after Save for the first. Measured: without this
            # the second Save was refused and the task stopped half done.
            observed = getattr(record, "observed", "") or ""
            if getattr(record, "action", "") == "click" and (
                    outcome == computer_use.CONFIRMED
                    or (outcome == computer_use.UNVERIFIABLE and observed
                        and observed != computer_use.UNREAD)):
                tried.clear()
            # Pressed, and not shown to have worked: that control is not pressed
            # again until something else moves the screen on, whatever the
            # planner calls it next time.
            if (getattr(record, "action", "") == "click"
                    and outcome not in ("", computer_use.CONFIRMED, computer_use.REFUSED)):
                tried.append((record.target, record.x, record.y))

            if task.generation != mine:
                step.state = ActionState.STALE
                task.note("stale", "a later instruction superseded this one")
                return task

            # ── Did it do what it was for? ──
            step.state = ActionState.VERIFYING
            if getattr(record, "verified", None):
                step.state = ActionState.SUCCEEDED
                task.note("verified", (record.observed or "")[:80])
                continue
            if outcome == computer_use.PARTIAL:
                # Half a click or half the text: the screen is in a state
                # nobody chose. Stop and say so; do not build on it.
                step.state = ActionState.FAILED
                task.to(TaskState.BLOCKED, "partly sent")
                task.spoken = record.spoken
                return task
            if outcome == computer_use.UNVERIFIABLE:
                # Sent, and not proven either way: never a success. With
                # nothing specific expected, the next turn re-observes and the
                # goal check at the top of the loop decides. With something
                # expected and not seen, it is accounted for below as it
                # always was, so it counts towards the recovery ceiling.
                step.state = ActionState.UNVERIFIED
                task.note("unverified", (record.observed or "")[:80])
                if not step.expect:
                    continue
            else:
                step.state = ActionState.FAILED

            # ── It did not. Work out why before doing anything else. ──
            after = _sc.window_by_id(before.get("id", 0))
            why = classify(record, before, after, step)
            task.note("failure", why)

            if why == AMBIGUITY:
                task.to(TaskState.WAITING_FOR_USER, why)
                task.question = record.spoken
                return task
            if why in (PERMISSION_FAILURE, REPEATED):
                task.to(TaskState.BLOCKED, why)
                task.spoken = record.spoken
                return task

            task.recoveries += 1
            if task.recoveries > MAX_RECOVERIES:
                task.to(TaskState.BLOCKED, f"{task.recoveries} recoveries")
                task.spoken = _honest_stop(why, record)
                return task

            task.to(TaskState.RECOVERING, why)
            if why in (MODAL_APPEARED, UNEXPECTED_STATE, APP_CHANGED,
                       WINDOW_CHANGED, TARGET_MOVED):
                # Not a failure to undo — a new situation to plan against.
                # The loop's next turn re-observes, which is the recovery.
                continue
            if why in (NO_EFFECT, TARGET_GONE, UNKNOWN):
                time.sleep(0.5)
                continue
            continue
    except Exception as e:
        # Whatever broke, the task is over and says so. It used to escape
        # with the task still "running", so the next request was told one
        # was already in progress for up to five minutes.
        task.note("error", type(e).__name__)
        task.to(TaskState.FAILED, f"stopped on {type(e).__name__}")
        task.spoken = ("Something went wrong part-way through that, so I have "
                       "stopped. Please check the screen before asking again.")
        return task
    finally:
        # The task's permission ends with the task, however it ended.
        computer_use.revoke()
        if _active is task and task.over:
            pass          # keep it for "what happened", until the next task


def _field_named(window: Optional[Dict[str, Any]], target: str) -> str:
    """
    The label of the one enabled, unfocused text field this target names
    exactly, or "" when there is none, more than one, or it already has focus.
    Never a guess: a name that matches a button is not a field to type into.
    """
    if not window or not target:
        return ""
    from skills import ax, grounding_gate
    asked, _roles = grounding_gate.wanted_name(target)
    if not asked:
        return ""
    fields = [e for e in ax.elements_for_window(window)
              if e.role in ax.EDITABLE_ROLES and e.enabled is not False
              and grounding_gate.published_name(e) == asked]
    if len(fields) != 1 or fields[0].focused:
        return ""
    return fields[0].label


def _as_record(result, step: Step, task: Task):
    """Give a typed action the same record shape a click produces."""
    from skills import computer_use
    record = computer_use.ActionRecord(
        task=task.id, at=time.time(), app=task.app, intent=step.describe(),
        action=step.verb, expected=step.expect,
        verdict="" if getattr(result, "ok", False) else "refuse")
    outcome = getattr(result, "outcome", "")
    if outcome:
        # One outcome, and everything else follows from it: a refused step
        # did not act, and `verified` is derived, never `ok`. It used to be
        # `ok`, so every keystroke posted counted as verified.
        record.settle(outcome, getattr(result, "spoken", ""), getattr(result, "refusal", ""))
    else:
        record.spoken = getattr(result, "spoken", "")
        record.verified = getattr(result, "verified", None)
    return record


def _progress(step: Step) -> str:
    """
    What the user hears. Not the timeline.

    One short line per meaningful step, in ordinary words. Nobody wants a
    narration of every click, and "Executing action 3 of 7" is a narration.
    """
    if step.verb == "type":
        return "Typing that in…"
    if step.verb == "press":
        return "Right…"
    return f"{step.target.capitalize()}…" if step.target else "Working…"


def _honest_stop(why: str, record) -> str:
    """Why it stopped, in a sentence that is useful rather than a status code."""
    return {
        NO_EFFECT: "I clicked it and nothing happened. I would rather stop "
                   "than keep pressing it.",
        TARGET_GONE: "The control I was using has gone, and I could not find "
                     "another way through.",
        MODAL_APPEARED: "Something opened that I did not expect and I could "
                        "not work out what it wanted.",
        APP_CHANGED: "The application changed under me, so I stopped.",
        WINDOW_CHANGED: "The window moved or closed while I was working.",
        UNEXPECTED_STATE: "The screen did not end up where I expected and I "
                          "did not want to guess from there.",
        TIMED_OUT: "That took longer than I was willing to keep going for.",
    }.get(why, getattr(record, "spoken", "") or "I could not finish that.")
