#!/usr/bin/env python3
"""
Every suite, in order, with one summary at the end.

Ordered cheapest-first so a broken import fails in seconds rather than after
the agent suite has spent a minute talking to a 14B. Suites that need a
microphone, a network or a focus-stealing app are opt-in through environment
variables and are reported as skipped rather than quietly passing.

    python3 tests/run_all.py              everything that is safe to run
    python3 tests/run_all.py --fast       skip the suites that use the model
"""

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

SUITES = [
    ("test_architecture.py", False, "the invariants: who owns what, and what delegates"),
    ("test_routing_live.py",  True,  "does routing generalise beyond the phrases it knows"),
    ("test_diablo.py",       False, "tool contracts, clarify loop, fast path"),
    ("test_mcp.py",          False, "MCP wire protocol against an embedded server"),
    ("test_filesystem.py",   False, "every file tool, and the sandbox boundary"),
    ("test_safety.py",       False, "shell denylist, confirmations, execute() fuzzing"),
    ("test_mic_wake.py",     False, "endpointer and wake listener under load"),
    ("test_manager.py",      False, "state machine, events, lifecycle, degradation"),
    ("test_app.py",          False, "the app API, its assets, and what is on screen"),
    ("test_pointing.py",     False, "finding things on screen, and marking them"),
    ("test_screen.py",       False, "displays, coordinates, cursor, change, permission"),
    ("test_roster.py",       True,  "delegation: who gets what, and several at once"),
    ("test_scenarios.py",    True,  "what a person says, and what actually happens"),
    ("test_walkthrough.py",  False, "the marker on screen, and waiting for a click"),
    ("test_vision_maths.py", False, "reading a question off the screen, drawing the working"),
    ("test_working.py",      False, "asking an agent how it got there"),
    ("test_memory.py",       True,  "what it remembers, and what it admits it does not"),
    ("test_rail.py",         False, "the sidebar: Diablo, then the work"),
    ("test_brain_errors.py", False, "what it says when the model refuses"),
    ("test_tidying.py",      False, "tidying a folder without reading it"),
    ("test_system.py",       False, "system tools, offline gating, screen sight"),
    ("test_voice.py",        False, "Voicebox, cache, and every fallback"),
    ("test_brain.py",        True,  "the model, conversation, anti-fabrication"),
    ("test_agent.py",        True,  "the agent loop against real files"),
    ("test_resilience.py",   True,  "what happens when dependencies vanish"),
    ("live_check.py",        True,  "offline agent, end to end"),
    ("live_screen_check.py", True,  "screen understanding, end to end"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true",
                        help="skip suites that call the model")
    args = parser.parse_args()

    python = os.path.join(os.path.expanduser("~/.diablo"), "venv", "bin", "python3")
    if not os.path.exists(python):
        python = sys.executable

    results, started = [], time.time()
    for name, needs_model, description in SUITES:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            results.append((name, "missing", 0.0))
            continue
        if args.fast and needs_model:
            print(f"{YELLOW}-{RESET} {name:24} {DIM}{description} (skipped: --fast){RESET}")
            results.append((name, "skipped", 0.0))
            continue

        print(f"\n{'=' * 72}\n  {name}  {DIM}{description}{RESET}\n{'=' * 72}")
        clock = time.time()
        proc = subprocess.run([python, "-u", path], cwd=HERE)
        elapsed = time.time() - clock
        results.append((name, "passed" if proc.returncode == 0 else "FAILED", elapsed))

    print(f"\n{'=' * 72}\n  Summary\n{'=' * 72}")
    failed = 0
    for name, status, elapsed in results:
        if status == "passed":
            mark, colour = "PASS", GREEN
        elif status in ("skipped", "missing"):
            mark, colour = "SKIP", YELLOW
        else:
            mark, colour = "FAIL", RED
            failed += 1
        timing = f"{elapsed:6.1f}s" if elapsed else "      "
        print(f"  {colour}{mark}{RESET} {name:24} {timing}")

    total = time.time() - started
    print(f"\n  {len(results)} suites in {total:.0f}s")
    print(f"  {RED}{failed} suite(s) failed{RESET}\n" if failed
          else f"  {GREEN}everything passed{RESET}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
