"""
A second economics diagram, deliberately unlike the first.

`diagram_app.py` has two shaded shapes, both named in one legend on the far
side of the window. If the only thing Phase B.1 proves is that grounding works
on that picture, it has proved nothing about grounding.

So this one varies every axis that could be doing the work by accident:

  a region with NO label anywhere        consumer surplus, blue, unnamed
  a region whose label sits beside it    producer surplus, named in place
  a region whose label is a legend       deadweight loss, named across the page
  two regions that look almost alike     the two surplus triangles mirror
  a line rather than an area             the demand curve
  a crossing rather than a shape         the equilibrium point

The two surplus triangles are the trap worth naming: they are the same size,
the same shape and share an edge, so a model that is pattern-matching on
"triangle near the crossing" rather than on colour and position will pick the
wrong one and the score will say so.

    python3 market_app.py
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

TITLE = "Tyre Market Surplus"

PLOT = (90.0, 90.0, 530.0, 430.0)
CROSS = (355.0, 310.0)

CONSUMER = [(90.0, 500.0), CROSS, (90.0, 310.0)]          # blue, never named
PRODUCER = [(90.0, 120.0), CROSS, (90.0, 310.0)]          # green, named beside it
DEADWEIGHT = [CROSS, (265.0, 382.0), (265.0, 238.0)]      # grey, named in the legend

DEMAND = ((90.0, 500.0), (620.0, 120.0))
SUPPLY = ((90.0, 120.0), (620.0, 500.0))

# The only two pieces of text that name a region. One sits beside its shape,
# one sits in a legend 300 points away.
NEARBY = ("Producer surplus", (372.0, 206.0))
LEGEND = ("Deadweight loss", (664.0, 300.0))


def _bounds(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return NSMakeRect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


class Market(NSView):

    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        NSColor.whiteColor().set()
        NSBezierPath.bezierPathWithRect_(self.bounds()).fill()

        for points, colour in ((CONSUMER, (0.28, 0.50, 0.80, 0.34)),
                               (PRODUCER, (0.28, 0.64, 0.42, 0.34)),
                               (DEADWEIGHT, (0.42, 0.42, 0.46, 0.58))):
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

        for (start, end), colour in ((DEMAND, (0.80, 0.20, 0.20)),
                                     (SUPPLY, (0.18, 0.38, 0.72))):
            line = NSBezierPath.bezierPath()
            line.setLineWidth_(2.6)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour, 1.0).set()
            line.moveToPoint_(NSMakePoint(*start))
            line.lineToPoint_(NSMakePoint(*end))
            line.stroke()

        def write(text, at, size=13.0, colour=(0.1, 0.1, 0.1), bold=False):
            font = (NSFont.boldSystemFontOfSize_(size) if bold
                    else NSFont.systemFontOfSize_(size))
            NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                NSMakePoint(*at),
                {NSFontAttributeName: font,
                 NSForegroundColorAttributeName:
                     NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour, 1.0)})

        write("Tyre Market Surplus", (90.0, 548.0), size=19.0, bold=True)
        write("Price", (36.0, 530.0))
        write("Quantity", (530.0, 58.0))
        write("D", (628.0, 112.0), colour=(0.80, 0.20, 0.20), bold=True)
        write("S", (628.0, 492.0), colour=(0.18, 0.38, 0.72), bold=True)
        write(NEARBY[0], NEARBY[1], size=13.0, colour=(0.16, 0.42, 0.26))

        swatch = NSBezierPath.bezierPathWithRect_(
            NSMakeRect(LEGEND[1][0] - 24.0, LEGEND[1][1] - 1.0, 16.0, 16.0))
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.42, 0.42, 0.46, 0.7).set()
        swatch.fill()
        write(LEGEND[0], LEGEND[1], size=14.0)


def _origin(x: float, y: float):
    """
    Where to put the window, so a harness can place a known region under the
    pointer without ever touching the pointer.

    `FIXTURE_ORIGIN="x,y"` in AppKit screen points (bottom-left). Moving the
    window is the only safe way to test a cursor-local question: moving the
    cursor would mean posting a synthetic event, which this build is not
    allowed to do and which Diablo must never do either.
    """
    import os as _os
    raw = _os.getenv("FIXTURE_ORIGIN", "")
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
        NSMakeRect(*_origin(380.0, 260.0), 900, 600), NSWindowStyleMaskTitled,
        NSBackingStoreBuffered, False)
    window.setTitle_(TITLE)
    view = Market.alloc().initWithFrame_(NSMakeRect(0, 0, 900, 600))
    window.contentView().addSubview_(view)

    def box(rect):
        return _layout.rect_in(window, view, rect)

    regions = [
        {"name": "consumer surplus", "labelled": False, **box(_bounds(CONSUMER))},
        {"name": "producer surplus", "labelled": "near", **box(_bounds(PRODUCER))},
        {"name": "deadweight loss", "labelled": "far", **box(_bounds(DEADWEIGHT))},
        # A line, not an area: the box is the line's own extent.
        {"name": "demand curve", "labelled": False,
         **box(NSMakeRect(min(DEMAND[0][0], DEMAND[1][0]),
                          min(DEMAND[0][1], DEMAND[1][1]),
                          abs(DEMAND[1][0] - DEMAND[0][0]),
                          abs(DEMAND[1][1] - DEMAND[0][1])))},
        # A crossing, not a shape: a small box around the point itself.
        {"name": "equilibrium", "labelled": False,
         **box(NSMakeRect(CROSS[0] - 36.0, CROSS[1] - 36.0, 72.0, 72.0))},
    ]
    labels = [
        {"text": NEARBY[0], **box(NSMakeRect(NEARBY[1][0], NEARBY[1][1] - 3.0,
                                             120.0, 18.0))},
        {"text": LEGEND[0], **box(NSMakeRect(LEGEND[1][0], LEGEND[1][1] - 3.0,
                                             130.0, 20.0))},
    ]
    figure = box(NSMakeRect(*PLOT))
    print("LAYOUT " + json.dumps({"title": TITLE, "controls": [],
                                  "regions": regions, "labels": labels,
                                  "figure": figure}), flush=True)

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    print("READY", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
