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

# The reply, in a shape Ollama enforces. Coordinates are normalised INSIDE the
# crop that was sent, never absolute — an absolute coordinate from a model is
# an invented one, and the crop is a rectangle Diablo chose and can check.
REGION_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        # Asked for FIRST, and required. Measured on the diagram fixture, three
        # trials per configuration: asking only for a box found the shaded area
        # in 2 of 10 attempts at a median IoU of 0.01. Asking the model to say
        # where it is in words and only then give the box found it in 11 of 12
        # and raised the median IoU to 0.14 — and the sentence it writes is
        # right even when the rectangle is loose, so it is also the best thing
        # Diablo can say out loud about a region it has only roughly located.
        "where": {"type": "string"},
        "what": {"type": "string"},
        "left": {"type": "number"},
        "top": {"type": "number"},
        "right": {"type": "number"},
        "bottom": {"type": "number"},
        "confidence": {"type": "number"},
    },
    "required": ["where", "found", "left", "top", "right", "bottom",
                 "confidence"],
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
        if anchor:
            return _clip(_around(anchor["left"], anchor["top"], anchor["width"],
                                 anchor["height"], PANEL_PAD), window)
        if cursor:
            return _clip(_around(cursor[0], cursor[1], 1.0, 1.0, PANEL_PAD), window)
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
        hits = pointing.locate(wanted.semantic, (window or {}).get("app", ""), limit=3)
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


def _rect_from(reply: Any, crop: Tuple[float, float, float, float]
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
    try:
        left = float(reply["left"])
        top = float(reply["top"])
        right = float(reply["right"])
        bottom = float(reply["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    confidence = _confidence_of(reply)
    for value in (left, top, right, bottom):
        if value != value or value < -0.02 or value > 1.02:   # NaN, or off the crop
            return None
    left, top = max(left, 0.0), max(top, 0.0)
    right, bottom = min(right, 1.0), min(bottom, 1.0)
    if right - left <= 0.005 or bottom - top <= 0.005:
        return None
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
        job = (f"the filled or marked area that IS “{what}” — the shaded shape, "
               f"the coloured region, the part of the drawing itself. Text "
               f"labels are never the answer.")
    # Words first, box second. The order is the finding: a model asked
    # straight for a rectangle on a diagram mostly says it cannot see one, and
    # the same model asked to describe the place first finds it and then
    # measures it. The sentence is also what Diablo says out loud.
    return (
        f"This is a crop of {app}.{near}\n\n"
        f"First, in `where`, say in plain words where to find {job} — for "
        f"example “the grey triangle just left of where the two lines cross”.\n\n"
        f"Then give that area's bounding box as fractions of THIS image: left "
        f"and right from 0 at the left edge to 1 at the right edge, top and "
        f"bottom from 0 at the top edge to 1 at the bottom edge.\n\n"
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
    reading = perception.read(wanted.semantic, window)
    best = reading.best
    if best is None:
        return Ground(target=wanted, scope=target.CONTROL_SCOPE, abstained=True,
                      note=target.abstain(wanted))
    if reading.ambiguous and reading.question():
        return Ground(target=wanted, scope=target.CONTROL_SCOPE, abstained=True,
                      note=reading.question())
    if not target.satisfies(wanted.kind, best):
        return Ground(target=wanted, scope=target.CONTROL_SCOPE, abstained=True,
                      note=target.abstain(wanted),
                      evidence=[target.why_not(wanted.kind, best)])
    return Ground(target=wanted, candidate=best, scope=target.CONTROL_SCOPE,
                  evidence=list(best.evidence))


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
        try:
            if not frame.fingerprint:
                try:
                    from skills import screen as _screen
                    frame.fingerprint = _screen._fingerprint(path)
                except Exception:
                    pass
            reply = _ask_region(path, _prompt(wanted, window, anchor), crop)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        rect = _rect_from(reply, crop)
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
            vision=perception.VisionEvidence(
                label=what, confidence=confidence, reason=why,
                frame_fingerprint=list(frame.fingerprint), crop=crop,
                at=time.time()))

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
