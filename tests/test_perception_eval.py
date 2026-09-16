#!/usr/bin/env python3
"""
The perception benchmark's own tests.

A benchmark nobody checks is a number nobody should trust. The planner
benchmark shipped with a scorer that credited "Open Safari" to a decision that
clicked Search — it reported 100% and the model had done nothing of the kind.
That is the failure this suite exists to prevent, so most of what follows feeds
the scorer an answer whose correctness is already known and asserts the verdict.

Nothing here launches a fixture or touches the screen except the sections that
say so, which skip when the fixture cannot come up.
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "evals"))
sys.path.insert(0, os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))

from harness import Results, guard  # noqa: E402

import run_perception_eval as ev  # noqa: E402


class FakeCandidate:
    """A Candidate as far as the scorer is concerned."""

    def __init__(self, x, y, width=80.0, height=30.0, label="", confidence=0.9,
                 sources=None):
        self.x, self.y = x, y
        self.width, self.height = width, height
        self.label = label
        self.confidence = confidence
        self.sources = sources or ["accessibility"]

    @property
    def centre(self):
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)


class FakeFixture:
    """Ground truth without an application."""

    SAVE = {"id": "save", "x": 100.0, "y": 200.0, "width": 90.0, "height": 30.0}
    COPY = {"id": "save_copy", "x": 200.0, "y": 200.0, "width": 130.0, "height": 30.0}
    FAR = {"id": "submit", "x": 400.0, "y": 200.0, "width": 90.0, "height": 30.0}

    def __init__(self):
        self.controls = [self.SAVE, self.COPY, self.FAR]

    def control(self, ident):
        for one in self.controls:
            if one["id"] == ident:
                return one
        return None


def at(control, dx=0.0, dy=0.0, **kw):
    """A candidate sitting on a ground-truth control, optionally nudged."""
    return FakeCandidate(control["x"] + dx, control["y"] + dy,
                         control["width"], control["height"], **kw)


def case(expect="save", outcome="found", alternatives=None, category="labelled_button"):
    return {"id": "t", "fixture": "controls_app", "variant": "light",
            "intent": "the Save button", "category": category,
            "expect": expect, "outcome": outcome,
            "alternatives": alternatives or []}


def main() -> int:
    r = Results()
    fixture = FakeFixture()
    S, C, F = FakeFixture.SAVE, FakeFixture.COPY, FakeFixture.FAR

    # ── 1. Corpus ───────────────────────────────────────────────────────────
    r.head("The corpus")
    with guard(r, "the corpus loads"):
        corpus = json.load(open(ev.CASES))
        cases = corpus["cases"]
        r.check("it loads and has cases", len(cases) > 0, f"{len(cases)} cases")
        r.check("every case has the fields the runner reads",
                all({"id", "fixture", "variant", "intent", "category",
                     "expect", "outcome"} <= set(c) for c in cases))
        ids = [c["id"] for c in cases]
        r.check("case ids are unique", len(ids) == len(set(ids)),
                f"{len(ids)} ids, {len(set(ids))} distinct")
        r.check("every outcome is one the scorer understands",
                all(c["outcome"] in ("found", "ambiguous", "abstain") for c in cases))
        r.check("a case that expects an answer names a control",
                all(c["expect"] for c in cases if c["outcome"] != "abstain"))
        r.check("a case that expects abstention names none",
                all(not c["expect"] for c in cases if c["outcome"] == "abstain"))
        r.check("every fixture named actually exists",
                all(os.path.exists(os.path.join(ev.FIXTURES, f"{c['fixture']}.py"))
                    for c in cases),
                ", ".join(sorted({c["fixture"] for c in cases})))

    # ── 2. Geometry ─────────────────────────────────────────────────────────
    r.head("Matching geometry")
    with guard(r, "the measures behave"):
        r.check("a centre inside the box counts as inside",
                ev.inside((145.0, 215.0), S))
        r.check("a centre outside does not",
                not ev.inside((145.0, 400.0), S))
        r.check("the tolerance is a couple of pixels, not a couple of controls",
                ev.inside((S["x"] - 1.5, 215.0), S)
                and not ev.inside((S["x"] - 40.0, 215.0), S))
        r.check("a box against itself is IoU 1.0", abs(ev.iou(S, S) - 1.0) < 1e-9)
        r.check("boxes that do not touch are IoU 0.0", ev.iou(S, F) == 0.0)
        r.check("a half-overlap is between the two",
                0.0 < ev.iou(S, {"x": 145.0, "y": 200.0,
                                 "width": 90.0, "height": 30.0}) < 1.0)
        r.check("distance from the centre of a box to itself is zero",
                ev.distance(ev.centre_of(S), S) == 0.0)
        r.check("which_control names the control a point lands on",
                ev.which_control((145.0, 215.0), fixture.controls) == "save")
        r.check("  and returns nothing for empty space",
                ev.which_control((145.0, 900.0), fixture.controls) is None)

    # ── 3. Top-1, top-3, wrong target ───────────────────────────────────────
    r.head("Verdicts")
    with guard(r, "the scorer classifies what it is given"):
        verdict, _, _ = ev.judge(case(), [at(S)], False, fixture)
        r.check("landing on the expected control is correct", verdict == ev.CORRECT,
                verdict)

        verdict, blame, _ = ev.judge(case(), [at(C), at(S)], False, fixture)
        r.check("the right answer ranked second is top-3, not correct",
                verdict == ev.IN_TOP3, verdict)
        r.check("  and the blame lands on the source that ranked it",
                blame == ev.ACCESSIBILITY, blame)

        verdict, _, _ = ev.judge(case(), [at(F)], False, fixture)
        r.check("landing on a different real control is a wrong target",
                verdict == ev.WRONG_TARGET, verdict)

        verdict, _, _ = ev.judge(
            case(), [FakeCandidate(900.0, 900.0)], False, fixture)
        r.check("landing on nothing at all is off target",
                verdict == ev.OFF_TARGET, verdict)

        verdict, _, _ = ev.judge(case(), [], False, fixture)
        r.check("returning nothing when the control exists is not found",
                verdict == ev.NOT_FOUND, verdict)

        # The exact failure the planner benchmark shipped: a near miss scored
        # as a hit. A candidate one control to the right is not "close".
        verdict, _, _ = ev.judge(case(), [at(C)], False, fixture)
        r.check("a neighbouring control is never partial credit",
                verdict == ev.WRONG_TARGET, verdict)

    # ── 4. Abstention ───────────────────────────────────────────────────────
    r.head("Abstention")
    with guard(r, "silence is judged on whether it was right"):
        verdict, _, _ = ev.judge(case(expect=None, outcome="abstain"), [],
                                 False, fixture)
        r.check("nothing returned for something absent is an abstention",
                verdict == ev.ABSTAINED, verdict)
        verdict, _, _ = ev.judge(case(expect=None, outcome="abstain"),
                                 [at(S)], False, fixture)
        r.check("inventing a control that is not there is a failure",
                verdict == ev.SHOULD_HAVE_ABSTAINED, verdict)
        r.check("  and abstention is not counted as a good outcome by accident",
                ev.SHOULD_HAVE_ABSTAINED not in ev.GOOD
                and ev.ABSTAINED in ev.GOOD)

    # ── 5. Ambiguity ────────────────────────────────────────────────────────
    r.head("Ambiguity")
    with guard(r, "ties are judged as ties"):
        tie = case(expect="save", outcome="ambiguous", alternatives=["save_copy"])
        verdict, _, _ = ev.judge(tie, [at(S), at(C)], True, fixture, "fusion")
        r.check("flagging a genuine tie is correct", verdict == ev.AMBIGUOUS_RIGHT,
                verdict)
        verdict, _, _ = ev.judge(tie, [at(S), at(C)], False, fixture, "fusion")
        r.check("picking one of a genuine tie without asking is a miss",
                verdict == ev.AMBIGUITY_MISSED, verdict)
        verdict, _, _ = ev.judge(case(), [at(S), at(C)], True, fixture, "fusion")
        r.check("asking when the answer was clear is a false ambiguity",
                verdict == ev.FALSE_AMBIGUITY, verdict)

        # A raw source has no way to say "these two are tied", so it must not
        # be marked down for failing to.
        verdict, _, _ = ev.judge(tie, [at(S), at(C)], False, fixture, "ax")
        r.check("a raw source is not punished for ambiguity it cannot express",
                verdict == ev.CORRECT, verdict)
        verdict, _, _ = ev.judge(tie, [at(F)], False, fixture, "ax")
        r.check("  but it is still marked down for the wrong control",
                verdict == ev.WRONG_TARGET, verdict)

    # ── 6. Bounding accuracy ────────────────────────────────────────────────
    r.head("Bounding accuracy")
    with guard(r, "the measurements are recorded"):
        _, _, detail = ev.judge(case(), [at(S)], False, fixture)
        r.check("an exact box reports no centre error",
                detail.get("centre_error_px") == 0.0, str(detail.get("centre_error_px")))
        r.check("  and IoU 1.0", abs(detail.get("iou", 0) - 1.0) < 1e-6,
                str(detail.get("iou")))
        _, _, detail = ev.judge(case(), [at(S, dx=6.0)], False, fixture)
        r.check("a shifted box reports the shift",
                detail.get("centre_error_px") == 6.0, str(detail.get("centre_error_px")))
        r.check("  and an IoU below 1.0", detail.get("iou", 1.0) < 1.0,
                str(detail.get("iou")))
        r.check("a centre that is right with a box that is wrong is still correct",
                ev.judge(case(), [FakeCandidate(S["x"] + 15.0, S["y"] + 5.0,
                                                60.0, 20.0)], False, fixture)[0]
                == ev.CORRECT,
                "OCR reports a point padded to 60x20; the click still lands")

    # ── 7. Attribution ──────────────────────────────────────────────────────
    r.head("Failure attribution")
    with guard(r, "blame lands on the right source"):
        for sources, expected in ((["accessibility"], ev.ACCESSIBILITY),
                                  (["ocr"], ev.OCR),
                                  (["vision"], ev.VISION),
                                  (["accessibility", "vision"], ev.FUSION)):
            _, blame, _ = ev.judge(case(), [at(F, sources=sources)], False, fixture)
            r.check(f"a wrong answer from {'+'.join(sources)} is blamed on {expected}",
                    blame == expected, blame)
        r.check("nothing is blamed when nothing went wrong",
                ev.judge(case(), [at(S)], False, fixture)[1] == "")

    # ── 8. Summary arithmetic ───────────────────────────────────────────────
    r.head("The summary")
    with guard(r, "the numbers add up"):
        records = [
            {"source": "ax", "verdict": ev.CORRECT, "good": True, "ms": 10.0},
            {"source": "ax", "verdict": ev.CORRECT, "good": True, "ms": 20.0},
            {"source": "ax", "verdict": ev.WRONG_TARGET, "good": False, "ms": 30.0},
            {"source": "ax", "verdict": ev.NOT_FOUND, "good": False, "ms": 1.0},
        ]
        s = ev.summarise(records, "ax")
        r.check("top-1 counts only the correct ones", s["correct"] == 2, str(s["correct"]))
        r.check("silence is not counted as an answer", s["answered"] == 3,
                str(s["answered"]))
        # 1e-3, not 1e-6: the summary rounds to three places so the JSON stays
        # readable, and the test should not argue with that.
        r.check("precision is over what it answered, not over everything",
                abs(s["precision"] - 2 / 3) < 1e-3, str(s["precision"]))
        r.check("a source that answers nothing has no precision, not zero",
                ev.summarise([{"source": "v", "verdict": ev.NOT_FOUND,
                               "good": False, "ms": 1.0}], "v")["precision"] is None)
        r.check("engaged latency ignores the instant declines",
                s["median_ms_engaged"] == 20.0, str(s["median_ms_engaged"]))
        r.check("overall latency does not", s["median_ms"] == 15.0, str(s["median_ms"]))

    # ── 9. Context attribution ──────────────────────────────────────────────
    r.head("Context attribution")
    with guard(r, "benchmark records can be traced back"):
        import tempfile
        from skills import trace
        path = tempfile.mktemp(suffix=".jsonl")
        was, trace.PATH = trace.PATH, path
        try:
            trace.new_turn("test")
            with trace.scope(run="r1", scenario="btn-save", source="ax"):
                trace.event("perception", verdict="correct")
            rows = [json.loads(line) for line in open(path)]
        finally:
            trace.PATH = was
        tagged = [row for row in rows if row.get("stage") == "perception"]
        r.check("a scoped event carries the run", tagged and tagged[0].get("run") == "r1")
        r.check("  the scenario", tagged and tagged[0].get("scenario") == "btn-save")
        r.check("  and the source", tagged and tagged[0].get("source") == "ax")
        r.check("  and still carries its turn",
                tagged and tagged[0].get("turn"))
        r.check("the binding does not leak out of the block",
                trace.bound() == {}, str(trace.bound()))

    with guard(r, "context crosses a thread when it is carried"):
        import threading
        import tempfile
        from skills import trace
        path = tempfile.mktemp(suffix=".jsonl")
        was, trace.PATH = trace.PATH, path
        try:
            turn = trace.new_turn("test")
            plain = {}
            threading.Thread(
                target=lambda: plain.setdefault("turn", trace.current_turn())
            ).start()
            time.sleep(0.2)
            carried = {}
            ctx = trace.carry()
            threading.Thread(
                target=lambda: ctx.run(
                    lambda: carried.setdefault("turn", trace.current_turn()))
            ).start()
            time.sleep(0.2)
        finally:
            trace.PATH = was
        # Recorded rather than asserted away: a plain thread inherits nothing,
        # which is exactly why roster agents lost 59% of their trace events and
        # why swapping the mechanism alone would not have fixed it.
        r.check("a plain thread inherits no context",
                plain.get("turn") is None, str(plain.get("turn")))
        r.check("a carried context does", carried.get("turn") == turn,
                f"{carried.get('turn')} vs {turn}")

    # ── 10. Live fixture ────────────────────────────────────────────────────
    r.head("Against the real fixture")
    fixture_live = ev.Fixture("controls_app", "light")
    started = False
    try:
        started = fixture_live.start()
        if not started:
            for name in ("the fixture declares its own layout",
                         "ground truth is not derived from perception",
                         "accessibility agrees with the declared layout",
                         "a screen fingerprint accompanies the reading",
                         "stale evidence is rejected against a new frame",
                         "the vision cache is keyed on the frame"):
                r.skip(name, "the fixture window did not come up")
        else:
            controls = fixture_live.controls
            r.check("the fixture declares its own layout",
                    len(controls) == 11, f"{len(controls)} controls")
            r.check("ground truth is not derived from perception",
                    all(set(c) >= {"x", "y", "width", "height", "role"}
                        for c in controls)
                    and all(c["width"] > 0 and c["height"] > 0 for c in controls))

            from skills import ax, screen
            elements = ax.elements_for_window(fixture_live.window,
                                              actionable_only=False)
            named = {e.title: e for e in elements if e.title}
            save = fixture_live.control("save")
            reported = named.get("Save")
            r.check("accessibility agrees with the declared layout",
                    reported is not None
                    and abs((reported.x + reported.width / 2.0)
                            - (save["x"] + save["width"] / 2.0)) < 3.0,
                    f"ax centre {reported.x + reported.width / 2.0:.1f} vs "
                    f"declared {save['x'] + save['width'] / 2.0:.1f}"
                    if reported else "Save not reported")
            r.check("  and reports the disabled control as disabled",
                    named.get("Archive") is not None
                    and named["Archive"].enabled is False,
                    str(named.get("Archive").enabled if named.get("Archive") else "-"))

            frame = screen.capture_this_window(fixture_live.window)
            try:
                r.check("a screen fingerprint accompanies the reading",
                        bool(frame.fingerprint) and frame.window_id != 0,
                        f"{len(frame.fingerprint)} buckets, "
                        f"window {frame.window_id}, scale {frame.display_scale}")
                r.check("  and it records which display it came from",
                        frame.display_index >= 1, str(frame.display_index))
            finally:
                frame.release()

            # Stale evidence: a reading taken against one frame must not be
            # accepted against a frame the screen has moved on from.
            from skills import perception
            evidence = perception.VisionEvidence(
                label="gear", confidence=0.9, reason="test",
                frame_fingerprint=[1] * 16, crop=(0, 0, 10, 10), at=time.time())
            moved = screen.ScreenFrame(path=None, captured_at=time.time(),
                                       source="window", fingerprint=[99] * 16)
            same = screen.ScreenFrame(path=None, captured_at=time.time(),
                                      source="window", fingerprint=[1] * 16)
            r.check("stale evidence is rejected against a new frame",
                    evidence.stale_against(moved), "fingerprint moved")
            r.check("  and accepted against the frame it came from",
                    not evidence.stale_against(same))

            # Cache: the same question against the same frame should not cost
            # a second model call.
            perception._vision_cache.clear()
            key_one = perception._cache_key("the gear", (0, 0, 10, 10), [1] * 16)
            key_two = perception._cache_key("the gear", (0, 0, 10, 10), [2] * 16)
            r.check("the vision cache is keyed on the frame",
                    key_one != key_two,
                    "a different screen is a different question")
            r.check("  and the same frame is the same key",
                    key_one == perception._cache_key("the gear", (0, 0, 10, 10),
                                                     [1] * 16))
    finally:
        fixture_live.stop()

    # ── 11. Reproducibility ─────────────────────────────────────────────────
    r.head("Reproducibility")
    with guard(r, "the same input scores the same way"):
        verdicts = {ev.judge(case(), [at(S)], False, fixture)[0] for _ in range(20)}
        r.check("judging is deterministic", len(verdicts) == 1, str(verdicts))
        r.check("the scorer never consults a model",
                "chat" not in open(ev.__file__).read()
                and "local_brain" not in open(ev.__file__).read(),
                "no model call in the scorer")

    return r.report("perception eval")


if __name__ == "__main__":
    sys.exit(main())
