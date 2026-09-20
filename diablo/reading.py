"""
What Diablo is looking at, in a form the interface can show.

Screen understanding already works: `screen_context` captures one window,
reads it with Vision, groups the text into regions and hands the result to a
model. What was missing is the part the user can see. The interface knew only
that "a read is in progress" and, at best, which whole window it was — so a
maximised document produced no visible focus at all, and a failure to read
looked exactly like a failure to answer.

This module is the translation layer, and nothing else. It captures nothing,
reads nothing, and resolves no windows. It takes what perception already
produced and answers the three questions the interface has:

  WHERE      is the content he is reading, in desktop points
  WHAT       should the surface say about it, in his own words
  WHETHER    what he read still describes what is on screen

Two things are deliberately not here. There is no second capture path, no
second OCR, and no arithmetic of its own: the normalised-box-to-desktop
conversion is `display.image_to_quartz`, which pointing and the tutor already
use. And there is no state machine: the phases below are names for events
that actually happen, emitted where they happen, not a clock to run.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

Rect = Tuple[float, float, float, float]      # x, y, w, h — Quartz, top-left
Bounds = Tuple[float, float, float, float]    # u0, v0, u1, v1 — 0..1 in a window


# ─── What happened ───────────────────────────────────────────────────────────
#
# One outcome per way this can end, each with a sentence a person would say.
# Perception distinguishes all of these already; it just said them in prose
# that the interface could not tell apart from an answer.

OK = "ok"
NO_WINDOW = "no_window"
NO_PERMISSION = "no_permission"
REFUSED = "refused"
NO_CAPTURE = "no_capture"
UNREADABLE = "unreadable"

MESSAGES = {
    NO_WINDOW: "I can't see a window to read.",
    NO_PERMISSION: "I can't read the screen until Screen Recording is on.",
    REFUSED: "I won't read that one.",
    NO_CAPTURE: "I couldn't capture that window.",
    UNREADABLE: "I could see it, but I couldn't read it clearly.",
}

# The distinction §12 asks for, in the order a person would check it: could I
# get a picture at all, and if I could, was there anything legible in it.
# Matched against what `look()` and `structured()` already return, because
# rewording those would change what the model is told as well.
_SIGNS = (
    ("screen recording permission", NO_PERMISSION),
    ("cannot enumerate windows", NO_PERMISSION),
    ("could not find a window", NO_WINDOW),
    ("no window is visible", NO_WINDOW),
    ("password manager", REFUSED),
    ("refused:", REFUSED),
    ("could not capture", NO_CAPTURE),
    ("another space", NO_CAPTURE),
    ("no readable text", UNREADABLE),
    ("ocr", UNREADABLE),
)


def _status_lines(text: str) -> List[str]:
    """
    The lines of a read that are about the read, not about the screen.

    This matters more than it looks. `look()` returns the window's whole
    text, so scanning the lot for "could not capture" would call a reading
    of a page that discusses screenshots a failure. Perception says so in
    exactly three shapes — an Error: line, a Refused: line, or a bracketed
    note — and only those are evidence about what happened.
    """
    found = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith(("error:", "refused:")):
            found.append(stripped)
        elif stripped.startswith("(") and stripped.endswith(")"):
            found.append(stripped)
    return found


def outcome_of(result: Any) -> str:
    """
    Which of the ways this ends happened, from what perception returned.

    Takes either the string `look()` returns or the dict `structured()`
    returns, because the conversational path and the tool path produce
    different shapes for the same six situations.
    """
    if isinstance(result, dict):
        reported = [str(result.get("error") or "")]
        if not result.get("error") and not result.get("regions"):
            return UNREADABLE
    else:
        text = str(result or "")
        if not text.strip():
            return UNREADABLE
        reported = _status_lines(text)
        if not reported:
            return OK
    for line in reported:
        lowered = line.lower()
        for sign, outcome in _SIGNS:
            if sign in lowered:
                return outcome
    return UNREADABLE if any(reported) else OK


def message_for(outcome: str, app: str = "") -> str:
    """
    What the surface says. Never the internal sentence.

    "Perception arbitration failed" is true and useless. These name the thing
    that went wrong in terms the person can act on, and the app when knowing
    which window it was helps.
    """
    if outcome == OK:
        return ""
    line = MESSAGES.get(outcome, MESSAGES[UNREADABLE])
    if app and outcome in (NO_CAPTURE, UNREADABLE, REFUSED):
        return line.replace("that one", app).replace("that window", app)
    return line


# ─── Where to look ───────────────────────────────────────────────────────────

# One region has to hold this much of the text before it is "the" region
# rather than merely the biggest of several. Below it there is no single
# thing being read, and §13 says stay home rather than pick one.
DOMINANT = 0.6

# Smaller than this on screen and an outline is a smudge, not a focus.
MIN_SIDE = 44.0

# A focus that fills this much of its own display is the display. Outlining
# the whole screen tells the user nothing they did not already know, so it
# falls back to the broad treatment: he stays where he is.
MAX_DISPLAY_SHARE = 0.55

# The reading is of the text, so the box hugs it. A little air makes the
# outline read as "around this" rather than "cutting through this".
PAD = 8.0


@dataclass
class Focus:
    """One thing on screen, and where it is."""

    rect: Rect                     # Quartz, top-left
    label: str = ""                # what it is, in a few words
    app: str = ""
    title: str = ""
    window_id: int = 0
    share: float = 0.0             # of the window's text this region holds
    lines: List[str] = field(default_factory=list)

    @property
    def centre(self) -> Tuple[float, float]:
        x, y, w, h = self.rect
        return (x + w / 2.0, y + h / 2.0)


def rect_of(bounds: Optional[Bounds], window: Dict[str, Any],
            pad: float = PAD) -> Optional[Rect]:
    """
    A region's normalised box as a rectangle on the desktop.

    Through `display.image_to_quartz`, which is the conversion pointing and
    the tutor already use — the scale factor cancels in the normalised form,
    which is why a Retina capture and an upscaled one land in the same place.
    """
    if not bounds or not window:
        return None
    try:
        from skills import display
    except ImportError:
        return None
    try:
        u0, v0, u1, v1 = (float(b) for b in bounds)
    except (TypeError, ValueError):
        return None
    if u1 <= u0 or v1 <= v0:
        return None
    x0, y0 = display.image_to_quartz(u0, v0, window)
    x1, y1 = display.image_to_quartz(u1, v1, window)
    return (x0 - pad, y0 - pad, (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad)


def worth_showing(rect: Optional[Rect]) -> bool:
    """A rectangle big enough to see and small enough to mean something."""
    if not rect:
        return False
    x, y, w, h = rect
    if w < MIN_SIDE or h < MIN_SIDE:
        return False
    try:
        from skills import display
        screen = display.for_point(x + w / 2.0, y + h / 2.0)
    except Exception:
        screen = None
    if screen is None:
        # Off every display: no evidence that this is anywhere. §13.
        return False
    return w * h <= MAX_DISPLAY_SHARE * screen.width * screen.height


def name_for(region: Dict[str, Any]) -> str:
    """
    A short name for what he is reading, taken from the content itself.

    A heading if the region has one, because that is what the person calls
    it. Otherwise where it sits, which `ocr_regions` already labels. Never
    invented, and never the raw text dumped into a caption.
    """
    for heading in (region.get("headings") or []):
        text = " ".join(str(heading).split())
        if 2 < len(text) <= 42:
            return text
    label = str(region.get("label") or "").strip()
    if label and label != "main area":
        return label
    return "the text"


def focus_of(regions: Sequence[Dict[str, Any]],
             window: Dict[str, Any]) -> Optional[Focus]:
    """
    The one region this reading is about, or nothing.

    Nothing is a real answer and the common one: a page with three columns of
    equal weight has no single thing being read, and §13 is explicit that the
    visual layer represents evidence rather than a guess. The caller stays
    home in that case and uses the broad treatment instead.
    """
    usable = [r for r in (regions or [])
              if isinstance(r, dict) and r.get("bounds") and r.get("chars")]
    if not usable or not window:
        return None
    total = float(sum(r["chars"] for r in usable))
    if total <= 0:
        return None
    best = max(usable, key=lambda r: r["chars"])
    share = best["chars"] / total
    if len(usable) > 1 and share < DOMINANT:
        return None
    rect = rect_of(best.get("bounds"), window)
    if not worth_showing(rect):
        return None
    return Focus(rect=rect,
                 label=name_for(best),
                 app=str(window.get("app") or ""),
                 title=str(window.get("title") or ""),
                 window_id=int(window.get("id") or 0),
                 share=share,
                 lines=list(best.get("lines") or [])[:4])


# ─── Whether it is still true ────────────────────────────────────────────────


def still_shows(window: Dict[str, Any]) -> bool:
    """
    Whether the window read is still where it was when it was read.

    A rectangle is only true of the window it was measured in. If that window
    has been moved, resized or closed between the capture and the moment the
    outline would appear, the rectangle now points at whatever took its
    place — which is the "Diablo pointing at the wrong application" failure
    §19 names. `screen_context.has_moved` already answers this from the
    window server, with no capture and no second window resolver.

    Scrolling inside a window it cannot see: the window has not moved, so
    this says nothing about it. That case is bounded instead by how briefly
    the outline is up, and is written down as a limitation rather than
    guessed at.
    """
    if not window:
        return False
    try:
        from skills import screen_context
    except ImportError:
        return True
    try:
        return screen_context.has_moved(window) is None
    except Exception:
        return True


# ─── What the surface is told ────────────────────────────────────────────────

LOOKING = "looking"        # a capture has been asked for
READING = "reading"        # pixels in hand, Vision working
UNDERSTOOD = "understood"  # a region came back, and where it is
FAILED = "failed"          # one of the outcomes above, with its sentence
DONE = "done"              # the read is over; the answer belongs to GUI-4

PHASES = (LOOKING, READING, UNDERSTOOD, FAILED, DONE)


def status_for(phase: str, app: str = "", label: str = "") -> str:
    """
    The line the surface shows while this is happening.

    Activity, not reasoning: §16 draws the line at describing what is being
    done, and these stay on the near side of it. Naming the app is what makes
    it believable that he is looking at the screen rather than thinking.
    """
    if phase == LOOKING:
        return f"Looking at {app}…" if app else "Looking at your screen…"
    if phase == READING:
        return f"Reading {app}…" if app else "Reading your screen…"
    if phase == UNDERSTOOD:
        return f"Reading {label}…" if label else "Working through it…"
    return ""


def source_line(app: str = "", title: str = "", label: str = "") -> str:
    """
    The quiet line under an answer saying what it was read from.

    Provenance, not a citation: the app and window that were actually
    captured, which the shell knows for certain because it watched the
    capture happen. Nothing here is generated from the answer.
    """
    if not app:
        return ""
    where = " ".join(str(title).split())[:48]
    if label and label not in ("the text", "main area"):
        head = f"{label} in {app}"
    else:
        head = app
    return f"From {head} — {where}" if where else f"From {head}"
