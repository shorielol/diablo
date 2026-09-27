#!/usr/bin/env python3
"""
B.5.1 — the thing has to be in the content that was asked about.

Found in B.5: with the intended diagram covered by a video and then by a
game, grounding still returned "welfare loss" and "consumer surplus" — out of
the video frame. Three leaks, all in how a window was used rather than in
what was known about it:

  1. grounding captured a screen RECTANGLE (`screencapture -R`), which holds
     whatever is on top at those coordinates, not the window it was meant to
     be about;
  2. OCR, and the caption used as evidence, looked their window up by APP
     NAME — the first window of that app, or the front window if none
     matched — so another window's words could anchor this one's answer;
  3. the window resolved at the start of a turn was not the one grounded:
     point_at resolved "whatever is in front" again, later.

The fix binds everything to one window, checked before any model runs. These
tests build a desktop the way `screencapture -R` would see it — the intended
window with an unrelated one in front of part of it — and use a judge that is
deliberately fooled by the distractor. The answer must still come from the
intended window, or not come at all.
"""

import json
import os
import shutil
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402
import test_grounding_geometry as geo  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import (computer_use, display, grounding, perception, pointing,  # noqa: E402
                    regions, screen_context, target)

NOW = time.time()


def live(wid, app, title, x, y, w, h):
    """A window as list_windows reports it, provenance included."""
    return {"id": wid, "app": app, "title": title, "x": float(x), "y": float(y),
            "width": w, "height": h, "sensitive": False, "seen_at": NOW}


# The intended content, and an unrelated window in front of part of it.
A = live(101, "Preview", "Subsidy and Welfare.pdf", 100, 100, 852, 632)
B = live(202, "Zen", "Economics lecture - video", 600, 150, 420, 330)
DESK = (1400, 1000)


def desktop(front=(B,), a_has_triangle=True, b_has_triangle=True):
    """The screen, as a rectangle capture sees it: A, then B painted over it."""
    img = np.full((DESK[1], DESK[0], 3), 240, np.uint8)
    a = geo.diagram(grain=6)
    if not a_has_triangle:
        a[270:370, 300:380] = 255
        a[:28, :] = 255
    ax, ay = int(A["x"]), int(A["y"])
    img[ay:ay + 632, ax:ax + 852] = a
    for w in front:
        x, y, ww, hh = int(w["x"]), int(w["y"]), int(w["width"]), int(w["height"])
        pane = np.full((hh, ww, 3), 252, np.uint8)
        if b_has_triangle:
            geo.triangle(pane, (60, 60), (220, 60), (220, 260), (160, 160, 165))
        img[y:y + hh, x:x + ww] = pane
    return img


class Screen:
    """The desktop, the window list and the model, all stood in for."""

    def __init__(self, windows, image, choose="distractor", cursor=None):
        self.windows, self.image, self.choose, self.cursor = windows, image, choose, cursor
        self.captures, self.asked, self.located = [], [], []

    def __enter__(self):
        self.saved = (screen_context.list_windows, display.capture_region,
                      screen_context._for_vision, screen_context.read_image,
                      pointing.locate, display.cursor)
        screen_context.list_windows = lambda *a, **k: [dict(w) for w in self.windows]

        def capture(x, y, w, h):
            self.captures.append((x, y, w, h))
            x0, y0 = int(round(x)), int(round(y))
            part = self.image[y0:y0 + int(round(h)), x0:x0 + int(round(w))]
            return geo.save_png(np.ascontiguousarray(part), f"scope_{len(self.captures)}.png")
        display.capture_region = capture
        screen_context._for_vision = lambda p: (p, False)

        def read_image(path, prompt, num_predict=0, schema=None):
            self.asked.append(prompt)
            if schema and "choice" in json.dumps(schema):
                letters = schema["properties"]["choice"]["enum"][:-1]
                return json.dumps({"choice": self.pick(path, letters)})
            return json.dumps({"found": False, "bbox_2d": [0, 0, 0, 0], "where": "",
                               "confidence": 0.0, "what": ""})
        screen_context.read_image = read_image

        def locate(intent, target="", limit=5, observation=None, window=None):
            self.located.append({"intent": intent, "target": target,
                                 "window": (window or {}).get("id")})
            return []
        pointing.locate = locate
        display.cursor = lambda: self.cursor
        return self

    def pick(self, sheet, letters):
        """
        A judge fooled by the distractor: it prefers any grey area it can see
        inside window B, and only otherwise takes the one in window A.
        """
        crop = self.captures[-1]
        rgb = regions.load_rgb(sheet.replace(".sheet.png", ""))
        found = regions.candidates(rgb, min_edge=24)
        grey = [(i, f) for i, f in enumerate(found)
                if abs(f.colour[0] - f.colour[2]) < 12 and f.colour[0] < 200]
        for i, f in grey:
            qx, qy = crop[0] + (f.left + f.right) / 2, crop[1] + (f.top + f.bottom) / 2
            inside_b = (B["x"] <= qx <= B["x"] + B["width"]
                        and B["y"] <= qy <= B["y"] + B["height"])
            if inside_b and self.choose == "distractor" and i < len(letters):
                return letters[i]
        for i, f in grey:
            if i < len(letters):
                return letters[i]
        return "NONE"

    def __exit__(self, *exc):
        (screen_context.list_windows, display.capture_region, screen_context._for_vision,
         screen_context.read_image, pointing.locate, display.cursor) = self.saved


def in_rect(c, w):
    cx, cy = c.x + c.width / 2, c.y + c.height / 2
    return w["x"] <= cx <= w["x"] + w["width"] and w["y"] <= cy <= w["y"] + w["height"]


def run() -> int:
    r = Results()
    question = "where is the welfare loss region"

    # ── A, K, M. The distractor in front of the intended window ─────────────
    r.head("A  an unrelated window in front of the intended one")
    with guard(r, "the fooled judge cannot reach the distractor"):
        with Screen([B, A], desktop()):
            found = grounding.ground(question, A, stated_kind="region")
        c = found.candidate
        r.check("an answer came from the intended window", c is not None
                and in_rect(c, A) and not in_rect(c, B), str(c))
        r.check("  and it is the welfare-loss triangle, not the distractor's",
                c is not None and abs(c.x - (100 + 313)) <= 4 and abs(c.y - (100 + 281)) <= 4,
                str(c))
        r.check("  it says which window it came from", c is not None
                and getattr(c, "window_id", 0) == A["id"])
    with guard(r, "K  the thing is only in the wrong window"):
        with Screen([B, A], desktop(a_has_triangle=False)):
            found = grounding.ground(question, A, stated_kind="region")
        r.check("nothing is returned from the distractor: an honest abstention",
                found.candidate is None and found.abstained, str(found.candidate))
    with guard(r, "M  mostly hidden: no model is asked at all"):
        big = live(303, "Minecraft", "Minecraft", 80, 80, 900, 680)
        with Screen([big, A], desktop(front=(big,))) as s:
            found = grounding.ground(question, A, stated_kind="region")
        r.check("abstains", found.candidate is None and found.abstained, found.note)
        r.check("  before any capture or vision call",
                s.captures == [] and s.asked == [], f"{s.captures} {len(s.asked)}")
        r.check("  and says why in plain words",
                "behind" in found.note.lower() and "window" in found.note.lower(), found.note)

    # ── J, H, I. The window itself, now ─────────────────────────────────────
    r.head("J-I  the window as it is now, not as it was")
    with guard(r, "J  closed"):
        with Screen([B], desktop()) as s:
            found = grounding.ground(question, A, stated_kind="region")
        r.check("a window that has closed is not looked at",
                found.candidate is None and found.abstained and s.captures == [], found.note)
        r.check("  and that is what is said", "closed" in found.note.lower() or
                "gone" in found.note.lower(), found.note)
    with guard(r, "I  moved"):
        moved = dict(A, x=300.0, y=200.0)
        image = np.full((DESK[1] + 200, DESK[0] + 200, 3), 240, np.uint8)
        image[200:832, 300:1152] = geo.diagram(grain=6)
        with Screen([moved], image, choose="first"):
            found = grounding.ground(question, A, stated_kind="region")
        c = found.candidate
        r.check("grounding uses where the window is now",
                c is not None and abs(c.x - (300 + 313)) <= 4, str(c))
    with guard(r, "H  stale by the time it would be drawn"):
        shown = []
        state = {"windows": [A]}
        saved = (screen_context.list_windows, grounding.ground, pointing.show)
        screen_context.list_windows = lambda *a, **k: [dict(w) for w in state["windows"]]
        wanted = target.read_question(question, stated="region")
        cand = perception.Candidate(label="welfare loss", role="region", x=413.0, y=381.0,
                                    width=59.0, height=81.0, confidence=0.85,
                                    sources=[target.REGION_SOURCE])

        def ground(q, window=None, stated_kind=""):
            state["windows"] = [dict(A, x=700.0)]       # it moved while we looked
            return grounding.Ground(target=wanted, candidate=cand, scope=target.WINDOW)
        grounding.ground = ground
        pointing.show = lambda *a, **k: shown.append(a) or ""
        try:
            answer = pointing.point_at("welfare loss region", kind="region", window=A)
        finally:
            screen_context.list_windows, grounding.ground, pointing.show = saved
        r.check("geometry from before the move is not drawn", shown == [], str(shown))
        r.check("  and the user is told", "moved" in answer.lower() or "again" in answer.lower(),
                answer)

    # ── L, B, C, T. Words and candidates keep their window ──────────────────
    r.head("L-T  words and candidates stay with their window")
    with guard(r, "L  the caption is read from the intended window only"):
        with Screen([B, A], desktop()) as s:
            grounding.ground(question, A, stated_kind="region")
        r.check("the anchor search was bound to window A",
                bool(s.located) and all(l["window"] == A["id"] for l in s.located),
                str(s.located))
    with guard(r, "C  same application, another window"):
        other = live(102, "Preview", "Lecture notes.pdf", 900, 100, 400, 500)
        with Screen([other, A], desktop(front=())) as s:
            perception._from_ocr("welfare loss", A)
        r.check("OCR reads the window it was given, not the app's first window",
                bool(s.located) and s.located[-1]["window"] == A["id"], str(s.located))
    with guard(r, "T  no fusion across windows"):
        one = perception.Candidate(label="Welfare loss", role="text", x=500, y=300, width=80,
                                   height=18, confidence=0.9, sources=["ocr"], window_id=101)
        two = perception.Candidate(label="Welfare loss", role="AXStaticText", x=505, y=302,
                                   width=80, height=18, confidence=0.9,
                                   sources=["accessibility"], window_id=202)
        same = perception.Candidate(label="Welfare loss", role="AXStaticText", x=505, y=302,
                                    width=80, height=18, confidence=0.9,
                                    sources=["accessibility"], window_id=101)
        r.check("two windows' matches stay two candidates",
                len(perception._fuse([one, two])) == 2)
        r.check("  while one window's two sources still fuse",
                len(perception._fuse([one, same])) == 1)

    # ── D, E, F, G, N, O. Which content is in scope ─────────────────────────
    r.head("D-O  which content the question is about")
    from skills import showing

    def prepared(question_text, windows, cursor=None):
        with Screen(windows, desktop(), cursor=cursor):
            shown = target.asked_to_show(question_text)
            return showing.prepare(question_text, shown)

    chrome = live(401, "Google Chrome", "Welfare economics - YouTube", 0, 0, 1400, 1000)
    with guard(r, "D  an application named in the question"):
        p = prepared("Where is the welfare loss region in Preview?", [chrome, A])
        r.check("Preview is the scope, although Chrome is in front",
                p is not None and not p.refused and p.window["id"] == A["id"],
                str(p and (p.window, p.refused)))
        r.check("  and 'in Preview' is not looked for as part of the thing",
                p is not None and "preview" not in p.shown.target.semantic.lower(),
                str(p and p.shown.target.semantic))
    with guard(r, "E  a window named by its content"):
        p = prepared("Where is the welfare loss region in the Subsidy and Welfare document?",
                     [chrome, A])
        r.check("the named window is the scope", p is not None and not p.refused
                and p.window["id"] == A["id"], str(p and (p.window, p.refused)))
    with guard(r, "D' a generic 'this diagram' means what is in front"):
        p = prepared("Where is the welfare loss region in this diagram?", [A, chrome])
        r.check("the front window", p is not None and not p.refused
                and p.window["id"] == A["id"], str(p and p.window))
    with guard(r, "F  the pointer's own window for 'what am I pointing at'"):
        p = prepared("What am I pointing at?", [chrome, B, A], cursor=(200.0, 200.0))
        r.check("the window under the pointer, not the front one",
                p is not None and not p.refused and p.window["id"] == chrome["id"],
                str(p and p.window))
        p = prepared("What am I pointing at?", [B, A], cursor=(150.0, 700.0))
        r.check("  and when the pointer is over A behind B, A",
                p is not None and p.window["id"] == A["id"], str(p and p.window))
    with guard(r, "G  a named window beats where the pointer is"):
        p = prepared("Where is the welfare loss region in Preview?", [chrome, A],
                     cursor=(50.0, 50.0))
        r.check("Preview, although the pointer is over Chrome",
                p is not None and p.window["id"] == A["id"], str(p and p.window))
    with guard(r, "N  nothing to look at"):
        p = prepared("Where is the welfare loss region?", [])
        r.check("no window: said, not guessed", p is not None and bool(p.refused)
                and "window" in p.refused.lower(), str(p and p.refused))
    with guard(r, "N' a named application that is not open"):
        p = prepared("Where is the welfare loss region in Keynote?", [chrome, A])
        r.check("said, and not answered from another window",
                p is not None and bool(p.refused) and "keynote" in p.refused.lower(),
                str(p and p.refused))
    with guard(r, "O  two windows match the name"):
        other = live(102, "Preview", "Lecture notes.pdf", 900, 100, 400, 500)
        p = prepared("Where is the welfare loss region in Preview?", [chrome, other, A])
        r.check("asked which, not guessed", p is not None and bool(p.refused)
                and "which" in p.refused.lower(), str(p and p.refused))
        r.check("  in plain words, with no internal terms",
                p is not None and not any(w in (p.refused or "").lower()
                                          for w in ("scope", "provenance", "window_id")),
                str(p and p.refused))

    # ── P, Q, R. Nothing that worked stops working ─────────────────────────
    r.head("P-R  what worked still works")
    with guard(r, "P  an unobstructed region"):
        with Screen([A], desktop(front=()), choose="first"):
            found = grounding.ground(question, A, stated_kind="region")
        c = found.candidate
        r.check("found, with its outline", c is not None and in_rect(c, A)
                and bool(getattr(c, "outline", None)), str(c))
    with guard(r, "Q  a control through perception, in the named window"):
        with Screen([B, A], desktop()) as s:
            perception.read("Save", A)
        r.check("its OCR is bound to that window too",
                all(l["window"] == A["id"] for l in s.located), str(s.located))
    with guard(r, "a window without provenance is taken as given (tests, scripts)"):
        bare = {k: v for k, v in A.items() if k != "seen_at"}
        with Screen([], desktop(front=()), choose="first"):
            found = grounding.ground(question, bare, stated_kind="region")
        r.check("not refused for being absent from the live list",
                found.candidate is not None, found.note)

    # ── Safety ─────────────────────────────────────────────────────────────
    r.head("safety")
    with guard(r, "source"):
        banned = ("CGEventPost", "CGEventCreateMouseEvent", "CGEventCreateKeyboardEvent",
                  "CGWarpMouseCursorPosition", "setMouseLocation", "_post_click")
        for module in (screen_context, grounding, perception, pointing, showing, regions):
            r.check(f"{module.__name__}: no synthetic input",
                    not any(b in open(module.__file__).read() for b in banned))
        r.check("computer use is off", computer_use.ENABLED is False)

    shutil.rmtree(geo.TMP, ignore_errors=True)
    return r.report("scope")


if __name__ == "__main__":
    sys.exit(run())
