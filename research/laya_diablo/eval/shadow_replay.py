#!/usr/bin/env python3
"""
Shadow mode, measured.

    python shadow_replay.py --model typed-decisions --device mps --out ../reports/shadow.json

1. Latency: decision.decide() with no shadow and with a real Laya shadow,
   over the same utterances. The shadow runs on its own thread, so the
   decide() time should not move; this measures that it does not.
2. Real requests: the utterances Diablo actually heard (the local trace's
   "heard" events), judged by Rules and by the shadow. The words stay in
   memory; only counts are written.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)


def pct(values, q):
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))], 3) if s else None


def heard(trace_path, limit):
    texts = []
    try:
        with open(trace_path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("stage") == "heard" and rec.get("text"):
                    texts.append(rec["text"])
    except OSError:
        pass
    return texts[-limit:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="typed-decisions")
    ap.add_argument("--device", default="mps")
    ap.add_argument("--trace", default=os.path.expanduser("~/.diablo/trace.jsonl"))
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    from skills import decision, trace
    from laya import Router

    router = Router(device=args.device, max_loaded=1)

    class Pinned:
        def predict(self, state, questions):
            return router.predict(state, questions, model=args.model)
    pinned = Pinned()
    pinned.predict({"request": "warm up"}, decision.LAYA_QUESTIONS)

    rows = [json.loads(line) for line in open(os.path.join(HERE, "..", "dataset", "v1",
                                                           "validation.jsonl"))][: args.n]
    texts = [r["text"] for r in rows]

    def run(with_shadow):
        decision._shadow = decision.Shadow(lambda: decision.Laya(router=pinned)) \
            if with_shadow else None
        times = []
        for t in texts:
            started = time.perf_counter()
            decision.decide(t)
            times.append((time.perf_counter() - started) * 1000)
            time.sleep(0.2)             # a person does not speak 200 times a second
        stats = dict(decision._shadow.stats) if decision._shadow else {}
        if decision._shadow:
            decision._shadow.stop()
        return {"decide_ms_p50": pct(times, 0.5), "decide_ms_p95": pct(times, 0.95),
                "decide_ms_max": pct(times, 1.0), "shadow": stats}

    latency = {"rules_only": run(False), "rules_plus_shadow": run(True)}
    shadow_ms = [e.get("ms") for e in trace.read(limit=5000)
                 if e.get("stage") == "decide_shadow" and e.get("ms") is not None]
    latency["shadow_judgement_ms"] = {"p50": pct(shadow_ms, 0.5), "p95": pct(shadow_ms, 0.95),
                                      "n": len(shadow_ms)}

    real = heard(args.trace, 500)
    laya = decision.Laya(router=pinned)
    rules = decision.Rules()
    pairs, disagree = Counter(), Counter()
    for t in real:
        r = rules.decide(t)
        j = laya.judge(t)
        rl = r.lane if r else "none"
        sl = j[0].lane if j else "failed"
        pairs[f"{rl}->{sl}"] += 1
        fast_r, fast_s = rl in ("fast", "instant"), sl == "fast"
        if fast_r != fast_s:
            disagree["laya_fast_rules_not" if fast_s else "rules_fast_laya_not"] += 1
    out = {"latency": latency,
           "real_requests": {"n": len(real), "lane_pairs_rules_to_laya": dict(pairs),
                             "fast_disagreements": dict(disagree), "words_logged": False}}
    print(json.dumps(out, indent=1))
    if args.out:
        json.dump(out, open(args.out, "w"), indent=1)


if __name__ == "__main__":
    main()
