#!/usr/bin/env python3
"""Understanding the screen, made visible.

What is checked: a read produces a region with real bounds, and those bounds
become a rectangle on the right display through the one existing mapper; the
region he focuses on is one the text actually grouped into, and when the page
has no single subject he stays home rather than picking; a window that moved
between the capture and the outline is not outlined; the six ways a read can
end are told apart and said in words a person would use; content focus and an
interaction target stay visibly different; nothing here posts a click, a key
or an event; and the page's own reading rules (app/surface.js, in node).
"""

import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

from AppKit import NSApplication  # noqa: E402

NSApplication.sharedApplication()
import app_shell  # noqa: E402
from skills import motion, reading  # noqa: E402
from skills.motion import Presence  # noqa: E402

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
HERE = os.path.dirname(os.path.abspath(__file__))

# A window 900 x 700 at (200, 120) on the main display, and a paragraph
# sitting in the top-left quarter of it. Every rectangle below is measured
# from these two.
WINDOW = {"id": 42, "app": "Preview", "title": "worksheet.pdf",
          "x": 200.0, "y": 120.0, "width": 900.0, "height": 700.0}
PARA = (0.1, 0.2, 0.5, 0.4)      # u0, v0, u1, v1


def region(chars=400, bounds=PARA, headings=(), label="main area"):
    return {"label": label, "span": (bounds[0], bounds[2]), "bounds": bounds,
            "lines": ["a line of it"], "headings": list(headings), "chars": chars}


class FakeDisplay:
    """One 1512 x 982 display at the origin. No AppKit, no real screens."""

    x = y = 0.0
    width, height = 1512.0, 982.0
    right, bottom = 1512.0, 982.0


def stand_in(home=(1461.0, 51.0)):
    """A Shell with a presence and a recording guide, and nothing else."""
    shell = types.SimpleNamespace()
    shell.presence = Presence(home=home)
    shell.wake = lambda: None
    shell.shown = []
    shell.guide = types.SimpleNamespace(
        hide=lambda: None,
        prepare=lambda *a, **k: None,
        show=lambda *a, **k: shell.shown.append(a),
        close=lambda: None)
    for name in ("focus_content", "reading_failed", "no_target", "clear_target",
                 "approach", "approach_region", "_under_surface"):
        setattr(shell, name, types.MethodType(getattr(app_shell.Shell, name), shell))
    return shell


def run():
    r = Results()

    # ── The region knows where it is ────────────────────────────────────
    r.head("A read produces something with a place")
    with guard(r, "bounds from the fragments already read"):
        from skills import screen_context
        fragments = [
            {"text": "one", "left": 0.10, "right": 0.40, "top": 0.20, "height": 0.05},
            {"text": "two", "left": 0.15, "right": 0.50, "top": 0.33, "height": 0.07},
        ]
        extent = screen_context._extent(fragments)
        r.check("a group of fragments has an outside", extent is not None)
        r.check("  across, it is the widest of them", extent[0] == 0.10 and extent[2] == 0.50)
        r.check("  down, it reaches the foot of the lowest",
                extent[1] == 0.20 and abs(extent[3] - 0.40) < 1e-9,
                "a region knew its columns but not how far down it ran")
        r.check("no fragments is no box, not a box of zeroes",
                screen_context._extent([]) is None)

    with guard(r, "the same mapper everything else uses"):
        from skills import display
        rect = reading.rect_of(PARA, WINDOW, pad=0.0)
        u0, v0 = display.image_to_quartz(PARA[0], PARA[1], WINDOW)
        r.check("a normalised box lands where image_to_quartz puts it",
                rect[0] == u0 and rect[1] == v0,
                "one conversion, not a second copy of the arithmetic")
        r.check("  and is the size the box is",
                rect[2] == 0.4 * 900.0 and rect[3] == 0.2 * 700.0)
        padded = reading.rect_of(PARA, WINDOW)
        r.check("the outline is given a little air",
                padded[2] > rect[2] and padded[3] > rect[3],
                "an outline cutting through the text reads as a strikethrough")
        r.check("a backwards box is refused",
                reading.rect_of((0.6, 0.6, 0.2, 0.2), WINDOW) is None)
        r.check("no window, no rectangle", reading.rect_of(PARA, {}) is None)

    # ── Which region, and when none ─────────────────────────────────────
    r.head("The thing he is reading, or nothing")
    saved = reading.worth_showing
    reading.worth_showing = lambda rect: bool(rect)
    try:
        with guard(r, "choosing"):
            one = reading.focus_of([region()], WINDOW)
            r.check("one region is the region", one is not None)
            r.check("  and it is named from the content",
                    reading.focus_of([region(headings=("Question 4",))], WINDOW).label
                    == "Question 4")
            r.check("  keeping the window it came from",
                    one.app == "Preview" and one.window_id == 42)

            dominant = reading.focus_of(
                [region(chars=900), region(chars=100, bounds=(0.6, 0.1, 0.9, 0.3))], WINDOW)
            r.check("one region holding most of the text wins", dominant is not None)
            r.check("  and it is the big one", dominant.share > reading.DOMINANT)

            even = reading.focus_of(
                [region(chars=500), region(chars=480, bounds=(0.6, 0.1, 0.9, 0.3))], WINDOW)
            r.check("two regions of equal weight name no single subject",
                    even is None,
                    "picking one of them would be the guess the brief forbids")
            r.check("no regions at all is nothing", reading.focus_of([], WINDOW) is None)
            r.check("a region with no bounds is not usable",
                    reading.focus_of([dict(region(), bounds=None)], WINDOW) is None)
    finally:
        reading.worth_showing = saved

    with guard(r, "and whether it is worth drawing"):
        from skills import display
        kept = display.for_point
        display.for_point = lambda x, y: FakeDisplay()
        try:
            r.check("a rectangle too small to see is not shown",
                    not reading.worth_showing((300.0, 300.0, 20.0, 18.0)))
            r.check("a sensible paragraph is",
                    reading.worth_showing((300.0, 300.0, 360.0, 280.0)))
            r.check("a rectangle that is most of the display is not",
                    not reading.worth_showing((0.0, 0.0, 1400.0, 900.0)),
                    "outlining the whole screen says nothing he did not know")
            display.for_point = lambda x, y: None
            r.check("a rectangle on no display is not anywhere",
                    not reading.worth_showing((9000.0, 9000.0, 300.0, 300.0)),
                    "the visual layer represents evidence, not guesswork")
        finally:
            display.for_point = kept

    # ── Multi-monitor ───────────────────────────────────────────────────
    r.head("Wherever the window is")
    with guard(r, "the second display, including negative coordinates"):
        right = dict(WINDOW, x=2000.0, y=300.0)
        rect = reading.rect_of(PARA, right, pad=0.0)
        r.check("a window on a display to the right maps there",
                rect[0] == 2000.0 + 0.1 * 900.0)
        left = dict(WINDOW, x=-1200.0, y=-140.0)
        rect = reading.rect_of(PARA, left, pad=0.0)
        r.check("a display at negative coordinates is not clamped away",
                rect[0] == -1200.0 + 0.1 * 900.0 and rect[1] == -140.0 + 0.2 * 700.0,
                "the arrangement is the display layer's business, not this one's")
        scaled = dict(WINDOW, width=1800.0, height=1400.0)
        rect = reading.rect_of(PARA, scaled, pad=0.0)
        r.check("a Retina window needs no special case",
                rect[2] == 0.4 * 1800.0,
                "the box is normalised, so the scale factor has already cancelled")

    # ── The screen moving under him ─────────────────────────────────────
    r.head("What was read is still what is there")
    with guard(r, "a window that moved"):
        from skills import screen_context
        kept = screen_context.has_moved
        try:
            screen_context.has_moved = lambda w, slack=2.0: None
            r.check("a window that has not moved is still good",
                    reading.still_shows(WINDOW))
            screen_context.has_moved = lambda w, slack=2.0: dict(w, x=900.0)
            r.check("one that moved is not outlined",
                    not reading.still_shows(WINDOW),
                    "the rectangle would now be over whatever took its place")
        finally:
            screen_context.has_moved = kept
        r.check("no window is not still showing", not reading.still_shows({}))

    # ── How it ended ────────────────────────────────────────────────────
    r.head("Six endings, told apart")
    with guard(r, "from what perception actually returns"):
        ok = "Window: Preview\nTitle: worksheet.pdf\n\n--- what is on screen ---\nsome text"
        r.check("a good read is a good read", reading.outcome_of(ok) == reading.OK)
        r.check("could not capture is not the same as could not read",
                reading.outcome_of("Window: Preview\n\n(Could not capture this window's contents.)")
                == reading.NO_CAPTURE)
        r.check("visible but unreadable says so",
                reading.outcome_of("Window: Preview\n\n(No readable text in this window.)")
                == reading.UNREADABLE)
        r.check("no window is its own ending",
                reading.outcome_of("Error: I could not find a window matching 'Excel'.")
                == reading.NO_WINDOW)
        r.check("a missing permission is not a failure to read",
                reading.outcome_of("Error: I cannot enumerate windows. Diablo needs Screen "
                                   "Recording permission in System Settings.")
                == reading.NO_PERMISSION)
        r.check("a refusal stays a refusal",
                reading.outcome_of("Refused: 1Password looks like a password manager.")
                == reading.REFUSED)
        r.check("nothing at all is unreadable", reading.outcome_of("") == reading.UNREADABLE)
        r.check("structured() reports through its error field",
                reading.outcome_of({"error": "Could not capture that window."})
                == reading.NO_CAPTURE)
        r.check("  and a result with no regions never read anything",
                reading.outcome_of({"app": "Preview"}) == reading.UNREADABLE)
        r.check("  while one with regions did",
                reading.outcome_of({"app": "Preview", "regions": [region()]}) == reading.OK)

    with guard(r, "and the screen's own words are not evidence"):
        about = ("Window: Chrome\n\n--- what is on screen ---\n"
                 "Our OCR pipeline could not capture the frame, so no readable text "
                 "was produced.")
        r.check("a page that talks about failing to read is not a failed read",
                reading.outcome_of(about) == reading.OK,
                "scanning the whole transcript for those phrases misreads the content "
                "as a report about itself")

    with guard(r, "said the way a person would"):
        for outcome in (reading.NO_CAPTURE, reading.UNREADABLE, reading.NO_WINDOW,
                        reading.NO_PERMISSION, reading.REFUSED):
            line = reading.message_for(outcome, "Preview")
            r.check(f"{outcome}: {line}",
                    bool(line) and line[0].isupper() and line.endswith(".")
                    and not any(word in line.lower() for word in
                                ("perception", "arbitration", "ocr", "fusion",
                                 "exception", "null", "traceback")),
                    "no vague technical language")
        r.check("a good read has nothing to apologise for",
                reading.message_for(reading.OK) == "")
        r.check("the window is named where it helps",
                "Preview" in reading.message_for(reading.NO_CAPTURE, "Preview"))

    # ── What the surface says ───────────────────────────────────────────
    r.head("Activity, never reasoning")
    with guard(r, "the lines"):
        lines = [reading.status_for(reading.LOOKING, "Preview"),
                 reading.status_for(reading.READING, "Preview"),
                 reading.status_for(reading.UNDERSTOOD, "Preview", "Question 4")]
        r.check("looking names the window", lines[0] == "Looking at Preview…")
        r.check("reading names it too", lines[1] == "Reading Preview…")
        r.check("understanding names what it found", lines[2] == "Reading Question 4…")
        r.check("with no window it still says something true",
                reading.status_for(reading.READING) == "Reading your screen…")
        r.check("a finished read has no line of its own",
                reading.status_for(reading.DONE) == "")
        r.check("none of them describe thinking",
                not any(w in " ".join(lines).lower()
                        for w in ("because", "first", "then i", "step", "reason")),
                "progress describes activity, not the chain of thought")

    with guard(r, "and where the answer came from"):
        line = reading.source_line("Preview", "worksheet.pdf", "Question 4")
        r.check("provenance names the app and the window",
                line == "From Question 4 in Preview — worksheet.pdf")
        r.check("with no window title it is still useful",
                reading.source_line("Preview") == "From Preview")
        r.check("with nothing known it says nothing", reading.source_line() == "")
        r.check("it is not a quotation of the content",
                "a line of it" not in reading.source_line("Preview", "worksheet.pdf"))

    # ── The shell ───────────────────────────────────────────────────────
    r.head("Going to look at it")
    with guard(r, "content focus"):
        shell = stand_in()
        shell.focus_content(400.0, 300.0, 320.0, 240.0, label="Question 4")
        r.check("he leaves home for a region that was really read",
                shell.presence.phase == motion.MOVING)
        r.check("  and he is not pointing at it",
                not shell.presence.guiding,
                "reading something is not saying it can be pressed")
        for _ in range(400):
            shell.presence.step(1 / 120.0)
        r.check("once he arrives the outline is drawn", len(shell.shown) == 1)
        r.check("  as a region, not a ring on a point",
                bool(shell.shown) and shell.shown[0][3] == 320.0
                and shell.shown[0][4] == 240.0,
                "a width and a height is what makes the overlay outline instead of ring")

    with guard(r, "and a target he is already pointing at is not abandoned"):
        shell = stand_in()
        shell.presence.guiding = True
        shell.focus_content(400.0, 300.0, 320.0, 240.0)
        r.check("a read does not interrupt an indication being read",
                shell.presence.phase != motion.MOVING)

    with guard(r, "a failure moves him nowhere"):
        shell = stand_in()
        before = shell.presence.phase
        shell.reading_failed()
        r.check("he stays where he is", shell.presence.phase == before,
                "no fabricated visual target")
        r.check("and nothing is outlined", shell.shown == [])

    with guard(r, "the shell routes the new events"):
        source = open(os.path.join(HOME, "app_shell.py")).read()
        r.check("focus reaches focus_content",
                '"focus"' in source and "focus_content(**data)" in source)
        r.check("a failed read reaches reading_failed",
                '"unreadable"' in source and "self.reading_failed()" in source)

    # ── Real events only ────────────────────────────────────────────────
    r.head("Nothing is animated on a guess")
    with guard(r, "the bridge"):
        bridge = open(os.path.join(HOME, "diablo_app.py")).read()
        r.check("the focus is emitted only after the text was grouped",
                "reading.focus_of(result, known)" in bridge,
                "before OCR there is no region, so there is nothing true to show")
        r.check("the window is resolved once, where the capture happens",
                "local.window = window" in bridge)
        r.check("the ending is read off what perception returned",
                "reading.outcome_of(result)" in bridge)
        r.check("no timer stands in for the backend",
                not any(call in bridge for call in
                        ("threading.Timer", "time.sleep", "setTimeout")),
                "the state must reflect actual runtime events")
        r.check("the wording is worked out once, not twice",
                "reading.status_for(" in bridge and "reading.source_line(" in bridge)

    # ── Safety ──────────────────────────────────────────────────────────
    r.head("Looking, never touching")
    with guard(r, "the whole build"):
        from skills import computer_use
        r.check("computer use is still off", computer_use.ENABLED is False)
        watched = ["skills/reading.py", "diablo_app.py", "app_shell.py",
                   "skills/screen_context.py", "app/app.js", "app/surface.js"]
        banned = ("CGEventPost", "_post_click", "kCGEventLeftMouseDown",
                  "CGEventCreateMouseEvent", "CGEventCreateKeyboardEvent")
        for name in watched:
            text = open(os.path.join(HOME, name)).read()
            r.check(f"{name} posts no events",
                    not any(word in text for word in banned))
        r.check("reading nothing but reads",
                not any(word in open(os.path.join(HOME, "skills/reading.py")).read()
                        for word in ("subprocess", "screencapture", "click(", "type_text")),
                "it translates what perception found; it does not go and look")

    # ── The page ────────────────────────────────────────────────────────
    r.head("The surface's own reading rules")
    with guard(r, "node"):
        proc = subprocess.run(
            ["node", os.path.join(HERE, "js", "reading_status.mjs")],
            capture_output=True, text=True, env=dict(os.environ, DIABLO_HOME=HOME))
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("ok "):
                r.check(stripped[3:], True)
            elif stripped.startswith("FAIL "):
                r.check(stripped[5:], False)
        if proc.returncode not in (0, 1):
            r.check("the node suite ran", False, (proc.stderr or "")[-200:])

    return r.report("reading")


if __name__ == "__main__":
    sys.exit(run())
