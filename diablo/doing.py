"""
What Diablo is about to do, and whether it worked.

The DO engine is finished and it is silent. `do_engine.py` carries two state
machines, eleven named ways an action can go wrong, a risk model with
thresholds, verification against the screen rather than against the click,
and a recovery path. It contains no push, no notify and not one print: a
complete account of a task that nothing can read.

This module is the reading of it, and nothing else. It maps the backend's
states onto the few a person needs to follow, and turns a risk class into a
sentence about consequences. It never decides whether to act, never changes
a threshold, and cannot execute anything.

Three rules, because these are the ones an interface gets wrong:

  ACTING IS NOT CHECKING   executing and verifying are separate phases, and
                           DONE is reachable only from VERIFYING. Sending
                           the event is not the result happening.
  ASKING IS NOT FAILING    a refusal, a denial and an ambiguity are ordinary
                           outcomes, not faults, and are not drawn in red.
  NEVER GUESS              an ambiguous target has no target at all, so
                           there is nothing for the interface to point at.
"""

from dataclasses import dataclass
from typing import Any, List, Optional

# ─── The phases a person follows ────────────────────────────────────────────
#
# Fewer than the backend's, on purpose: TaskState and ActionState between them
# name twenty-three situations, and someone watching needs to know which of
# about a dozen is true. Nothing here replaces those — this is a reading.

UNDERSTANDING = "understanding"
TARGETING = "targeting"      # looking for the place
ASKING = "asking"            # permission, with a question on screen
READY = "ready"              # authorized, about to act
EXECUTING = "executing"
VERIFYING = "verifying"      # checking the screen, not the click
RECOVERING = "recovering"
WAITING = "waiting"          # the user has to do something
AMBIGUOUS = "ambiguous"      # two candidates; asking rather than picking
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"          # refused, safely, on purpose
CANCELLED = "cancelled"

PHASES = (UNDERSTANDING, TARGETING, ASKING, READY, EXECUTING, VERIFYING,
          RECOVERING, WAITING, AMBIGUOUS, DONE, FAILED, BLOCKED, CANCELLED)

# Over. A late event belonging to one of these changes nothing.
CLOSED = (DONE, FAILED, BLOCKED, CANCELLED)

# Drawn as a fault. A refusal and a denial are deliberately not here: being
# told "I won't do that safely" is an answer, not a crash.
FAULTS = (FAILED,)


def is_fault(phase: str) -> bool:
    return phase in FAULTS


# ─── The backend's states, read ─────────────────────────────────────────────

_TASK = {
    "new": UNDERSTANDING,
    "understanding": UNDERSTANDING,
    "planning": UNDERSTANDING,
    "observing": TARGETING,
    "locating": TARGETING,
    "awaiting_permission": ASKING,
    "executing": EXECUTING,
    "verifying": VERIFYING,
    "recovering": RECOVERING,
    "waiting_for_user": WAITING,
    "completed": DONE,
    "blocked": BLOCKED,
    "failed": FAILED,
    "cancelled": CANCELLED,
}

_ACTION = {
    "planned": UNDERSTANDING,
    "targeting": TARGETING,
    "authorized": READY,
    "executing": EXECUTING,
    "verifying": VERIFYING,
    "succeeded": DONE,
    "failed": FAILED,
    "cancelled": CANCELLED,
    "stale": RECOVERING,
}


def phase_for(state: Any, action: Any = None) -> str:
    """
    One phase from the backend's own state.

    The action's state refines the task's where it is the more specific of
    the two — a task saying EXECUTING while its action says VERIFYING is
    checking, and that distinction is the point of the whole build.
    """
    if action is not None:
        refined = _ACTION.get(getattr(action, "value", str(action)))
        if refined in (VERIFYING, READY, DONE, FAILED):
            return refined
    return _TASK.get(getattr(state, "value", str(state)), UNDERSTANDING)


# ─── What is about to happen ────────────────────────────────────────────────

@dataclass
class Intent:
    """The action preview: the verb and the thing, in the screen's words."""

    verb: str = ""               # Click, Choose, Open, Enter
    target: str = ""             # Save, Dark Mode, the account name

    @property
    def preview(self) -> str:
        return (" ".join(p for p in (self.verb.strip(), self.target.strip()) if p)
                or "Do it")


# What each risk class costs, said as a consequence rather than a class. The
# wording follows computer_use's own comments: LOW is undone by looking away,
# MEDIUM is undone with effort, HIGH is not undone.
_CONSEQUENCE = {
    "low": "",
    "medium": " That changes something you'd have to put back.",
    "high": " That can't be undone.",
}


def consent_for(risk: str, intent: Intent) -> str:
    """
    The permission question, as Diablo would ask it.

    Never the risk class, never a confidence value, never a tool name: what
    it would do, and what it would cost if it is wrong.
    """
    tail = _CONSEQUENCE.get(str(risk).lower(), "")
    if tail:
        return "%s?%s Shall I?" % (intent.preview, tail)
    return "%s?" % intent.preview


# ─── When something moves under him ─────────────────────────────────────────
#
# The engine names eleven ways an action can go wrong. A person needs the
# sentence, not the identifier.

_RECOVERY = {
    "target_moved": "That moved. I'm looking again.",
    "target_disappeared": "I've lost the target.",
    "window_changed": "The window changed. I'm checking again.",
    "app_changed": "A different application came forward.",
    "modal_appeared": "Something opened on top. I'm looking at it.",
    "action_registered_but_no_effect": "That didn't take effect.",
    "unexpected_state": "The screen isn't what I expected.",
    "ambiguity": "There's more than one of those.",
    "permission_failure": "I don't have permission for that.",
    "timeout": "That took too long.",
    "unknown": "Something changed. I'm checking again.",
}


def recovery_words(code: str) -> str:
    return _RECOVERY.get(str(code), _RECOVERY["unknown"])


# ─── The task being watched ─────────────────────────────────────────────────

@dataclass
class Target:
    """Where the action is going to happen, once that is actually known."""

    x: float = 0.0
    y: float = 0.0
    label: str = ""


class Doing:
    """
    One task, the phase it is in, and the refusal to be moved by an old one.

    Two identities guard it, both the backend's own: the task id, so an event
    from a superseded task changes nothing, and a sequence number, so events
    arriving out of order are dropped. There is no second generation system
    here — this reads the one that exists.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.task_id = ""
        self.phase = UNDERSTANDING
        self.intent = Intent()
        self.consent: Optional[str] = None
        self.target: Optional[Target] = None
        self.index = 1
        self.total = 1
        self.note = ""
        self.seq = 0
        self.history: List[str] = []

    # ── whether an event counts ─────────────────────────────────────────

    def accept(self, seq: Optional[int] = None,
               task_id: Optional[str] = None) -> bool:
        if task_id is not None and task_id != self.task_id:
            return False
        if seq is not None and int(seq) <= self.seq:
            return False
        return self.phase not in CLOSED

    def _take(self, phase: str, seq: Optional[int] = None) -> None:
        if seq is not None:
            self.seq = int(seq)
        self.phase = phase
        self.history.append(phase)

    # ── the lifecycle ───────────────────────────────────────────────────

    def begin(self, task_id: str, intent: Optional[Intent] = None,
              index: int = 1, total: int = 1,
              seq: Optional[int] = None) -> bool:
        """A task, or the next step of one. Anything showing belonged to the last."""
        if seq is not None and int(seq) <= self.seq:
            return False
        held = self.seq
        self.reset()
        self.seq = held if seq is None else int(seq)
        self.task_id = task_id or ""
        self.intent = intent or Intent()
        self.index, self.total = max(1, int(index)), max(1, int(total))
        self._take(UNDERSTANDING)
        return True

    def targeting(self, seq=None, task_id=None) -> bool:
        if not self.accept(seq, task_id):
            return False
        self._take(TARGETING, seq)
        return True

    def located(self, x: float, y: float, label: str = "",
                seq=None, task_id=None) -> bool:
        """A place, once one is actually known. Never called on a guess."""
        if not self.accept(seq, task_id):
            return False
        self.target = Target(x=x, y=y, label=label)
        self._take(TARGETING, seq)
        return True

    def asking(self, question: str, seq=None, task_id=None) -> bool:
        if not self.accept(seq, task_id):
            return False
        self.consent = question
        self._take(ASKING, seq)
        return True

    def granted(self, seq=None, task_id=None) -> bool:
        if self.phase != ASKING or not self.accept(seq, task_id):
            return False
        self.consent = None
        self._take(READY, seq)
        return True

    def denied(self, seq=None, task_id=None) -> bool:
        """Said no. An ordinary answer, not a failure."""
        if self.phase != ASKING or not self.accept(seq, task_id):
            return False
        self.consent = None
        self._take(CANCELLED, seq)
        return True

    def executing(self, seq=None, task_id=None) -> bool:
        if not self.accept(seq, task_id):
            return False
        self._take(EXECUTING, seq)
        return True

    def verifying(self, seq=None, task_id=None) -> bool:
        if not self.accept(seq, task_id):
            return False
        self._take(VERIFYING, seq)
        return True

    def verified(self, seq=None, task_id=None) -> bool:
        """
        Done — and only from VERIFYING.

        This is the rule the whole build exists for. The click having been
        dispatched is not the change having happened; the engine knows the
        difference, so the interface must not lose it.
        """
        if self.phase != VERIFYING or not self.accept(seq, task_id):
            return False
        self.target = None
        self._take(DONE, seq)
        return True

    def failed(self, note: str = "", seq=None, task_id=None) -> bool:
        if not self.accept(seq, task_id):
            return False
        self.note = note or "I couldn't verify that."
        self.target = None
        self._take(FAILED, seq)
        return True

    def blocked(self, note: str = "", seq=None, task_id=None) -> bool:
        """Refused, on purpose, with a reason a person can act on."""
        if not self.accept(seq, task_id):
            return False
        self.note = note or "I can't safely do that."
        self.target = None
        self._take(BLOCKED, seq)
        return True

    def ambiguous(self, question: str, seq=None, task_id=None) -> bool:
        """
        Two candidates. There is no target while this is true, because a
        target would be the interface choosing one — which is the guess.
        """
        if not self.accept(seq, task_id):
            return False
        self.note = question
        self.target = None
        self.consent = question
        self._take(AMBIGUOUS, seq)
        return True

    def waiting(self, seq=None, task_id=None) -> bool:
        if not self.accept(seq, task_id):
            return False
        self._take(WAITING, seq)
        return True

    def recovering(self, code: str = "unknown", seq=None, task_id=None) -> bool:
        if not self.accept(seq, task_id):
            return False
        self.note = recovery_words(code)
        self._take(RECOVERING, seq)
        return True

    def finish(self, why: str = CANCELLED, seq=None, task_id=None) -> bool:
        if self.phase in CLOSED:
            return False
        self.consent = None
        self.target = None
        self._take(why if why in CLOSED else CANCELLED, seq)
        return True

    # ── what is true now ────────────────────────────────────────────────

    @property
    def live(self) -> bool:
        return bool(self.task_id) and self.phase not in CLOSED

    @property
    def line(self) -> str:
        return line_for(self.phase, self.intent, self.index, self.total,
                        self.note, self.consent or "")


# ─── What the surface says ──────────────────────────────────────────────────

def counter_of(index: int, total: int) -> str:
    return "Step %d of %d" % (index, total) if total > 1 else ""


def line_for(phase: str, intent: Optional[Intent] = None, index: int = 1,
             total: int = 1, note: str = "", consent: str = "") -> str:
    """One short line: the step, then what is happening to it."""
    intent = intent or Intent()
    head = counter_of(index, total)
    what = intent.preview

    if phase in (ASKING, AMBIGUOUS):
        body = consent or note or ("%s?" % what)
    elif phase == UNDERSTANDING:
        # A step announces itself by what it is going to do, which is what
        # makes "Step 2 of 4 · Choose Dark Mode" readable before anything
        # has happened. Only before there is a plan is there nothing to name.
        body = what if (intent.verb or intent.target) else "Working out how"
    elif phase == TARGETING:
        body = "Finding %s" % (intent.target.strip() or "the place")
    elif phase == READY:
        body = what
    elif phase == EXECUTING:
        body = "%s…" % what
    elif phase == VERIFYING:
        body = "Checking"
    elif phase == RECOVERING:
        body = note or recovery_words("unknown")
    elif phase == WAITING:
        body = "Go ahead"
    elif phase == DONE:
        body = "Done"
    elif phase == FAILED:
        body = note or "I couldn't verify that."
    elif phase == BLOCKED:
        body = note or "I can't safely do that."
    elif phase == CANCELLED:
        body = "Stopped"
    else:
        body = ""

    if not body:
        return ""
    return "%s · %s" % (head, body) if head else body


def announcement(phase: str) -> str:
    """What a screen reader says: a human phrase, never the internal name."""
    return {
        UNDERSTANDING: "Diablo: working it out",
        TARGETING: "Diablo: finding the place",
        ASKING: "Diablo: asking for permission",
        READY: "Diablo: ready",
        EXECUTING: "Diablo: doing it",
        VERIFYING: "Diablo: checking",
        RECOVERING: "Diablo: checking again",
        WAITING: "Diablo: waiting for you",
        AMBIGUOUS: "Diablo: asking which one",
        DONE: "Diablo: done",
        FAILED: "Diablo: could not verify",
        BLOCKED: "Diablo: declined",
        CANCELLED: "Diablo: stopped",
    }.get(phase, "")
