#!/usr/bin/env python3
"""
Scores a decision engine on the Diablo decision corpus. A benchmark, not a
test: it needs no network for `rules`, the model for `router`, and the Laya
package (with its checkpoint) for `laya`.

    python eval_decision.py --engine rules
    python eval_decision.py --engine router             # today's path: understand()
    python eval_decision.py --engine laya --device mps --model typed-decisions

What it reports, per engine:

    false fast   fast_ok=False turns put on a fast lane. Must be 0.
    fast recall  fast_ok=True turns that got a fast lane (the speed win)
    by category  the same, broken down
    partials     a fast lane on a partial the final transcript would not get
    latency      p50 / p95 per decision, and the cold start
    memory       peak resident set size of the process
"""

import argparse
import json
import os
import resource
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decision_corpus import CORPUS, HELD_OUT, PARTIALS  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

FASTS = ("instant", "fast")


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def _lane(verdict):
    if verdict is None:
        return None, "none"
    return verdict.lane, getattr(verdict, "intent", "")


def engine_for(args):
    from skills import decision
    if args.engine == "rules":
        rules = decision.Rules()
        return (lambda text: _lane(rules.decide(text))), None
    if args.engine == "laya":
        from laya import Router
        started = time.perf_counter()
        router = Router(device=args.device, max_loaded=1)
        router.predict("warm up", {"x": {"type": "noul", "instructions": "Is this a test?"}},
                       model=args.model)
        cold = time.perf_counter() - started

        class Pinned:
            def predict(self, text, questions):
                return router.predict(text, questions, model=args.model)
        laya = decision.Laya(router=Pinned(), threshold=args.threshold)
        return (lambda text: _lane(laya.decide(text))), cold
    if args.engine == "router":
        from skills import route, tutor

        def existing(text):
            d = route.classify(text)
            if getattr(d, "show", None) is not None:
                return "agent", d.route
            if tutor.wants_teaching(text):
                return "agent", "teach"
            return ("fast" if d.route == "chat" else "agent"), d.route
        return existing, None
    raise SystemExit(f"unknown engine {args.engine}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="rules")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--model", default="english")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    decide, cold = engine_for(args)
    rows, times = [], []
    for text, category, fast_ok in CORPUS:
        started = time.perf_counter()
        lane, detail = decide(text)
        times.append((time.perf_counter() - started) * 1000)
        rows.append({"text": text, "category": category, "fast_ok": fast_ok,
                     "lane": lane, "detail": detail, "fast": lane in FASTS})

    false_fast = [r for r in rows if r["fast"] and not r["fast_ok"]]
    ok = [r for r in rows if r["fast_ok"]]
    recall = sum(r["fast"] for r in ok) / max(1, len(ok))
    by_category = {}
    for r in rows:
        c = by_category.setdefault(r["category"], {"n": 0, "fast": 0})
        c["n"] += 1
        c["fast"] += int(r["fast"])

    partial_leaks = []
    for sequence, final_ok in PARTIALS:
        for partial in sequence:
            lane, _ = decide(partial)
            if lane in FASTS and not final_ok:
                partial_leaks.append(partial)

    held = [(text, ok, decide(text)[0]) for text, ok in HELD_OUT]
    held_false_fast = [(t, lane) for t, ok, lane in held if lane in FASTS and not ok]
    held_recall = (sum(1 for _, ok, lane in held if ok and lane in FASTS)
                   / max(1, sum(1 for _, ok, _ in held if ok)))

    times.sort()
    report = {
        "engine": args.engine,
        "device": args.device if args.engine == "laya" else "",
        "model": args.model if args.engine == "laya" else "",
        "n": len(rows), "false_fast": len(false_fast),
        "false_fast_cases": [(r["text"], r["lane"], r["detail"]) for r in false_fast],
        "fast_recall": round(recall, 3),
        "missed_fast": [(r["text"], r["lane"], r["detail"]) for r in ok if not r["fast"]],
        "by_category": by_category, "partial_leaks": partial_leaks,
        "held_out": {"n": len(held), "false_fast": held_false_fast,
                     "fast_recall": round(held_recall, 3),
                     "missed": [t for t, ok, lane in held if ok and lane not in FASTS]},
        "latency_ms": {"p50": round(statistics.median(times), 2),
                       "p95": round(times[int(0.95 * (len(times) - 1))], 2),
                       "max": round(times[-1], 2)},
        "cold_s": round(cold, 2) if cold is not None else None,
        "peak_rss_mb": round(rss_mb(), 1),
    }
    print(json.dumps({k: v for k, v in report.items() if k != "missed_fast"}, indent=1))
    print("missed fast:", report["missed_fast"])
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"report": report, "rows": rows}, f, indent=1)


if __name__ == "__main__":
    main()
