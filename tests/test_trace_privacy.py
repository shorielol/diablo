#!/usr/bin/env python3
"""
The trace records decisions, not what was on the screen.

`trace.jsonl` is a log on disk. It used to keep the first 120 characters of
every tool result — the screen for read_screen, every window title for
list_windows, the file for read_file — plus the text of the control Diablo
pointed at, the evidence quoting it, the window's title and the opening of
every answer. Measured on this machine before the fix: fifteen read_screen
events, each carrying 120 characters of whatever was on screen.

Each path is driven here with a marker string standing in for screen content,
and the trace file is then searched for the marker. No model, no capture.
"""

import ast
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

BASE = tempfile.mkdtemp(prefix="diablo-trace-privacy-")
TRACE = os.path.join(BASE, "trace.jsonl")
os.environ["DIABLO_TRACE"] = TRACE

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from skills import agent_core, agent_tools, local_brain, pointing, trace  # noqa: E402

SECRET = "QX7-PRIVATE-SCREEN-TEXT"

# Keyword names that carried content into the trace before this fix. None of
# them may appear in a trace call at all, whatever the value looks like: the
# first version of this check only caught `text[:120]`, and an unsliced
# `label=best.label` in agent_core walked straight past it.
CONTENT_FIELDS = ("detail", "summary", "answer", "claim", "result", "content",
                  "label", "evidence", "window", "title", "what", "dropped", "quote")


def written() -> str:
    try:
        return open(TRACE, encoding="utf-8").read()
    except OSError:
        return ""


def rows():
    return [json.loads(line) for line in written().splitlines() if line.strip()]


def run():
    r = Results()
    trace.PATH = TRACE

    r.head("An agent's tool results")
    with guard(r, "read_screen through the agent loop"):
        script = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "read_screen", "arguments": {}}}]},
            {"role": "assistant", "content": f"You are looking at {SECRET}."},
        ]
        saved = (local_brain.chat, agent_core.local_brain.chat, agent_core.plan,
                 agent_tools.execute)
        local_brain.chat = agent_core.local_brain.chat = (
            lambda messages, tools=None, **k: script.pop(0) if script
            else {"role": "assistant", "content": "Done, Sir."})
        agent_core.plan = lambda task: []
        agent_tools.execute = lambda name, args: f"Window: Mail\n{SECRET}\nRe: salary review"
        printed = io.StringIO()
        try:
            trace.new_turn("test")
            # stdout too: in the app it is diablo.log, and the same 160
            # characters of every result used to be printed there.
            with contextlib.redirect_stdout(printed):
                answer = agent_core.run("what is on my screen", speak=lambda *a, **k: None,
                                        ask=None, confirm=None)
        finally:
            (local_brain.chat, agent_core.local_brain.chat, agent_core.plan,
             agent_tools.execute) = saved
        tools = [e for e in rows() if e.get("stage") == "tool" and e.get("tool") == "read_screen"]
        r.check("the tool call is still recorded", len(tools) == 1, f"{len(tools)} events")
        r.check("with its size", bool(tools) and tools[0].get("chars", 0) > len(SECRET),
                str(tools[0].get("chars") if tools else None))
        r.check("the screen's text is not in the trace", SECRET not in written(),
                f"answer was {str(answer)[:40]!r}")
        r.check("nor the window title it named", "salary review" not in written())
        r.check("and none of it was printed to the log",
                SECRET not in printed.getvalue()
                and "salary review" not in printed.getvalue(),
                f"{len(printed.getvalue())} chars printed")

    with guard(r, "a tool call built from what was read"):
        # The next call's arguments come from the screen too: a search for
        # what was read, a file written from it. Every call's arguments used
        # to be printed, so the screen reached the log one step later.
        script = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "read_screen", "arguments": {}}}]},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "web_search",
                                          "arguments": {"query": f"what is {SECRET}"}}}]},
            {"role": "assistant", "content": "Done, Sir."},
        ]
        saved = (local_brain.chat, agent_core.local_brain.chat, agent_core.plan,
                 agent_tools.execute)
        local_brain.chat = agent_core.local_brain.chat = (
            lambda messages, tools=None, **k: script.pop(0) if script
            else {"role": "assistant", "content": "Done, Sir."})
        agent_core.plan = lambda task: []
        agent_tools.execute = (lambda name, args: f"Window: Mail\n{SECRET}"
                               if name == "read_screen" else "Error: offline")
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                agent_core.run("look this up from my screen", speak=lambda *a, **k: None,
                               ask=None, confirm=None)
        finally:
            (local_brain.chat, agent_core.local_brain.chat, agent_core.plan,
             agent_tools.execute) = saved
        r.check("the search is still printed, by name", "web_search" in printed.getvalue())
        r.check("but not what it searched for", SECRET not in printed.getvalue(),
                f"{sum(SECRET in l for l in printed.getvalue().splitlines())} lines carry it")

    r.head("What Diablo pointed at")
    with guard(r, "the perceive record"):
        class Candidate:
            label, role, confidence = f"{SECRET} button", "AXButton", 0.93
            sources, source_confidence, corroborated = ["ocr"], {"ocr": 0.93}, False
            evidence, enabled = [f"OCR reads “{SECRET}”"], True
            x, y, width, height = 10.0, 20.0, 30.0, 40.0

        class Reading:
            best, ambiguous, margin, required = Candidate(), False, 0.4, 0.45
            candidates, unreadable, verdict = [Candidate()], [], "confident"
            observation = {"ocr": {"frame": {"fingerprint": [1, 2, 3]}}}

        trace.new_turn("test")
        pointing._record("the thing", Reading(),
                         {"id": 9, "app": "Mail", "title": f"Inbox — {SECRET}"})
        perceived = [e for e in rows() if e.get("stage") == "perceive"]
        r.check("a perceive event is still written", len(perceived) == 1)
        r.check("its measurements survive",
                bool(perceived) and perceived[-1].get("confidence") == 0.93
                and perceived[-1].get("width") == 30.0)
        r.check("the control's text, its evidence and the title do not",
                SECRET not in written())

    r.head("Every other place that writes the trace")
    with guard(r, "no trace call is handed a slice of content"):
        # A static floor under the two behavioural checks above, covering the
        # trace calls that cannot be driven without a model or a window.
        offenders = []
        for name in sorted(os.listdir(os.path.join(REPO, "diablo"))):
            if not name.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(REPO, "diablo", name)).read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and getattr(node.func, "attr", "") == "event"
                        and getattr(node.func.value, "id", "") == "trace"):
                    continue
                for keyword in node.keywords:
                    if keyword.arg in CONTENT_FIELDS:
                        offenders.append(f"{name}:{node.lineno} {keyword.arg}=")
        r.check("no result, answer, summary, label or evidence is written",
                not offenders, "; ".join(offenders[:4]))

    # In the app, stdout is ~/.diablo/diablo.log. Every reply used to be
    # printed there in full, and a reply can be read off the screen.
    r.head("What the app's log keeps of a spoken line")
    bridge = open(os.path.join(REPO, "voice_bridge.py")).read()
    tree = ast.parse(bridge)
    with guard(r, "no print interpolates a spoken line"):
        # What was said, heard, asked or handed to a tool. A name anywhere
        # inside the braces counts, so json.dumps(args)[:110] is caught too.
        spoken = {"text", "piece", "question", "answer", "command", "args"}
        offenders = []
        for path in ("voice_bridge.py", os.path.join("diablo", "agent_core.py")):
            source_tree = ast.parse(open(os.path.join(REPO, path)).read())
            for node in ast.walk(source_tree):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print"):
                    continue
                for value in ast.walk(node):
                    if not isinstance(value, ast.FormattedValue):
                        continue
                    names = {getattr(n, "id", "") for n in ast.walk(value.value)}
                    # len(...) and type(...).__name__ say how much or what
                    # kind, not what.
                    calls = {getattr(n.func, "id", "") for n in ast.walk(value.value)
                             if isinstance(n, ast.Call)}
                    if names & spoken and not calls & {"len", "type"}:
                        offenders.append(f"{os.path.basename(path)}:{node.lineno}")
        r.check("spoken lines and tool arguments reach stdout only as sizes",
                not offenders, ", ".join(offenders))
    with guard(r, "_echo"):
        source = next(ast.get_source_segment(bridge, node) for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_echo")
        scope = {"sys": sys}
        exec(source, scope)
        line = "You are currently looking at the Mail window, Sir."

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        for stream, label in ((io.StringIO(), "a file"), (Terminal(), "a terminal")):
            with contextlib.redirect_stdout(stream):
                scope["_echo"]("Diablo", line)
            shown = stream.getvalue()
            if label == "a file":
                r.check("a file gets the length, not the words",
                        line not in shown and f"<{len(line)} chars>" in shown, shown.strip())
            else:
                r.check("a terminal still shows the line", line in shown, shown.strip()[:50])

    return r.report("trace privacy")


if __name__ == "__main__":
    try:
        sys.exit(run())
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
