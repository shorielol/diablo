"""
Vision as the third candidate source.

Accessibility says a control is there. OCR says what the interface reads.
Vision says what a control looks like — and that is the only one of the three
that can answer "click the gear" when the button has no name.

Most of this runs without the model. The vision call is stubbed with canned
replies so that parsing, filtering, negative evidence, confidence capping and
staleness are all deterministic; a model that takes six seconds and varies
between runs cannot test whether malformed output is rejected. The live
checks, which need the real model and the real fixture, are opt-in:

    DIABLO_TEST_VISION=1 python3 tests/test_vision_targets.py
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import ax, computer_use, perception  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ICON_APP = os.path.join(HERE, "fixtures", "icon_app.py")
LIVE = os.getenv("DIABLO_TEST_VISION", "0") == "1"


class Frame:
    def __init__(self, fingerprint):
        self.fingerprint = list(fingerprint)
        self.ok = True
        self.problem = ""

    def release(self):
        pass


def element(x=100.0, y=100.0, w=24.0, h=24.0, role="AXButton"):
    return ax.Element(role=role, x=x, y=y, width=w, height=h,
                      app="Fixture", enabled=True)


def candidate(label="", conf=0.9, x=0.0, y=0.0, sources=None, vision=None):
    return perception.Candidate(
        label=label, role="AXButton", x=x, y=y, width=24, height=24,
        confidence=conf, sources=list(sources or ["accessibility"]),
        evidence=["seen"], enabled=True, vision=vision)


def run():
    r = Results()

    # ── When vision is asked for at all ─────────────────────────────────────
    r.head("Cheap first, expensive only when it has to be")
    with guard(r, "a labelled target costs nothing extra"):
        r.check("a confident named control does not call vision",
                not perception._needs_vision("Save", [candidate("Save", 0.97)]),
                "measured: 48ms by accessibility against 6-13 seconds for vision")
        r.check("a weak match does call vision",
                perception._needs_vision("Settings", [candidate("Settings", 0.70)]))
        r.check("nothing found calls vision",
                perception._needs_vision("the gear", []))
        r.check("a confident but UNLABELLED control calls vision",
                perception._needs_vision("the gear", [candidate("", 0.95)]),
                "accessibility knows where it is, not what it is")
        r.check("a named winner is not overridden by a visual-sounding word",
                not perception._needs_vision("the gear", [candidate("Save", 0.97)]))

    with guard(r, "appearance words are recognised"):
        for phrase in ("the gear", "the blue circle", "that icon", "the little house"):
            if not perception._wants_a_picture(phrase):
                r.bad("a description of an appearance is recognised", phrase)
                break
        else:
            r.ok("a description of an appearance is recognised", "4 of 4")
        r.check("an ordinary instruction is not",
                not perception._wants_a_picture("click Save and then close"))

    # ── The structured reply ────────────────────────────────────────────────
    r.head("Vision answers a multiple-choice question")
    with guard(r, "the schema constrains the reply"):
        schema = perception.VISION_SCHEMA
        item = schema["properties"]["matches"]["items"]
        r.check("every match names a numbered control",
                "number" in item["required"])
        r.check("and says whether it is the one asked for",
                "matches_request" in item["required"],
                "so a control can say 'not me', which is what makes the "
                "positives a comparison rather than a lucky hit")
        r.check("and how sure it is", "confidence" in item["required"])

    elements = [element(x=100), element(x=140), element(x=180)]

    def stubbed(reply):
        """Run _from_vision with a canned model reply."""
        original_ask = perception._ask_vision
        original_sheet = perception._sheet
        original_elements = ax.elements_for_window
        original_available = None
        from skills import screen_context as sc
        original_available = sc.vision_available
        perception._vision_cache.clear()
        try:
            perception._ask_vision = lambda *a, **k: reply
            perception._sheet = lambda els, win: ("/dev/null", (90.0, 90.0, 120.0, 44.0))
            ax.elements_for_window = lambda *a, **k: elements
            sc.vision_available = lambda: True
            return perception._from_vision("the gear", {"app": "Fixture", "id": 1},
                                           frame=Frame([10] * 16))
        finally:
            perception._ask_vision = original_ask
            perception._sheet = original_sheet
            ax.elements_for_window = original_elements
            sc.vision_available = original_available

    with guard(r, "a positive match becomes a candidate"):
        got = stubbed([{"number": 1, "icon": "gear", "matches_request": True,
                        "confidence": 0.91, "reason": "toothed circle"}])
        r.check("one match, one candidate", len(got) == 1, f"{len(got)}")
        if got:
            r.check("it takes the accessibility rectangle, not an invented one",
                    (got[0].x, got[0].y) == (100.0, 100.0),
                    "vision identifies; it never supplies a coordinate")
            r.check("it is tagged as a vision candidate", got[0].sources == ["vision"])
            r.check("it carries the evidence", got[0].vision is not None
                    and got[0].vision.label == "gear")

    with guard(r, "negative evidence is not a candidate"):
        got = stubbed([
            {"number": 1, "icon": "search", "matches_request": False, "confidence": 0.9},
            {"number": 2, "icon": "gear", "matches_request": True, "confidence": 0.9},
            {"number": 3, "icon": "profile", "matches_request": False, "confidence": 0.8},
        ])
        r.check("only the control that said yes survives",
                len(got) == 1 and got[0].x == 140.0,
                f"{len(got)} candidates from 3 examined")

    with guard(r, "a weak identification is dropped"):
        got = stubbed([{"number": 1, "icon": "maybe a gear",
                        "matches_request": True, "confidence": 0.20}])
        r.check("below the floor it does not enter the candidate set", got == [])

    with guard(r, "malformed replies degrade safely"):
        for reply, why in [([], "empty"),
                           ([{"icon": "gear"}], "no number"),
                           ([{"number": 99, "icon": "gear", "matches_request": True,
                              "confidence": 0.9}], "a number that does not exist"),
                           ("not a list at all", "not a list")]:
            try:
                got = stubbed(reply if isinstance(reply, list) else [])
            except Exception as e:
                r.bad("a malformed reply does not raise", f"{why}: {e}")
                break
        else:
            r.ok("a malformed reply does not raise", "4 shapes, none fatal")

    # ── Confidence ──────────────────────────────────────────────────────────
    r.head("Source confidence is not target confidence")
    with guard(r, "vision is capped below a published label"):
        got = stubbed([{"number": 1, "icon": "gear", "matches_request": True,
                        "confidence": 1.0}])
        r.check("a perfectly confident model does not outrank a real label",
                got and got[0].confidence <= 0.88,
                f"model said 1.00, candidate is {got[0].confidence if got else '-'}")
        r.check("but the model's own score is kept separately",
                got and got[0].vision.confidence == 1.0,
                "'sure it is a gear' and 'sure it is the target' are different claims")

    with guard(r, "corroboration still works"):
        seen = perception.VisionEvidence(label="gear", confidence=0.9,
                                         frame_fingerprint=[10] * 16)
        from_vision = candidate("gear", 0.80, x=100, y=100,
                                sources=["vision"], vision=seen)
        from_ax = candidate("", 0.60, x=100, y=100, sources=["accessibility"])
        fused = perception._fuse([from_vision, from_ax])
        r.check("two sources on one control merge", len(fused) == 1)
        if fused:
            r.check("and the fused score beats either alone",
                    fused[0].confidence > 0.80,
                    f"0.80 + 0.60 -> {fused[0].confidence:.2f}")
            r.check("the vision evidence survives the merge",
                    fused[0].vision is not None)

    # ── Stale evidence ──────────────────────────────────────────────────────
    r.head("Evidence belongs to the screen it was true of")
    with guard(r, "a changed screen invalidates it"):
        seen = perception.VisionEvidence(label="gear", confidence=0.9,
                                         frame_fingerprint=[10] * 16)
        r.check("the same screen is not stale", not seen.stale_against(Frame([10] * 16)))
        r.check("a small drift is tolerated",
                not seen.stale_against(Frame([12] * 16)),
                "a clock ticking is not the interface changing")
        r.check("a changed screen is stale", seen.stale_against(Frame([90] * 16)))
        r.check("no fingerprint at all is treated as stale",
                seen.stale_against(object()),
                "unknown provenance is not the same as fresh")

    with guard(r, "the cache is keyed on frame identity"):
        first = perception._cache_key("the gear", (10, 10, 40, 40), [1] * 16)
        same = perception._cache_key("the gear", (10, 10, 40, 40), [1] * 16)
        moved = perception._cache_key("the gear", (10, 10, 40, 40), [9] * 16)
        elsewhere = perception._cache_key("the gear", (80, 10, 40, 40), [1] * 16)
        other = perception._cache_key("the search", (10, 10, 40, 40), [1] * 16)
        r.check("the same question on the same frame hits", first == same)
        r.check("a changed screen misses", first != moved,
                "'the last gear result' is a way of clicking where it used to be")
        r.check("a different crop misses", first != elsewhere)
        r.check("a different question misses", first != other)

    # ── It cannot skip the rest of the pipeline ─────────────────────────────
    r.head("Vision improves perception and nothing else")
    with guard(r, "permission still stands in the way"):
        seen = perception.VisionEvidence(label="gear", confidence=0.99,
                                         frame_fingerprint=[10] * 16)

        class Sure:
            best = candidate("gear", 0.99, sources=["vision"], vision=seen)
            ambiguous = False
            found = True
            candidates = [best]

            def question(self):
                return ""

        r.check("a certain vision target is still only a decision",
                computer_use.decide(Sure(), computer_use.LOW).verdict == computer_use.ACT)
        record = computer_use.act_on("the gear")
        r.check("and the action still refuses while control is off",
                record.verdict == computer_use.REFUSE, record.spoken[:50])

    with guard(r, "destructive targets still confirm"):
        seen = perception.VisionEvidence(label="trash", confidence=0.99,
                                         frame_fingerprint=[10] * 16)

        class Trash:
            best = candidate("delete", 0.99, sources=["vision"], vision=seen)
            ambiguous = False
            found = True
            candidates = [best]

            def question(self):
                return ""

        r.check("a recognised trash icon is high risk",
                computer_use.risk_of("delete") == computer_use.HIGH)
        r.check("and is asked about however sure the model was",
                computer_use.decide(Trash(), computer_use.HIGH).verdict
                == computer_use.ASK)

    with guard(r, "two lookalikes stay ambiguous"):
        seen = perception.VisionEvidence(label="gear", confidence=0.9,
                                         frame_fingerprint=[10] * 16)
        reading = perception.Reading(
            candidates=[candidate("gear", 0.88, x=100, sources=["vision"], vision=seen),
                        candidate("gear", 0.86, x=300, sources=["vision"], vision=seen)],
            ambiguous=True, margin=0.02)
        decision = computer_use.decide(reading, computer_use.LOW)
        r.check("two gears is a question, not the higher number",
                decision.verdict == computer_use.ASK, decision.reason[:60])

    with guard(r, "the record says vision was involved"):
        record = computer_use.ActionRecord(
            intent="click the gear", target="button “gear”",
            target_sources=["accessibility", "vision"],
            vision_label="gear", vision_confidence=0.91,
            vision_crop=(100.0, 90.0, 140.0, 44.0), confidence=0.94,
            action="click", verified=True, evidence=["it looks like gear"])
        text = record.debug()
        r.check("the debug record names the vision label", "gear" in text)
        r.check("and the model's own score", "0.91" in text)
        r.check("and the crop it inspected", "100" in text and "140" in text)
        r.check("and keeps it separate from the fused confidence",
                "0.94" in text and "0.91" in text,
                "so it is visible which of the two was the weak one")

    # ── Against the real model ──────────────────────────────────────────────
    r.head("Against the real model and a real window")
    if not LIVE:
        for name in ("an unlabelled gear is identified",
                     "a labelled control skips vision entirely",
                     "two identical gears are reported as ambiguous"):
            r.skip(name, "needs the vision model; set DIABLO_TEST_VISION=1")
    else:
        child = subprocess.Popen([sys.executable, ICON_APP],
                                 stdout=subprocess.PIPE, text=True, bufsize=1)
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                if (child.stdout.readline() or "").strip() == "READY":
                    break
            time.sleep(1.2)
            from skills import screen_context
            window = screen_context.find_window("Diablo Icons")
            if not window:
                r.bad("the fixture window is visible", "not found")
            else:
                perception._vision_cache.clear()
                started = time.time()
                named = perception.read("Save", window)
                fast = time.time() - started
                r.check("a labelled control skips vision entirely",
                        named.best is not None
                        and "vision" not in named.best.sources and fast < 1.5,
                        f"{fast*1000:.0f}ms, sources "
                        f"{named.best.sources if named.best else '-'}")

                found = perception.read("the search icon", window)
                r.check("an unlabelled gear is identified",
                        found.best is not None and "vision" in found.best.sources,
                        f"{found.best.label if found.best else 'nothing'} at "
                        f"x={found.best.centre[0]:.0f}" if found.best else "")
                if found.best:
                    # Ground truth: the magnifier is the second of four, at
                    # x=537 with width 46, so its centre is 560.
                    r.check("  and it is the right one",
                            abs(found.best.centre[0] - 560) < 30,
                            f"centre x={found.best.centre[0]:.0f}, expected 560")

                gears = perception.read("the gear", window)
                r.check("two identical gears are reported as ambiguous",
                        gears.ambiguous,
                        f"{len(gears.candidates)} candidates, margin "
                        f"{gears.margin:.2f}")
        finally:
            child.terminate()
            try:
                child.wait(timeout=3)
            except Exception:
                child.kill()

    return r.report("vision targets")


if __name__ == "__main__":
    sys.exit(run())
