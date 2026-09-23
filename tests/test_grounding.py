"""
Question-directed screen understanding: the thing, not the word for the thing.

The failure this suite exists for was observed out loud. Asked "where is the
welfare loss region?", Diablo found the words "welfare loss" printed on the
diagram and drew its ring around the label. The label is not the region. It is
a caption sitting somewhere near it, and on a real economics diagram it is
often nowhere near it at all.

Nothing in the perception stack could have done better, because the target was
a bare string the whole way down. `point_at("the welfare loss region")` became
`perception.read(intent)`, and all three of its sources return the geometry of
their own evidence: accessibility returns a control's rectangle, OCR returns
the matched text fragment's rectangle, vision returns an unlabelled control's
rectangle. A text match therefore WAS a pointing target, with nothing in
between to ask whether the kind of thing found is the kind of thing wanted.

So these tests are about two new things and nothing else:

  WHAT KIND        the question says whether the answer should be a control, a
                   piece of text, a region of a picture, a whole figure, the
                   thing under the cursor, or the screen itself.
  DOES IT FIT      a candidate becomes the target only if its geometry is the
                   kind that was asked for. A label may point at a region. It
                   may not be one.

The model is stubbed everywhere below. A vision call that takes six seconds and
varies between runs cannot test whether an out-of-range rectangle is rejected.
The live half needs the real model and the real diagram fixture:

    DIABLO_TEST_VISION=1 python3 tests/test_grounding.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import computer_use, grounding, perception, target  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The diagram, as a window: a 900x600 chart sitting at 400,200 on the desktop.
WINDOW = {"id": 7, "app": "Preview", "title": "welfare.pdf",
          "x": 400.0, "y": 200.0, "width": 900.0, "height": 600.0}

# Ground truth. The legend is bottom-right; the shaded triangle it names is up
# and left of it, 300 points away. Pointing at one is measurably not the other.
LABEL = {"x": 1060.0, "y": 700.0, "w": 120.0, "h": 16.0}
REGION = {"x": 640.0, "y": 420.0, "w": 180.0, "h": 150.0}


class Frame:
    """A ScreenFrame as far as staleness is concerned."""

    def __init__(self, fingerprint=None):
        self.fingerprint = list(fingerprint or [10] * 16)
        self.ok = True
        self.problem = ""
        self.path = "/dev/null"

    def release(self):
        pass


def anchor(text="Welfare loss", box=None):
    """One OCR hit, in the shape pointing.locate returns."""
    box = box or LABEL
    return {"text": text, "x": box["x"] + box["w"] / 2.0,
            "y": box["y"] + box["h"] / 2.0,
            "left": box["x"], "top": box["y"],
            "width": box["w"], "height": box["h"],
            "score": 0.97, "confidence": 0.94, "reading_index": 3,
            "app": WINDOW["app"], "window": WINDOW["title"],
            "window_id": WINDOW["id"]}


def region_reply(box=None, found=True, confidence=0.86, what="shaded triangle"):
    """What the vision model says, in the crop's own normalised space."""
    return {"found": found, "what": what, "confidence": confidence,
            "reason": "grey wedge between the two curves above the price line",
            "left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0} if box is None else \
           dict(found=found, what=what, confidence=confidence,
                reason="grey wedge between the two curves", **box)


def grounded(question, hits=None, reply=None, crop_used=None, frame=None,
             cursor=None, stated=""):
    """
    Run the grounder with every expensive thing stubbed.

    Anchors, capture and the model are replaced; the geometry, the validation
    and the escalation are the real ones, because those are what is under test.
    """
    from skills import display, pointing
    taken = {}
    originals = (pointing.locate, display.capture_region, display.cursor,
                 grounding._ask_region, grounding._frame_for)
    try:
        pointing.locate = lambda *a, **k: list(hits if hits is not None else [anchor()])
        def capture(x, y, w, h):
            taken["crop"] = (x, y, w, h)
            return "/dev/null"
        display.capture_region = capture
        display.cursor = lambda: cursor
        replies = list(reply) if isinstance(reply, list) else [reply]
        def ask(path, question_text, crop):
            taken.setdefault("asked", []).append(crop)
            got = replies[min(len(taken["asked"]), len(replies)) - 1]
            return got
        grounding._ask_region = ask
        grounding._frame_for = lambda window: frame or Frame()
        got = grounding.ground(question, WINDOW, stated_kind=stated)
        got.taken = taken
        return got
    finally:
        (pointing.locate, display.capture_region, display.cursor,
         grounding._ask_region, grounding._frame_for) = originals


def run():
    r = Results()

    # ── The question decides what kind of answer is wanted ──────────────────
    r.head("A question names a kind of thing, not just a string")
    corpus = [
        ("A  control",          "where is the File menu",              target.CONTROL),
        ("B  exact text",       "read this paragraph",                 target.TEXT),
        ("C  local object",     "what is this",                        target.OBJECT),
        ("D  diagram region",   "where is the shaded area on this graph", target.REGION),
        ("E  labelled region",  "where is the welfare loss region",    target.REGION),
        ("F  unlabelled region","where is the area between the two curves", target.REGION),
        ("G  graph component",  "what does this graph show",           target.FIGURE),
        ("H  cursor question",  "explain this",                        target.OBJECT),
        ("I  whole screen",     "what's on my screen",                 target.SCREEN),
        ("K  label trap",       "where is consumer surplus",           target.REGION),
        ("L  related-text trap","where is the deadweight loss triangle", target.REGION),
    ]
    with guard(r, "classification"):
        for name, question, expected in corpus:
            got = target.read_question(question)
            r.check(f"{name}: {question!r}", got.kind == expected,
                    f"wanted {expected}, got {got.kind} — {got.reason}")

    with guard(r, "the semantic survives the scaffolding"):
        got = target.read_question("where is the welfare loss region")
        r.check("the question words are stripped off the thing asked for",
                "welfare loss" in got.semantic.lower()
                and "where is" not in got.semantic.lower(),
                repr(got.semantic))
        r.check("but what kind it is survives too", got.kind == target.REGION)

    with guard(r, "J  ambiguous: a question with no thing in it"):
        got = target.read_question("where is it")
        r.check("a question that names nothing is marked vague", got.vague,
                repr(got.semantic))
        r.check("and a vague target is not pointable", not got.pointable)

    with guard(r, "the decision layer may say the kind outright"):
        got = target.read_question("where is welfare loss", stated="region")
        r.check("a stated kind is taken", got.kind == target.REGION)
        r.check("and recorded as stated, not guessed", "stated" in got.reason)
        r.check("an unknown stated kind falls back to the phrasing",
                target.read_question("where is the File menu",
                                     stated="nonsense").kind == target.CONTROL)

    # ── Which instruments a question needs ──────────────────────────────────
    r.head("The question decides which evidence matters")
    with guard(r, "source selection"):
        r.check("a control asks the application first",
                target.sources_for(target.CONTROL)[0] == target.ACCESSIBILITY)
        r.check("text asks OCR", target.OCR in target.sources_for(target.TEXT))
        r.check("a region needs to look at pixels",
                target.VISION in target.sources_for(target.REGION))
        r.check("and a region does not resolve on accessibility alone",
                target.ACCESSIBILITY not in target.sources_for(target.REGION),
                "there are no accessibility elements inside a PDF diagram")
        r.check("a figure needs to look too",
                target.VISION in target.sources_for(target.FIGURE))

    with guard(r, "progressive scope"):
        near = target.scope_for(target.read_question("what is this"))
        r.check("a cursor question starts at the cursor",
                near[0] == target.CURSOR, str(near))
        r.check("and can widen if that is not enough", len(near) > 1, str(near))
        wide = target.scope_for(target.read_question("where is the welfare loss region"))
        r.check("a named region starts at the panel around its caption",
                wide[0] == target.PANEL, str(wide))
        r.check("and not at the caption itself",
                target.ANCHOR not in wide,
                "measured on the fixture: the legend is 396pt from the shape "
                "it names, so a crop centred on the caption is faster and "
                "cannot contain the answer")
        r.check("the full screen is the last resort, never the first",
                wide[-1] == target.SCREEN_SCOPE and wide[0] != target.SCREEN_SCOPE,
                str(wide))

    # ── The rule the welfare-loss bug broke ─────────────────────────────────
    r.head("A label may point at a region. It may not be one.")
    label_candidate = perception.Candidate(
        label="Welfare loss", role="text",
        x=LABEL["x"], y=LABEL["y"], width=LABEL["w"], height=LABEL["h"],
        confidence=0.97, sources=["ocr"], source_confidence={"ocr": 0.97})
    region_candidate = perception.Candidate(
        label="the welfare loss region", role="region",
        x=REGION["x"], y=REGION["y"], width=REGION["w"], height=REGION["h"],
        confidence=0.86, sources=["region"], source_confidence={"region": 0.86})
    control_candidate = perception.Candidate(
        label="File", role="AXMenuBarItem", x=12.0, y=4.0, width=38.0, height=22.0,
        confidence=0.99, sources=["accessibility"])

    with guard(r, "validation by kind"):
        r.check("an OCR text hit does NOT satisfy a region request",
                not target.satisfies(target.REGION, label_candidate),
                "this one line is the whole bug")
        r.check("and says why", "label" in
                target.why_not(target.REGION, label_candidate).lower(),
                target.why_not(target.REGION, label_candidate))
        r.check("a real region does satisfy it",
                target.satisfies(target.REGION, region_candidate))
        r.check("a control satisfies a control request",
                target.satisfies(target.CONTROL, control_candidate))
        r.check("a text hit still satisfies a text request",
                target.satisfies(target.TEXT, label_candidate),
                "reading a paragraph is exactly a text target")
        r.check("a text hit satisfies a control request too",
                target.satisfies(target.CONTROL, label_candidate),
                "a button's printed label IS the button; do not regress apps "
                "with no accessibility tree")
        r.check("a degenerate region is not a region",
                not target.satisfies(target.REGION, perception.Candidate(
                    label="x", role="region", x=1, y=1, width=0, height=0,
                    sources=["region"])))

    # ── The failure, end to end ─────────────────────────────────────────────
    r.head("Where is the welfare loss region?")
    with guard(r, "the region, not the label"):
        got = grounded("where is the welfare loss region",
                       reply=region_reply())
        r.check("it resolves", got.found, got.note)
        if got.found:
            box = got.candidate
            r.check("the target is not the label",
                    abs(box.x - LABEL["x"]) > 40 or abs(box.y - LABEL["y"]) > 40,
                    f"target at ({box.x:.0f},{box.y:.0f}), "
                    f"label at ({LABEL['x']:.0f},{LABEL['y']:.0f})")
            r.check("the target has the extent of a region, not of a line of type",
                    box.height > LABEL["h"] * 3,
                    f"{box.height:.0f}pt tall against a {LABEL['h']:.0f}pt label")
            r.check("it is tagged as a region candidate",
                    "region" in box.sources, str(box.sources))
            r.check("the label is kept as evidence, not as the answer",
                    any("welfare loss" in e.lower() for e in box.evidence),
                    "; ".join(box.evidence)[:90])
            r.check("and the validated target passes its own kind check",
                    target.satisfies(target.REGION, box))

    with guard(r, "the label anchors the crop instead of becoming the answer"):
        got = grounded("where is the welfare loss region", reply=region_reply())
        crop = got.taken.get("crop")
        r.check("something narrower than the window was captured",
                crop is not None and crop[2] < WINDOW["width"],
                str(crop))
        r.check("and the crop contains the label it anchored on",
                crop is not None
                and crop[0] <= LABEL["x"] <= crop[0] + crop[2]
                and crop[1] <= LABEL["y"] <= crop[1] + crop[3],
                str(crop))
        r.check("and it contains the region too, which is the point",
                crop is not None
                and crop[0] <= REGION["x"] <= crop[0] + crop[2]
                and crop[1] <= REGION["y"] <= crop[1] + crop[3],
                str(crop))

    with guard(r, "a box drawn round the caption is the same bug in disguise"):
        got = grounded("where is the welfare loss region",
                       reply=region_reply({"left": 0.71, "top": 0.82,
                                           "right": 0.87, "bottom": 0.88}))
        r.check("it is refused", not got.found,
                str(got.candidate and (got.candidate.x, got.candidate.width)))
        r.check("and it is not widened into a second look",
                len(got.taken.get("asked", [])) == 1,
                f"{len(got.taken.get('asked', []))} looks")

    with guard(r, "L  a related sentence is not the thing either"):
        sentence = anchor("Consumer surplus is the area above the price line",
                          {"x": 500.0, "y": 760.0, "w": 340.0, "h": 14.0})
        got = grounded("where is the consumer surplus region",
                       hits=[sentence],
                       reply=region_reply({"left": 0.1, "top": 0.1,
                                           "right": 0.5, "bottom": 0.6}))
        if got.found:
            r.check("the prose describing it does not become the target",
                    abs(got.candidate.y - sentence["top"]) > 30
                    or got.candidate.height > sentence["height"] * 3,
                    f"target {got.candidate.height:.0f}pt tall at "
                    f"y={got.candidate.y:.0f}, sentence at y={sentence['top']:.0f}")
        else:
            r.check("the prose describing it does not become the target", True,
                    "abstained, which is also correct")

    # ── Nothing about controls changes ──────────────────────────────────────
    r.head("Where is File?")
    with guard(r, "the control path is the one that already worked"):
        from skills import perception as p
        original = p.read
        seen = {}
        try:
            def fake(intent, window=None):
                seen["intent"] = intent
                return p.Reading(candidates=[control_candidate], margin=1.0)
            p.read = fake
            got = grounding.ground("where is the File menu", WINDOW)
        finally:
            p.read = original
        r.check("it goes through the existing perception read",
                "intent" in seen, "no second perception engine")
        r.check("asked for what the user named", "file" in seen.get("intent", "").lower(),
                repr(seen.get("intent")))
        r.check("the File control is still the target", got.found
                and got.candidate.label == "File")
        r.check("and no capture was taken for it", got.scope == target.CONTROL_SCOPE,
                got.scope)

    # ── Abstention ──────────────────────────────────────────────────────────
    r.head("Not finding it is an answer")
    with guard(r, "the model says it cannot see it"):
        got = grounded("where is the welfare loss region",
                       reply=region_reply(found=False))
        r.check("nothing is returned", not got.found)
        r.check("it is recorded as an abstention, not a failure", got.abstained)
        r.check("and it does not fall back to the label",
                got.candidate is None, str(got.candidate))

    with guard(r, "a confidence on the wrong scale is not a certainty"):
        r.check("100 is read as 1.0, not as a hundredfold certainty",
                abs(grounding._confidence_of({"confidence": 100}) - 1.0) < 1e-9,
                "qwen2.5vl returns a percentage about as often as a fraction")
        r.check("0.95 is still 0.95",
                abs(grounding._confidence_of({"confidence": 0.95}) - 0.95) < 1e-9)
        for bad in ({}, {"confidence": "a lot"}, {"confidence": float("nan")},
                    {"confidence": -4}):
            if grounding._confidence_of(bad) != 0.0:
                r.bad("nonsense reads as no confidence at all", repr(bad))
                break
        else:
            r.ok("nonsense reads as no confidence at all", "4 shapes")

    with guard(r, "the model is asked to say where before measuring where"):
        r.check("`where` is a required field",
                "where" in grounding.REGION_SCHEMA["required"],
                "measured: box-only found it 2 of 10, words-then-box 11 of 12")
        prompt = grounding._prompt(
            target.read_question("where is the welfare loss region"), WINDOW,
            anchor())
        r.check("and the prompt asks for it first",
                prompt.index("`where`") < prompt.index("bounding box"))
        r.check("the caption is named as a caption, not as the answer",
                "NOT the thing itself" in prompt)
        got = grounded("where is the welfare loss region", reply=region_reply())
        r.check("the words it wrote are carried as evidence",
                got.found and any("wedge" in e for e in got.candidate.evidence),
                "; ".join(got.candidate.evidence)[:80] if got.found else "-")

    with guard(r, "a weak identification abstains"):
        got = grounded("where is the welfare loss region",
                       reply=region_reply(confidence=0.12))
        r.check("below the floor nothing is pointed at", not got.found)

    with guard(r, "an impossible rectangle is refused"):
        for box, why in [({"left": 0.6, "top": 0.1, "right": 0.2, "bottom": 0.4}, "inverted"),
                         ({"left": -3.0, "top": 0.1, "right": 9.0, "bottom": 0.4}, "outside the crop"),
                         ({"left": 0.5, "top": 0.5, "right": 0.5, "bottom": 0.5}, "zero area")]:
            got = grounded("where is the welfare loss region", reply=region_reply(box))
            if got.found:
                r.bad("an impossible rectangle is refused", why)
                break
        else:
            r.ok("an impossible rectangle is refused", "3 shapes, none accepted")

    with guard(r, "a malformed reply does not raise"):
        for reply in (None, {}, {"found": True}, "not a dict", {"found": True,
                      "left": "a", "top": 0.1, "right": 0.5, "bottom": 0.4}):
            try:
                got = grounded("where is the welfare loss region", reply=reply)
                if got.found:
                    r.bad("a malformed reply does not become a target", repr(reply)[:40])
                    break
            except Exception as e:
                r.bad("a malformed reply does not raise", f"{reply!r}: {e}")
                break
        else:
            r.ok("a malformed reply does not raise", "5 shapes, none fatal")

    with guard(r, "nothing to anchor on"):
        got = grounded("where is the welfare loss region", hits=[],
                       reply=region_reply())
        r.check("with no anchor it still tries a wider scope",
                len(got.taken.get("asked", [])) >= 1, str(got.taken.get("asked")))

    with guard(r, "J  a vague question asks rather than guesses"):
        got = grounding.ground("where is it", WINDOW)
        r.check("nothing is pointed at", not got.found)
        r.check("and there is something to say back", bool(got.note), got.note)

    with guard(r, "I  a whole-screen question has no single target"):
        got = grounding.ground("what's on my screen", WINDOW)
        r.check("it does not invent one", not got.found)
        r.check("and it says that is what happened",
                got.abstained and "screen" in got.note.lower(), got.note)

    # ── Escalation ──────────────────────────────────────────────────────────
    r.head("Widen only when the narrow look could not answer")
    with guard(r, "one look is enough when it answers"):
        got = grounded("where is the welfare loss region", reply=region_reply())
        r.check("the narrow crop is not widened after a good answer",
                len(got.taken.get("asked", [])) == 1,
                f"{len(got.taken.get('asked', []))} looks")

    with guard(r, "a failed narrow look widens"):
        got = grounded("where is the welfare loss region",
                       reply=[region_reply(found=False), region_reply()])
        asked = got.taken.get("asked", [])
        r.check("it looks again", len(asked) >= 2, f"{len(asked)} looks")
        if len(asked) >= 2:
            r.check("and the second look is wider than the first",
                    asked[1][2] * asked[1][3] > asked[0][2] * asked[0][3],
                    f"{asked[0][2]:.0f}x{asked[0][3]:.0f} then "
                    f"{asked[1][2]:.0f}x{asked[1][3]:.0f}")

    with guard(r, "H  the cursor is evidence, not truth"):
        got = grounded("what is this", cursor=(700.0, 500.0),
                       reply=region_reply())
        crop = got.taken.get("crop")
        r.check("a cursor question looks near the cursor first",
                crop is not None and crop[0] <= 700.0 <= crop[0] + crop[2]
                and crop[1] <= 500.0 <= crop[1] + crop[3], str(crop))
        r.check("the crop is small, not the whole window",
                crop is not None and crop[2] < WINDOW["width"] / 2, str(crop))
        without = grounded("what is this", cursor=None, reply=region_reply())
        r.check("with no cursor it still works from the window",
                without.found or without.abstained, without.note)

    # ── Staleness ───────────────────────────────────────────────────────────
    r.head("A target belongs to the screen it was found on")
    with guard(r, "the existing fingerprint mechanism"):
        got = grounded("where is the welfare loss region", reply=region_reply(),
                       frame=Frame([10] * 16))
        r.check("the reading carries the frame it was true of",
                bool(got.frame_fingerprint), str(got.frame_fingerprint)[:30])
        if got.found and got.candidate.vision is not None:
            r.check("the same screen is not stale",
                    not got.candidate.vision.stale_against(Frame([10] * 16)))
            r.check("a changed screen is",
                    got.candidate.vision.stale_against(Frame([90] * 16)))

    # ── Safety ──────────────────────────────────────────────────────────────
    r.head("Observation only")
    with guard(r, "nothing here can act"):
        r.check("computer use is still disabled", computer_use.ENABLED is False)
        source = ""
        for name in ("grounding.py", "target.py"):
            with open(os.path.join(ROOT, "diablo", name)) as f:
                source += f.read()
        for forbidden in ("CGEventPost", "_post_click", "CGEventCreateMouseEvent",
                          "CGEventCreateKeyboardEvent", "CGWarpMouseCursorPosition"):
            if forbidden in source:
                r.bad("no synthetic event anywhere in this build", forbidden)
                break
        else:
            r.ok("no synthetic event anywhere in this build", "5 symbols, none present")
        r.check("and no do_engine execution path was added",
                "do_engine" not in source)

    # ── The real diagram ────────────────────────────────────────────────────
    r.head("The fixture, for real")
    fixture = os.path.join(HERE, "fixtures", "diagram_app.py")
    r.check("there is a deterministic diagram to point at",
            os.path.exists(fixture), fixture)
    if os.getenv("DIABLO_TEST_VISION", "0") != "1":
        r.skip("the live look at it", "set DIABLO_TEST_VISION=1")

    return r.report("grounding")


if __name__ == "__main__":
    sys.exit(run())
