"""
Reading a question off the screen, and drawing the working next to it.

Two failures are being tested for, and only one of them is obvious.

The obvious one: Apple's OCR cannot read mathematical notation. Measured on
three separate renderings, at sizes up to 34pt bold and at 8x magnification,
it dropped the relation every time — "3x + 5 = 20" came back as "3x+5",
"4x - 6 = 18" as "4x" and "18", "5x - 12 = 23" as "5x". A vision model reads
the same picture correctly.

The one that matters more: a vision model that reads it *wrong* is exactly the
same failure wearing a better coat. So the model is never trusted on its own.
It transcribes, sympy solves, and OCR corroborates — whatever characters OCR
did manage have to appear inside the model's line. Those are the tests below.

Nothing here needs the model running. The vision call is stubbed, so this
suite measures the guards rather than the weather.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import screen_context, tutor  # noqa: E402


def frag(text, left=0.1, top=0.3, width=0.2, height=0.04, confidence=0.35):
    return {"text": text, "confidence": confidence, "left": left,
            "right": left + width, "top": top, "height": height}


WINDOW = {"x": 0, "y": 0, "width": 1920, "height": 1080,
          "app": "Preview", "id": 1}


class Stub:
    """Stands in for the vision model, so the guards are what is measured."""

    def __init__(self, reply, available=True):
        self.reply, self.available, self.calls = reply, available, 0

    def __enter__(self):
        self._was = (screen_context.vision_available, screen_context.read_image)
        screen_context.vision_available = lambda: self.available

        def read_image(path, instruction, num_predict=300):
            self.calls += 1
            return self.reply
        screen_context.read_image = read_image
        return self

    def __exit__(self, *exc):
        screen_context.vision_available, screen_context.read_image = self._was


def main():
    r = Results()

    # ── 1. The three real OCR failures ───────────────────────────────────
    r.head("What OCR actually returned")
    cases = [
        ("3x + 5 = 20", ["Solve for x:", "3x+5"],            "3x + 5 = 20", "5"),
        ("4x - 6 = 18", ["Solve for x:", "4x", "18"],        "4x - 6 = 18", "6"),
        ("5x - 12 = 23", ["Solve for x:", "5x"],             "5x - 12 = 23", "7"),
    ]
    for real, ocr, reply, answer in cases:
        fragments = [frag(t, top=0.2 + i * 0.1) for i, t in enumerate(ocr)]
        with guard(r, f"reads {real}"):
            with Stub(reply) as stub:
                seen = tutor.vision_reading("/tmp/nope.png", fragments,
                                            "solve this question")
            r.check(f"OCR alone cannot solve {real}",
                    all(tutor.verify_equation(t) is None for t in ocr))
            r.check(f"the vision model recovers {real}",
                    seen is not None and seen["read_as"] == reply,
                    f"got {seen}")
            r.check(f"sympy gives x = {answer}",
                    seen is not None and seen["solutions"] == [answer],
                    f"got {seen and seen['solutions']}")
            r.check(f"{real} is corroborated by OCR",
                    seen is not None and seen["corroborated"])
            r.check("the model was asked exactly once", stub.calls == 1)

    # ── 2. It is not allowed to invent one ───────────────────────────────
    r.head("A reading OCR cannot back up")
    fragments = [frag("Solve for x:"), frag("5x", top=0.4)]
    with Stub("2y + 7 = 15") as stub:
        seen = tutor.vision_reading("/tmp/nope.png", fragments, "solve this")
    r.check("an equation OCR never saw is refused", seen is None,
            "the model read 2y+7=15 where OCR read 5x; nothing corroborates it")

    with Stub("5x - 12 = 23") as stub:
        seen = tutor.vision_reading("/tmp/nope.png", fragments, "solve this")
    r.check("the same picture with a matching reading is accepted",
            seen is not None and seen["corroborated"])

    # ── 3. Corroboration, on its own ─────────────────────────────────────
    r.head("Corroboration")
    pairs = [
        ("5x - 12 = 23", ["5x"],          True,  "dropped tail"),
        ("3x + 5 = 20",  ["3x+5"],        True,  "dropped right-hand side"),
        ("4x - 6 = 18",  ["4x", "18"],    True,  "dropped the middle"),
        ("2y + 7 = 15",  ["5x"],          False, "different unknown entirely"),
        ("x = 18",       ["18"],          False, "a bare number backs up nothing"),
        ("9a - 4 = 14",  ["9a"],          True,  "a different letter, still matched"),
    ]
    for line, ocr, expected, why in pairs:
        got = tutor.corroborated(line, [frag(t) for t in ocr])
        r.check(f"{line!r} vs {ocr}: {'backed' if expected else 'refused'}",
                got is expected, why)

    r.check("spacing and case do not matter",
            tutor.corroborated("5X  -  12  =  23", [frag("5x")]))
    r.check("an empty reading corroborates nothing",
            not tutor.corroborated("", [frag("5x")]))

    # ── 4. Nothing to check against ──────────────────────────────────────
    r.head("When OCR read none of it")
    blank = [frag("Solve for x:"), frag("Show all working", top=0.5)]
    with Stub("7x - 3 = 25") as stub:
        seen = tutor.vision_reading("/tmp/nope.png", blank, "solve this")
    r.check("a reading with no OCR fragment at all is still usable",
            seen is not None and seen["solutions"] == ["4"], f"got {seen}")
    r.check("but it is flagged as uncorroborated, so it gets read back aloud",
            seen is not None and not seen["corroborated"])

    # ── 5. Refusing, still ───────────────────────────────────────────────
    r.head("Refusals that must survive")
    with Stub("NONE") as stub:
        r.check("NONE is not an equation",
                tutor.vision_reading("/tmp/nope.png", fragments, "solve") is None)
    with Stub("I can see a worksheet with several algebra questions on it.") as s:
        r.check("prose from the model is not an equation",
                tutor.vision_reading("/tmp/nope.png", fragments, "solve") is None,
                "it must transcribe, not chat")
    with Stub("Error: vision model unavailable (connection refused)") as stub:
        r.check("an error string is not an equation",
                tutor.vision_reading("/tmp/nope.png", fragments, "solve") is None)
    with Stub("5x - 12 = 23", available=False) as stub:
        r.check("no model installed means no call and no reading",
                tutor.vision_reading("/tmp/nope.png", fragments, "solve") is None
                and stub.calls == 0)

    # ── 6. Drawing the working ───────────────────────────────────────────
    # The actual complaint: the marker never once appeared for a maths
    # question, because working steps have no on-screen label to anchor to.
    r.head("The working goes on screen")
    fragments = [frag("Solve for x:", top=0.20),
                 frag("5x", left=0.12, top=0.30, width=0.06)]
    steps = [tutor.Step(say="Add twelve to both sides.", show="5x = 35",
                        found=False),
             tutor.Step(say="Divide both sides by five.", show="x = 7",
                        found=False)]
    pinned = tutor.pin_working(steps, fragments, WINDOW)
    r.check("both working steps get a position", pinned == 2, f"pinned {pinned}")
    r.check("every one of them will draw", all(s["found"] for s in steps))
    r.check("the caption is the working, not the label on screen",
            [s["matched"] for s in steps] == ["5x = 35", "x = 7"])
    xs = {round(s["x"]) for s in steps}
    ys = {round(s["y"]) for s in steps}
    r.check("the marker stays on the question while the working develops",
            len(xs) == 1 and len(ys) == 1, f"x={xs} y={ys}")
    r.check("it lands inside the window",
            0 < steps[0]["x"] < WINDOW["width"] and 0 < steps[0]["y"] < WINDOW["height"],
            f"({steps[0]['x']:.0f}, {steps[0]['y']:.0f})")

    r.head("What pinning must not do")
    anchored = [tutor.Step(say="Click Export.", show="", found=True,
                           x=100.0, y=200.0, matched="Export")]
    tutor.pin_working(anchored, fragments, WINDOW)
    r.check("a step already anchored to a real label is left alone",
            anchored[0]["matched"] == "Export" and anchored[0]["x"] == 100.0)

    no_show = [tutor.Step(say="Think about it.", found=False)]
    r.check("a step with no working to show is not given a marker",
            tutor.pin_working(no_show, fragments, WINDOW) == 0
            and not no_show[0]["found"])

    essay = [tutor.Step(say="Now simplify.", found=False,
                        show="x = (-b + sqrt(b^2 - 4ac)) / 2a for every real root")]
    r.check("working too long to read at a glance is not drawn",
            tutor.pin_working(essay, fragments, WINDOW) == 0)

    prose_only = [tutor.Step(say="Subtract twelve.", show="5x = 35", found=False)]
    r.check("with no question visible, nothing is pinned",
            tutor.pin_working(prose_only, [frag("Welcome back")], WINDOW) == 0,
            "there is no problem on screen to sit the marker on")

    return r.report("vision maths")


if __name__ == "__main__":
    sys.exit(main())
