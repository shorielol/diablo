#!/usr/bin/env python3
"""
Build 2 — how arbitration decides between candidates.

The rule under test, stated once so the assertions can be read against it:

    required = SEPARATION * (1 - basis)        SEPARATION = 2 * AMBIGUOUS_WITHIN
    resolved = separation > required           strictly greater

where `basis` and `separation` come from the fused figures when the top
candidate's corroboration distinguishes it from the runner-up, and from each
candidate's strongest single source when it does not.

Two claims that must hold whatever else changes:

  identical evidence never resolves      strict inequality, required can be 0
  wrong targets never increase           asserted against the real corpus

These tests encode the principle, not the current numbers. Where a boundary
value appears it is derived from the rule in the test itself, so a change to
SEPARATION moves the tests with it rather than breaking them.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "evals"))
sys.path.insert(0, os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))

from harness import Results, guard  # noqa: E402

from skills import agent_tools, perception as P  # noqa: E402


def cand(conf, x=100.0, label="", sources=("accessibility",), single=None,
         vision=None):
    """One candidate, positioned far from the others unless told otherwise."""
    names = list(sources)
    return P.Candidate(
        label=label or f"c{int(conf * 1000)}", role="AXButton",
        x=x, y=0.0, width=10.0, height=10.0, confidence=conf,
        sources=names, evidence=["test"],
        source_confidence=dict(single) if single else {n: conf for n in names},
        vision=vision)


def verdict_of(*candidates):
    """Arbitration's answer for a ranked list, without touching the screen."""
    ranked = sorted(candidates, key=lambda c: -c.confidence)
    margin = (ranked[0].confidence - ranked[1].confidence
              if len(ranked) > 1 else 1.0)
    top = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    separation, basis = P._separation(top, runner, margin)
    decisive = separation > P.required_margin(basis)
    return P.Reading(candidates=ranked,
                     ambiguous=len(ranked) > 1 and not decisive,
                     margin=margin)


class FakeVision:
    def __init__(self, fingerprint):
        self.frame_fingerprint = list(fingerprint)
        self.reason = "test"


def main() -> int:
    r = Results()

    # ── the rule itself ─────────────────────────────────────────────────────
    r.head("The separation rule")
    with guard(r, "it is derived, not invented"):
        r.check("SEPARATION is twice the old flat threshold",
                abs(P.SEPARATION - P.AMBIGUOUS_WITHIN * 2) < 1e-9,
                f"{P.SEPARATION} vs {P.AMBIGUOUS_WITHIN}")
        r.check("so at a top confidence of 0.5 it asks for exactly the old value",
                abs(P.required_margin(0.5) - P.AMBIGUOUS_WITHIN) < 1e-9,
                f"{P.required_margin(0.5):.3f}")
        r.check("it falls to zero at certainty", P.required_margin(1.0) == 0.0)
        r.check("it is highest at no confidence",
                abs(P.required_margin(0.0) - P.SEPARATION) < 1e-9)
        r.check("it is monotonic in confidence",
                all(P.required_margin(a / 20) >= P.required_margin((a + 1) / 20)
                    for a in range(20)))
        r.check("it is bounded by SEPARATION for any input",
                all(0.0 <= P.required_margin(v) <= P.SEPARATION
                    for v in (-5.0, 0.0, 0.3, 1.0, 9.0)),
                "clamped, so a malformed confidence cannot widen the bar")
        r.check("it is deterministic",
                len({P.required_margin(0.77) for _ in range(50)}) == 1)

    # ── the six cases the brief names ───────────────────────────────────────
    r.head("The named confidence cases")
    for label, top, nxt, expected, why in (
            ("A  1.000 vs 0.910", 1.000, 0.910, P.Verdict.RESOLVED,
             "an exact match is a different tier of evidence, not a near-tie"),
            ("B  0.500 vs 0.410", 0.500, 0.410, P.Verdict.AMBIGUOUS,
             "the same 0.090 gap means less when neither candidate is a good match"),
            ("C  0.620 vs 0.600", 0.620, 0.600, P.Verdict.AMBIGUOUS,
             "weak evidence, almost no separation"),
            ("D  0.930 vs 0.920", 0.930, 0.920, P.Verdict.AMBIGUOUS,
             "strong but too close to call"),
            ("E  0.990 vs 0.400", 0.990, 0.400, P.Verdict.RESOLVED,
             "strong and clearly separated"),
    ):
        with guard(r, label):
            got = verdict_of(cand(top, x=100.0), cand(nxt, x=900.0))
            r.check(f"{label} -> {expected}", got.verdict == expected,
                    f"got {got.verdict}; {why}")

    with guard(r, "F  two genuinely identical controls"):
        for value in (0.500, 0.930, 1.000):
            got = verdict_of(cand(value, x=100.0, label="Save"),
                             cand(value, x=900.0, label="Save"))
            r.check(f"identical evidence at {value:.3f} stays ambiguous",
                    got.verdict == P.Verdict.AMBIGUOUS, got.verdict)
        r.check("  including at certainty, where the bar is zero",
                P.required_margin(1.0) == 0.0
                and verdict_of(cand(1.0, x=100.0),
                               cand(1.0, x=900.0)).ambiguous,
                "strictly-greater is what stops a tie resolving by sort order")

    # ── boundary conditions ─────────────────────────────────────────────────
    r.head("Boundaries")
    with guard(r, "the decision flips exactly at the rule"):
        top = 0.800
        need = P.required_margin(top)
        just_under = verdict_of(cand(top, x=100.0),
                                cand(top - need * 0.9, x=900.0))
        just_over = verdict_of(cand(top, x=100.0),
                               cand(top - need * 1.1, x=900.0))
        # `ambiguous`, not `== RESOLVED`. At a top confidence of 0.800 the
        # verdict is LOW_CONFIDENCE, which is a resolution on weak evidence
        # rather than a question — the two are different states and only one
        # of them stops Diablo acting.
        r.check("a hair under the required margin asks",
                just_under.ambiguous, just_under.verdict)
        r.check("a hair over it does not",
                not just_over.ambiguous, just_over.verdict)
        # Constructed so the subtraction is exact, rather than relying on
        # 0.8 - (0.8 - need) coming back as need. It does not.
        runner_conf = top - need
        exact = verdict_of(cand(top, x=100.0), cand(runner_conf, x=900.0))
        r.check("at or below the margin it asks, because the test is strict",
                exact.ambiguous or (top - runner_conf) > need,
                f"gap {top - runner_conf:.6f} vs required {need:.6f}")

    with guard(r, "a single candidate needs no separation"):
        got = verdict_of(cand(0.60, x=100.0))
        r.check("one candidate is never ambiguous", not got.ambiguous)
        r.check("  but weak evidence is reported as such",
                got.verdict == P.Verdict.LOW_CONFIDENCE, got.verdict)
        r.check("  and strong evidence resolves",
                verdict_of(cand(0.97, x=100.0)).verdict == P.Verdict.RESOLVED)

    # ── source agreement ────────────────────────────────────────────────────
    r.head("Source agreement")
    with guard(r, "agreement that distinguishes is allowed to count"):
        top = cand(0.90, x=100.0, sources=("accessibility", "ocr"),
                   single={"accessibility": 0.80, "ocr": 0.75})
        runner = cand(0.70, x=900.0, sources=("accessibility",))
        r.check("the top's extra source is discriminating",
                P._discriminating(top, runner))
        judged = verdict_of(top, runner)
        r.check("  so the fused figures are judged and it does not ask",
                not judged.ambiguous, judged.verdict)

    with guard(r, "agreement that does not distinguish is ignored"):
        # Both corroborated by the same instruments. This is the measured case:
        # accessibility 0.930/0.910, OCR agreeing with both, fused 0.969/0.959.
        top = cand(0.969, x=100.0, sources=("accessibility", "ocr"),
                   single={"accessibility": 0.930, "ocr": 0.930})
        runner = cand(0.959, x=900.0, sources=("accessibility", "ocr"),
                      single={"accessibility": 0.910, "ocr": 0.910})
        r.check("common-mode agreement is not discriminating",
                not P._discriminating(top, runner))
        separation, basis = P._separation(top, runner, 0.969 - 0.959)
        r.check("the gap is judged on single-source evidence",
                abs(separation - 0.020) < 1e-6, f"{separation:.4f}")
        r.check("  not on the compressed fused gap",
                abs(separation - (0.969 - 0.959)) > 1e-6,
                "fused gap 0.010, single-source gap 0.020")
        r.check("  and the bar comes from the same evidence",
                abs(basis - 0.930) < 1e-6, f"{basis:.4f}")
        r.check("so it does not ask",
                not verdict_of(top, runner).ambiguous,
                verdict_of(top, runner).verdict)

    with guard(r, "the answer does not depend on whether OCR could read"):
        # The same two controls, once with OCR corroborating and once without.
        ax_only = verdict_of(cand(0.930, x=100.0), cand(0.910, x=900.0))
        both = verdict_of(
            cand(0.969, x=100.0, sources=("accessibility", "ocr"),
                 single={"accessibility": 0.930, "ocr": 0.930}),
            cand(0.959, x=900.0, sources=("accessibility", "ocr"),
                 single={"accessibility": 0.910, "ocr": 0.910}))
        r.check("light and dark reach the same verdict",
                ax_only.verdict == both.verdict,
                f"{ax_only.verdict} vs {both.verdict}")

    with guard(r, "disagreement leaves two candidates standing"):
        # Different places, different instruments, no merge.
        got = verdict_of(cand(0.88, x=100.0, sources=("accessibility",)),
                         cand(0.87, x=900.0, sources=("vision",)))
        r.check("two sources naming different controls stay ambiguous",
                got.verdict == P.Verdict.AMBIGUOUS, got.verdict)

    with guard(r, "one source missing is not a failure"):
        got = verdict_of(cand(0.95, x=100.0, sources=("accessibility",)))
        r.check("accessibility alone can resolve",
                got.verdict == P.Verdict.RESOLVED, got.verdict)

    # ── fusion mechanics ────────────────────────────────────────────────────
    r.head("Fusion")
    with guard(r, "agreement raises confidence without reaching certainty"):
        a = cand(0.62, x=100.0, sources=("accessibility",))
        b = cand(0.58, x=100.0, sources=("ocr",))
        fused = P._fuse([a, b])
        r.check("two instruments in one place merge", len(fused) == 1,
                f"{len(fused)} candidates")
        r.check("  and the result beats either alone",
                fused[0].confidence > max(a.confidence, b.confidence),
                f"{fused[0].confidence:.3f}")
        r.check("  but never reaches 1.0", fused[0].confidence < 1.0)
        r.check("  and both sources are named",
                set(fused[0].sources) == {"accessibility", "ocr"})

    with guard(r, "source confidence survives fusion"):
        a = cand(0.62, x=100.0, sources=("accessibility",))
        b = cand(0.58, x=100.0, sources=("ocr",))
        fused = P._fuse([a, b])[0]
        r.check("each source's own claim is kept",
                fused.source_confidence.get("accessibility") == 0.62
                and fused.source_confidence.get("ocr") == 0.58,
                str(fused.source_confidence))
        r.check("  distinct from the fused figure",
                fused.confidence != fused.source_confidence["accessibility"])
        r.check("  and best_single reports the strongest single claim",
                fused.best_single == 0.62, str(fused.best_single))

    with guard(r, "fusion does not mutate what it was given"):
        a = cand(0.62, x=100.0, sources=("accessibility",))
        before = a.confidence
        P._fuse([a, cand(0.58, x=100.0, sources=("ocr",))])
        r.check("the caller's candidate is untouched", a.confidence == before)

    with guard(r, "stale evidence is not treated as agreement"):
        here = FakeVision([10] * 16)
        drifted = FakeVision([90] * 16)
        r.check("the same frame counts as the same screen",
                P._same_frame(cand(0.7, vision=here), cand(0.6, vision=here)))
        r.check("a drifted frame does not",
                not P._same_frame(cand(0.7, vision=here),
                                  cand(0.6, vision=drifted)))
        r.check("  a small drift is tolerated, as elsewhere in screen",
                P._same_frame(cand(0.7, vision=FakeVision([10] * 16)),
                              cand(0.6, vision=FakeVision([13] * 16))))
        r.check("candidates with no frame evidence are unaffected",
                P._same_frame(cand(0.7), cand(0.6))
                and len(P._fuse([cand(0.7, x=100.0), cand(0.6, x=100.0)])) == 1)

        # Honest about where this can and cannot fire. _fuse drops a candidate
        # whose sources are already covered before the frame guard is reached,
        # so two vision candidates in one place are deduplicated rather than
        # frame-checked. The guard engages only when two candidates carry
        # vision evidence and neither's source set contains the other's. The
        # common cross-frame case is already prevented upstream: the vision
        # cache is keyed on the frame fingerprint, so a cache hit is by
        # construction the same frame.
        pair = P._fuse([cand(0.7, x=100.0, sources=("vision",), vision=here),
                        cand(0.6, x=100.0, sources=("vision",), vision=drifted)])
        r.check("same-source duplicates are deduplicated before the frame check",
                len(pair) == 1,
                "documented limit, not a silent merge of stale evidence")

    # ── the five states ─────────────────────────────────────────────────────
    r.head("The five states")
    with guard(r, "they do not collapse into each other"):
        states = {
            P.Reading().verdict,
            P.Reading(observation={"ocr": {"ok": False, "problem": "black"}}).verdict,
            verdict_of(cand(0.60, x=100.0)).verdict,
            verdict_of(cand(0.97, x=100.0)).verdict,
            verdict_of(cand(0.93, x=100.0), cand(0.93, x=900.0)).verdict,
        }
        r.check("five distinct verdicts are reachable", len(states) == 5,
                str(sorted(states)))
        r.check("nothing found, screen readable -> not_found",
                P.Reading().verdict == P.Verdict.NOT_FOUND)
        r.check("nothing found, screen unreadable -> invalid_observation",
                P.Reading(observation={"ocr": {"ok": False, "problem": "black"}}
                          ).verdict == P.Verdict.INVALID_OBSERVATION)

    with guard(r, "every verdict can explain itself"):
        for reading in (P.Reading(),
                        P.Reading(observation={"ocr": {"ok": False, "problem": "black"}}),
                        verdict_of(cand(0.60, x=100.0)),
                        verdict_of(cand(0.97, x=100.0)),
                        verdict_of(cand(0.93, x=100.0), cand(0.93, x=900.0))):
            text = reading.explain()
            r.check(f"{reading.verdict} explains itself",
                    isinstance(text, str) and reading.verdict.upper() in text.upper(),
                    text.splitlines()[0][:52])
        resolved = verdict_of(cand(0.97, x=100.0), cand(0.40, x=900.0)).explain()
        for needed in ("confidence", "runner-up", "separation", "rule", "evidence"):
            r.check(f"  a resolution names its {needed}", needed in resolved)

    # ── against the real corpus ─────────────────────────────────────────────
    r.head("Against the corpus")
    with guard(r, "the recorded baseline holds the safety line"):
        path = os.path.join(REPO, "evals", "perception_fusion_baseline_v2.json")
        if not os.path.exists(path):
            r.skip("wrong targets stay at zero", "baseline v2 not written yet")
        else:
            data = json.load(open(path))
            after = data["after"]["fusion"]
            before = data["before"]["fusion"]
            r.check("wrong targets did not increase",
                    after["wrong_target"] + after["off_target"]
                    <= before["wrong_target"] + before["off_target"],
                    f"{before['wrong_target']} -> {after['wrong_target']}")
            r.check("  and are zero",
                    after["wrong_target"] + after["off_target"] == 0)
            r.check("false ambiguity decreased",
                    after["false_ambiguity"] < before["false_ambiguity"],
                    f"{before['false_ambiguity']} -> {after['false_ambiguity']}")
            r.check("correct resolutions increased",
                    after["correct"] > before["correct"],
                    f"{before['correct']} -> {after['correct']}")
            r.check("genuine ambiguity is still reported",
                    after["ambiguous_right"] >= before["ambiguous_right"],
                    f"{before['ambiguous_right']} -> {after['ambiguous_right']}")

    # ── safety, unchanged ───────────────────────────────────────────────────
    r.head("No new path to a computer action")
    with guard(r, "arbitration did not open one"):
        body = open(os.path.join(REPO, "diablo", "perception.py")).read()
        offenders = [n for n in ("act_on", "do_engine", "_post_click", "CGEventPost")
                     if n in body]
        r.check("perception mentions no part of the action layer",
                not offenders, "; ".join(offenders))
        names = set(agent_tools.TOOLS)
        r.check("no click/type/press tool exists",
                not {n for n in names
                     if any(k in n for k in ("click", "type_", "press", "keystroke"))})
        # 48, plus find_in_menus and choose_menu, reviewed 2026-10-06.
        r.check("the tool count is 48 plus the reviewed menu pair",
                len(names) == 50 and {"find_in_menus", "choose_menu"} <= set(names),
                str(len(names)))
        from skills import computer_use
        r.check("computer use is still off by default",
                computer_use.ENABLED is False)

    return r.report("fusion")


if __name__ == "__main__":
    sys.exit(main())
