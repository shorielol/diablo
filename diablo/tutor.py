"""
D.I.A.B.L.O. — showing you how, rather than doing it for you.

The complaint that produced this file: asked to help with a maths question on
screen, Diablo read the screen, worked out the answer, and said the answer. The
question had been "help me with this", and the help that was wanted was the
method. Being handed a number teaches nothing, and the next question of the
same shape is just as hard.

So there are two failures to fix and they are separate:

1. **Intent.** "Help me with this" and "what is the answer" are different
   requests wearing similar words. The first wants to be taught.
2. **Delivery.** An explanation of something on screen is much better pointed
   at than described. "The tempo box, top left" is a sentence you have to
   decode; a marker sitting on the tempo box is not.

── The accuracy rule ───────────────────────────────────────────────────────
Every marker position comes from a measured OCR box in the window that is
actually in front. Not from the model, ever. The model proposes what to point
at as a string; that string is matched against text genuinely on screen, and a
step whose target cannot be found loses its marker and is narrated instead.

This is the whole reason the feature is worth having. A tutor that points
confidently at the wrong button is worse than one that does not point at all,
because you will look where it points.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import config  # noqa: E402
from skills import pointing  # noqa: E402
from skills import screen_context  # noqa: E402
from skills.voice import EXPLAIN_WPM  # noqa: E402

OVERLAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay.py")

MAX_STEPS = config.env_int("DIABLO_TUTOR_MAX_STEPS", 7, low=2, high=12)

# Stricter than pointing.MIN_SCORE, deliberately.
#
# Pointing once at roughly the right place is recoverable — you look, you see
# it is wrong, you ask again. A walkthrough is not: step two points somewhere
# wrong and steps three, four and five are built on it, and by then you are
# following a confident account of an interface that is not there. "Tutor"
# scores 0.89 against "tutor test.txt", which is a fine guess for a pointer
# and a bad basis for a lesson.
ANCHOR_SCORE = config.env_float("DIABLO_TUTOR_ANCHOR_SCORE", 0.62)
# A beat between steps, so one idea lands before the next arrives.
STEP_PAUSE = config.env_float("DIABLO_TUTOR_STEP_PAUSE", 0.9)


# ─── Is this a request to be taught? ─────────────────────────────────────────
#
# Kept as patterns rather than asked of the model. It is a cheap, stable
# judgement, it has to be right far more often than it has to be clever, and
# the model is the thing that was getting it wrong.

TEACH_PHRASES = (
    "help me with", "help me on", "how do i", "how do you", "how does",
    "show me how", "teach me", "walk me through", "explain how", "explain this",
    "explain it", "talk me through", "guide me", "step by step", "step-by-step",
    "where do i", "what do i do", "how would i", "how can i", "i don't know how",
    "i dont know how", "im stuck", "i'm stuck", "help me understand",
)

# A bare request for a value. "What is the answer" really does want the answer.
ANSWER_PHRASES = (
    "just tell me", "just the answer", "what's the answer", "whats the answer",
    "give me the answer", "don't explain", "dont explain", "no explanation",
)


def wants_teaching(text: str) -> bool:
    """True if he is asking to be shown, rather than told."""
    lowered = " " + (text or "").lower().strip() + " "
    if any(p in lowered for p in ANSWER_PHRASES):
        return False
    return any(p in lowered for p in TEACH_PHRASES)


# ─── Planning the walkthrough ────────────────────────────────────────────────

STEPS_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["ui", "work", "unclear"]},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "say": {"type": "string"},
                    "look_for": {"type": "string"},
                },
                "required": ["say"],
            },
        },
    },
    "required": ["kind", "steps"],
}

PLAN_PROMPT = """You are teaching someone to do something themselves, using what is on their screen.

You are given their question and the text currently visible on their screen.

Write the steps they should follow. For each step:
  "say"      one short spoken sentence. Second person. Say what to do, and why
             where that is not obvious.
  "look_for" A SHORT phrase, two to five words, copied exactly from the screen
             text above. It puts a marker on that spot, so it must be a label
             that really appears there, not a description of one. Write
             "Settings", not "the Settings link in the sidebar". Leave it out
             if the step is not about one spot.

Almost always, the first step is navigation. What they are asking about is
usually not on the screen yet — only the way to reach it is. A visible tab,
section, menu or link whose name is related to their question IS the answer to
the first step. "Settings" is where settings are. "Export" is where exporting
is. Start there and describe what they will find.

Set "kind":
  "ui"      they are learning to use an application
  "work"    they are learning to solve a problem shown on screen, such as a
            question or an equation
  "unclear" ONLY when there is neither the thing they asked about nor any
            visible route towards it. If any label on screen plausibly leads
            there, this is "ui" and not "unclear".

Rules that matter:
- Teach the method. Never give only the result. For a calculation, each step is
  one operation, and says what was done to both sides or what rule applies.
- "look_for" must be text from the screen above. Never invent a button, a menu
  or a label you cannot see.
- Between two and seven steps. One idea each.

Reply with JSON only."""


# Vision reports a confidence per fragment. Anything at or under this is a
# guess, and the maths on screen is routinely the lowest-scoring thing in the
# window — "3x + 5 = 20" came back as '3x*5' at 0.3 while the prose around it
# scored 0.5. Teaching from a 0.3 reading is how you end up explaining
# multiplication for a question about addition.
SHAKY = config.env_float("DIABLO_OCR_SHAKY", 0.45)

# Characters that make a misread change the meaning rather than the spelling.
MATHS = set("=+-*/^<>()[]{}\u00d7\u00f7\u2212\u2260\u2264\u2265\u221a\u03c0")

# Phrases that say, on the screen itself, that there is a problem to solve.
#
# This is the strongest signal available and it does not depend on OCR getting
# the symbols right — "Solve for x:" is prose, which Vision reads well, while
# the equation underneath it is exactly what Vision reads badly.
SOLVE_CUES = (
    "solve for", "solve the", "evaluate", "simplify", "find the value",
    "find an expression", "calculate", "work out", "determine the value",
    "show that", "prove that", "differentiate", "integrate", "factorise",
    "factorize", "expand and simplify",
)

SPOKEN_MATHS = [
    (r"\bequals\b|\bis equal to\b|\bis\b(?=\s*[-\d])", "="),
    (r"\bplus\b|\band\b(?=\s*\d)", "+"),
    (r"\bminus\b|\btake away\b|\bsubtract\b", "-"),
    (r"\btimes\b|\bmultiplied by\b", "*"),
    (r"\bdivided by\b|\bover\b", "/"),
    (r"\bsquared\b", "**2"),
    (r"\bcubed\b", "**3"),
    (r"\bto the power of\b", "**"),
]

_NUMBER_WORDS = {
    "zero": "0", "nought": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "thirteen": "13", "fourteen": "14", "fifteen": "15", "sixteen": "16",
    "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20",
    "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
    "seventy": "70", "eighty": "80", "ninety": "90", "hundred": "100",
}


def spoken_to_maths(text: str) -> str:
    """
    "three x plus five equals twenty" into "3x+5=20".

    Needed because the answer to "read it out to me" arrives through Whisper as
    words. Deliberately small: it handles the operators and the small numbers
    people actually say, and anything it cannot convert simply fails to parse
    later, which is the safe direction.
    """
    # Numbers first, then operators. The other way round, "is eleven" never
    # becomes "= 11", because the "is" rule is looking for a digit that is
    # still spelled out.
    out = (text or "").lower().strip()
    out = " ".join(_NUMBER_WORDS.get(w.strip(".,"), w) for w in out.split())
    for pattern, symbol in SPOKEN_MATHS:
        out = re.sub(pattern, f" {symbol} ", out)
    out = re.sub(r"[^0-9a-z+\-*/=().\s]", " ", out)
    return re.sub(r"\s+", "", out)


def shaky_readings(fragments: List[dict]) -> List[dict]:
    """Fragments Diablo is not confident it read, that would matter if wrong."""
    out = []
    for f in fragments:
        if f["confidence"] > SHAKY:
            continue
        # A maths symbol, and nothing weaker. The version that also accepted
        # "looks like a problem" warned about "33m" in a sidebar, and a warning
        # that fires on timestamps is a warning he learns to ignore.
        if any(ch in MATHS for ch in f["text"]):
            out.append(f)
    return out


def verify_equation(text: str):
    """
    Solve it properly, or return None.

    The model is not allowed to do the arithmetic. This project already learnt
    that on a three-row CSV it totalled as 350 when the answer was 115, and a
    tutor inventing a step is worse than one refusing to.
    """
    cleaned = (text or "").replace("\u00d7", "*").replace("\u00f7", "/").replace("^", "**")
    cleaned = cleaned.replace(" ", "")
    if "=" not in cleaned or not any(c.isalpha() for c in cleaned):
        return None
    left, _, right = cleaned.partition("=")
    if not left or not right:
        return None
    try:
        import sympy
        from sympy.parsing.sympy_parser import (
            parse_expr, standard_transformations, implicit_multiplication_application)
        transforms = standard_transformations + (implicit_multiplication_application,)
        lhs = parse_expr(left, transformations=transforms)
        rhs = parse_expr(right, transformations=transforms)
        unknowns = sorted((lhs - rhs).free_symbols, key=str)
        if len(unknowns) != 1:
            return None
        symbol = unknowns[0]
        solutions = sympy.solve(sympy.Eq(lhs, rhs), symbol)
        if not solutions:
            return None
        return {"symbol": str(symbol),
                "solutions": [str(v) for v in solutions],
                "equation": f"{lhs} = {rhs}"}
    except Exception:
        return None


def looks_like_a_problem(fragment: dict) -> bool:
    """A fragment that is part of an expression rather than prose."""
    text = fragment["text"].replace(" ", "")
    if len(text) < 2 or len(text) > 60:
        return False
    letters = sum(1 for c in text if c.isalpha())
    digits = sum(1 for c in text if c.isdigit())
    symbols = sum(1 for c in text if c in MATHS)
    if letters > 4:
        return False
    if digits >= 1 and symbols >= 1:
        return True
    # A bare coefficient and unknown: "4x". On its own that is not a question,
    # it is the wreckage of one — Vision split "4x - 6 = 18" into "4x" and "18"
    # and dropped everything between them.
    #
    # Restricted to letters actually used as unknowns, because the first
    # version matched "14h" and "32m" in a list of timestamps and refused to
    # explain a settings panel on the grounds that it could not read the
    # algebra.
    if digits >= 1 and letters == 1 and symbols == 0:
        letter = next((c for c in text if c.isalpha()), "")
        return letter.lower() in "abcdfgkmnpqrstuvwxyz" and len(text) <= 4
    return False


def unreadable_problem(fragments: List[dict]) -> Optional[str]:
    """
    The text of a problem that cannot be read well enough to teach from.

    This gate exists because of a specific wrong answer. OCR read
    "3x + 5 = 20" as "3x+5" — losing the entire right-hand side — and the
    walkthrough that followed was perfectly reasoned, confidently delivered and
    wrong: it invented "3x = -5" and taught x = -5/3. Nothing downstream could
    have caught it, because every step after the first was correct given the
    first.

    The gate only closes when the screen itself says there is something to
    solve. That instruction is prose, which Vision reads reliably, while the
    equation under it is symbols, which it does not — so the prose is the
    trustworthy half of the signal and the whole test rests on it.
    """
    prose = " ".join(f["text"].lower() for f in fragments)
    if not any(cue in prose for cue in SOLVE_CUES):
        return None

    candidates = [f for f in fragments if looks_like_a_problem(f)]
    for fragment in candidates:
        if verify_equation(fragment["text"]):
            return None            # at least one reads cleanly; go ahead
    if not candidates:
        return "the equation"
    return " ... ".join(f["text"] for f in candidates[:3])


def readable(fragment: dict) -> str:
    """
    The fragment's text, if it is text at all.

    OCR of a real window is about a quarter rubbish: "rfy X. Levi", "f T",
    "(y", the leftovers of icons and partially covered labels. Handing all of
    it to the planner cost real accuracy — asked how to change a setting with
    the word "Settings" plainly on screen, it returned "unclear", because the
    list it was reading looked like noise. Filtered, it navigates.

    Anchoring still runs against the unfiltered fragments, so dropping a
    fragment here only stops the model quoting it, never stops a marker
    landing on it.
    """
    text = (fragment.get("text") or "").strip()
    if len(text) < 2:
        return ""
    letters = sum(1 for c in text if c.isalpha())
    # Mostly punctuation is a rendering artefact, not a label.
    if letters < max(1, len(text) // 3):
        return ""
    # A short string with no vowel is almost always a fragment of an icon.
    if len(text) <= 4 and not any(v in text.lower() for v in "aeiou0123456789"):
        return ""
    return text


class Step(dict):
    """{say, look_for, x, y, found}"""


def plan(question: str, screen_text: str,
         verified: Optional[dict] = None) -> Dict[str, Any]:
    """Ask for steps. Returns {"kind": ..., "steps": [...]} — never raises."""
    from skills import local_brain
    known = ""
    if verified:
        known = (f"\n\nThis equation has already been solved for you, exactly: "
                 f"{verified['equation']}, giving {verified['symbol']} = "
                 f"{', '.join(verified['solutions'])}. Teach the method that "
                 f"reaches it. Do not arrive at a different answer, and do not "
                 f"do any arithmetic of your own.")
    try:
        result = local_brain.chat_json(
            [{"role": "system", "content": PLAN_PROMPT},
             {"role": "user", "content":
              f"Their question: {question}\n\n"
              f"Text currently on their screen:\n{screen_text[:3500]}{known}"}],
            schema=STEPS_SCHEMA, num_predict=700)
    except Exception as e:
        return {"kind": "unclear", "steps": [], "error": f"{type(e).__name__}: {e}"}

    steps = []
    for raw in (result.get("steps") or [])[:MAX_STEPS]:
        say = str(raw.get("say") or "").strip()
        if not say:
            continue
        steps.append(Step(say=say, look_for=str(raw.get("look_for") or "").strip()))
    return {"kind": result.get("kind") or "unclear", "steps": steps}


def anchor(steps: List[Step], fragments: List[dict], window: dict) -> List[Step]:
    """
    Turn each step's `look_for` into a real screen position, or none at all.

    This is the check the whole feature rests on. The model's `look_for` is a
    claim about what is on screen; the OCR fragments are the evidence. A claim
    without evidence loses its marker rather than getting a guessed one.
    """
    wx, wy = window["x"], window["y"]
    ww, wh = window["width"], window["height"]

    for step in steps:
        step["found"] = False
        wanted = step.get("look_for") or ""
        if not wanted:
            continue
        # The model writes a description where a label was asked for — "the
        # part that says 'find the value of a'" instead of "find the value".
        # Anything in quotes is what it meant; failing that, the longest run of
        # real words is a better needle than the whole sentence.
        candidates = [wanted]
        quoted = re.findall(r"['\u2018\u2019\"\u201c\u201d]([^'\u2018\u2019\"\u201c\u201d]{3,60})", wanted)
        candidates = quoted + candidates
        words = pointing._words(wanted)
        if len(words) > 5:
            candidates.append(" ".join(words[:5]))

        best, best_score, used = None, 0.0, wanted
        for needle in candidates:
            for fragment in fragments:
                score = pointing._score(needle, fragment["text"])
                if score > best_score:
                    best, best_score, used = fragment, score, needle
        wanted = used
        # Every real word of the target must actually be in the fragment.
        # Fuzzy similarity alone lets a one-word invention ride in on a longer
        # unrelated string.
        if best is not None:
            asked = set(pointing._words(wanted))
            saw = set(pointing._words(best["text"]))
            if asked and not asked.issubset(saw):
                best_score = min(best_score, ANCHOR_SCORE - 0.01)
        if best is None or best_score < ANCHOR_SCORE:
            # Said to be on screen, and is not. Narrate it and point at nothing.
            step["found"] = False
            step["missing"] = wanted
            continue
        left = wx + best["left"] * ww
        right = wx + best["right"] * ww
        top = wy + best["top"] * wh
        height = best["height"] * wh
        step["x"] = (left + right) / 2.0
        step["y"] = top + height / 2.0
        step["matched"] = best["text"]
        step["score"] = best_score
        step["found"] = True
    return steps


# ─── The marker, across a whole walkthrough ──────────────────────────────────

def background_is_light(capture: str, window: dict,
                        x: float, y: float, radius: int = 46) -> bool:
    """
    Whether the screen around a point is light, so the caption can oppose it.

    Sampled from the capture that was already taken for the OCR, so it costs no
    extra screenshot. The point is in absolute screen coordinates and the image
    is the window, so it comes back to window-relative first.
    """
    try:
        from AppKit import NSBitmapImageRep
        from Foundation import NSData
        rep = NSBitmapImageRep.imageRepWithData_(
            NSData.dataWithContentsOfFile_(capture))
        if rep is None:
            return False
        width, height = rep.pixelsWide(), rep.pixelsHigh()
        px = (x - window["x"]) / max(1.0, window["width"]) * width
        py = (y - window["y"]) / max(1.0, window["height"]) * height
        total, count = 0.0, 0
        for dy in range(-radius, radius + 1, 12):
            for dx in range(-radius, radius + 1, 12):
                sx, sy = int(px + dx), int(py + dy)
                if not (0 <= sx < width and 0 <= sy < height):
                    continue
                colour = rep.colorAtX_y_(sx, sy)
                if colour is None:
                    continue
                total += (0.2126 * colour.redComponent()
                          + 0.7152 * colour.greenComponent()
                          + 0.0722 * colour.blueComponent())
                count += 1
        if not count:
            return False
        return (total / count) > 0.5
    except Exception:
        return False


class Marker:
    """One overlay process, moved from step to step."""

    def __init__(self):
        self.process: Optional[subprocess.Popen] = None

    def move(self, x: float, y: float, label: str, step: str,
             on_light: bool = False) -> bool:
        try:
            if self.process is None or self.process.poll() is not None:
                command = [sys.executable, OVERLAY, "--session",
                           "--x", f"{x:.1f}", "--y", f"{y:.1f}",
                           "--label", label[:60], "--step", step]
                if on_light:
                    command.append("--on-light")
                self.process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, text=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                return True
            self.process.stdin.write(json.dumps(
                {"x": x, "y": y, "label": label[:60], "step": step,
                 "on_light": on_light}) + "\n")
            self.process.stdin.flush()
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            if self.process and self.process.poll() is None:
                self.process.stdin.write(json.dumps({"quit": True}) + "\n")
                self.process.stdin.flush()
                self.process.wait(timeout=2)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        self.process = None


# ─── Running one ─────────────────────────────────────────────────────────────

def walk(question: str, speak: Callable[[str], None], target: str = "",
         should_stop: Optional[Callable[[], bool]] = None,
         ask: Optional[Callable[[str], str]] = None) -> str:
    """
    Teach him how, pointing at the screen as it goes.

    Returns a short written summary for the log and the window. Everything the
    user actually receives is spoken and drawn as it happens, because a
    walkthrough delivered as a paragraph afterwards is not a walkthrough.
    """
    window = None
    if target:
        window = screen_context.find_window(target)
        if window is None:
            # Strict, unlike point_at, which falls back to the front window.
            # Pointing at the wrong window wastes a second; teaching a
            # five-step lesson about the wrong application wastes a minute and
            # sounds authoritative while doing it.
            return (f"I can't find a window called '{target}', Sir. "
                    "Bring it to the front and ask me again.")
    else:
        window = screen_context.frontmost_window()
    if not window:
        return "I can't see a window to work from, Sir."
    if window.get("sensitive"):
        return (f"I won't read a {window['app']} window, Sir. "
                "Password managers and banking stay private.")

    capture = screen_context.capture_window(window["id"])
    if not capture:
        return "I couldn't capture that window, Sir."
    # Kept until the walkthrough finishes: each step samples it to decide
    # whether its caption should be dark on light or light on dark.
    fragments = screen_context._ocr_fragments(capture, window)

    if isinstance(fragments, str) or not fragments:
        return (f"I can see {window['app']} but I can't read any text in it, "
                "Sir, so I have nothing to point at.")

    fragments = sorted(fragments, key=lambda f: (round(f["top"], 3), f["left"]))
    screen_text = "\n".join(readable(f) for f in fragments if readable(f))

    # Say what was read, before teaching anything from it.
    #
    # OCR gets maths wrong far more often than it gets prose wrong, and it is
    # wrong silently — a misread "+" produces a confident, coherent, useless
    # explanation. One sentence up front turns that into something he can
    # catch in two seconds.
    shaky = shaky_readings(fragments)
    if shaky:
        worst = sorted(shaky, key=lambda f: f["confidence"])[:2]
        reading = " and ".join(f'"{f["text"]}"' for f in worst)
        speak(f"Before I start, Sir — I'm reading {reading}, and I'm not "
              f"confident I have the symbols right. Correct me if that's wrong.")

    # Anything that is an equation gets solved properly rather than in the
    # model's head. The working it narrates is then narrating a fact.
    verified = None
    for fragment in fragments:
        verified = verify_equation(fragment["text"])
        if verified:
            break

    # Half a question is not a question. Ask rather than fill in the gap.
    #
    # Apple's OCR reads prose well and mathematical notation badly — it turned
    # "3x + 5 = 20" into "3x+5" and "4x - 6 = 18" into "4x" and "18", dropping
    # the relation both times. Nothing downstream can detect that, because
    # every step after a misread first step is correct given the first. So the
    # loop is closed the only way it can be: he reads the line out, and the
    # lesson is built on what he said.
    if not verified:
        partial = unreadable_problem(fragments)
        if partial:
            heard = ""
            if ask:
                if partial == "the equation":
                    heard = ask("I can see there's something to solve, Sir, but "
                                "I can't read the equation itself. Read it out "
                                "to me.")
                else:
                    heard = ask(f"I can only make out {partial} of that line, "
                                f"Sir. Read the whole thing out to me.")
            if heard:
                verified = verify_equation(spoken_to_maths(heard))
                if verified:
                    screen_text = f"{screen_text}\n(He read it out as: {heard})"
                else:
                    return (f"I heard \"{heard}\", Sir, but I can't turn that "
                            "into an equation I trust. Try it again slowly.")
            else:
                if partial == "the equation":
                    return ("I can see there's something to solve, Sir, but I "
                            "can't read the equation. Read it out and I'll take "
                            "you through it.")
                return (f"I can only make out {partial} of that line, Sir, and "
                        "I won't teach a method for a question I've half read. "
                        "Read it out to me and I'll do it properly.")

    plan_result = plan(question, screen_text, verified)
    steps = anchor(plan_result["steps"], fragments, window)

    # The "unclear" label is the model's opinion of its own answer, and this
    # project has been here before: asked for its confidence, the classifier
    # returned 0.9 for everything, including sentences it had not understood.
    # So the label is not the test. The test is whether the steps it wrote land
    # on things that are really on screen — evidence rather than self-report.
    #
    # It produced "Look for Settings", "Check Trace" and then labelled the lot
    # unclear. Both of those anchor to real labels, and throwing them away left
    # the user with "I can't tell which part you mean" in front of a window
    # with the answer plainly visible.
    anchored = sum(1 for step in steps if step.get("found"))
    if not steps or (plan_result["kind"] == "unclear" and anchored == 0):
        return (f"I can see {window['app']}, Sir, but I can't tell which part "
                "you mean. Point me at it and I'll take you through it.")
    # Not every caller's speak() takes a pace — the agent loop passes its own
    # one-argument version. Probed once rather than guessed, so a walkthrough
    # never dies on a TypeError halfway through step three.
    def narrate(line: str) -> None:
        try:
            speak(line, pace=EXPLAIN_WPM)
        except TypeError:
            speak(line)

    marker = Marker()
    spoken = 0
    try:
        for index, step in enumerate(steps, 1):
            if should_stop and should_stop():
                speak("Stopping there, Sir.")
                break
            counter = f"{index} of {len(steps)}"
            if step.get("found"):
                light = background_is_light(capture, window, step["x"], step["y"])
                marker.move(step["x"], step["y"], step.get("matched", ""),
                            counter, on_light=light)
                # A beat for the eye to find the marker before the sentence
                # about it starts. The marker stays up for the whole sentence,
                # because speak() blocks — that is what makes the drawing and
                # the talking one event rather than two.
                time.sleep(0.25)
            narrate(step["say"])
            spoken += 1
            time.sleep(STEP_PAUSE)
    finally:
        marker.close()
        try:
            os.unlink(capture)
        except OSError:
            pass

    pointed = sum(1 for s in steps if s.get("found"))
    missed = [s["missing"] for s in steps if s.get("missing")]
    summary = f"Took him through {spoken} steps in {window['app']}"
    summary += f", pointing at {pointed} of them." if pointed else "."
    if missed:
        # Worth saying out loud. A step that could not be anchored is a step
        # the model may have imagined, and he should know which.
        summary += (" I couldn't find " + ", ".join(f"'{m}'" for m in missed[:3])
                    + " on screen, so I only described " +
                    ("those." if len(missed) > 1 else "that one."))
    return summary


if __name__ == "__main__":
    question = " ".join(sys.argv[1:]) or "help me with what's on screen"
    print(walk(question, lambda t: print(f"  SAY: {t}")))
