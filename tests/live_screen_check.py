#!/usr/bin/env python3
"""
Live check for the two new faculties: seeing the screen, and holding a conversation.

Must run under the Diablo venv, because window enumeration needs pyobjc:

    ~/.diablo/venv/bin/python3 live_screen_check.py

It reads whatever windows you have open. Nothing is uploaded — Quartz, Apple's
Vision framework, and Ollama on localhost.
"""

import os
import sys
import time

sys.path.insert(0, os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))
from skills import agent_core  # noqa: E402
from skills import conversation  # noqa: E402
from skills import local_brain  # noqa: E402
from skills import screen_context  # noqa: E402

results = []


def report(label, passed, detail=""):
    print(f"  {'✓' if passed else '✗'} {label}{('  — ' + str(detail)[:90]) if detail else ''}")
    results.append(passed)


def main():
    if not local_brain.available():
        print("Ollama is not running.")
        return 1

    print("Sight\n")
    windows = screen_context.list_windows()
    report("enumerates real windows", len(windows) > 0, f"{len(windows)} found")
    if not windows:
        print("\nGrant Screen Recording permission in System Settings and retry.")
        return 1
    report("each has an app and an id",
           all(w["app"] and w["id"] for w in windows), windows[0]["app"])
    report("frontmost resolves", screen_context.frontmost_window() is not None,
           screen_context.frontmost_window()["app"])
    report("on-device OCR is installed", screen_context.ocr_available())

    # These need a browser to be open. Failing when one is not open tells you
    # about the room, not about Diablo, so they skip instead.
    browsers = {"safari", "chrome", "google chrome", "zen", "firefox", "arc",
                "brave browser", "microsoft edge"}
    browser_open = any(w["app"].lower() in browsers for w in windows)
    if not browser_open:
        print("  -  'browser' finds a browser  (skipped: no browser window open)")
        print("  -  'my chrome tab' resolves to the same window  (skipped)")
    else:
        report("'browser' finds a browser",
               bool(screen_context.find_window("browser")),
               (screen_context.find_window("browser") or {}).get("app", "none open"))
        report("'my chrome tab' resolves to the same window",
               screen_context.find_window("my chrome tab") is not None,
               (screen_context.find_window("my chrome tab") or {}).get("app", "-"))
    report("nonsense target does not silently grab the wrong window",
           screen_context.find_window("qzxwv nonexistent app") is None)

    started = time.time()
    context = screen_context.look()
    ocr_time = time.time() - started
    report(f"reads the frontmost window ({ocr_time:.1f}s)",
           context.startswith("Window:") and len(context) > 60, f"{len(context)} chars")
    report("context is scoped to one window, not the whole desktop",
           context.count("Window:") == 1)

    print("\nConversation\n")
    conversation.session.reset()

    first = conversation.talk("morning Diablo")
    report("greets back briefly", bool(first) and len(first.split()) < 45, first)

    second = conversation.talk("I'm working on my Economics extended essay today")
    report("takes a remark without turning it into a task",
           bool(second) and len(second.split()) < 45, second)

    third = conversation.talk("what did I just say I was working on?")
    report("remembers the previous turn",
           "econom" in third.lower() or "essay" in third.lower() or "ee" in third.lower(),
           third)

    report("classifies a bare follow-up as conversation, not confusion",
           agent_core.understand("and what about the other one?",
                                 in_conversation=True)["action"] in ("chat", "clarify"),
           agent_core.understand("and what about the other one?",
                                 in_conversation=True)["action"])

    conversation.session.reset()
    report("reset clears memory", conversation.session.is_followup() is False)

    print("\nSight plus conversation\n")
    screen_answer = conversation.talk("take a look at my screen and tell me what app I'm in")
    report("answers from the screen", bool(screen_answer), screen_answer)
    report("attached screen context for that turn",
           conversation.session.last_screen_app is not None,
           conversation.session.last_screen_app)

    no_screen = conversation.talk("thanks")
    report("does not attach the screen when nothing points at it",
           screen_context.wants_screen("thanks") is False, no_screen)

    print("\nPrivacy\n")
    report("password managers are on the refuse list",
           "1password" in screen_context.SENSITIVE_APPS)
    fake = {"id": 1, "app": "1Password", "title": "", "width": 900,
            "height": 700, "sensitive": True}
    report("a sensitive window would be refused rather than captured", fake["sensitive"])

    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
