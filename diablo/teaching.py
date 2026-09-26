"""
How to show something, once perception has said where it is.

Every target Diablo pointed at used to be shown the same way: the orb walked
over and a ring appeared. For a button that is right. For a shaded region the
ring says less than the region's own outline, and for "show me how the
subsidy creates welfare loss" there was nothing that could show it at all —
the answer is three places in order, and a ring is only ever one place.

This module decides HOW. It never decides WHAT or WHERE: target.py says what
kind of thing was asked for, grounding and perception say where it is, and
nothing here can put a place on the screen that they did not produce.

Four ways of showing something, and no more:

    POINT    a precise place — the existing ring, unchanged
    REGION   an area — the existing outline, unchanged
    CURSOR   a small pointer Diablo draws and moves; never the real one
    PATH     several grounded places in order, one current at a time

No AppKit here. The shell draws the teaching cursor from `CursorMotion`,
which is made of the same springs, clips and timing tokens as the orb and is
stepped by the shell's one display link. The path driver takes its
side effects as arguments, so the whole lifecycle is testable without a
screen, a voice or a model.
"""

import math
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

from skills import lesson, motion, target

Point = Tuple[float, float]

# ─── The four ways ───────────────────────────────────────────────────────────

POINT = "point"
REGION = "region"
CURSOR = "cursor"
PATH = "path"
MODES = (POINT, REGION, CURSOR, PATH)

# An area no bigger than this on either edge is a place, not an area: a
# crossing of two curves, a dot on a line. Outlining a 40-point box round an
# intersection says "this box"; putting the tip on it says "here". About twice
# the ring's radius (26pt), so anything the ring would fit is a point.
POINT_LIKE = 56.0

# Where a place is allowed to have come from. These are the source names
# perception and grounding already stamp on their candidates, plus "fixture"
# for geometry a deterministic fixture published about itself. A rectangle
# with any other provenance — a model's free-form reply above all — is not a
# place Diablo may send anything to.
GROUNDED = ("accessibility", "ocr", "vision", "region", "fixture")

# How a step that needed the user ended. The lesson's own phase names, so the
# surface can say them without translating.
VERIFIED = lesson.VERIFIED
UNVERIFIED = lesson.UNVERIFIED

# Phrases that say the relationship or the change is what is wanted. A hint
# only: with fewer than two grounded places there is nothing to move between,
# and the single place is shown the way it would have been anyway.
MOVEMENT_SIGNALS = (
    "show me how", "walk me through", "follow this", "trace", "show what happens",
    "what happens when", "what happens if", "take me through", "explain the change",
    "show the movement", "show the relationship", "how does", "step by step",
)


def asks_for_movement(question: str) -> bool:
    lowered = (question or "").lower()
    return any(phrase in lowered for phrase in MOVEMENT_SIGNALS)


@dataclass
class Presentation:
    mode: str               # one of MODES, or "" when there is nothing to show
    reason: str             # one sentence, for the trace; never spoken


def _point_like(size: Optional[Tuple[float, float]]) -> bool:
    if not size:
        return False
    w, h = float(size[0] or 0.0), float(size[1] or 0.0)
    return 0.0 < w <= POINT_LIKE and 0.0 < h <= POINT_LIKE


def choose(kind: str, size: Optional[Tuple[float, float]] = None,
           question: str = "", waypoints: int = 1) -> Presentation:
    """
    How to show a target, from what it is and how many grounded places there
    are. Deterministic, and every answer carries its reason.

    Several grounded places are a path whatever each one is. After that the
    target's own kind decides, because it is the stronger signal: "show me
    how to save" is still a question about one button.
    """
    if waypoints >= 2:
        return Presentation(PATH, f"{waypoints} grounded places, shown in order")
    note = (" (asked to be shown how, but only one place was found, so it is "
            "shown as that one place)") if asks_for_movement(question) else ""
    if kind == target.SCREEN:
        return Presentation("", "the whole screen is not one thing to show")
    if kind in (target.CONTROL, target.TEXT):
        return Presentation(POINT, "something to look at or press: a precise place" + note)
    if kind == target.OBJECT:
        return Presentation(CURSOR, "the thing under the pointer: shown with the "
                                    "teaching cursor" + note)
    if kind in (target.REGION, target.FIGURE):
        if _point_like(size):
            return Presentation(POINT, "an area no larger than a crossing: shown as "
                                       "the place it is" + note)
        return Presentation(REGION, "an area: outlined at its own size, however "
                                    "large" + note)
    return Presentation(POINT, "an unrecognised kind: the safest precise place" + note)


# ─── Places ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Waypoint:
    """One grounded place in a lesson. Quartz, top-left, like Candidate."""

    label: str
    say: str
    x: float
    y: float
    w: float
    h: float
    kind: str
    mode: str
    sources: Tuple[str, ...]
    act: bool = False           # the user has to do something here

    @property
    def centre(self) -> Point:
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)

    @property
    def is_region(self) -> bool:
        return self.mode == REGION


def _finite(*values: Any) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def _grounded(sources: Sequence[str]) -> bool:
    return any(s in GROUNDED for s in (sources or ()))


class _Checked:
    """A candidate as the target contract should judge it."""

    def __init__(self, candidate: Any, sources: Tuple[str, ...]):
        self.x, self.y = candidate.x, candidate.y
        self.width, self.height = candidate.width, candidate.height
        self.role = getattr(candidate, "role", "")
        self.label = getattr(candidate, "label", "")
        self.sources = list(sources)


def _as_checked(candidate: Any, sources: Tuple[str, ...]) -> Any:
    """
    A fixture's published rectangle is ground truth about an area, which is
    what the "region" source stands for in the target contract: geometry that
    is the thing itself rather than words about it. It is judged as that —
    size and all — and nothing else is widened. An OCR caption is still an
    OCR caption, and still refused as a region.
    """
    if "fixture" in sources and target.REGION_SOURCE not in sources:
        return _Checked(candidate, sources + (target.REGION_SOURCE,))
    return candidate


def waypoint(candidate: Any, kind: str, say: str = "", label: str = "",
             act: bool = False) -> Optional[Waypoint]:
    """
    A place, made only from something perception or grounding returned.

    Refused, rather than repaired: a candidate with no grounded source, one
    whose geometry is not a number, and one that does not satisfy the target
    contract for the kind that was asked for. The last is the Phase B rule —
    a caption read off the screen is never the region it names — applied to
    every step of a path as well as to a single answer.
    """
    if candidate is None:
        return None
    try:
        x, y = float(candidate.x), float(candidate.y)
        w, h = float(candidate.width), float(candidate.height)
    except (AttributeError, TypeError, ValueError):
        return None
    if not _finite(x, y, w, h) or w < 0.0 or h < 0.0:
        return None
    sources = tuple(getattr(candidate, "sources", ()) or ())
    if not _grounded(sources):
        return None
    if not target.satisfies(kind, _as_checked(candidate, sources)):
        return None
    mode = choose(kind, (w, h)).mode or POINT
    return Waypoint(label=label or str(getattr(candidate, "label", "") or ""),
                    say=say or "", x=x, y=y, w=w, h=h, kind=kind, mode=mode,
                    sources=sources, act=bool(act))


@dataclass
class TeachingPath:
    """Grounded places in the order they should be shown."""

    steps: List[Waypoint] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.steps)

    @classmethod
    def of(cls, candidates: Sequence[Optional[Waypoint]]) -> "TeachingPath":
        """
        Keep only real, grounded waypoints. A Waypoint built by hand with
        invented provenance is dropped here even though it is the right type,
        because the type is not the guarantee — where it came from is.
        """
        kept = []
        for step in candidates or ():
            if not isinstance(step, Waypoint):
                continue
            if not _grounded(step.sources) or not _finite(step.x, step.y, step.w, step.h):
                continue
            kept.append(step)
        return cls(kept)


def announce(label: str, index: int, total: int) -> str:
    """What is said and shown in words, so motion is never the only signal."""
    what = (label or "here").strip()
    if total <= 1:
        return f"Looking here: {what}"
    return f"Step {index} of {total}: {what}"


# ─── What a step may say ─────────────────────────────────────────────────────
#
# The contract between whatever writes a lesson and the code that shows it.
# `point_at`'s tool schema uses this object and so does the step generation
# for a spoken explanation, so the two cannot drift apart. There is no field
# for a position, on purpose: a step names what to find, grounding finds it,
# and a coordinate from a model has nowhere to go.

STEP_ITEM = {
    "type": "object",
    "properties": {
        "what": {"type": "string",
                 "description": "The thing to show, as it would be named on its "
                                "own: a shaded area, a point where two lines "
                                "cross, a curve, a button. Not its caption, and "
                                "never where it is."},
        "say": {"type": "string",
                "description": "One short spoken sentence about it."},
        "kind": {"type": "string",
                 "description": "'region' for an area of a picture, 'figure' "
                                "for a whole chart, 'control' for a button or "
                                "menu, 'text' for words. Empty if unsure."},
    },
    "required": ["what", "say"],
}

# The most a single lesson shows. More than this is a lecture, and every step
# costs a look at the screen.
MAX_STEPS = 6


def step_kind(value: Any) -> str:
    """
    A step's kind as a target kind, or "" to let the phrasing decide.

    Writers of steps say "POINT" and "REGION", which are ways of showing, not
    kinds of thing. "region" is both and passes; "cursor" means the thing the
    cursor shows, an object; "point" says nothing about what the thing is, so
    it is dropped rather than guessed into a kind.
    """
    wanted = str(value or "").strip().lower()
    if wanted in target.KINDS:
        return wanted
    if wanted == CURSOR:
        return target.OBJECT
    return ""


# Words that say what KIND of thing a step is and not which one. Told to name
# parts of a picture, the 4B wrote "region", "region", "curve": grammatical,
# schema-valid, and nothing grounding could look for.
KIND_WORDS = {"region", "regions", "area", "areas", "part", "parts", "place",
              "places", "point", "points", "thing", "things", "shape", "shapes",
              "spot", "item", "figure", "curve", "curves", "line", "lines",
              "section", "portion", "element"}


def _names_something(what: str) -> bool:
    return bool(target._named(what) - KIND_WORDS)


def clean_steps(raw: Any, limit: int = MAX_STEPS) -> List[dict]:
    """
    Steps as `point_at` accepts them, from whatever a writer produced.

    Only what, say, kind and act survive. Anything else a model adds — an x,
    a box, a guess at where the thing is — is dropped here, before it can
    reach a tool that draws. So is a step that names only a kind of thing.
    """
    steps: List[dict] = []
    for item in list(raw) if isinstance(raw, (list, tuple)) else []:
        if not isinstance(item, dict):
            continue
        what, say = item.get("what"), item.get("say")
        if not isinstance(what, str) or not isinstance(say, str):
            continue
        what, say = what.strip(), say.strip()
        if not what or not say or not _names_something(what):
            continue
        step = {"what": what, "say": say}
        kind = step_kind(item.get("kind"))
        if kind:
            step["kind"] = kind
        if item.get("act") is True:
            step["act"] = True
        steps.append(step)
        if len(steps) >= limit:
            break
    return steps


# ─── The path, driven ────────────────────────────────────────────────────────
#
# The runtime that shows each step is supplied, not imported. In the app it is
# the bridge in diablo_app, which talks to the shell; in the tests it is a
# recorder. Unset, nothing here assumes there is a screen at all.

CHANNEL = None

# How long to wait for the shell to say the cursor has arrived. Only a
# ceiling: the shell answers as soon as it arrives, and releases the wait if
# the lesson is cancelled or overtaken, so this is reached only if the shell
# has gone away altogether.
ARRIVAL_TIMEOUT = motion.Timing.MOVE_MAX + motion.Timing.SETTLE + 1.5


@dataclass
class Outcome:
    done: bool
    shown: int
    note: str = ""


def run_path(path: TeachingPath, channel: Any,
             speak: Optional[Callable[[str], None]] = None,
             should_stop: Optional[Callable[[], bool]] = None,
             dwell: float = motion.Timing.GUIDE_HOLD) -> Outcome:
    """
    Show each place, let the cursor settle, explain it, then move on.

    Never speaks about a step before the shell has shown it, and never shows
    the next one while the last sentence is still being said: `speak` blocks
    until it has been heard, which is the real audio event this waits on. With
    no voice, each step stays up for the least time it takes to be read.

    A step marked `act` hands the next move to the user through the existing
    closed loop — the shell's armed click detection and the screen check —
    and the path continues only after that loop has reported.
    """
    def stopped() -> bool:
        if should_stop is not None and should_stop():
            return True
        try:
            return bool(channel.cancelled())
        except Exception:
            return False

    total = path.total
    if total == 0:
        return Outcome(False, 0, "nothing grounded to show")
    channel.begin(total)
    shown = 0
    for index, step in enumerate(path.steps, 1):
        if stopped():
            channel.finish(lesson.CANCELLED)
            return Outcome(False, shown, "stopped")
        arrived = channel.step(step, index, total)
        if arrived is not None:
            arrived.wait(ARRIVAL_TIMEOUT)
        if stopped():
            channel.finish(lesson.CANCELLED)
            return Outcome(False, shown, "stopped")
        # The shell releases a wait it could not honour — a place on no
        # display, a step overtaken — with `shown` False. Explaining a place
        # that is not on screen would be the worst kind of confident.
        if arrived is not None and getattr(arrived, "shown", True) is False:
            continue
        shown += 1
        if step.say and speak is not None:
            speak(step.say)
        elif channel.wait(dwell):
            channel.finish(lesson.CANCELLED)
            return Outcome(False, shown, "stopped")
        if step.act:
            channel.await_action(step, index, total)
            if stopped():
                channel.finish(lesson.CANCELLED)
                return Outcome(False, shown, "stopped")
    channel.finish(lesson.DONE)
    return Outcome(True, shown)


# ─── The teaching cursor's motion ────────────────────────────────────────────

HIDDEN = "hidden"
TRAVELLING = "travelling"
RESTING = "resting"
LEAVING = "leaving"
FADING_OUT = "fading-out"       # reduced motion only
FADING_IN = "fading-in"         # reduced motion only


@dataclass
class CursorLook:
    position: Point             # AppKit points: where the tip is
    alpha: float
    cue: Optional[float]        # 0..1 while the arrival cue plays, else None


class CursorMotion:
    """
    Where Diablo's teaching cursor is, frame by frame. Pure: no drawing.

    Built from motion.py and nothing else — a Spring2D for position, a Spring
    for opacity, a Clip for the one arrival cue — so it moves with the same
    critically damped, speed-capped, no-overshoot character as the orb, and is
    exact whatever the frame rate. It only asks for frames while something is
    actually changing; at rest it is still.

    It is never the system pointer and never touches it. It is a picture of a
    pointer, in a window that lets every click through.
    """

    def __init__(self, origin: Point = (0.0, 0.0), reduced: bool = False):
        self.spot = motion.Spring2D(origin)
        self.alpha = motion.Spring(0.0, motion.Timing.LOOK)
        self.cue: Optional[motion.Clip] = None
        self.origin = origin
        self.reduced = bool(reduced)
        self.phase = HIDDEN
        self._goal: Optional[Point] = None
        self._trip = 0.0
        self._on_arrive: Optional[Callable[[], None]] = None
        self._hold: Optional[motion.Clip] = None
        self._hold_quiet: Optional[Callable[[], bool]] = None
        self._hold_then: Optional[Callable[[], None]] = None

    # ── what it is doing ────────────────────────────────────────────────

    @property
    def visible(self) -> bool:
        return self.phase != HIDDEN

    def set_reduced(self, reduced: bool) -> None:
        self.reduced = bool(reduced)

    def look(self) -> CursorLook:
        cue = self.cue.progress if self.cue is not None else None
        return CursorLook(self.spot.value, max(0.0, min(1.0, self.alpha.value)), cue)

    def busy(self) -> bool:
        """Does anything need another frame? Never true at rest."""
        if self.phase in (TRAVELLING, LEAVING, FADING_OUT, FADING_IN):
            return True
        if not (self.spot.at_rest() and self.alpha.at_rest()):
            return True
        if self.cue is not None:
            return True
        # Like the orb's hold: frames only while the minimum is counting. After
        # that it ends on a state change, which wakes the clock by itself.
        return self._hold is not None and self._hold.elapsed < self._hold.duration

    # ── what it is told ─────────────────────────────────────────────────

    def travel(self, point: Point, on_arrive: Optional[Callable[[], None]] = None) -> None:
        """
        Go to a point. From wherever it is if it is showing; from `origin` —
        where Diablo is — if it is not, so it visibly comes from him.
        Whatever it was on its way to before is abandoned, arrival and all.
        """
        self._goal = (float(point[0]), float(point[1]))
        self._on_arrive = on_arrive
        self._hold = None
        self.cue = None
        if self.reduced:
            # The same information without the travel: fade out where it is,
            # appear where it is going, fade in.
            if self.phase == HIDDEN:
                self.spot.jump(self._goal)
                self.alpha.jump(0.0)
                self.alpha.to(1.0, motion.Timing.REDUCED_FADE)
                self.phase = FADING_IN
            else:
                self.alpha.to(0.0, motion.Timing.REDUCED_FADE)
                self.phase = FADING_OUT
            return
        if self.phase == HIDDEN:
            self.spot.jump(self.origin)
            self.alpha.jump(0.0)
        self.alpha.to(1.0, motion.Timing.LOOK)
        here = self.spot.value
        distance = math.hypot(self._goal[0] - here[0], self._goal[1] - here[1])
        self._trip = distance
        self.spot.to(self._goal, motion.move_duration(distance))
        self.phase = TRAVELLING

    def hide(self) -> None:
        """Fade out where it is. Anything it was on its way to is abandoned."""
        self._on_arrive = None
        self._hold = None
        self.cue = None
        if self.phase == HIDDEN:
            return
        duration = (motion.Timing.REDUCED_FADE if self.reduced
                    else motion.Timing.PANEL_CLOSE)
        self.alpha.to(0.0, duration)
        self.phase = LEAVING

    def hold(self, seconds: float, quiet: Callable[[], bool],
             then: Callable[[], None]) -> None:
        """
        Stay at least `seconds`, and for as long as Diablo is still talking
        about it; then `then`. The same rule the orb's hold follows.
        """
        self._hold = motion.Clip(max(seconds, 1e-3))
        self._hold_quiet = quiet
        self._hold_then = then

    # ── the frame ───────────────────────────────────────────────────────

    def _near(self) -> bool:
        """
        Arrived, to the eye. The orb's rule: within 1% of the trip or two
        points, whichever is larger. Waiting for the spring's last quarter of
        a point made every arrival — and so every sentence that waits on it —
        about half a second late for nothing anyone could see.
        """
        if self._goal is None:
            return True
        x, y = self.spot.value
        left = math.hypot(self._goal[0] - x, self._goal[1] - y)
        return left <= max(motion.ARRIVE_NEAR, motion.ARRIVE_FRACTION * self._trip)

    def step(self, dt: float) -> None:
        dt = min(max(dt, 0.0), motion.MAX_STEP)
        self.alpha.step(dt)
        if self.phase in (TRAVELLING, RESTING, LEAVING):
            # Keeps easing onto the exact spot after arrival is declared.
            self.spot.step(dt)
        if self.phase == TRAVELLING:
            if self._near():
                self._arrive()
        elif self.phase == FADING_OUT:
            if self.alpha.at_rest():
                self.spot.jump(self._goal)
                self.alpha.to(1.0, motion.Timing.REDUCED_FADE)
                self.phase = FADING_IN
        elif self.phase == FADING_IN:
            if self.alpha.at_rest():
                self._arrive()
        elif self.phase == LEAVING:
            if self.alpha.at_rest():
                self.phase = HIDDEN
                self.spot.jump(self.spot.value)
                self._goal = None
        if self.cue is not None and self.cue.step(dt):
            self.cue = None
        self._check_hold(dt)

    def _arrive(self) -> None:
        self.phase = RESTING
        if self.reduced and self._goal is not None:
            self.spot.jump(self._goal)
        if not self.reduced:
            # One small acknowledgement, once. Never a loop.
            self.cue = motion.Clip(motion.Timing.SETTLE)
        callback, self._on_arrive = self._on_arrive, None
        if callback is not None:
            callback()

    def _check_hold(self, dt: float) -> None:
        if self._hold is None or self.phase != RESTING:
            return
        if self._hold.elapsed < self._hold.duration:
            self._hold.step(dt)
            return
        if self._hold_quiet is not None and not self._hold_quiet():
            return
        then, self._hold = self._hold_then, None
        if then is not None:
            then()
