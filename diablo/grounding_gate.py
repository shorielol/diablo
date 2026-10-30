"""
D.I.A.B.L.O. — the boundary between a model's point and the mouse.

A grounding model answers "where is the Save button?" with a coordinate, and a
coordinate is the most dangerous thing a model can hand Diablo: it carries no
account of what it claims to be, so whatever label travels with it is the
model's word, not the screen's. Put "Save" on a point that lands on "Delete
All" and every later safety check that reads the label is checking the wrong
thing.

So a model never produces a target. It produces a GroundingProposal: a point,
in a coordinate space it has to name, against the one capture it was read
from. This module turns a proposal into a target by asking the application
what is actually there, and refuses whenever the answer does not settle it:

    proposal ─► coordinate space ─► observation still current?
             ─► point inside the window it was asked about?
             ─► what macOS says is frontmost under the point (hit test)
             ─► same control the observation saw there?
             ─► actionable role, compatible with what was asked for?
             ─► its own label names the target?           (never the model's)
             ─► enabled?
             ─► the only control with that name?
             ─► ValidatedTarget, or one REJECT_* with a reason

A ValidatedTarget is the only thing computer_use.click() accepts, and it is
issued only here. `revalidate()` repeats the hit test immediately before the
click, because a target that was right a second ago is not proof of anything
now.

Nothing in this module can press anything. It imports no event API and no
action layer; tests/test_grounding_gate.py checks that it stays that way.
"""

import hashlib
import hmac
import math
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import ax  # noqa: E402
from skills import config  # noqa: E402

# An observation older than this cannot vouch for a point, however little has
# visibly changed. It is a ceiling, not the test: the hit test below is what
# decides whether the screen still says what the observation said.
MAX_OBSERVATION_AGE = config.env_float("DIABLO_PROPOSAL_MAX_AGE", 10.0, low=0.5, high=60.0)

# Two rectangles within this many points are the same place. Applications
# report positions in whole or half points and windows settle by a point or so.
SLACK = 2.0


class Space(str, Enum):
    """Where a proposal's x and y are measured. There is no default."""

    FRAME_PIXEL = "frame_pixel"            # a pixel of this frame's image
    FRAME_NORMALISED = "frame_normalised"  # 0..1 across this frame
    DISPLAY_PIXEL = "display_pixel"        # a backing pixel of one display
    QUARTZ_POINT = "quartz_point"          # Diablo's desktop space, top-left origin


# The verdicts. VALIDATED is the only one a click can follow without asking.
VALIDATED = "VALIDATED"
CONFIRM_REQUIRED = "CONFIRM_REQUIRED"       # a real, enabled control with no name
REJECT_NO_TARGET = "REJECT_NO_TARGET"
REJECT_DISABLED = "REJECT_DISABLED"
REJECT_WRONG_WINDOW = "REJECT_WRONG_WINDOW"
REJECT_AMBIGUOUS = "REJECT_AMBIGUOUS"
REJECT_STALE_FRAME = "REJECT_STALE_FRAME"
REJECT_COORDINATE_SPACE = "REJECT_COORDINATE_SPACE"
REJECT_OFF_DISPLAY = "REJECT_OFF_DISPLAY"
REJECT_ROLE = "REJECT_ROLE"
REJECT_LABEL_MISMATCH = "REJECT_LABEL_MISMATCH"
REJECT_UNVERIFIABLE = "REJECT_UNVERIFIABLE"   # accessibility could not be asked


@dataclass(frozen=True)
class GroundingProposal:
    """
    A model's answer to "where is it?", and nothing more.

    `target` is what was asked for, in the words of the user or the planner.
    `claimed_label` is what the model called the thing it pointed at. It is
    kept for the record and read by nothing: the label that counts is the one
    the application publishes for the control under the point.
    """

    frame_id: str
    space: Space
    x: float
    y: float
    target: str
    source_model: str
    proposed_at: float = field(default_factory=time.time)
    display_id: int = 0                      # required for DISPLAY_PIXEL
    confidence: Optional[float] = None
    bbox: Optional[Tuple[float, float, float, float]] = None
    claimed_label: str = ""
    reasoning: str = ""


@dataclass
class Observation:
    """
    One look at one window: the capture, and the controls the application
    published at the same moment.

    The published controls are what makes "the screen changed" detectable at
    the level that matters. A fingerprint notices pixels; this notices that the
    button under the point used to be called Save.
    """

    frame: Any                                   # screen.ScreenFrame
    window: Dict[str, Any]
    elements: List[ax.Element] = field(default_factory=list)

    @property
    def frame_id(self) -> str:
        return getattr(self.frame, "frame_id", "")


def observe(window: Dict[str, Any]) -> Observation:
    """Capture a window and read its controls together. The caller releases the frame."""
    from skills import screen as _screen
    frame = _screen.capture_this_window(window)
    elements = ax.elements_for_window(window) if frame.ok else []
    return Observation(frame=frame, window=dict(window), elements=elements)


# A key that exists only in this process and only in this module. Every
# ValidatedTarget carries a seal over the fields the gate vouched for, so a
# copy with one field changed — dataclasses.replace(target, label="Save") on
# a target that is really "Delete All" — is no longer an issued target.
_KEY = os.urandom(32)


def _seal_of(fields: Tuple[Any, ...]) -> str:
    return hmac.new(_KEY, repr(fields).encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ValidatedTarget:
    """
    A control the application vouched for, at the moment it was checked.

    Everything here comes from accessibility, never from the proposal: the
    label is the control's own, the point is the centre of the control's own
    rectangle rather than wherever the model happened to aim inside it.
    """

    role: str
    label: str
    x: float
    y: float
    width: float
    height: float
    pid: int
    window_id: int
    window_bounds: Tuple[float, float, float, float]
    frame_id: str
    validated_at: float
    source_model: str
    named: bool = True
    seal: str = field(default="", repr=False, compare=False)

    def vouched(self) -> Tuple[Any, ...]:
        """The fields the seal covers: everything that decides a click."""
        return (self.role, self.label, self.x, self.y, self.width, self.height, self.pid,
                self.window_id, self.window_bounds, self.frame_id, self.validated_at,
                self.source_model, self.named)

    @property
    def centre(self) -> Tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    def describe(self) -> str:
        kind = self.role.replace("AX", "").lower() or "control"
        return f"{kind} “{self.label}”" if self.label else f"unlabelled {kind}"


def issued(target: Any) -> bool:
    """Whether this target came out of validate(), unaltered. computer_use.click() asks."""
    return (isinstance(target, ValidatedTarget) and bool(target.seal)
            and hmac.compare_digest(target.seal, _seal_of(target.vouched())))


def _issue(**fields: Any) -> ValidatedTarget:
    unsealed = ValidatedTarget(**fields)
    return ValidatedTarget(**fields, seal=_seal_of(unsealed.vouched()))


@dataclass
class Validation:
    """One verdict, the reason for it, and what was actually found."""

    verdict: str
    reason: str
    point: Optional[Tuple[float, float]] = None      # the proposal, in Quartz
    target: Optional[ValidatedTarget] = None
    found: Optional[ax.Element] = None               # what is really there
    detail: str = ""                                 # finer reason within a verdict

    @property
    def ok(self) -> bool:
        return self.verdict == VALIDATED

    def as_record(self) -> Dict[str, Any]:
        """For the trace: the verdict and the control's role, never screen text."""
        return {"verdict": self.verdict, "detail": self.detail,
                "role": self.found.role if self.found else ""}


# ─── Names ───────────────────────────────────────────────────────────────────

# Words that say what kind of control was asked for, not which one.
ROLE_WORDS: Dict[str, frozenset] = {
    "button": frozenset({"AXButton", "AXMenuButton", "AXPopUpButton", "AXToolbarButton",
                         "AXDisclosureTriangle"}),
    "checkbox": frozenset({"AXCheckBox"}),
    "check": frozenset({"AXCheckBox"}),
    "switch": frozenset({"AXCheckBox"}),
    "toggle": frozenset({"AXCheckBox", "AXButton"}),
    "radio": frozenset({"AXRadioButton"}),
    "field": frozenset({"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}),
    "textbox": frozenset({"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}),
    "tab": frozenset({"AXTab", "AXRadioButton"}),
    "link": frozenset({"AXLink"}),
    "slider": frozenset({"AXSlider"}),
    "dropdown": frozenset({"AXPopUpButton", "AXComboBox", "AXMenuButton"}),
    "popup": frozenset({"AXPopUpButton", "AXComboBox", "AXMenuButton"}),
    "menu": frozenset({"AXMenuItem", "AXMenuBarItem", "AXMenuButton", "AXPopUpButton"}),
}
_FILLER = {"the", "a", "an", "this", "that", "on", "in", "click", "press", "tap",
           "box", "item", "control", "element", "icon", "option", "labelled",
           "labeled", "called", "named"}

_PLAIN = {**dict.fromkeys(map(ord, "‐‑‒–—−"), "-"), ord("‘"): "'", ord("’"): "'",
          ord("…"): " "}


def _words(text: str) -> List[str]:
    text = unicodedata.normalize("NFKC", text or "").translate(_PLAIN).lower()
    return re.findall(r"[a-z0-9]+", text)


def wanted_name(target: str) -> Tuple[Tuple[str, ...], Optional[frozenset]]:
    """
    The name asked for, and the roles it allows.

    "the Save button" asks for a control named "save" that is some kind of
    button. Role words come off the name, so "Save" and "the Save button" ask
    for the same control.
    """
    roles: Optional[frozenset] = None
    name = []
    for word in _words(target):
        if word in ROLE_WORDS:
            roles = ROLE_WORDS[word] if roles is None else roles | ROLE_WORDS[word]
            continue
        if word in _FILLER:
            continue
        name.append(word)
    return tuple(name), roles


def published_name(element: ax.Element) -> Tuple[str, ...]:
    """The control's own name, as words. Empty when it publishes none."""
    return tuple(_words(element.label))


# ─── Coordinates ─────────────────────────────────────────────────────────────

def to_quartz(proposal: GroundingProposal, frame: Any,
              displays: Callable[[], List[Any]]) -> Tuple[Optional[Tuple[float, float]], str]:
    """
    The proposal as a Quartz point, or None and why not.

    Each space has exactly one conversion and every one is checked against
    its own bounds first. A FRAME_PIXEL point beyond the image is not clamped
    onto its edge: that is a model pointing outside what it was shown.
    """
    x, y = proposal.x, proposal.y
    if not (isinstance(x, (int, float)) and isinstance(y, (int, float))
            and math.isfinite(x) and math.isfinite(y)):
        return None, "the point is not a number"
    try:
        space = Space(proposal.space)
    except ValueError:
        return None, f"unknown coordinate space {proposal.space!r}"

    if space is Space.QUARTZ_POINT:
        return (float(x), float(y)), ""
    if space is Space.FRAME_NORMALISED:
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            return None, "a normalised point outside 0..1"
        return frame.to_quartz(x, y), ""
    if space is Space.FRAME_PIXEL:
        pw, ph = getattr(frame, "pixel_width", 0), getattr(frame, "pixel_height", 0)
        if pw <= 0 or ph <= 0:
            return None, "the frame does not know its own pixel size"
        if not (0.0 <= x < pw and 0.0 <= y < ph):
            return None, "a pixel outside the frame's image"
        return frame.to_quartz(x / pw, y / ph), ""
    if space is Space.DISPLAY_PIXEL:
        screen = next((d for d in displays() if d.id == proposal.display_id), None)
        if screen is None or not proposal.display_id:
            return None, "a display pixel on no display Diablo knows"
        if not (0.0 <= x < screen.width * screen.scale
                and 0.0 <= y < screen.height * screen.scale):
            return None, "a pixel outside that display"
        return (screen.x + x / screen.scale, screen.y + y / screen.scale), ""
    return None, f"no conversion for {space.value}"


def _inside(px: float, py: float, x: float, y: float, w: float, h: float) -> bool:
    return x <= px < x + w and y <= py < y + h


def _same_rect(a: Tuple[float, ...], b: Tuple[float, ...]) -> bool:
    return all(abs(p - q) <= SLACK for p, q in zip(a, b))


def _rect(element: ax.Element) -> Tuple[float, float, float, float]:
    return (element.x, element.y, element.width, element.height)


def _identity(element: ax.Element) -> Tuple[str, Tuple[str, ...]]:
    return (element.role, published_name(element))


def _observed_at(observation: Observation, px: float, py: float) -> Optional[ax.Element]:
    """The smallest control the observation saw containing the point."""
    best, smallest = None, float("inf")
    for element in observation.elements:
        if _inside(px, py, *_rect(element)):
            area = element.width * element.height
            if area < smallest:
                best, smallest = element, area
    return best


def _bounds(window: Dict[str, Any]) -> Tuple[float, float, float, float]:
    return (float(window.get("x", 0)), float(window.get("y", 0)),
            float(window.get("width", 0)), float(window.get("height", 0)))


# ─── The gate ────────────────────────────────────────────────────────────────

def validate(proposal: GroundingProposal, observation: Observation, *,
             hit: Callable[[float, float], Optional[ax.Hit]] = None,
             window_now: Callable[[int], Optional[Dict[str, Any]]] = None,
             elements_now: Callable[[Dict[str, Any]], List[ax.Element]] = None,
             pid_of: Callable[[Dict[str, Any]], int] = None,
             displays: Callable[[], List[Any]] = None,
             now: Callable[[], float] = time.time) -> Validation:
    """
    One proposal against one observation, checked against the screen as it is
    now. Every exit is a verdict; there is no exception path to a click.

    The callables default to the real accessibility and window queries and are
    parameters only so tests can stand in for the screen.
    """
    from skills import display as _display
    from skills import screen_context as _sc
    hit = hit or ax.hit_test
    window_now = window_now or _sc.window_by_id
    elements_now = elements_now or ax.elements_for_window
    pid_of = pid_of or ax._pid_for_window
    displays = displays or _display.displays

    def no(verdict: str, reason: str, point=None, found=None, detail="") -> Validation:
        return Validation(verdict, reason, point=point, found=found, detail=detail)

    frame = observation.frame

    # 1. The coordinate, in a space that is named and bounded.
    if proposal.frame_id != observation.frame_id or not proposal.frame_id:
        return no(REJECT_STALE_FRAME, "the point was read from a different capture",
                  detail="other_frame")
    point, why = to_quartz(proposal, frame, displays)
    if point is None:
        return no(REJECT_COORDINATE_SPACE, why)
    px, py = point
    if not _inside(px, py, frame.x, frame.y, frame.width, frame.height):
        return no(REJECT_COORDINATE_SPACE, "the point is outside the capture it came from",
                  point)
    if not any(_inside(px, py, d.x, d.y, d.width, d.height) for d in displays()):
        return no(REJECT_OFF_DISPLAY, "the point is on no display", point)

    # 2. The observation is still the screen.
    if not getattr(frame, "ok", False):
        return no(REJECT_STALE_FRAME, "the capture could not be read", point,
                  detail="invalid_frame")
    if now() - frame.captured_at > MAX_OBSERVATION_AGE:
        return no(REJECT_STALE_FRAME, "the capture is too old to vouch for anything",
                  point, detail="too_old")
    window = window_now(int(observation.window.get("id") or 0))
    if window is None:
        return no(REJECT_STALE_FRAME, "the window has gone", point, detail="window_gone")
    if not _same_rect(_bounds(window), _bounds(observation.window)):
        return no(REJECT_STALE_FRAME, "the window has moved since the capture", point,
                  detail="window_moved")
    if not _inside(px, py, *_bounds(window)):
        return no(REJECT_WRONG_WINDOW, "the point is outside the window it was asked about",
                  point)

    # 3. What is actually there, frontmost, now.
    found = hit(px, py)
    if found is None:
        return no(REJECT_UNVERIFIABLE, "accessibility cannot be asked what is there", point)
    expected_pid = pid_of(window)
    if found.pid and expected_pid and found.pid != expected_pid:
        return no(REJECT_WRONG_WINDOW, "another application's window is on top there",
                  point, found.element)
    if found.window_frame and not _same_rect(found.window_frame, _bounds(window)):
        return no(REJECT_WRONG_WINDOW, "another window is on top there", point, found.element)

    element = found.element
    seen = _observed_at(observation, px, py)
    if element is None:
        if seen is not None:
            return no(REJECT_STALE_FRAME, "the control the capture showed there has gone",
                      point, detail="gone")
        return no(REJECT_NO_TARGET, "there is no control at that point", point)
    if seen is None:
        return no(REJECT_STALE_FRAME, "a control is there now that the capture did not show",
                  point, element, detail="appeared")
    if _identity(seen) != _identity(element):
        return no(REJECT_STALE_FRAME, "the control there is not the one the capture showed",
                  point, element, detail="changed")
    if not _same_rect(_rect(seen), _rect(element)):
        return no(REJECT_STALE_FRAME, "the control has moved since the capture", point,
                  element, detail="moved")

    # 4. Is it the thing that was asked for — by its own name.
    asked, roles = wanted_name(proposal.target)
    if element.role not in ax.ACTIONABLE_ROLES:
        return no(REJECT_ROLE, "that is not something that can be pressed", point, element)
    if roles is not None and element.role not in roles:
        return no(REJECT_ROLE, f"that is a {element.role.replace('AX', '').lower()}, "
                               f"not what was asked for", point, element)
    name = published_name(element)
    if name and name != asked:
        return no(REJECT_LABEL_MISMATCH,
                  f"the control there is called “{element.label}”", point, element)
    if element.enabled is False:
        return no(REJECT_DISABLED, "that control is disabled", point, element)

    if name:
        twins = [e for e in elements_now(window)
                 if published_name(e) == name
                 and (roles is None or e.role in roles)
                 and e.role in ax.ACTIONABLE_ROLES]
        if len(twins) > 1:
            return no(REJECT_AMBIGUOUS,
                      f"{len(twins)} controls are called “{element.label}”", point, element)

    target = _issue(
        role=element.role, label=element.label, x=element.x, y=element.y,
        width=element.width, height=element.height, pid=found.pid or expected_pid,
        window_id=int(window.get("id") or 0), window_bounds=_bounds(window),
        frame_id=observation.frame_id, validated_at=now(),
        source_model=proposal.source_model, named=bool(name))
    if not name:
        # A real, enabled control that publishes no name. Nothing on the
        # screen can say whether it is the one asked for, so the person has
        # to: Diablo points at it and asks.
        return Validation(CONFIRM_REQUIRED, "the control there has no name to check",
                          point, target, element)
    return Validation(VALIDATED, f"the control there is {target.describe()}", point,
                      target, element)


def revalidate(target: ValidatedTarget, *,
               hit: Callable[[float, float], Optional[ax.Hit]] = None,
               window_now: Callable[[int], Optional[Dict[str, Any]]] = None,
               now: Callable[[], float] = time.time) -> Validation:
    """
    Immediately before a click: is the control still there, still itself,
    still enabled, still on top, in a window that has not moved?

    A frame's age is not this. A screen can change in a tenth of a second and
    stay the same for a minute; this asks the screen instead of the clock.
    """
    from skills import screen_context as _sc
    hit = hit or ax.hit_test
    window_now = window_now or _sc.window_by_id

    if not issued(target):
        return Validation(REJECT_UNVERIFIABLE, "that target was not issued by the gate")
    point = target.centre
    if now() - target.validated_at > MAX_OBSERVATION_AGE:
        return Validation(REJECT_STALE_FRAME, "the check is too old", point, detail="too_old")
    window = window_now(target.window_id)
    if window is None:
        return Validation(REJECT_STALE_FRAME, "the window has gone", point,
                          detail="window_gone")
    if not _same_rect(_bounds(window), target.window_bounds):
        return Validation(REJECT_STALE_FRAME, "the window has moved", point,
                          detail="window_moved")
    found = hit(*point)
    if found is None:
        return Validation(REJECT_UNVERIFIABLE, "accessibility cannot be asked", point)
    if found.pid and target.pid and found.pid != target.pid:
        return Validation(REJECT_WRONG_WINDOW, "another application is on top there", point,
                          found=found.element)
    if found.window_frame and not _same_rect(found.window_frame, target.window_bounds):
        return Validation(REJECT_WRONG_WINDOW, "another window is on top there", point,
                          found=found.element)
    element = found.element
    if element is None:
        return Validation(REJECT_STALE_FRAME, "the control has gone", point, detail="gone")
    if element.role != target.role or element.label != target.label:
        return Validation(REJECT_STALE_FRAME, "the control there has changed", point,
                          found=element, detail="changed")
    if not _same_rect(_rect(element), (target.x, target.y, target.width, target.height)):
        return Validation(REJECT_STALE_FRAME, "the control has moved", point, found=element,
                          detail="moved")
    if element.enabled is False:
        return Validation(REJECT_DISABLED, "the control is disabled now", point,
                          found=element)
    return Validation(VALIDATED, "still there and unchanged", point, target, element)


def check_point(x: float, y: float, label: str, role: str, window: Dict[str, Any], *,
                hit: Callable[[float, float], Optional[ax.Hit]] = None,
                pid_of: Callable[[Dict[str, Any]], int] = None) -> Validation:
    """
    The last look before act_on presses a control perception found.

    Perception chose the control from the window's own tree and the pixels,
    and a tree cannot see a window lying over it. This asks what is frontmost
    at the point and refuses unless it is that control, enabled, in that
    window. The label compared is the one perception read from the screen,
    never one a model supplied.
    """
    hit = hit or ax.hit_test
    pid_of = pid_of or ax._pid_for_window
    found = hit(x, y)
    if found is None:
        return Validation(REJECT_UNVERIFIABLE, "accessibility cannot be asked", (x, y))
    expected_pid = pid_of(window)
    if found.pid and expected_pid and found.pid != expected_pid:
        return Validation(REJECT_WRONG_WINDOW, "another application is on top there", (x, y),
                          found=found.element)
    if found.window_frame and not _same_rect(found.window_frame, _bounds(window)):
        return Validation(REJECT_WRONG_WINDOW, "another window is on top there", (x, y),
                          found=found.element)
    element = found.element
    if element is None:
        return Validation(REJECT_NO_TARGET, "there is no control at that point", (x, y))
    expected = tuple(_words(label))
    actual = published_name(element)
    if expected and actual and actual != expected:
        return Validation(REJECT_LABEL_MISMATCH,
                          f"the control there is called “{element.label}”", (x, y),
                          found=element)
    # Only a role accessibility reported can be compared; OCR has no roles.
    if role.startswith("AX") and element.role and role != element.role:
        return Validation(REJECT_ROLE, "the control there is a different kind", (x, y),
                          found=element)
    if element.enabled is False:
        return Validation(REJECT_DISABLED, "that control is disabled", (x, y), found=element)
    return Validation(VALIDATED, "the control is there and on top", (x, y), found=element)
