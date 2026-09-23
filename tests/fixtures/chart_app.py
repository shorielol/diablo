"""
A bar chart, because not every visual question is about a shaded triangle.

Three things here that no diagram fixture covers: a chart segment (one bar
among five that look alike), a highlighted area (the one bar drawn in a
different colour), and an annotation (an arrow with its caption at the tail
rather than at the head).

The traps are the point of the layout:

  every bar looks like every other bar, so "the third bar" can only be
  answered by counting, not by recognising;

  the caption "Peak quarter" sits beside the arrow's TAIL, 170 points from
  the bar it is about, so matching those words lands nowhere near the answer;

  each bar has its own small label underneath, so there is always a nearby
  piece of text that is not the thing being asked for.

    python3 chart_app.py
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

TITLE = "Quarterly Revenue"

BASE = 130.0
BAR_W = 78.0
GAP = 34.0
FIRST = 130.0
HEIGHTS = [118.0, 196.0, 154.0, 312.0, 176.0]
NAMES = ["Q1", "Q2", "Q3", "Q4", "Q5"]
HIGHLIGHT = 3                      # zero-based: Q4, the tall one

ARROW_TAIL = (700.0, 470.0)
ARROW_HEAD = (560.0, 452.0)
CAPTION = ("Peak quarter", (706.0, 462.0))


def bar_rect(index):
    return NSMakeRect(FIRST + index * (BAR_W + GAP), BASE, BAR_W, HEIGHTS[index])


class Chart(NSView):

    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        NSColor.whiteColor().set()
        NSBezierPath.bezierPathWithRect_(self.bounds()).fill()

        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.7, 0.7, 0.74, 1.0).set()
        axis = NSBezierPath.bezierPath()
        axis.setLineWidth_(1.4)
        axis.moveToPoint_(NSMakePoint(96.0, BASE))
        axis.lineToPoint_(NSMakePoint(770.0, BASE))
        axis.stroke()

        for index in range(len(HEIGHTS)):
            colour = ((0.93, 0.62, 0.16, 1.0) if index == HIGHLIGHT
                      else (0.42, 0.47, 0.56, 1.0))
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour).set()
            NSBezierPath.bezierPathWithRect_(bar_rect(index)).fill()

        def write(text, at, size=13.0, colour=(0.12, 0.12, 0.14), bold=False):
            font = (NSFont.boldSystemFontOfSize_(size) if bold
                    else NSFont.systemFontOfSize_(size))
            NSString.stringWithString_(text).drawAtPoint_withAttributes_(
                NSMakePoint(*at),
                {NSFontAttributeName: font,
                 NSForegroundColorAttributeName:
                     NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour, 1.0)})

        write(TITLE, (96.0, 548.0), size=19.0, bold=True)
        for index, name in enumerate(NAMES):
            r = bar_rect(index)
            write(name, (r.origin.x + 26.0, BASE - 26.0))

        # The arrow: a shaft and a filled head, pointing at the tall bar.
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.15, 0.18, 1.0).set()
        shaft = NSBezierPath.bezierPath()
        shaft.setLineWidth_(2.4)
        shaft.moveToPoint_(NSMakePoint(*ARROW_TAIL))
        shaft.lineToPoint_(NSMakePoint(ARROW_HEAD[0] + 16.0, ARROW_HEAD[1] + 2.0))
        shaft.stroke()
        head = NSBezierPath.bezierPath()
        head.moveToPoint_(NSMakePoint(*ARROW_HEAD))
        head.lineToPoint_(NSMakePoint(ARROW_HEAD[0] + 20.0, ARROW_HEAD[1] + 9.0))
        head.lineToPoint_(NSMakePoint(ARROW_HEAD[0] + 19.0, ARROW_HEAD[1] - 7.0))
        head.closePath()
        head.fill()
        write(CAPTION[0], CAPTION[1], size=14.0)


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
        NSMakeRect(*_origin(340.0, 200.0), 880, 600), NSWindowStyleMaskTitled,
        NSBackingStoreBuffered, False)
    window.setTitle_(TITLE)
    view = Chart.alloc().initWithFrame_(NSMakeRect(0, 0, 880, 600))
    window.contentView().addSubview_(view)

    def box(rect):
        return _layout.rect_in(window, view, rect)

    arrow = NSMakeRect(min(ARROW_HEAD[0], ARROW_TAIL[0]) - 2.0,
                       min(ARROW_HEAD[1], ARROW_TAIL[1]) - 9.0,
                       abs(ARROW_TAIL[0] - ARROW_HEAD[0]) + 4.0,
                       abs(ARROW_TAIL[1] - ARROW_HEAD[1]) + 20.0)
    regions = [
        {"name": "highlighted bar", "labelled": "far", **box(bar_rect(HIGHLIGHT))},
        {"name": "tallest bar", "labelled": "far", **box(bar_rect(HIGHLIGHT))},
        {"name": "third bar", "labelled": "near", **box(bar_rect(2))},
        {"name": "arrow", "labelled": "near", **box(arrow)},
    ]
    labels = [{"text": CAPTION[0],
               **box(NSMakeRect(CAPTION[1][0], CAPTION[1][1] - 3.0, 120.0, 20.0))}]
    for index, name in enumerate(NAMES):
        r = bar_rect(index)
        labels.append({"text": name,
                       **box(NSMakeRect(r.origin.x + 26.0, BASE - 29.0, 26.0, 18.0))})
    figure = box(NSMakeRect(96.0, BASE - 30.0, 680.0, 400.0))

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
