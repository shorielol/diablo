#!/usr/bin/env python3
"""
GUI-X.1 — can a normal conversation reach the teaching system?

GUI-X built four ways of showing something (a point, an outline, a teaching
cursor, a path) and proved them through a direct `point_at` call. A real turn
never got there. Measured before this build, on the running app's own code:

    "Where is the welfare loss region?"      router -> clarify
    "Point at the welfare loss region."      router -> act, then the 4B made up
                                             a window called "chrome" or "Notes"
    "Explain the subsidy."                   router -> chat; target -> CONTROL;
                                             planner -> research (a web search)
    "Show me how the subsidy causes ..."     tutor.walk, which anchors on OCR text
    "Show me the equilibrium."               planner -> ask_user ("market or
                                             chemical?")
    "What does this triangle mean?"          router -> clarify

The fix is a contract rather than more prompt: the phrasing of a request to be
shown something is read in code, the Decision carries it, and the turn goes to
the existing `point_at`. A multi-step explanation gets its steps from a
constrained generation against `point_at`'s own step schema. The model names
things; it never places them.

Nothing here draws on the real screen. Grounding is the fixture's own geometry
and the shell is the recorder the teaching suite uses.
"""

import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import (agent_core, agent_tools, conversation, delegation_tool,  # noqa: E402
                    grounding, lesson, local_brain, perception, pointing, route,
                    screen_context, target, teaching, tutor)

A = "Where is the welfare loss region?"
B = "Point at the welfare loss region."
C = "Explain the subsidy."
D = "Show me how the subsidy causes welfare loss."
E = "Show me the equilibrium."
F = "What does this triangle mean?"

# What OCR reads off the subsidy fixture: a title, axis names, curve names, the
# legend and a boxed note. Nothing is printed where the areas are drawn.
FIXTURE_TEXT = ("Subsidy and Welfare\nPrice\nQuantity\nD\nS\nS + subsidy\n"
                "Consumer surplus\nProducer surplus\nWelfare loss\nSubsidy\n"
                "Note: the subsidy is paid\nper unit, not to scale")
BLANK_TEXT = "Inbox\nDrafts\nSent\nArchive"

# The same rectangles test_teaching uses (Quartz, top-left).
PLACES = {
    "original equilibrium": (680.0, 400.0, 40.0, 40.0),
    "subsidised equilibrium": (742.0, 452.0, 40.0, 40.0),
    "subsidy wedge": (748.0, 380.0, 28.0, 92.0),
    "welfare loss": (700.0, 410.0, 64.0, 70.0),
    "consumer surplus": (510.0, 260.0, 190.0, 160.0),
    "producer surplus": (510.0, 430.0, 190.0, 150.0),
}

# The brief's own example of what a planner should hand over.
BRIEF_STEPS = {"steps": [
    {"what": "original equilibrium",
     "say": "First, this is the original equilibrium.", "kind": "POINT"},
    {"what": "new equilibrium after the subsidy",
     "say": "The subsidy changes the equilibrium to this point.", "kind": "POINT"},
    {"what": "welfare loss region",
     "say": "This triangle is the welfare loss created by the change.",
     "kind": "REGION"}]}


def region(name, source="region"):
    x, y, w, h = PLACES[name]
    return perception.Candidate(label=name, role="region", x=x, y=y, width=w,
                                height=h, confidence=0.84, sources=[source],
                                source_confidence={source: 0.84})


class Found:
    def __init__(self, candidate, kind):
        self.candidate = candidate
        self.found = candidate is not None
        self.target = target.Target(semantic="", kind=kind)
        self.note = "" if candidate is not None else "not found"


def fake_ground(question, window=None, stated_kind=""):
    """Fixture geometry: a place is found when the words asked for name it."""
    wanted = target.read_question(question, stated=stated_kind)
    asked = set(target._named(question.lower())) - {"region", "area", "point", "after"}
    if "equilibrium" in asked and asked & {"new", "subsidy", "subsidised"}:
        asked = {"subsidised", "equilibrium"}
    best, best_score = None, 0
    for name in PLACES:
        score = len(asked & set(name.split()))
        if score > best_score:
            best, best_score = name, score
    return Found(region(best) if best else None, wanted.kind)


class Channel:
    """The shell, as a list of what it was asked to do (as in test_teaching)."""

    def __init__(self):
        self.events = []

    def begin(self, total):
        self.events.append(("begin", total))

    def step(self, place, index, total, hold=False):
        self.events.append(("step", index, place.label, place.mode))
        done = threading.Event()
        done.shown = True
        done.set()
        return done

    def await_action(self, place, index, total):
        self.events.append(("await", index))
        return True

    def finish(self, outcome):
        self.events.append(("finish", outcome))

    def cancelled(self):
        return False

    def wait(self, seconds):
        self.events.append(("dwell", seconds))


class Patch:
    """Swap attributes for the length of a block, and put them all back."""

    def __init__(self):
        self.saved = []

    def __call__(self, owner, name, value):
        self.saved.append((owner, name, getattr(owner, name, None)))
        setattr(owner, name, value)

    def undo(self):
        for owner, name, value in reversed(self.saved):
            setattr(owner, name, value)
        self.saved = []


class Session:
    """conversation.session, without the disk."""

    def __init__(self):
        self.turns, self.tasks, self.focus = [], [], []

    def is_followup(self, generation=None):
        return False

    def reset(self, generation=None):
        pass

    def commit_turn(self, generation, text, summary):
        self.turns.append((text, summary))

    def record_task(self, agent, task, working, summary, task_id="", generation=None):
        self.tasks.append((agent, task, list(working), summary))
        return {"findings": [], "sources": []}

    def set_focus(self, agent, generation=None):
        self.focus.append(agent)


def run() -> int:
    r = Results()
    patch = Patch()

    # ── 1. Reading the request ──────────────────────────────────────────────
    r.head("1  is this a request to be shown something?")
    with guard(r, "the six brief requests"):
        a, b, c, d, e, f = (target.asked_to_show(t) for t in (A, B, C, D, E, F))
        r.check("A 'where is ...' is a request to see where", a is not None
                and a.intent == target.LOCATE and a.target.kind == target.REGION,
                repr(a))
        r.check("  and certain: a named area of a picture", a is not None and a.certain)
        r.check("B 'point at ...' likewise", b is not None
                and b.intent == target.LOCATE and b.target.kind == target.REGION
                and b.certain, repr(b))
        r.check("C 'explain the subsidy' is an explanation", c is not None
                and c.intent == target.EXPLAIN, repr(c))
        r.check("  of something that is not a control", c is not None
                and c.target.kind != target.CONTROL, repr(c))
        r.check("  and not certain: only the screen can say whether the subsidy "
                "is on it", c is not None and not c.certain, repr(c))
        r.check("D 'show me how X causes Y' is a trace", d is not None
                and d.intent == target.TRACE and d.certain, repr(d))
        r.check("E 'show me the equilibrium' is a request to see where", e is not None
                and e.intent == target.LOCATE and e.certain, repr(e))
        r.check("F 'what does this triangle mean' is an explanation of a shape",
                f is not None and f.intent == target.EXPLAIN
                and f.target.kind == target.REGION and f.certain, repr(f))

    with guard(r, "requests that are not, and stay where they were"):
        for text, why in (
                ("Where is it?", "names nothing: still a question back"),
                ("Where is Paris?", "a place in the world, not on the screen"),
                ("Show me my calendar", "opening an app, not pointing"),
                ("find my econ ee draft", "a file"),
                ("show me how to save this file", "a procedure: the tutor's job"),
                ("how do i export this as a pdf", "a procedure"),
                ("help me with this question", "the maths tutor's"),
                ("explain this", "names nothing: the tutor's, as before"),
                ("walk me through setting up git", "a procedure, no picture"),
                ("show me the settings", "probably open them, not ring them"),
                ("show me the map", "probably open Maps"),
                ("what's the weather like", "a question with a spoken answer")):
            r.check(f"{text!r} -> not a showing request ({why})",
                    target.asked_to_show(text) is None,
                    repr(target.asked_to_show(text)))

    with guard(r, "a few more ways of saying it"):
        for text, intent in (
                ("show me where the consumer surplus is", target.LOCATE),
                ("can you point out the demand curve", target.LOCATE),
                ("where's the Save button", target.LOCATE),
                ("highlight the shaded area", target.LOCATE),
                ("walk me through this graph", target.TRACE),
                ("explain how the tax shifts the supply curve", target.TRACE),
                ("what does the shaded area represent", target.EXPLAIN),
                ("what am I pointing at", target.EXPLAIN)):
            got = target.asked_to_show(text)
            r.check(f"{text!r} -> {intent}", got is not None and got.intent == intent,
                    repr(got))

    # ── 2. Target classification ───────────────────────────────────────────
    r.head("2  'explain the subsidy' is not a control")
    with guard(r, "target.read_question"):
        t = target.read_question(C)
        r.check("an explanation of a named thing reads as a figure-level "
                "explanation, not a button", t.kind == target.FIGURE, repr(t))
        r.check("  and keeps what was named", t.semantic == "subsidy", repr(t))
        r.check("'explain the Save button' is still a control",
                target.read_question("explain the Save button").kind == target.CONTROL)
        r.check("'where is the Save button' is still a control",
                target.read_question("where is the Save button").kind == target.CONTROL)
        r.check("'what does this graph show' is still a figure",
                target.read_question("what does this graph show").kind == target.FIGURE)
        r.check("no economics was added to decide it: 'explain the photosynthesis' "
                "reads the same way",
                target.read_question("Explain the photosynthesis.").kind == target.FIGURE)

    # ── 3. The router ──────────────────────────────────────────────────────
    r.head("3  route.classify")
    asked_model = []

    def understand(text, in_conversation=False):
        asked_model.append(text)
        if text.lower().startswith("where is it"):
            return {"action": "clarify", "question": "Where is what, Sir?"}
        return {"action": "chat", "task": text}

    try:
        patch(agent_core, "understand", understand)
        for text in (A, B, D, E, F):
            del asked_model[:]
            decision = route.classify(text)
            r.check(f"{text!r}: acted on, not clarified", decision.route == "act",
                    f"{decision.route} {decision.reason}")
            r.check("  a screen request", decision.kind == route.Kind.SCREEN
                    and decision.needs_screen, decision.kind)
            r.check("  carrying what was asked to be shown",
                    decision.show is not None and decision.show.certain)
            r.check("  without a model round trip to decide it", asked_model == [],
                    str(asked_model))
        del asked_model[:]
        decision = route.classify(C)
        r.check("C: the model is still asked, because the phrasing alone cannot "
                "say whether it is about the screen", asked_model == [C])
        r.check("  and the request to be shown travels with its answer",
                decision.show is not None and decision.show.intent == target.EXPLAIN)
        decision = route.classify("Where is it?")
        r.check("'Where is it?' is still clarified", decision.route == "clarify"
                and decision.show is None, f"{decision.route} {decision.show!r}")
        r.check("the Decision still serialises for the trace",
                isinstance(json.dumps(decision.as_dict(), default=str), str))
    finally:
        patch.undo()

    # ── 4. The step contract ───────────────────────────────────────────────
    r.head("4  steps name things; they never place them")
    with guard(r, "teaching.clean_steps"):
        cleaned = teaching.clean_steps(BRIEF_STEPS["steps"])
        r.check("the brief's example survives", [s["what"] for s in cleaned] == [
            "original equilibrium", "new equilibrium after the subsidy",
            "welfare loss region"], str(cleaned))
        r.check("presentation names are not passed off as target kinds",
                [s.get("kind", "") for s in cleaned] == ["", "", target.REGION],
                str(cleaned))
        smuggled = teaching.clean_steps([
            {"what": "welfare loss", "say": "Here.", "x": 700, "y": 410,
             "box": [1, 2, 3, 4], "kind": "region"}])
        r.check("coordinates a model adds are dropped, not used",
                smuggled and set(smuggled[0]) <= {"what", "say", "kind", "act"},
                str(smuggled))
        r.check("a step with nothing to find is dropped",
                teaching.clean_steps([{"what": "", "say": "x"}, {"say": "y"}]) == [])
        r.check("a step with nothing to say is dropped",
                teaching.clean_steps([{"what": "welfare loss", "say": " "}]) == [])
        r.check("junk is not a step", teaching.clean_steps(
            ["welfare loss", None, 3, {"what": 5, "say": 6}]) == [])
        # What the 4B wrote once it was told to name parts of the picture:
        # "region", "region", "curve". A kind of thing is not a thing to find.
        r.check("a step that names only a kind of thing is dropped",
                teaching.clean_steps([{"what": "region", "say": "a"},
                                      {"what": "the curve", "say": "b"},
                                      {"what": "an area", "say": "c"}]) == [])
        r.check("  a named shape is kept", [s["what"] for s in teaching.clean_steps(
            [{"what": "triangle", "say": "a"},
             {"what": "the demand curve", "say": "b"}])] == ["triangle",
                                                             "the demand curve"])
        many = [{"what": f"thing {i}", "say": "x"} for i in range(12)]
        r.check("capped", len(teaching.clean_steps(many)) <= pointing.MAX_STEPS)
        r.check("step_kind: target kinds pass", teaching.step_kind("Region") == "region")
        r.check("step_kind: 'cursor' is the object the cursor shows",
                teaching.step_kind("CURSOR") == target.OBJECT)
        r.check("step_kind: anything else is left to the phrasing",
                teaching.step_kind("POINT") == "" and teaching.step_kind("banana") == "")

    with guard(r, "one schema, used by the tool and by the step generation"):
        spec = agent_tools.TOOLS["point_at"]["schema"]
        r.check("point_at's step items are teaching.STEP_ITEM",
                json.dumps(teaching.STEP_ITEM, sort_keys=True) in
                json.dumps(spec, sort_keys=True))
        r.check("the step item has no coordinate fields",
                not ({"x", "y", "w", "h", "box", "bounds"}
                     & set(teaching.STEP_ITEM["properties"])))
        r.check("the tool no longer offers window names to copy",
                "'Notes'" not in json.dumps(spec) and "'chrome'" not in json.dumps(spec))

    # ── 5. showing: prepare and present ────────────────────────────────────
    r.head("5  from a request to point_at")
    from skills import showing

    def harness(screen=FIXTURE_TEXT, planned=None, grounded=True, plan_raises=False):
        """point_at, the step generator and the screen, all recorded."""
        calls = {"point_at": [], "plan": [], "explain": []}

        def point_at(description, target="", seconds=3.0, reading_out=None,
                     kind="", steps=None, speak=None):
            calls["point_at"].append({"description": description, "target": target,
                                      "kind": kind, "steps": steps, "speak": speak})
            if reading_out is not None:
                reading_out.append(perception.Reading(
                    candidates=[region("welfare loss")] if grounded else [],
                    margin=1.0 if grounded else 0.0))
            return ("I've shown you that on screen, Sir." if grounded else
                    "I couldn't find that on screen, Sir, so there is nothing "
                    "I can show you.")

        def chat_json(messages, schema=None, **kw):
            calls["plan"].append({"messages": messages, "schema": schema})
            if plan_raises:
                raise RuntimeError("model offline")
            return planned if planned is not None else BRIEF_STEPS

        def explain(question, target=""):
            calls["explain"].append(question)
            calls.setdefault("explained_window", []).append(target)
            return "The diagram shows a subsidy shifting supply down, Sir."

        patch(pointing, "point_at", point_at)
        patch(local_brain, "chat_json", chat_json)
        patch(screen_context, "explain", explain)
        patch(showing, "_screen_text", lambda window: screen)
        patch(screen_context, "frontmost_window",
              lambda: {"id": 7, "app": "Preview", "title": "Subsidy and Welfare"})
        return calls

    said = []

    def speak(text, **kw):
        said.append(text)

    try:
        calls = harness()
        answer = showing.present(showing.prepare(A, target.asked_to_show(A)), speak, [])
        call = calls["point_at"][0] if calls["point_at"] else {}
        r.check("A: point_at, for the region, by the name it was asked for",
                call.get("description") == "welfare loss region"
                and call.get("kind") == target.REGION and not call.get("steps"),
                str(call))
        r.check("  in whatever is in front: no invented window", call.get("target") == "")
        r.check("  no step generation for a single place", calls["plan"] == [])
        r.check("  the answer is point_at's own", "shown" in answer)
        patch.undo()

        calls = harness()
        showing.present(showing.prepare(E, target.asked_to_show(E)), speak, [])
        r.check("E: point_at for the equilibrium",
                calls["point_at"] and calls["point_at"][0]["description"] == "equilibrium",
                str(calls["point_at"]))
        patch.undo()

        calls = harness()
        working = []
        answer = showing.present(showing.prepare(D, target.asked_to_show(D)), speak,
                                 working)
        call = calls["point_at"][0] if calls["point_at"] else {}
        r.check("D: steps were generated", len(calls["plan"]) == 1)
        prompt = json.dumps(calls["plan"][0]["messages"]) if calls["plan"] else ""
        r.check("  from the question and what the window says",
                "welfare loss" in prompt.lower() and "S + subsidy" in prompt, prompt[:200])
        r.check("  constrained to point_at's own step schema",
                bool(calls["plan"]) and json.dumps(teaching.STEP_ITEM, sort_keys=True) in
                json.dumps(calls["plan"][0]["schema"], sort_keys=True))
        r.check("  and handed to the existing point_at as steps",
                len(call.get("steps") or []) == 3, str(call))
        r.check("  with the voice, so each sentence is said at its place",
                call.get("speak") is speak)
        r.check("  no step carries a coordinate",
                all(set(s) <= {"what", "say", "kind", "act"}
                    for s in call.get("steps") or []))
        r.check("  and the turn leaves evidence behind",
                bool(working) and working[0]["tool"] == "point_at"
                and "observation" in working[0], str(working)[:200])
        patch.undo()

        calls = harness()
        prepared = showing.prepare(C, target.asked_to_show(C))
        r.check("C, with a subsidy diagram in front: taken", prepared is not None)
        if prepared is not None:
            showing.present(prepared, speak, [])
        r.check("  and shown as steps", bool(calls["point_at"])
                and len(calls["point_at"][0]["steps"] or []) >= 2)
        patch.undo()

        calls = harness(screen=BLANK_TEXT)
        r.check("C, with nothing about a subsidy on screen: declined, so the turn "
                "is answered the ordinary way",
                showing.prepare(C, target.asked_to_show(C)) is None)
        r.check("  and nothing was generated or drawn to find that out",
                calls["plan"] == [] and calls["point_at"] == [])
        patch.undo()

        calls = harness(planned={"steps": [
            {"what": "the shaded triangle", "say": "This is the welfare loss."}]})
        showing.present(showing.prepare(F, target.asked_to_show(F)), speak, [])
        call = calls["point_at"][0] if calls["point_at"] else {}
        r.check("F: one step is fine — the shape, shown and explained",
                len(call.get("steps") or []) == 1, str(call))
        patch.undo()

        # What the 4B actually wrote for "explain the subsidy" on the fixture,
        # 5 runs in 5: three areas and then the printed word.
        calls = harness(planned={"steps": [
            {"what": "the shaded area labeled 'Consumer surplus'", "say": "a",
             "kind": "region"},
            {"what": "the shaded area labeled 'Welfare loss'", "say": "b",
             "kind": "region"},
            {"what": "the text labeled 'Subsidy'", "say": "c", "kind": "text"},
            {"what": "the Export button", "say": "d", "kind": "control"}]})
        showing.present(showing.prepare(C, target.asked_to_show(C)), speak, [])
        call = calls["point_at"][0] if calls["point_at"] else {}
        r.check("a picture is explained with parts of the picture: a printed "
                "caption or a button is not a step",
                [s.get("kind") for s in call.get("steps") or []] == ["region", "region"],
                str(call.get("steps")))
        patch.undo()

        # And for "what does this triangle mean", 5 in 5: the triangle and
        # three other things. One shape was asked about.
        calls = harness(planned={"steps": [
            {"what": "triangle labeled 'Welfare loss'", "say": "a", "kind": "region"},
            {"what": "area under the demand curve", "say": "b", "kind": "region"},
            {"what": "area above the supply curve", "say": "c", "kind": "region"}]})
        showing.present(showing.prepare(F, target.asked_to_show(F)), speak, [])
        call = calls["point_at"][0] if calls["point_at"] else {}
        r.check("a question about one shape is answered at that shape alone",
                len(call.get("steps") or []) == 1, str(call.get("steps")))
        prompt = json.dumps(calls["plan"][0]["messages"]) if calls["plan"] else ""
        r.check("  and the writer is told so", "one place" in prompt.lower())
        patch.undo()

        calls = harness(planned={"steps": []})
        answer = showing.present(showing.prepare(D, target.asked_to_show(D)), speak, [])
        r.check("no steps: the existing screen explanation, not an invented path",
                calls["point_at"] == [] and calls["explain"] == [D], str(calls))
        # Live, the fallback explained whatever was frontmost a minute later:
        # "error messages, process logs, account details" — the editor, not
        # the diagram the question was about.
        r.check("  of the window the turn was about, not whatever is in front "
                "by the time it runs",
                calls.get("explained_window") == ["Subsidy and Welfare"],
                str(calls.get("explained_window")))
        r.check("  and its answer is what is said", "subsidy" in answer.lower())
        patch.undo()

        calls = harness(plan_raises=True)
        answer = showing.present(showing.prepare(D, target.asked_to_show(D)), speak, [])
        r.check("the step generator failing is not the turn failing",
                calls["explain"] == [D] and bool(answer), answer)
        patch.undo()

        calls = harness(grounded=False)
        answer = showing.present(showing.prepare(C, target.asked_to_show(C)), speak, [])
        r.check("steps that ground to nothing: explained instead of 'nothing to show'",
                calls["explain"] == [C] and "nothing i can show" not in answer.lower(),
                answer)
        patch.undo()

        calls = harness(grounded=False)
        answer = showing.present(showing.prepare(A, target.asked_to_show(A)), speak, [])
        r.check("a single place that is not there is said to be not there, "
                "not replaced by an explanation", calls["explain"] == []
                and "couldn't find" in answer.lower(), answer)
        patch.undo()
    finally:
        patch.undo()

    # ── 6. handle(): the real turn ─────────────────────────────────────────
    r.head("6  delegation_tool.handle")

    def turn(text, screen=FIXTURE_TEXT, planned=None):
        calls = harness(screen=screen, planned=planned)
        calls.update({"tutor": [], "run": [], "talk": [], "understand": []})
        session = Session()
        patch(conversation, "session", session)
        patch(local_brain, "available", lambda: True)
        patch(tutor, "walk", lambda q, s, **k: calls["tutor"].append(q) or "walked")
        patch(agent_core, "run", lambda task, s, **k: calls["run"].append(task) or "ran")
        patch(conversation, "talk", lambda t, **k: calls["talk"].append(t) or "talked")

        def understand(t, in_conversation=False):
            calls["understand"].append(t)
            if "where is it" in t.lower():
                return {"action": "clarify", "question": "Where is what, Sir?"}
            if "save this file" in t.lower():
                return {"action": "act", "task": t}
            return {"action": "chat", "task": t}
        patch(agent_core, "understand", understand)
        spoken = []
        outcome = delegation_tool.handle(
            text, lambda t, **k: spoken.append(t),
            ask=lambda q: spoken.append(q) or "", confirm=lambda q: False)
        calls["session"], calls["spoken"], calls["outcome"] = session, spoken, outcome
        patch.undo()
        return calls

    try:
        for text in (A, B, E):
            calls = turn(text)
            r.check(f"{text!r}: shown", calls["outcome"] == "shown"
                    and len(calls["point_at"]) == 1, str(calls["outcome"]))
            r.check("  not clarified, not planned, not tutored",
                    not calls["tutor"] and not calls["run"] and not calls["talk"])
            r.check("  the reply is spoken", bool(calls["spoken"])
                    and "shown" in calls["spoken"][-1])
            r.check("  and the turn is remembered with its evidence",
                    bool(calls["session"].turns) and bool(calls["session"].tasks)
                    and bool(calls["session"].tasks[0][2]))

        calls = turn(D)
        r.check("D: shown as a path, and the tutor did not take it",
                calls["outcome"] == "shown" and not calls["tutor"]
                and bool(calls["point_at"])
                and len(calls["point_at"][0]["steps"] or []) == 3,
                f"{calls['outcome']} tutor={calls['tutor']}")

        calls = turn(F, planned={"steps": [
            {"what": "the shaded triangle", "say": "This is the welfare loss."}]})
        r.check("F: shown", calls["outcome"] == "shown" and bool(calls["point_at"]))

        calls = turn(C)
        r.check("C, diagram in front: shown", calls["outcome"] == "shown"
                and bool(calls["point_at"]))
        calls = turn(C, screen=BLANK_TEXT)
        r.check("C, nothing relevant in front: an ordinary spoken answer",
                calls["outcome"] == "chat" and calls["talk"] == [C]
                and not calls["point_at"], str(calls["outcome"]))

        calls = turn("show me how to save this file")
        r.check("a procedure still goes to the tutor",
                bool(calls["tutor"]) and not calls["point_at"], str(calls["outcome"]))
        calls = turn("Where is it?")
        r.check("'Where is it?' still asks", not calls["point_at"]
                and any("Where is what" in s for s in calls["spoken"]),
                str(calls["spoken"]))
    finally:
        patch.undo()

    # ── 7. End to end, on the fixture ──────────────────────────────────────
    r.head("7  the whole chain, with the real point_at and the teaching path")
    try:
        channel = Channel()
        drawn = []
        patch(grounding, "ground", fake_ground)
        patch(teaching, "CHANNEL", channel)
        patch(pointing, "show", lambda x, y, label="", seconds=3.0, marker=True,
              width=0.0, height=0.0: drawn.append((label, width, height)) or "")
        patch(local_brain, "chat_json", lambda m, schema=None, **k: BRIEF_STEPS)
        patch(showing, "_screen_text", lambda window: FIXTURE_TEXT)
        patch(screen_context, "frontmost_window",
              lambda: {"id": 7, "app": "Preview", "title": "Subsidy and Welfare"})
        patch(local_brain, "available", lambda: True)
        patch(conversation, "session", Session())
        patch(agent_core, "understand",
              lambda t, in_conversation=False: {"action": "chat", "task": t})

        spoken = []
        with guard(r, "D, end to end"):
            outcome = delegation_tool.handle(D, lambda t, **k: spoken.append(t),
                                             ask=lambda q: "", confirm=lambda q: False)
            steps = [e for e in channel.events if e[0] == "step"]
            r.check("D: three grounded places, in order",
                    [s[2] for s in steps] == ["original equilibrium",
                                              "subsidised equilibrium", "welfare loss"],
                    str(steps))
            r.check("  the two crossings as points, the triangle as an area",
                    [s[3] for s in steps] == [teaching.POINT, teaching.POINT,
                                              teaching.REGION], str(steps))
            r.check("  each sentence said at its place", all(
                s in spoken for s in (x["say"] for x in BRIEF_STEPS["steps"])),
                str(spoken))
            r.check("  and the lesson finished",
                    bool(channel.events) and channel.events[-1] == ("finish", lesson.DONE))

        with guard(r, "A, end to end"):
            del drawn[:]
            outcome = delegation_tool.handle(A, lambda t, **k: spoken.append(t),
                                             ask=lambda q: "", confirm=lambda q: False)
            r.check("A: the welfare loss area outlined, through the existing guidance",
                    outcome == "shown" and bool(drawn) and drawn[-1][0] == "welfare loss"
                    and drawn[-1][1] > 0 and drawn[-1][2] > 0, str(drawn))

        with guard(r, "E, end to end"):
            del drawn[:]
            outcome = delegation_tool.handle(E, lambda t, **k: spoken.append(t),
                                             ask=lambda q: "", confirm=lambda q: False)
            r.check("E: the equilibrium, a crossing, gets the point and not an outline",
                    outcome == "shown" and bool(drawn) and drawn[-1][1] == 0.0
                    and drawn[-1][2] == 0.0, str(drawn))
    finally:
        patch.undo()

    return r.report("conversation teaching")


if __name__ == "__main__":
    sys.exit(run())
