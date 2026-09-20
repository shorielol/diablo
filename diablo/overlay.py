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

The marker is make_icon.draw_signal — the same mark as the app icon and the
floating presence, at a different size. Three renderings of one character stay one
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

# No colour of its own. Pointing is not a warning, so the ring, the leader and
# a region's outline are drawn in the ink Diablo's mark and the caption use:
# light on dark content, dark on light. Each stroke sits on a keyline of the
# opposite value, so it holds on any surface, including the ones nobody
# measured (the shell does not pass --on-light). Red means an error, and the
# overlay never shows one.
# Measured on light content with light ink (the shell's case): at 0.45 the
# ring faded to a grey hairline and the target stopped being the focus. On
# contrasting content the keyline disappears into the surface either way.
KEYLINE = 2.0          # how much wider the underlay is than the stroke it carries
KEYLINE_ALPHA = 0.75   # of the stroke's own alpha


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
    parser.add_argument("--no-marker", action="store_true",
                        help="Diablo's orb is already standing there; draw the "
                             "ring, line and caption but not a second copy of him")
    parser.add_argument("--marker-radius", type=float, default=0.0,
                        help="radius of whoever stands at the marker (the orb, "
                             "with its state ring, when --no-marker)")
    parser.add_argument("--w", type=float, default=0.0,
                        help="width of a content region being referred to; 0 = a control")
    parser.add_argument("--h", type=float, default=0.0, help="height of that region")
    parser.add_argument("--hidden", action="store_true",
                        help="session only: start invisible and wait to be shown")
    args = parser.parse_args()
    # Who stands at the marker: the small marker drawn here, or the orb and
    # its state ring. The placement, the caption and the line all clear it.
    marker_radius = args.marker_radius or (27.0 if args.no_marker else guidance.MARKER)

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

    def tones(view):
        """(ink, paper) as greys. The caption's pair, so ring and caption agree."""
        return (0.08, 0.98) if view.on_light else (0.97, 0.08)

    def keyed_stroke(path, width, alpha, view):
        """Stroke a path in the ink, over a slightly wider keyline of the paper."""
        ink, paper = tones(view)
        path.setLineWidth_(width + KEYLINE)
        NSColor.colorWithCalibratedWhite_alpha_(paper, alpha * KEYLINE_ALPHA).set()
        path.stroke()
        path.setLineWidth_(width)
        NSColor.colorWithCalibratedWhite_alpha_(ink, alpha).set()
        path.stroke()

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
        spot = view.place
        if spot is not None and spot.is_region:
            # A region of content is outlined, not ringed: a table or a graph
            # is not something to press, and a pulse would say it was.
            box = NSMakeRect(cx - spot.ring_w, cy - spot.ring_h,
                             spot.ring_w * 2, spot.ring_h * 2)
            outline = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(box, 8, 8)
            keyed_stroke(outline, 1.8, 0.85 * view.opacity, view)
            return
        radius = guidance.RING

        spread = view.phase % 1.0
        grow = radius + 2.0 + spread * 16.0
        fade = (1.0 - spread) * 0.42 * view.opacity
        if fade > 0.01:
            # The pulse is the quiet part: ink alone, no keyline.
            NSColor.colorWithCalibratedWhite_alpha_(tones(view)[0], fade).set()
            pulse = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(cx - grow, cy - grow, grow * 2, grow * 2))
            pulse.setLineWidth_(1.6)
            pulse.stroke()

        # The keyline under the ring is what lets it survive landing on a
        # surface its own value.
        ring = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2))
        keyed_stroke(ring, 2.2, 0.95 * view.opacity, view)

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

        spot = view.place
        begin = (spot.marker_r if spot is not None else marker_radius) + 5.0
        if spot is not None and spot.is_region:
            # Stop short of the region's edge along the line, not a circle.
            reach = min(spot.ring_w / abs(ux) if abs(ux) > 1e-6 else float("inf"),
                        spot.ring_h / abs(uy) if abs(uy) > 1e-6 else float("inf"))
            finish = length - (reach + 10.0)
        else:
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
            piece = NSBezierPath.bezierPath()
            piece.moveToPoint_(NSPoint(sx + ux * t0, sy + uy * t0))
            piece.lineToPoint_(NSPoint(sx + ux * t1, sy + uy * t1))
            piece.setLineCapStyle_(1)
            keyed_stroke(piece, width, alpha, view)

    def draw_marker(view, centre):
        """
        Diablo's mark, small, on a disc, turned to face the target. The same
        mark as the floating presence and the app icon.

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
        NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.96 * view.opacity).set()
        disc.fill()

        # Diablo's mark, the same drawing as the floating presence, turned to
        # face the target it is marking.
        if make_icon is not None and view.place is not None:
            spot = view.place
            tx, ty = view_point(view, spot.target_x, spot.target_y)
            try:
                from skills import motion
                side, lean = motion.face(tx - cx, ty - cy)
                make_icon.draw_signal(
                    cx, cy, radius * 2 * 0.70,
                    NSColor.labelColor().colorWithAlphaComponent_(view.opacity),
                    heading=lean, side=side)
            except Exception:
                pass

        NSColor.separatorColor().colorWithAlphaComponent_(view.opacity).set()
        disc.setLineWidth_(1.0)
        disc.stroke()

    def draw_caption(view, marker, spot):
        """
        The instruction, beside him, never over the target.

        One weight, one size. The step counter is the only secondary thing and
        it is smaller and quieter rather than a different colour.
        """
        ink, paper = tones(view)
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
        clear = spot.marker_r
        if spot.side.startswith("above"):
            box = NSMakeRect(cx - box_w / 2, cy + clear + 8, box_w, box_h)
        elif spot.side.startswith("below"):
            box = NSMakeRect(cx - box_w / 2, cy - clear - 8 - box_h, box_w, box_h)
        elif spot.side == "right":
            box = NSMakeRect(cx + clear + 9, cy - box_h / 2, box_w, box_h)
        else:
            box = NSMakeRect(cx - clear - 9 - box_w, cy - box_h / 2, box_w, box_h)

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
            # Where opacity is heading. Showing, hiding and quitting all fade
            # rather than blink, in session mode.
            self.target_opacity = 1.0
            self.quitting = False
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
            if not args.no_marker:
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

    def aim(x, y, label, step, on_light, animate=False, w=0.0, h=0.0, fresh=False):
        """
        Point at a new target: re-place, resize, and slide him over.

        `fresh` is for when the orb has walked to the new target itself: the
        indication fades in where it now belongs instead of sliding across
        from the old one, because the orb has already done the travelling.
        """
        extent = (w / 2.0 + 6.0, h / 2.0 + 6.0) if w > 0 and h > 0 else None
        spot = guidance.place(x, y, label, window=None, step=step,
                              extent=extent, marker=marker_radius)
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
        if fresh:
            view.opacity = 0.0
        elif not args.session:
            view.opacity = 1.0
        view.target_opacity = 1.0
        view.was = previous if (animate and previous and not fresh) else None
        view.travel = 0.0 if view.was else 1.0
        window.orderFrontRegardless()
        view.setNeedsDisplay_(True)
        return True

    if not aim(args.x, args.y, args.label, args.step, args.on_light,
               w=args.w, h=args.h):
        return 1
    if args.session and args.hidden:
        view.opacity = view.target_opacity = 0.0

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

    def move(x, y, label, step, on_light, w=0.0, h=0.0, fresh=False):
        # AppKit off the main thread is undefined behaviour that usually looks
        # like it works, right up until it draws nothing.
        #
        # The block has to return void. A lambda returning aim()'s boolean
        # raises "did not return None, expecting void return value" from inside
        # the operation queue, which kills the process — the guide simply
        # disappeared on the second step with nothing in the log.
        def apply():
            aim(x, y, label, step, on_light, animate=True, w=w, h=h, fresh=fresh)
        NSOperationQueue.mainQueue().addOperationWithBlock_(apply)

    def fade(to, then_quit=False):
        def apply():
            view.target_opacity = to
            view.quitting = view.quitting or then_quit
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
            if message.get("hide"):
                fade(0.0)
                continue
            try:
                move(float(message["x"]), float(message["y"]),
                     str(message.get("label", ""))[:80],
                     str(message.get("step", ""))[:12],
                     bool(message.get("on_light", False)),
                     float(message.get("w", 0.0)), float(message.get("h", 0.0)),
                     bool(message.get("fresh", False)))
            except Exception:
                continue
        # Asked to go, or the other end went away: fade out, then exit.
        fade(0.0, then_quit=True)

    threading.Thread(target=reader, daemon=True).start()

    step = 1.0 / 60.0

    def tick(timer):
        view.phase = (view.phase + step * 0.75) % 1.0
        if view.travel < 1.0:
            view.travel = min(1.0, view.travel + step * 2.6)
        # Fade toward the target opacity: in over ~0.2 s, out over ~0.2 s.
        change = step / 0.2
        if view.opacity < view.target_opacity:
            view.opacity = min(view.target_opacity, view.opacity + change)
        elif view.opacity > view.target_opacity:
            view.opacity = max(view.target_opacity, view.opacity - change)
        if view.quitting and view.opacity <= 0.0:
            NSApp().terminate_(None)
            return
        view.setNeedsDisplay_(True)

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(step, True, tick)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
