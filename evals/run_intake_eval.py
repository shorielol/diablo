#!/usr/bin/env python3
"""
How well the intake decides: act, ask, or talk.

Every case goes through route.classify, the production decision, with the real
model. Nothing is executed: the decision is the thing measured, because a turn
that asks a question it did not need to, or acts on a sentence it should have
asked about, has already gone wrong before any tool runs.

    python3 evals/run_intake_eval.py                     # 3 repeats per case
    python3 evals/run_intake_eval.py --repeats 5 --json intake.json
    python3 evals/run_intake_eval.py --model qwen3.5:4b

Reported per group and overall: pass rate, unnecessary clarification (asked
when the request was clear), false action (acted on a fragment, an ambiguous
command or a missing target), and decision latency P50/P95. The trace goes to
a temp folder, never to the install's.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, HOME)
os.environ["DIABLO_TRACE"] = os.path.join(tempfile.mkdtemp(prefix="diablo-intake-eval-"),
                                          "trace.jsonl")


ACTS = ("act", "escalate", "delegate")


def passed(rule: str, route: str, needs_screen: bool) -> bool:
    if rule == "not_clarify":
        return route != "clarify"
    if rule == "clarify":
        return route == "clarify"
    if rule == "not_act":
        return route in ("clarify", "chat")
    if rule == "look_ok":
        return route in ("clarify", "chat") or (route == "act" and needs_screen)
    raise ValueError(rule)


def pct(values, q):
    values = sorted(values)
    if not values:
        return None
    return values[max(0, -(-len(values) * q // 100) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--model", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    try:
        from companion_manager import load_env
        load_env()
    except Exception:
        pass
    if args.model:
        os.environ["DIABLO_MODEL"] = os.environ["DIABLO_FAST_MODEL"] = args.model
    from skills import local_brain, route, trace
    if args.model:
        local_brain.CHAT_MODEL = local_brain.FAST_MODEL = args.model
    trace.PATH = os.environ["DIABLO_TRACE"]
    local_brain.warm(local_brain.FAST_MODEL)

    cases = json.load(open(os.path.join(HERE, "intake_cases.json")))["cases"]
    runs = []
    for _ in range(args.repeats):
        for case in cases:
            trace.new_turn("eval")
            began = time.perf_counter()
            try:
                decision = route.classify(case["say"])
                got, screen, error = decision.route, bool(decision.needs_screen), None
            except Exception as e:
                got, screen, error = "error", False, type(e).__name__
            runs.append({"id": case["id"], "group": case["group"], "pass_rule": case["pass"],
                         "route": got, "needs_screen": screen, "error": error,
                         "seconds": round(time.perf_counter() - began, 3),
                         "passed": error is None and passed(case["pass"], got, screen)})

    groups = defaultdict(list)
    for run in runs:
        groups[run["group"]].append(run)
    clear = [r for r in runs if r["pass_rule"] == "not_clarify"]
    unsafe = [r for r in runs if r["pass_rule"] in ("not_act", "clarify", "look_ok")]
    report = {
        "model": local_brain.FAST_MODEL,
        "repeats": args.repeats,
        "cases": len(cases),
        "passed": f"{sum(r['passed'] for r in runs)} of {len(runs)}",
        "unnecessary_clarification": f"{sum(r['route'] == 'clarify' for r in clear)} of {len(clear)}",
        # escalate and delegate act too, with a bigger model or an agent.
        "false_action": f"{sum(r['route'] in ACTS and not r['passed'] for r in unsafe)} of {len(unsafe)}",
        "errors": sum(r["error"] is not None for r in runs),
        "seconds_p50": pct([r["seconds"] for r in runs], 50),
        "seconds_p95": pct([r["seconds"] for r in runs], 95),
        "groups": {g: f"{sum(r['passed'] for r in rs)} of {len(rs)}" for g, rs in groups.items()},
        "failing": sorted({r["id"] for r in runs if not r["passed"]}),
        "runs": runs,
    }
    shown = {k: v for k, v in report.items() if k != "runs"}
    print(json.dumps(shown, indent=2))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
