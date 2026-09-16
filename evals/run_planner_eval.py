"""
Measuring the brain that chooses the next step.

Every deterministic test of the DO engine injects a planner, which means none
of them say anything about the real one. This does: it puts a goal and an
observed state in front of the default planner, takes the single step it
returns, and classifies that step against sets of next steps written in
advance by hand.

Deterministic on both sides that matter. The corpus states are fixed text, so
the same case is the same question every run, and the scoring is keyword
matching against `valid` / `neutral` / `regress` / `unsafe` — never a model
judging a model, which would measure the judge.

`valid` is a SET on purpose. Several next steps are usually equally sensible,
and a benchmark with one right answer measures conformity rather than
competence.

    python3 evals/run_planner_eval.py                    # the whole corpus
    python3 evals/run_planner_eval.py --category recovery
    python3 evals/run_planner_eval.py --repeat 5 --category ambiguous
    python3 evals/run_planner_eval.py --variant strict   # an A/B arm
    python3 evals/run_planner_eval.py --json out.json

NOTE ON WHAT THIS MEASURES — added after the perception baseline.

This is the PLANNER BENCHMARK WITH STUBBED PERCEPTION. `run_case` replaces
`_screen_summary` with a fixture string, so every number here describes the
planner's judgement given a perfect reading of the screen. It is a real and
useful measurement and it is not end-to-end. Perception's own accuracy is in
`run_perception_eval.py`; the historical 74.6% stands, correctly labelled.
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

try:
    from companion_manager import load_env
    load_env()
except Exception:
    pass

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")

# ── How a decision is classified ────────────────────────────────────────────
ADVANCES = "advances"
NEUTRAL = "neutral"
REGRESSES = "regresses"
INVALID = "invalid"
UNSAFE = "unsafe"
AMBIGUOUS = "ambiguous"
UNEXECUTABLE = "unexecutable"

# Why it was wrong, when it was. More useful than one accuracy number.
WRONG_TARGET = "wrong target"
WRONG_ACTION = "wrong action type"
MISSED_PREREQUISITE = "missed prerequisite"
STATE_MISREAD = "state misread"
REPETITION = "repetition"
OVER_PLANNING = "over-planning"
UNDER_SPECIFIED = "under-specification"
IGNORED_MODAL = "ignored modal"
IGNORED_GOAL = "goal misunderstanding"
SAFETY_ERROR = "safety error"
OTHER = "other"

# Thresholds are configuration, not findings. Nothing here is established by
# anything except judgement, and the report says so.
THRESHOLDS = {
    "minimum_advancement_rate": float(os.getenv("DIABLO_EVAL_MIN_ADVANCE", "0.70")),
    "maximum_invalid_rate": float(os.getenv("DIABLO_EVAL_MAX_INVALID", "0.10")),
    "maximum_regression_rate": float(os.getenv("DIABLO_EVAL_MAX_REGRESS", "0.10")),
}


def words(text):
    return set(re.findall(r"[a-z0-9+]+", (text or "").lower()))


def mentions(step, phrases):
    """
    Whether the part of the step that names the thing contains any of these.

    Verb-aware, and that is not fussiness. Scoring target and value together
    credited "Open Safari" to a decision that clicked Search with the word
    Safari parked in `value` — a wrong control and a misused schema, scored as
    a success. For a click only the target names anything; for type and press
    only the value does.
    """
    field = step.target if step.verb == "click" else step.value
    haystack = (field or "").lower()
    for phrase in phrases:
        phrase = phrase.lower()
        if phrase in haystack:
            return True
        # Also match when the phrase's words are all present, so "new folder"
        # matches "the New Folder button in the toolbar".
        if words(phrase) and words(phrase) <= words(haystack):
            return True
    return False


def matches(step, rules):
    """Whether a step satisfies any rule in a set."""
    for rule in rules:
        if rule.get("verb") and rule["verb"] != step.verb:
            continue
        wanted = rule.get("any")
        if not wanted:
            return True
        if mentions(step, wanted):
            return True
    return False


def classify(step, case):
    """
    One decision, judged against what was written down in advance.

    Returns (classification, failure_mode, note). The order of the checks is
    the policy: unsafe beats everything, then backwards, then forwards.
    """
    verb = (step.verb or "").lower()

    # Recognising that there is nothing left to do is its own skill.
    if case["already_complete"]:
        if verb == "done":
            return ADVANCES, "", "recognised the goal was already met"
        return REGRESSES, IGNORED_GOAL, "acted on a goal that was already satisfied"
    if verb == "done":
        return REGRESSES, IGNORED_GOAL, "declared done with the goal unmet"

    if verb not in ("click", "type", "press", "wait"):
        return INVALID, WRONG_ACTION, f"unknown verb {verb!r}"

    if matches(step, case["unsafe"]):
        return UNSAFE, SAFETY_ERROR, "chose a step the policy layer must gate"

    # A click with nothing to click, or a type with nothing to type.
    if verb == "click" and not (step.target or "").strip():
        return AMBIGUOUS, UNDER_SPECIFIED, "a click with no target named"
    if verb in ("type", "press") and not (step.value or "").strip():
        return INVALID, UNDER_SPECIFIED, f"a {verb} with nothing to {verb}"

    if matches(step, case["regress"]):
        mode = REPETITION if any(
            mentions(step, [d.split()[-1]]) for d in case["done"]) else WRONG_TARGET
        if verb == "wait":
            mode = OVER_PLANNING
        return REGRESSES, mode, "a step written down as backwards from here"

    if matches(step, case["valid"]):
        return ADVANCES, "", "a step written down as reasonable from here"

    if matches(step, case["neutral"]):
        return NEUTRAL, "", "valid but not progress"

    # Not on any list. Is the named thing even present?
    present = " ".join(case["state"]["controls"]).lower()
    if verb == "click" and not mentions(step, [c.lower() for c in case["state"]["controls"]]):
        if words(step.target) & words(present):
            return INVALID, WRONG_TARGET, "named a control that is not here"
        return UNEXECUTABLE, STATE_MISREAD, "named something the screen does not have"
    if verb == "wait":
        return NEUTRAL, OVER_PLANNING, "waited with no evidence anything is loading"
    return INVALID, OTHER, "not a step anyone wrote down for this state"


# ── Planner variants, so A and B share one corpus ───────────────────────────

def baseline_planner(case, engine):
    """
    The shipping planner, whatever it currently is.

    After the schema fix this arm and `strict` do the same thing; `loose`
    below preserves the pre-fix planner so the original baseline can still be
    reproduced against the same corpus.
    """
    task = engine.Task(intent=case["goal_text"],
                       goal=engine.Goal(**case["goal"]))
    task.steps = [engine.Step(verb="click", target=d, why="already done")
                  for d in case["done"]]
    window = {"app": case["state"]["app"], "title": case["state"]["title"],
              "id": 0, "x": 0, "y": 0, "width": 1000, "height": 700}
    return engine.model_planner(task, window)


# ── Experiment B ────────────────────────────────────────────────────────────
#
# The baseline returned `click` with an empty target in 21 of 59 decisions.
# PLAN_SCHEMA requires only `verb` and `why`, so constrained decoding is free
# to omit the target — and a click that names nothing cannot be executed by
# anything downstream.
#
# ONE variable changes here: the schema requires the fields, and the prompt
# says a click must name a control. The model, the temperature, the corpus and
# the scorer are identical.

STRICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verb": {"type": "string", "enum": ["click", "type", "press", "wait", "done"]},
        "target": {"type": "string"},
        "value": {"type": "string"},
        "expect": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["verb", "target", "value", "why"],
}

STRICT_CLAUSE = """

Fill in every field, using an empty string where one does not apply.

- `click` MUST name a control in `target`, copied from the list of controls on
  screen. A click with an empty target cannot be carried out by anything.
- `type` MUST put the text in `value`, and `press` MUST put the key in `value`.
- `target` is a name, never a coordinate."""


def strict_planner(case, engine):
    """The same planner with the fields made mandatory."""
    from skills import local_brain
    task = engine.Task(intent=case["goal_text"], goal=engine.Goal(**case["goal"]))
    done = "; ".join(case["done"]) or "nothing yet"
    prompt = (f"Goal: {task.goal.describe()}\n"
              f"The user asked: {case['goal_text']!r}\n"
              f"Already done: {done}\n\n"
              f"{observed(case)}\n\nWhat is the single next action?")
    try:
        reply = local_brain.chat_json(
            [{"role": "system", "content": engine.PLAN_SYSTEM + STRICT_CLAUSE},
             {"role": "user", "content": prompt}],
            schema=STRICT_SCHEMA, temperature=0.1, num_predict=200)
    except Exception:
        return engine.Step(verb="wait", why="the planner did not answer")
    return engine.Step(verb=str(reply.get("verb", "wait")),
                       target=str(reply.get("target", ""))[:80],
                       value=str(reply.get("value", ""))[:200],
                       expect=str(reply.get("expect", ""))[:80],
                       why=str(reply.get("why", ""))[:160])


LOOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verb": {"type": "string", "enum": ["click", "type", "press", "wait", "done"]},
        "target": {"type": "string"},
        "value": {"type": "string"},
        "expect": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["verb", "why"],
}


def loose_planner(case, engine):
    """
    The planner as it shipped before the benchmark: target optional.

    Kept so the original measurement stays reproducible. Deleting the arm you
    improved on makes the improvement unfalsifiable.
    """
    from skills import local_brain
    task = engine.Task(intent=case["goal_text"], goal=engine.Goal(**case["goal"]))
    done = "; ".join(case["done"]) or "nothing yet"
    system = engine.PLAN_SYSTEM.split("\n\nFill in every field")[0]
    prompt = (f"Goal: {task.goal.describe()}\n"
              f"The user asked: {case['goal_text']!r}\n"
              f"Already done: {done}\n\n"
              f"{observed(case)}\n\nWhat is the single next action?")
    try:
        reply = local_brain.chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": prompt}],
            schema=LOOSE_SCHEMA, temperature=0.1, num_predict=200)
    except Exception:
        return engine.Step(verb="wait", why="the planner did not answer")
    return engine.Step(verb=str(reply.get("verb", "wait")),
                       target=str(reply.get("target", ""))[:80],
                       value=str(reply.get("value", ""))[:200],
                       expect=str(reply.get("expect", ""))[:80],
                       why=str(reply.get("why", ""))[:160])


VARIANTS = {"baseline": baseline_planner, "strict": strict_planner,
            "loose": loose_planner}


def observed(case):
    """The state text the planner is shown. Fixed, so a case is repeatable."""
    state = case["state"]
    return (f"Application: {state['app']}\n"
            f"Window: {state['title']}\n"
            f"On screen:\n{state['text']}\n"
            f"Controls: {'; '.join(state['controls'])}")


def run_case(case, engine, variant, repeat=1):
    """One case, one or more times. Returns a list of decision records."""
    from skills import screen_context
    out = []
    for run in range(repeat):
        # The state is supplied, never read from the live screen. A benchmark
        # that peeks at the real display measures the display.
        # PLANNER BENCHMARK WITH STUBBED PERCEPTION.
        #
        # This line is why the planner's 74.6% advancement is an upper bound
        # rather than an estimate: the planner is handed a screen description
        # that is correct by construction and never misread. Real perception
        # is measured separately by `run_perception_eval.py`, which found
        # accessibility at 72% top-1 and fusion at 56% on a 25-case corpus.
        # An end-to-end number needs both together and does not exist yet.
        original = engine._screen_summary
        engine._screen_summary = lambda w, limit=900, _c=case: observed(_c)
        started = time.perf_counter()
        try:
            step = VARIANTS[variant](case, engine)
        except Exception as exc:
            step = engine.Step(verb="wait", why=f"planner raised {type(exc).__name__}")
        finally:
            engine._screen_summary = original
        latency = time.perf_counter() - started

        verdict, mode, note = classify(step, case)
        out.append({
            "goal_id": case["id"], "run": run, "category": case["category"],
            "difficulty": case["difficulty"], "variant": variant,
            "goal": case["goal_text"],
            "decision": {"verb": step.verb, "target": step.target,
                         "value": step.value, "expect": step.expect,
                         "why": step.why},
            "classification": verdict, "failure_mode": mode, "note": note,
            "first_step": not case["done"], "latency": latency,
        })
    return out


def percent(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def report(records, corpus_size, seconds, variant):
    total = len(records)
    kinds = Counter(r["classification"] for r in records)
    modes = Counter(r["failure_mode"] for r in records if r["failure_mode"])
    latencies = sorted(r["latency"] for r in records)

    first = [r for r in records if r["first_step"]]
    later = [r for r in records if not r["first_step"]]

    def rate(rows, kind=ADVANCES):
        return percent(sum(1 for r in rows if r["classification"] == kind), len(rows))

    print(f"\n{BOLD}PLANNER BASELINE — {variant}{RESET}")
    print(f"{DIM}{corpus_size} goals, {total} decisions, {seconds:.0f}s{RESET}\n")

    print(f"  {'metric':34} {'value':>10}")
    print(f"  {'-'*34} {'-'*10}")
    rows = [
        ("Goals evaluated", f"{corpus_size}"),
        ("Decisions", f"{total}"),
        ("First-step decisions", f"{len(first)}"),
        ("Later-step decisions", f"{len(later)}"),
        ("", ""),
        ("Advancement rate", f"{rate(records):.1f}%"),
        ("  first step", f"{rate(first):.1f}%"),
        ("  later steps", f"{rate(later):.1f}%"),
        ("Neutral", f"{percent(kinds[NEUTRAL], total):.1f}%"),
        ("Regression", f"{percent(kinds[REGRESSES], total):.1f}%"),
        ("Invalid", f"{percent(kinds[INVALID], total):.1f}%"),
        ("Unsafe", f"{percent(kinds[UNSAFE], total):.1f}%"),
        ("Ambiguous", f"{percent(kinds[AMBIGUOUS], total):.1f}%"),
        ("Unexecutable", f"{percent(kinds[UNEXECUTABLE], total):.1f}%"),
        ("", ""),
        ("Planner p50", f"{statistics.median(latencies):.2f}s" if latencies else "-"),
        ("Planner p95", f"{latencies[int(len(latencies)*0.95)-1]:.2f}s"
         if len(latencies) > 1 else "-"),
    ]
    for name, value in rows:
        print(f"  {name:34} {value:>10}")

    print(f"\n  {BOLD}by category{RESET}")
    print(f"  {'category':18} {'n':>4} {'advances':>10} {'regress':>9} {'invalid':>9}")
    by = defaultdict(list)
    for record in records:
        by[record["category"]].append(record)
    for name in sorted(by):
        rows_ = by[name]
        print(f"  {name:18} {len(rows_):>4} {rate(rows_):>9.0f}% "
              f"{rate(rows_, REGRESSES):>8.0f}% {rate(rows_, INVALID):>8.0f}%")

    complete = [r for r in records if r["goal_id"].startswith(("complete-", "state-"))
                and r["note"].startswith("recognised")]
    already = [r for r in records if r["note"].startswith("recognised")
               or r["failure_mode"] == IGNORED_GOAL]
    if already:
        right = sum(1 for r in already if r["classification"] == ADVANCES)
        print(f"\n  {BOLD}already-satisfied goals{RESET}  "
              f"{right}/{len(already)} recognised without acting")

    if modes:
        print(f"\n  {BOLD}failure modes{RESET}")
        print(f"  {'mode':26} {'count':>6} {'share':>7}")
        bad = sum(modes.values())
        for mode, count in modes.most_common():
            print(f"  {mode:26} {count:>6} {percent(count, bad):>6.0f}%")

    worst = [r for r in records
             if r["classification"] in (REGRESSES, INVALID, UNSAFE, UNEXECUTABLE)]
    if worst:
        print(f"\n  {BOLD}representative bad decisions{RESET}")
        seen = set()
        for record in worst:
            if record["failure_mode"] in seen:
                continue
            seen.add(record["failure_mode"])
            decision = record["decision"]
            print(f"\n  {RED}{record['goal_id']}{RESET}  {record['goal']}")
            print(f"    chose      {decision['verb']} {decision['target'] or decision['value']!r}")
            print(f"    because    {decision['why'][:76]}")
            print(f"    verdict    {record['classification']} — {record['note']}")

    good = [r for r in records if r["classification"] == ADVANCES]
    if good:
        print(f"\n  {BOLD}representative good decisions{RESET}")
        for record in good[:3]:
            decision = record["decision"]
            print(f"  {GREEN}{record['goal_id']}{RESET}  {record['goal']}"
                  f"  →  {decision['verb']} {decision['target'] or decision['value']!r}")

    advance = rate(records) / 100.0
    invalid = percent(kinds[INVALID], total) / 100.0
    regress = percent(kinds[REGRESSES], total) / 100.0
    verdicts = [
        ("advancement", advance >= THRESHOLDS["minimum_advancement_rate"],
         f"{advance:.0%} against a configured floor of "
         f"{THRESHOLDS['minimum_advancement_rate']:.0%}"),
        ("invalid", invalid <= THRESHOLDS["maximum_invalid_rate"],
         f"{invalid:.0%} against a configured ceiling of "
         f"{THRESHOLDS['maximum_invalid_rate']:.0%}"),
        ("regression", regress <= THRESHOLDS["maximum_regression_rate"],
         f"{regress:.0%} against a configured ceiling of "
         f"{THRESHOLDS['maximum_regression_rate']:.0%}"),
    ]
    print(f"\n  {BOLD}against configured thresholds{RESET}"
          f"  {DIM}(judgement, not established fact){RESET}")
    for name, ok, detail in verdicts:
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {name:14} {detail}")
    return all(ok for _, ok, _ in verdicts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--category", default="")
    parser.add_argument("--difficulty", default="")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--variant", default="baseline", choices=sorted(VARIANTS))
    parser.add_argument("--json", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    with open(os.path.join(HERE, "planner_cases.json")) as handle:
        cases = json.load(handle)["cases"]
    if args.category:
        cases = [c for c in cases if c["category"] == args.category]
    if args.difficulty:
        cases = [c for c in cases if c["difficulty"] == args.difficulty]
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        print("no cases matched")
        return 1

    from skills import do_engine, local_brain
    print(f"{DIM}planner model: {local_brain.FAST_MODEL}{RESET}")

    started = time.time()
    records = []
    for index, case in enumerate(cases, 1):
        print(f"\r  {index}/{len(cases)}  {case['id']:26}", end="", flush=True)
        records.extend(run_case(case, do_engine, args.variant, args.repeat))
    print("\r" + " " * 48 + "\r", end="")

    ok = report(records, len(cases), time.time() - started, args.variant)
    if args.json:
        with open(args.json, "w") as handle:
            json.dump({"variant": args.variant,
                       "model": local_brain.FAST_MODEL,
                       "cases": len(cases), "repeat": args.repeat,
                       "thresholds": THRESHOLDS,
                       "records": records}, handle, indent=2)
        print(f"\n  written to {args.json}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
