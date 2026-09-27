#!/usr/bin/env python3
"""
Turns the Diablo decision dataset into Laya training items: one tokenised
sequence per (example, question), with its soft target, weight and the
option order it was rendered in.

    python prepare_items.py --model-dir <laya checkpoint dir> \
        --config ../configs/train_v1.json --out items/

Writes train.pt, validation.pt, calibration.pt. The test splits are never
read here. Uses Laya's own sequence builder, so a training sequence is the
sequence inference will see.
"""

import argparse
import json
import math
import os
import random
import statistics
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
CONTRACT = json.load(open(os.path.join(RESEARCH, "contract.json")))
QUESTIONS = CONTRACT["questions"]


def diablo_state(ex):
    """The state the runtime adapter builds (decision.laya_state), from an example."""
    state = {"request": ex["text"].strip()[:400]}
    if ex["history"]:
        state["history"] = [str(h)[:160] for h in ex["history"][-2:]]
    if ex["context"].get("app"):
        state["app"] = str(ex["context"]["app"])[:40]
    if ex["context"].get("window"):
        state["window"] = str(ex["context"]["window"])[:80]
    return state


def load(path):
    with open(path) as f:
        return [json.loads(line) for line in f]


def class_weights(rows):
    counts = Counter(e["gold_answers"]["intent"] for e in rows)
    median = statistics.median(counts.values())
    return {k: min(2.0, max(0.5, math.sqrt(median / v))) for k, v in counts.items()}


def targets(question, ex, order, smoothing):
    gold = ex["gold_answers"][question]
    if QUESTIONS[question]["type"] == "choice":
        k = len(order)
        return [(1 - smoothing) + smoothing / k if key == gold else smoothing / k for key in order]
    p_true = (1 - smoothing) if gold else smoothing
    return [1 - p_true, p_true]          # noul order is always [false, true]


def items_for(ex, tok, cfg, weights, rng, augment, build_sequence, render_options, qtypes):
    out = []
    state = diablo_state(ex)
    weight = weights.get(ex["gold_answers"]["intent"], 1.0)
    if ex["metadata"].get("phenomenon") == "hard_negative" or ex["safety_class"] != "none":
        weight *= cfg["weighting"]["hard_negative_and_safety_boost"]
    for question, spec in QUESTIONS.items():
        orders = [None]
        if spec["type"] == "choice":
            orders = [ex["options"]["intent"]]
            for _ in range(augment):
                extra = list(spec["criteria"])
                rng.shuffle(extra)
                orders.append(extra)
        for order in orders:
            crit = {k: spec["criteria"][k] for k in order} if order else spec.get("criteria")
            q = {"t": spec["type"], "ins": spec["instructions"], "crit": crit}
            seq, markers = build_sequence(tok, state, q, cfg["sequence"]["max_len"],
                                          cfg["sequence"]["head_max_len"])
            if len(markers) != len(render_options(q)):
                continue
            target = targets(question, ex, order or [False, True],
                             cfg["targets"]["label_smoothing"])
            out.append({"ids": seq, "markers": markers, "qtype": qtypes[spec["type"]],
                        "target": target, "label": target.index(max(target)), "weight": weight,
                        "example_id": ex["id"], "question": question,
                        "options": list(order) if order else ["false", "true"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--config", default=os.path.join(RESEARCH, "configs", "train_v1.json"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="first N examples per split (checks)")
    args = ap.parse_args()
    import torch
    from transformers import AutoTokenizer
    from laya.agent import _fix_tokenizer_config
    from laya.common import QTYPES, build_sequence, render_options

    cfg = json.load(open(args.config))
    _fix_tokenizer_config(args.model_dir)
    tok = AutoTokenizer.from_pretrained(os.path.join(args.model_dir, "tokenizer"))
    os.makedirs(args.out, exist_ok=True)
    weights = class_weights(load(os.path.join(RESEARCH, cfg["data"]["train"])))
    summary = {"class_weights": weights}
    for split in ("train", "validation", "calibration"):
        rows = load(os.path.join(RESEARCH, cfg["data"][split]))
        if args.limit:
            rows = rows[: args.limit]
        rng = random.Random(f"{cfg['run_name']}:{split}")
        augment = cfg["weighting"]["option_order_augment"] if split == "train" else 0
        items = []
        for ex in rows:
            items.extend(items_for(ex, tok, cfg, weights if split == "train" else {}, rng,
                                   augment, build_sequence, render_options, QTYPES))
        torch.save(items, os.path.join(args.out, f"{split}.pt"))
        lengths = [len(it["ids"]) for it in items]
        summary[split] = {"examples": len(rows), "items": len(items),
                          "max_tokens": max(lengths) if lengths else 0,
                          "mean_tokens": round(statistics.mean(lengths), 1) if lengths else 0}
    json.dump(summary, open(os.path.join(args.out, "items_summary.json"), "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
