"""
D.I.A.B.L.O. — the guide he projects into your application.

Run as its own process, always. AppKit needs an NSApplication event loop on the
main thread, and voice_bridge's main thread is already committed to pulling
audio blocks forever. Rather than restructure the service around a UI toolkit,
a walkthrough spawns a short-lived process that draws, moves, fades and exits.
Nothing persists, nothing can leak, and a crash in the drawing code cannot take
the assistant down with it.

What is drawn
-------------
Three things, in one window, because they are one gesture:

    a ring          on the target, thin, with the control still readable
                    through the middle of it
    a connector     from Diablo toward the ring, fading out before it arrives
    Diablo          standing to one side, with the instruction beside him

He never stands on the thing he is pointing at. Where he stands is arithmetic
over display geometry, the window being explained and the user's pointer, and
it lives in guidance.py where it can be tested. This file draws what that
decides.

The face is make_icon.draw_head — the same drawing as the app icon and the orb
in the corner, at a different size. Three renderings of one character stay one
character only if there is one drawing.

Coordinates arrive in Quartz space, origin top-left of the primary display,
which is what CGWindowList and the OCR maths produce. AppKit puts the origin at
the bottom-left instead, so there is exactly one flip, against the primary
display's height. Getting this wrong is invisible on a single screen and
obvious the moment a second display sits above or below the first.

    python3 overlay.py --x 960 --y 797 --label "Click Continue" --seconds 3

With --session it stays up instead and takes instructions on stdin, one JSON
object per line, so a walkthrough can move one guide from step to step:

    {"x": 960, "y": 797, "label": "Click Continue", "step": "2 of 5"}
    {"quit": true}

Respawning a process per step was the obvious thing and it looks wrong: the
guide blinks out and back, which reads as two separate pointers rather than
one thing moving, and the spawn costs about 180ms of nothing on screen.
"""

import argparse
import json
import math
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import guidance  # noqa: E402

# The crimson. One accent, used for the ring, the connector and his streak,
# and for nothing else.
ACCENT = (0.85, 0.16, 0.18)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--x", type=float, required=True, help="Quartz x of the TARGET")
    parser.add_argument("--y", type=float, required=True, help="Quartz y of the TARGET")
    parser.add_argument("--label", default="")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--step", default="", help="e.g. '2 of 5'")
    parser.add_argument("--session", action="store_true",
                        help="stay up and take moves on stdin")
    parser.add_argument("--on-light", action="store_true",
                        help="the screen behind this point is light")
    parser.add_argument("--watch-clicks", action="store_true",
                        help="report every click on stdout as JSON")
    args = parser.parse_args()

    try:
        import objc
        from AppKit import (
            NSApp, NSApplication, NSApplicationActivationPolicyAccessory,
            NSAffineTransform, NSBackingStoreBuffered, NSBezierPath, NSColor,
            NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
            NSGraphicsContext, NSMakeRect, NSPoint, NSScreen, NSString, NSTimer,
            NSView, NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
        )
    except ImportError as exc:
        print(f"overlay unavailable: {exc}", file=sys.stderr)
        return 1

    try:
        from skills import make_icon
    except Exception:
        make_icon = None

    screens = NSScreen.screens()
    if not screens:
        return 1
    # The flip is against the PRIMARY display, not the display the point is on.
    primary_height = screens[0].frame().size.height

    def _rgb(rgb, alpha):
        return NSColor.colorWithCalibratedRed_green_blue_alpha_(
            rgb[0], rgb[1], rgb[2], alpha)

    # The drawing helpers are plain functions, not methods.
    #
    # PyObjC maps every method on an NSView subclass onto an Objective-C
    # selector, and a Python name with no underscores maps to a selector taking
    # no arguments. `def _ring(self, centre)` therefore fails to build the
    # class at all — "_ring expects 0 arguments, has 1". Keeping the drawing
    # out here sidesteps the selector system entirely and costs nothing.

    def view_point(view, qx, qy):
        """A Quartz point in the view's own coordinates."""
        return (qx - view.origin[0],
                view.frame().size.height - (qy - view.origin[1]))

    def draw_ring(view, centre):
        """
        Thin, and open in the middle. The control has to stay readable — a
        filled dot on a button is the assistant standing in front of the thing
        it is naming.

        One pulse travelling outward, not two. Two rings read as a warning
        badge; one reads as attention.
        """
        cx, cy = centre
        radius = guidance.RING

        spread = view.phase % 1.0
        grow = radius + 2.0 + spread * 16.0
        fade = (1.0 - spread) * 0.42 * view.opacity
        if fade > 0.01:
            _rgb(ACCENT, fade).set()
            pulse = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - grow, cy - grow, grow * 2, grow * 2))
            pulse.setLineWidth_(1.6)
            pulse.stroke()

        _rgb(ACCENT, 0.95 * view.opacity).set()
        ring = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2))
        ring.setLineWidth_(2.2)
        ring.stroke()

        # A hairline of the opposite value just inside it, so the ring survives
        # landing on a surface its own colour.
        NSColor.colorWithCalibratedWhite_alpha_(
            0.0 if view.on_light else 1.0, 0.22 * view.opacity).set()
        inner = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - radius + 2, cy - radius + 2,
                       (radius - 2) * 2, (radius - 2) * 2))
        inner.setLineWidth_(1.0)
        inner.stroke()

    def draw_connector(view, start, end):
        """
        A leader from Diablo toward the ring, stopping short of it.

        Not an arrow. An arrowhead at this size is three pixels of clutter that
        says what the geometry already says, and a big one is the cartoon
        pointer the brief rules out. The line thins and fades as it approaches,
        which reads as direction on its own.
        """
        sx, sy = start
        ex, ey = end
        dx, dy = ex - sx, ey - sy
        length = math.hypot(dx, dy)
        if length < 1.0:
            return
        ux, uy = dx / length, dy / length

        begin = guidance.MARKER + 5.0
        finish = length - (guidance.RING + 10.0)
        if finish <= begin:
            return

        segments = 18
        for i in range(segments):
            t0 = begin + (finish - begin) * (i / segments)
            t1 = begin + (finish - begin) * ((i + 1) / segments)
            along = i / max(1, segments - 1)
            alpha = (0.72 - 0.58 * along) * view.opacity
            width = 2.8 - 1.7 * along
            if alpha <= 0.02:
                continue
            _rgb(ACCENT, alpha).set()
            piece = NSBezierPath.bezierPath()
            piece.moveToPoint_(NSPoint(sx + ux * t0, sy + uy * t0))
            piece.lineToPoint_(NSPoint(sx + ux * t1, sy + uy * t1))
            piece.setLineWidth_(width)
            piece.setLineCapStyle_(1)
            piece.stroke()

    def draw_marker(view, centre):
        """
        Him, small, in a dark disc. The same face as the orb and the app icon.

        Deliberately not a dot: a dot beside a ring is two shapes with no
        relationship, and the point of the guide is that it is *Diablo* showing
        you, not a generic annotation layer.
        """
        cx, cy = centre
        radius = guidance.MARKER
        context = NSGraphicsContext.currentContext()

        NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.28 * view.opacity).set()
        NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - radius, cy - radius - 1.5, radius * 2, radius * 2)).fill()

        disc = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2))
        NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.075, 0.075, 0.09, 0.98 * view.opacity).set()
        disc.fill()

        if make_icon is not None:
            context.saveGraphicsState()
            disc.addClip()
            # Framed with room around him, and centred on the face rather than
            # on the 120-unit canvas. At 1.34 his jaw was clipped by the disc,
            # which at this size reads as a smudge rather than as a character.
            # make_icon draws the face ellipse centred at (60, 54), so putting
            # mascot y=52 on the disc's centre is what actually centres him.
            scale = radius * 2 * 1.12 / 120.0
            transform = NSAffineTransform.transform()
            transform.translateXBy_yBy_(cx - 60 * scale, cy + 52 * scale)
            transform.scaleXBy_yBy_(scale, -scale)
            transform.concat()
            try:
                make_icon.draw_head(detail=False)
            except Exception:
                pass
            context.restoreGraphicsState()

        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.16 * view.opacity).set()
        disc.setLineWidth_(1.0)
        disc.stroke()

    def draw_caption(view, marker, spot):
        """
        The instruction, beside him, never over the target.

        One weight, one size. The step counter is the only secondary thing and
        it is smaller and quieter rather than a different colour.
        """
        ink = 0.08 if view.on_light else 0.97
        paper = 0.98 if view.on_light else 0.08
        fade = view.opacity * min(1.0, max(0.0, (view.travel - 0.55) / 0.25))
        if fade <= 0.01:
            return

        text = NSString.stringWithString_(view.label)
        attributes = {
            NSFontAttributeName: NSFont.systemFontOfSize_(13.0),
            NSForegroundColorAttributeName:
                NSColor.colorWithCalibratedWhite_alpha_(ink, fade),
        }
        size = text.sizeWithAttributes_(attributes)

        pad_x, pad_y = 12.0, 8.0
        counter_height = 14.0 if view.step else 0.0
        box_w = size.width + pad_x * 2
        box_h = size.height + pad_y * 2 + counter_height

        # Anchored to the side guidance.py chose, so the caption sits where the
        # placement search already proved there was room for it.
        cx, cy = marker
        if spot.side.startswith("above"):
            box = NSMakeRect(cx - box_w / 2, cy + guidance.MARKER + 8, box_w, box_h)
        elif spot.side.startswith("below"):
            box = NSMakeRect(cx - box_w / 2, cy - guidance.MARKER - 8 - box_h, box_w, box_h)
        elif spot.side == "right":
            box = NSMakeRect(cx + guidance.MARKER + 9, cy - box_h / 2, box_w, box_h)
        else:
            box = NSMakeRect(cx - guidance.MARKER - 9 - box_w, cy - box_h / 2, box_w, box_h)

        NSColor.colorWithCalibratedWhite_alpha_(paper, 0.94 * fade).set()
        rounded = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(box, 8, 8)
        rounded.fill()
        NSColor.colorWithCalibratedWhite_alpha_(ink, 0.16 * fade).set()
        rounded.setLineWidth_(1.0)
        rounded.stroke()

        top = box.origin.y + box.size.height - pad_y - size.height
        text.drawAtPoint_withAttributes_(
            NSPoint(box.origin.x + pad_x, top), attributes)

        if view.step:
            counter = NSString.stringWithString_(view.step)
            counter.drawAtPoint_withAttributes_(
                NSPoint(box.origin.x + pad_x, box.origin.y + pad_y - 2),
                {NSFontAttributeName: NSFont.systemFontOfSize_(10.0),
                 NSForegroundColorAttributeName:
                     NSColor.colorWithCalibratedWhite_alpha_(ink, 0.5 * fade)})

    class GuideView(NSView):
        def initWithFrame_(self, frame):
            self = objc.super(GuideView, self).initWithFrame_(frame)
            if self is None:
                return None
            self.phase = 0.0
            self.opacity = 1.0
            self.label = ""
            self.step = ""
            self.place = None            # a guidance.Placement
            self.origin = (0.0, 0.0)     # Quartz top-left of this window
            # What is behind the caption. A white label on a white document is
            # invisible, and so is a dark one on a dark IDE — the caption has
            # to be the opposite of whatever it is sitting on, not a fixed
            # colour that happens to suit half of them.
            self.on_light = False
            # Counts up from 0 when a new target arrives, so the guide slides
            # rather than teleporting. Spatial continuity is the whole reason
            # one process holds the session.
            self.travel = 1.0
            self.was = None              # the previous marker point, Quartz
            return self

        def isOpaque(self):
            return False

        def drawRect_(self, rect):
            NSColor.clearColor().set()
            NSBezierPath.fillRect_(rect)
            if self.place is None:
                return

            spot = self.place
            target = view_point(self, spot.target_x, spot.target_y)
            # Where he is this instant: eased from the last target to this one.
            if self.was and self.travel < 1.0:
                ease = 1.0 - pow(1.0 - self.travel, 3.0)      # out-cubic
                mx = self.was[0] + (spot.marker_x - self.was[0]) * ease
                my = self.was[1] + (spot.marker_y - self.was[1]) * ease
            else:
                mx, my = spot.marker_x, spot.marker_y
            marker = view_point(self, mx, my)

            draw_ring(self, target)
            draw_connector(self, marker, target)
            draw_marker(self, marker)
            if self.label:
                draw_caption(self, marker, spot)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 10, 10), NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered, False)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setIgnoresMouseEvents_(True)          # never intercept a click
    window.setLevel_(25)                          # above normal windows, below alerts
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary)
    window.setHasShadow_(False)

    view = GuideView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
    window.setContentView_(view)

    def aim(x, y, label, step, on_light, animate=False):
        """Point at a new target: re-place, resize, and slide him over."""
        spot = guidance.place(x, y, label, window=None, step=step)
        if spot is None:
            return False
        bx, by, bw, bh = spot.bounds()
        previous = (view.place.marker_x, view.place.marker_y) if view.place else None
        window.setFrame_display_(
            NSMakeRect(bx, primary_height - by - bh, bw, bh), True)
        view.setFrame_(NSMakeRect(0, 0, bw, bh))
        view.origin = (bx, by)
        view.place = spot
        view.label = label
        view.step = step
        view.on_light = on_light
        view.opacity = 1.0
        view.was = previous if (animate and previous) else None
        view.travel = 0.0 if view.was else 1.0
        window.orderFrontRegardless()
        view.setNeedsDisplay_(True)
        return True

    if not aim(args.x, args.y, args.label, args.step, args.on_light):
        return 1

    if args.session:
        return _run_session(args, app, window, view, primary_height, aim,
                            NSApp, NSMakeRect, NSTimer)

    state = {"elapsed": 0.0}
    step = 1.0 / 60.0
    fade_for = 0.45

    def tick(timer):
        state["elapsed"] += step
        view.phase = (view.phase + step * 0.75) % 1.0
        view.travel = min(1.0, view.travel + step * 2.6)
        remaining = args.seconds - state["elapsed"]
        view.opacity = max(0.0, min(1.0, remaining / fade_for)) if remaining < fade_for else 1.0
        view.setNeedsDisplay_(True)
        if state["elapsed"] >= args.seconds:
            NSApp().terminate_(None)

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(step, True, tick)
    app.run()
    return 0


def _run_session(args, app, window, view, primary_height, aim,
                 NSApp, NSMakeRect, NSTimer) -> int:
    """
    Stay up and move when told. One guide, many steps.

    stdin is read on its own thread because a blocking readline on the main
    thread would stop the run loop, and a run loop that is not running is a
    guide that does not move and a window that does not redraw.
    """
    from Foundation import NSOperationQueue

    def move(x, y, label, step, on_light):
        # AppKit off the main thread is undefined behaviour that usually looks
        # like it works, right up until it draws nothing.
        #
        # The block has to return void. A lambda returning aim()'s boolean
        # raises "did not return None, expecting void return value" from inside
        # the operation queue, which kills the process — the guide simply
        # disappeared on the second step with nothing in the log.
        def apply():
            aim(x, y, label, step, on_light, animate=True)
        NSOperationQueue.mainQueue().addOperationWithBlock_(apply)

    # Watching for clicks.
    #
    # This lives here rather than in the tutor because a global NSEvent
    # monitor needs an NSApplication run loop on the main thread, and this
    # process already has one — the assistant's main thread belongs to the
    # audio queue and cannot host a second one. The overlay was already a
    # separate process for exactly that reason, so it may as well do both jobs.
    #
    # Clicks are reported, never swallowed: the monitor is passive and the
    # window is click-through, so a step that says "click Export" is followed
    # by the user actually clicking Export and the application actually
    # receiving it.
    if args.watch_clicks:
        from AppKit import (NSEvent, NSEventMaskLeftMouseDown,
                            NSEventMaskRightMouseDown)

        def clicked(event):
            try:
                # The event's own location, not the cursor's current one.
                #
                # NSEvent.mouseLocation() reads where the pointer is *now*,
                # which is not where the click happened if anything moved in
                # between — and with synthetic clicks the cursor often has not
                # caught up at all, so a click at one point was reported at the
                # previous one. For a global monitor with no window,
                # locationInWindow is already in screen coordinates.
                where = event.locationInWindow()
                print(json.dumps({"click": {"x": float(where.x),
                                            "y": float(primary_height - where.y)}}),
                      flush=True)
            except Exception:
                pass
            return event

        try:
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown, clicked)
            NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown, clicked)
            print(json.dumps({"watching": True}), flush=True)
        except Exception as e:
            print(json.dumps({"watching": False, "error": str(e)}), flush=True)

    def reader():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except Exception:
                continue
            if message.get("quit"):
                break
            try:
                move(float(message["x"]), float(message["y"]),
                     str(message.get("label", ""))[:80],
                     str(message.get("step", ""))[:12],
                     bool(message.get("on_light", False)))
            except Exception:
                continue
        NSOperationQueue.mainQueue().addOperationWithBlock_(
            lambda: NSApp().terminate_(None))

    threading.Thread(target=reader, daemon=True).start()

    step = 1.0 / 60.0

    def tick(timer):
        view.phase = (view.phase + step * 0.75) % 1.0
        if view.travel < 1.0:
            view.travel = min(1.0, view.travel + step * 2.6)
        view.setNeedsDisplay_(True)

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(step, True, tick)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
