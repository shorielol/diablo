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
import queue
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
from skills import lesson  # noqa: E402
from skills import motion  # noqa: E402
# Where the conversation surface goes and how big it is at each level.
from skills import surface  # noqa: E402


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

class _SurfaceBridge(NSObject):
    """
    What the page tells the shell: which level it is at, how tall its
    content is, whether someone is using it, and when it is finished with.
    WebKit delivers these on the main thread; the panel checks their order.
    """

    def initWithPanel_(self, panel):
        self = objc.super(_SurfaceBridge, self).init()
        if self is None:
            return None
        self.panel = panel
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            body = message.body()
            report = {str(k): body[k] for k in body.keys()} if hasattr(body, "keys") else {}
            self.panel.report(report)
        except Exception:
            traceback.print_exc()


class _SurfaceDelegate(NSObject):
    """The window's own events: a person resizing, moving or closing it."""

    def initWithPanel_(self, panel):
        self = objc.super(_SurfaceDelegate, self).init()
        if self is None:
            return None
        self.panel = panel
        return self

    def windowShouldClose_(self, sender):
        # The red button folds it back into him like every other way out,
        # rather than making it vanish.
        self.panel.close()
        return False

    def windowWillStartLiveResize_(self, note):
        self.panel.live = True

    def windowDidEndLiveResize_(self, note):
        self.panel.live = False
        self.panel.resized_by_hand()

    def windowDidMove_(self, note):
        self.panel.moved()


class Panel:
    """
    Diablo's conversation surface. Built the first time it is needed, and
    not before: until then there is no web view and no 33 MB.

    It is one object at three sizes (see skills/surface.py): nothing but
    the orb; a compact surface as tall as one exchange; a larger work
    surface when the content needs it. The page decides which, from what it
    is showing, and reports it; the geometry decides what that means on this
    screen; the motion system moves between them.

    It comes out of the orb and folds back into him. Its bottom-right corner
    is anchored just above him and never moves on its own, so growing into
    the work surface and shrinking back both happen away from him, from the
    corner he is at. Opening, it rises the last few points out of him, grows
    from that corner and fades in; closing is the same, reversed and faster.
    A close overtaken by a reopen never hides the window.
    """

    # How far it travels and grows as it opens: enough to read as coming out
    # of the orb, not so much that it reads as sliding or zooming in.
    RISE = 14.0
    GROW = 0.04

    def __init__(self, url: str, presence=None, wake=None):
        self.url = url
        self.window = None
        self.webview = None
        self.container = None
        self.presence = presence
        self.wake = wake or (lambda: None)
        self.closing = False
        self.level = surface.INTERACTION
        self.content_height = surface.INTERACTION_MIN_HEIGHT
        self.last_seq = 0
        self.chosen_work = None     # the size someone dragged the work surface to
        self.detached = None        # the corner someone dragged it to, instead of his
        self.live = False           # a person is resizing it right now
        self.placing = False        # the frame change in progress is ours
        self.implicit = False       # opened by him for a spoken turn, not by you
        self.engaged = False        # someone is reading, hovering or typing in it
        self._dwell = 0             # owner token of the pending fold-away

    # ── building ─────────────────────────────────────────────────────────
    def _build(self):
        width, height = surface.INTERACTION_WIDTH, surface.INTERACTION_MIN_HEIGHT
        frame = NSMakeRect(0, 0, width, height)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskFullSizeContentView)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, style, NSBackingStoreBuffered, False)
        self.window.setTitle_("Diablo")
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(NSWindowTitleHidden)
        # The system's window colour, so light and dark follow the Mac; the
        # page paints over it with the same values once it has loaded.
        self.window.setBackgroundColor_(NSColor.windowBackgroundColor())
        # Released only by us: closing hides it, and the next open reuses
        # the same web view.
        self.window.setReleasedWhenClosed_(False)
        self.delegate = _SurfaceDelegate.alloc().initWithPanel_(self)
        self.window.setDelegate_(self.delegate)

        config = WKWebViewConfiguration.alloc().init()
        self.bridge = _SurfaceBridge.alloc().initWithPanel_(self)
        config.userContentController().addScriptMessageHandler_name_(self.bridge, "surface")
        # A layer-backed container holds the page, so the whole surface can
        # grow out of his corner in the compositor without the page laying
        # itself out again every frame.
        self.container = NSView.alloc().initWithFrame_(frame)
        self.container.setWantsLayer_(True)
        self.webview = WKWebView.alloc().initWithFrame_configuration_(frame, config)
        self.webview.setValue_forKey_(False, "drawsBackground")
        self.webview.setAutoresizingMask_(2 | 16)          # width and height sizable
        self.container.addSubview_(self.webview)
        self.window.setContentView_(self.container)
        self._chrome()
        self.webview.loadRequest_(
            NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url)))

    def _chrome(self):
        """
        The window's own controls follow the level. The compact surface is
        part of him: no traffic lights, no resize handle, as tall as what is
        in it. The work surface is a window you may arrange: all three
        buttons, and it resizes within bounds for this screen.
        """
        if self.window is None:
            return
        work = self.level == surface.WORK
        for button in (0, 1, 2):     # close, miniaturize, zoom
            control = self.window.standardWindowButton_(button)
            if control is not None:
                control.setHidden_(not work)
        mask = self.window.styleMask()
        mask = (mask | NSWindowStyleMaskResizable) if work else (mask & ~NSWindowStyleMaskResizable)
        self.window.setStyleMask_(mask)
        if work:
            low, high = surface.work_bounds(self._visible(), self._anchor())
            self.window.setMinSize_(NSMakeSize(*low))
            self.window.setMaxSize_(NSMakeSize(*high))

    # ── where ────────────────────────────────────────────────────────────
    def _screen(self):
        home = self.presence.home if self.presence is not None else None
        point = NSPoint(*home) if home else NSEvent.mouseLocation()
        return next((s for s in NSScreen.screens() if _contains(s.frame(), point)),
                    NSScreen.mainScreen())

    def _visible(self):
        v = self._screen().visibleFrame()
        return (v.origin.x, v.origin.y, v.size.width, v.size.height)

    def _anchor(self):
        """His corner: just above the orb's home, or wherever it was dragged to."""
        if self.detached is not None:
            return self.detached
        if self.presence is not None and self.presence.home:
            return surface.anchor_for(self.presence.home, CANVAS)
        v = self._visible()
        return (v[0] + v[2] - surface.INSET, v[1] + surface.INSET + CANVAS + surface.GAP)

    def _size(self):
        return surface.size_for(self.level, self.content_height, self._visible(),
                                self._anchor(), chosen=self.chosen_work)

    # ── opening and closing ──────────────────────────────────────────────
    def visible(self) -> bool:
        return self.window is not None and self.window.isVisible() and not self.closing

    def toggle(self):
        if self.visible():
            self.close()
        else:
            self.open()

    def open(self):
        """Opened by you: it takes the keyboard, and stays until you are done."""
        self.present(activate=True, implicit=False)

    def present(self, activate: bool = True, implicit: bool = False):
        """
        Bring the surface out of the orb. `activate=False` shows it without
        taking the keyboard from whatever you are working in — how it appears
        for a spoken turn, which you did not ask to type into.
        """
        if self.window is None:
            self._build()
        self._cancel_dwell()
        showing = self.visible()
        self.implicit = implicit and (not showing or self.implicit)
        self.closing = False
        if not self.window.isVisible():
            self.detached = None
            if self.presence is not None:
                # Closed some other way: start from closed, or the spring
                # would think it is already open and never run.
                self.presence.panel.jump(0.0)
                self.presence.size_panel(*self._size(), instant=True)
            self._chrome()
            self.apply(0.0)
        if activate:
            self.window.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        else:
            self.window.orderFrontRegardless()
        if self.presence is None:
            self.apply(1.0)
            return
        self.presence.open_panel()
        self.wake()

    def close(self):
        if self.window is None or not self.window.isVisible():
            return
        self._cancel_dwell()
        if self.presence is None:
            self.window.orderOut_(None)
            return
        self.closing = True
        self.presence.close_panel(on_done=self._closed)
        self.wake()

    def _closed(self, is_open):
        # Only ever reached for the newest close: a reopen during the close
        # took ownership, and this completion was dropped.
        if not is_open and self.window is not None:
            self.closing = False
            self.implicit = False
            self.engaged = False
            self.detached = None
            self.window.orderOut_(None)
            # The page forgets any level it was holding for this opening.
            self._script("window.diabloSurface && window.diabloSurface.closed()")

    # ── what the page reports ────────────────────────────────────────────
    def report(self, message: Dict[str, Any]) -> None:
        """
        One message from the page. Levels and heights arrive numbered and
        only a newer one is used, so a late report from before the last
        change can never shrink or grow the surface back.
        """
        if "engaged" in message:
            self.engaged = bool(message.get("engaged"))
            if self.engaged:
                self.implicit = False
                self._cancel_dwell()
        if message.get("close"):
            self.close()
            return
        if message.get("dismiss"):
            self.dwell(motion.Timing.CONFIRM_DWELL)
        seq = int(message.get("seq") or 0)
        if not seq or not surface.accept(seq, self.last_seq):
            return
        self.last_seq = seq
        level = message.get("level")
        if level not in (surface.INTERACTION, surface.WORK):
            return
        changed = level != self.level
        self.level = level
        try:
            self.content_height = float(message.get("height") or self.content_height)
        except (TypeError, ValueError):
            pass
        if changed:
            self._chrome()
        if self.window is not None and self.window.isVisible() and self.presence is not None \
                and not self.live:
            self.presence.size_panel(*self._size())
            self.wake()

    # ── a person arranging it ────────────────────────────────────────────
    def resized_by_hand(self):
        """Dragged to a size: that is the work surface's size from now on."""
        frame = self.window.frame()
        if self.level == surface.WORK:
            self.chosen_work = (frame.size.width, frame.size.height)
        self._adopt(frame)

    def moved(self):
        """Dragged somewhere: it stays there, anchored at its own corner, until closed."""
        if self.placing or self.live or self.window is None or not self.window.isVisible():
            return
        if self.presence is not None and not self.presence.panel.at_rest():
            return
        self._adopt(self.window.frame())

    def _adopt(self, frame):
        corner = (frame.origin.x + frame.size.width, frame.origin.y)
        if abs(corner[0] - self._anchor()[0]) > 1 or abs(corner[1] - self._anchor()[1]) > 1:
            self.detached = corner
        if self.presence is not None:
            self.presence.size_panel(frame.size.width, frame.size.height, instant=True)

    # ── folding away by itself ───────────────────────────────────────────
    def dwell(self, seconds: float) -> None:
        """
        Fold back into him after `seconds`, unless someone is using the
        surface by then or anything newer happens. A token, not a sleep: a
        later dwell, a present or a close makes this one void.
        """
        if not self.visible():
            return
        self._dwell += 1
        token = self._dwell

        def due(_timer):
            if token == self._dwell and not self.engaged and self.visible():
                self.close()
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(float(seconds), False, due)

    def _cancel_dwell(self):
        self._dwell += 1

    def turn_over(self) -> None:
        """A spoken turn has finished. If he opened the surface for it, let it go."""
        if self.implicit and self.visible():
            self.dwell(motion.Timing.SURFACE_DWELL)

    # ── drawing a frame ──────────────────────────────────────────────────
    def frame(self) -> Optional[tuple]:
        """Where it is right now, AppKit (x, y, w, h), or None when hidden."""
        if not self.visible():
            return None
        f = self.window.frame()
        return (f.origin.x, f.origin.y, f.size.width, f.size.height)

    def _script(self, source: str) -> None:
        if self.webview is not None:
            try:
                self.webview.evaluateJavaScript_completionHandler_(source, None)
            except Exception:
                pass

    def apply(self, progress, surface_alpha=1.0, size=None):
        """
        One frame: `progress` 0 is inside the orb and 1 is open; `size` is
        the surface's current size from the motion system; `surface_alpha`
        steps it back while Diablo is away at a target, so the eye goes to
        him, and all the way out when what he points at is under it.
        """
        if self.window is None or self.live:
            return
        progress = max(0.0, min(1.0, progress))
        reduced = self.presence is not None and self.presence.reduced
        if size is None or size[0] <= 0 or size[1] <= 0:
            size = self._size()
        x, y, w, h = surface.frame_for(size, self._anchor(), self._visible())
        # Whole points: the last fraction of a spring's tail is invisible on
        # screen and a fractional window frame is a blurred one.
        x, y, w, h = round(x), round(y), round(w), round(h)
        rise = 0.0 if reduced else (1.0 - progress) * Panel.RISE
        self.placing = True
        try:
            self.window.setFrame_display_(NSMakeRect(x, y - rise, w, h), True)
        finally:
            self.placing = False
        alpha = progress * max(0.0, min(1.0, surface_alpha))
        self.window.setAlphaValue_(alpha)
        # Out of the way means out of the way: an invisible surface must not
        # catch the click meant for what he is pointing at.
        self.window.setIgnoresMouseEvents_(alpha < 0.3)
        self._grow(1.0 if reduced else 1.0 - Panel.GROW * (1.0 - progress), w)

    def _grow(self, scale, width):
        """Scale the surface about its bottom-right corner, his corner."""
        layer = self.container.layer() if self.container is not None else None
        if layer is None:
            return
        t = Quartz.CATransform3DMakeTranslation(width, 0.0, 0.0)
        t = Quartz.CATransform3DScale(t, scale, scale, 1.0)
        t = Quartz.CATransform3DTranslate(t, -width, 0.0, 0.0)
        layer.setSublayerTransform_(t)


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
        # A lesson needs to know when the user actually did the thing. The
        # overlay can already report clicks on stdout and is click-through,
        # so the press still reaches the application underneath. Watching
        # from here rather than from a second overlay is what keeps one mark
        # on screen: the orb is the marker, and there is only ever one.
        self.watch = False
        self.clicks: "queue.Queue" = queue.Queue()
        self.watching = threading.Event()

    def _alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _read(self) -> None:
        """Drain the overlay's stdout: it says when it is watching, and clicks."""
        process = self.process

        def pump():
            try:
                for line in process.stdout:
                    try:
                        message = json.loads(line.strip())
                    except Exception:
                        continue
                    if message.get("watching"):
                        self.watching.set()
                    if "click" in message:
                        self.clicks.put(message["click"])
            except Exception:
                pass
        threading.Thread(target=pump, daemon=True).start()

    def ready(self, timeout: float = 5.0) -> bool:
        """Wait until the overlay's click monitor is actually installed."""
        if not self.watch:
            return False
        return self.watching.wait(timeout=timeout)

    def wait_for_click(self, x: float, y: float, radius: float = 44.0,
                       timeout: float = 90.0, should_stop=None) -> bool:
        """
        Wait until the user clicks near the target. True if they did.

        Clicks elsewhere are ignored rather than counted: during a lesson
        people click scrollbars and other windows, and none of that is the
        step being taken.
        """
        while True:
            try:
                self.clicks.get_nowait()
            except Exception:
                break
        deadline = time.time() + timeout
        while time.time() < deadline:
            if should_stop and should_stop():
                return False
            try:
                click = self.clicks.get(timeout=0.25)
            except Exception:
                continue
            if ((click["x"] - x) ** 2 + (click["y"] - y) ** 2) ** 0.5 <= radius:
                return True
        return False

    def _send(self, message: Dict[str, Any]) -> bool:
        if not self._alive():
            return False
        try:
            # The pipe is text mode now that the overlay talks back on stdout;
            # a bytes write would raise on every message.
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError, TypeError):
            self.process = None
            return False

    def prepare(self, x: float, y: float, label: str = "",
                w: float = 0.0, h: float = 0.0, watch: bool = False,
                step: str = "") -> None:
        """Have an overlay ready and invisible, so showing it costs nothing."""
        if self._alive():
            return
        try:
            import subprocess
            from skills import pointing
            command = [sys.executable, pointing.OVERLAY, "--session", "--hidden",
                       "--no-marker", "--marker-radius", f"{motion.STAND_RADIUS:.1f}",
                       "--x", f"{x:.1f}", "--y", f"{y:.1f}",
                       "--label", (label or "")[:48],
                       "--w", f"{w:.1f}", "--h", f"{h:.1f}"]
            if step:
                command += ["--step", step[:12]]
            if watch:
                command.append("--watch-clicks")
            self.watch = bool(watch)
            self.watching.clear()
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE,
                stdout=(subprocess.PIPE if watch else subprocess.DEVNULL),
                stderr=subprocess.DEVNULL, start_new_session=True, text=True)
            if watch:
                self._read()
        except Exception:
            traceback.print_exc()
            self.process = None

    def show(self, x: float, y: float, label: str = "",
             w: float = 0.0, h: float = 0.0, step: str = "",
             on_light: bool = False) -> None:
        """Indicate the target: fades in where it belongs, no slide."""
        self.prepare(x, y, label, w, h, step=step)
        self._send({"x": x, "y": y, "label": (label or "")[:80],
                    "w": w, "h": h, "fresh": True,
                    "step": step[:12], "on_light": bool(on_light)})

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
        # Which step of a walkthrough is current, and the refusal to have two.
        self.lesson = lesson.Lesson()
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
                state = payload.get("new_state", "idle")
                self._apply_state(state)
                if state == "idle":
                    _on_main(self.panel.turn_over)
            elif event == "wake_detected":
                self._apply_state("listening")
            elif event == "transcript_ready":
                # Something was said to him. The exchange comes out of the orb
                # where it can be read, without taking the keyboard from
                # whatever you were doing; typed turns already have it open.
                _on_main(self._present_turn)
        except Exception:
            traceback.print_exc()

    def _present_turn(self) -> None:
        if not self.panel.visible():
            self.panel.present(activate=False, implicit=True)
        else:
            self.panel._cancel_dwell()

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
        elif event == "focus":
            self.focus_content(**data)
        elif event == "unreadable":
            self.reading_failed()
        elif event == "lesson_begin":
            self.guide_begin(**data)
        elif event == "lesson_step":
            self.guide_step(**data)
        elif event == "lesson_waiting":
            self.guide_waiting(**data)
        elif event == "lesson_verified":
            self.guide_verified(**data)
        elif event == "lesson_unverified":
            self.guide_unverified(**data)
        elif event == "lesson_finish":
            self.guide_finish(**data)
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
        presence.covering = self._under_surface(x, y, w, h)
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

    def focus_content(self, x: float, y: float, w: float, h: float,
                      label: str = "", seconds: float = 0.0,
                      generation: Optional[int] = None) -> None:
        """
        Stand beside content he has actually read, and outline it.

        The same three beats as `approach`, and the same geometry — this is
        not a second way of going somewhere. What differs is what appears
        when he arrives: passing a width and height makes the overlay draw a
        soft outline round the region instead of a ring on a point. That is
        the whole distinction between "look at this" and "this is the thing
        you can press", and it is one parameter rather than a second visual
        language.

        The rectangle comes from `reading.focus_of`, which returns one only
        when the text actually groups into one region, so there is nothing
        here that can send him to an invented place. A region too large to
        mean anything never reaches this method; he stays home and the
        surface says what he is reading instead.
        """
        stand = motion.stand_for(x + w / 2.0, y + h / 2.0, label,
                                 extent=(w / 2.0 + 6.0, h / 2.0 + 6.0))
        if stand is None:
            return
        presence = self.presence
        # Already pointing at something, or on his way to: a read does not
        # interrupt an indication a person may still be reading.
        if presence.guiding or (presence.phase == motion.MOVING
                                and presence.motion_owner in presence._on_ready):
            return
        presence.covering = self._under_surface(x, y, w, h)
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

        # indicating=False: he is reading it, not pointing at it, so he keeps
        # the reading ring rather than taking the guide's look.
        presence.approach_target(stand, on_ready=ready, indicating=False,
                                 facing=_appkit(x + w / 2.0, y + h / 2.0))
        self.wake()

    # ── teaching ─────────────────────────────────────────────────────────
    #
    # A lesson is the walkthrough tutor.py already runs, made visible. Each
    # step arrives here as an event; this decides what is current and lets
    # `approach` do the moving, so there is no second way of travelling and
    # no second thing drawing.

    def guide_begin(self, total: int = 0, app: str = "",
                    seq: Optional[int] = None) -> None:
        """A walkthrough is starting. Anything showing belongs to the last one."""
        if not self.lesson.begin(total, app, seq):
            return
        self.guide.hide()
        self.wake()

    def guide_step(self, x: float, y: float, label: str = "", index: int = 1,
                   total: int = 1, say: str = "", w: float = 0.0, h: float = 0.0,
                   on_light: bool = False, found: bool = True,
                   seq: Optional[int] = None,
                   generation: Optional[int] = None) -> None:
        """
        Show one step: move, settle, then indicate. Never all at once.

        The old target stops being current before he leaves, so there is one
        ring on screen at a time — a second appearing while the first fades
        is the thing that makes a walkthrough impossible to follow.

        A step that was planned but never found on screen is not a place. He
        stays where he is and the surface says so, rather than travelling to
        (0, 0) and pointing at a corner with confidence.
        """
        step = lesson.Step(index=index, total=total, label=label, say=say,
                           x=x, y=y, w=w, h=h, on_light=on_light, found=found)
        if not self.lesson.present(step, seq):
            return
        # One current target: whatever is drawn now belongs to the step just
        # replaced, so it goes before anything else happens.
        self.guide.hide()
        if not step.placed:
            self.presence.await_user()
            self.wake()
            return

        presence = self.presence
        stand = motion.stand_for(x + w / 2.0, y + h / 2.0, label,
                                 extent=((w / 2.0 + 6.0, h / 2.0 + 6.0)
                                         if step.is_region else None))
        if stand is None:
            # On no display: nowhere honest to stand. Say it and stay.
            self.no_target()
            return
        presence.covering = self._under_surface(x, y, w, h)
        self.guide.prepare(x, y, label, w, h, watch=True, step=step.counter)

        def ready():
            # A step overtaken while he was still travelling: the lesson has
            # already moved on, so this arrival draws nothing.
            if self.lesson.current is not step:
                return
            if generation is not None and not _current_generation(generation):
                self.guide_finish(lesson.CANCELLED)
                return
            self.guide.show(x, y, label, w, h, step=step.counter, on_light=on_light)
            presence.hold_target(motion.Timing.GUIDE_HOLD, until_quiet=True)
            self.wake()

        presence.approach_target(stand, on_ready=ready, facing=_appkit(x, y))
        self.wake()

    def guide_waiting(self, seq: Optional[int] = None) -> None:
        """He has asked for the click. The mark says waiting, and holds."""
        if not self.lesson.waiting(seq):
            return
        self.presence.await_user()
        self.wake()

    def guide_verified(self, seq: Optional[int] = None) -> None:
        """The screen really changed. A short acknowledgement, then the next step."""
        if not self.lesson.verified(seq):
            return
        self.presence.verified()
        self.wake()

    def guide_unverified(self, note: str = "", seq: Optional[int] = None) -> None:
        """
        Asked, waited, and nothing changed.

        No flash of success, and the indication stays where it is: the step
        has not been taken, so the thing to look at has not changed either.
        """
        if not self.lesson.unverified(note, seq):
            return
        self.presence.await_user()
        self.wake()

    def guide_finish(self, why: str = lesson.DONE,
                     seq: Optional[int] = None) -> None:
        """Over, by completion or by cancellation. Clear, then home."""
        if not self.lesson.finish(why, seq):
            return
        self.clear_target()

    def reading_failed(self) -> None:
        """
        He could not read what he was asked to read.

        No movement and no outline: there is nothing to stand beside, and
        going anywhere would be the invention this build exists to avoid.
        The cue is the one `no_target` already uses; the sentence saying
        which kind of failure it was goes to the surface, not here.
        """
        self.no_target()

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

        presence.covering = self._under_surface(x, y, w, h)
        presence.approach_target(stand, on_ready=ready, indicating=False,
                                 facing=_appkit(x + w / 2.0, y + h / 2.0))
        self.wake()

    def _under_surface(self, x: float, y: float, w: float = 0.0, h: float = 0.0) -> bool:
        """
        Does what he is about to refer to (Quartz, top-left) lie under the
        open surface? Then the surface must get out of its way, not merely
        step back: the point of going there is that you can see it.
        """
        panel = getattr(self, "panel", None)
        frame = panel.frame() if panel is not None else None
        if frame is None:
            return False
        fx, fy, fw, fh = frame
        a, b = _appkit(x, y), _appkit(x + w, y + h)
        if a is None or b is None:
            return False
        (left, top), (right, bottom) = a, b     # AppKit: top is the larger y
        return not (right < fx or left > fx + fw or top < fy or bottom > fy + fh)

    def clear_target(self) -> None:
        """The indication has done its job: fade it, and go home."""
        # Anything that clears the indication ends the lesson too. Otherwise a
        # walkthrough interrupted by a pointing target would still think a
        # step was current, and the next arrival would draw for a lesson the
        # user had already left.
        if self.lesson.live:
            self.lesson.finish(lesson.CANCELLED)
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

        panel_moving = not (presence.panel.at_rest() and presence.surface.at_rest()
                            and presence.panel_w.at_rest() and presence.panel_h.at_rest())
        presence.step(interval)
        look = presence.look()
        self.pill.place(look)
        if panel_moving and self.panel.window is not None and self.panel.window.isVisible():
            self.panel.apply(look.panel, look.surface, look.panel_size)
        self.pill.view.setNeedsDisplay_(True)

        # Travel, the panel and its resizing at the display's full rate; loops
        # alone at 60, which is plenty for a ring and half the work on a
        # ProMotion panel.
        fast = not (presence.position.at_rest() and presence.panel.at_rest()
                    and presence.scale.at_rest() and presence.panel_w.at_rest()
                    and presence.panel_h.at_rest())
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
