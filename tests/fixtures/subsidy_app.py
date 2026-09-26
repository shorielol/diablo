"""
A subsidy diagram, for teaching a relationship rather than naming a place.

"Where is welfare loss?" has one answer. "Show me how the subsidy creates
welfare loss" has three, in order: where the market cleared before, where it
clears once the subsidy shifts supply, and the triangle between them that the
extra units cost more than they are worth. This fixture draws exactly that,
and publishes every one of those places, so the teaching path can be checked
against rectangles this file chose rather than against another guess.

Nothing is labelled where it is drawn. The areas are named in a legend on the
right, and there is a distractor — a boxed note — for anything that matches
words instead of shapes to land on.

    python3 subsidy_app.py
"""

import json
import os
import sys

from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSBezierPath, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName,
                    NSMakePoint, NSMakeRect, NSString, NSView, NSWindow,
                    NSWindowStyleMaskTitled)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layout  # noqa: E402

TITLE = "Subsidy and Welfare"

PLOT = (90.0, 90.0, 530.0, 430.0)
DEMAND = ((90.0, 500.0), (620.0, 120.0))
SUPPLY = ((90.0, 120.0), (620.0, 500.0))
SUBSIDISED = ((174.0, 90.0), (620.0, 410.0))       # supply, shifted down
BEFORE = (355.0, 310.0)                             # D meets S
AFTER = (418.0, 265.0)                              # D meets S + subsidy
ON_SUPPLY = (418.0, 355.0)                          # S at the new quantity

CONSUMER = [(90.0, 500.0), BEFORE, (90.0, 310.0)]
PRODUCER = [(90.0, 120.0), BEFORE, (90.0, 310.0)]
WELFARE_LOSS = [BEFORE, ON_SUPPLY, AFTER]

LEGEND = [("Consumer surplus", 380.0, (0.28, 0.50, 0.80, 0.40)),
          ("Producer surplus", 350.0, (0.28, 0.64, 0.42, 0.40)),
          ("Welfare loss", 320.0, (0.42, 0.42, 0.46, 0.62)),
          ("Subsidy", 290.0, (0.85, 0.52, 0.10, 1.0))]
LEGEND_X = 668.0


def _bounds(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return NSMakeRect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


class Subsidy(NSView):

    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        NSColor.whiteColor().set()
        NSBezierPath.bezierPathWithRect_(self.bounds()).fill()

        for points, colour in ((CONSUMER, LEGEND[0][2]), (PRODUCER, LEGEND[1][2]),
                               (WELFARE_LOSS, LEGEND[2][2])):
            path = NSBezierPath.bezierPath()
            path.moveToPoint_(NSMakePoint(*points[0]))
            for point in points[1:]:
                path.lineToPoint_(NSMakePoint(*point))
            path.closePath()
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour).set()
            path.fill()

        x, y, w, h = PLOT
        NSColor.blackColor().set()
        axes = NSBezierPath.bezierPath()
        axes.setLineWidth_(1.6)
        axes.moveToPoint_(NSMakePoint(x, y + h))
        axes.lineToPoint_(NSMakePoint(x, y))
        axes.lineToPoint_(NSMakePoint(x + w, y))
        axes.stroke()

        for (start, end), colour, dashed in (
                (DEMAND, (0.80, 0.20, 0.20), False),
                (SUPPLY, (0.18, 0.38, 0.72), False),
                (SUBSIDISED, (0.18, 0.38, 0.72), True)):
            line = NSBezierPath.bezierPath()
            line.setLineWidth_(2.6)
            if dashed:
                line.setLineDash_count_phase_([8.0, 5.0], 2, 0.0)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour, 1.0).set()
            line.moveToPoint_(NSMakePoint(*start))
            line.lineToPoint_(NSMakePoint(*end))
            line.stroke()

        # The subsidy: the gap between the two supply curves at the new
        # quantity, drawn as a bracket beside it rather than a filled area.
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.85, 0.52, 0.10, 1.0).set()
        bracket = NSBezierPath.bezierPath()
        bracket.setLineWidth_(2.4)
        bracket.moveToPoint_(NSMakePoint(AFTER[0] + 6.0, AFTER[1]))
        bracket.lineToPoint_(NSMakePoint(AFTER[0] + 16.0, AFTER[1]))
        bracket.lineToPoint_(NSMakePoint(AFTER[0] + 16.0, ON_SUPPLY[1]))
        bracket.lineToPoint_(NSMakePoint(AFTER[0] + 6.0, ON_SUPPLY[1]))
        bracket.stroke()

        for point in (BEFORE, AFTER):
            NSColor.blackColor().set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(point[0] - 4.5, point[1] - 4.5, 9.0, 9.0)).fill()

        def write(text, at, size=13.0, colour=(0.1, 0.1, 0.1), bold=False):
            font = (NSFont.boldSystemFontOfSize_(size) if bold
                    else NSFont.systemFontOfSize_(size))
            NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                NSMakePoint(*at),
                {NSFontAttributeName: font,
                 NSForegroundColorAttributeName:
                     NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour, 1.0)})

        write(TITLE, (90.0, 548.0), size=19.0, bold=True)
        write("Price", (36.0, 530.0))
        write("Quantity", (530.0, 58.0))
        write("D", (628.0, 112.0), colour=(0.80, 0.20, 0.20), bold=True)
        write("S", (628.0, 492.0), colour=(0.18, 0.38, 0.72), bold=True)
        write("S + subsidy", (628.0, 402.0), colour=(0.18, 0.38, 0.72))

        for text, at_y, colour in LEGEND:
            swatch = NSBezierPath.bezierPathWithRect_(
                NSMakeRect(LEGEND_X - 24.0, at_y - 1.0, 16.0, 16.0))
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour).set()
            swatch.fill()
            write(text, (LEGEND_X, at_y), size=14.0)

        # A distractor: words that match nothing on the diagram's shapes.
        NSColor.colorWithCalibratedWhite_alpha_(0.55, 1.0).set()
        note = NSBezierPath.bezierPathWithRect_(NSMakeRect(646.0, 150.0, 200.0, 54.0))
        note.setLineWidth_(1.0)
        note.stroke()
        write("Note: the subsidy is paid", (656.0, 180.0), size=12.0,
              colour=(0.4, 0.4, 0.4))
        write("per unit, not to scale", (656.0, 162.0), size=12.0,
              colour=(0.4, 0.4, 0.4))


def _origin(x: float, y: float):
    """`FIXTURE_ORIGIN="x,y"` places the window without touching the pointer."""
    raw = os.getenv("FIXTURE_ORIGIN", "")
    if raw:
        try:
            left, bottom = [float(v) for v in raw.split(",")[:2]]
            return (left, bottom)
        except ValueError:
            pass
    return (x, y)


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(*_origin(360.0, 240.0), 900, 600), NSWindowStyleMaskTitled,
        NSBackingStoreBuffered, False)
    window.setTitle_(TITLE)
    view = Subsidy.alloc().initWithFrame_(NSMakeRect(0, 0, 900, 600))
    window.contentView().addSubview_(view)

    def box(rect):
        return _layout.rect_in(window, view, rect)

    def around(point, half=20.0):
        return NSMakeRect(point[0] - half, point[1] - half, half * 2.0, half * 2.0)

    regions = [
        {"name": "original equilibrium", **box(around(BEFORE))},
        {"name": "subsidised equilibrium", **box(around(AFTER))},
        {"name": "subsidy wedge",
         **box(NSMakeRect(AFTER[0] + 2.0, AFTER[1] - 3.0, 28.0,
                          ON_SUPPLY[1] - AFTER[1] + 6.0))},
        {"name": "welfare loss", **box(_bounds(WELFARE_LOSS))},
        {"name": "consumer surplus", **box(_bounds(CONSUMER))},
        {"name": "producer surplus", **box(_bounds(PRODUCER))},
    ]
    labels = [{"text": text, **box(NSMakeRect(LEGEND_X, at_y - 3.0, 140.0, 20.0))}
              for text, at_y, _ in LEGEND]
    labels.append({"text": "Note: the subsidy is paid",
                   **box(NSMakeRect(656.0, 177.0, 180.0, 18.0))})
    print("LAYOUT " + json.dumps({"title": TITLE, "controls": [],
                                  "regions": regions, "labels": labels,
                                  "figure": box(NSMakeRect(*PLOT))}), flush=True)
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    print("READY", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
