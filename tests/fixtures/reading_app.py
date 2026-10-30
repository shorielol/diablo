"""
A window to read, not to click: text at several sizes, an error, a table, a
process diagram, a line chart and a pie chart.

The perception corpus asks where a control is. This asks what the screen
says, which is the other half of what the vision model is for: "what does
this error mean", "which customer owes the most", "is the trend going up".
The traps are deliberate:

    small text          9 pt, the size of a footnote or a status line
    similar numbers     3,918.00 and 3,981.00 in the same column
    a caption           beside Delete and Delete All, saying what each does
    a branch            the diagram splits at one box and only one

Everything is drawn by AppKit, so a capture of it is a real capture of a
real window. Nothing here is interactive except the two Delete buttons,
which do nothing.

    python3 reading_app.py
"""

import math
import os
import sys

from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSBezierPath,
                    NSButton, NSColor, NSFont, NSFontAttributeName,
                    NSForegroundColorAttributeName, NSMakePoint, NSMakeRect, NSString,
                    NSTextField, NSView, NSWindowStyleMaskTitled)

TITLE = "Diablo Reading"
WINDOW = (240.0, 120.0, 980.0, 740.0)

TABLE = [("Customer", "Order", "Qty", "Outstanding"),
         ("Northwind", "#10479", "12", "$1,240.50"),
         ("Acme Ltd", "#10480", "7", "$3,918.00"),
         ("Globex", "#10481", "15", "$3,981.00"),
         ("Initech", "#10482", "9", "$2,045.75"),
         ("Umbrella", "#10483", "11", "$3,189.00"),
         ("Hooli", "#10484", "6", "$980.25")]
LINE = [("Jan", 12), ("Feb", 15), ("Mar", 14), ("Apr", 19), ("May", 23), ("Jun", 27)]
PIE = [("Rent", 50, (0.85, 0.35, 0.30)), ("Salaries", 30, (0.30, 0.55, 0.85)),
       ("Software", 20, (0.40, 0.70, 0.40))]


def draw_text(text, x, y, size=11, colour=None):
    attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(size),
             NSForegroundColorAttributeName: colour or NSColor.blackColor()}
    NSString.stringWithString_(text).drawAtPoint_withAttributes_(NSMakePoint(x, y), attrs)


class Figures(NSView):
    """The diagram and the two charts, drawn rather than laid out."""

    def drawRect_(self, _rect):
        NSColor.blackColor().set()
        # Process diagram: Order received -> Check stock -> Ship / Back-order.
        boxes = {"Order received": (20, 300), "Check stock": (190, 300),
                 "Ship order": (360, 350), "Back-order": (360, 250)}
        for name, (x, y) in boxes.items():
            path = NSBezierPath.bezierPathWithRect_(NSMakeRect(x, y, 130, 40))
            path.setLineWidth_(1.5)
            path.stroke()
            draw_text(name, x + 10, y + 12, 12)
        for (x0, y0, x1, y1) in ((150, 320, 190, 320), (320, 330, 360, 370),
                                 (320, 310, 360, 270)):
            line = NSBezierPath.bezierPath()
            line.moveToPoint_(NSMakePoint(x0, y0))
            line.lineToPoint_(NSMakePoint(x1, y1))
            line.setLineWidth_(1.5)
            line.stroke()
            angle = math.atan2(y1 - y0, x1 - x0)
            head = NSBezierPath.bezierPath()
            head.moveToPoint_(NSMakePoint(x1, y1))
            head.lineToPoint_(NSMakePoint(x1 - 9 * math.cos(angle - 0.4),
                                          y1 - 9 * math.sin(angle - 0.4)))
            head.lineToPoint_(NSMakePoint(x1 - 9 * math.cos(angle + 0.4),
                                          y1 - 9 * math.sin(angle + 0.4)))
            head.closePath()
            head.fill()
        draw_text("In stock", 322, 362, 10)
        draw_text("Out of stock", 300, 262, 10)
        draw_text("Fulfilment process", 20, 400, 14)

        # Line chart.
        ox, oy, width, height = 20, 40, 240, 150
        draw_text("Monthly sign-ups", ox, oy + height + 14, 14)
        axis = NSBezierPath.bezierPath()
        axis.moveToPoint_(NSMakePoint(ox, oy + height))
        axis.lineToPoint_(NSMakePoint(ox, oy))
        axis.lineToPoint_(NSMakePoint(ox + width, oy))
        axis.stroke()
        top = max(v for _, v in LINE)
        points = [(ox + 20 + i * (width - 40) / (len(LINE) - 1),
                   oy + v / top * (height - 20)) for i, (_, v) in enumerate(LINE)]
        trend = NSBezierPath.bezierPath()
        trend.moveToPoint_(NSMakePoint(*points[0]))
        for p in points[1:]:
            trend.lineToPoint_(NSMakePoint(*p))
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.2, 0.4, 0.8, 1).set()
        trend.setLineWidth_(2.5)
        trend.stroke()
        NSColor.blackColor().set()
        for (month, _), (px, _py) in zip(LINE, points):
            draw_text(month, px - 10, oy - 18, 10)

        # Pie chart.
        cx, cy, radius = 400, 120, 70
        draw_text("Monthly costs", cx - 50, cy + radius + 14, 14)
        start = 90.0
        for _name, share, colour in PIE:
            sweep = 360.0 * share / 100.0
            piece = NSBezierPath.bezierPath()
            piece.moveToPoint_(NSMakePoint(cx, cy))
            piece.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                NSMakePoint(cx, cy), radius, start, start - sweep, True)
            piece.closePath()
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour, 1).set()
            piece.fill()
            start -= sweep
        for i, (name, share, colour) in enumerate(PIE):
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*colour, 1).set()
            NSBezierPath.fillRect_(NSMakeRect(cx + 90, cy + 30 - i * 24, 14, 14))
            draw_text(f"{name} {share}%", cx + 110, cy + 29 - i * 24, 11)


def label(ground, text, rect, size=12, bold=False, bezeled=False):
    field = NSTextField.alloc().initWithFrame_(NSMakeRect(*rect))
    field.setStringValue_(text)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setBezeled_(bezeled)
    field.setDrawsBackground_(bezeled)
    if bezeled:
        field.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(1, 0.94, 0.92, 1))
    field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                   else NSFont.systemFontOfSize_(size))
    field.setTextColor_(NSColor.blackColor())
    field.cell().setWraps_(True)
    ground.addSubview_(field)
    return field


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    from AppKit import NSAppearance
    app.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua"))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _layout
    window = _layout.new_window(WINDOW, NSWindowStyleMaskTitled)
    window.setTitle_(TITLE)
    ground = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WINDOW[2], WINDOW[3]))
    ground.setWantsLayer_(True)
    ground.layer().setBackgroundColor_(NSColor.whiteColor().CGColor())
    window.setContentView_(ground)

    # Left column: text.
    label(ground, "Quarterly Review", (20, 680, 420, 34), 24, bold=True)
    label(ground, "Prepared by Ana Ruiz", (22, 660, 300, 18), 10)
    label(ground, "Sync error\nConnection failed.\nError code: 503.\nRetry after 30 seconds.",
          (20, 560, 300, 84), 13, bezeled=True)
    label(ground, "Invoice INV-20931 is due on 14 November.", (20, 530, 420, 14), 9)
    label(ground, ("Operations update. Across the three regions the network carried more "
                   "volume than in any previous quarter. The northern hub cleared its "
                   "backlog in nine days, the coastal depot added a second shift, and the "
                   "warehouse in Pune ships 1,240 units per week after the conveyor upgrade. "
                   "Returns fell slightly, and the carrier contract is due for renewal in "
                   "spring."), (20, 420, 420, 100), 11)
    for title, x in (("Delete", 20), ("Delete All", 130)):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(x, 370, 100, 30))
        button.setTitle_(title)
        ground.addSubview_(button)
    label(ground, "Delete removes only the selected file. Delete All empties the whole folder.",
          (20, 340, 420, 28), 11)

    # Right column: the table.
    widths = (110, 80, 50, 100)
    for row, values in enumerate(TABLE):
        x = 500
        for col, value in enumerate(values):
            label(ground, value, (x, 680 - row * 26, widths[col], 20), 12, bold=row == 0)
            x += widths[col] + 6

    figures = Figures.alloc().initWithFrame_(NSMakeRect(480, 20, 490, 440))
    ground.addSubview_(figures)

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    _layout.emit(window, [])
    print("READY", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
