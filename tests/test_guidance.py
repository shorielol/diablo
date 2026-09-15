"""
Where Diablo stands when he is pointing at something.

The one rule every check here defends: the thing being explained is never the
thing being covered. Everything else — which side, how far, whether the label
fits — follows from that and from the display the target is actually on.

Placement is pure arithmetic, so most of this runs against constructed
geometry. The cases that must not be constructed are the ones about the real
display arrangement, because the bug this module replaced was a placement rule
that knew about one edge of one display.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import display, guidance  # noqa: E402


def _overlaps(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def run():
    r = Results()
    screens = display.displays()
    main = display.main_display()
    if not main:
        r.head("Placement")
        r.bad("a display is needed to test placement", "none found")
        return r.report("guidance")

    centre = (main.x + main.width / 2, main.y + main.height / 2)
    window = {"x": main.x, "y": main.y, "width": main.width, "height": main.height}

    # ── The one rule ────────────────────────────────────────────────────────
    r.head("The target is never covered")
    with guard(r, "nothing is drawn on the target"):
        covered = []
        step = 160
        for x in range(int(main.x) + 30, int(main.right) - 30, step):
            for y in range(int(main.y) + 30, int(main.bottom) - 30, step):
                spot = guidance.place(float(x), float(y), "Click Export", window)
                if spot is None:
                    covered.append((x, y, "no placement"))
                    continue
                ring = (x - guidance.RING, y - guidance.RING,
                        guidance.RING * 2, guidance.RING * 2)
                marker = (spot.marker_x - guidance.MARKER, spot.marker_y - guidance.MARKER,
                          guidance.MARKER * 2, guidance.MARKER * 2)
                if _overlaps(marker, ring):
                    covered.append((x, y, f"marker on target from {spot.side}"))
                if spot.label_width and _overlaps(
                        (spot.label_x, spot.label_y, spot.label_width, spot.label_height),
                        ring):
                    covered.append((x, y, f"label on target from {spot.side}"))
        total = len(range(int(main.x) + 30, int(main.right) - 30, step)) * \
                len(range(int(main.y) + 30, int(main.bottom) - 30, step))
        r.check(f"no placement covers its own target ({total} points across the display)",
                not covered, "; ".join(f"{c[0]},{c[1]}: {c[2]}" for c in covered[:3]))

    with guard(r, "everything stays on the display"):
        outside = []
        step = 160
        for x in range(int(main.x) + 30, int(main.right) - 30, step):
            for y in range(int(main.y) + 30, int(main.bottom) - 30, step):
                spot = guidance.place(float(x), float(y), "Click Export", window)
                if spot is None:
                    continue
                if not (main.x <= spot.marker_x <= main.right
                        and main.y <= spot.marker_y <= main.bottom):
                    outside.append((x, y, spot.side))
                if spot.label_width and not (
                        main.x <= spot.label_x
                        and spot.label_x + spot.label_width <= main.right
                        and main.y <= spot.label_y
                        and spot.label_y + spot.label_height <= main.bottom):
                    outside.append((x, y, f"label {spot.side}"))
        r.check("no marker or label falls off the display", not outside,
                "; ".join(f"{o[0]},{o[1]} {o[2]}" for o in outside[:3]))

    # ── Choosing a side ─────────────────────────────────────────────────────
    r.head("Choosing a side")
    with guard(r, "the middle of a screen prefers above"):
        spot = guidance.place(centre[0], centre[1], "Click Export", window)
        r.check("a target with room on every side is pointed at from above",
                spot.side == "above", f"chose {spot.side}")
        r.check("the marker sits a standoff away, not on top",
                abs(spot.marker_y - centre[1]) >= guidance.STANDOFF - 1,
                f"{abs(spot.marker_y - centre[1]):.0f} points up")

    with guard(r, "edges push it to the other side"):
        cases = [
            ("near the top", (centre[0], main.y + 34), "must not choose above"),
            ("near the bottom", (centre[0], main.bottom - 34), "must not choose below"),
            ("near the left", (main.x + 34, centre[1]), "must not choose left"),
            ("near the right", (main.right - 34, centre[1]), "must not choose right"),
        ]
        for name, (x, y), why in cases:
            spot = guidance.place(x, y, "Click Export", window)
            banned = why.split()[-1]
            r.check(f"a target {name} is not pointed at from {banned}",
                    spot is not None and banned not in spot.side.split(),
                    f"chose {spot.side if spot else 'nothing'}")

    with guard(r, "a corner still finds somewhere"):
        for name, (x, y) in [("top-left", (main.x + 30, main.y + 30)),
                             ("top-right", (main.right - 30, main.y + 30)),
                             ("bottom-left", (main.x + 30, main.bottom - 30)),
                             ("bottom-right", (main.right - 30, main.bottom - 30))]:
            spot = guidance.place(x, y, "Click Export", window)
            r.check(f"a target in the {name} corner is placed", spot is not None,
                    f"{spot.side} — {spot.why}" if spot else "nothing")

    # ── The cursor belongs to the user ──────────────────────────────────────
    r.head("The pointer is left alone")
    with guard(r, "the marker avoids the cursor"):
        # Put the cursor exactly where 'above' would land, and check it moves.
        above = (centre[0], centre[1] - guidance.STANDOFF)
        spot = guidance.place(centre[0], centre[1], "Click Export", window,
                              cursor=above)
        r.check("a cursor sitting on the preferred side pushes Diablo elsewhere",
                spot.side != "above", f"chose {spot.side} instead")
        moved = ((spot.marker_x - above[0]) ** 2 + (spot.marker_y - above[1]) ** 2) ** 0.5
        r.check("and the marker ends up clear of the pointer", moved > 40,
                f"{moved:.0f} points away")

    with guard(r, "no cursor is not a crash"):
        spot = guidance.place(centre[0], centre[1], "Click Export", window, cursor=None)
        r.check("placement works with the cursor unknown", spot is not None)

    # ── Labels ──────────────────────────────────────────────────────────────
    r.head("Captions")
    with guard(r, "a long label still fits or is dropped"):
        long_label = "Click the Export button in the top right of the toolbar to save"
        spot = guidance.place(centre[0], centre[1], long_label, window)
        r.check("a long caption is placed or honestly refused",
                spot is not None, spot.why if spot else "")
        if spot and spot.label_width:
            r.check("a long caption is capped rather than run off the screen",
                    spot.label_width <= 342, f"{spot.label_width:.0f}pt wide")

    with guard(r, "no label is a valid request"):
        spot = guidance.place(centre[0], centre[1], "", window)
        r.check("a marker with no caption places fine",
                spot is not None and spot.label_width == 0)

    with guard(r, "the step counter reserves its own line"):
        plain = guidance.place(centre[0], centre[1], "Click Export", window)
        stepped = guidance.place(centre[0], centre[1], "Click Export", window, step="2 of 5")
        r.check("a step counter makes the caption box taller",
                stepped.label_height > plain.label_height,
                f"{plain.label_height:.0f} -> {stepped.label_height:.0f}")

    # ── The window the overlay has to cover ─────────────────────────────────
    r.head("The overlay window")
    with guard(r, "bounds enclose everything drawn"):
        spot = guidance.place(centre[0], centre[1], "Click Export", window)
        bx, by, bw, bh = spot.bounds()
        pieces = {
            "ring": (spot.target_x - guidance.RING, spot.target_y - guidance.RING,
                     guidance.RING * 2, guidance.RING * 2),
            "marker": (spot.marker_x - guidance.MARKER, spot.marker_y - guidance.MARKER,
                       guidance.MARKER * 2, guidance.MARKER * 2),
            "label": (spot.label_x, spot.label_y, spot.label_width, spot.label_height),
        }
        outside = [name for name, (x, y, w, h) in pieces.items()
                   if not (bx <= x and by <= y and x + w <= bx + bw and y + h <= by + bh)]
        r.check("the ring, the marker and the caption are all inside the window bounds",
                not outside, ", ".join(outside))
        r.check("the window is one rectangle, not several",
                bw > 0 and bh > 0, f"{bw:.0f}x{bh:.0f}")

    # ── Every display, not just the main one ────────────────────────────────
    r.head("Every display")
    if len(screens) < 2:
        r.skip("placement on a second display", "only one display attached")
        r.skip("placement on a negative-origin display", "only one display")
    else:
        for screen in screens[1:]:
            with guard(r, f"placement on display {screen.index}"):
                spot = guidance.place(screen.x + screen.width / 2,
                                      screen.y + screen.height / 2,
                                      "Click Export",
                                      {"x": screen.x, "y": screen.y,
                                       "width": screen.width, "height": screen.height})
                r.check(f"a target on the {screen.describe()} is placed",
                        spot is not None, spot.side if spot else "nothing")
                r.check("and is reported as being on that display",
                        spot.display_index == screen.index,
                        f"said display {spot.display_index}, is {screen.index}")
                r.check("and carries that display's scale factor",
                        abs(spot.display_scale - screen.scale) < 0.01,
                        f"{spot.display_scale:g}x")
                r.check("and the marker really is on that display",
                        display.for_point(spot.marker_x, spot.marker_y) is not None
                        and display.for_point(spot.marker_x, spot.marker_y).index == screen.index)

        negative = [s for s in screens if s.x < 0]
        if negative:
            with guard(r, "negative origins need no special case"):
                screen = negative[0]
                spot = guidance.place(screen.x + 60, screen.y + screen.height / 2,
                                      "Click Export",
                                      {"x": screen.x, "y": screen.y,
                                       "width": screen.width, "height": screen.height})
                r.check("a target near the left edge of a negative-x display is placed",
                        spot is not None and spot.marker_x >= screen.x,
                        f"{spot.side} at x={spot.marker_x:.0f}, display starts at "
                        f"{screen.x:.0f}" if spot else "nothing")
        else:
            r.skip("placement on a negative-origin display", "no display at negative x")

    with guard(r, "a target in a gap between displays"):
        spot = guidance.place(999999.0, 999999.0, "Click Export")
        r.check("a target on no display is refused rather than guessed at",
                spot is None,
                "there is nothing to point at, and a marker in a gap points at nothing")

    # ── Saying it out loud ──────────────────────────────────────────────────
    r.head("The spoken half")
    with guard(r, "placement can be described"):
        spot = guidance.place(centre[0], centre[1], "Click Export", window)
        said = spot.describe()
        r.check("where Diablo is standing can be said out loud",
                said and said[0].islower() and "," not in said, said)
        for side in ("above", "below", "left", "right", "above right"):
            fake = guidance.Placement(0, 0, 0, 0, side, 0, 0, 0, 0)
            r.check(f"'{side}' has a spoken form",
                    fake.describe() != "next to it", fake.describe())

    return r.report("guidance")


if __name__ == "__main__":
    sys.exit(run())
