#!/usr/bin/env python3
"""
The promotion gate. Reads measured reports and says PROMOTE, HYBRID or
KEEP_EXPERIMENTAL. It never promotes anything by itself: the decision is a
report, and the runtime only changes if a person acts on it.

    python research/laya_diablo/eval/promotion.py \
        --rules reports/rules_test.json --laya reports/laya_test.json \
        --hybrid reports/hybrid_test.json --longrun reports/longrun.json \
        --fine-tuned false --calibrated true --out reports/promotion.json

Every threshold is fixed here, before any result is read, so a result cannot
move the bar. The bar for authority is the brief's section 23.
"""

import argparse
import json

LATENCY_P95_MS = 150.0          # a decision costing more than this is slower than it saves
RSS_BUDGET_MB = 1500.0          # the Mac already swaps; more than this is not affordable
RSS_GROWTH_MB = 100.0           # over a long run of 3000 decisions
MIN_GENERALISATION_GAIN = 0.10  # fast recall above rules, on the same test set
MAX_ECE = 0.10


def check(name, ok, detail):
    return {"criterion": name, "pass": bool(ok), "detail": detail}


def gate(candidate, rules, longrun=None, fine_tuned=False, calibrated=False):
    """Every criterion for one candidate (laya or hybrid) against rules."""
    c = [
        check("trained on Diablo data", fine_tuned,
              "a stock checkpoint is not eligible for authority"),
        check("calibrated on the calibration split", calibrated, ""),
        check("lane accuracy at least rules'",
              candidate["lane_accuracy"] >= rules["lane_accuracy"],
              f"{candidate['lane_accuracy']} vs {rules['lane_accuracy']}"),
        check("no more false-fast than rules", candidate["false_fast"] <= rules["false_fast"],
              f"{candidate['false_fast']} vs {rules['false_fast']}"),
        check("zero false-fast on safety boundaries", candidate["safety"]["false_fast"] == 0,
              f"{candidate['safety']['false_fast']} of {candidate['safety']['n']}"),
        check("better generalisation (fast recall)",
              candidate["fast_recall"] >= rules["fast_recall"] + MIN_GENERALISATION_GAIN,
              f"{candidate['fast_recall']} vs {rules['fast_recall']} (+{MIN_GENERALISATION_GAIN})"),
    ]
    p = candidate.get("partials") or {}
    c.append(check("no fast lane on a partial transcript", p.get("fast_on_partial", 0) == 0,
                   json.dumps(p)))
    p95 = (candidate.get("latency_ms") or {}).get("p95")
    c.append(check("warm latency", p95 is not None and p95 <= LATENCY_P95_MS,
                   f"p95 {p95} ms <= {LATENCY_P95_MS}"))
    c.append(check("memory", candidate["peak_rss_mb"] <= RSS_BUDGET_MB,
                   f"{candidate['peak_rss_mb']} MB <= {RSS_BUDGET_MB}"))
    ece = (candidate.get("calibration") or {}).get("intent_ece")
    c.append(check("calibration error", ece is not None and ece <= MAX_ECE, f"ECE {ece}"))
    if longrun is None:
        c.append(check("long-run stability", False, "no long-run report"))
    else:
        c.append(check("long-run stability", longrun["errors"] == 0 and
                       (longrun.get("rss_growth_mb") or 0) <= RSS_GROWTH_MB,
                       f"errors {longrun['errors']}, RSS growth {longrun.get('rss_growth_mb')} MB"))
    return c


def decide(rules, laya=None, hybrid=None, longrun=None, fine_tuned=False, calibrated=False):
    out = {"rules_baseline": {k: rules[k] for k in ("lane_accuracy", "false_fast", "fast_recall")}}
    verdict = "KEEP_EXPERIMENTAL"
    if laya is not None:
        out["laya"] = gate(laya, rules, longrun, fine_tuned, calibrated)
        if all(x["pass"] for x in out["laya"]):
            verdict = "PROMOTE"
    if hybrid is not None and verdict != "PROMOTE":
        out["hybrid"] = gate(hybrid, rules, longrun, fine_tuned, calibrated)
        if all(x["pass"] for x in out["hybrid"]):
            verdict = "HYBRID"
    out["decision"] = verdict
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", required=True)
    ap.add_argument("--laya")
    ap.add_argument("--hybrid")
    ap.add_argument("--longrun")
    ap.add_argument("--fine-tuned", default="false")
    ap.add_argument("--calibrated", default="false")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    def load(path):
        return json.load(open(path)) if path else None
    out = decide(load(a.rules), load(a.laya), load(a.hybrid), load(a.longrun),
                 a.fine_tuned.lower() == "true", a.calibrated.lower() == "true")
    print(json.dumps(out, indent=1))
    if a.out:
        json.dump(out, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
