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

# What each manager state is called, and the colour it used to be drawn in.
#
# The words are the tooltip, the menu bar status line and the accessibility
# label — the mark is never the only way to know the state. The colours no
# longer reach the screen except as the red of a fault: state is carried by
# the mark's own geometry now (see motion.SIGNAL).
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
# So: one mark, source and front, on a small disc, and nothing else. State is
# carried by the mark's own shape (see motion.SIGNAL) and, for anyone who
# cannot see it, by the accessibility label; the front stepping outward is
# what says "this is speaking to you" without a caption.

# ── Orb geometry: one source of truth ───────────────────────────────────────

ORB = motion.ORB    # the visible disc, in points; motion owns it (placement uses it)
HALO = 11.0         # room outside it: the growth when active, and the shadow
CANVAS = ORB + HALO * 2

# How much of the disc the mark spans. Enough to be read across a room, with
# a ring of quiet between the front and the rim at its widest.
MARK_FILL = 0.70


def _colour(rgb, alpha=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, alpha)


class OrbView(NSView):
    """
    Diablo, as a mark on a small disc.

    It draws exactly what the motion system says, and nothing on its own
    clock: every frame is one Look from skills.motion, and the shell's
    display link decides when there is a frame at all. At rest there are no
    frames — idle is still, because a presence that never stops moving is one
    you stop being able to look past.

    Colour is the system's, not ours: the mark is the label colour and the
    disc is the window background, so light and dark appearance, increased
    contrast and every wallpaper are handled by the platform. The only colour
    that means anything is red, and only for a fault.
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
        self.setAccessibilityRole_("AXButton")
        self.setAccessibilityLabel_("Diablo, ready")
        return self

    def isOpaque(self):
        return False

    def mouseDown_(self, event):
        if self.on_click is not None:
            self.on_click()

    def accessibilityPerformPress(self):
        if self.on_click is not None:
            self.on_click()
        return True

    def viewDidChangeEffectiveAppearance(self):
        # Light to dark (or back) changes the system colours this draws with;
        # there may be no frame coming, so ask for one.
        self.setNeedsDisplay_(True)

    def update_(self, payload):
        """The manager's state changed. The motion system decides the look."""
        state = payload.get("state", self.state)
        self.state = state
        self.accent, words = STATE_LOOK.get(state, (GREY, state))
        self.presence.set_runtime(state)
        self.tooltip = payload.get("headline") or words
        self.setToolTip_(self.tooltip)
        # The mark is never the only way to know the state.
        self.setAccessibilityLabel_(f"Diablo, {words}")
        self.setNeedsDisplay_(True)

    def animating(self) -> bool:
        """Does the current state have a loop of its own (not just a transition)?"""
        return self.presence.looping()

    def pushLevel_(self, level):
        # Heavily smoothed. The front drawing in is there to say "I can hear
        # you", not to plot the room: an unsmoothed level makes it shake.
        target = max(0.0, min(1.0, float(level)))
        self.level += (target - self.level) * 0.12

    _draws = 0

    def drawRect_(self, rect):
        OrbView._draws += 1
        look = self.presence.look()
        cx = cy = CANVAS / 2.0
        r = ORB / 2.0 * look.scale
        contrast = _increase_contrast()

        # The disc: the window background, nearly opaque, with the system's
        # own separator as its edge. It is what keeps the mark legible on any
        # wallpaper, and it is the only surface he has.
        disc = NSBezierPath.bezierPathWithOvalInRect_(
            NSMakeRect(cx - r, cy - r, r * 2, r * 2))
        NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.96).set()
        disc.fill()
        edge = (NSColor.labelColor().colorWithAlphaComponent_(0.45) if contrast
                else NSColor.separatorColor())
        edge.set()
        disc.setLineWidth_(1.0 if not contrast else 1.5)
        disc.stroke()

        # The mark. Its geometry is the state: the front's distance and
        # length, which way it faces, where it has travelled round to.
        radius = look.radius + 0.05 * look.rhythm
        radius -= 0.03 * self.level * look.rings.get("listen", 0.0)
        span = look.span
        if look.cue == "success":
            # Done: the front steps out and lengthens a little, once, and
            # settles. Never round the source: that would be another symbol.
            lift = math.sin(math.pi * look.cue_progress)
            radius += 0.05 * lift
            span += 20.0 * lift
        luminosity = 1.0 if contrast else look.luminosity
        ink = NSColor.labelColor().colorWithAlphaComponent_(luminosity)
        front = None
        if look.visual is motion.Visual.ERROR:
            front = NSColor.systemRedColor()
        size = ORB * MARK_FILL * look.scale
        make_icon.draw_signal(cx, cy, size, ink, radius=radius, span=span,
                              heading=look.lean, side=look.side, turn=look.turn,
                              front_ink=front, shift=look.face_dx / max(size, 1.0))


def _increase_contrast() -> bool:
    try:
        return bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldIncreaseContrast())
    except Exception:
        return False


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

    def apply(self, progress, surface=1.0):
        """
        One frame of opening or closing: 0 is inside the orb, 1 is open.
        `surface` steps the panel back while Diablo is away at a target, so
        the eye goes to him, not to a window in the corner.
        """
        if self.window is None or self.rest is None:
            return
        progress = max(0.0, min(1.0, progress))
        self.window.setAlphaValue_(progress * max(0.0, min(1.0, surface)))
        self.window.setFrameOrigin_(
            NSPoint(self.rest[0], self.rest[1] - (1.0 - progress) * Panel.RISE))


def _status_icon():
    """
    The menu bar glyph: the mark itself, drawn once, as a template image.

    Template mode is what makes it follow the menu bar's own tint in light
    and dark and when the bar is highlighted, instead of being a fixed colour.
    """
    size = 18.0
    image = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    image.lockFocus()
    # The mark's ink is about 57% of its box, so a 25-point box gives ~14
    # points of glyph: the height of the other icons in the bar.
    make_icon.draw_signal(size / 2.0, size / 2.0, 25.0, NSColor.blackColor(), weight=0.1)
    image.unlockFocus()
    image.setTemplate_(True)
    return image


# ─── The indication ──────────────────────────────────────────────────────────

class Guide:
    """
    What Diablo points with: the ring (or, for a region of content, an
    outline) on the target, the line from him to it, and the caption.

    One overlay process in session mode, owned here. It is started hidden
    while he is still travelling, shown only once he has arrived and
    settled, hidden the moment a newer target takes over, and asked to
    fade and exit when he goes home. Drawn without its own copy of him,
    because he is standing there.

    The overlay is a separate process for the reason it always was: the
    drawing cannot take the assistant down with it.
    """

    def __init__(self):
        self.process = None

    def _alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _send(self, message: Dict[str, Any]) -> bool:
        if not self._alive():
            return False
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            self.process.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            self.process = None
            return False

    def prepare(self, x: float, y: float, label: str = "",
                w: float = 0.0, h: float = 0.0) -> None:
        """Have an overlay ready and invisible, so showing it costs nothing."""
        if self._alive():
            return
        try:
            import subprocess
            from skills import pointing
            self.process = subprocess.Popen(
                [sys.executable, pointing.OVERLAY, "--session", "--hidden", "--no-marker",
                 "--marker-radius", f"{motion.STAND_RADIUS:.1f}",
                 "--x", f"{x:.1f}", "--y", f"{y:.1f}", "--label", (label or "")[:48],
                 "--w", f"{w:.1f}", "--h", f"{h:.1f}"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception:
            traceback.print_exc()
            self.process = None

    def show(self, x: float, y: float, label: str = "",
             w: float = 0.0, h: float = 0.0) -> None:
        """Indicate the target: fades in where it belongs, no slide."""
        self.prepare(x, y, label, w, h)
        self._send({"x": x, "y": y, "label": (label or "")[:80],
                    "w": w, "h": h, "fresh": True})

    def hide(self) -> None:
        self._send({"hide": True})

    def close(self) -> None:
        """Fade out and exit. The process ends itself once it has faded."""
        if self._send({"quit": True}):
            try:
                self.process.stdin.close()
            except Exception:
                pass
        self.process = None


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
        self.guide = Guide()
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
        elif event == "region":
            self.approach_region(**data)
        elif event == "no_target":
            self.no_target()
        elif event == "await_user":
            presence.await_user()
        elif event == "verified":
            presence.verified()
        elif event == "clear":
            self.clear_target()
        self.wake()

    # ── going somewhere ──────────────────────────────────────────────────
    def approach(self, x: float, y: float, label: str = "", seconds: float = 3.0,
                 spawn: Optional[Callable[[bool], None]] = None,
                 generation: Optional[int] = None, w: float = 0.0, h: float = 0.0) -> None:
        """
        Go and point at a target that has actually been found, in Quartz space.

        Three beats, never blended: MOVE (he travels, looking where he is
        going, and any older indication fades as he leaves), SETTLE (a small
        arrival), INDICATE (only now the ring, the line to him and the
        caption appear). Then he holds: at least `seconds`, so it can be
        read, and for as long as he is still talking about it. Then the
        indication fades and he goes home.

        Where he stands is the guidance geometry's choice, with his real size,
        so he never covers the target. If the target is on no display there
        is nowhere to stand, and that is reported rather than invented.
        `spawn` is the old overlay path, kept for callers without a shell.
        """
        extent = (w / 2.0 + 6.0, h / 2.0 + 6.0) if w > 0 and h > 0 else None
        stand = motion.stand_for(x, y, label, extent=extent)
        if stand is None:
            self.no_target()
            return
        presence = self.presence
        self.guide.hide()
        self.guide.prepare(x, y, label, w, h)

        def ready():
            if generation is not None and not _current_generation(generation):
                self.clear_target()
                return
            self.guide.show(x, y, label, w, h)
            presence.hold_target(max(seconds, motion.Timing.GUIDE_HOLD),
                                 then=self.clear_target, until_quiet=True)
            self.wake()

        presence.approach_target(stand, on_ready=ready, facing=_appkit(x, y))
        self.wake()

    def approach_region(self, x: float, y: float, w: float, h: float,
                        generation: Optional[int] = None) -> None:
        """
        Stand beside a region of content he is reading — a window, a table, a
        graph — without pretending it is something to press: no ring, no
        line, just him beside it wearing the reading ring. Quartz, top-left.

        Only when it means something. A region that fills most of its display
        is the whole screen, and walking to its edge says nothing; and a
        target he is already pointing at is not abandoned for a read.
        """
        presence = self.presence
        if presence.guiding or (presence.phase == motion.MOVING
                                and presence.motion_owner in presence._on_ready):
            return
        try:
            from skills import display
            screen = display.for_point(x + w / 2.0, y + h / 2.0)
        except Exception:
            screen = None
        if screen is None or w * h > 0.5 * screen.width * screen.height:
            return
        stand = motion.stand_for(x + w / 2.0, y + h / 2.0, "",
                                 extent=(w / 2.0 + 6.0, h / 2.0 + 6.0))
        if stand is None:
            return

        def ready():
            if generation is not None and not _current_generation(generation):
                presence.return_home()
                self.wake()
                return
            presence.hold_target(1.0, then=presence.return_home, until_quiet=True)
            self.wake()

        presence.approach_target(stand, on_ready=ready, indicating=False,
                                 facing=_appkit(x + w / 2.0, y + h / 2.0))
        self.wake()

    def clear_target(self) -> None:
        """The indication has done its job: fade it, and go home."""
        self.guide.close()
        self.presence.return_home()
        self.wake()

    def no_target(self) -> None:
        """
        Looked and found nothing to point at. A short warning cue where he
        is — never a wander to a made-up place — and home if he was away.
        """
        self.presence.flash("missed")
        if self.presence.phase != motion.HOME:
            self.clear_target()
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

        panel_moving = not (presence.panel.at_rest() and presence.surface.at_rest())
        presence.step(interval)
        look = presence.look()
        self.pill.place(look)
        if panel_moving and self.panel.window is not None and self.panel.window.isVisible():
            self.panel.apply(look.panel, look.surface)
        self.pill.view.setNeedsDisplay_(True)

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


def _appkit(x: float, y: float):
    """A Quartz point in AppKit space, through display.py's one flip."""
    try:
        from skills import display
        return display.quartz_to_appkit(x, y)
    except Exception:
        return None


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
