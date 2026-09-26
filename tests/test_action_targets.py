#!/usr/bin/env python3
"""
B.3 — an action is shown at the control that does it, not at the thing it is about.

The failure this exists for, observed on a real Notes window: asked "show me
how to open a new note", Diablo put its marker on the note's title, "Diablo
Notes", instead of the pencil that makes a new one. The teaching system drew
exactly what it was given. What it was given was wrong, and wrong in a way
that has a name: the SUBJECT of the request (a note) was found, and the
OPERATION (create) was thrown away before anything looked at the screen.

Traced on the committed code, the request went to the tutor, whose anchor
matches words against OCR text and nothing else; and had it gone to point_at,
a failed perception read fell back to the same OCR text match. Both roads end
at whatever text mentions a note.

So the contract here is:

    an action request carries its operation all the way to perception
    the answer to it must be a control that offers that operation
    a passive piece of text never satisfies it, however well it matches
    finding nothing is said out loud; it never becomes the title instead

and, just as important, a question ABOUT the note still finds the note.

Nothing here touches a real application. Accessibility, OCR and vision are
stand-ins that return what a real window would.
"""

import os
import sys
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import (agent_core, agent_tools, ax, computer_use, conversation,  # noqa: E402
                    delegation_tool, grounding, local_brain, perception, pointing,
                    route, screen_context, target, teaching, tutor)

WINDOW = {"id": 41, "app": "Notes", "title": "Diablo Notes", "x": 100.0, "y": 80.0,
          "width": 900.0, "height": 600.0, "sensitive": False}


def el(role, title="", description="", x=0.0, y=0.0, w=30.0, h=24.0,
       enabled=True, help=""):
    return ax.Element(role=role, title=title, description=description, help=help,
                      enabled=enabled, x=x, y=y, width=w, height=h, app="Notes")


# ─── Screens, as the instruments would report them ──────────────────────────
#
# Each has the control that performs the action and at least one thing that
# is about the same subject and performs nothing.

NOTES = {
    "ax": [el("AXRow", "Diablo Notes", x=120, y=160, w=220, h=40),
           el("AXButton", description="New Note", x=610, y=92, w=30, h=26),
           el("AXButton", description="Share", x=660, y=92, w=30, h=26),
           el("AXSearchField", description="Search", x=760, y=92, w=200, h=26),
           el("AXButton", description="Delete", x=700, y=92, w=30, h=26)],
    "ocr": [("Diablo Notes", 540, 150), ("Notes", 180, 110),
            ("Meeting notes for Thursday", 560, 200)],
}
SAVE = {"ax": [el("AXButton", "Q3 Report", x=300, y=90, w=160, h=24),
               el("AXButton", "Save", x=820, y=90, w=60, h=24)],
        "ocr": [("Q3 Report", 380, 102), ("Save", 850, 102),
                ("Report saved at 10:42", 500, 640)]}
EXPORT = {"ax": [el("AXButton", "Export", x=840, y=90, w=70, h=24)],
          "ocr": [("Revenue Chart", 400, 140), ("Export", 875, 102),
                  ("Exported charts appear here", 400, 600)]}
OPEN = {"ax": [el("AXButton", "Open…", x=130, y=90, w=70, h=24),
               el("AXButton", "Essay.docx", x=400, y=90, w=150, h=24)],
        "ocr": [("Essay.docx", 475, 102), ("Open…", 165, 102),
                ("Opened 3 days ago", 470, 130)]}
NEW_TAB = {"ax": [el("AXRadioButton", "Diablo — Home", x=120, y=60, w=200, h=28),
                  el("AXButton", description="New Tab", x=330, y=60, w=28, h=28)],
           "ocr": [("Diablo — Home", 220, 74), ("New tab page", 500, 300)]}
SEARCH = {"ax": [el("AXSearchField", description="Search", x=700, y=90, w=200, h=26)],
          "ocr": [("Search results for 'cats'", 400, 160), ("Search", 760, 103)]}
CLOSE = {"ax": [el("AXButton", description="close button", x=108, y=86, w=14, h=14),
                el("AXButton", "Closed tickets", x=300, y=160, w=140, h=24)],
         "ocr": [("Closed tickets", 370, 172)]}
DELETE = {"ax": [el("AXButton", description="Delete", x=700, y=92, w=30, h=26),
                 el("AXRow", "Deleted Items", x=120, y=300, w=200, h=30)],
          "ocr": [("Deleted Items", 220, 315)]}
ADD = {"ax": [el("AXButton", "Add Item", x=780, y=560, w=90, h=26),
              el("AXRow", "Shopping list item", x=120, y=200, w=300, h=30)],
       "ocr": [("Shopping list item", 270, 215), ("Add Item", 825, 573)]}
PLAY = {"ax": [el("AXButton", description="Play", x=480, y=520, w=40, h=40),
               el("AXButton", "Playlist", x=120, y=90, w=90, h=24)],
        "ocr": [("Playlist", 165, 102), ("Played 12 times", 500, 600)]}


class Screen:
    """Stand-ins for the three instruments, for one screen."""

    def __init__(self, screen, vision=None):
        self.screen, self.vision, self.asked_ocr, self.asked_vision = screen, vision, [], []

    def __enter__(self):
        self.saved = (ax.elements_for_window, pointing.locate, perception._from_vision,
                      screen_context.frontmost_window)
        ax.elements_for_window = lambda window, actionable_only=True: [
            e for e in self.screen.get("ax", [])
            if not actionable_only or e.role in ax.ACTIONABLE_ROLES]

        def ocr(intent, app="", limit=5, observation=None):
            self.asked_ocr.append(intent)
            if observation is not None:
                observation["ok"] = True
            hits = []
            for text, x, y in self.screen.get("ocr", []):
                score = pointing._score(intent, text)
                if score >= perception.FLOOR:
                    hits.append({"text": text, "x": float(x), "y": float(y),
                                 "score": score})
            return sorted(hits, key=lambda h: -h["score"])[:limit]
        pointing.locate = ocr

        def vision(intent, window, frame=None):
            self.asked_vision.append(intent)
            return list(self.vision or [])
        perception._from_vision = vision
        screen_context.frontmost_window = lambda: WINDOW
        return self

    def __exit__(self, *exc):
        (ax.elements_for_window, pointing.locate, perception._from_vision,
         screen_context.frontmost_window) = self.saved


def best(screen, intent, operation="", vision=None):
    with Screen(screen, vision):
        reading = perception.read(intent, WINDOW, operation=operation)
    return reading.best, reading


def run() -> int:
    r = Results()

    # ── 1. Operation and subject ────────────────────────────────────────────
    r.head("1  what is being done, apart from what it is done to")
    with guard(r, "action requests carry their operation"):
        for text, operation, semantic in (
                ("Show me how to open a new note.", target.CREATE, "new note"),
                ("Create a new note.", target.CREATE, "new note"),
                ("Open a new note.", target.CREATE, "new note"),
                ("How do I open a new tab?", target.CREATE, "new tab"),
                ("How do I add an item?", target.CREATE, "add item"),
                ("Show me how to save this.", target.SAVE, "save"),
                ("Show me how to save the report.", target.SAVE, "save report"),
                ("Show me how to export this chart.", target.EXPORT, "export chart"),
                ("Show me how to open a document.", target.OPEN, "open document"),
                ("How do I close this window?", target.CLOSE, "close window"),
                ("How do I search my notes?", target.SEARCH, "search notes"),
                ("Show me how to delete this note.", target.DELETE, "delete note"),
                ("Show me how to play the video.", target.PLAY, "play video"),
                ("Where is the export button?", target.EXPORT, "export button")):
            t = target.read_question(text)
            r.check(f"{text!r} -> {operation}, a control, named {semantic!r}",
                    t.operation == operation and t.kind == target.CONTROL
                    and t.semantic == semantic, repr(t))

    with guard(r, "questions about a thing carry none"):
        for text in ("Where is the note?", "Where is Diablo Notes?",
                     "Which note is open?", "What does the note say?",
                     "Explain this note.", "Explain Diablo Notes.",
                     "Show me the current note.", "Where is the note title?"):
            t = target.read_question(text)
            r.check(f"{text!r} -> no operation", t.operation == "", repr(t))
        r.check("'Where is the note title?' is text",
                target.read_question("Where is the note title?").kind == target.TEXT)
        r.check("'Explain Diablo Notes' is not a control",
                target.read_question("Explain Diablo Notes.").kind != target.CONTROL)

    with guard(r, "vague requests stay vague"):
        for text in ("Show me how to work with this note.", "Do the thing with the note.",
                     "Where is it?"):
            t = target.read_question(text)
            r.check(f"{text!r} -> no operation invented", t.operation == "", repr(t))
            r.check("  and not a request to be guided",
                    (target.asked_to_show(text) or types.SimpleNamespace(
                        intent="")).intent != target.GUIDE)

    with guard(r, "the request to be guided"):
        shown = target.asked_to_show("Show me how to open a new note.")
        r.check("'show me how to open a new note' is GUIDE, certain",
                shown is not None and shown.intent == target.GUIDE and shown.certain,
                repr(shown))
        r.check("  at a control, for creating", shown is not None
                and shown.target.kind == target.CONTROL
                and shown.target.operation == target.CREATE)
        r.check("a procedure with no known operation is still the tutor's",
                target.asked_to_show("show me how to change my wallpaper") is None)

    # ── 2. Does this control offer that? ────────────────────────────────────
    r.head("2  affordance: the control's own words offer the operation")
    with guard(r, "target.affords"):
        for operation, text, expected in (
                (target.CREATE, "New Note", True), (target.CREATE, "Diablo Notes", False),
                (target.CREATE, "+", True), (target.CREATE, "Add Item", True),
                (target.CREATE, "Compose", True), (target.SAVE, "Save", True),
                (target.SAVE, "Report saved at 10:42", False),
                (target.SAVE, "Q3 Report", False), (target.EXPORT, "Export…", True),
                (target.EXPORT, "Exported charts appear here", False),
                (target.OPEN, "Open…", True), (target.OPEN, "Opened 3 days ago", False),
                (target.CLOSE, "close button", True), (target.CLOSE, "Closed tickets", False),
                (target.SEARCH, "Search", True), (target.DELETE, "Deleted Items", False),
                (target.PLAY, "Play", True), (target.PLAY, "Playlist", False)):
            r.check(f"{operation} / {text!r} -> {expected}",
                    target.affords(operation, text) is expected)

    # ── 3. Perception, with and without an operation ────────────────────────
    r.head("3  the control that does it, not the thing it is about")
    with guard(r, "the canonical Notes screen"):
        chosen, reading = best(NOTES, "new note", target.CREATE)
        r.check("'show me how to open a new note' -> the New Note control",
                chosen is not None and chosen.label == "New Note"
                and chosen.role == "AXButton", repr(chosen))
        r.check("  and its geometry is the control's",
                chosen is not None and (chosen.x, chosen.y) == (610, 92))
        r.check("  not the title, in any candidate",
                all("Diablo Notes" not in c.label for c in reading.candidates),
                str([c.label for c in reading.candidates]))
        chosen, _ = best(NOTES, "Diablo Notes")
        r.check("'where is Diablo Notes' -> Diablo Notes, still",
                chosen is not None and chosen.label == "Diablo Notes", repr(chosen))

    for name, screen, intent, operation, want, not_want in (
            ("save", SAVE, "save report", target.SAVE, "Save", "Q3 Report"),
            ("export", EXPORT, "export chart", target.EXPORT, "Export", "Revenue Chart"),
            ("open", OPEN, "open document", target.OPEN, "Open…", "Essay.docx"),
            ("new tab", NEW_TAB, "new tab", target.CREATE, "New Tab", "Diablo — Home"),
            ("search", SEARCH, "search", target.SEARCH, "Search", "Search results"),
            ("close", CLOSE, "close window", target.CLOSE, "close button", "Closed"),
            ("delete", DELETE, "delete item", target.DELETE, "Delete", "Deleted"),
            ("add item", ADD, "add item", target.CREATE, "Add Item", "Shopping"),
            ("play", PLAY, "play video", target.PLAY, "Play", "Playlist")):
        with guard(r, name):
            chosen, reading = best(screen, intent, operation)
            r.check(f"{name}: the control ({want!r})",
                    chosen is not None and chosen.label == want, repr(chosen))
            r.check(f"  not {not_want!r}", all(not c.label.startswith(not_want)
                                              for c in reading.candidates),
                    str([c.label for c in reading.candidates]))

    with guard(r, "not every noun becomes a control"):
        chosen, _ = best(SAVE, "Q3 Report")
        r.check("'where is the Q3 Report' -> the report title", chosen is not None
                and chosen.label == "Q3 Report", repr(chosen))

    # ── 4. Evidence rules ──────────────────────────────────────────────────
    r.head("4  what may answer an action")
    with guard(r, "OCR alone"):
        title_only = {"ax": [], "ocr": [("Diablo Notes", 540, 150)]}
        chosen, reading = best(title_only, "new note", target.CREATE)
        r.check("a title read off the glass cannot answer 'open a new note'",
                chosen is None and not reading.candidates, repr(chosen))
        labelled = {"ax": [], "ocr": [("Diablo Notes", 540, 150), ("New Note", 620, 104)]}
        chosen, _ = best(labelled, "new note", target.CREATE)
        r.check("a short label that says the operation can, where nothing "
                "better exists", chosen is not None and chosen.label == "New Note"
                and chosen.sources == ["ocr"], repr(chosen))
        sentence = {"ax": [], "ocr": [("Click New Note in the toolbar to start a new note",
                                        500, 300)]}
        chosen, _ = best(sentence, "new note", target.CREATE)
        r.check("a sentence ABOUT the operation is instructions, not a control",
                chosen is None, repr(chosen))
        both = {"ax": [el("AXButton", description="New Note", x=610, y=92)],
                "ocr": [("New Note", 900, 500)]}
        chosen, _ = best(both, "new note", target.CREATE)
        r.check("accessibility's control wins over matching text elsewhere",
                chosen is not None and "accessibility" in chosen.sources
                and (chosen.x, chosen.y) == (610, 92), repr(chosen))

    with guard(r, "disabled controls"):
        two = {"ax": [el("AXButton", "Save", x=100, y=90, enabled=False),
                      el("AXButton", "Save", x=820, y=90)], "ocr": []}
        chosen, _ = best(two, "save", target.SAVE)
        r.check("the enabled one, when there is an enabled one",
                chosen is not None and chosen.enabled is not False and chosen.x == 820,
                repr(chosen))
        one = {"ax": [el("AXButton", "Save", x=100, y=90, enabled=False)], "ocr": []}
        chosen, reading = best(one, "save", target.SAVE)
        r.check("never a disabled one", chosen is None, repr(chosen))
        r.check("  and why is kept, so it can be said",
                any("disabled" in n for n in reading.observation.get("refused", [])),
                str(reading.observation))

    with guard(r, "icon only"):
        pencil = perception.Candidate(
            label="pencil", role="AXButton", x=610, y=92, width=30, height=26,
            confidence=0.8, sources=["vision"], source_confidence={"vision": 0.8},
            evidence=["it looks like pencil — compose a new note"], enabled=True)
        icon_only = {"ax": [el("AXButton", x=610, y=92)], "ocr": [("Diablo Notes", 540, 150)]}
        screen = Screen(icon_only, vision=[pencil])
        with screen:
            reading = perception.read("new note", WINDOW, operation=target.CREATE)
        r.check("an unlabelled button vision identified for the request is taken",
                reading.best is not None and reading.best.label == "pencil"
                and (reading.best.x, reading.best.y) == (610, 92), repr(reading.best))
        r.check("  vision was asked with the request, not with the title",
                bool(screen.asked_vision) and "new note" in screen.asked_vision[0])

    with guard(r, "no candidate at all"):
        chosen, reading = best({"ax": [el("AXRow", "Diablo Notes", x=120, y=160)],
                                "ocr": [("Diablo Notes", 540, 150)]},
                               "new note", target.CREATE)
        r.check("nothing that does it -> nothing", chosen is None and not reading.candidates)

    # ── 5. point_at never falls back to the text ────────────────────────────
    r.head("5  point_at: an action that cannot be found is said, not guessed")
    drawn = []
    saved_show = pointing.show
    pointing.show = lambda x, y, label="", seconds=3.0, marker=True, width=0.0, \
        height=0.0: drawn.append((label, x, y)) or ""
    try:
        with guard(r, "found"):
            with Screen(NOTES):
                pointing.point_at("new note", operation=target.CREATE)
            r.check("the New Note control is marked",
                    bool(drawn) and drawn[-1][0] == "New Note", str(drawn))
            r.check("  at its centre", bool(drawn)
                    and (drawn[-1][1], drawn[-1][2]) == (625.0, 105.0), str(drawn))
        with guard(r, "not found"):
            del drawn[:]
            with Screen({"ax": [], "ocr": [("Diablo Notes", 540, 150)]}):
                answer = pointing.point_at("new note", operation=target.CREATE)
            r.check("with only the title on screen nothing is drawn", drawn == [],
                    str(drawn))
            r.check("  and it says there is no control for it",
                    "new note" in answer.lower() and ("can't" in answer.lower()
                                                      or "cannot" in answer.lower()),
                    answer)
        with guard(r, "inferred from the description"):
            del drawn[:]
            with Screen(NOTES):
                pointing.point_at("the new note button")
            r.check("a planner that names the control gets the same rule, inferred",
                    bool(drawn) and drawn[-1][0] == "New Note", str(drawn))
        with guard(r, "disabled"):
            del drawn[:]
            with Screen({"ax": [el("AXButton", "Save", x=100, y=90, enabled=False)],
                         "ocr": []}):
                answer = pointing.point_at("save", operation=target.SAVE)
            r.check("a disabled control is named as disabled, not marked",
                    drawn == [] and "disabled" in answer.lower(), answer)
        with guard(r, "reference"):
            del drawn[:]
            with Screen(NOTES):
                pointing.point_at("Diablo Notes")
            r.check("and a reference with no operation is unchanged",
                    bool(drawn) and "Diablo Notes" in drawn[-1][0], str(drawn))
    finally:
        pointing.show = saved_show

    with guard(r, "grounding carries the operation for a path's steps"):
        seen = []
        saved_read = perception.read
        perception.read = lambda intent, window=None, operation="": (
            seen.append((intent, operation)) or perception.Reading())
        try:
            grounding.ground("the New Note button", WINDOW, stated_kind="control")
        finally:
            perception.read = saved_read
        r.check("a step naming a control that creates is looked for as one",
                bool(seen) and seen[0][1] == target.CREATE, str(seen))

    # ── 6. The conversational path, into GUI-X ──────────────────────────────
    r.head("6  a real turn: router -> show -> point_at -> the existing presentation")

    class Session:
        def __init__(self):
            self.turns, self.tasks = [], []

        def is_followup(self, generation=None):
            return False

        def commit_turn(self, generation, text, summary):
            self.turns.append((text, summary))

        def record_task(self, *a, **k):
            self.tasks.append(a)
            return {"findings": [], "sources": []}

        def set_focus(self, *a, **k):
            pass

        def reset(self, *a, **k):
            pass

    calls = {"point_at": [], "tutor": [], "run": []}
    saved = (pointing.point_at, tutor.walk, agent_core.run, agent_core.understand,
             local_brain.available, conversation.session,
             screen_context.frontmost_window)

    def fake_point_at(description, target="", seconds=3.0, reading_out=None, kind="",
                      steps=None, speak=None, operation=""):
        calls["point_at"].append({"description": description, "kind": kind,
                                  "operation": operation, "steps": steps})
        return "Top right, marked — 'New Note'."
    try:
        with guard(r, "the canonical turn"):
            pointing.point_at = fake_point_at
            tutor.walk = lambda q, s, **k: calls["tutor"].append(q) or "walked"
            agent_core.run = lambda task, s, **k: calls["run"].append(task) or "ran"
            agent_core.understand = lambda t, in_conversation=False: {"action": "act",
                                                                       "task": t}
            local_brain.available = lambda: True
            conversation.session = Session()
            screen_context.frontmost_window = lambda: WINDOW
            spoken = []
            outcome = delegation_tool.handle("Show me how to open a new note.",
                                             lambda t, **k: spoken.append(t),
                                             ask=lambda q: "", confirm=lambda q: False)
            call = calls["point_at"][0] if calls["point_at"] else {}
            r.check("shown, not tutored", outcome == "shown" and not calls["tutor"]
                    and not calls["run"], f"{outcome} {calls}")
            r.check("point_at gets the control, the subject and the operation",
                    call.get("kind") == target.CONTROL
                    and call.get("operation") == target.CREATE
                    and call.get("description") == "new note", str(call))
            r.check("  one control, so no path", not call.get("steps"))
            decision = route.classify("Show me how to open a new note.")
            r.check("the router decides it from the phrasing", decision.route == "act"
                    and decision.show is not None
                    and decision.show.intent == target.GUIDE)
            calls["tutor"].clear()
            delegation_tool.handle("show me how to change my wallpaper",
                                   lambda t, **k: None, ask=lambda q: "",
                                   confirm=lambda q: False)
            r.check("a procedure with no known operation still goes to the tutor",
                    calls["tutor"] == ["show me how to change my wallpaper"],
                    str(calls["tutor"]))
    finally:
        (pointing.point_at, tutor.walk, agent_core.run, agent_core.understand,
         local_brain.available, conversation.session,
         screen_context.frontmost_window) = saved

    with guard(r, "the reference request, through the real turn"):
        shown = target.asked_to_show("Where is Diablo Notes?")
        r.check("'Where is Diablo Notes?' is a LOCATE the screen settles",
                shown is not None and shown.intent == target.LOCATE
                and not shown.certain and shown.target.operation == "", repr(shown))
        from skills import showing
        saved_text = showing._screen_text
        saved_front = screen_context.frontmost_window
        screen_context.frontmost_window = lambda: WINDOW
        try:
            showing._screen_text = lambda window: "Notes\nDiablo Notes\nMeeting notes"
            r.check("  taken when the name is on screen",
                    showing.prepare("Where is Diablo Notes?", shown) is not None)
            showing._screen_text = lambda window: "Inbox\nDrafts"
            r.check("  declined when it is not",
                    showing.prepare("Where is Diablo Notes?", shown) is None)
        finally:
            showing._screen_text, screen_context.frontmost_window = saved_text, saved_front
        drawn = []
        saved_show = pointing.show
        pointing.show = lambda x, y, label="", seconds=3.0, marker=True, width=0.0, \
            height=0.0: drawn.append((label, x, y)) or ""
        try:
            with Screen(NOTES):
                showing.present(showing.Prepared("Where is Diablo Notes?", shown, WINDOW),
                                lambda *a, **k: None, [])
        finally:
            pointing.show = saved_show
        r.check("  and it rings the title, not a control that acts on notes",
                bool(drawn) and drawn[-1][0] == "Diablo Notes", str(drawn))

    with guard(r, "the presentation it gets is GUI-X's own"):
        r.check("a single control is a POINT",
                teaching.choose(target.CONTROL, (30.0, 26.0)).mode == teaching.POINT)

    with guard(r, "a multi-step action is a PATH of controls"):
        class Channel:
            def __init__(self):
                self.events = []

            def begin(self, total):
                self.events.append(("begin", total))

            def step(self, place, index, total, hold=False):
                self.events.append(("step", place.label, place.mode))
                done = threading.Event()
                done.shown = True
                done.set()
                return done

            def await_action(self, *a):
                return True

            def finish(self, outcome):
                self.events.append(("finish", outcome))

            def cancelled(self):
                return False

            def wait(self, seconds):
                pass

        channel = Channel()
        saved = (teaching.CHANNEL, screen_context.frontmost_window)
        teaching.CHANNEL = channel
        screen_context.frontmost_window = lambda: WINDOW
        try:
            with Screen(NOTES):
                pointing.point_at(
                    "how to create a note and find it again",
                    steps=[{"what": "the New Note button", "say": "Start here.",
                            "kind": "control"},
                           {"what": "the Search field", "say": "Then find it here.",
                            "kind": "control"}], speak=lambda t: None)
        finally:
            teaching.CHANNEL, screen_context.frontmost_window = saved
        steps = [e for e in channel.events if e[0] == "step"]
        r.check("both controls, in order, and not the title",
                [s[1] for s in steps] == ["New Note", "Search"], str(channel.events))
        r.check("  each as a point", bool(steps)
                and all(s[2] == teaching.POINT for s in steps))

    # ── 7. Safety ───────────────────────────────────────────────────────────
    r.head("7  showing is not doing")
    with guard(r, "nothing here can act"):
        from skills import showing
        r.check("computer use is off", computer_use.ENABLED is False)
        r.check("the tool list did not grow", len(agent_tools.TOOLS) == 48,
                str(len(agent_tools.TOOLS)))
        banned = ("CGEventPost", "CGEventCreateMouseEvent", "CGEventCreateKeyboardEvent",
                  "CGWarpMouseCursorPosition", "setMouseLocation")
        for module in (target, perception, pointing, showing):
            source = open(module.__file__).read()
            r.check(f"{module.__name__} posts no events and moves no pointer",
                    not any(b in source for b in banned))

    return r.report("action targets")


if __name__ == "__main__":
    sys.exit(run())
