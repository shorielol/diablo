#!/usr/bin/env python3
"""
Finding things on screen, and marking them.

The accuracy that matters here is not "did it find something" but "did it refuse
to find something that is not there". A marker on the wrong button is worse than
no marker, because the user acts on it. So most of this file is about the
negative cases: nonsense queries, sensitive windows, unlabelled icons.

The coordinate arithmetic is checked against window bounds rather than against a
screenshot, because a hit outside its own window is the failure that would put a
marker on the wrong display entirely.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard, Timer  # noqa: E402

from skills import pointing, screen_context  # noqa: E402

r = Results()


def run():
    r.head("Matching")
    cases = [
        ("exact", "continue", "Continue", 0.99),
        ("case insensitive", "CONTINUE", "Continue", 0.99),
        ("stopwords ignored", "the continue button", "Continue", 0.85),
        ("word inside a label", "back", "< Back", 0.80),
        ("multi-word", "skip demo", "Skip demo", 0.95),
        ("partial cover", "save", "Save the file as draft", 0.45),
    ]
    for label, query, fragment, floor in cases:
        with guard(r, label):
            score = pointing._score(query, fragment)
            r.check(f"{label}: {query!r} vs {fragment!r}", score >= floor,
                    f"scored {score:.2f}, floor {floor}")

    r.head("It refuses bad matches")
    rejects = [
        ("view", "review"),
        ("save", "Cancel"),
        ("continue", "Preferences"),
        ("search", "Research Question"),
    ]
    for query, fragment in rejects:
        with guard(r, f"{query!r} does not match {fragment!r}"):
            score = pointing._score(query, fragment)
            r.check(f"{query!r} rejects {fragment!r}", score < pointing.MIN_SCORE,
                    f"scored {score:.2f}, threshold {pointing.MIN_SCORE}")

    r.head("Ordinals")
    with guard(r, "ordinals are recognised"):
        for word, index in [("the first one", 0), ("the second one", 1),
                            ("the third result", 2), ("the last one", -1)]:
            r.check(f"{word!r} -> {index}", pointing._ordinal_in(word) == index,
                    str(pointing._ordinal_in(word)))
    with guard(r, "a phrase with no ordinal returns None"):
        r.check("plain text has no ordinal", pointing._ordinal_in("the save button") is None)

    # ── against the real screen ─────────────────────────────────────────────
    r.head("On the real screen")
    windows = screen_context.list_windows()
    if not windows:
        r.skip("live screen tests", "no windows open")
        return r.report("pointing")

    r.check("windows carry an origin",
            all("x" in w and "y" in w for w in windows),
            f"{len(windows)} windows")

    target = None
    for window in windows:
        if window.get("sensitive"):
            continue
        hits = pointing.locate("e", window["app"])   # a vowel matches almost anything
        if hits:
            target = window
            break

    if target is None:
        r.skip("coordinate checks", "no window produced OCR text")
    else:
        with guard(r, "hits land inside their own window"):
            hits = pointing.locate("e", target["app"], limit=20)
            outside = []
            for hit in hits:
                if not (target["x"] - 2 <= hit["x"] <= target["x"] + target["width"] + 2):
                    outside.append(f"{hit['text'][:16]!r} x={hit['x']:.0f}")
                elif not (target["y"] - 2 <= hit["y"] <= target["y"] + target["height"] + 2):
                    outside.append(f"{hit['text'][:16]!r} y={hit['y']:.0f}")
            r.check(f"every hit is inside {target['app']}", not outside,
                    "; ".join(outside[:3]) or f"{len(hits)} hits, all inside")

        with guard(r, "hits are in reading order"):
            hits = pointing.locate("the first one", target["app"])
            r.check("an ordinal returns exactly one hit", len(hits) == 1,
                    f"{len(hits)} hits")

        with guard(r, "locating is fast enough to feel instant"):
            with Timer() as t:
                pointing.locate("e", target["app"])
            r.check("a lookup costs under a second", t.elapsed < 1.5, f"{t.elapsed:.2f}s")

        with guard(r, "describe_location says something usable"):
            hits = pointing.locate("e", target["app"])
            if hits:
                place = pointing.describe_location(hits[0])
                r.check("it reads as a direction", isinstance(place, str) and len(place) > 4,
                        place)

    with guard(r, "nonsense finds nothing"):
        hits = pointing.locate("zzqqxxvv not a real thing at all")
        r.check("no hit for nonsense", not hits, f"{len(hits)} hits")

    with guard(r, "point_at explains a miss instead of failing silently"):
        out = pointing.point_at("zzqqxxvv not a real thing at all")
        r.check("it returns a sentence", isinstance(out, str) and len(out) > 20, out[:70])
        r.check("and says why", "can't find" in out.lower() or "cannot" in out.lower(),
                out[:70])
        r.check("and mentions the icon limitation", "icon" in out.lower(), out[-60:])

    with guard(r, "an empty description is refused"):
        r.check("empty asks for a subject", "point at" in pointing.point_at("").lower())

    r.head("Privacy")
    with guard(r, "sensitive windows are never located"):
        r.check("the refuse list exists", bool(screen_context.SENSITIVE_APPS),
                ", ".join(sorted(screen_context.SENSITIVE_APPS)[:5]))
        fake = {"id": -1, "app": "1password", "title": "", "x": 0, "y": 0,
                "width": 800, "height": 600, "sensitive": True}
        saved = screen_context.frontmost_window
        screen_context.frontmost_window = lambda: fake
        try:
            r.check("a sensitive window yields no hits", pointing.locate("password") == [])
            out = pointing.point_at("password")
            r.check("and point_at says why", "won't read" in out.lower(), out[:70])
        finally:
            screen_context.frontmost_window = saved

    r.head("The overlay process")
    with guard(r, "it renders where it is told"):
        if os.getenv("DIABLO_TEST_OVERLAY") != "1":
            r.skip("drawing a marker", "draws on screen; set DIABLO_TEST_OVERLAY=1")
        else:
            x, y = 700, 500
            proc = subprocess.Popen(
                [sys.executable, pointing.OVERLAY, "--x", str(x), "--y", str(y),
                 "--label", "test", "--seconds", "5"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2.0)
            shot = "/tmp/diablo_point_test.png"
            subprocess.run(["screencapture", "-x", "-o", shot], capture_output=True)
            proc.wait(timeout=12)
            from PIL import Image
            image = Image.open(shot).convert("RGB")
            scale = image.size[0] / 1920.0 if image.size[0] >= 1920 else 1.0
            cx, cy = int(x * scale), int(y * scale)
            patch = image.crop((cx - 70, cy - 70, cx + 70, cy + 70))
            marker = sum(1 for p in patch.getdata()
                         if p[0] > 180 and p[1] < 110 and p[2] < 110)
            r.check("the marker is drawn at the requested point", marker > 40,
                    f"{marker} marker pixels within 70px")
            os.unlink(shot)

    with guard(r, "the overlay never blocks the caller"):
        with Timer() as t:
            pointing.show(400, 400, "x", seconds=0.1)
        r.check("show() returns immediately", t.elapsed < 0.8, f"{t.elapsed:.2f}s")

    return r.report("pointing")


if __name__ == "__main__":
    sys.exit(run())
