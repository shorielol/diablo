#!/usr/bin/env python3
"""
An explicit research request the intake called chat is research.

Real use, 2026-09: when the 4B called "can you research apprenticeships in
Delhi NCR, India?" chat, the reply came from its memory and nothing was looked
up: chat returned before handle()'s research checks were reached.

  - a chat verdict on an explicit research request becomes research;
  - a clarify verdict on one still asks: scoping questions on research are
    accepted behaviour (test_repeated_clarification);
  - a research request that points at something unnamed ("research that") still
    asks;
  - a fragment, a local request and a general-knowledge question are untouched;
  - nothing here is executed: the agent is a double and the model is scripted.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

BASE = tempfile.mkdtemp(prefix="diablo-research-override-")
os.environ["DIABLO_BASE_DIR"] = BASE
os.environ["DIABLO_TRACE"] = os.path.join(BASE, "trace.jsonl")

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import agent_core, conversation, local_brain, route  # noqa: E402
from skills import delegation_tool as dt  # noqa: E402

RAN, CHATTED = [], []
agent_core.run = lambda task, speak, **k: (RAN.append(task), "done")[1]
dt._chat = lambda speak, text, generation=None, **k: CHATTED.append(text)
dt._push = lambda *a, **k: None
dt.CLASSIFY = False                   # no model call to decide research either
local_brain.available = lambda timeout=3: True
conversation.session.record_task = lambda *a, **k: None


def turn(text: str, verdict: str):
    """handle() with the intake model's verdict scripted."""
    route.classify = lambda t, in_conversation=False: route.Decision(
        text=t, goal=t, route=verdict, question="Which one, Sir?" if verdict == "clarify" else "",
        reason="scripted")
    conversation.session.reset()
    RAN.clear()
    CHATTED.clear()
    asked = []
    before = len(dt.store.recent(1000))
    with contextlib.redirect_stdout(io.StringIO()):
        result = dt.handle(text, lambda *a, **k: None, ask=lambda q: (asked.append(q), "")[1],
                           confirm=lambda d: False)
    deadline = time.time() + 5
    while dt.store.active() and time.time() < deadline:
        time.sleep(0.05)
    jobs = len(dt.store.recent(1000)) - before
    return result, jobs, asked


def run() -> int:
    r = Results()

    r.head("Explicit research is research")
    for text, verdict in (("can you research apprenticeships in Delhi NCR, India?", "chat"),
                          ("Hey Diablo research into news recently about OpenAI", "chat"),
                          ("look into the history of the East India Company", "chat")):
        with guard(r, f"{verdict}: {text[:40]}"):
            result, jobs, asked = turn(text, verdict)
            r.check(f"a {verdict} verdict on {text[:38]!r} becomes a research job",
                    result == "queued" and jobs == 1 and not asked,
                    f"{result}, {jobs} job(s), asked {len(asked)}")

    r.head("Everything else is left as the model decided")
    for text, verdict, why in (
            ("research that for me", "chat", "points at something unnamed"),
            ("can you research apprenticeships in Delhi NCR, India?", "clarify",
             "a research request the model asked about"),
            ("and then she ran out the back and they all", "clarify", "a fragment"),
            ("look into my downloads folder", "clarify", "about this Mac, not research"),
            ("who wrote Dubliners", "chat", "general knowledge")):
        with guard(r, why):
            result, jobs, asked = turn(text, verdict)
            if verdict == "clarify":
                r.check(f"{why}: still asks, nothing queued", jobs == 0 and bool(asked),
                        f"{result}, {jobs} job(s), asked {len(asked)}")
            elif why == "points at something unnamed":
                r.check(f"{why}: stays a chat, nothing queued",
                        result == "chat" and jobs == 0, f"{result}, {jobs} job(s)")
            else:
                r.check(f"{why}: still a chat, nothing queued",
                        result == "chat" and jobs == 0 and bool(CHATTED), f"{result}, {jobs} job(s)")

    return r.report("research override")


if __name__ == "__main__":
    try:
        sys.exit(run())
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
