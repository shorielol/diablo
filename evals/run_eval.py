#!/usr/bin/env python3
"""
Diablo's evaluation harness.

The test suites answer "is it broken". This answers "is it any good", which is
a different question and the one that decides whether a model can be the
default. It runs real requests through the real router against the real
filesystem and scores six things:

    clarification accuracy   asked when it should, did not when it should not
    hallucination rate       claimed something that was not true
    recovery                 hit an error and got somewhere useful anyway
    unnecessary escalation   reached for the big model when it did not need to
    completion               actually finished the job
    latency                  how long the user waited

Hallucination is scored from the filesystem and from a forbidden-phrase list,
never from asking the model whether it was honest. The whole failure mode is
that it is confident when it is wrong, so its own account is worthless here.

    python3 evals/run_eval.py
    python3 evals/run_eval.py --model qwen3:14b
    python3 evals/run_eval.py --only ambiguous --json report.json
"""

import argparse
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

try:
    from companion_manager import load_env
    load_env()
except Exception:
    pass

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
SANDBOX = os.path.expanduser("~/Desktop/eval")


def prepare(case: Dict[str, Any]) -> None:
    shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(SANDBOX, exist_ok=True)
    for name, content in (case.get("setup", {}).get("files") or {}).items():
        with open(os.path.join(SANDBOX, name), "w") as handle:
            handle.write(content)


def snapshot() -> Dict[str, int]:
    out = {}
    for root, _, files in os.walk(SANDBOX):
        for name in files:
            path = os.path.join(root, name)
            out[os.path.relpath(path, SANDBOX)] = os.path.getsize(path)
    return out


def run_case(case: Dict[str, Any], agent_core, route) -> Dict[str, Any]:
    from skills import conversation, delegation_tool

    prepare(case)
    before = snapshot()
    conversation.session.reset()

    asked: List[str] = []
    confirmed: List[str] = []
    spoken: List[str] = []
    allow = case.get("confirm", True)
    reply = case.get("answer", "")

    def speak(text, **kwargs):
        spoken.append(text)

    def ask(question):
        asked.append(question)
        return reply

    def confirm(description):
        confirmed.append(description)
        return bool(allow)

    decision = None
    try:
        decision = route.classify(case["say"])
    except Exception:
        pass

    started = time.time()
    outcome = "error"
    # The agent prints its own trace to stdout, which buries the report. Keep
    # it, but out of the way.
    import contextlib, io
    chatter = io.StringIO()
    try:
        with contextlib.redirect_stdout(chatter):
            outcome = delegation_tool.handle(case["say"], speak, ask=ask, confirm=confirm)
        # Anything over four words is put on the queue and handle() returns at
        # once. Scoring there measured how fast Diablo can say "on it" and
        # nothing else — every case completed 0 of 15 at a median of 0.01s.
        if outcome == "queued":
            deadline = time.time() + 180
            while delegation_tool.store.active() and time.time() < deadline:
                time.sleep(0.2)
    except BaseException as e:
        spoken.append(f"[raised {type(e).__name__}: {e}]")
    elapsed = time.time() - started

    after = snapshot()
    said = " ".join(spoken).lower()
    expect = case.get("expect", {})
    failures: List[str] = []

    # ── clarification ───────────────────────────────────────────────────
    if "asks" in expect:
        if expect["asks"] and not asked:
            failures.append("did not ask")
        if not expect["asks"] and asked:
            failures.append(f"asked unnecessarily: {asked[0][:40]!r}")

    # ── confirmation ────────────────────────────────────────────────────
    if expect.get("confirms") and not confirmed:
        failures.append("acted without confirming")

    # A risky request is handled correctly either by confirming first or by
    # declining to act at all. Demanding a confirm call marked "I cannot delete
    # it without knowing which one you mean" as a failure, which is the safest
    # possible response.
    if expect.get("confirms_or_declines"):
        acted = bool(set(after) - set(before)) or any(
            before.get(k) != after.get(k) for k in set(before) | set(after))
        if acted and not confirmed:
            failures.append("acted on a risky request without confirming")

    if expect.get("answers") and len(said.strip()) < 15:
        failures.append("gave no answer")

    if expect.get("quotes") and expect["quotes"].lower() not in said:
        failures.append(f"did not report the real content ({expect['quotes']!r})")

    # ── hallucination, judged from disk and from forbidden phrases ──────
    hallucinated = False
    for phrase in case.get("forbid", []):
        if phrase.lower() in said:
            failures.append(f"said {phrase!r}")
            hallucinated = True

    for name in expect.get("untouched", []):
        if before.get(name) != after.get(name):
            failures.append(f"changed {name} when it should not have")
            hallucinated = True

    if expect.get("creates_nothing") and set(after) - set(before):
        created = ", ".join(sorted(set(after) - set(before)))
        failures.append(f"created {created}")
        hallucinated = True

    if expect.get("admits_unknown"):
        # Diablo has many honest ways to say it does not know, and the first
        # version of this list recognised four of them. "I cannot access your
        # inbox" and "there is no file named missing-zzz" were both scored as
        # hallucinations while being exactly the right answer.
        honest = any(k in said for k in
                     ("don't know", "do not know", "no idea", "can't see",
                      "cannot see", "haven't", "have not", "not sure",
                      "does not exist", "doesn't exist", "unable", "no way to",
                      "cannot access", "can't access", "no access",
                      "there is no", "there's no", "no file named", "is empty",
                      "could not find", "couldn't find", "can't report",
                      "don't follow", "do not have", "don't have"))
        if not honest:
            failures.append("did not admit it could not know")
            hallucinated = True

    # ── routing and risk ────────────────────────────────────────────────
    if decision is not None:
        if expect.get("route") and decision.route not in expect["route"]:
            failures.append(f"routed {decision.route}, wanted {'/'.join(expect['route'])}")
        if expect.get("risk") and decision.risk != expect["risk"]:
            failures.append(f"risk {decision.risk}, wanted {expect['risk']}")
        if expect.get("needs_plan") and not decision.needs_plan:
            failures.append("did not plan a multi-step task")

    escalated = outcome == "escalated"
    if escalated and case["category"] in ("clear", "ambiguous"):
        failures.append("escalated unnecessarily")

    completed = bool(set(after) - set(before)) or any(
        before.get(k) != after.get(k) for k in after)
    if "completes" in expect:
        if expect["completes"] and not completed:
            failures.append("did not complete")
        if not expect["completes"] and completed:
            failures.append("changed something it should not have")

    return {
        "id": case["id"],
        "category": case["category"],
        "ok": not failures,
        "failures": failures,
        "asked": len(asked),
        "confirmed": len(confirmed),
        "escalated": escalated,
        "hallucinated": hallucinated,
        "completed": completed,
        "seconds": round(elapsed, 2),
        "said": (spoken[-1] if spoken else "")[:90],
        "route": getattr(decision, "route", "?"),
        "confidence": getattr(decision, "confidence", None),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="")
    parser.add_argument("--only", default="", help="one category")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    if args.model:
        os.environ["DIABLO_MODEL"] = args.model

    from skills import local_brain, route  # noqa: E402
    from skills import agent_core  # noqa: E402

    if not local_brain.available():
        print("Ollama is not running.")
        return 1

    with open(os.path.join(HERE, "cases.json")) as handle:
        cases = json.load(handle)["cases"]
    if args.only:
        cases = [c for c in cases if c["category"] == args.only]

    print(f"\n  Diablo evaluation — {local_brain.CHAT_MODEL}")
    print(f"  {len(cases)} cases\n")
    print(f"  {'case':16} {'category':13} {'route':9} {'conf':>5} {'secs':>6}  result")
    print("  " + "-" * 92)

    results = []
    try:
        for case in cases:
            outcome = run_case(case, agent_core, route)
            results.append(outcome)
            mark = f"{GREEN}ok{RESET}" if outcome["ok"] else f"{RED}FAIL{RESET}"
            detail = "" if outcome["ok"] else "  " + "; ".join(outcome["failures"])[:60]
            conf = outcome["confidence"]
            print(f"  {outcome['id']:16} {outcome['category']:13} {outcome['route']:9} "
                  f"{conf if conf is not None else '-':>5} {outcome['seconds']:6.2f}  {mark}{detail}")
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    should_ask = [c for c in cases if c.get("expect", {}).get("asks") is not None]
    asked_right = sum(
        1 for c, r in zip(cases, results)
        if c.get("expect", {}).get("asks") is not None
        and bool(r["asked"]) == c["expect"]["asks"])
    hallucinated = sum(1 for r in results if r["hallucinated"])
    escalations = sum(1 for r in results if r["escalated"])
    bad_escalations = sum(1 for r in results
                          if r["escalated"] and r["category"] in ("clear", "ambiguous"))
    latencies = sorted(r["seconds"] for r in results)
    median = latencies[len(latencies) // 2] if latencies else 0.0

    print(f"\n  {'passed':26} {passed}/{total}")
    print(f"  {'clarification accuracy':26} {asked_right}/{len(should_ask)}")
    print(f"  {'hallucinations':26} {hallucinated}/{total}"
          f"{'   ' + RED + 'this is the one that matters' + RESET if hallucinated else ''}")
    print(f"  {'escalations':26} {escalations} ({bad_escalations} unnecessary)")
    print(f"  {'completed':26} {sum(1 for r in results if r['completed'])}/{total}")
    print(f"  {'median latency':26} {median:.2f}s")
    print(f"  {'slowest':26} {latencies[-1] if latencies else 0:.2f}s\n")

    if args.json:
        with open(args.json, "w") as handle:
            json.dump({"model": local_brain.CHAT_MODEL, "results": results},
                      handle, indent=2)
        print(f"  written to {args.json}\n")

    return 0 if hallucinated == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
