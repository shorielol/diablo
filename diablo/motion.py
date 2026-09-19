"""
D.I.A.B.L.O. — the one motion system.

Every moving thing Diablo draws is driven from here: where the orb is, how big
it is, which state ring is showing and how strongly, one-shot cues like
"done" and "that failed", and the panel opening out of the orb. The AppKit
shell only reads values out of this module once per display frame and puts
them on screen. Nothing in here imports AppKit, so all of it is testable
without a window.

Three rules the whole module is built on:

  Time, not frames. Every channel advances by measured elapsed time, with an
  exact solution rather than a per-frame nudge, so the same motion looks the
  same at 60 Hz, at 120 Hz, and after a dropped frame.

  Events, not timers. A state change starts because something real happened
  — the key went down, audio started, a job finished — and a motion ends
  when it actually arrives. The one deliberate hold (standing next to a
  target for as long as the overlay shows) is measured on the same clock and
  owned like any other motion.

  Ownership. Every motion that can finish carries a token. Starting another
  motion replaces the token, and a completion that arrives holding an old
  one is dropped. An old arrival cannot move him, reopen a panel, or restore
  a state that has already been left.

The springs are critically damped: they approach without overshooting, and a
retarget keeps the current velocity so a change of mind curves instead of
stopping dead.
"""

import math
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

Point = Tuple[float, float]


# ─── Tokens ──────────────────────────────────────────────────────────────────
#
# Every duration in the system. A duration is the time a spring takes to get
# within one per cent of where it is going, which reads as the length of the
# motion. Nothing else in the codebase may invent its own.

class Timing:
    RESPOND = 0.14        # acknowledging the key: fast enough to feel instant
    LOOK = 0.20           # one state ring handing over to the next
    SCALE = 0.26          # growing or relaxing
    SETTLE = 0.30         # the small "I have arrived" at a target
    # Measured in the walkthrough: at 0.73 s for 1,100 points the peak was
    # 5,400 pt/s, which crossed most of the screen in three frames of a 10 fps
    # strip — too quick to follow. These give ~2,900 pt/s for the same trip.
    MOVE_MIN = 0.45       # a short hop
    MOVE_MAX = 1.10       # across a large display
    MOVE_PER_POINT = 0.00045
    PANEL_OPEN = 0.26
    PANEL_CLOSE = 0.18    # leaving is faster than arriving
    SUCCESS = 0.60
    FAILURE = 0.46
    INTERRUPTED = 0.30
    REDUCED_FADE = 0.16   # reduced motion: fade out, move, fade in


class Scale:
    """How large he is drawn, as a multiple of the resting size."""
    REST = 1.0
    ACTIVE = 1.06         # listening or speaking: a little forward
    PRESS = 1.10          # the instant the key goes down
    TRAVEL = 0.94         # tucked while he travels
    ARRIVE = 0.94         # the settle starts from here
    REDUCED_LIMIT = 0.02  # reduced motion keeps scale within 2 per cent


# How close counts as there. Below both, a spring snaps to its target and
# reports arrival exactly once.
ARRIVE_DISTANCE = 0.25    # points
ARRIVE_SPEED = 4.0        # points per second
# He has visibly arrived well before the spring's tail has closed the last
# fraction of a point, and waiting for the tail cost 0.7 s of apparent
# nothing on a long trip. So arrival is declared within 1% of the trip (or
# two points), and the position keeps easing onto the exact spot while the
# settle plays over it.
ARRIVE_FRACTION = 0.01
ARRIVE_NEAR = 2.0
SCALAR_DISTANCE = 0.002
SCALAR_SPEED = 0.02
# Within 1% of the distance at the stated duration.
_SETTLE_CONSTANT = 6.64

# The fastest he ever travels. A retarget keeps its speed and stiffens for
# the shorter remaining distance, which measured 7,600 pt/s in the
# walkthrough: fast enough to lose track of him. Capping speed only ever
# makes a trip longer; it cannot add overshoot.
MAX_SPEED = 3000.0        # points per second

# A frame longer than this is a stall (the lid was shut, the process was
# stopped), not motion, and is not allowed to fling anything.
MAX_STEP = 0.1


def move_duration(distance: float) -> float:
    """Longer trips take longer, but never so long that he is hard to follow."""
    return min(Timing.MOVE_MAX,
               max(Timing.MOVE_MIN, Timing.MOVE_MIN + distance * Timing.MOVE_PER_POINT))


def _omega(duration: float) -> float:
    return _SETTLE_CONSTANT / max(duration, 1e-3)


def _critical(offset: float, velocity: float, omega: float, dt: float) -> Tuple[float, float]:
    """
    The exact critically damped step. `offset` is the distance from the
    target. Exact, so the result does not depend on how elapsed time was
    sliced into frames.
    """
    decay = math.exp(-omega * dt)
    carry = velocity + omega * offset
    return (offset + carry * dt) * decay, (velocity - omega * carry * dt) * decay


def _no_overshoot(spring) -> None:
    """
    A critically damped spring can still overshoot if it is already moving
    toward its target faster than it would choose to. That only happens after
    a retarget to somewhere nearer, and it is exactly the case where an
    overshoot looks like a mistake, so the approach speed is capped.
    """
    offset = spring.value - spring.target
    if offset * spring.velocity < 0 and abs(spring.velocity) > spring.omega * abs(offset):
        spring.velocity = -spring.omega * offset


class Spring:
    """One number, heading somewhere, at a speed set by a duration."""

    def __init__(self, value: float = 0.0, duration: float = Timing.LOOK):
        self.value = float(value)
        self.target = float(value)
        self.velocity = 0.0
        self.omega = _omega(duration)

    def to(self, target: float, duration: Optional[float] = None) -> None:
        self.target = float(target)
        if duration is not None:
            self.omega = _omega(duration)
        _no_overshoot(self)

    def jump(self, value: float) -> None:
        self.value = self.target = float(value)
        self.velocity = 0.0

    def step(self, dt: float) -> bool:
        """Advance. True on the frame it arrives."""
        if self.at_rest():
            return False
        offset, self.velocity = _critical(self.value - self.target, self.velocity,
                                          self.omega, dt)
        self.value = self.target + offset
        if abs(offset) < SCALAR_DISTANCE and abs(self.velocity) < SCALAR_SPEED:
            self.jump(self.target)
            return True
        return False

    def at_rest(self) -> bool:
        return self.value == self.target and self.velocity == 0.0


class Spring2D:
    """A point on the screen, in AppKit points: two springs on one clock."""

    def __init__(self, point: Point):
        self.x = Spring(point[0])
        self.y = Spring(point[1])

    @property
    def value(self) -> Point:
        return (self.x.value, self.y.value)

    @property
    def target(self) -> Point:
        return (self.x.target, self.y.target)

    @property
    def speed(self) -> float:
        return math.hypot(self.x.velocity, self.y.velocity)

    def to(self, point: Point, duration: float) -> None:
        # The velocity is kept: that is what makes a retarget curve rather
        # than stop and start again.
        self.x.to(point[0], duration)
        self.y.to(point[1], duration)

    def jump(self, point: Point) -> None:
        self.x.jump(point[0])
        self.y.jump(point[1])

    def step(self, dt: float) -> bool:
        if self.at_rest():
            return False
        for axis in (self.x, self.y):
            offset, axis.velocity = _critical(axis.value - axis.target, axis.velocity,
                                              axis.omega, dt)
            axis.value = axis.target + offset
        speed = self.speed
        if speed > MAX_SPEED:
            # Pull the step back to what the capped speed covers this frame.
            ratio = MAX_SPEED / speed
            for axis in (self.x, self.y):
                back = axis.velocity * (1.0 - ratio) * dt
                axis.value -= back
                axis.velocity *= ratio
        distance = math.hypot(self.x.value - self.x.target, self.y.value - self.y.target)
        if distance < ARRIVE_DISTANCE and self.speed < ARRIVE_SPEED:
            self.jump(self.target)
            return True
        return False

    def at_rest(self) -> bool:
        return self.x.at_rest() and self.y.at_rest()


class Clip:
    """
    A one-shot that runs 0 → 1 over a fixed time and then reports that it has
    finished. Used for cues that play once — "done", "that failed" — and for
    the deliberate hold beside a target.
    """

    def __init__(self, duration: float):
        self.duration = max(duration, 1e-3)
        self.elapsed = 0.0

    @property
    def progress(self) -> float:
        return min(1.0, self.elapsed / self.duration)

    def step(self, dt: float) -> bool:
        if self.elapsed >= self.duration:
            return False
        self.elapsed += dt
        return self.elapsed >= self.duration


# ─── Visual states ───────────────────────────────────────────────────────────

class Visual(str, Enum):
    """What he looks like he is doing. Derived from runtime facts, never set."""
    IDLE = "idle"
    ACTIVATING = "activating"
    LISTENING = "listening"
    THINKING = "thinking"
    READING = "reading"
    MOVING = "moving"
    TARGETING = "targeting"
    GUIDING = "guiding"
    WORKING = "working"
    WAITING = "waiting"
    SPEAKING = "speaking"
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    RETURNING = "returning"


# The manager's states and what each looks like. Anything not listed (cold,
# init, shutdown) is idle, drawn dimmed. One state machine, mapped; not two.
RUNTIME = {
    "listening": Visual.LISTENING,
    "processing": Visual.THINKING,
    "preparing": Visual.THINKING,
    "responding": Visual.SPEAKING,
    "error": Visual.ERROR,
}
DIM_RUNTIME = ("cold", "init", "shutdown")

# States that need the frame clock for as long as they last. Everything else
# only needs it while a transition is still settling.
LOOPING = {Visual.LISTENING, Visual.THINKING, Visual.READING, Visual.SPEAKING,
           Visual.WAITING, Visual.WORKING}

# Which ring each state wears. The drawing code knows how to draw each ring;
# this table is the only place that decides which one a state gets.
RING = {
    Visual.IDLE: None,
    Visual.ACTIVATING: "listen",
    Visual.LISTENING: "listen",
    Visual.THINKING: "think",
    Visual.READING: "read",
    Visual.MOVING: "travel",
    Visual.TARGETING: "guide",
    Visual.GUIDING: "guide",
    Visual.WORKING: "work",
    Visual.WAITING: "wait",
    Visual.SPEAKING: "speak",
    Visual.SUCCESS: None,
    Visual.ERROR: "error",
    Visual.INTERRUPTED: None,
    Visual.RETURNING: "travel",
}
RINGS = ("listen", "think", "read", "travel", "guide", "work", "wait", "speak", "error")

# Spatial phases: the motion system's own, not the runtime's.
HOME, MOVING, SETTLING, HOLDING, RETURNING = (
    "home", "moving", "settling", "holding", "returning")


@dataclass
class Look:
    """Everything the drawing code needs for one frame."""
    visual: Visual
    position: Point           # the orb's centre, AppKit points
    scale: float
    rings: Dict[str, float]   # ring name -> opacity, only the visible ones
    time: float               # seconds this visual has lasted, for loops
    face_dx: float            # horizontal shift of the face inside the disc
    cue: Optional[str]        # "success", "failure", "interrupted" or None
    cue_progress: float
    alpha: float              # the whole orb; only reduced-motion moves fade it
    dim: bool
    panel: float              # 0 closed … 1 open
    still: bool               # preparing: the thinking ring, not sweeping


class Presence:
    """
    Diablo, as far as motion is concerned.

    Fed real events, asked for a Look each frame. Not thread-safe by design:
    the shell only calls it on the main thread.
    """

    def __init__(self, home: Point = (0.0, 0.0), reduced: bool = False):
        self.reduced = bool(reduced)
        self.home = home
        self.position = Spring2D(home)
        self.scale = Spring(Scale.REST, Timing.SCALE)
        self.rings = {name: Spring(0.0, Timing.LOOK) for name in RINGS}
        self.alpha = Spring(1.0, Timing.REDUCED_FADE)
        self.panel = Spring(0.0, Timing.PANEL_OPEN)

        # Runtime facts, as reported by the app. Nothing here is guessed.
        self.runtime = "idle"
        self.key_down = False
        self.reading = 0
        self.waiting = False
        self.jobs = 0

        self.phase = HOME
        self.guiding = False       # holding because an approach settled, not merely stopped
        self.cue: Optional[str] = None
        self._cue_clip: Optional[Clip] = None
        self._hold: Optional[Clip] = None
        self._teleport: Optional[Point] = None
        self._trip = 0.0            # length of the current trip, for arrival
        self._landed = 0            # the owner whose arrival has been reported

        # Ownership: one token for where he is going, one for the panel.
        self._serial = 0
        self.motion_owner = 0
        self.panel_owner = 0
        self._on_arrive: Dict[int, Callable[[], None]] = {}
        self._on_ready: Dict[int, Callable[[], None]] = {}
        self._on_hold_end: Dict[int, Callable[[], None]] = {}
        self._on_panel: Dict[int, Callable[[bool], None]] = {}

        self.visual = Visual.IDLE
        self._since = 0.0
        self._clock = 0.0
        self.log: List[Tuple[float, str]] = []     # (time, change), for tests and traces
        self._apply()

    def _token(self) -> int:
        self._serial += 1
        return self._serial

    # ── runtime facts ────────────────────────────────────────────────────
    def set_runtime(self, state: str) -> None:
        """The manager's state changed."""
        self.runtime = state or "idle"
        if self.runtime == "listening":
            self.key_down = False          # the key has done its job
        self._apply()

    def key_pressed(self) -> None:
        """The push-to-talk key actually went down."""
        self.key_down = True
        self._apply()

    def key_released(self) -> None:
        self.key_down = False
        self._apply()

    def set_reading(self, depth: int) -> None:
        self.reading = max(0, int(depth))
        self._apply()

    def set_waiting(self, waiting: bool) -> None:
        self.waiting = bool(waiting)
        self._apply()

    def set_jobs(self, running: int) -> None:
        self.jobs = max(0, int(running))
        self._apply()

    def flash(self, cue: str) -> None:
        """A one-shot: 'success', 'failure' or 'interrupted'."""
        durations = {"success": Timing.SUCCESS, "failure": Timing.FAILURE,
                     "interrupted": Timing.INTERRUPTED}
        if cue not in durations:
            return
        self.cue = cue
        self._cue_clip = Clip(durations[cue])
        self._apply()

    def set_reduced(self, reduced: bool) -> None:
        self.reduced = bool(reduced)
        self._apply()

    # ── spatial primitives ───────────────────────────────────────────────
    def set_home(self, point: Point) -> None:
        """Where he rests. Moves him there directly if he is resting now."""
        self.home = point
        if self.phase == HOME:
            self.position.jump(point)

    def move_to(self, point: Point, on_arrive: Optional[Callable[[], None]] = None,
                phase: str = MOVING) -> int:
        """Travel to a point, from wherever he is, at whatever speed he has."""
        token = self._token()
        self.motion_owner = token
        self._hold = None
        self.guiding = False
        if on_arrive is not None:
            self._on_arrive[token] = on_arrive
        self.phase = phase
        if self.reduced:
            # No travel: fade out where he is, appear where he is going.
            self._teleport = point
            self.alpha.to(0.0, Timing.REDUCED_FADE)
        else:
            here = self.position.value
            distance = math.hypot(point[0] - here[0], point[1] - here[1])
            self._trip = distance
            self.position.to(point, move_duration(distance))
        self._apply()
        return token

    def retarget(self, point: Point, on_arrive: Optional[Callable[[], None]] = None) -> int:
        """A change of mind mid-flight. Keeps his speed; the old arrival is void."""
        return self.move_to(point, on_arrive=on_arrive,
                            phase=RETURNING if self.phase == RETURNING else MOVING)

    def approach_target(self, stand: Point,
                        on_ready: Optional[Callable[[], None]] = None) -> int:
        """
        Go and stand beside something. `stand` comes from the guidance
        geometry, so the target itself is never covered. Travel, settle, then
        report ready.
        """
        token = self.move_to(stand, phase=MOVING)
        self._on_ready[token] = on_ready or (lambda: None)
        return token

    def settle_near_target(self) -> None:
        """The arrival settle, where he is now."""
        self.phase = SETTLING
        if not self.reduced:
            self.scale.jump(Scale.ARRIVE)
        self._apply()
        if self.reduced:
            # Nothing to settle; ready at once.
            self.phase = HOLDING
            self.guiding = True
            self._apply()
            self._fire(self._on_ready, self.motion_owner)

    def hold_position(self, seconds: Optional[float] = None,
                      then: Optional[Callable[[], None]] = None) -> int:
        """
        Stay put. With `seconds`, a deliberate hold that ends by itself —
        measured on the frame clock and owned, so a newer motion cancels it.
        """
        token = self._token()
        self.motion_owner = token
        self.phase = HOLDING
        self._hold = Clip(seconds) if seconds else None
        if then is not None and seconds:
            self._on_hold_end[token] = then
        self._apply()
        return token

    def return_home(self) -> int:
        if self.phase == HOME:
            return self.motion_owner
        return self.move_to(self.home, phase=RETURNING)

    def interrupt_motion(self) -> None:
        """
        Stop, smoothly. He coasts to where his current speed carries him in
        about one settle time instead of freezing mid-stride, and every
        pending arrival is voided.
        """
        self.motion_owner = self._token()
        self._hold = None
        self.guiding = False
        if self._teleport is not None:
            self._teleport = None
            self.alpha.to(1.0, Timing.REDUCED_FADE)
        here = self.position.value
        coast = 1.0 / max(self.position.x.omega, 1e-3)
        stop = (here[0] + self.position.x.velocity * coast,
                here[1] + self.position.y.velocity * coast)
        self.position.to(stop, Timing.SETTLE)
        if self.phase != HOME:
            self.phase = HOLDING
        self._apply()

    # ── the panel ────────────────────────────────────────────────────────
    def open_panel(self, on_done: Optional[Callable[[bool], None]] = None) -> int:
        return self._panel_to(1.0, Timing.PANEL_OPEN, on_done)

    def close_panel(self, on_done: Optional[Callable[[bool], None]] = None) -> int:
        return self._panel_to(0.0, Timing.PANEL_CLOSE, on_done)

    def _panel_to(self, target, duration, on_done) -> int:
        token = self._token()
        self.panel_owner = token
        if on_done is not None:
            self._on_panel[token] = on_done
        self.panel.to(target, Timing.REDUCED_FADE if self.reduced else duration)
        return token

    # ── deriving the look ────────────────────────────────────────────────
    def derive(self) -> Visual:
        """One visual state from all the facts. The order is the priority."""
        runtime = RUNTIME.get(self.runtime)
        if runtime is Visual.ERROR:
            return Visual.ERROR
        # The key outranks every cue: pressing it to cut him off must be
        # acknowledged at once. The cue still plays, drawn over the top.
        if self.key_down and runtime is not Visual.LISTENING:
            return Visual.ACTIVATING
        if runtime is Visual.LISTENING:
            return Visual.LISTENING
        if self.cue == "interrupted":
            return Visual.INTERRUPTED
        if self.cue == "failure":
            return Visual.ERROR
        if runtime is Visual.SPEAKING:
            return Visual.SPEAKING
        if self.phase == MOVING:
            return Visual.MOVING
        if self.phase == SETTLING:
            return Visual.TARGETING
        if self.phase == RETURNING:
            return Visual.RETURNING
        # Standing beside something he found outranks the working rings: the
        # point of being there is to show it, and thinking carries on
        # underneath. The voice states above still win.
        if self.phase == HOLDING and self.guiding:
            return Visual.GUIDING
        if self.reading:
            return Visual.READING
        if runtime is Visual.THINKING:
            return Visual.THINKING
        if self.waiting:
            return Visual.WAITING
        if self.cue == "success":
            return Visual.SUCCESS
        if self.jobs:
            return Visual.WORKING
        return Visual.IDLE

    def _apply(self) -> None:
        """Point every channel at where the current state wants it."""
        visual = self.derive()
        if visual is not self.visual:
            self.log.append((self._clock, f"{self.visual.value}->{visual.value}"))
            self.visual = visual
            self._since = 0.0

        ring = RING.get(visual)
        for name, spring in self.rings.items():
            spring.to(1.0 if name == ring else 0.0,
                      Timing.RESPOND if visual is Visual.ACTIVATING else Timing.LOOK)

        if self.phase == SETTLING:
            target, duration = Scale.REST, Timing.SETTLE
        elif visual is Visual.ACTIVATING:
            target, duration = Scale.PRESS, Timing.RESPOND
        elif visual in (Visual.LISTENING, Visual.SPEAKING):
            target, duration = Scale.ACTIVE, Timing.SCALE
        elif visual in (Visual.MOVING, Visual.RETURNING):
            target, duration = Scale.TRAVEL, Timing.SCALE
        else:
            target, duration = Scale.REST, Timing.SCALE
        if self.reduced:
            target = max(Scale.REST - Scale.REDUCED_LIMIT,
                         min(Scale.REST + Scale.REDUCED_LIMIT, target))
        self.scale.to(target, duration)

    # ── the clock ────────────────────────────────────────────────────────
    def step(self, dt: float) -> None:
        """Advance everything by `dt` seconds of real time; fire what finished."""
        dt = max(0.0, min(float(dt), MAX_STEP))
        self._clock += dt
        self._since += dt
        owner = self.motion_owner

        if self._teleport is not None:
            # Reduced motion: fade out, jump, fade in. Arrival is the jump.
            if self.alpha.step(dt) and self.alpha.target == 0.0:
                self.position.jump(self._teleport)
                self._teleport = None
                self.alpha.to(1.0, Timing.REDUCED_FADE)
                self._arrived(owner)
        else:
            self.alpha.step(dt)
            exact = self.position.step(dt)
            if exact or self._near():
                self._arrived(owner)

        if self.scale.step(dt) and self.phase == SETTLING and owner == self.motion_owner:
            self.phase = HOLDING
            self.guiding = True
            self._apply()
            self._fire(self._on_ready, owner)

        for spring in self.rings.values():
            spring.step(dt)

        if self._hold is not None and self._hold.step(dt):
            self._hold = None
            self._fire(self._on_hold_end, self.motion_owner)

        if self._cue_clip is not None and self._cue_clip.step(dt):
            self._cue_clip = None
            self.cue = None
            self._apply()

        if self.panel.step(dt):
            self._fire(self._on_panel, self.panel_owner, self.panel.value >= 0.5)

    def _near(self) -> bool:
        if self.phase not in (MOVING, RETURNING):
            return False
        here, there = self.position.value, self.position.target
        remaining = math.hypot(there[0] - here[0], there[1] - here[1])
        return remaining <= max(ARRIVE_NEAR, ARRIVE_FRACTION * self._trip)

    def _arrived(self, owner: int) -> None:
        if owner != self.motion_owner or owner == self._landed:
            return
        self._landed = owner
        if self.phase == RETURNING:
            self.phase = HOME
            self._apply()
        elif self.phase == MOVING and owner in self._on_ready:
            self.settle_near_target()
        elif self.phase == MOVING:
            self.phase = HOLDING
            self._apply()
        self._fire(self._on_arrive, owner)

    def _fire(self, table: Dict[int, Callable], owner: int, *args) -> None:
        """Run a completion only if it belongs to the current owner."""
        callback = table.pop(owner, None)
        for stale in [k for k in table if k < owner]:
            table.pop(stale, None)          # older than the owner: can never fire
        if callback is not None:
            callback(*args)

    # ── what the shell reads ─────────────────────────────────────────────
    def busy(self) -> bool:
        """Does anything need another frame?"""
        if self.looping():
            return True
        if not (self.position.at_rest() and self.scale.at_rest()
                and self.alpha.at_rest() and self.panel.at_rest()):
            return True
        if any(not s.at_rest() for s in self.rings.values()):
            return True
        return self._hold is not None or self._cue_clip is not None

    def looping(self) -> bool:
        """A state with a continuous loop. Preparing holds still on purpose."""
        return self.visual in LOOPING and not (
            self.visual is Visual.THINKING and self.runtime == "preparing")

    def look(self) -> Look:
        t = self._since
        dx = 0.0
        cue_progress = self._cue_clip.progress if self._cue_clip else 0.0
        if not self.reduced:
            if self.visual is Visual.THINKING and self.runtime != "preparing":
                dx = 0.8 * math.sin(2 * math.pi * t / 2.2)
            elif self.visual is Visual.READING:
                dx = 1.6 * math.sin(2 * math.pi * t / 1.4)
            elif self.cue == "failure":
                dx = 2.0 * math.sin(2 * math.pi * 3.0 * cue_progress) * (1 - cue_progress)
        return Look(
            visual=self.visual, position=self.position.value, scale=self.scale.value,
            rings={k: s.value for k, s in self.rings.items() if s.value > 0.01},
            time=t, face_dx=dx, cue=self.cue, cue_progress=cue_progress,
            alpha=self.alpha.value, dim=self.runtime in DIM_RUNTIME,
            panel=self.panel.value, still=self.runtime == "preparing")


# ─── Where to stand ──────────────────────────────────────────────────────────

def stand_for(target_x: float, target_y: float, label: str = "",
              cursor: Optional[Point] = None) -> Optional[Point]:
    """
    Where the orb's centre goes to refer to a target, in AppKit points.

    The target is in Quartz space, as everything that finds things on screen
    reports it. The choice of side is the guidance geometry's, unchanged —
    the same clearance that keeps the overlay's marker off the target keeps
    the orb off it — and the conversion is display.py's, so there is still
    one coordinate system and one flip. None when the target is on no display.
    """
    from skills import display, guidance
    placement = guidance.place(target_x, target_y, label, cursor=cursor)
    if placement is None:
        return None
    return display.quartz_to_appkit(placement.marker_x, placement.marker_y)
