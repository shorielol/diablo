"""
A lesson: which step Diablo is showing, and the rule that there is only one.

The walkthrough itself already exists. `tutor.py` plans the steps, finds each
one on screen, waits for the click, asks the screen whether anything actually
changed, and re-anchors the remaining steps against the screen that resulted.
It has done all of that for a long time, and it says so — to stdout, where
nobody can see it. The orb stayed at home throughout and a separate overlay
did the pointing.

So this module is not a step engine. It is the small piece that was missing:
something that holds which step is current, refuses to let two be current at
once, and turns the lifecycle into the one sentence the surface shows.

Two rules worth stating, because the failure modes of a walkthrough are these
two going wrong:

  ONE CURRENT TARGET   presenting a step clears the one before it. Never two
                       rings, never a stale ring beside a new one.
  NEVER CELEBRATE      verified() counts only after the user was actually
                       asked to act and the screen was actually checked. A
                       step that was not verified says so.

No AppKit, no timers, no drawing. The shell moves the orb, the overlay draws,
motion.py owns every animation, and this decides what is true.
"""

from dataclasses import dataclass
from typing import List, Optional

# Where a lesson can be. These are names for things that happen in the
# walkthrough loop, not a second state machine running beside it.
IDLE = "idle"
GUIDING = "guiding"        # the step is shown and he is beside it
WAITING = "waiting"        # the user has been asked to act
VERIFIED = "verified"      # the screen really changed
UNVERIFIED = "unverified"  # it did not, and that is said plainly
DONE = "done"
CANCELLED = "cancelled"

PHASES = (IDLE, GUIDING, WAITING, VERIFIED, UNVERIFIED, DONE, CANCELLED)

# A lesson is over in these; a late event belonging to it is not acted on.
CLOSED = (DONE, CANCELLED)


@dataclass
class Step:
    """One step, as the interface needs it."""

    index: int
    total: int
    label: str = ""              # what it points at, in the screen's words
    say: str = ""                # the sentence the backend speaks
    x: float = 0.0               # Quartz, top-left
    y: float = 0.0
    w: float = 0.0               # a content region rather than a control
    h: float = 0.0
    on_light: bool = False
    found: bool = True           # False: planned, but not located on screen

    @property
    def counter(self) -> str:
        return "%d of %d" % (self.index, self.total)

    @property
    def is_region(self) -> bool:
        """A paragraph, a number, a row — outlined rather than ringed."""
        return self.w > 0.0 and self.h > 0.0

    @property
    def placed(self) -> bool:
        """Whether there is a real place on screen to send him to."""
        return bool(self.found) and (self.x != 0.0 or self.y != 0.0)


class Lesson:
    """
    The current step, and the refusal to have two.

    Events arrive numbered. A lesson that has finished ignores everything
    that follows it, and a step from an older lesson cannot replace a newer
    one's target — the same rule the conversation surface uses for its own
    reports, for the same reason: what arrives late has already been
    overtaken.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.phase = IDLE
        self.step: Optional[Step] = None
        self.total = 0
        self.app = ""
        self.seq = 0
        self.note = ""          # why a step was not verified, in plain words
        self.history: List[str] = []

    # ── the lifecycle ───────────────────────────────────────────────────

    def accept(self, seq: Optional[int]) -> bool:
        """An event is acted on only if it is newer than the last one acted on."""
        if seq is None:
            return True
        return int(seq) > self.seq

    def _take(self, seq: Optional[int], phase: str) -> None:
        if seq is not None:
            self.seq = int(seq)
        self.phase = phase
        self.history.append(phase)

    def begin(self, total: int, app: str = "", seq: Optional[int] = None) -> bool:
        """A walkthrough started. Anything still showing belongs to the last one."""
        if not self.accept(seq):
            return False
        held = self.seq
        self.reset()
        self.seq = held if seq is None else int(seq)
        self.total = max(0, int(total))
        self.app = app or ""
        self.history.append(GUIDING)
        self.phase = GUIDING
        self.step = None
        return True

    def present(self, step: Step, seq: Optional[int] = None) -> bool:
        """
        Show a step. Whatever was current stops being current here, before
        anything moves — that is the whole of the one-target rule.
        """
        if self.phase in CLOSED or not self.accept(seq):
            return False
        self.step = step
        self.total = step.total or self.total
        self.note = ""
        self._take(seq, GUIDING)
        return True

    def waiting(self, seq: Optional[int] = None) -> bool:
        """He has asked the user to do it. Only meaningful on a shown step."""
        if self.phase != GUIDING or self.step is None or not self.accept(seq):
            return False
        self._take(seq, WAITING)
        return True

    def verified(self, seq: Optional[int] = None) -> bool:
        """
        The screen really changed.

        Only from WAITING. A lesson cannot congratulate the user on a step it
        never asked them to take — which is what "do not falsely celebrate
        success" looks like in code rather than in prose.
        """
        if self.phase != WAITING or not self.accept(seq):
            return False
        self._take(seq, VERIFIED)
        return True

    def unverified(self, note: str = "", seq: Optional[int] = None) -> bool:
        """Asked, waited, and nothing changed. Said, not hidden."""
        if self.phase != WAITING or not self.accept(seq):
            return False
        self.note = note or ""
        self._take(seq, UNVERIFIED)
        return True

    def finish(self, why: str = DONE, seq: Optional[int] = None) -> bool:
        """The lesson is over, by completion or by cancellation."""
        if self.phase in CLOSED or not self.accept(seq):
            return False
        self._take(seq, CANCELLED if why == CANCELLED else DONE)
        self.step = None
        return True

    # ── what is true now ────────────────────────────────────────────────

    @property
    def live(self) -> bool:
        return self.phase not in (IDLE,) + CLOSED

    @property
    def current(self) -> Optional[Step]:
        """The one target, or nothing. Nothing once the lesson has closed."""
        return self.step if self.live else None

    @property
    def line(self) -> str:
        return line_for(self.phase, self.step, self.note)


# ─── What the surface says ──────────────────────────────────────────────────
#
# Compact, because the screen is the explanation and this is its caption. The
# step's own sentence is spoken and shown by GUI-4; these are the words about
# the lesson, not about the task.

def counter_of(step: Optional[Step]) -> str:
    if step is None or step.total <= 1:
        return ""
    return "Step %s" % step.counter


def line_for(phase: str, step: Optional[Step] = None, note: str = "") -> str:
    """One short line. Never two places saying the same thing."""
    head = counter_of(step)
    if phase == GUIDING:
        if step is None:
            return "Finding it…"
        what = step.label.strip()
        body = ("This is %s" % what) if what else "Here"
        return "%s · %s" % (head, body) if head else body
    if phase == WAITING:
        return "%s · Go ahead" % head if head else "Go ahead"
    if phase == VERIFIED:
        return "%s · Done" % head if head else "Done"
    if phase == UNVERIFIED:
        return note.strip() or "That didn't seem to change anything."
    if phase == DONE:
        return "That's the lot."
    if phase == CANCELLED:
        return "Stopped."
    return ""


def is_fault(phase: str) -> bool:
    """Which phases the surface draws as a fault rather than as progress."""
    return phase == UNVERIFIED
