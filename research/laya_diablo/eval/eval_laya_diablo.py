#!/usr/bin/env python3
"""
Diablo's evaluation of decision engines on the Diablo decision dataset.

    # Rules alone (no Laya needed)
    python research/laya_diablo/eval/eval_laya_diablo.py score --engine rules

    # Laya: the stock typed-decisions checkpoint, or a Diablo-trained one
    python .../eval_laya_diablo.py score --engine laya --model typed-decisions --device mps
    python .../eval_laya_diablo.py score --engine laya --checkpoint path/to/ckpt

    # Rules where they are sure, Laya where they are not
    python .../eval_laya_diablo.py score --engine hybrid --model typed-decisions

    python .../eval_laya_diablo.py calibrate --model typed-decisions      # calibration split only
    python .../eval_laya_diablo.py robustness --model typed-decisions     # option order
    python .../eval_laya_diablo.py scaling --model typed-decisions        # 4/8/12/20 options
    python .../eval_laya_diablo.py longrun --model typed-decisions --n 3000

Runs on local files only. `score` defaults to the test split; test_private
is scored only with --split test_private, once, at the end.
"""

import argparse
import json
import math
import os
import random
import resource
import statistics
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "dataset")
DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

FAST_LANES = ("fast", "instant")


def load(version, split):
    with open(os.path.join(DATA, version, f"{split}.jsonl")) as f:
        return [json.loads(line) for line in f]


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def pct(values, q):
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))], 2) if s else None


# ─── Engines ─────────────────────────────────────────────────────────────────

def decision_module():
    from skills import decision
    return decision


class RulesEngine:
    name = "rules"

    def __init__(self):
        self.d = decision_module()
        self.rules = self.d.Rules()

    def predict(self, ex, order=None):
        v = self.rules.decide(ex["text"], in_conversation=bool(ex["history"]))
        if v is None:
            return {"lane": "none", "intent": None, "needs_screen": None, "complete": None}
        return {"lane": v.lane, "intent": v.intent, "needs_screen": v.needs_screen,
                "complete": v.complete}


def softmax_t(probs, t):
    logits = {k: math.log(max(v, 1e-9)) / t for k, v in probs.items()}
    m = max(logits.values())
    z = sum(math.exp(v - m) for v in logits.values())
    return {k: math.exp(v - m) / z for k, v in logits.items()}


def sigmoid_t(p, t):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return 1 / (1 + math.exp(-math.log(p / (1 - p)) / t))


class LayaEngine:
    """The Diablo adapter's contract over a stock or Diablo-trained checkpoint."""

    name = "laya"

    def __init__(self, model="typed-decisions", checkpoint=None, device="cpu", calibration=None,
                 predict_raw=None):
        self.d = decision_module()
        started = time.perf_counter()
        if predict_raw is not None:
            self.predict_raw = predict_raw
        elif checkpoint:
            from laya import Agent
            agent = Agent(checkpoint, device=device)
            self.predict_raw = lambda state, questions: agent.predict(state, questions)
        else:
            from laya import Router
            router = Router(device=device, max_loaded=1)
            self.predict_raw = lambda state, questions: router.predict(state, questions,
                                                                       model=model)
        self.predict_raw({"request": "warm up"}, self.d.LAYA_QUESTIONS)
        self.cold_s = round(time.perf_counter() - started, 2)
        self.calibration = calibration or {}

    def questions(self, order=None):
        q = json.loads(json.dumps(self.d.LAYA_QUESTIONS))
        if order:
            crit = q["intent"]["criteria"]
            q["intent"]["criteria"] = {k: crit[k] for k in order}
        return q

    def answers(self, ex, order=None):
        state = self.d.laya_state(ex["text"], ex["history"], ex["context"].get("app", ""),
                                  ex["context"].get("window", ""))
        return (self.predict_raw(state, self.questions(order)) or {}).get("answers") or {}

    def predict(self, ex, order=None):
        a = self.answers(ex, order)
        probs = dict((a.get("intent") or {}).get("probabilities") or {})
        if self.calibration.get("intent") and probs:
            probs = softmax_t(probs, self.calibration["intent"])
        intent = max(probs, key=probs.get) if probs else (a.get("intent") or {}).get("choice")

        def noul(key):
            p = (a.get(key) or {}).get("noul")
            if p is None:
                return None
            return sigmoid_t(p, self.calibration[key]) if self.calibration.get(key) else p
        p_screen, p_complete = noul("needs_screen"), noul("complete")
        screen = p_screen is not None and p_screen >= 0.5
        complete = p_complete is not None and p_complete >= 0.5
        return {"lane": self.d.lane_for(intent, screen, complete), "intent": intent,
                "needs_screen": screen, "complete": complete,
                "p_intent": probs.get(intent, 0.0), "p_screen": p_screen,
                "p_complete": p_complete}


class HybridEngine:
    """Rules where they give a verdict; Laya only where Rules have no opinion."""

    name = "hybrid"

    def __init__(self, laya):
        self.rules, self.laya = RulesEngine(), laya

    def predict(self, ex, order=None):
        r = self.rules.predict(ex, order)
        if r["lane"] != "none":
            return dict(r, source="rules")
        return dict(self.laya.predict(ex, order), source="laya")


# ─── Scoring ─────────────────────────────────────────────────────────────────

def ece(conf, correct, bins=10):
    if not conf:
        return None
    total, err = len(conf), 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, c in enumerate(conf) if lo <= c < hi or (b == bins - 1 and c == 1.0)]
        if idx:
            acc = sum(correct[i] for i in idx) / len(idx)
            avg = sum(conf[i] for i in idx) / len(idx)
            err += len(idx) / total * abs(acc - avg)
    return round(err, 4)


def score(engine, rows):
    preds, times = [], []
    for ex in rows:
        started = time.perf_counter()
        p = engine.predict(ex, ex["options"]["intent"])
        times.append((time.perf_counter() - started) * 1000)
        preds.append(p)
    gold_fast = [e["gold_answers"]["lane"] == "fast" for e in rows]
    pred_fast = [p["lane"] in FAST_LANES for p in preds]
    n = len(rows)
    ff = [i for i in range(n) if pred_fast[i] and not gold_fast[i]]
    fa = [i for i in range(n) if not pred_fast[i] and gold_fast[i]]
    report = {
        "engine": engine.name, "n": n,
        "lane_accuracy": round(sum(pred_fast[i] == gold_fast[i] for i in range(n)) / n, 4),
        "false_fast": len(ff), "false_fast_rate": round(len(ff) / max(1, n - sum(gold_fast)), 4),
        "false_fast_examples": [rows[i]["text"] for i in ff[:20]],
        "false_agent": len(fa), "fast_recall": round(1 - len(fa) / max(1, sum(gold_fast)), 4),
        "abstained": sum(1 for p in preds if p["lane"] == "none"),
        "latency_ms": {"p50": pct(times, 0.5), "p95": pct(times, 0.95), "max": pct(times, 1.0)},
        "peak_rss_mb": round(rss_mb(), 1),
    }
    per_q = {}
    for q in ("intent", "needs_screen", "complete"):
        pairs = [(p[q], e["gold_answers"][q]) for p, e in zip(preds, rows) if p.get(q) is not None]
        per_q[q] = {"answered": len(pairs),
                    "accuracy": round(sum(a == b for a, b in pairs) / len(pairs), 4) if pairs else None}
    report["per_question"] = per_q
    recall = {}
    for cls in sorted({e["gold_answers"]["intent"] for e in rows}):
        idx = [i for i, e in enumerate(rows) if e["gold_answers"]["intent"] == cls]
        recall[cls] = round(sum(preds[i].get("intent") == cls for i in idx) / len(idx), 4)
    report["per_class_recall"] = recall
    report["macro_intent_accuracy"] = round(statistics.mean(recall.values()), 4) if recall else None
    idx = [i for i, e in enumerate(rows) if e["metadata"].get("phenomenon") == "hard_negative"]
    report["hard_negative_lane_accuracy"] = round(
        sum(pred_fast[i] == gold_fast[i] for i in idx) / max(1, len(idx)), 4)
    safety = [i for i, e in enumerate(rows) if e["safety_class"] not in ("none", "advice")]
    report["safety"] = {"n": len(safety),
                        "false_fast": sum(1 for i in safety if pred_fast[i] and not gold_fast[i])}
    confs = [p.get("p_intent") for p in preds if p.get("p_intent") is not None]
    if confs:
        correct = [int(p["intent"] == e["gold_answers"]["intent"])
                   for p, e in zip(preds, rows) if p.get("p_intent") is not None]
        report["calibration"] = {"intent_ece": ece(confs, correct)}
        for q, key in (("needs_screen", "p_screen"), ("complete", "p_complete")):
            ps = [(p[key], e["gold_answers"][q]) for p, e in zip(preds, rows) if p.get(key) is not None]
            report["calibration"][f"{q}_brier"] = round(
                statistics.mean((pp - float(g)) ** 2 for pp, g in ps), 4) if ps else None
    report["_preds"] = preds
    return report


def disagreements(rows, a, b, name_a, name_b):
    out, kinds = [], Counter()
    for ex, pa, pb in zip(rows, a, b):
        fa, fb = pa["lane"] in FAST_LANES, pb["lane"] in FAST_LANES
        if fa == fb:
            continue
        gold = ex["gold_answers"]["lane"] == "fast"
        kind = (f"{name_a}_correct" if fa == gold else
                f"{name_b}_correct" if fb == gold else "both_wrong")
        kinds[kind] += 1
        out.append({"id": ex["id"], "text": ex["text"], "gold_lane": ex["gold_answers"]["lane"],
                    name_a: pa["lane"], name_b: pb["lane"], "kind": kind,
                    "review": "pending: adjudicate as correct / label problem / ambiguous"})
    return dict(kinds), out


def partials(engine, rows):
    seqs = defaultdict(list)
    for e in rows:
        if "sequence" in e["metadata"]:
            seqs[e["metadata"]["sequence"]].append(e)
    early_fast, flips, total = 0, 0, 0
    for items in seqs.values():
        items.sort(key=lambda e: e["metadata"]["position"])
        lanes = [engine.predict(e)["lane"] for e in items]
        total += len(items) - 1
        early_fast += sum(1 for lane in lanes[:-1] if lane in FAST_LANES)
        flips += sum(1 for x, y in zip(lanes, lanes[1:]) if x != y)
    return {"sequences": len(seqs), "early_partials": total, "fast_on_partial": early_fast,
            "lane_changes": flips}


# ─── Commands ────────────────────────────────────────────────────────────────

def make_laya(args, calibration=None):
    return LayaEngine(model=args.model, checkpoint=args.checkpoint, device=args.device,
                      calibration=calibration)


def load_calibration(path):
    if not path:
        return None
    with open(path) as f:
        return json.load(f)["temperature"]


def cmd_score(args):
    rows = load(args.version, args.split)
    if args.engine == "rules":
        engine = RulesEngine()
    else:
        laya = make_laya(args, load_calibration(args.calibration))
        engine = laya if args.engine == "laya" else HybridEngine(laya)
    report = score(engine, rows)
    report.update(split=args.split, dataset_version=args.version,
                  model=args.checkpoint or (args.model if args.engine != "rules" else ""),
                  device=args.device if args.engine != "rules" else "",
                  calibrated=bool(args.calibration) if args.engine != "rules" else None)
    if args.engine != "rules":
        report["cold_s"] = (engine.laya if args.engine == "hybrid" else engine).cold_s
    preds = report.pop("_preds")
    if args.engine != "rules":
        rules = score(RulesEngine(), rows)
        kinds, queue = disagreements(rows, rules.pop("_preds"), preds, "rules", args.engine)
        report["disagreements_vs_rules"] = kinds
        if args.queue:
            with open(args.queue, "w") as f:
                for item in queue:
                    f.write(json.dumps(item) + "\n")
    report["partials"] = partials(engine, rows)
    print(json.dumps(report, indent=1))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(report, f, indent=1)


def fit_temperature(pairs, kind):
    """One temperature per question: golden-section search on the NLL."""
    def nll(t):
        total = 0.0
        for probs, gold in pairs:
            if kind == "choice":
                q = softmax_t(probs, t)
                total -= math.log(max(q.get(gold, 1e-12), 1e-12))
            else:
                q = sigmoid_t(probs, t)
                total -= math.log(q if gold else 1 - q)
        return total / max(1, len(pairs))
    lo, hi = 0.1, 10.0
    for _ in range(60):
        a, b = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
        if nll(a) < nll(b):
            hi = b
        else:
            lo = a
    t = (lo + hi) / 2
    return round(t, 4), round(nll(1.0), 4), round(nll(t), 4)


def cmd_calibrate(args):
    rows = load(args.version, "calibration")
    laya = make_laya(args)
    choice, screen, complete = [], [], []
    for ex in rows:
        a = laya.answers(ex, ex["options"]["intent"])
        choice.append(((a.get("intent") or {}).get("probabilities") or {},
                       ex["gold_answers"]["intent"]))
        screen.append(((a.get("needs_screen") or {}).get("noul", 0.5),
                       ex["gold_answers"]["needs_screen"]))
        complete.append(((a.get("complete") or {}).get("noul", 0.5), ex["gold_answers"]["complete"]))
    out = {"temperature": {}, "nll_before_after": {}}
    for name, pairs, kind in (("intent", choice, "choice"), ("needs_screen", screen, "noul"),
                              ("complete", complete, "noul")):
        t, before, after = fit_temperature(pairs, kind)
        out["temperature"][name] = t
        out["nll_before_after"][name] = [before, after]
    manifest = json.load(open(os.path.join(DATA, args.version, "manifest.json")))
    out.update(fitted_on_split="calibration", dataset_version=args.version,
               calibration_split_sha256=manifest["split_hashes"]["calibration"],
               model=args.checkpoint or args.model, n=len(rows))
    print(json.dumps(out, indent=1))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, indent=1)


def cmd_robustness(args):
    rows = load(args.version, "validation")[: args.n]
    laya = make_laya(args)
    rng = random.Random(7)
    stable, flips = 0, []
    intents = list(laya.d.LAYA_INTENTS)
    for ex in rows:
        seen = set()
        for _ in range(args.orders):
            order = intents[:]
            rng.shuffle(order)
            seen.add(laya.predict(ex, order)["intent"])
        if len(seen) == 1:
            stable += 1
        else:
            flips.append((ex["text"], sorted(seen)))
    out = {"examples": len(rows), "orders_each": args.orders,
           "intent_stable_across_orders": round(stable / max(1, len(rows)), 4),
           "examples_that_flip": flips[:15]}
    print(json.dumps(out, indent=1))
    if args.out:
        json.dump(out, open(args.out, "w"), indent=1)


def cmd_scaling(args):
    laya = make_laya(args)
    rows = load(args.version, "validation")[: args.n]
    base = dict(laya.d.LAYA_INTENTS)
    out = {}
    for k in (4, 8, 12, 20):
        gold_offered = [e for e in rows if e["gold_answers"]["intent"] in list(base)[:k]]
        criteria = dict(list(base.items())[:min(k, len(base))])
        for i in range(max(0, k - len(criteria))):
            criteria[f"other_{i}"] = f"an unrelated kind of request, number {i}"
        q = {"intent": {"type": "choice", "instructions": "What does the user want?",
                        "criteria": criteria}}
        times, correct = [], 0
        for ex in gold_offered:
            state = laya.d.laya_state(ex["text"], ex["history"])
            started = time.perf_counter()
            a = (laya.predict_raw(state, q) or {}).get("answers") or {}
            times.append((time.perf_counter() - started) * 1000)
            correct += int((a.get("intent") or {}).get("choice") == ex["gold_answers"]["intent"])
        out[k] = {"n": len(gold_offered), "p50_ms": pct(times, 0.5), "p95_ms": pct(times, 0.95),
                  "accuracy_where_gold_offered": round(correct / max(1, len(gold_offered)), 4),
                  "rss_mb": round(rss_mb(), 1)}
    print(json.dumps(out, indent=1))
    if args.out:
        json.dump(out, open(args.out, "w"), indent=1)


def cmd_longrun(args):
    laya = make_laya(args)
    rows = load(args.version, "train")
    rng = random.Random(11)
    samples, errors, times = [], 0, []
    for i in range(args.n):
        ex = rng.choice(rows)
        started = time.perf_counter()
        try:
            p = laya.predict(ex)
            if p["intent"] not in laya.d.LAYA_INTENTS:
                errors += 1
        except Exception:
            errors += 1
        times.append((time.perf_counter() - started) * 1000)
        if i % 250 == 0 or i == args.n - 1:
            samples.append((i, round(rss_mb(), 1)))
    tenth = max(1, len(times) // 10)
    out = {"decisions": args.n, "errors": errors, "rss_samples": samples,
           "rss_growth_mb": round(samples[-1][1] - samples[1][1], 1) if len(samples) > 2 else None,
           "p50_first_tenth_ms": pct(times[:tenth], 0.5), "p50_last_tenth_ms": pct(times[-tenth:], 0.5),
           "p95_ms": pct(times, 0.95)}
    print(json.dumps(out, indent=1))
    if args.out:
        json.dump(out, open(args.out, "w"), indent=1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("score", "calibrate", "robustness", "scaling", "longrun"):
        p = sub.add_parser(name)
        p.add_argument("--version", default="v1")
        p.add_argument("--model", default="typed-decisions")
        p.add_argument("--checkpoint", default=None)
        p.add_argument("--device", default="cpu")
        p.add_argument("--out", default="")
        if name == "score":
            p.add_argument("--engine", default="rules", choices=("rules", "laya", "hybrid"))
            p.add_argument("--split", default="test")
            p.add_argument("--calibration", default="")
            p.add_argument("--queue", default="")
        if name in ("robustness", "scaling"):
            p.add_argument("--n", type=int, default=120)
            p.add_argument("--orders", type=int, default=5)
        if name == "longrun":
            p.add_argument("--n", type=int, default=3000)
    args = ap.parse_args()
    {"score": cmd_score, "calibrate": cmd_calibrate, "robustness": cmd_robustness,
     "scaling": cmd_scaling, "longrun": cmd_longrun}[args.cmd](args)


if __name__ == "__main__":
    main()
