"""
D.I.A.B.L.O. — finding things on screen, exactly.

The problem this solves
-----------------------
An assistant that can describe your screen is useful. One that can *show you
where a thing is* is a different class of tool, because "it's in the toolbar"
and a marker sitting on the button are not the same help.

The usual way to build this is to hand a screenshot to a large vision model and
ask it for pixel coordinates. Clicky does exactly that, through Claude's
Computer Use API, downscaling the screen to 1280x800 first because the API
degrades above that. It works, and it costs a network round trip, a frontier
model, and whatever accuracy is lost in the resize.

Diablo cannot do that. Not for privacy reasons, though those apply — a 14B
running locally simply cannot ground coordinates in an image. Ask qwen3 where a
button is and you get a confident, wrong number.

So this module inverts the problem. The model never sees a pixel and never
guesses a coordinate. It only has to *name* the thing, which is the part a small
model is good at. Everything spatial is measured:

    Apple's Vision framework      exact text boxes, normalised to the image
    CGWindowList                  the window's origin and size, in points
    arithmetic                    absolute position on the desktop

Measured, not inferred. It works at full resolution, offline, in about a fifth
of a second, and a window on a second display at x=-1512 needs no special case
because the origin is already negative.

What it cannot do
-----------------
Only text is findable. An icon with no label has no OCR box, so "the gear icon"
will miss. macOS's accessibility API would cover those; that is a later job, and
`locate` is shaped to take a second source of candidates when it arrives.
"""

import difflib
import os

from skills import config  # noqa: E402
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

# The package parent, not the package itself. Importing as `skills.x` rather
# than bare `x` is deliberate: several older modules are loaded twice, once
# under each name, with separate globals, and that has already cost a debugging
# session. One module, one instance.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import screen_context  # noqa: E402

# A hit below this is not worth pointing at. Tuned so that a wrong-but-plausible
# match stays silent rather than confidently marking the wrong thing, because a
# marker on the wrong button is worse than no marker.
MIN_SCORE = config.env_float("DIABLO_POINT_MIN_SCORE", 0.45)

# Spoken ordinals, for "the third result". Diablo knows reading order because
# the fragments are sorted; a coordinate guess never does.
ORDINALS = {
    "first": 0, "1st": 0, "one": 0,
    "second": 1, "2nd": 1, "two": 1,
    "third": 2, "3rd": 2, "three": 2,
    "fourth": 3, "4th": 3, "four": 3,
    "fifth": 4, "5th": 4, "five": 4,
    "last": -1, "bottom": -1, "top": 0,
}

STOPWORDS = {"the", "a", "an", "that", "this", "my", "your", "is", "of", "on",
             "in", "at", "to", "it", "button", "link", "tab", "menu", "icon",
             "option", "field", "box", "one", "thing", "item", "please", "show",
             "me", "where", "point", "click", "find"}


class Hit(dict):
    """One place on screen, with everything needed to point at it or talk about it."""

    def __repr__(self) -> str:
        return (f"<Hit {self['text']!r} at ({self['x']:.0f},{self['y']:.0f}) "
                f"score={self['score']:.2f}>")


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _words(text: str) -> List[str]:
    return [w for w in _normalise(text).split() if w and w not in STOPWORDS]


def _score(query: str, fragment_text: str) -> float:
    """
    How well a spoken phrase matches one OCR fragment.

    Deliberately generous about extra words in the fragment and strict about
    missing ones: menus read as "File Edit View" in a single fragment, so
    "the view menu" should match it, while "review" should not match "view".
    """
    q, f = _normalise(query), _normalise(fragment_text)
    if not q or not f:
        return 0.0
    if q == f:
        return 1.0

    q_words, f_words = _words(query), _words(fragment_text)
    if not q_words:
        return 0.0

    # Whole-word containment, both directions.
    if q_words and all(w in f_words for w in q_words):
        # Shorter fragments are better targets — matching "Save" inside a
        # paragraph that mentions saving is not a place to point.
        return 0.93 - min(0.2, 0.02 * max(0, len(f_words) - len(q_words)))
    if f_words and all(w in q_words for w in f_words):
        return 0.85

    # Partial overlap only counts if it covers most of what was asked for. One
    # shared word out of four is not a match: "zzqqxxvv not a real thing at all"
    # and "clinic. All additional losses and gains" share nothing but "all", and
    # that was enough to put a marker on a paragraph of someone's notes.
    overlap = len(set(q_words) & set(f_words))
    if overlap:
        coverage = overlap / len(set(q_words))
        return 0.55 + 0.35 * coverage if coverage >= 0.5 else 0.0

    # Character similarity is the last resort and the most dangerous, because
    # "view" and "review" are 80% identical and mean different things. Only a
    # near-identical string qualifies, which leaves this to catch typos and
    # OCR damage rather than to guess.
    ratio = difflib.SequenceMatcher(None, q, f).ratio()
    return ratio * 0.75 if ratio > 0.88 else 0.0


def _ordinal_in(query: str) -> Optional[int]:
    for word in _normalise(query).split():
        if word in ORDINALS:
            return ORDINALS[word]
    return None


def locate(description: str, target: str = "", limit: int = 5) -> List[Hit]:
    """
    Find where something is, in absolute screen points.

    `target` picks the window the way the rest of Diablo does — "" is whatever
    is in front, "chrome" or "my browser" resolve through find_window.

    Returns hits sorted best first. An empty list means it was not found, which
    is a real answer and better than pointing somewhere plausible.
    """
    # A small model puts all sorts of things in `target` — sometimes the whole
    # request. If it does not resolve to a window, look at the front one rather
    # than reporting nothing found, which reads as a failure of sight.
    window = screen_context.find_window(target) if target else None
    if window is None:
        window = screen_context.frontmost_window()
    if not window or window.get("sensitive"):
        return []

    path = screen_context.capture_window(window["id"])
    if not path:
        return []
    try:
        fragments = screen_context._ocr_fragments(path, window)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if isinstance(fragments, str) or not fragments:
        return []

    # Reading order, so ordinals mean what a person means by them.
    fragments = sorted(fragments, key=lambda f: (round(f["top"], 3), f["left"]))

    wx, wy = window["x"], window["y"]
    ww, wh = window["width"], window["height"]

    def to_hit(fragment, score, index):
        left = wx + fragment["left"] * ww
        right = wx + fragment["right"] * ww
        top = wy + fragment["top"] * wh
        height = fragment["height"] * wh
        return Hit(
            text=fragment["text"],
            x=(left + right) / 2.0,
            y=top + height / 2.0,
            left=left, top=top, width=right - left, height=height,
            score=score,
            confidence=fragment["confidence"],
            reading_index=index,
            app=window["app"],
            window=window["title"],
            window_id=window["id"],
        )

    ordinal = _ordinal_in(description)
    stripped = " ".join(w for w in _normalise(description).split()
                        if w not in ORDINALS)

    scored = []
    # "the third one" names no text at all. There is nothing to match on, so
    # every fragment is a candidate and position decides — which only works
    # because the fragments are in reading order.
    bare_ordinal = ordinal is not None and not _words(stripped)
    for index, fragment in enumerate(fragments):
        if bare_ordinal:
            scored.append(to_hit(fragment, 0.5, index))
            continue
        score = _score(stripped or description, fragment["text"])
        if score >= MIN_SCORE:
            scored.append(to_hit(fragment, score, index))

    # "the third result" — rank by position, not by text similarity.
    if ordinal is not None and scored:
        scored.sort(key=lambda h: h["reading_index"])
        picked = scored[ordinal] if -len(scored) <= ordinal < len(scored) else scored[-1]
        return [picked]

    scored.sort(key=lambda h: (-h["score"], -h["confidence"], h["reading_index"]))
    return scored[:limit]


def describe_location(hit: Hit, window: Optional[Dict[str, Any]] = None) -> str:
    """
    Where something is, in words a person would use.

    Spoken directions beat coordinates over a speaker: "top left of the
    toolbar" is actionable, "at 1413, 87" is not.
    """
    windows = {w["id"]: w for w in screen_context.list_windows()}
    w = window or windows.get(hit["window_id"])
    if not w:
        return hit["text"]
    across = (hit["x"] - w["x"]) / max(1.0, w["width"])
    down = (hit["y"] - w["y"]) / max(1.0, w["height"])
    horizontal = "left" if across < 0.33 else ("right" if across > 0.66 else "centre")
    vertical = "top" if down < 0.25 else ("bottom" if down > 0.75 else "middle")
    if vertical == "middle" and horizontal == "centre":
        return "in the middle of the window"
    if vertical == "middle":
        return f"on the {horizontal}"
    return f"{vertical} {horizontal}" if horizontal != "centre" else f"{vertical} of the window"



# ─── Showing it ──────────────────────────────────────────────────────────────

OVERLAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overlay.py")


def show(x: float, y: float, label: str = "", seconds: float = 3.0) -> bool:
    """
    Put a marker on the screen at an absolute point, and return immediately.

    Spawned rather than drawn in-process: AppKit wants an event loop on the main
    thread and Diablo's main thread belongs to the audio queue. A separate
    process also means the drawing code cannot crash the assistant.
    """
    try:
        subprocess.Popen(
            [sys.executable, OVERLAY, "--x", f"{x:.1f}", "--y", f"{y:.1f}",
             "--label", (label or "")[:48], "--seconds", f"{seconds:.1f}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        return True
    except Exception:
        return False


def point_at(description: str, target: str = "", seconds: float = 3.0) -> str:
    """
    Find something on screen and mark it, then say where it is.

    The sentence matters as much as the marker. Diablo is spoken to, often while
    the user is looking somewhere else entirely, so "top right of the toolbar"
    has to stand on its own if the marker is missed.
    """
    if not description or not description.strip():
        return "I need to know what to point at, Sir."

    # Through the shared perception layer, not through locate() directly.
    #
    # This is the one place it matters most. If the marker came from OCR and
    # the click came from the accessibility tree, Diablo could draw a ring
    # around one control and press another — and the user, watching the ring,
    # would have no way to know. One pipeline, one target, both consumers.
    #
    # Imported here rather than at the top because perception imports this
    # module for its scorer, and a cycle at import time would take the whole
    # assistant down rather than just this feature.
    try:
        from skills import perception, screen_context
        window = (screen_context.find_window(target) if target
                  else screen_context.frontmost_window())
        reading = perception.read(description, window)
        if reading.ambiguous and reading.question():
            return reading.question()
        if reading.best:
            best = reading.best
            x, y = best.centre
            show(x, y, best.label or description, seconds)
            place = describe_location(
                {"x": x, "y": y, "window_id": (window or {}).get("id", 0),
                 "text": best.label}, window)
            hedge = "" if best.confidence >= 0.9 else " I think"
            found = best.label or best.describe()
            return f"{place.capitalize()}, marked{hedge} — '{found[:40]}'."
    except Exception:
        # Perception is an improvement on the OCR path, not a replacement for
        # it. If it cannot answer, the original path still can.
        pass

    hits = locate(description, target)
    if not hits:
        window = screen_context.find_window(target) if target else screen_context.frontmost_window()
        if window and window.get("sensitive"):
            return (f"I won't read a {window['app']} window, Sir. "
                    f"Password managers and banking stay private.")
        where = f" in {window['app']}" if window else ""
        return (f"I can't find anything matching '{description}'{where}. "
                f"I can only point at text I can read on screen, so an unlabelled "
                f"icon won't show up.")

    best = hits[0]
    show(best["x"], best["y"], best["text"], seconds)
    place = describe_location(best)

    # Say when the match was loose, rather than sounding certain about a guess.
    hedge = "" if best["score"] >= 0.9 else " I think"
    answer = f"{place.capitalize()}, marked{hedge} — '{best['text'][:40]}'."

    others = [h for h in hits[1:] if h["score"] >= 0.8]
    if others:
        answer += f" There are {len(others) + 1} matches; I've marked the closest."
    return answer

if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "file"
    hits = locate(query)
    if not hits:
        print(f"Nothing matching {query!r} on screen.")
    for hit in hits:
        print(f"  {hit['text'][:40]:42} ({hit['x']:7.0f}, {hit['y']:6.0f})  "
              f"score {hit['score']:.2f}  {describe_location(hit)}")
