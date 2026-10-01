#!/usr/bin/env python3
"""
What the planner decides, against a screen that is really there.

`evals/run_planner_eval.py` replaces `_screen_summary` with a fixture string and
measures judgement given a perfect reading. This runs the same unchanged planner
against a real fixture window and scores what it names, using the real
perception system to decide whether the name it produced points at the control
the case expects.

    python3 scripts/run_planner_benchmark.py
    python3 scripts/run_planner_benchmark.py --iterations 3
    python3 scripts/run_planner_benchmark.py --json baseline.json

Nothing is clicked. `computer_use.act_on`, `_post_click`, `do_engine.run` and
CGEventPost are replaced with tripwires for the duration of the run; if any of
them is reached the benchmark fails its safety gate and reports no scores.

Every number below is computed here. Nothing is classified by hand afterwards.
"""

import argparse
import collections
import json
import os
import statistics
import sys
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tests"))
sys.path.insert(0, os.path.join(REPO, "evals"))
sys.path.insert(0, os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))

BENCHMARK_VERSION = "4a.1"

# Fixture lifecycle and the coordinate-to-control map come from the perception
# benchmark rather than a second copy of both.
from run_perception_eval import Fixture, which_control  # noqa: E402

import planner_benchmark_corpus as corpus  # noqa: E402

from skills import computer_use, do_engine as engine, perception  # noqa: E402

# ─── Outcomes ────────────────────────────────────────────────────────────────
CORRECT_TARGET = "CORRECT_TARGET"
WRONG_TARGET = "WRONG_TARGET"
UNRESOLVABLE_TARGET = "UNRESOLVABLE_TARGET"
WRONG_ACTION = "WRONG_ACTION"
WAIT_WHEN_ACTION_REQUIRED = "WAIT_WHEN_ACTION_REQUIRED"
INVALID_GOAL = "INVALID_GOAL"

CATEGORIES = [CORRECT_TARGET, WRONG_TARGET, UNRESOLVABLE_TARGET,
              WRONG_ACTION, WAIT_WHEN_ACTION_REQUIRED, INVALID_GOAL]


class SafetyGate(Exception):
    """Raised when something capable of mutating a screen is reached."""


def arm():
    """
    Replace every mutation-capable entry point with one that raises.

    Executable, not a promise. A benchmark that measures a planner has no
    business touching an application, and the way to know it did not is to make
    the attempt fail loudly rather than to assert afterwards that it did not
    happen.
    """
    saved = []
    targets = [(computer_use, "act_on"), (computer_use, "_post_click"),
               (engine, "run")]
    try:
        import Quartz
        targets.append((Quartz, "CGEventPost"))
    except Exception:
        pass
    for module, name in targets:
        if hasattr(module, name):
            saved.append((module, name, getattr(module, name)))

            def tripped(*a, _n=name, **k):
                raise SafetyGate(_n)
            setattr(module, name, tripped)
    return saved


def disarm(saved):
    for module, name, original in saved:
        setattr(module, name, original)


# ─── Validation ──────────────────────────────────────────────────────────────

def refocus(fixture: Fixture) -> bool:
    """
    Bring the fixture's own window back to the front, and re-read it.

    Not cosmetic. A window that is not frontmost drops out of
    CGWindowListCopyWindowInfo within a few seconds on this machine, and the
    planner call takes eight seconds or more, so by the time its answer is
    scored the window it named is no longer visible to the thing doing the
    scoring. Measured before this was added: three cases whose targets the
    planner named correctly came back UNRESOLVABLE_TARGET with verdict
    invalid_observation.

    This activates the benchmark's own fixture process and nothing else. It is
    the same call `Fixture.start()` already makes, repeated.
    """
    try:
        from AppKit import NSRunningApplication
        from skills import screen_context
        pid = fixture.child.pid if fixture.child else None
        if pid:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is not None:
                app.activateWithOptions_(1 << 1)   # ActivateIgnoringOtherApps
                time.sleep(0.45)
        found = screen_context.find_window(fixture.layout.get("title", ""))
        if found:
            fixture.window = found
            return True
    except Exception:
        pass
    return False


def validate(case: Dict[str, Any], fixture: Fixture) -> Optional[str]:
    """
    Why this case cannot be scored, or None if it can.

    Runs before the planner is asked anything. A case whose target does not
    exist, cannot be reached, or cannot be moved by one action is a fault in the
    corpus or the fixture, and scoring it as a planner failure would make the
    baseline say something false.
    """
    if not fixture.window:
        return "the fixture window did not come up"
    control = fixture.control(case["expect_control"])
    if control is None:
        return f"{case['expect_control']!r} is not in the fixture layout"
    if control.get("enabled") is False:
        return (f"{control.get('label') or case['expect_control']!r} is disabled, "
                f"so no single action can reach the goal")
    try:
        reading = perception.read(case["probe"], fixture.window)
    except Exception as e:
        return f"perception raised {type(e).__name__}"
    if reading.best is None:
        return (f"perception cannot resolve {case['probe']!r} "
                f"({reading.verdict})")
    landed = which_control(reading.best.centre, fixture.controls)
    if landed != case["expect_control"]:
        return (f"{case['probe']!r} resolves to {landed!r}, "
                f"not {case['expect_control']!r}")
    return None


# ─── Classification ──────────────────────────────────────────────────────────

def classify(case: Dict[str, Any], step, fixture: Fixture,
             invalid: Optional[str]) -> Dict[str, Any]:
    """
    One planner decision, placed in exactly one category, with its evidence.

    The target is judged by asking the real perception system where the name the
    planner produced actually points, and comparing that against the control the
    case expects. A name is not scored by string equality: "Save", "the Save
    button" and 'Button "Save"' are the same answer and a scorer that says
    otherwise is measuring phrasing.
    """
    record: Dict[str, Any] = {
        "case": case["id"],
        "coverage": case["coverage"],
        "expect_control": case["expect_control"],
        "expect_verb": case["expect_verb"],
        "verb": getattr(step, "verb", None),
        "target": getattr(step, "target", None),
        "why": (getattr(step, "why", "") or "")[:120],
        "resolved_to": None,
        "resolution_verdict": None,
        "reason": "",
    }

    if invalid is not None:
        record["outcome"] = INVALID_GOAL
        record["reason"] = invalid
        return record

    verb = (step.verb or "").strip().lower()
    if verb in ("wait", "done"):
        record["outcome"] = WAIT_WHEN_ACTION_REQUIRED
        record["reason"] = f"answered {verb!r} where one action reaches the goal"
        return record
    if verb != case["expect_verb"]:
        record["outcome"] = WRONG_ACTION
        record["reason"] = f"expected {case['expect_verb']!r}, got {verb!r}"
        return record

    name = (step.target or "").strip()
    if not name:
        record["outcome"] = UNRESOLVABLE_TARGET
        record["reason"] = "named nothing"
        return record

    try:
        reading = perception.read(name, fixture.window)
    except Exception as e:
        record["outcome"] = UNRESOLVABLE_TARGET
        record["reason"] = f"perception raised {type(e).__name__} on {name!r}"
        return record

    record["resolution_verdict"] = reading.verdict
    if reading.best is None:
        record["outcome"] = UNRESOLVABLE_TARGET
        record["reason"] = f"{name!r} resolves to nothing ({reading.verdict})"
        return record

    landed = which_control(reading.best.centre, fixture.controls)
    record["resolved_to"] = landed
    record["confidence"] = round(reading.best.confidence, 3)
    if landed == case["expect_control"]:
        record["outcome"] = CORRECT_TARGET
        record["reason"] = f"{name!r} resolves to {landed!r}"
    else:
        record["outcome"] = WRONG_TARGET
        record["reason"] = (f"{name!r} resolves to {landed!r}, "
                            f"expected {case['expect_control']!r}")
    return record


# ─── Running ─────────────────────────────────────────────────────────────────

def run_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """
    One case, start to finish, with its own fixture.

    The fixture is launched per case rather than once per run because a window
    that is not frontmost drops out of the window list within a few seconds on
    this machine, and a benchmark that measures a window it can no longer see
    measures nothing.
    """
    fixture = Fixture(case["fixture"], case["variant"])
    try:
        fixture.start()
        refocus(fixture)
        invalid = validate(case, fixture)
        step = None
        began = time.perf_counter()
        if invalid is None:
            task = engine.Task(
                intent=case["intent"],
                goal=engine.Goal(kind=case["goal_kind"], value=case["goal_value"]))
            step = engine.model_planner(task, fixture.window)
        took = (time.perf_counter() - began) * 1000.0
        # The planner took seconds; the window may have gone. Put it back
        # before asking perception where its answer points.
        if invalid is None:
            refocus(fixture)
        record = classify(case, step, fixture, invalid)
        record["ms"] = round(took, 1)
        return record
    finally:
        fixture.stop()


def aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = collections.Counter(r["outcome"] for r in records)
    total = len(records)
    scored = total - counts[INVALID_GOAL]
    return {
        "decisions": total,
        "scored": scored,
        "counts": {c: counts[c] for c in CATEGORIES},
        "rates_over_all": {c: round(100.0 * counts[c] / total, 1) if total else 0.0
                           for c in CATEGORIES},
        "rates_over_scored": {c: round(100.0 * counts[c] / scored, 1) if scored else 0.0
                              for c in CATEGORIES if c != INVALID_GOAL},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Diablo planner benchmark")
    parser.add_argument("--iterations", type=int, default=1,
                        help="complete passes over the corpus")
    parser.add_argument("--json", help="write machine-readable results here")
    args = parser.parse_args(argv)

    print(f"  DIABLO PLANNER BENCHMARK {BENCHMARK_VERSION} "
          f"· corpus {corpus.CORPUS_VERSION} · {len(corpus.CASES)} cases "
          f"· {args.iterations} iteration(s)")
    print(f"  safety: act_on, _post_click, do_engine.run and CGEventPost are "
          f"armed as tripwires\n")

    saved = arm()
    iterations: List[List[Dict[str, Any]]] = []
    began = time.time()
    try:
        for i in range(1, args.iterations + 1):
            print(f"  iteration {i}")
            rows = []
            for case in corpus.CASES:
                row = run_case(case)
                row["iteration"] = i
                rows.append(row)
                mark = "ok " if row["outcome"] == CORRECT_TARGET else "   "
                print(f"    {mark}{row['case']:18} {row['outcome']:26} "
                      f"{row['reason'][:60]}")
            iterations.append(rows)
            print()
    except SafetyGate as reached:
        disarm(saved)
        print(f"\n  SAFETY GATE FAILED: {reached} was reached.")
        print("  No scores are reported. The benchmark does not continue.")
        return 2
    finally:
        disarm(saved)

    flat = [row for rows in iterations for row in rows]
    overall = aggregate(flat)
    per_iteration = [aggregate(rows) for rows in iterations]

    print("  " + "=" * 74)
    print(f"  {'outcome':30} {'count':>6} {'of all':>8} {'of scored':>11}")
    print("  " + "-" * 74)
    for category in CATEGORIES:
        scored_rate = overall["rates_over_scored"].get(category)
        print(f"  {category:30} {overall['counts'][category]:>6} "
              f"{overall['rates_over_all'][category]:>7.1f}% "
              f"{(f'{scored_rate:.1f}%' if scored_rate is not None else '—'):>11}")
    print("  " + "-" * 74)
    print(f"  {'decisions':30} {overall['decisions']:>6}")
    print(f"  {'scored (excludes INVALID_GOAL)':30} {overall['scored']:>6}")

    # Variance, computed here rather than eyeballed.
    print(f"\n  per-iteration CORRECT_TARGET, and spread across iterations")
    print("  " + "-" * 74)
    variance = {}
    for category in CATEGORIES:
        series = [it["counts"][category] for it in per_iteration]
        variance[category] = {
            "per_iteration": series,
            "min": min(series) if series else 0,
            "max": max(series) if series else 0,
            "stdev": round(statistics.stdev(series), 3) if len(series) > 1 else 0.0,
        }
    for category in CATEGORIES:
        v = variance[category]
        print(f"  {category:30} {str(v['per_iteration']):>18} "
              f"min {v['min']} max {v['max']} stdev {v['stdev']}")

    unstable = [c for c in CATEGORIES if variance[c]["min"] != variance[c]["max"]]
    print(f"\n  categories that varied between iterations: "
          f"{', '.join(unstable) if unstable else 'none'}")
    latencies = sorted(r["ms"] for r in flat if r.get("ms"))
    if latencies:
        print(f"  planner latency: median {statistics.median(latencies):.0f} ms, "
              f"max {latencies[-1]:.0f} ms")
    print(f"  {time.time() - began:.0f}s total\n")

    if args.json:
        payload = {
            "benchmark_version": BENCHMARK_VERSION,
            "corpus_version": corpus.CORPUS_VERSION,
            "timestamp": time.time(),
            "iterations": args.iterations,
            "cases_per_iteration": len(corpus.CASES),
            "aggregate": overall,
            "per_iteration": per_iteration,
            "variance": variance,
            "records": flat,
        }
        with open(args.json, "w") as handle:
            json.dump(payload, handle, indent=2)
        print(f"  written to {args.json}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
