"""
A real supply-and-demand diagram, for the one case that broke.

The other fixtures publish controls. This one deliberately publishes none: it
is a drawn figure, the way an economics diagram in a PDF or a lecture slide is
a drawn figure, and the Accessibility API can say nothing whatever about what
is inside it. That is not a shortcoming of the fixture — it is the situation
that made "where is the welfare loss region?" unanswerable by the old path.

Two shapes are shaded and neither has any text inside it. Both are named, once
each, in a legend in the bottom-right corner, as far from the shapes as the
window allows. So a system that answers by matching the user's words against
printed words lands on the legend every time, and lands somewhere measurably
wrong every time.

Ground truth is printed as JSON before READY, using the same conversion every
other fixture uses, so a test can check a returned rectangle against the
rectangle this file chose rather than against another guess.

    python3 diagram_app.py
"""

import os
import sys

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSBezierPath, NSColor, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName,
                    NSMakePoint, NSMakeRect, NSString, NSView, NSWindow,
                    NSWindowStyleMaskTitled)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layout  # noqa: E402

TITLE = "Market for Tyres"

# The plot, in the drawing view's own coordinates (bottom-left origin).
PLOT = (80.0, 80.0, 520.0, 440.0)

# The two shaded shapes. Neither carries any text.
WELFARE_LOSS = [(340.0, 310.0), (256.0, 382.0), (256.0, 238.0)]
CONSUMER_SURPLUS = [(80.0, 500.0), (340.0, 310.0), (80.0, 310.0)]

# The legend, in a clear strip to the right of the plot with nothing else near
# it. Kept well away from the axis labels as well as from the shapes: two
# pieces of small type a few points apart are read by OCR as one mangled
# fragment, and a fixture whose trap only works when OCR fails is no fixture.
LEGEND = [("Welfare loss", 318.0, (0.45, 0.45, 0.48)),
          ("Consumer surplus", 272.0, (0.32, 0.52, 0.78))]
LEGEND_X = 630.0


def _bounds(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return NSMakeRect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


class Diagram(NSView):

    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        NSColor.whiteColor().set()
        NSBezierPath.bezierPathWithRect_(self.bounds()).fill()

        x, y, w, h = PLOT

        # The two shaded regions, drawn before the lines so the curves sit on
        # top of them the way they do on a real diagram.
        for points, colour in ((CONSUMER_SURPLUS, (0.32, 0.52, 0.78, 0.30)),
                               (WELFARE_LOSS, (0.45, 0.45, 0.48, 0.55))):
            path = NSBezierPath.bezierPath()
            path.moveToPoint_(NSMakePoint(*points[0]))
            for point in points[1:]:
                path.lineToPoint_(NSMakePoint(*point))
            path.closePath()
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour).set()
            path.fill()

        # Axes.
        NSColor.blackColor().set()
        axes = NSBezierPath.bezierPath()
        axes.setLineWidth_(1.6)
        axes.moveToPoint_(NSMakePoint(x, y + h))
        axes.lineToPoint_(NSMakePoint(x, y))
        axes.lineToPoint_(NSMakePoint(x + w, y))
        axes.stroke()

        # Demand falls, supply rises, and they cross where the shapes meet.
        for start, end, colour in (
                ((x, y + 420.0), (x + w, y + 40.0), (0.78, 0.22, 0.22)),
                ((x, y + 40.0), (x + w, y + 420.0), (0.20, 0.40, 0.72))):
            line = NSBezierPath.bezierPath()
            line.setLineWidth_(2.4)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour, 1.0).set()
            line.moveToPoint_(NSMakePoint(*start))
            line.lineToPoint_(NSMakePoint(*end))
            line.stroke()

        def write(text, at, size=13.0, colour=(0.1, 0.1, 0.1), bold=False):
            font = (NSFont.boldSystemFontOfSize_(size) if bold
                    else NSFont.systemFontOfSize_(size))
            attrs = {NSFontAttributeName: font,
                     NSForegroundColorAttributeName:
                         NSColor.colorWithCalibratedRed_green_blue_alpha_(
                             *colour, 1.0)}
            NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                NSMakePoint(*at), attrs)

        write("Market for Tyres", (80.0, 548.0), size=19.0, bold=True)
        write("Price", (30.0, 530.0))
        write("Quantity", (500.0, 50.0))
        write("D", (606.0, 112.0), colour=(0.78, 0.22, 0.22), bold=True)
        write("S", (606.0, 492.0), colour=(0.20, 0.40, 0.72), bold=True)

        # The legend. The only place either shape is named.
        for text, at_y, colour in LEGEND:
            swatch = NSBezierPath.bezierPathWithRect_(
                NSMakeRect(LEGEND_X, at_y, 16.0, 16.0))
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour, 0.7).set()
            swatch.fill()
            write(text, (LEGEND_X + 24.0, at_y - 1.0), size=14.0)


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

    window = _layout.new_window((*_origin(420.0, 220.0), 900, 600), NSWindowStyleMaskTitled)
    window.setTitle_(TITLE)

    view = Diagram.alloc().initWithFrame_(NSMakeRect(0, 0, 900, 600))
    window.contentView().addSubview_(view)

    regions = [
        {"name": "welfare loss",
         **_layout.rect_in(window, view, _bounds(WELFARE_LOSS))},
        {"name": "consumer surplus",
         **_layout.rect_in(window, view, _bounds(CONSUMER_SURPLUS))},
    ]
    labels = [
        {"text": text,
         **_layout.rect_in(window, view,
                           NSMakeRect(LEGEND_X + 24.0, at_y - 3.0, 140.0, 20.0))}
        for text, at_y, _ in LEGEND
    ]
    figure = _layout.rect_in(window, view,
                             NSMakeRect(PLOT[0], PLOT[1], PLOT[2], PLOT[3]))

    import json
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
