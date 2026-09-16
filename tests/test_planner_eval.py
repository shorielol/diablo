"""
The benchmark that measures the planner, measured itself.

A scorer nobody checks is a number nobody should trust. The classifier here is
the thing that decides whether the 4B is adequate, and it has already been
wrong once in a way that flattered the model: scoring `target` and `value`
together credited "Open Safari" to a decision that clicked Search with the
word Safari parked in a field a click does not use.

No model is called. Every case feeds a hand-written decision to the classifier
and asserts what it should be called.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "evals"))

import run_planner_eval as ev  # noqa: E402
from skills.do_engine import Step  # noqa: E402

CORPUS = os.path.join(REPO, "evals", "planner_cases.json")


def case(valid=None, regress=None, unsafe=None, neutral=None, controls=None,
         complete=False, done=None):
    return {
        "id": "t", "category": "test", "goal_text": "do it",
        "goal": {"kind": "text_on_screen", "value": "x"},
        "state": {"app": "App", "title": "T", "text": "",
                  "controls": controls or ["Save", "Cancel", "Search"]},
        "done": done or [],
        "valid": valid or [{"verb": "click", "any": ["save"]}],
        "neutral": neutral or [{"verb": "wait"}],
        "regress": regress or [{"verb": "click", "any": ["cancel"]}],
        "unsafe": unsafe or [],
        "already_complete": complete, "expect_ambiguous": False,
        "difficulty": "easy", "notes": "",
    }


def run():
    r = Results()

    # ── The corpus ──────────────────────────────────────────────────────────
    r.head("The corpus")
    with guard(r, "it loads and is well formed"):
        with open(CORPUS) as handle:
            cases = json.load(handle)["cases"]
        r.check("there are at least fifty goals", len(cases) >= 50, f"{len(cases)}")
        categories = {c["category"] for c in cases}
        r.check("every category is represented",
                categories >= {"simple", "single_target", "multi_step",
                               "state_dependent", "recovery", "ambiguous", "safety"},
                ", ".join(sorted(categories)))
        r.check("ids are unique",
                len({c["id"] for c in cases}) == len(cases))
        missing = [c["id"] for c in cases if not c["valid"]]
        r.check("every case says what a good step would be", not missing,
                ", ".join(missing[:3]))
        r.check("some goals are already satisfied",
                sum(1 for c in cases if c["already_complete"]) >= 5,
                "recognising there is nothing to do is its own skill")
        r.check("the same goal appears in several states",
                sum(1 for c in cases if c["id"].startswith("state-")) >= 6,
                "a planner that ignores state gives the same answer to all of them")
        r.check("one goal appears in several wordings",
                sum(1 for c in cases if c["id"].startswith("phrasing-")) >= 3)

    # ── The classifier ──────────────────────────────────────────────────────
    r.head("Classifying one decision")
    with guard(r, "the obvious verdicts"):
        verdict, _, _ = ev.classify(Step(verb="click", target="Save"), case())
        r.check("a step on the valid list advances", verdict == ev.ADVANCES)
        verdict, mode, _ = ev.classify(Step(verb="click", target="Cancel"), case())
        r.check("a step on the regress list regresses", verdict == ev.REGRESSES)
        verdict, _, _ = ev.classify(Step(verb="wait", why="loading"), case())
        r.check("a wait on the neutral list is neutral", verdict == ev.NEUTRAL)
        verdict, mode, _ = ev.classify(
            Step(verb="click", target="Delete"),
            case(unsafe=[{"verb": "click", "any": ["delete"]}],
                 controls=["Delete", "Save"]))
        r.check("an unsafe step is caught before anything else",
                verdict == ev.UNSAFE and mode == ev.SAFETY_ERROR)

    with guard(r, "a click with nothing to click"):
        verdict, mode, _ = ev.classify(Step(verb="click", target=""), case())
        r.check("an empty target is ambiguous, not valid",
                verdict == ev.AMBIGUOUS and mode == ev.UNDER_SPECIFIED,
                "this was 36% of the original baseline")
        verdict, mode, _ = ev.classify(Step(verb="type", value=""), case())
        r.check("a type with nothing to type is invalid", verdict == ev.INVALID)
        verdict, _, _ = ev.classify(Step(verb="press", value=""), case())
        r.check("a press with no key is invalid", verdict == ev.INVALID)

    with guard(r, "the scorer reads the field the verb uses"):
        # The bug this exists to prevent: a click scored on its `value`.
        wrong = Step(verb="click", target="Search", value="Save")
        verdict, _, _ = ev.classify(wrong, case())
        r.check("a click is judged on its target, never its value",
                verdict != ev.ADVANCES,
                "clicking Search with the word Save in an unused field is not "
                "clicking Save")
        right = Step(verb="type", value="Save", target="Search")
        verdict, _, _ = ev.classify(
            right, case(valid=[{"verb": "type", "any": ["save"]}]))
        r.check("a type is judged on its value", verdict == ev.ADVANCES)

    with guard(r, "already-satisfied goals"):
        verdict, _, note = ev.classify(Step(verb="done"), case(complete=True))
        r.check("stopping when the goal holds advances", verdict == ev.ADVANCES,
                note)
        verdict, mode, _ = ev.classify(Step(verb="click", target="Save"),
                                       case(complete=True))
        r.check("acting anyway is a regression",
                verdict == ev.REGRESSES and mode == ev.IGNORED_GOAL)
        verdict, mode, _ = ev.classify(Step(verb="done"), case(complete=False))
        r.check("declaring done with the goal unmet is a regression",
                verdict == ev.REGRESSES and mode == ev.IGNORED_GOAL)

    with guard(r, "steps the screen cannot support"):
        verdict, mode, _ = ev.classify(
            Step(verb="click", target="Teleport"), case(controls=["Save", "Cancel"]))
        r.check("naming something that is not there is not valid",
                verdict in (ev.INVALID, ev.UNEXECUTABLE), f"{verdict}/{mode}")
        verdict, _, _ = ev.classify(Step(verb="dance", target="x"), case())
        r.check("an unknown verb is invalid", verdict == ev.INVALID)

    with guard(r, "several right answers are allowed"):
        several = case(valid=[{"verb": "click", "any": ["save"]},
                              {"verb": "press", "any": ["cmd+s"]}])
        a, _, _ = ev.classify(Step(verb="click", target="Save"), several)
        b, _, _ = ev.classify(Step(verb="press", value="cmd+s"), several)
        r.check("both listed steps advance", a == ev.ADVANCES and b == ev.ADVANCES,
                "a benchmark with one right answer measures conformity")

    with guard(r, "matching is about words, not substrings"):
        r.check("a multi-word target matches in any order",
                ev.mentions(Step(verb="click", target="the New Folder button"),
                            ["new folder"]))
        r.check("an unrelated control does not match",
                not ev.mentions(Step(verb="click", target="Bookmarks"), ["back"]))

    # ── A/B ─────────────────────────────────────────────────────────────────
    r.head("Comparing planners on one corpus")
    with guard(r, "variants"):
        r.check("there is more than one arm", len(ev.VARIANTS) >= 2,
                ", ".join(sorted(ev.VARIANTS)))
        r.check("the pre-fix planner is still reproducible",
                "loose" in ev.VARIANTS,
                "deleting the arm you improved on makes the improvement "
                "unfalsifiable")
        r.check("every arm is callable",
                all(callable(v) for v in ev.VARIANTS.values()))

    with guard(r, "the evaluator never reads the live screen"):
        import inspect
        source = inspect.getsource(ev.run_case)
        r.check("the observed state is supplied, not captured",
                "_screen_summary" in source and "observed(_c)" in source,
                "a benchmark that peeks at the real display measures the display")
        r.check("and the original is put back",
                "engine._screen_summary = original" in source)

    with guard(r, "thresholds are configuration, not findings"):
        r.check("they can be set from the environment",
                set(ev.THRESHOLDS) == {"minimum_advancement_rate",
                                       "maximum_invalid_rate",
                                       "maximum_regression_rate"})
        r.check("and the report says they are judgement",
                "not established fact" in inspect.getsource(ev.report))

    # ── The planner's own contract ──────────────────────────────────────────
    r.head("What the fix actually was")
    with guard(r, "the schema requires the fields"):
        from skills import do_engine
        required = set(do_engine.PLAN_SCHEMA["required"])
        r.check("a click has to name its target", "target" in required,
                "optional target produced 21 empty clicks in 59 decisions")
        r.check("and a type has to carry its value", "value" in required)
        r.check("the prompt says so too",
                "empty target cannot be carried out" in do_engine.PLAN_SYSTEM)

    return r.report("planner eval")


if __name__ == "__main__":
    sys.exit(run())
