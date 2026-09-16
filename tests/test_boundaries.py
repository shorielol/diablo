#!/usr/bin/env python3
"""
Build 1.5 — does the important information survive the boundaries?

Three defects from the runtime connectivity audit, each with the same shape:
something structured reached a boundary and a flatter thing came out the other
side. These tests assert the structure arrives, not that the code was written.

  A  an unreadable frame and an empty screen used to produce the same []
  B  a Candidate used to become an English sentence and nothing else
  C  a Decision used to be dropped between handle() and agent_core.run

The last section asserts what did NOT change: no path to computer_use.act_on
and no action tool. That is the part worth failing loudly.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "evals"))
sys.path.insert(0, os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))

from harness import Results, guard  # noqa: E402

from skills import agent_core, agent_tools, perception, pointing, screen  # noqa: E402


class FakeFrame:
    """A ScreenFrame as far as pointing.locate is concerned."""

    def __init__(self, ok, problem="", path="/tmp/does-not-matter.png"):
        self.ok, self.problem, self.path = ok, problem, path
        self.window_id, self.app, self.title = 4242, "Fixture", "A Window"
        self.fingerprint = [1, 2, 3, 4, 5, 6, 7, 8]
        self.display_index, self.display_scale = 1, 2.0
        self.captured_at = time.time()
        self.released = False

    def release(self):
        self.released = True
        self.path = None


def main() -> int:
    r = Results()
    window = {"id": 4242, "app": "Fixture", "title": "A Window",
              "x": 0.0, "y": 0.0, "width": 800.0, "height": 600.0}

    # ── A. invalid observation is not "no match" ─────────────────────────────
    r.head("A rejected frame is not an empty screen")

    with guard(r, "an unreadable frame reports why"):
        real_screen = sys.modules["skills.screen"]
        frame = FakeFrame(False, "that window captured as solid black")
        original = real_screen.capture_this_window
        real_screen.capture_this_window = lambda w, _f=frame: _f
        note = {}
        try:
            hits = pointing.locate("Save", "", observation=note)
        finally:
            real_screen.capture_this_window = original

        r.check("no hits come back", hits == [], f"{len(hits)} hits")
        r.check("the observation says it failed", note.get("ok") is False,
                str(note.get("ok")))
        r.check("  and says why in a sentence",
                "black" in (note.get("problem") or ""), note.get("problem", "")[:50])
        r.check("the rejected frame is still released", frame.released,
                "released" if frame.released else "LEAKED")

    with guard(r, "a readable frame with no match is a different answer"):
        real_screen = sys.modules["skills.screen"]
        frame = FakeFrame(True)
        original_cap = real_screen.capture_this_window
        sc = sys.modules["skills.screen_context"]
        original_ocr = sc._ocr_fragments
        real_screen.capture_this_window = lambda w, _f=frame: _f
        sc._ocr_fragments = lambda path, win: []
        note = {}
        try:
            hits = pointing.locate("Nothing Like This", "", observation=note)
        finally:
            real_screen.capture_this_window = original_cap
            sc._ocr_fragments = original_ocr

        r.check("still no hits", hits == [], f"{len(hits)} hits")
        r.check("but the observation says it succeeded", note.get("ok") is True,
                str(note.get("ok")))
        r.check("the two states are distinguishable",
                note.get("ok") is True, "ok=True here, ok=False above")
        r.check("frame identity is recorded",
                (note.get("frame") or {}).get("window_id") == 4242
                and (note.get("frame") or {}).get("fingerprint"),
                str((note.get("frame") or {}).get("window_id")))

    with guard(r, "an OCR failure string is a failed reading"):
        real_screen = sys.modules["skills.screen"]
        frame = FakeFrame(True)
        sc = sys.modules["skills.screen_context"]
        original_cap, original_ocr = real_screen.capture_this_window, sc._ocr_fragments
        real_screen.capture_this_window = lambda w, _f=frame: _f
        sc._ocr_fragments = lambda path, win: "Error: Vision is unavailable"
        note = {}
        try:
            pointing.locate("Save", "", observation=note)
        finally:
            real_screen.capture_this_window = original_cap
            sc._ocr_fragments = original_ocr
        r.check("an OCR error is not silence", note.get("ok") is False,
                str(note.get("ok")))
        r.check("  and the reason survives",
                "Vision" in (note.get("problem") or ""), note.get("problem", "")[:40])

    with guard(r, "a caller that does not ask is unaffected"):
        import inspect
        sig = inspect.signature(pointing.locate)
        r.check("observation is optional",
                sig.parameters["observation"].default is None)
        r.check("the old call shape still type-checks",
                list(sig.parameters)[:3] == ["description", "target", "limit"],
                ", ".join(list(sig.parameters)))

    with guard(r, "there is still only one validity gate"):
        # Code only. The first version of this grepped the whole file and
        # matched the comment that explains the change — the assertion failed
        # on the prose describing what it was asserting.
        source = "\n".join(
            line for line in
            open(os.path.join(REPO, "diablo", "pointing.py")).read().splitlines()
            if not line.lstrip().startswith("#"))
        r.check("pointing does not implement its own blank-frame check",
                "def validity" not in source and "def _fingerprint" not in source)
        r.check("  it uses ScreenFrame instead", "capture_this_window" in source)
        r.check("  and no longer takes the raw capture path",
                "screen_context.capture_window(" not in source)

    # ── B. the Candidate survives ───────────────────────────────────────────
    r.head("A Candidate is not just a sentence")

    with guard(r, "Reading carries the observation"):
        reading = perception.Reading()
        r.check("an empty Reading has an observation field",
                isinstance(reading.observation, dict))
        r.check("unreadable is empty when nothing failed", reading.unreadable == [])
        bad = perception.Reading(observation={"ocr": {"ok": False, "problem": "black"}})
        r.check("a failed source is named", bad.unreadable == ["ocr"],
                str(bad.unreadable))
        good = perception.Reading(observation={"ocr": {"ok": True, "problem": ""}})
        r.check("a source that read the screen is not named",
                good.unreadable == [], str(good.unreadable))

    with guard(r, "the structured reading reaches the trace"):
        from skills import trace
        candidate = perception.Candidate(
            label="Save", role="AXButton", x=100.0, y=200.0,
            width=90.0, height=30.0, confidence=0.93,
            sources=["accessibility"], evidence=["accessibility calls it “Save”"],
            enabled=True, app="Fixture")
        reading = perception.Reading(
            candidates=[candidate], ambiguous=False, margin=0.42,
            observation={"ocr": {"ok": True, "problem": "",
                                 "frame": {"window_id": 4242,
                                           "fingerprint": [1, 2, 3, 4, 5, 6, 7, 8]}}})
        path = tempfile.mktemp(suffix=".jsonl")
        was, trace.PATH = trace.PATH, path
        try:
            trace.new_turn("test")
            pointing._record("the Save button", reading, window)
            rows = [json.loads(line) for line in open(path)]
        finally:
            trace.PATH = was
        seen = [row for row in rows if row.get("stage") == "perceive"]
        r.check("a perceive event is recorded", len(seen) == 1, f"{len(seen)} events")
        if seen:
            ev = seen[0]
            for field, expected, label in (
                    ("label", "Save", "candidate identity"),
                    ("confidence", 0.93, "confidence"),
                    ("sources", "accessibility", "source"),
                    ("role", "AXButton", "role"),
                    ("enabled", True, "enabled state"),
                    ("app", "Fixture", "app context"),
                    ("window_id", 4242, "window identity"),
            ):
                r.check(f"  {label} survives", ev.get(field) == expected,
                        f"{field}={ev.get(field)!r}")
            r.check("  evidence survives", "accessibility calls it" in (ev.get("evidence") or ""),
                    (ev.get("evidence") or "")[:40])
            r.check("  bounds survive",
                    (ev.get("x"), ev.get("y"), ev.get("width"), ev.get("height"))
                    == (100.0, 200.0, 90.0, 30.0),
                    f"{ev.get('x')},{ev.get('y')} {ev.get('width')}x{ev.get('height')}")
            r.check("  frame identity survives", bool(ev.get("frame_fingerprint")),
                    str(ev.get("frame_fingerprint")))
            r.check("  ambiguity survives", ev.get("ambiguous") is False
                    and ev.get("margin") == 0.42, str(ev.get("margin")))

    with guard(r, "an unreadable screen is recorded as unreadable"):
        from skills import trace
        reading = perception.Reading(
            observation={"ocr": {"ok": False, "problem": "solid black"}})
        path = tempfile.mktemp(suffix=".jsonl")
        was, trace.PATH = trace.PATH, path
        try:
            trace.new_turn("test")
            pointing._record("the Save button", reading, window)
            rows = [json.loads(line) for line in open(path)]
        finally:
            trace.PATH = was
        ev = [row for row in rows if row.get("stage") == "perceive"][0]
        r.check("the trace says which source could not see",
                ev.get("unreadable") == "ocr", str(ev.get("unreadable")))
        r.check("  and why", "black" in (ev.get("why_unreadable") or ""),
                (ev.get("why_unreadable") or "")[:40])
        r.check("  and does not claim a candidate", ev.get("found") is False)

    with guard(r, "recording never breaks pointing"):
        class Exploding:
            @property
            def best(self):
                raise RuntimeError("boom")
        pointing._record("x", Exploding(), window)
        r.check("a broken reading does not raise out of _record", True,
                "swallowed")

    with guard(r, "the user still gets a sentence"):
        source = open(os.path.join(REPO, "diablo", "pointing.py")).read()
        r.check("point_at still returns prose",
                "marked{hedge}" in source or "marked" in source)
        r.check("  and the tool contract is unchanged",
                agent_tools.TOOLS["point_at"]["schema"]["function"]["parameters"]
                ["properties"].keys() >= {"description"})

    # ── C. the Decision survives ────────────────────────────────────────────
    r.head("A Decision is not dropped at delegation")

    with guard(r, "agent_core.run accepts it"):
        import inspect
        sig = inspect.signature(agent_core.run)
        r.check("run takes a decision", "decision" in sig.parameters)
        r.check("  and it is optional",
                sig.parameters["decision"].default is None)
        from skills import delegation_tool
        inline = inspect.signature(delegation_tool._run_inline)
        r.check("_run_inline takes a decision", "decision" in inline.parameters)
        r.check("  and it is optional", inline.parameters["decision"].default is None)

    with guard(r, "every field of the Decision survives the call"):
        from skills import route, trace
        decision = route.Decision(
            text="tidy my downloads", goal="tidy the downloads folder",
            kind=route.Kind.ACTION, route="act", confidence=0.82,
            risk=route.Risk.MEDIUM, needs_screen=False, needs_tools=True,
            needs_plan=True, reason="names 'downloads'")
        path = tempfile.mktemp(suffix=".jsonl")
        was, trace.PATH = trace.PATH, path
        spoken = []
        try:
            trace.new_turn("test")
            # Stop the loop at the first model call; the carry happens before it.
            brain = sys.modules["skills.local_brain"]
            original = brain.chat
            brain.chat = lambda *a, **k: {"content": "done", "tool_calls": []}
            try:
                agent_core.run("tidy my downloads", spoken.append,
                               decision=decision)
            finally:
                brain.chat = original
            rows = [json.loads(line) for line in open(path)]
        finally:
            trace.PATH = was
        carried = [row for row in rows if row.get("stage") == "carry"]
        r.check("the decision arrives at agent_core.run", len(carried) == 1,
                f"{len(carried)} carry events")
        if carried:
            ev = carried[0]
            for field, expected in (("kind", "action"), ("confidence", 0.82),
                                    ("risk", "medium"), ("needs_screen", False),
                                    ("into", "agent_core.run"),
                                    ("needs_tools", True), ("needs_plan", True)):
                r.check(f"  {field} survives", ev.get(field) == expected,
                        f"{field}={ev.get(field)!r}")

    with guard(r, "a caller that passes nothing is unaffected"):
        from skills import trace
        path = tempfile.mktemp(suffix=".jsonl")
        was, trace.PATH = trace.PATH, path
        try:
            trace.new_turn("test")
            brain = sys.modules["skills.local_brain"]
            original = brain.chat
            brain.chat = lambda *a, **k: {"content": "done", "tool_calls": []}
            try:
                agent_core.run("say hello", lambda t: None)
            finally:
                brain.chat = original
            rows = [json.loads(line) for line in open(path)]
        finally:
            trace.PATH = was
        r.check("no carry event when there is no decision",
                not [row for row in rows if row.get("stage") == "carry"])

    # ── E. safety, unchanged ────────────────────────────────────────────────
    r.head("No new path to a computer action")

    with guard(r, "nothing added reaches the action layer"):
        touched = ["pointing.py", "perception.py", "agent_core.py",
                   "delegation_tool.py"]
        offenders = []
        for name in touched:
            body = open(os.path.join(REPO, "diablo", name)).read()
            for needle in ("act_on", "do_engine", "_post_click", "CGEventPost"):
                if needle in body:
                    offenders.append(f"{name}:{needle}")
        r.check("no file changed by this build mentions the action layer",
                not offenders, "; ".join(offenders))

    with guard(r, "the tool menu still cannot click"):
        names = set(agent_tools.TOOLS)
        forbidden = {n for n in names
                     if any(k in n for k in ("click", "type_", "press", "keystroke"))}
        r.check("no click/type/press tool exists", not forbidden, str(forbidden))
        r.check("the tool count is unchanged at 48", len(names) == 48, str(len(names)))

    with guard(r, "computer use is still off by default"):
        from skills import computer_use
        r.check("ENABLED defaults off", computer_use.ENABLED is False,
                str(computer_use.ENABLED))
        r.check("the high-risk threshold is still unreachable",
                computer_use.NEEDED[computer_use.HIGH] > 1.0,
                str(computer_use.NEEDED[computer_use.HIGH]))

    with guard(r, "do_engine still has no production consumer"):
        import subprocess as sp
        out = sp.run(["grep", "-rn", "do_engine", "--include=*.py",
                      os.path.join(REPO, "diablo")],
                     capture_output=True, text=True).stdout
        callers = [ln for ln in out.splitlines()
                   if "do_engine.py:" not in ln and "# " not in ln.split(":", 2)[-1][:3]]
        real = [ln for ln in callers if "import" in ln or "do_engine." in ln]
        r.check("no production module imports do_engine", not real,
                "; ".join(r.split(REPO + "/")[-1] for r in real[:2]))

    return r.report("boundaries")


if __name__ == "__main__":
    sys.exit(main())
