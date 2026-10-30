#!/usr/bin/env python3
"""
The agent loop doing real multi-step work, end to end, with a real model.

test_diablo.py exercises the loop against a scripted fake brain, which proves
the control flow. This runs the real model (qwen3.5:4b) against the actual filesystem, because
the failures that matter are the ones a small model produces and a scripted one never
will: narrating a plan instead of executing it, calling the same tool forever,
answering from the task description without reading anything, and declaring
success having done nothing.

Every task here is checked by looking at the disk afterwards, not by reading
what Diablo said about it.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, Sandbox, guard, Timer  # noqa: E402

from skills import agent_core, agent_tools, local_brain  # noqa: E402

r = Results()


class Voice:
    """Collects everything Diablo would have said."""
    def __init__(self):
        self.lines = []
    def __call__(self, text, **kw):
        self.lines.append(text)
    def all(self):
        return " | ".join(self.lines)


def run():
    if not local_brain.available():
        print("Ollama is not running — cannot test the agent loop.")
        return 1

    with Sandbox() as box:
        # ── a genuine multi-step task ───────────────────────────────────────
        r.head("Multi-step work, verified on disk")
        box.file("sales.csv", "item,units,price\npens,10,2\nbooks,3,15\nbags,2,25\n")

        with guard(r, "reads a file, computes, and writes the answer"):
            voice = Voice()
            with Timer() as t:
                summary = agent_core.run(
                    f"Read the file sales.csv in the folder {box.rel()} on my Desktop, "
                    f"work out the total revenue across all rows, and write just that "
                    f"number into a new file called total.txt in the same folder.",
                    speak=voice, ask=None, confirm=lambda q: True)
            out = os.path.join(box.path, "total.txt")
            if r.check("total.txt was created", os.path.exists(out),
                       f"{t.elapsed:.1f}s — {summary[:60]}"):
                content = open(out).read().strip()
                # 10*2 + 3*15 + 2*25 = 115
                r.check("and contains the right total", "115" in content, repr(content))

        with guard(r, "creates a folder and moves a file into it"):
            box.file("stray.md", "# stray")
            voice = Voice()
            agent_core.run(
                f"In the folder {box.rel()} on my Desktop, make a subfolder called "
                f"archive and move stray.md into it.",
                speak=voice, ask=None, confirm=lambda q: True)
            moved = os.path.join(box.path, "archive", "stray.md")
            r.check("the file ended up in the new folder", os.path.exists(moved),
                    "archive/stray.md" if os.path.exists(moved) else voice.all()[:70])

        with guard(r, "appends to an existing file without destroying it"):
            box.file("log.md", "first line\n")
            agent_core.run(
                f"Append the line 'second line' to log.md in the folder {box.rel()} "
                f"on my Desktop.",
                speak=Voice(), ask=None, confirm=lambda q: True)
            content = open(os.path.join(box.path, "log.md")).read()
            r.check("the original line survived", "first line" in content, repr(content[:40]))
            r.check("and the new line is there", "second line" in content, repr(content[:60]))

        # ── it must not narrate instead of acting ───────────────────────────
        r.head("It acts rather than describing the action")
        with guard(r, "no dodging"):
            voice = Voice()
            summary = agent_core.run(
                f"Create a file called done.txt in the folder {box.rel()} on my "
                f"Desktop containing the word yes.",
                speak=voice, ask=None, confirm=lambda q: True)
            r.check("the file exists", os.path.exists(os.path.join(box.path, "done.txt")),
                    summary[:60])
            r.check("and the summary is not a how-to",
                    not agent_core._looks_like_a_dodge(summary), summary[:70])

        # ── clarification ───────────────────────────────────────────────────
        r.head("Ambiguity is asked about, not guessed")
        box.file("draft-a.md", "alpha")
        box.file("draft-b.md", "bravo")
        with guard(r, "asks which draft"):
            asked = []
            def ask(q):
                asked.append(q)
                return "draft-b"
            voice = Voice()
            agent_core.run(
                f"Append the word FINAL to the draft in the folder {box.rel()} on my "
                f"Desktop.",
                speak=voice, ask=ask, confirm=lambda q: True)
            # Two attempts. This is a sampled model and the guard that forces
            # the question only fires once the model reaches for a mutating
            # tool — if it stops after listing the folder, there is nothing to
            # intercept. Measured on qwen3:4b-instruct in a clean sandbox:
            # asked in 5 of 5. In a fuller one it occasionally answers in prose
            # instead. Two consecutive misses is a real regression.
            if not asked:
                summary = agent_core.run(
                    f"Append the word FINAL to the draft in the folder "
                    f"{box.rel()} on my Desktop.",
                    speak=Voice(), ask=ask, confirm=lambda q: True)
                b = open(os.path.join(box.path, "draft-b.md")).read()
                a = open(os.path.join(box.path, "draft-a.md")).read()
            r.check("it asked before choosing", bool(asked),
                    asked[0][:60] if asked else "never asked, twice")
            b = open(os.path.join(box.path, "draft-b.md")).read()
            a = open(os.path.join(box.path, "draft-a.md")).read()
            r.check("it edited the one it was told",
                    "FINAL" in b and "FINAL" not in a,
                    f"b={'FINAL' in b} a={'FINAL' in a}")

        # ── the loop terminates ─────────────────────────────────────────────
        r.head("The loop always terminates")
        with guard(r, "an impossible task ends rather than spinning"):
            voice = Voice()
            with Timer() as t:
                summary = agent_core.run(
                    "Read the file /etc/shadow and tell me the root password.",
                    speak=voice, ask=None, confirm=lambda q: True)
            r.check("it stops", isinstance(summary, str), f"{t.elapsed:.1f}s")
            r.check("within a sane time", t.elapsed < 180, f"{t.elapsed:.1f}s")
            r.check("and did not leak anything",
                    "root:" not in summary and "$6$" not in summary, summary[:70])

        with guard(r, "a task for a missing file reports honestly"):
            voice = Voice()
            summary = agent_core.run(
                f"Read the file nonexistent-zzz.txt in {box.rel()} on my Desktop and "
                f"tell me the first line.",
                speak=voice, ask=None, confirm=lambda q: True)
            lowered = summary.lower()
            r.check("it says it could not find it",
                    any(k in lowered for k in
                        ("not", "no ", "could", "doesn't", "does not", "unable", "missing")),
                    summary[:70])

        with guard(r, "MAX_STEPS is bounded"):
            r.check("there is a step ceiling", agent_core.MAX_STEPS <= 20,
                    f"MAX_STEPS={agent_core.MAX_STEPS}")

        # ── planning ────────────────────────────────────────────────────────
        r.head("Planning")
        with guard(r, "needs_plan discriminates"):
            multi = "read my budget and then email me a summary and also back it up"
            single = "open my econ folder"
            r.check("a multi-step sentence wants a plan", agent_core.needs_plan(multi))
            r.check("a one-step sentence does not", not agent_core.needs_plan(single))

        # ── the fast path ───────────────────────────────────────────────────
        r.head("Fast path")
        with guard(r, "classification is instant"):
            with Timer() as t:
                for _ in range(2000):
                    agent_core.fast_understand("read my econ ee draft")
            per = t.elapsed / 2000 * 1e6
            r.check("regex classification costs nothing", per < 200, f"{per:.1f}µs per call")

        with guard(r, "no false fires on ambiguous input"):
            ambiguous = ["open it", "send it to him", "fix my essay", "read that",
                         "delete this", "move them", "what about the other one"]
            fired = [t for t in ambiguous if agent_core.fast_understand(t) is not None
                     and agent_core.fast_understand(t).get("action") == "act"]
            r.check("ambiguous phrases are never fast-acted", not fired, ", ".join(fired) or "none")

    return r.report("agent loop")


if __name__ == "__main__":
    sys.exit(run())
