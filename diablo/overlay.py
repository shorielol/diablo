"""
D.I.A.B.L.O. — the marker that appears on screen.

Run as its own process, always. AppKit needs an NSApplication event loop on the
main thread, and voice_bridge's main thread is already committed to pulling
audio blocks forever. Rather than restructure the service around a UI toolkit,
each point spawns a short-lived process that draws, pulses, fades and exits.
Nothing persists, nothing can leak, and a crash in the drawing code cannot take
the assistant down with it.

The window is borderless, transparent, click-through and non-activating, and it
joins every Space. It never takes focus, so it cannot interrupt what you are
typing into.

Coordinates arrive in Quartz space — origin top-left of the primary display,
which is what CGWindowList and the OCR maths produce. AppKit puts the origin at
the bottom-left instead, so there is exactly one flip, against the primary
display's height. Getting this wrong is invisible on a single screen and
obvious the moment a second display sits above or below the first.

    python3 overlay.py --x 960 --y 797 --label "Continue" --seconds 3
"""

import argparse
import math
import sys

WIDTH, HEIGHT = 420.0, 260.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--x", type=float, required=True, help="Quartz x, top-left origin")
    parser.add_argument("--y", type=float, required=True, help="Quartz y, top-left origin")
    parser.add_argument("--label", default="")
    parser.add_argument("--seconds", type=float, default=3.0)
    args = parser.parse_args()

    try:
        import objc
        from AppKit import (
            NSApp, NSApplication, NSApplicationActivationPolicyAccessory,
            NSBackingStoreBuffered, NSBezierPath, NSColor, NSFont,
            NSFontAttributeName, NSForegroundColorAttributeName,
            NSMakeRect, NSPoint, NSScreen, NSString, NSTimer, NSView, NSWindow,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
        )
    except ImportError as exc:
        print(f"overlay unavailable: {exc}", file=sys.stderr)
        return 1

    screens = NSScreen.screens()
    if not screens:
        return 1
    # The flip is against the PRIMARY display, not the display the point is on.
    primary_height = screens[0].frame().size.height
    centre_x, centre_y = args.x, primary_height - args.y

    class MarkerView(NSView):
        def initWithFrame_(self, frame):
            self = objc.super(MarkerView, self).initWithFrame_(frame)
            if self is None:
                return None
            self.phase = 0.0
            self.opacity = 1.0
            self.label = ""
            return self

        def isOpaque(self):
            return False

        def drawRect_(self, rect):
            NSColor.clearColor().set()
            NSBezierPath.fillRect_(rect)

            cx, cy = WIDTH / 2.0, HEIGHT / 2.0

            # Two rings a half-cycle apart, so there is always one expanding.
            # A single pulse reads as a glitch; two read as deliberate.
            for offset in (0.0, 0.5):
                t = (self.phase + offset) % 1.0
                radius = 10.0 + 34.0 * t
                alpha = (1.0 - t) * 0.55 * self.opacity
                if alpha <= 0.01:
                    continue
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    0.92, 0.22, 0.20, alpha).set()
                ring = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2))
                ring.setLineWidth_(2.5)
                ring.stroke()

            # The solid centre. This is the thing being pointed at.
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.92, 0.22, 0.20, 0.95 * self.opacity).set()
            dot = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - 6, cy - 6, 12, 12))
            dot.fill()
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.9 * self.opacity).set()
            dot.setLineWidth_(2.0)
            dot.stroke()

            if not self.label:
                return

            text = NSString.stringWithString_(self.label)
            attributes = {
                NSFontAttributeName: NSFont.boldSystemFontOfSize_(13.0),
                NSForegroundColorAttributeName:
                    NSColor.colorWithCalibratedWhite_alpha_(1.0, self.opacity),
            }
            size = text.sizeWithAttributes_(attributes)
            pad_x, pad_y = 10.0, 6.0
            # Sit the label below the marker, unless that would fall off the
            # bottom of the overlay, in which case put it above.
            box_y = cy - 58 - size.height
            if box_y < 4:
                box_y = cy + 46
            box = NSMakeRect(cx - size.width / 2 - pad_x, box_y,
                             size.width + pad_x * 2, size.height + pad_y * 2)
            NSColor.colorWithCalibratedWhite_alpha_(0.06, 0.88 * self.opacity).set()
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(box, 7, 7).fill()
            text.drawAtPoint_withAttributes_(
                NSPoint(cx - size.width / 2, box_y + pad_y), attributes)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    frame = NSMakeRect(centre_x - WIDTH / 2, centre_y - HEIGHT / 2, WIDTH, HEIGHT)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setIgnoresMouseEvents_(True)          # never intercept a click
    window.setLevel_(25)                          # above normal windows, below alerts
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary)
    window.setHasShadow_(False)

    view = MarkerView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, HEIGHT))
    view.label = args.label
    window.setContentView_(view)
    window.orderFrontRegardless()                 # show without taking focus

    state = {"elapsed": 0.0}
    step = 1.0 / 30.0
    fade_for = 0.45

    def tick(timer):
        state["elapsed"] += step
        view.phase = (view.phase + step * 0.9) % 1.0
        remaining = args.seconds - state["elapsed"]
        view.opacity = max(0.0, min(1.0, remaining / fade_for)) if remaining < fade_for else 1.0
        view.setNeedsDisplay_(True)
        if state["elapsed"] >= args.seconds:
            NSApp().terminate_(None)

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(step, True, tick)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
