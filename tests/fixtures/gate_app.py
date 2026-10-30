"""
A real window built to attack the grounding gate.

Everything grounding_gate has to refuse, on one window, as real AppKit
controls that Accessibility reports the way any application would:

    Save and Delete All side by side      a point on one, labelled the other
    Delete beside Delete All              a name inside a longer name
    Export, disabled                      present but not pressable
    a second Save in another group        two controls with one name
    an unlabelled gear                    a real control with no name to check
    a status line                         what was last pressed, for verification

and a way to change it after it has been looked at. Each line on stdin is a
command, applied on the main thread, answered with the new layout and DONE:

    hide <id>      show <id>      disable <id>      enable <id>
    move <id> <dx> <dy>           rename <id> <new label>

    python3 gate_app.py [--dark] [--at X,Y] [--title Suffix] [--only id,id]

`--at` places the window (AppKit screen coordinates, bottom-left), so a second
copy can be put on top of the first for the wrong-window case. Prints LAYOUT
then READY, like every fixture.
"""

import os
import sys
import threading

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSImage,
                    NSMakeRect, NSTextField, NSView, NSWindow, NSWindowStyleMaskTitled)
from Foundation import NSObject, NSTimer

TITLE = "Diablo Gate"
WINDOW = (420.0, 300.0, 520.0, 300.0)

CONTROLS = [
    # id           kind      label         rect (x, y, w, h), content-view space
    ("save",       "button", "Save",       (300, 120, 90, 32)),
    ("delete_all", "button", "Delete All", (180, 120, 110, 32)),
    ("delete",     "button", "Delete",     (70, 120, 100, 32)),
    ("export",     "button", "Export",     (400, 120, 100, 32)),
    ("save_other", "button", "Save",       (300, 40, 90, 32)),
    ("gear",       "icon",   "",           (24, 230, 32, 32)),
]
DISABLED = {"export"}

STATE = {}


class Handler(NSObject):
    """Presses, and the commands that change the window under test."""

    def initWithStatus_(self, status):
        self = objc.super(Handler, self).init()
        if self is None:
            return None
        self.status = status
        self.pending = []
        self.lock = threading.Lock()
        return self

    def pressed_(self, sender):
        name = str(sender.title()) or "the gear"
        self.status.setStringValue_(f"Last pressed: {name}")

    def poll_(self, _timer):
        with self.lock:
            work, self.pending = self.pending, []
        for command in work:
            apply(command)


def apply(command: str) -> None:
    parts = command.split()
    if len(parts) < 2 or parts[1] not in STATE["made"]:
        print(f"ERROR {command}", flush=True)
        return
    verb, ident = parts[0], parts[1]
    view = STATE["made"][ident]
    if verb == "hide":
        view.setHidden_(True)
    elif verb == "show":
        view.setHidden_(False)
    elif verb == "disable":
        view.setEnabled_(False)
    elif verb == "enable":
        view.setEnabled_(True)
    elif verb == "move" and len(parts) == 4:
        frame = view.frame()
        view.setFrameOrigin_((frame.origin.x + float(parts[2]),
                              frame.origin.y - float(parts[3])))
    elif verb == "rename" and len(parts) >= 3:
        view.setTitle_(" ".join(parts[2:]))
    else:
        print(f"ERROR {command}", flush=True)
        return
    emit()
    print(f"DONE {command}", flush=True)


def emit() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _layout
    rows = []
    for ident, kind, _label, _rect in STATE["controls"]:
        view = STATE["made"][ident]
        if view.isHidden():
            continue
        rows.append((ident, kind, str(view.title()), "AXButton", view,
                     {"enabled": bool(view.isEnabled())}))
    _layout.emit(STATE["window"], rows, dark=STATE["dark"])


def read_commands(handler) -> None:
    for line in sys.stdin:
        line = line.strip()
        if line:
            with handler.lock:
                handler.pending.append(line)


def main() -> int:
    args = sys.argv[1:]
    dark = "--dark" in args
    at, suffix, only = None, "", None
    for i, arg in enumerate(args):
        if arg == "--at" and i + 1 < len(args):
            at = tuple(float(v) for v in args[i + 1].split(","))
        if arg == "--title" and i + 1 < len(args):
            suffix = " " + args[i + 1]
        if arg == "--only" and i + 1 < len(args):
            only = set(args[i + 1].split(","))
    controls = [c for c in CONTROLS if only is None or c[0] in only]

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    # Pinned, as in every fixture: an unpinned light window on a dark Mac drew
    # its button titles white on white.
    from AppKit import NSAppearance
    app.setAppearance_(NSAppearance.appearanceNamed_(
        "NSAppearanceNameDarkAqua" if dark else "NSAppearanceNameAqua"))

    sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
    import _layout as _place
    window = _place.new_window(WINDOW, NSWindowStyleMaskTitled,
                               at=f"{at[0]},{at[1]}" if at else None)
    window.setTitle_(TITLE + suffix + (" Dark" if dark else ""))
    ground = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WINDOW[2], WINDOW[3]))
    ground.setWantsLayer_(True)
    ground.layer().setBackgroundColor_(
        (NSColor.blackColor() if dark else NSColor.whiteColor()).CGColor())
    window.setContentView_(ground)

    status = NSTextField.alloc().initWithFrame_(NSMakeRect(70, 240, 400, 24))
    status.setStringValue_("Last pressed: nothing")
    status.setEditable_(False)
    status.setBezeled_(False)
    status.setDrawsBackground_(False)
    status.setFont_(NSFont.systemFontOfSize_(16))
    status.setTextColor_(NSColor.whiteColor() if dark else NSColor.blackColor())
    ground.addSubview_(status)

    handler = Handler.alloc().initWithStatus_(status)
    made = {}
    for ident, kind, label, box in controls:
        button = NSButton.alloc().initWithFrame_(NSMakeRect(*box))
        if kind == "icon":
            # A symbol image publishes its own description ("Gear shape") as
            # the button's name, which made this a named control. Cleared, so
            # the gear really has no name to check, like many icon buttons.
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "gearshape", None)
            image.setAccessibilityDescription_("")
            button.setImage_(image)
            button.setTitle_("")
            button.setBordered_(False)
        else:
            button.setTitle_(label)
        button.setEnabled_(ident not in DISABLED)
        button.setTarget_(handler)
        button.setAction_("pressed:")
        ground.addSubview_(button)
        made[ident] = button

    STATE.update(made=made, controls=controls, window=window, dark=dark)
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    emit()
    print("READY", flush=True)

    threading.Thread(target=read_commands, args=(handler,), daemon=True).start()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.05, handler, "poll:", None, True)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
