#!/usr/bin/env python3
"""
Build 2.5 — does the structured result reach a real consumer?

The consumer is `conversation.session.record_task`, which already existed for
this purpose. Its own docstring says evidence is "extracted once, at the point
where the tool results are still structured", and `evidence_for()` reads it
back when someone asks "where did you get that?". It had no branch for
perception, and the inline path never passed it a `working` list at all.

The important test here is the production-path one. Calling `perception.read`
and asserting the result has fields proves nothing about whether the running
assistant ever sees them, so the handoff test drives `agent_core.run` with a
stubbed model and a real fixture window, and asserts on what came out the far
end.
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "evals"))
sys.path.insert(0, os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))

from harness import Results, guard  # noqa: E402

from skills import (agent_core, agent_tools, conversation, delegation_tool,  # noqa: E402
                    perception, pointing, route, trace)


def a_reading(label="Save", confidence=0.93, sources=("accessibility",),
              ambiguous=False, unreadable=False):
    """A Reading of the shape perception really produces."""
    best = perception.Candidate(
        label=label, role="AXButton", x=443.0, y=521.0, width=92.0, height=32.0,
        confidence=confidence, sources=list(sources),
        evidence=["the application calls it Button “Save”"],
        source_confidence={s: confidence for s in sources},
        enabled=True, app="Fixture")
    note = {"ocr": {"ok": not unreadable,
                    "problem": "solid black" if unreadable else "",
                    "frame": {"window_id": 4242, "app": "Fixture",
                              "title": "A Window", "fingerprint": [1, 2, 3, 4],
                              "display_scale": 2.0, "captured_at": time.time()}}}
    return perception.Reading(candidates=[] if unreadable else [best],
                              ambiguous=ambiguous, margin=0.42, observation=note)


def main() -> int:
    r = Results()

    # ── the production path, end to end ─────────────────────────────────────
    r.head("The real production path")
    with guard(r, "a tool's structured result reaches the task record"):
        # The whole chain, with only the model and the screen replaced:
        #   agent_core.run -> agent_tools.execute("point_at")
        #     -> pointing.point_at(reading_out=...) -> CONTEXT["observations"]
        #     -> working step -> record_task -> _absorb_observation
        reading = a_reading()
        real_point_at = pointing.point_at

        # The real signature, including `kind` (Phase B) and `steps`/`speak`
        # (GUI-X). Without them the tool call raised TypeError inside the
        # stand-in, and everything downstream of the handoff failed with it.
        def fake_point_at(description, target="", seconds=3.0, reading_out=None,
                          kind="", steps=None, speak=None):
            if reading_out is not None:
                reading_out.append(reading)
            return "Top right, marked — 'Save'."

        brain = sys.modules["skills.local_brain"]
        real_chat = brain.chat
        calls = {"n": 0}

        def scripted(messages, tools=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"content": "", "tool_calls": [
                    {"function": {"name": "point_at",
                                  "arguments": {"description": "the Save button"}}}]}
            return {"content": "Marked Save.", "tool_calls": []}

        pointing.point_at = fake_point_at
        brain.chat = scripted
        spoken, working = [], []
        try:
            said = agent_core.run("point at the Save button", spoken.append,
                                  working=working)
        finally:
            pointing.point_at = real_point_at
            brain.chat = real_chat

        r.check("the run completed", isinstance(said, str) and said,
                repr(said)[:50])
        r.check("the tool call was recorded as a step",
                any(w.get("tool") == "point_at" for w in working),
                f"{len(working)} step(s)")
        step = next((w for w in working if w.get("tool") == "point_at"), {})
        r.check("the step carries the structured observation",
                step.get("observation") is not None,
                "a Reading, not a rendering of one")
        r.check("  and it is the Reading object itself, not a copy",
                step.get("observation") is reading)
        r.check("the prose result is still there too",
                "marked" in (step.get("result") or "").lower(),
                (step.get("result") or "")[:40])

        # ...and on into the consumer.
        session = conversation.Session()
        record = session.record_task("diablo", "point at the Save button",
                                     working, said)
        seen = (record.get("observations") or [])
        r.check("the consumer received it", len(seen) == 1, f"{len(seen)}")
        if seen:
            o = seen[0]
            for field, expected, label in (
                    ("label", "Save", "candidate identity"),
                    ("role", "AXButton", "role"),
                    ("verdict", "resolved", "verdict"),
                    ("confidence", 0.93, "confidence"),
                    ("app", "Fixture", "app context"),
                    ("ambiguous", False, "ambiguity"),
            ):
                r.check(f"  {label} survives", o.get(field) == expected,
                        f"{field}={o.get(field)!r}")
            r.check("  source provenance survives", o.get("sources") == ["accessibility"],
                    str(o.get("sources")))
            r.check("  per-source confidence survives",
                    o.get("source_confidence") == {"accessibility": 0.93},
                    str(o.get("source_confidence")))
            r.check("  evidence survives",
                    any("calls it" in e for e in (o.get("evidence") or [])),
                    str((o.get("evidence") or [])[:1]))
            r.check("  bounds survive",
                    o.get("bounds") == {"x": 443.0, "y": 521.0,
                                        "width": 92.0, "height": 32.0},
                    str(o.get("bounds")))
            r.check("  frame identity survives",
                    (o.get("frame") or {}).get("window_id") == 4242
                    and (o.get("frame") or {}).get("fingerprint"),
                    str((o.get("frame") or {}).get("window_id")))
            r.check("  and the display scale with it",
                    (o.get("frame") or {}).get("display_scale") == 2.0)

    with guard(r, "the inline path collects a record at all"):
        import inspect
        body = inspect.getsource(delegation_tool._run_inline)
        r.check("_run_inline passes a working list",
                "working=working" in body)
        r.check("  and records the task", "record_task" in body)
        # Whitespace removed first: the call was wrapped across two lines and
        # a literal match on one line reported a formatting change as a
        # behaviour change.
        r.check("  against Diablo, not an agent",
                'record_task("diablo"' in "".join(body.split()))

    # ── staleness ───────────────────────────────────────────────────────────
    r.head("Stale perception cannot pass as current")
    with guard(r, "a reading is stored with its moment"):
        session = conversation.Session()
        session.record_task("diablo", "point at Save",
                            [{"tool": "point_at", "args": "{}",
                              "result": "Marked.", "observation": a_reading()}],
                            "Marked Save.")
        o = session.tasks["diablo"]["observations"][0]
        r.check("the observation is timestamped", o.get("observed_at"),
                "observed_at present")
        r.check("  and carries the frame it came from",
                (o.get("frame") or {}).get("fingerprint"))

        # Age it, then read the evidence back the way a follow-up does.
        session.tasks["diablo"]["observations"][0]["observed_at"] = time.time() - 300
        text = session.evidence_for("diablo")
        r.check("the rendered evidence states its age",
                "300s ago" in text or "299s ago" in text,
                [l for l in text.splitlines() if "ago" in l][:1])
        r.check("  and refuses to present it as current",
                "not necessarily now" in text,
                "the consumer is told to look again")

    with guard(r, "an unreadable screen is recorded as unreadable"):
        session = conversation.Session()
        session.record_task("diablo", "point at Save",
                            [{"tool": "point_at", "args": "{}", "result": "",
                              "observation": a_reading(unreadable=True)}], "")
        o = session.tasks["diablo"]["observations"][0]
        r.check("no candidate is invented", not o.get("label"), str(o.get("label")))
        r.check("the verdict says the screen could not be read",
                o.get("verdict") == perception.Verdict.INVALID_OBSERVATION,
                str(o.get("verdict")))
        text = session.evidence_for("diablo")
        r.check("  and the evidence says so in words",
                "could not read it" in text,
                [l for l in text.splitlines() if "could not" in l][:1])

    # ── Decision ────────────────────────────────────────────────────────────
    r.head("The Decision reaches the consumer")
    with guard(r, "every field survives delegation"):
        decision = route.Decision(
            text="point at the Save button", goal="point at the Save button",
            kind=route.Kind.SCREEN, route="act", confidence=0.88,
            risk=route.Risk.NONE, needs_screen=True, needs_tools=True,
            needs_plan=False, reason="names 'Save'")
        import tempfile
        path = tempfile.mktemp(suffix=".jsonl")
        was, trace.PATH = trace.PATH, path
        brain = sys.modules["skills.local_brain"]
        real_chat = brain.chat
        brain.chat = lambda *a, **k: {"content": "done", "tool_calls": []}
        try:
            trace.new_turn("test")
            agent_core.run("point at the Save button", lambda t: None,
                           decision=decision)
            rows = [json.loads(line) for line in open(path)]
        finally:
            brain.chat = real_chat
            trace.PATH = was
        carried = [row for row in rows if row.get("stage") == "carry"]
        r.check("the decision arrives", len(carried) == 1, f"{len(carried)}")
        if carried:
            ev = carried[0]
            for field, expected in (("kind", "screen"), ("confidence", 0.88),
                                    ("risk", "none"), ("needs_screen", True),
                                    ("needs_tools", True)):
                r.check(f"  {field} survives", ev.get(field) == expected,
                        f"{field}={ev.get(field)!r}")
        r.check("it is not reconstructed from prose later",
                all("reason" in row or row.get("stage") != "carry" for row in rows))

    # ── the chain is observable ─────────────────────────────────────────────
    r.head("The handoff is observable")
    with guard(r, "trace records each stage"):
        import tempfile
        reading = a_reading()
        real_point_at = pointing.point_at
        pointing.point_at = (lambda d, t="", seconds=3.0, reading_out=None,
                             kind="", steps=None, speak=None:
                             (reading_out.append(reading), "Marked.")[1]
                             if reading_out is not None else "Marked.")
        brain = sys.modules["skills.local_brain"]
        real_chat = brain.chat
        calls = {"n": 0}

        def scripted(messages, tools=None, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"content": "", "tool_calls": [
                    {"function": {"name": "point_at",
                                  "arguments": {"description": "Save"}}}]}
            return {"content": "Marked Save.", "tool_calls": []}

        brain.chat = scripted
        path = tempfile.mktemp(suffix=".jsonl")
        was, trace.PATH = trace.PATH, path
        try:
            trace.new_turn("test")
            agent_core.run("point at Save", lambda t: None, working=[],
                           decision=route.Decision(text="x", kind=route.Kind.SCREEN,
                                                   confidence=0.9))
            rows = [json.loads(line) for line in open(path)]
        finally:
            pointing.point_at = real_point_at
            brain.chat = real_chat
            trace.PATH = was
        stages = [row.get("stage") for row in rows]
        r.check("the decision is traced", "carry" in stages, str(stages[:6]))
        r.check("the observation is traced", "observed" in stages, str(stages[:8]))
        seen = next((row for row in rows if row.get("stage") == "observed"), {})
        r.check("  with its verdict", seen.get("verdict") == "resolved",
                str(seen.get("verdict")))
        r.check("  its confidence", seen.get("confidence") == 0.93,
                str(seen.get("confidence")))
        r.check("  and its source", seen.get("sources") == "accessibility",
                str(seen.get("sources")))

    # ── nothing user-facing changed ─────────────────────────────────────────
    r.head("The user still hears a sentence")
    with guard(r, "the tool contract is unchanged"):
        spec = agent_tools.TOOLS["point_at"]["schema"]["function"]
        r.check("point_at still takes a description",
                "description" in spec["parameters"]["properties"])
        r.check("  and still returns text",
                isinstance(agent_tools.point_at("zzqqxx nothing"), str))
        # 48, plus find_in_menus and choose_menu, reviewed 2026-10-06.
        r.check("the tool count is 48 plus the reviewed menu pair",
                len(agent_tools.TOOLS) == 50
                and {"find_in_menus", "choose_menu"} <= set(agent_tools.TOOLS),
                str(len(agent_tools.TOOLS)))
        r.check("a caller that wants no structure still gets prose",
                isinstance(pointing.point_at("zzqqxx nothing", seconds=0.1), str))

    # ── safety ──────────────────────────────────────────────────────────────
    r.head("No new path to a computer action")
    with guard(r, "nothing changed reaches the action layer"):
        offenders = []
        for name in ("pointing.py", "perception.py", "agent_core.py",
                     "agent_tools.py", "delegation_tool.py", "conversation.py"):
            body = open(os.path.join(REPO, "diablo", name)).read()
            for needle in ("act_on", "do_engine", "_post_click", "CGEventPost"):
                if needle in body:
                    offenders.append(f"{name}:{needle}")
        r.check("no changed file mentions the action layer",
                not offenders, "; ".join(offenders))
        r.check("no click/type/press tool exists",
                not {n for n in agent_tools.TOOLS
                     if any(k in n for k in ("click", "type_", "press", "keystroke"))})
        from skills import computer_use
        r.check("computer use is still off by default",
                computer_use.ENABLED is False)
        r.check("the high-risk threshold is still unreachable",
                computer_use.NEEDED[computer_use.HIGH] > 1.0)

    return r.report("handoff")


if __name__ == "__main__":
    sys.exit(main())
