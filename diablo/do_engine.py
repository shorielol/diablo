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
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

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

    def describe(self) -> str:
        if self.kind == "file_exists":
            return f"{self.value} exists in {self.where or 'the folder'}"
        if self.kind == "window_exists":
            return f"a window for {self.value} is open"
        if self.kind == "text_absent":
            return f"“{self.value}” is gone from the screen"
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
                present = self.value.lower() in text.lower()
                return present if self.kind == "text_on_screen" else not present
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
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "verb": {"type": "string", "enum": ["click", "type", "press", "wait", "done"]},
        "target": {"type": "string"},
        "value": {"type": "string"},
        "expect": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["verb", "target", "value", "why"],
}

PLAN_SYSTEM = """You decide the single next action in someone else's application.

You are given a goal and a description of what is on screen right now. Reply
with ONE action, never a sequence — you will be asked again after it has been
carried out and the screen has been looked at afresh.

- `target` names a control the way a person would say it: "the Save button",
  "the search field". Never a coordinate; you cannot see coordinates and
  something else resolves them.
- `expect` is the text you would expect to appear if the action worked. Leave
  it empty if you do not know.
- Use "done" when what is on screen already satisfies the goal.
- Use "wait" if the screen looks like it is still loading.
- If the screen shows something you did not expect, deal with that first. An
  unexpected dialog is usually the next thing to handle, not an obstacle to
  push past.

Fill in every field, using an empty string where one does not apply.

- `click` MUST name a control in `target`, copied from the list of controls on
  screen. A click with an empty target cannot be carried out by anything.
- `type` MUST put the text in `value`, and `press` MUST put the key in `value`.
- `target` is a name, never a coordinate."""


def _screen_summary(window: Optional[Dict[str, Any]], limit: int = 900) -> str:
    """A compact account of the current screen for the planner."""
    from skills import screen_context as _sc
    if not window:
        return "No window is visible."
    parts = [f"Application: {window.get('app','?')}",
             f"Window: {window.get('title','')[:80]}"]
    try:
        text = _sc.look(window.get("app", "")) or ""
        if text.startswith("Error:") or text.startswith("Refused:"):
            parts.append(text[:120])
        else:
            parts.append("On screen:\n" + text[:limit])
    except Exception:
        pass
    try:
        from skills import ax
        controls = [e.describe() for e in ax.elements_for_window(window)[:24]]
        if controls:
            parts.append("Controls: " + "; ".join(controls))
    except Exception:
        pass
    return "\n".join(parts)


def model_planner(task: "Task", window: Optional[Dict[str, Any]]) -> Step:
    """
    Ask the local model for the next step. The default planner.

    Injectable so tests do not measure a sampled model: a suite whose result
    depends on what a 4B felt like saying is measuring the 4B.
    """
    from skills import local_brain
    done = "; ".join(s.describe() for s in task.steps[-4:]) or "nothing yet"
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
                target=str(reply.get("target", ""))[:80],
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
    if record.verdict == "ask":
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

    if record.verified is False:
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

            speak(_progress(step))

            # ── Act. Everything below here is the existing DO 1.0 loop. ──
            task.steps.append(step)
            step.state = ActionState.EXECUTING
            before = dict(here)

            if step.verb == "click":
                record = computer_use.act_on(
                    step.target or intent, task=task.id, expect=step.expect,
                    window=here, confirm=confirm)
            else:
                record = computer_use.type_text(step.value, task=task.id,
                                                confirm=confirm)
                record = _as_record(record, step, task)
            task.actions.append(record)
            task.note("action", f"{step.describe()} -> {record.verdict or 'done'}")

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

            # ── It did not. Work out why before doing anything else. ──
            step.state = ActionState.FAILED
            after = _sc.window_by_id(before.get("id", 0))
            why = classify(record, before, after, step)
            task.note("failure", why)

            if why == AMBIGUITY:
                task.to(TaskState.WAITING_FOR_USER, why)
                task.question = record.spoken
                return task
            if why == PERMISSION_FAILURE:
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
    finally:
        if _active is task and task.over:
            pass          # keep it for "what happened", until the next task


def _as_record(result, step: Step, task: Task):
    """Give a typed action the same record shape a click produces."""
    from skills import computer_use
    record = computer_use.ActionRecord(
        task=task.id, at=time.time(), app=task.app, intent=step.describe(),
        action=step.verb, expected=step.expect,
        verdict="" if getattr(result, "ok", False) else "refuse",
        spoken=getattr(result, "spoken", ""))
    record.verified = bool(getattr(result, "ok", False))
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
