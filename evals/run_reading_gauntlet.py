#!/usr/bin/env python3
"""
What the screen says: text, an error, a table, charts and diagrams, read by
the one model Diablo has.

    python3 evals/run_reading_gauntlet.py [--display ID] [--repeat N] [--json out.json]

Each question is asked the way Diablo asks one in production: a capture of the
window, handed to screen_context.read_image, which sends it to the brain model.
An answer is scored by whether it contains the fact the fixture drew there and
none of the facts it would be wrong to say. The facts are the fixture's own
invented data; nothing is read from a real screen.
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_grounding_gauntlet as G  # noqa: E402  (loads .env, sets the path)
from skills import local_brain, screen, screen_context  # noqa: E402

# (fixture, id, group, question, any-of required, none-of forbidden)
QUESTIONS = [
    ("reading_app", "T1-small", "text", "When is invoice INV-20931 due?",
     [["14 november", "november 14", "14th november"]], []),
    ("reading_app", "T2-dense", "text",
     "According to the paragraph, how many units per week does the Pune warehouse ship?",
     [["1,240", "1240"]], []),
    ("reading_app", "T3-error", "text",
     "What does the error on this screen mean, and when can I retry?",
     [["503", "connection failed", "could not connect", "connection"],
      ["30 seconds", "30 s"]], []),
    ("reading_app", "T4-sizes", "text", "Who prepared the quarterly review?",
     [["ana ruiz"]], []),
    ("reading_app", "T5-caption", "text",
     "According to the caption, what does the Delete button do?",
     [["selected file"]], [["empties", "whole folder"]]),
    ("reading_app", "TABLE1-max", "table",
     "Which customer has the highest outstanding amount?", [["globex"]], [["acme"]]),
    ("reading_app", "TABLE2-row", "table",
     "Which customer's row contains order #10482?", [["initech"]], []),
    ("reading_app", "TABLE3-sum", "table",
     "What is the total of the Qty column? Give one number.", [["60"]], []),
    ("reading_app", "TABLE4-dense", "table",
     "What is Acme Ltd's outstanding amount?", [["3,918", "3918"]], [["3,981", "3981"]]),
    ("reading_app", "CHART2-line-max", "chart",
     "In the line chart, which month has the highest value?", [["jun"]], []),
    ("reading_app", "CHART3-trend", "chart",
     "Is the line chart's trend increasing or decreasing overall?",
     [["increas", "upward", "rising"]], [["decreas"]]),
    ("reading_app", "CHART4-pie", "chart",
     "Which category is the largest slice of the pie chart?", [["rent"]], []),
    ("reading_app", "DIAG1-branch", "diagram",
     "In the process diagram, at which step does the process branch?",
     [["check stock"]], []),
    ("reading_app", "DIAG2-next", "diagram",
     "In the process diagram, which step comes right after Order received?",
     [["check stock"]], []),
    ("chart_app", "CHART1-bar", "chart",
     "Which quarter has the tallest bar?", [["q4"]], []),
    ("diagram_app", "DIAG3-legend", "diagram",
     "According to the legend, what does the grey shaded region represent?",
     [["welfare loss", "deadweight"]], []),
]


def score(answer: str, required, forbidden) -> bool:
    text = (answer or "").lower()
    if not all(any(option in text for option in group) for group in required):
        return False
    return not any(any(option in text for option in group) for group in forbidden)


def run(repeat: int):
    records = []
    by_fixture = {}
    for q in QUESTIONS:
        by_fixture.setdefault(q[0], []).append(q)
    for fixture, questions in by_fixture.items():
        with G.Live(fixture) as live:
            if not live.started or live.covered:
                for q in questions:
                    records.append({"id": q[1], "group": q[2], "correct": None,
                                    "note": "INFRASTRUCTURE: fixture not visible"})
                continue
            for _ in range(repeat):
                for _f, ident, group, question, required, forbidden in questions:
                    frame = screen.capture_this_window(live.window)
                    began = time.time()
                    try:
                        answer = (screen_context.read_image(frame.path, question,
                                                            num_predict=200)
                                  if frame.ok else "Error: capture failed")
                    finally:
                        frame.release()
                    ms = round((time.time() - began) * 1000)
                    ok = score(answer, required, forbidden)
                    records.append({"id": ident, "group": group, "correct": ok, "ms": ms,
                                    "answer": (answer or "")[:240]})
                    print(f"{'OK  ' if ok else 'MISS'} {ident:16} {ms:6} ms  "
                          f"{(answer or '')[:110]!r}", flush=True)
    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--display", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    if args.display:
        G.FIXTURE_AT = G.placed_on(args.display)[1]
    records = run(args.repeat)
    groups = {}
    for r in records:
        if r["correct"] is None:
            continue
        g = groups.setdefault(r["group"], [0, 0, []])
        g[0] += bool(r["correct"])
        g[1] += 1
        g[2].append(r["ms"])
    summary = {k: {"correct": v[0], "asked": v[1], "p50_ms": G.percentile(v[2], 0.5),
                   "p95_ms": G.percentile(v[2], 0.95)} for k, v in groups.items()}
    summary["model"] = local_brain.CHAT_MODEL
    print(json.dumps(summary, indent=1))
    if args.json:
        json.dump({"summary": summary, "records": records}, open(args.json, "w"), indent=1)


if __name__ == "__main__":
    main()
