#!/usr/bin/env python3
"""
Multi-step tasks through the real DO engine, against records_app, with real
clicks and real typing.

    python3 evals/run_long_horizon.py --moves-the-mouse [--display ID] [--json out.json]

This is the one gauntlet that posts input events, so it is guarded:

  * it does nothing without --moves-the-mouse;
  * it starts a task only after the Mac has had no keyboard or mouse input
    for IDLE_BEFORE seconds, so it never runs while someone is using it;
  * every task runs against records_app, a fixture, and typing is refused
    unless records_app is the frontmost window, so keystrokes cannot reach
    anything else;
  * a watchdog cancels the running task through do_engine.cancel the moment
    the keyboard or mouse is used, so a person coming back takes over at once;
  * computer use is switched on inside this process only, for the duration.

The planner is the production one (qwen3.5:4b), perception is the production
one (accessibility first, GoClick behind the gate), and the action layer is
computer_use.act_on with every gate in place. The person's "yes" to each
permission question is given by this harness and counted. A task's success is
what records_app printed it saved, never what Diablo reported.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import run_grounding_gauntlet as G  # noqa: E402  (loads .env, sets the path)
from skills import computer_use, do_engine, goclick, local_brain  # noqa: E402
from skills import screen_context  # noqa: E402

IDLE_BEFORE = 240.0
TASKS = [
    {"name": "5-step", "args": [],
     "intent": "Open order 10482, set its quantity to 7, and save it.",
     "goal": ("text_on_screen", "Saved order 10482: quantity 7"),
     "expect": [{"order": "10482", "quantity": "7"}],
     "allowed": {"Order 10482", "Save", "Priority", "Back"}},
    {"name": "7-step", "args": ["--confirm"],
     "intent": ("Open order 10483, set its quantity to 12, mark it as priority, "
                "and save it."),
     "goal": ("text_on_screen", "Saved order 10483: quantity 12, priority on"),
     "expect": [{"order": "10483", "quantity": "12", "priority": "on"}],
     "allowed": {"Order 10483", "Save", "Priority", "Confirm", "Back"}},
    {"name": "10-step", "args": [],
     "intent": ("Set the quantity of order 10481 to 3 and save it, then set the "
                "quantity of order 10484 to 9 and save it."),
     # Both saves. With only the last one here, the engine reported the task
     # done when the planner had skipped saving 10481 (2026-10-06).
     "goal": ("text_on_screen", "Saved order 10484: quantity 9", "",
              ("Saved order 10481: quantity 3",)),
     "expect": [{"order": "10481", "quantity": "3"}, {"order": "10484", "quantity": "9"}],
     "allowed": {"Order 10481", "Order 10484", "Save", "Back", "Priority"}},
]


def idle_seconds() -> float:
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            return int(line.split("=")[-1].strip()) / 1e9
    return 0.0


# When this harness last posted a click or a keystroke. Posted events reset
# the idle clock as a person's do, so the check before the second task used
# to see its own clicks from the first and skip every task after it.
LAST_POST = {"at": 0.0}


def nobody_since_us() -> bool:
    """True if the only input since the last task was this harness's own."""
    if not LAST_POST["at"]:
        return False
    return idle_seconds() + 1.5 >= time.time() - LAST_POST["at"]


class Records(G.Live):
    """records_app, with everything it prints after READY collected."""

    def start(self, settle: float = 1.3) -> bool:
        ok = super().start(settle)
        self.events = []
        if ok:
            def pump():
                for line in self.child.stdout:
                    self.events.append(line.strip())
            threading.Thread(target=pump, daemon=True).start()
        return ok

    def saved(self):
        out = []
        for line in self.events:
            if line.startswith("SAVED "):
                out.append(dict(part.split("=", 1) for part in line[6:].split()))
        return out

    def pressed(self):
        return [line[8:] for line in self.events if line.startswith("PRESSED ")]


def run_task(task):
    calls = {"goclick": 0, "planner": 0, "questions": [], "typing_refused": 0}
    saved = (goclick.propose, local_brain.chat_json, computer_use.type_text)

    def counted_propose(*a, **k):
        calls["goclick"] += 1
        return saved[0](*a, **k)

    def counted_chat_json(*a, **k):
        calls["planner"] += 1
        return saved[1](*a, **k)

    def confirm(question):
        calls["questions"].append(question[:80])
        return True

    with Records("records_app", *task["args"]) as live:
        if not live.started or live.covered:
            return {"task": task["name"], "status": "INFRASTRUCTURE: fixture not visible"}

        def fixture_only_typing(*a, **k):
            front = screen_context.frontmost_window()
            if not front or G.ax._pid_for_window(front) != live.child.pid:
                calls["typing_refused"] += 1
                return computer_use._refused(computer_use.NO_WINDOW,
                                             "records_app is not in front; not typing")
            calls["last_post"] = LAST_POST["at"] = time.time()
            try:
                return saved[2](*a, **k)
            finally:
                calls["last_post"] = LAST_POST["at"] = time.time()

        goclick.propose, local_brain.chat_json = counted_propose, counted_chat_json
        computer_use.type_text = fixture_only_typing
        computer_use.ENABLED = True
        began = time.time()
        finished = threading.Event()

        def watchdog():
            # The harness's own clicks reset the idle clock too, so this
            # watches for a reset it did not cause: idle under a second while
            # no click of ours is in the last two.
            while not finished.wait(0.5):
                if idle_seconds() < 0.6 and time.time() - calls.get("last_post", 0) > 2.0:
                    calls["interrupted"] = True
                    do_engine.cancel("someone started using the Mac")
                    return
        posting = computer_use._post_click

        def noted_post(x, y):
            calls["last_post"] = LAST_POST["at"] = time.time()
            try:
                return posting(x, y)
            finally:
                LAST_POST["at"] = time.time()
        computer_use._post_click = noted_post
        guard = threading.Thread(target=watchdog, daemon=True)
        guard.start()
        try:
            result = do_engine.run(task["intent"], do_engine.Goal(*task["goal"]),
                                   window=live.window, confirm=confirm, runtime=300)
        finally:
            finished.set()
            computer_use._post_click = posting
            computer_use.ENABLED = False
            computer_use.revoke()
            goclick.propose, local_brain.chat_json, computer_use.type_text = saved
        seconds = round(time.time() - began, 1)
        time.sleep(0.5)
        saved_lines = live.saved()
        pressed = live.pressed()

    def matches(want, got):
        return all(got.get(k) == v for k, v in want.items())
    succeeded = all(any(matches(w, s) for s in saved_lines) for w in task["expect"])
    claims_done = result.state.value == "completed"
    return {
        "task": task["name"], "intent": task["intent"], "succeeded": succeeded,
        "engine_state": result.state.value,
        # The engine never says it finished a task that did not happen.
        "honest": not (claims_done and not succeeded),
        "steps": len(result.steps), "actions": len(result.actions),
        "planner_calls": calls["planner"], "goclick_calls": calls["goclick"],
        "permission_questions": len(calls["questions"]),
        "typing_refused": calls["typing_refused"], "recoveries": result.recoveries,
        "interrupted_by_user": bool(calls.get("interrupted")),
        "outcomes": [getattr(a, "outcome", "") for a in result.actions],
        "refusals": [getattr(a, "refusal", "") for a in result.actions
                     if getattr(a, "refusal", "")],
        "pressed": pressed,
        "wrong_presses": [p for p in pressed if p not in task["allowed"]],
        "saved": saved_lines, "seconds": seconds, "summary": result.summary()[:160],
        # What the engine planned and saw, step by step: fixture labels only.
        "timeline": [f"{m.kind}: {m.detail}"[:140] for m in result.timeline][:60],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--moves-the-mouse", action="store_true")
    parser.add_argument("--display", type=int, default=0)
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    if not args.moves_the_mouse:
        print("This posts real clicks and keystrokes. Pass --moves-the-mouse to run it.")
        return 2
    if args.display:
        G.FIXTURE_AT = G.placed_on(args.display)[1]
    results = []
    for task in TASKS:
        idle = idle_seconds()
        if idle < IDLE_BEFORE and not nobody_since_us():
            results.append({"task": task["name"],
                            "status": f"NOT RUN: the Mac was in use {idle:.0f}s ago"})
            print(f"{task['name']}: not run, someone is using the Mac", flush=True)
            continue
        row = run_task(task)
        results.append(row)
        print(json.dumps(row, default=str), flush=True)
    if args.json:
        json.dump(results, open(args.json, "w"), indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
