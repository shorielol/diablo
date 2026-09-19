"""
D.I.A.B.L.O. — the Mac app.

Three surfaces, and the split between them is the whole design:

  menu bar    always there, costs nothing
  the pill    a small floating capsule near your cursor showing what Diablo is
              doing right now. Drawn natively in AppKit.
  the panel   the full window — conversation, agents, trace, health, settings.
              A WKWebView, created the first time you open it and not before.

The pill is native rather than a second web view on purpose. It is the surface
that is visible all day, and a WKWebView content process costs about 33 MB on
this machine. Drawing a rounded rectangle and twenty-eight bars in Core
Graphics costs nothing measurable. The rich panel earns its 33 MB because you
only open it when you want it, and it closes again.

The shape is borrowed from Clicky, which got it right: menu-bar only with no
Dock icon, a non-activating always-on-top pill that never steals focus, and a
live waveform so you can see it hearing you. What is different is what the pill
says — Diablo does work, not just dictation, so it reports which agent is
running and what tool it is in.

Threading: AppKit owns the main thread, full stop. The voice loop moves to a
background thread, which is the opposite of how voice_bridge ran before and the
only arrangement macOS allows.
"""

import json
import math
import os
import sys
import threading
import time
import traceback
import urllib.request
from typing import Any, Callable, Dict, List, Optional

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
# Set DIABLO_APP_DEBUG=1 to trace the pill. Verifying this by counting red
# pixels in a screenshot produced two false positives in a row — a stop button
# elsewhere on screen is also crimson — so the app says what it is doing.
DEBUG = os.getenv("DIABLO_APP_DEBUG", "0") not in ("0", "", "false")


def _log(*parts):
    if DEBUG:
        print("  [shell]", *parts, flush=True)
sys.path.insert(0, HOME)

import objc  # noqa: E402
from AppKit import (  # noqa: E402
    NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
    NSBezierPath, NSColor, NSEvent, NSFont, NSFontAttributeName,
    NSForegroundColorAttributeName, NSImage, NSMakeRect, NSMakeSize, NSMenu,
    NSMenuItem, NSPoint, NSScreen, NSStatusBar, NSString, NSTimer, NSView,
    NSWindow, NSWindowStyleMaskBorderless, NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView, NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary,
    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowTitleHidden,
    NSPanel, NSWindowStyleMaskNonactivatingPanel, NSGraphicsContext,
    NSGradient, NSEventMaskFlagsChanged, NSEventModifierFlagControl,
    NSEventModifierFlagOption, NSEventModifierFlagCommand, NSEventModifierFlagShift,
)
from AppKit import (  # noqa: E402
    NSWorkspace, NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification,
)
from Foundation import (  # noqa: E402
    NSAffineTransform, NSObject, NSOperationQueue, NSRunLoop, NSRunLoopCommonModes,
    NSURL, NSURLRequest,
)
import Quartz  # noqa: E402
from Quartz import (  # noqa: E402
    CABasicAnimation, CAMediaTimingFunction, kCAMediaTimingFunctionEaseInEaseOut,
)
from WebKit import WKWebView, WKWebViewConfiguration  # noqa: E402

# The character is defined once, in the icon builder, on the mascot's own
# 120-unit grid. The orb draws the same paths rather than a second copy of
# them, so his face cannot differ between the Dock and the screen.
from skills import make_icon  # noqa: E402
# Every moving thing is driven by one motion system; the shell only draws it.
from skills import motion  # noqa: E402


# ─── Push to talk ────────────────────────────────────────────────────────────
#
# Hold two modifiers and speak. Clicky uses Control+Option and it is a good
# choice: no application binds that pair, and it needs no key on top, so you
# are not aiming for a letter while composing a sentence.
#
# Watched with a global NSEvent monitor, which requires Accessibility. That is
# a real dependency and it fails silently when the permission is missing, so
# the shell checks up front and says so rather than leaving a key that does
# nothing.

PTT_MASK = NSEventModifierFlagControl | NSEventModifierFlagOption
PTT_NAME = "Control + Option"


def _ptt_engaged(flags) -> bool:
    """Both modifiers down, and nothing else that would make it a real chord."""
    interesting = (NSEventModifierFlagControl | NSEventModifierFlagOption
                   | NSEventModifierFlagCommand | NSEventModifierFlagShift)
    return (int(flags) & interesting) == PTT_MASK

# ─── Look ────────────────────────────────────────────────────────────────────
# The same tokens the web UI uses, so the two surfaces match exactly.

CRIMSON = (0.878, 0.137, 0.235)
GOLD = (0.949, 0.761, 0.188)
BLUE = (0.239, 0.663, 0.988)
GREY = (0.290, 0.290, 0.333)
RED = (1.0, 0.420, 0.420)

# Two colours, both his own, and red only for a fault.
#
# This was five: grey, crimson, gold, a sky blue for speaking and a pale red
# for errors. Photographed side by side, the blue is the one that looks wrong
# — it belongs to no part of the character and reads as a generic status
# indicator bolted to him.
#
# Colour was not carrying the distinction anyway. The shapes already do, and
# they do it unambiguously: listening pushes rings outward on the microphone
# level, speaking holds a steady halo that breathes with his mouth, thinking
# sweeps one arc. Two of those are crimson now and nobody will confuse them,
# because one travels and one does not — and his mouth moves for exactly one
# of them.
#
# Gold stays for thinking. It is his eye colour, so it is still him, and it
# separates "working on it" from anything to do with the voice.
STATE_LOOK = {
    "idle":       (GREY,    "ready"),
    "listening":  (CRIMSON, "listening"),
    "processing": (GOLD,    "thinking"),
    # Answer ready, speaker silent. Deliberately no new drawing: it falls
    # through to the plain static ring the draw code already has, in the same
    # gold as thinking. Thinking sweeps, preparing holds still — which is
    # exactly the difference, and it costs nothing to say it that way.
    "preparing":  (GOLD,    "ready"),
    "responding": (CRIMSON, "speaking"),
    "error":      (RED,     "something broke"),
    "init":       (GREY,    "waking"),
    "cold":       (GREY,    "asleep"),
    "shutdown":   (GREY,    "shutting down"),
}

# ─── The orb ─────────────────────────────────────────────────────────────────
#
# It used to be a 300 by 56 capsule with his face on the left, the state in
# words and a live waveform on the right. It was legible and it was too much:
# a strip that wide sits across whatever is underneath it all day, and none of
# the three things it said were things you needed to read.
#
# So: the character, in a circle, and nothing else. State is carried by the
# ring around him and by his own face — the mouth moves while he talks, which
# is the one animation that says "this is speaking to you" without a caption.

# ── Orb geometry: one source of truth ───────────────────────────────────────
#
# Everything about the presence's size and alignment is derived from these
# eight numbers. Nothing here is a guess: the mascot figures were measured by
# rendering draw_head into a 480px buffer and finding its actual ink.
#
# What the measurement found, and why the old orb looked off-centre:
#
#   draw_head works in a 120-unit square and does NOT fill it. The ink runs
#   x 28.0..91.5 and y 23.0..84.8, which leaves 23.0 units of empty space
#   above the drawing and 35.2 below it. The canvas is symmetric; the artwork
#   inside it is not. Centring the canvas therefore centres the padding.
#
#   Worse, the old code centred mascot y=49 — neither the canvas centre (60)
#   nor the ink centre (53.9) nor the eye line (59.5). The eyes landed 10.5
#   mascot units below the orb's centre, which at the old scale was 7.5 points
#   in a 62 point circle. Twelve per cent of the diameter, which is exactly
#   how far off it looked.

ORB = 44.0          # the visible disc, in points
HALO = 11.0         # room outside it for the state ring to breathe into
CANVAS = ORB + HALO * 2

# The mascot's real ink, in its own 120-unit space. Measured, not assumed.
MASCOT_INK_X = (28.0, 91.5)
MASCOT_INK_Y = (23.0, 84.8)
MASCOT_EYE_X = 59.9       # midpoint between the irises; the canvas centre is 60
MASCOT_EYE_LINE = 59.5    # the irises sit here, well below the ink centre of 53.9

# How much of the disc the artwork spans. He is contained by the circle rather
# than pressed against it, so there is a ring of quiet between his hair and the
# rim on every side.
FACE_FILL = 0.70

# The one optical correction, and the only one.
#
# The ink is centred in the disc, which gives equal breathing room above and
# below. That alone leaves the eyes 2.7 points low, because the drawing is
# top-heavy — there is more hair above the eyes than jaw below them. Lifting
# the whole figure by three per cent of the diameter puts the eye line within
# 1.4 points of the true centre while keeping the breathing room close to even
# (5.7 points above, 8.3 below), which reads correctly because a top-heavy
# shape wants slightly less air above it.
#
# Tuned by photographing the result, not by arithmetic. At 0.03 the eyes
# measured 3.6% of the diameter below the disc's centre; at 0.045, 2.3%; at
# 0.060, 0.4% — which is under a fifth of a point at this size. The ink is
# then 4.4 points from the rim above and 9.6 below, and that asymmetry is
# invisible because the hair occupying the top is within a few per cent of
# the disc's own colour. What the eye reads as "the character" is the pale
# face and the two streaks, and those sit centred.
#
# It is a fraction of ORB rather than a pixel count so it survives any size
# change, and it is the single place any such nudge belongs.
OPTICAL_LIFT = 0.060


def _colour(rgb, alpha=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, alpha)


class OrbView(NSView):
    """
    Diablo, as a face in a circle.

    It draws exactly what the motion system says, and nothing on its own
    clock: every frame is one Look from skills.motion, and the shell's
    display link decides when there is a frame at all. At rest there are no
    frames, because idle is by far the most common state and a view redrawing
    a picture that does not change is how a menu bar app ends up in the
    battery report.
    """

    def initWithFrame_(self, frame):
        self = objc.super(OrbView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.presence = motion.Presence()
        self.state = "idle"
        self.accent = GREY
        self.level = 0.0          # smoothed microphone amplitude, listening only
        self.on_click = None
        self.tooltip = "ready"
        return self

    def isOpaque(self):
        return False

    def mouseDown_(self, event):
        if self.on_click is not None:
            self.on_click()

    def update_(self, payload):
        """The manager's state changed. The motion system decides the look."""
        state = payload.get("state", self.state)
        self.state = state
        self.accent, words = STATE_LOOK.get(state, (GREY, state))
        self.presence.set_runtime(state)
        self.tooltip = payload.get("headline") or words
        self.setToolTip_(self.tooltip)
        self.setNeedsDisplay_(True)

    def animating(self) -> bool:
        """Does the current state have a loop of its own (not just a transition)?"""
        return self.presence.looping()

    def pushLevel_(self, level):
        # Heavily smoothed. The rings are there to say "I can hear you", not
        # to plot the room: an unsmoothed level makes them shake.
        target = max(0.0, min(1.0, float(level)))
        self.level += (target - self.level) * 0.12

    # ── the face ─────────────────────────────────────────────────────────

    @objc.python_method
    def _mouth_openness(self, look) -> float:
        """
        How far open his mouth is, this frame. Only while speaking.

        Two sine waves at unrelated frequencies rather than one: a single sine
        is a metronome, and a mouth opening on a metronome reads as a machine
        rather than as speech. The slower wave supplies the pauses.
        """
        weight = look.rings.get("speak", 0.0)
        if weight <= 0.0:
            return 0.0
        fast = math.sin(look.time * 11.0)
        slow = math.sin(look.time * 2.7 + 1.1)
        shaped = (0.55 + 0.45 * fast) * (0.45 + 0.55 * (0.5 + 0.5 * slow))
        return max(0.0, min(1.0, shaped * 0.9 * weight))

    _draws = 0

    def drawRect_(self, rect):
        OrbView._draws += 1
        look = self.presence.look()
        cx = cy = CANVAS / 2.0
        r = ORB / 2.0
        context = NSGraphicsContext.currentContext()
        context.saveGraphicsState()

        # Scale about the centre, so growing and tucking never shift him.
        grow = NSAffineTransform.transform()
        grow.translateXBy_yBy_(cx, cy)
        grow.scaleBy_(look.scale)
        grow.translateXBy_yBy_(-cx, -cy)
        grow.concat()

        for name, weight in look.rings.items():
            self._draw_ring(name, weight, look, cx, cy, r)
        if look.cue:
            self._draw_cue(look, cx, cy, r)

        if look.dim:
            Quartz.CGContextSetAlpha(context.CGContext(), 0.6)

        # The disc he sits in.
        disc = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - r, cy - r, r * 2, r * 2))
        context.saveGraphicsState()
        disc.addClip()
        # Pitched at his hair rather than darker than it. A disc noticeably
        # darker than the character leaves the edge of his head visible as a
        # seam inside the circle. Matched, he reads as filling the circle.
        NSGradient.alloc().initWithStartingColor_endingColor_(
            _colour((0.067, 0.067, 0.082), 0.97),
            _colour((0.098, 0.098, 0.118), 0.97)).drawInRect_angle_(
                NSMakeRect(cx - r, cy - r, r * 2, r * 2), 90.0)

        # The character, drawn from make_icon so that the orb, the app icon and
        # the window are one drawing rather than three that drift apart.
        # Scaled and placed from the measured ink, never from the 120-unit
        # canvas; the face may shift sideways (thinking, reading, a shake),
        # always inside the disc.
        ink_w = MASCOT_INK_X[1] - MASCOT_INK_X[0]
        ink_mid_x = (MASCOT_INK_X[0] + MASCOT_INK_X[1]) / 2.0
        ink_mid_y = (MASCOT_INK_Y[0] + MASCOT_INK_Y[1]) / 2.0
        scale = ORB * FACE_FILL / ink_w
        transform = NSAffineTransform.transform()
        transform.translateXBy_yBy_(
            cx + look.face_dx - ink_mid_x * scale,
            cy + ORB * OPTICAL_LIFT + ink_mid_y * scale)
        transform.scaleXBy_yBy_(scale, -scale)
        context.saveGraphicsState()
        transform.concat()
        # His streak stays crimson in every state. The streak is how you
        # recognise him; state belongs on the ring, which is not part of him.
        make_icon.draw_head(detail=True, mouth=self._mouth_openness(look))
        context.restoreGraphicsState()
        context.restoreGraphicsState()

        # A hairline, so he keeps an edge against a light background.
        _colour((1, 1, 1), 0.14).set()
        disc.setLineWidth_(1.0)
        disc.stroke()
        context.restoreGraphicsState()

    @objc.python_method
    def _circle(self, cx, cy, radius, width, rgb, alpha):
        if alpha <= 0.01:
            return
        _colour(rgb, alpha).set()
        ring = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - radius, cy - radius, radius * 2, radius * 2))
        ring.setLineWidth_(width)
        ring.stroke()

    @objc.python_method
    def _draw_ring(self, name, weight, look, cx, cy, r):
        """
        One state ring, at `weight` opacity. Rings cross-fade: while one state
        hands over to the next, both are drawn, one fading out and one in.

        Colour is never the only difference. Listening pushes rings outward,
        speaking holds a halo that breathes with his mouth, thinking sweeps an
        arc, reading holds still while his face scans, working circles a dot,
        waiting pulses slowly, guiding holds a steady ring.
        """
        t = look.time
        if name == "listen":
            level = self.level
            for index in range(2):
                offset = (t * 0.9 + index * 0.5) % 1.0
                spread = 3.0 + offset * (HALO - 3.0)
                fade = (1.0 - offset) * 0.5 * (0.6 + 0.4 * level)
                self._circle(cx, cy, r + spread, 2.0, CRIMSON, fade * weight)
        elif name == "speak":
            glow = 4.0 + 5.0 * self._mouth_openness(look)
            self._circle(cx, cy, r + glow / 2, glow, CRIMSON, 0.30 * weight)
        elif name == "think":
            if look.still:
                # Preparing: the answer exists, the sound has not started.
                self._circle(cx, cy, r + 5, 2.4, GOLD, 0.65 * weight)
                return
            self._circle(cx, cy, r + 5, 2.4, GOLD, 0.16 * weight)
            sweep = NSBezierPath.bezierPath()
            head = (-t * 200.0) % 360.0
            sweep.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                NSPoint(cx, cy), r + 5, head, head - 78, True)
            sweep.setLineWidth_(2.4)
            sweep.setLineCapStyle_(1)          # round
            _colour(GOLD, 0.95 * weight).set()
            sweep.stroke()
        elif name == "read":
            self._circle(cx, cy, r + 5, 2.0, GOLD, 0.55 * weight)
        elif name == "travel":
            self._circle(cx, cy, r + 4, 1.5, GOLD, 0.30 * weight)
        elif name == "guide":
            self._circle(cx, cy, r + 5, 2.0, GOLD, 0.80 * weight)
        elif name == "work":
            self._circle(cx, cy, r + 5, 2.0, GOLD, 0.12 * weight)
            angle = math.radians(90.0 - (t / 2.4) * 360.0)
            dot_x = cx + (r + 5) * math.cos(angle)
            dot_y = cy + (r + 5) * math.sin(angle)
            _colour(GOLD, 0.85 * weight).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(dot_x - 2.4, dot_y - 2.4, 4.8, 4.8)).fill()
        elif name == "wait":
            pulse = 0.35 + 0.30 * (0.5 + 0.5 * math.sin(2 * math.pi * t / 1.6))
            self._circle(cx, cy, r + 4, 2.0, CRIMSON, pulse * weight)
        elif name == "error":
            self._circle(cx, cy, r + 4, 2.0, RED, 0.75 * weight)

    @objc.python_method
    def _draw_cue(self, look, cx, cy, r):
        """The one-shots: done, interrupted, failed. Each plays once and ends."""
        p = look.cue_progress
        if look.cue == "success":
            self._circle(cx, cy, r + 3 + 8 * p, 2.0, GOLD, 0.8 * (1 - p))
        elif look.cue == "interrupted":
            self._circle(cx, cy, r + 11 - 8 * p, 2.0, CRIMSON, 0.7 * (1 - p))
        elif look.cue == "failure":
            self._circle(cx, cy, r + 4, 2.0, RED, 0.9 * (1 - 0.4 * p))


class Pill:
    """
    The panel the orb lives in. Never takes focus, never eats a click that
    was meant for something else.

    Still called Pill because everything that talks to it calls it that, and
    renaming the class would be the whole diff.
    """

    def __init__(self):
        frame = NSMakeRect(0, 0, CANVAS, CANVAS)
        # alloc() and init...() must be chained. Calling init on the allocated
        # object and throwing the return value away leaves you holding an
        # uninitialised panel that accepts every setter and draws nothing —
        # silently, with no error anywhere, which cost two rounds of debugging.
        # NonactivatingPanel is not optional. Without it, a borderless NSPanel
        # belonging to an accessory app that has never been activated is
        # created, reports isVisible() as true, runs drawRect_, and is never
        # composited — it does not even appear in CGWindowList. Every API call
        # succeeds and nothing is on screen.
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        self.panel.setFloatingPanel_(True)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(True)
        self.panel.setLevel_(25)                       # above windows, below alerts
        # Clickable at home, unlike Clicky's, which is purely informational. On
        # a full menu bar macOS hides new status items with no error and no
        # way to override it, so the orb has to be reachable — otherwise the
        # app has no entry point at all on a machine like this one.
        self.panel.setIgnoresMouseEvents_(False)
        self.panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.view = OrbView.alloc().initWithFrame_(frame)
        # Layer-backed so the idle breath can run in the compositor with no
        # frames drawn here at all.
        self.view.setWantsLayer_(True)
        self.panel.setContentView_(self.view)
        self.visible = False
        self.away = False
        self.on_click = None

    def home_point(self):
        """
        Where he rests: bottom-right of whichever screen the cursor is on,
        as the orb's centre in AppKit points.

        Clicky puts its character at the cursor. Diablo's is up for whole
        tasks rather than the moment you hold a key, so it rests in a corner
        and only leaves it when there is something on screen to go to.
        """
        mouse = NSEvent.mouseLocation()
        screen = next((s for s in NSScreen.screens()
                       if _contains(s.frame(), mouse)), NSScreen.mainScreen())
        visible = screen.visibleFrame()
        return (visible.origin.x + visible.size.width - 18 - CANVAS / 2.0,
                visible.origin.y + 18 + CANVAS / 2.0)

    def position(self):
        self.view.presence.set_home(self.home_point())
        self.place(self.view.presence.look())

    def place(self, look):
        """Put the window where the motion system says, this frame."""
        x, y = look.position
        self.panel.setFrameOrigin_(NSPoint(x - CANVAS / 2.0, y - CANVAS / 2.0))
        self.panel.setAlphaValue_(look.alpha)
        # Away from home he is referring to something, and a click near what
        # he is pointing at belongs to the application underneath, not to him.
        away = self.view.presence.phase != motion.HOME
        if away != self.away:
            self.panel.setIgnoresMouseEvents_(away)
            self.away = away

    def show(self, **payload):
        self.view.on_click = self.on_click
        self.view.update_(payload)
        if not self.visible:
            self.position()
            self.panel.orderFrontRegardless()
            self.visible = True

    def hide(self):
        if self.visible:
            _log("orb.hide()")
            self.panel.orderOut_(None)
            self.visible = False


def _contains(frame, point):
    return (frame.origin.x <= point.x <= frame.origin.x + frame.size.width
            and frame.origin.y <= point.y <= frame.origin.y + frame.size.height)


# ─── The panel ───────────────────────────────────────────────────────────────

class Panel:
    """
    The full window. Built the first time it is opened, and not before.

    That laziness is the point of the whole split: until you press the menu bar
    icon there is no web view and no 33 MB.

    It opens out of the orb and closes back into it: it sits directly above
    him, and on opening it rises the last few points out of him while it
    fades in; on closing it sinks back and fades, faster. Both are driven by
    the same motion system as everything else, and a close that is overtaken
    by a reopen never hides the window.
    """

    # How far the window travels as it opens: enough to read as coming out
    # of the orb below it, not so far that it reads as sliding in.
    RISE = 14.0

    def __init__(self, url: str, presence=None, wake=None):
        self.url = url
        self.window = None
        self.webview = None
        self.presence = presence
        self.wake = wake or (lambda: None)
        self.rest = None          # the window's origin when fully open
        self.closing = False

    # The size it opens at.
    #
    # It opened at 1040x680 and centred, which is a window you go TO. Almost
    # every use of this surface is one sentence and one reply, and a centred
    # window two-thirds the width of the screen for that put a dashboard in
    # front of whatever was being worked on.
    #
    # 392 is wide enough for a sentence and a reply at a comfortable measure
    # and narrow enough to sit beside an editor rather than over it. The rail,
    # the trace and the settings are all still there — the stylesheet brings
    # the rail back above 720 points, so dragging the window wider restores
    # the old layout exactly. Nothing was removed; it simply is not what you
    # get for asking one question.
    COMPACT = (392.0, 560.0)

    def _build(self):
        frame = NSMakeRect(0, 0, *Panel.COMPACT)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskFullSizeContentView)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False)
        self.window.setTitle_("Diablo")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(NSWindowTitleHidden)
        self.window.setBackgroundColor_(_colour((0.043, 0.043, 0.051), 1.0))
        self.window.setMinSize_(NSMakeSize(340, 360))
        # Released only by us: closing it with the red button hides it, and
        # the next open reuses the same web view.
        self.window.setReleasedWhenClosed_(False)
        self._anchor()

        config = WKWebViewConfiguration.alloc().init()
        self.webview = WKWebView.alloc().initWithFrame_configuration_(frame, config)
        self.webview.setValue_forKey_(False, "drawsBackground")
        self.window.setContentView_(self.webview)
        self.webview.loadRequest_(
            NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url)))

    def _anchor(self):
        """
        Open beside the orb, not in the middle of the screen.

        The expansion has to read as the presence growing into a surface. A
        window that appears centred is an unrelated thing arriving; a window
        that appears where Diablo already was is the same thing, larger. The
        orb sits 18 points in from the bottom-right of whichever screen the
        cursor is on, so the panel sits directly above it with the same inset,
        and both share a right edge.
        """
        width, height = Panel.COMPACT
        try:
            mouse = NSEvent.mouseLocation()
            screen = next((s for s in NSScreen.screens()
                           if _contains(s.frame(), mouse)), NSScreen.mainScreen())
            visible = screen.visibleFrame()
            x = visible.origin.x + visible.size.width - width - 18
            # Clear of the orb, which is CANVAS tall with an 18 point inset.
            y = visible.origin.y + 18 + CANVAS + 10
            # Never taller than the screen it is on.
            if y + height > visible.origin.y + visible.size.height:
                y = max(visible.origin.y + 18,
                        visible.origin.y + visible.size.height - height - 18)
            self.window.setFrameOrigin_(NSPoint(x, y))
        except Exception:
            self.window.center()
        origin = self.window.frame().origin
        self.rest = (origin.x, origin.y)

    def toggle(self):
        if self.window is None:
            self._build()
        if self.window.isVisible() and not self.closing:
            self.close()
        else:
            self.open()

    def open(self):
        if self.window is None:
            self._build()
        self.closing = False
        hidden = not self.window.isVisible()
        if hidden:
            self.apply(0.0)
            if self.presence is not None:
                # Closed some other way (the red button): start from closed,
                # or the spring would think it is already open and never run.
                self.presence.panel.jump(0.0)
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if self.presence is None:
            self.apply(1.0)
            return
        self.presence.open_panel()
        self.wake()

    def close(self):
        if self.window is None or not self.window.isVisible():
            return
        if self.presence is None:
            self.window.orderOut_(None)
            return
        if self.presence.panel.at_rest():
            # Fully open: wherever it has been dragged to is where it rests.
            origin = self.window.frame().origin
            self.rest = (origin.x, origin.y)
        self.closing = True
        self.presence.close_panel(on_done=self._closed)
        self.wake()

    def _closed(self, is_open):
        # Only ever reached for the newest close: a reopen during the close
        # took ownership, and this completion was dropped.
        if not is_open and self.window is not None:
            self.closing = False
            self.window.orderOut_(None)

    def apply(self, progress):
        """One frame of opening or closing: 0 is inside the orb, 1 is open."""
        if self.window is None or self.rest is None:
            return
        progress = max(0.0, min(1.0, progress))
        self.window.setAlphaValue_(progress)
        self.window.setFrameOrigin_(
            NSPoint(self.rest[0], self.rest[1] - (1.0 - progress) * Panel.RISE))


def _status_icon():
    """
    The menu bar glyph: a small ring with a notch, drawn once.

    Template mode is what makes it follow the menu bar's tint instead of
    staying a fixed colour when the wallpaper is light.
    """
    size = 18.0
    image = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    image.lockFocus()
    NSColor.blackColor().set()
    ring = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(2.5, 2.5, 13, 13))
    ring.setLineWidth_(2.0)
    ring.stroke()
    NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(7, 7, 4, 4)).fill()
    image.unlockFocus()
    image.setTemplate_(True)
    return image


# ─── The frame clock ─────────────────────────────────────────────────────────

class _FrameClock(NSObject):
    """
    The display link's target.

    Defined once, here: an NSObject subclass defined inside a function
    registers a new Objective-C class on every call, which works exactly once.
    The link calls it on the main thread, in step with the display — 120 Hz
    on a ProMotion panel, 60 elsewhere — and only while it is unpaused.
    """

    def initWithHandler_(self, handler):
        self = objc.super(_FrameClock, self).init()
        if self is None:
            return None
        self.handler = handler
        return self

    def tick_(self, link):
        try:
            self.handler(link)
        except Exception:
            traceback.print_exc()


class FrameStats:
    """What the clock actually did, measured, so claims about smoothness are data."""

    def __init__(self):
        self.intervals: List[float] = []
        self.costs: List[float] = []

    def note(self, interval: float, cost: float) -> None:
        self.intervals.append(interval)
        self.costs.append(cost)
        if len(self.intervals) > 4000:
            del self.intervals[:2000]
            del self.costs[:2000]

    def summary(self) -> Dict[str, float]:
        if not self.intervals:
            return {"frames": 0}
        ordered = sorted(self.intervals)
        costs = sorted(self.costs)
        return {
            "frames": len(ordered),
            "median_interval_ms": 1000 * ordered[len(ordered) // 2],
            "p95_interval_ms": 1000 * ordered[int(len(ordered) * 0.95)],
            "long_frames": sum(1 for i in ordered if i > 1.5 / 60.0),
            "median_cost_ms": 1000 * costs[len(costs) // 2],
            "p95_cost_ms": 1000 * costs[int(len(costs) * 0.95)],
        }


def _reduce_motion() -> bool:
    try:
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
    except Exception:
        return False


# The running shell, so the rest of the process can tell it things without
# holding a reference. None until the shell is built.
_SHELL = None


def notify(event: str, **data: Any) -> bool:
    """
    Tell the presence a real thing happened. Safe from any thread.

    `reading` (depth), `waiting` (bool), `jobs` (running), `success`,
    `failure`, `interrupted`, and `target` (x, y, label, seconds, spawn,
    generation). Returns False when there is no shell to tell, so a caller
    can fall back to what it did before.
    """
    shell = _SHELL
    if shell is None:
        return False
    _on_main(lambda: shell.signal(event, data))
    return True


# ─── The app ─────────────────────────────────────────────────────────────────

class Shell:
    def __init__(self, url: str, manager=None):
        global _SHELL
        self.url = url
        self.manager = manager
        self.app = NSApplication.sharedApplication()
        # Accessory policy: lives in the menu bar, no Dock icon, no app switcher
        # entry. An assistant you talk to should not be something you alt-tab to.
        self.app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        _log("Shell.__init__ start")
        self.pill = Pill()
        self.presence = self.pill.view.presence
        self.presence.set_reduced(_reduce_motion())
        self.pill.on_click = lambda: _on_main(self.panel.toggle)
        _log("  pill built")
        self.panel = Panel(url, presence=self.presence, wake=self.wake)
        self._build_menu()
        _log("  menu built, status item:", self.item, "button:", self.item.button())

        self._link = None
        self._last = None
        self._fast = None
        self._breathing = False
        self.stats = FrameStats()
        self._watch_reduce_motion()

        try:
            from skills import server as _server
            _server.attach(panel_toggle=lambda: _on_main(self.panel.toggle))
        except Exception:
            pass
        if manager is not None:
            manager.on("*", self._on_event)
        _SHELL = self
        _log("Shell.__init__ done")

    def _build_menu(self):
        bar = NSStatusBar.systemStatusBar()
        self.item = bar.statusItemWithLength_(-1.0)          # variable length
        self.item.setVisible_(True)
        button = self.item.button()
        # A drawn template image rather than a text glyph. Text status items
        # are wider, and a template image picks up the menu bar's own tint in
        # light and dark automatically.
        button.setImage_(_status_icon())
        button.setToolTip_("Diablo")
        _log("status item visible =", self.item.isVisible(),
             "length =", self.item.length())

        menu = NSMenu.alloc().init()
        self.status_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Ready", None, "")
        self.status_item.setEnabled_(False)
        menu.addItem_(self.status_item)
        menu.addItem_(NSMenuItem.separatorItem())

        self._add(menu, "Open Diablo", "togglePanel:", "d")
        self._add(menu, "Stop speaking", "interrupt:", ".")
        self._add(menu, "Forget this conversation", "resetChat:", "")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add(menu, "Quit Diablo", "quitApp:", "q")
        self.item.setMenu_(menu)

    def _add(self, menu, title, selector, key):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, objc.selector(getattr(self, selector.rstrip(":")),
                                 signature=b"v@:@"), key)
        item.setTarget_(self)
        menu.addItem_(item)

    # ── menu actions ─────────────────────────────────────────────────────
    def togglePanel(self, sender):
        self.panel.toggle()

    def interrupt(self, sender):
        _post("/api/control", {"action": "interrupt"})

    def resetChat(self, sender):
        _post("/api/control", {"action": "reset"})

    def quitApp(self, sender):
        os._exit(0)

    # ── state ────────────────────────────────────────────────────────────
    def _on_event(self, payload: Dict[str, Any]) -> None:
        """
        Manager events arrive on whatever thread produced them. AppKit is not
        thread-safe, so the actual drawing is bounced to the main thread.
        """
        try:
            event = payload.get("event")
            _log("event", event, payload.get("new_state", ""))
            if event == "state_changed":
                self._apply_state(payload.get("new_state", "idle"))
            elif event == "wake_detected":
                self._apply_state("listening")
        except Exception:
            traceback.print_exc()

    def _apply_state(self, state: str) -> None:
        _, words = STATE_LOOK.get(state, (GREY, state))
        if state in ("idle", "cold"):
            # It stays up. Going away entirely would leave nothing to click,
            # and something that is always in the same corner is easier to
            # find than something that appears only while it is busy.
            self._set_pill(headline="Hey Diablo", subtitle="ready", state=state)
        else:
            self._set_pill(headline=words, state=state)
        self.status_item.setTitle_(words.capitalize())

    def _set_pill(self, **payload):
        _log("_set_pill", payload)

        def apply():
            self.pill.show(**payload)
            self.wake()
        _on_main(apply)

    def signal(self, event: str, data: Dict[str, Any]) -> None:
        """A real thing happened (see notify). Main thread only."""
        presence = self.presence
        if event == "reading":
            presence.set_reading(data.get("depth", 0))
        elif event == "waiting":
            presence.set_waiting(data.get("waiting", False))
        elif event == "jobs":
            presence.set_jobs(data.get("running", 0))
        elif event in ("success", "failure", "interrupted"):
            presence.flash(event)
        elif event == "target":
            self.approach(**data)
        self.wake()

    # ── going somewhere ──────────────────────────────────────────────────
    def approach(self, x: float, y: float, label: str = "", seconds: float = 3.0,
                 spawn: Optional[Callable[[bool], None]] = None,
                 generation: Optional[int] = None) -> None:
        """
        Go and stand beside a target that has actually been found.

        The target is in Quartz space. Where to stand is the guidance
        geometry's choice, so he never covers what he is referring to. The
        overlay (the ring on the target and the line to him) is only started
        once he has arrived and settled, and then drawn without its own copy
        of him, because he is already there. He holds for as long as the
        overlay shows, then goes home.

        If there is nowhere to stand, or the turn this belonged to has been
        superseded by the time he arrives, the old behaviour stands in.
        """
        stand = motion.stand_for(x, y, label)
        if stand is None:
            if spawn is not None:
                spawn(True)
            return
        presence = self.presence

        def ready():
            if generation is not None and not _current_generation(generation):
                presence.return_home()
                self.wake()
                return
            if spawn is not None:
                spawn(False)
            presence.hold_position(seconds, then=presence.return_home)
            self.wake()

        presence.approach_target(stand, on_ready=ready)
        self.wake()

    # ── the clock ────────────────────────────────────────────────────────
    def _start_clock(self) -> None:
        """One display link on the orb's view; paused whenever nothing moves."""
        try:
            clock = _FrameClock.alloc().initWithHandler_(self._frame)
            self._clock_target = clock
            link = self.pill.view.displayLinkWithTarget_selector_(clock, "tick:")
            link.addToRunLoop_forMode_(NSRunLoop.mainRunLoop(), NSRunLoopCommonModes)
            link.setPaused_(True)
            self._link = link
        except Exception:
            traceback.print_exc()
            self._link = None
        self.wake()

    def wake(self) -> None:
        """Something changed: make sure the next display frame runs. Main thread."""
        if self._link is not None and self._link.isPaused():
            self._last = None
            self._link.setPaused_(False)

    def _frame(self, link) -> None:
        began = time.perf_counter()
        now = link.targetTimestamp()
        interval = (now - self._last) if self._last is not None else (1.0 / 120.0)
        self._last = now

        presence = self.presence
        if presence.visual in (motion.Visual.LISTENING, motion.Visual.ACTIVATING) \
                and self.manager is not None:
            try:
                self.pill.view.pushLevel_(min(1.0, self.manager.mic.audio_power_level * 3.2))
            except Exception:
                pass

        panel_moving = not presence.panel.at_rest()
        presence.step(interval)
        look = presence.look()
        self.pill.place(look)
        if panel_moving:
            self.panel.apply(look.panel)
        self.pill.view.setNeedsDisplay_(True)
        self._breathe(look)

        # Travel and the panel at the display's full rate; loops alone at 60,
        # which is plenty for a ring and half the work on a ProMotion panel.
        fast = not (presence.position.at_rest() and presence.panel.at_rest()
                    and presence.scale.at_rest())
        if fast != self._fast:
            self._fast = fast
            try:
                rate = (Quartz.CAFrameRateRangeMake(80, 120, 120) if fast
                        else Quartz.CAFrameRateRangeMake(30, 60, 60))
                link.setPreferredFrameRateRange_(rate)
            except Exception:
                pass

        self.stats.note(interval, time.perf_counter() - began)
        if not presence.busy():
            link.setPaused_(True)
            self._last = None
            if DEBUG:
                _log("clock paused", self.stats.summary())

    def _breathe(self, look) -> None:
        """
        At home and idle, a breath too slow to notice unless you look for it,
        run by the compositor: no frames drawn here, no Python per frame.
        Removed the moment he is anything but idle at home, easing back from
        wherever the breath was rather than snapping.
        """
        want = (look.visual is motion.Visual.IDLE and self.presence.phase == motion.HOME
                and not self.presence.reduced and not look.dim)
        if want == self._breathing:
            return
        layer = self.pill.view.layer()
        if layer is None:
            return
        self._breathing = want
        try:
            if want:
                breath = CABasicAnimation.animationWithKeyPath_("opacity")
                breath.setFromValue_(1.0)
                breath.setToValue_(0.93)
                breath.setDuration_(2.8)
                breath.setAutoreverses_(True)
                breath.setRepeatCount_(1e9)
                breath.setTimingFunction_(CAMediaTimingFunction.functionWithName_(
                    kCAMediaTimingFunctionEaseInEaseOut))
                layer.addAnimation_forKey_(breath, "breathe")
            else:
                shown = layer.presentationLayer()
                start = shown.opacity() if shown is not None else 1.0
                layer.removeAnimationForKey_("breathe")
                back = CABasicAnimation.animationWithKeyPath_("opacity")
                back.setFromValue_(start)
                back.setToValue_(1.0)
                back.setDuration_(motion.Timing.LOOK)
                layer.addAnimation_forKey_(back, "unbreathe")
        except Exception:
            traceback.print_exc()

    def _watch_reduce_motion(self) -> None:
        """Follow the system setting while running, not just at launch."""
        def changed(_note):
            self.presence.set_reduced(_reduce_motion())
            self.wake()
        try:
            self._reduce_observer = (NSWorkspace.sharedWorkspace().notificationCenter()
                                     .addObserverForName_object_queue_usingBlock_(
                                         NSWorkspaceAccessibilityDisplayOptionsDidChangeNotification,
                                         None, NSOperationQueue.mainQueue(), changed))
        except Exception:
            self._reduce_observer = None

    # ── Push to talk ─────────────────────────────────────────────────────

    def install_push_to_talk(self, on_start, on_stop) -> bool:
        """
        Watch for the modifiers being held, anywhere on the system.

        Two monitors, not one. The global monitor never fires while our own
        window is focused — that is documented behaviour, not a bug — so
        holding the key with the Diablo window in front would do nothing
        without the local one. The local monitor must return the event or the
        modifier is swallowed from the rest of the app.

        The key going down is itself an event the presence shows at once, on
        this thread, before the microphone has even been asked for: the
        acknowledgement must not wait for the audio side.
        """
        self._ptt_down = False
        self._ptt_start = on_start
        self._ptt_stop = on_stop

        def changed(event):
            try:
                engaged = _ptt_engaged(event.modifierFlags())
                if engaged and not self._ptt_down:
                    self._ptt_down = True
                    _note_key(self, True)
                    self._ptt_start()
                elif not engaged and self._ptt_down:
                    self._ptt_down = False
                    _note_key(self, False)
                    self._ptt_stop()
            except Exception:
                traceback.print_exc()
            return event

        try:
            self._ptt_global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskFlagsChanged, changed)
            self._ptt_local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskFlagsChanged, changed)
        except Exception as e:
            print(f"  [PTT] could not install the key watcher: {e}")
            return False

        # A global monitor is accepted and never fires without Accessibility,
        # which is the worst possible failure: a key that silently does
        # nothing. Say so instead.
        trusted = True
        try:
            from skills import permissions
            trusted = permissions.accessibility()
        except Exception:
            pass
        if trusted:
            print(f"  Hold {PTT_NAME} and speak.")
        else:
            print(f"  [PTT] {PTT_NAME} needs Accessibility, which is not granted.")
            print("        Open Diablo > Health and press Grant.")
        return trusted

    def run(self):
        _log("run(): showing the orb, starting the clock, entering the AppKit loop")
        # Show it immediately rather than waiting for the first state change,
        # so there is something to click from the moment the app opens.
        def first():
            self.pill.show(headline="Hey Diablo", subtitle="ready", state="idle")
            self._start_clock()
        _on_main(first)
        self.app.run()


def _note_key(shell, down: bool) -> None:
    """
    Show the key at once. Never allowed to stand between the key and the
    microphone: whatever goes wrong here, push to talk still starts.
    """
    presence = getattr(shell, "presence", None)
    if presence is None:
        return
    try:
        if down:
            presence.key_pressed()
        else:
            presence.key_released()
        shell.wake()
    except Exception:
        traceback.print_exc()


def _current_generation(generation: int) -> bool:
    """Is this still the interaction it was started for? The voice layer decides."""
    try:
        from skills import voice
        return voice.is_current_generation(generation)
    except Exception:
        return True


def _on_main(fn: Callable[[], None]) -> None:
    """
    Run a block on the main thread. AppKit will not tolerate anything else.

    The first version of this defined an NSObject subclass inside the function
    and called performSelectorOnMainThread on it. That registers a new
    Objective-C class named _Runner on every single call, which works exactly
    once and then silently stops — the pill never appeared and nothing was
    logged. NSOperationQueue takes a block directly and has none of that
    problem.
    """
    def guarded():
        try:
            fn()
        except Exception:
            traceback.print_exc()

    try:
        NSOperationQueue.mainQueue().addOperationWithBlock_(guarded)
    except Exception:
        traceback.print_exc()


def _post(path: str, body: Dict[str, Any]) -> None:
    url = os.getenv("DIABLO_UI_URL", "http://127.0.0.1:17494") + path
    try:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(request, timeout=3).read()
    except Exception:
        pass


def run(listen: Optional[Callable[[], None]] = None, manager=None,
        url: Optional[str] = None, push_to_talk=None) -> None:
    """
    Start the app. Blocks forever on the main thread.

    `listen` is the voice loop. It moves to a background thread because AppKit
    owns the main one, which is the reverse of how the bridge ran before.

    `push_to_talk` is a (start, stop) pair. Given, the shell watches the
    modifiers system-wide and calls them.
    """
    url = url or os.getenv("DIABLO_UI_URL", "http://127.0.0.1:17494")
    if listen is not None:
        threading.Thread(target=listen, name="diablo-voice", daemon=True).start()
    shell = Shell(url, manager=manager)
    if push_to_talk is not None:
        shell.install_push_to_talk(*push_to_talk)
    shell.run()


if __name__ == "__main__":
    from companion_manager import create_manager
    from skills import server

    manager = create_manager()
    manager.start()
    served = server.start()
    server.attach(manager=manager)
    print(f"  app shell on {served}")
    run(manager=manager, url=served or None)
