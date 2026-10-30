"""
Finding the thing the question asked about, rather than the words that name it.

`target.py` decides WHAT KIND of thing would answer a question. This decides
WHERE that thing is, and it is the only new place in the runtime that can
return an area of a picture.

It is not a second perception engine, and the split is worth stating because
it is the whole design:

    a CONTROL or a piece of TEXT   goes straight to perception.read(), which
                                   has arbitrated accessibility against OCR
                                   against vision for a long time and is not
                                   touched here. "Where is File?" runs today's
                                   code, byte for byte.

    a REGION, FIGURE or OBJECT     is what nothing could answer. OCR's match
                                   on "welfare loss" is kept — but as an
                                   ANCHOR, which says where to look, not as
                                   the answer, which is what the bug was.

The anchored look is narrow on purpose. A vision model handed a whole 3.5
megapixel screen answers about whichever tenth of it is loudest; handed a 500
point crop centred on the caption that names the thing, it is answering the
question that was asked. Scope widens only when the narrow look says it could
not see the thing, which is the one signal worth escalating on.

Everything expensive is behind a seam so the tests can be deterministic:
`_ask_region` is the model, `_frame_for` is the provenance. Capture goes
through `display.capture_region`, which already exists and already takes its
rectangle in Quartz points; mapping back out of the crop goes through
`display.image_to_quartz`, which is the one normalised-to-Quartz mapper in the
codebase. No second coordinate system was created for this.

Nothing here clicks, types, or moves anything. It looks and it reports.
"""

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import config, perception, target  # noqa: E402

# How far around an anchor to look, in Quartz points. A caption on an
# economics diagram sits beside, under or in the legend of the shape it names,
# so the box has to be generous enough to contain the shape and small enough
# that the model is not being asked about the whole page again.
ANCHOR_PAD = config.env_float("DIABLO_ANCHOR_PAD", 260.0, low=40.0, high=1200.0)

# The next rung: the figure or column the anchor sits in. Not the window yet.
PANEL_PAD = config.env_float("DIABLO_PANEL_PAD", 620.0, low=120.0, high=3000.0)

# A cursor question is about what is under the pointer, and a box this size is
# about what a person means by "this".
CURSOR_PAD = config.env_float("DIABLO_CURSOR_PAD", 140.0, low=30.0, high=800.0)

# A model-supplied rectangle is weaker evidence than a published one, for the
# same reason vision is capped below a real label: the application knows where
# its own controls are, and nothing knows where a shaded triangle is except by
# looking. It can never out-rank an accessibility element.
REGION_CEILING = 0.85

# How much of the window may be behind other windows before a look at it is
# not worth taking (B.5.1). Past this, most of what a capture holds is some
# other window's pixels: the answer is to say so, not to look.
HIDDEN_LIMIT = config.env_float("DIABLO_HIDDEN_LIMIT", 0.5, low=0.05, high=1.0)

# The reply, in a shape Ollama enforces.
#
# `bbox_2d`, and the name is not cosmetic. Phase B asked for left/top/right/
# bottom as fractions of the image, which is a format no Qwen VL model was
# ever trained to produce, and the measured consequence was boxes quantised
# to multiples of 0.05 — the model guessing round numbers rather than
# measuring. Qwen2.5-VL and Qwen3-VL are trained to emit `bbox_2d` as
# [x1, y1, x2, y2] in absolute pixels of the image they were handed.
#
# Same model, same crop, same wording, five trials each:
#
#     protocol        welfare loss IoU   consumer surplus IoU   latency
#     fractions               0.01               0.29            4.0s
#     bbox_2d                 0.59               0.53            1.5s
#     bbox_2d + where         0.63               0.48            3.0s
#
# `where` stays because it costs about a second and it is what Diablo says
# out loud: the sentence is right even on the trials where the rectangle is
# loose, and it is the half of the answer a person can act on.
REGION_SCHEMA = {
    "type": "object",
    "properties": {
        "where": {"type": "string"},
        "found": {"type": "boolean"},
        "bbox_2d": {"type": "array", "items": {"type": "number"}},
        "what": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["where", "found", "bbox_2d", "confidence"],
}


@dataclass
class Ground:
    """Where the requested thing is, or an honest account of why it is not."""

    target: target.Target
    candidate: Optional[perception.Candidate] = None
    scope: str = ""
    abstained: bool = False
    note: str = ""
    evidence: List[str] = field(default_factory=list)
    frame_fingerprint: List[int] = field(default_factory=list)
    looks: int = 0                 # how many captures this cost
    took_ms: float = 0.0

    @property
    def found(self) -> bool:
        return self.candidate is not None


# ─── The seams ───────────────────────────────────────────────────────────────

class _Frame:
    """Provenance for a look: which screen it was true of."""

    def __init__(self, fingerprint=None):
        self.fingerprint = list(fingerprint or [])
        self.ok = True
        self.problem = ""

    def release(self) -> None:
        pass


def _frame_for(window: Optional[Dict[str, Any]]) -> _Frame:
    """
    What this look will be tied to.

    Deliberately cheap. The fingerprint is taken from the crop that is about
    to be captured rather than from a second full-window capture, because a
    capture taken only to fingerprint another capture is a second of latency
    spent on bookkeeping. The mechanism is the existing one — `screen._fingerprint`
    and `VisionEvidence.stale_against` — applied to the pixels actually used.
    """
    return _Frame()


def _sized_for_model(path: str) -> Tuple[str, int, int, bool]:
    """
    The image as the model will actually receive it, and its pixel size.

    `read_image` already downscales anything over `VISION_MAX_EDGE` before it
    sends it. Doing that here first makes the second call a no-op and — the
    point — means the dimensions the model is answering in are known rather
    than guessed. An absolute pixel coordinate is only readable if you know
    which picture it is absolute to.
    """
    from skills import screen_context as _sc
    sized, temporary = _sc._for_vision(path)
    try:
        from AppKit import NSBitmapImageRep
        from Foundation import NSData
        rep = NSBitmapImageRep.imageRepWithData_(
            NSData.dataWithContentsOfFile_(sized))
        if rep is not None:
            return sized, int(rep.pixelsWide()), int(rep.pixelsHigh()), temporary
    except Exception:
        pass
    return sized, 0, 0, temporary


# ─── The box's coordinate space ──────────────────────────────────────────────
#
# A box is four numbers, and four numbers mean nothing until it is said what
# they are measured in. This used to be guessed: fractions of the image first,
# then Qwen2.5-VL's resized canvas, then the image's own pixels, then a
# 0..1000 grid, taking the first reading that made a sane rectangle. For a
# model answering on a 0..1000 grid about an image wider than 1000 pixels the
# "pixels" reading always fits first, and the box lands in the wrong place
# without a word.
#
# So the space is declared per model family and read exactly one way. Qwen3.5
# answers on Qwen's documented 0..1000 grid. Measured 2026-10-05 on red boxes
# at known positions, qwen3.5:4b at Q4_K_M was exact on that grid for a
# 426x316 image and drifted towards pixel-like values on larger ones, which is
# why GUI targets are GoClick's job and a model box is used only for regions
# inside figures, where the deterministic shape picker checks it.
BOX_SPACES = (("qwen3.5", 1000.0), ("qwen3-vl", 1000.0), ("qwen3vl", 1000.0))


def box_scale(model: Optional[str] = None) -> Optional[float]:
    """The grid the vision model's boxes are measured on, or None if unknown."""
    from skills import local_brain as _lb
    name = (model or _lb.CHAT_MODEL or "").lower()
    for family, scale in BOX_SPACES:
        if family in name:
            return scale
    return None


def _box_in_crop(values: Any, image: Tuple[int, int], scale: Optional[float] = None
                 ) -> Optional[Tuple[float, float, float, float]]:
    """
    Four numbers from the model, as fractions of the crop — or nothing.

    Read in the model's declared space and no other. Numbers that are not a
    sane rectangle there are refused rather than reinterpreted, and a model
    with no declared space gets no box at all.
    """
    if not isinstance(values, (list, tuple)) or len(values) < 4:
        return None
    try:
        x1, y1, x2, y2 = [float(v) for v in values[:4]]
    except (TypeError, ValueError):
        return None
    if any(v != v for v in (x1, y1, x2, y2)):
        return None
    scale = scale or box_scale()
    if not scale:
        return None
    u1, v1, u2, v2 = x1 / scale, y1 / scale, x2 / scale, y2 / scale
    if not all(-0.02 <= v <= 1.02 for v in (u1, v1, u2, v2)):
        return None
    if u2 - u1 <= 0.005 or v2 - v1 <= 0.005:
        return None
    return (max(u1, 0.0), max(v1, 0.0), min(u2, 1.0), min(v2, 1.0))


def _ask_region(path: str, question: str,
                crop: Tuple[float, float, float, float]) -> Optional[dict]:
    """
    One narrow question about one crop, answered in a shape that is checkable.

    The model is asked where the thing is INSIDE this picture, as fractions of
    the picture. It is not asked for a screen coordinate: it does not know
    where the crop came from, and an invented screen coordinate is
    indistinguishable from a real one.
    """
    from skills import screen_context as _sc
    raw = _sc.read_image(path, question, num_predict=260, schema=REGION_SCHEMA)
    if not raw or raw.startswith("Error:"):
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


# ─── Crops ───────────────────────────────────────────────────────────────────

def _clip(box: Tuple[float, float, float, float],
          bounds: Optional[Dict[str, Any]]) -> Tuple[float, float, float, float]:
    """Keep a crop inside the window it belongs to."""
    x, y, w, h = box
    if not bounds:
        return (x, y, max(w, 1.0), max(h, 1.0))
    bx, by = float(bounds.get("x", 0.0)), float(bounds.get("y", 0.0))
    bw, bh = float(bounds.get("width", 0.0)), float(bounds.get("height", 0.0))
    left, top = max(x, bx), max(y, by)
    right, bottom = min(x + w, bx + bw), min(y + h, by + bh)
    return (left, top, max(right - left, 1.0), max(bottom - top, 1.0))


def _around(x: float, y: float, w: float, h: float, pad: float) -> Tuple[float, float, float, float]:
    return (x - pad, y - pad, w + pad * 2, h + pad * 2)


def _crop_for(scope: str, window: Optional[Dict[str, Any]],
              anchor: Optional[Dict[str, Any]],
              cursor: Optional[Tuple[float, float]]
              ) -> Optional[Tuple[float, float, float, float]]:
    """The rectangle to capture for this scope, or None if it cannot be formed."""
    if scope == target.CURSOR:
        if not cursor:
            return None
        return _clip(_around(cursor[0], cursor[1], 1.0, 1.0, CURSOR_PAD), window)
    if scope == target.ANCHOR:
        if not anchor:
            return None
        return _clip(_around(anchor["left"], anchor["top"], anchor["width"],
                             anchor["height"], ANCHOR_PAD), window)
    if scope == target.PANEL:
        # The cursor first, not the caption. PANEL is only reached by a cursor
        # question now, and centring it on whatever words happened to match
        # put the crop on the far side of the window from the pointer.
        if cursor:
            return _clip(_around(cursor[0], cursor[1], 1.0, 1.0, PANEL_PAD), window)
        if anchor:
            return _clip(_around(anchor["left"], anchor["top"], anchor["width"],
                                 anchor["height"], PANEL_PAD), window)
        return _window_crop(window)
    if scope == target.WINDOW:
        return _window_crop(window)
    # The whole desktop. Not reachable while a window is known, because a
    # question about what is in this window cannot be answered by looking at
    # a different one — and capturing every display to answer it would be the
    # expensive, least accurate thing this build exists to stop doing.
    return None


def _window_crop(window: Optional[Dict[str, Any]]
                 ) -> Optional[Tuple[float, float, float, float]]:
    if not window:
        return None
    return (float(window.get("x", 0.0)), float(window.get("y", 0.0)),
            float(window.get("width", 0.0)), float(window.get("height", 0.0)))


# ─── Anchors ─────────────────────────────────────────────────────────────────

def _anchor_for(wanted: target.Target, window: Optional[Dict[str, Any]]
                ) -> Optional[Dict[str, Any]]:
    """
    The printed words that name the thing, if any are on screen.

    This is the line that used to be the answer. It is evidence now: it says
    where on the page the thing is talked about, which is almost always near
    where the thing is. What it is not, ever, is the target.
    """
    if not wanted.semantic:
        return None
    try:
        from skills import pointing
        hits = pointing.locate(wanted.semantic, (window or {}).get("app", ""), limit=3,
                               window=window)
    except Exception:
        return None
    for hit in hits or []:
        if float(hit.get("score", 0.0)) >= perception.FLOOR:
            return hit
    return None


# ─── Reading the reply ───────────────────────────────────────────────────────

def _confidence_of(reply: dict) -> float:
    """
    The model's own confidence, on a scale of 0 to 1 whatever scale it used.

    Measured: qwen2.5vl returns `100` about as often as it returns `0.95`.
    Taking that at face value turned a percentage into a certainty above any
    threshold there is.
    """
    try:
        value = float(reply.get("confidence", 0.0))
    except (TypeError, ValueError):
        return 0.0
    if value != value:
        return 0.0
    if value > 1.0:
        value = value / 100.0
    return max(0.0, min(value, 1.0))


def _rect_from(reply: Any, crop: Tuple[float, float, float, float],
               image: Tuple[int, int] = (0, 0)
               ) -> Optional[Tuple[float, float, float, float, float, str, str]]:
    """
    A checked rectangle in Quartz points, or nothing.

    Every refusal here is a refusal to point somewhere. An inverted box, a box
    outside the crop, a box with no area and a box the model was not confident
    about are all the same answer: say so rather than ring a guess.
    """
    if not isinstance(reply, dict):
        return None
    if not reply.get("found"):
        return None
    box, scale = reply.get("bbox_2d"), None
    if box is None and "left" in reply:
        # The older fraction shape. Its keys say what its numbers are —
        # fractions of the crop — so it is read on that scale, never the
        # model's grid.
        box = [reply.get("left"), reply.get("top"),
               reply.get("right"), reply.get("bottom")]
        scale = 1.0
    normalised = _box_in_crop(box, image, scale)
    if normalised is None:
        return None
    left, top, right, bottom = normalised
    confidence = _confidence_of(reply)
    if confidence != confidence or confidence < perception.FLOOR:
        return None

    from skills import display as _display
    frame = {"x": crop[0], "y": crop[1], "width": crop[2], "height": crop[3]}
    x0, y0 = _display.image_to_quartz(left, top, frame)
    x1, y1 = _display.image_to_quartz(right, bottom, frame)
    return (x0, y0, x1 - x0, y1 - y0, min(confidence, REGION_CEILING),
            str(reply.get("what", ""))[:60],
            str(reply.get("where", "") or reply.get("reason", ""))[:160])


def _prompt(wanted: target.Target, window: Optional[Dict[str, Any]],
            anchor: Optional[Dict[str, Any]]) -> str:
    app = (window or {}).get("app", "an application")
    what = wanted.semantic or "the thing under discussion"
    near = ""
    if anchor:
        near = (f" The words “{str(anchor.get('text', ''))[:50]}” appear in this "
                f"crop; they are a caption or a legend entry, so they tell you "
                f"what is being discussed but they are NOT the thing itself.")
    if wanted.kind == target.FIGURE:
        job = f"the whole figure — the chart, diagram or image — that shows {what}"
    elif wanted.cursor_local and not wanted.semantic:
        job = ("the single object in the middle of this crop, which is what the "
               "user is pointing at")
    else:
        job = (f"the filled or marked area that is the “{what}” region — the "
               f"shaded shape, the coloured area, the part of the drawing "
               f"itself. Text labels are never the region.")
    # "Outline the position of X and output its coordinates as bbox_2d" is the
    # phrasing these models were trained on, and asking in their own words is
    # most of why this works at all. `where` comes first because the sentence
    # is what Diablo says out loud and because thinking before measuring costs
    # a second and loses nothing.
    #
    # No example sentence. There used to be one, and on a real rendered PDF
    # the model returned it word for word when asked about the demand curve —
    # "the grey triangle just left of where the two lines cross" — with a box
    # to match. The fixtures could not show that, because on them the example
    # happened to be the correct answer. A model handed a finished sentence
    # reaches for it exactly when it is least sure, which is the moment it
    # most needs to be looking instead.
    return (
        f"This is a crop of {app}.{near}\n\n"
        f"Outline the position of {job}\n\n"
        f"First, in `where`, say in plain words where it is, using what is "
        f"actually in this picture: its colour, its shape, and what it sits "
        f"beside. Then output its coordinates in this image as bbox_2d.\n\n"
        f"If it is not visible in this crop, set found to false — that is a "
        f"useful answer and a wrong box is not.")


# ─── The one entry point ─────────────────────────────────────────────────────

def ground(question: str, window: Optional[Dict[str, Any]] = None,
           stated_kind: str = "") -> Ground:
    """
    What the question is about, and where it is.

    `stated_kind` is the decision layer's own verdict when it has one. Nothing
    about the screen goes back the other way: no coordinates, no confidence,
    no frame identity. The planner says what is wanted; this says where it is.
    """
    started = time.time()
    wanted = target.read_question(question, stated=stated_kind)

    if window is None:
        try:
            from skills import screen_context as _sc
            window = _sc.frontmost_window()
        except Exception:
            window = None
    if window and window.get("seen_at"):
        # A live window is looked at where it is now, or not at all. One that
        # was never listed (a test, a script) is taken as given.
        try:
            from skills import screen_context as _sc
            now = _sc.window_by_id(window.get("id", 0))
        except Exception:
            now = window
        if now is None:
            return Ground(target=wanted, abstained=True,
                          note=(f"The {window.get('app') or 'that'} window has closed, "
                                f"Sir, so there is nothing there to look at."),
                          took_ms=(time.time() - started) * 1000)
        window = now
    if window and window.get("sensitive"):
        return Ground(target=wanted, abstained=True,
                      note=f"I won't read a {window.get('app', '')} window, Sir.",
                      took_ms=(time.time() - started) * 1000)

    if not wanted.pointable:
        return Ground(target=wanted, abstained=True, note=target.abstain(wanted),
                      took_ms=(time.time() - started) * 1000)

    if wanted.kind in (target.CONTROL, target.TEXT):
        out = _by_perception(wanted, window)
    else:
        out = _by_looking(wanted, window)
    out.took_ms = (time.time() - started) * 1000
    return out


def _by_perception(wanted: target.Target, window) -> Ground:
    """
    Today's path, unchanged, with one thing added: the answer has to be the
    kind of thing that was asked for before it is handed on.
    """
    # An action's operation goes with it (B.3): a step "the New Note button"
    # must land on a control that creates, not on text that mentions a note.
    reading = perception.read(wanted.semantic, window,
                              **({"operation": wanted.operation}
                                 if wanted.operation else {}))
    best = reading.best
    if best is None:
        disabled = (reading.observation or {}).get("disabled") or []
        note = (target.cannot_do(wanted, disabled=disabled[0] if disabled else "")
                if wanted.operation else target.abstain(wanted))
        return Ground(target=wanted, scope=target.CONTROL_SCOPE, abstained=True,
                      note=note)
    if reading.ambiguous and reading.question():
        return Ground(target=wanted, scope=target.CONTROL_SCOPE, abstained=True,
                      note=reading.question())
    if not target.satisfies(wanted.kind, best):
        return Ground(target=wanted, scope=target.CONTROL_SCOPE, abstained=True,
                      note=target.abstain(wanted),
                      evidence=[target.why_not(wanted.kind, best)])
    return Ground(target=wanted, candidate=best, scope=target.CONTROL_SCOPE,
                  evidence=list(best.evidence))


# ─── Choosing among areas the pixels found (B.4) ─────────────────────────────
#
# Geometry from the picture, identity from the model. regions.candidates finds
# the filled areas exactly; the model is shown them lettered and asked which
# one is meant, as a letter or NONE. It is never asked where anything is, and
# a letter cannot be an invented coordinate. The box that comes back is still
# checked by everything a model's box is checked by.

# Kinds whose answer is an area of a picture, which is what the pixels find.
AREA_KINDS = (target.REGION, target.FIGURE)

# How a chosen area's provenance note begins. It is kept in the evidence, for
# the trace and for "how do you know", and never read out as the answer:
# spoken live, it came out as "it is one of 3 filled areas found in the
# picture itself", which is how Diablo works rather than where the thing is.
PICKED = "chosen from"

# When the model says none of the areas found is the thing: say it could not be
# found ("abstain"), or look the old way with the canvas corrected ("bbox").
# Abstain, measured: falling back to the model's own box put a confident ring
# on a grey triangle for "the purple circle" 2 trials in 2, and abstaining
# cost nothing on the 18 area cases (16 right either way).
NONE_POLICY = os.getenv("DIABLO_REGION_NONE", "abstain").strip().lower()


@dataclass
class _Pick:
    """What choosing among found areas came to."""

    rect: Optional[Tuple[float, float, float, float, float, str, str]] = None
    offered: int = 0          # how many areas the model was shown
    choice: str = ""          # the letter, or NONE
    outline: Tuple[Tuple[float, float], ...] = ()   # its edge, in Quartz (B.5)


# ─── Does the chosen area agree with the words? ─────────────────────────────
#
# The judge chooses; it is not believed on its own. Measured: asked for "the
# purple circle" on a picture with no purple and no circle, it chose the grey
# triangle, and asked for "the arrow" it chose the area the arrow points at.
# When the words name a colour or a shape, the area chosen has to have it, and
# that is checked against the pixels, not asked of the model again.

# Hue in degrees, for colours with one. Pale, semi-transparent fills keep
# their hue and lose their saturation, so hue is what is compared.
_HUES = {"red": 0, "orange": 30, "yellow": 55, "green": 130, "teal": 175,
         "cyan": 185, "blue": 215, "purple": 270, "violet": 265, "lilac": 270,
         "magenta": 300, "pink": 330}
_GREYS = {"grey", "gray", "black", "white", "silver"}
_SHAPES = {
    "circle": (0.68, 0.86), "circular": (0.68, 0.86), "round": (0.68, 0.86),
    "oval": (0.68, 0.86), "disc": (0.68, 0.86), "dot": (0.68, 0.86),
    "triangle": (0.3, 0.7), "triangular": (0.3, 0.7), "wedge": (0.3, 0.7),
    "rectangle": (0.8, 1.01), "square": (0.8, 1.01), "bar": (0.8, 1.01),
    "column": (0.8, 1.01), "box": (0.8, 1.01), "block": (0.8, 1.01),
}

# Things that are not areas: an area step handed one of these chooses the
# nearest filled shape instead, which is a confident wrong answer.
_THIN = {"curve", "curves", "line", "lines", "arrow", "arrows", "axis", "axes",
         "point", "points", "dot", "dots", "intersection", "crossing", "cross",
         "segment", "tick", "ticks", "slope", "trend", "edge", "border", "boundary"}
# ...unless the words also say it is an area: "the area under the curve".
_AREA = {"area", "region", "shaded", "shading", "zone", "band", "patch", "triangle",
         "rectangle", "square", "bar", "slice", "wedge", "portion", "section",
         "quadrant", "fill", "filled", "surplus"}


def _hue_of(colour: Tuple[int, int, int]) -> Tuple[float, float, float]:
    import colorsys
    h, s, v = colorsys.rgb_to_hsv(*(c / 255.0 for c in colour))
    return h * 360.0, s, v


def _words_of(text: str) -> set:
    return set(re.findall(r"[a-z]+", (text or "").lower()))


def _not_an_area(semantic: str) -> bool:
    words = _words_of(semantic)
    return bool(words & _THIN) and not (words & _AREA)


def _agrees(semantic: str, region: Any) -> bool:
    """Whether an area's measured colour and shape fit the words asked with."""
    words = _words_of(semantic)
    hue, saturation, value = _hue_of(region.colour)
    named = [w for w in words if w in _HUES]
    if named:
        if saturation < 0.08:
            return False
        if not any(min(abs(hue - _HUES[w]), 360 - abs(hue - _HUES[w])) <= 30 for w in named):
            return False
    if words & _GREYS and not named:
        if saturation > 0.15:
            return False
        if "black" in words and value > 0.35:
            return False
    for word in words:
        if word in _SHAPES:
            low, high = _SHAPES[word]
            if not low <= region.fill <= high:
                return False
            if _SHAPES[word][0] > 0.6 and word in ("circle", "circular", "round", "disc") \
                    and max(region.width, region.height) > 1.6 * min(region.width, region.height):
                return False
    return True


def _judge_prompt(wanted: target.Target, letters: str,
                  anchor: Optional[Dict[str, Any]]) -> str:
    what = wanted.semantic or "the thing asked about"
    caption = ""
    if anchor and anchor.get("text"):
        caption = (f" The words “{str(anchor['text'])[:40]}” are printed somewhere in "
                   f"the picture; they are a label, not the thing.")
    return (f"This picture has boxes drawn on it, each with a letter "
            f"({', '.join(letters)}). Which box outlines the {what}?{caption} "
            f"Judge by what is inside each box. Each box holds a filled area. If the "
            f"{what} is a line, a curve, an arrow or a single point rather than a "
            f"filled area, or no box holds it, answer NONE. Otherwise answer with "
            f"one letter.")


def _pick_area(path: str, image: Tuple[int, int], wanted: target.Target,
               anchor: Optional[Dict[str, Any]],
               crop: Tuple[float, float, float, float]) -> Optional[_Pick]:
    """
    The area the model picks from those the pixels found, or None if there
    were none to offer — in which case the caller asks the old question.

    Never raises: a picture the pixel step cannot read is a reason to ask the
    old way, not a reason the question goes unanswered.
    """
    try:
        return _pick_area_unguarded(path, image, wanted, anchor, crop)
    except Exception:
        return None


def _pick_area_unguarded(path: str, image: Tuple[int, int], wanted: target.Target,
                         anchor: Optional[Dict[str, Any]],
                         crop: Tuple[float, float, float, float]) -> Optional[_Pick]:
    if wanted.kind not in AREA_KINDS or _not_an_area(wanted.semantic):
        return None
    from skills import regions
    width, height = int(image[0] or 0), int(image[1] or 0)
    rgb = regions.load_rgb(path)
    if rgb is None or not width or not height:
        return None
    # The same floor target.satisfies holds a region to, in this picture's
    # pixels rather than in screen points.
    scale = width / max(1.0, float(crop[2]))
    found = regions.candidates(rgb, min_edge=target.MIN_REGION_EDGE * scale)
    if not found:
        return None
    letters = regions.LETTERS[: len(found)]
    sheet = regions.sheet(path, found, path + ".sheet.png")
    if not sheet:
        return None
    try:
        from skills import screen_context as _sc
        schema = {"type": "object",
                  "properties": {"choice": {"type": "string",
                                            "enum": list(letters) + ["NONE"]},
                                 "reason": {"type": "string"}},
                  "required": ["choice"]}
        raw = _sc.read_image(sheet, _judge_prompt(wanted, letters, anchor),
                             num_predict=80, schema=schema)
        reply = json.loads(raw) if raw and not raw.startswith("Error:") else {}
    except Exception:
        reply = {}
    finally:
        try:
            os.unlink(sheet)
        except OSError:
            pass
    choice = str((reply or {}).get("choice", "")).strip().upper()
    if choice not in letters:
        return _Pick(offered=len(found), choice=choice or "NONE")
    region = found[letters.index(choice)]
    if not _agrees(wanted.semantic, region):
        # Chosen, and contradicted by the pixels: a colour or a shape the
        # words named that this area does not have. Refused, not rescued.
        return _Pick(offered=len(found), choice="NONE")
    from skills import display as _display
    left, top, right, bottom = region.fractions(width, height)
    frame = {"x": crop[0], "y": crop[1], "width": crop[2], "height": crop[3]}
    x0, y0 = _display.image_to_quartz(left, top, frame)
    x1, y1 = _display.image_to_quartz(right, bottom, frame)
    # The area's edge, through the same mapper as its box (B.5).
    edge = tuple(_display.image_to_quartz(px / width, py / height, frame)
                 for px, py in region.outline)
    why = (f"{PICKED} {len(found)} filled areas found in the picture itself, as "
           f"the {wanted.semantic or 'thing asked about'}")
    return _Pick(rect=(x0, y0, x1 - x0, y1 - y0, REGION_CEILING,
                       wanted.semantic, why),
                 offered=len(found), choice=choice, outline=edge)


def _hidden(window: Optional[Dict[str, Any]]
            ) -> Tuple[float, List[Tuple[float, float, float, float]]]:
    """
    How much of a live window other windows cover, and which parts.

    A rectangle capture takes whatever is on top, so these parts of it are
    not this window at all. (0.0, []) for a window that was never listed, or
    when the list cannot be read: nothing is known to be in front of it.
    """
    if not window or not window.get("seen_at"):
        return 0.0, []
    try:
        from skills import screen_context as _sc
        return _sc.covering(window)
    except Exception:
        return 0.0, []


def _blank(path: str, crop: Tuple[float, float, float, float],
           covered: List[Tuple[float, float, float, float]]) -> None:
    """
    Paint the parts of a capture that belong to other windows as blank page.

    Blank rather than cut away, so the picture keeps its size and every
    coordinate mapping after this is unchanged; paper, because paper is what
    the area finder already skips. What another window shows can then be
    neither offered as an answer nor chosen as one.
    """
    if not covered:
        return
    try:
        from skills import regions
        rgb = regions.load_rgb(path)
        if rgb is None:
            return
        cx, cy, cw, ch = crop
        sx, sy = rgb.shape[1] / max(cw, 1e-6), rgb.shape[0] / max(ch, 1e-6)
        painted = False
        for x, y, w, h in covered:
            left, top = max(x, cx), max(y, cy)
            right, bottom = min(x + w, cx + cw), min(y + h, cy + ch)
            if right <= left or bottom <= top:
                continue
            rgb[int((top - cy) * sy):int(round((bottom - cy) * sy)),
                int((left - cx) * sx):int(round((right - cx) * sx))] = 255
            painted = True
        if painted:
            regions.save_rgb(rgb, path)
    except Exception:
        pass


def _by_looking(wanted: target.Target, window) -> Ground:
    """
    The new path: an area of a picture, found by looking at a small part of it.

    The ladder in `target.scope_for` is walked narrowest first and stops at the
    first look that answers. A look that says "not in this crop" is the only
    thing that widens it — a malformed reply or a rejected rectangle does not,
    because widening the picture does not make a broken reply more likely to
    parse.
    """
    from skills import display as _display

    cursor = None
    if wanted.cursor_local or wanted.kind == target.OBJECT:
        try:
            cursor = _display.cursor()
        except Exception:
            cursor = None

    hidden, covered = _hidden(window)
    if hidden > HIDDEN_LIMIT:
        # Checked before the anchor, the capture and the model, all of which
        # would be spent on another window's pixels (B.5.1).
        return Ground(target=wanted, scope=target.WINDOW, abstained=True,
                      note=(f"Most of the {window.get('app') or 'that'} window is "
                            f"behind another window, Sir. Bring it to the front "
                            f"and ask me again."))

    anchor = _anchor_for(wanted, window)
    frame = _frame_for(window)
    evidence: List[str] = []
    if anchor:
        evidence.append(
            f"the words “{str(anchor.get('text', ''))[:40]}” are printed nearby, "
            f"which is where I looked")

    looks, last_scope, widened = 0, "", False
    for scope in target.scope_for(wanted):
        crop = _crop_for(scope, window, anchor, cursor)
        if crop is None:
            continue
        last_scope = scope
        path = _display.capture_region(*crop)
        if not path:
            continue
        looks += 1
        _blank(path, crop, covered)
        sized, iw, ih, temporary = _sized_for_model(path)
        try:
            if not frame.fingerprint:
                try:
                    from skills import screen as _screen
                    frame.fingerprint = _screen._fingerprint(path)
                except Exception:
                    pass
            picked = _pick_area(sized, (iw, ih), wanted, anchor, crop)
            reply = None
            if picked is None or (picked.rect is None and NONE_POLICY == "bbox"):
                reply = _ask_region(sized, _prompt(wanted, window, anchor), crop)
        finally:
            for leftover in ({path} | ({sized} if temporary else set())):
                try:
                    os.unlink(leftover)
                except OSError:
                    pass

        if picked is not None and picked.rect is not None:
            rect = picked.rect
        else:
            if picked is not None:
                evidence.append(f"none of the {picked.offered} areas in the picture "
                                f"was it, looking at them one by one")
                if NONE_POLICY != "bbox":
                    break
            rect = _rect_from(reply, crop, (iw, ih))
        if rect is None:
            # "I cannot see it here" is worth another, wider look. Anything
            # else — a malformed reply, a box off the edge of the crop — is a
            # broken answer, and a bigger picture does not repair one.
            if isinstance(reply, dict) and reply.get("found") is False:
                widened = True
                continue
            if reply is None or not isinstance(reply, dict):
                break
            break

        x, y, w, h, confidence, what, why = rect
        candidate = perception.Candidate(
            label=what or wanted.semantic, role="region",
            x=x, y=y, width=w, height=h, confidence=confidence,
            sources=[target.REGION_SOURCE],
            source_confidence={target.REGION_SOURCE: confidence},
            evidence=evidence + ([why] if why else []) +
                     ([f"it looks like {what}"] if what else []),
            app=(window or {}).get("app", ""),
            window_id=int((window or {}).get("id") or 0),
            vision=perception.VisionEvidence(
                label=what, confidence=confidence, reason=why,
                frame_fingerprint=list(frame.fingerprint), crop=crop,
                at=time.time()))
        if picked is not None and picked.rect is not None and picked.outline:
            candidate.outline = [tuple(p) for p in picked.outline]

        if not target.satisfies(wanted.kind, candidate):
            evidence.append(target.why_not(wanted.kind, candidate))
            widened = True
            continue
        if target.boxed_the_caption(candidate, anchor):
            # The disguised version of the same bug: not the label's rectangle,
            # but a rectangle drawn round the label. Refused for the same
            # reason, and a wider look will not fix it, so this stops.
            evidence.append(
                "the area it gave back is a box around the caption rather "
                "than the thing the caption names")
            break

        return Ground(target=wanted, candidate=candidate, scope=scope,
                      evidence=list(candidate.evidence), looks=looks,
                      frame_fingerprint=list(frame.fingerprint))

    note = target.abstain(wanted)
    if looks == 0:
        note = ("I could not get a look at that part of the screen, Sir.")
    return Ground(target=wanted, scope=last_scope, abstained=True, note=note,
                  evidence=evidence, looks=looks,
                  frame_fingerprint=list(frame.fingerprint))


def debug(question: str, window: Optional[Dict[str, Any]] = None) -> str:
    """What was asked for, where it was looked for, and what came back."""
    out = ground(question, window)
    lines = [f"GROUNDING — {question!r}",
             f"  kind       {out.target.kind}  ({out.target.reason})",
             f"  semantic   {out.target.semantic!r}",
             f"  scope      {out.scope}   looks: {out.looks}   {out.took_ms:.0f}ms"]
    if out.found:
        c = out.candidate
        lines.append(f"  TARGET     {c.x:.0f},{c.y:.0f},{c.width:.0f},{c.height:.0f} "
                     f"at {c.confidence:.2f} from {'+'.join(c.sources)}")
    else:
        lines.append(f"  ABSTAINED  {out.note}")
    for item in out.evidence:
        lines.append(f"  evidence   {item[:76]}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(debug(" ".join(sys.argv[1:]) or "where is the welfare loss region"))
