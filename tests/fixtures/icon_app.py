"""
A real application whose toolbar icons have no text, on purpose.

Accessibility will report four AXButtons with valid bounds and no title,
which is exactly the case OCR cannot touch and the case vision exists for.
The glyphs are drawn rather than loaded so the fixture has no assets and no
ambiguity about what each control is meant to be:

    1  gear        settings
    2  magnifier   search
    3  house       home
    4  gear        a second gear, so "the gear" is genuinely ambiguous

Button 5 is labelled "Save" and is the control that must NOT cost a vision
call: a named target is answered by accessibility alone.

    python3 icon_app.py
"""

import math
import sys

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSBezierPath, NSButton,
                    NSButtonTypeMomentaryChange, NSColor, NSImage, NSMakeRect,
                    NSMakePoint, NSTextField, NSWindow, NSWindowStyleMaskTitled)

TITLE = "Diablo Icons"
SIDE = 34.0


def _image(draw) -> NSImage:
    image = NSImage.alloc().initWithSize_((SIDE, SIDE))
    image.lockFocus()
    NSColor.blackColor().set()
    draw()
    image.unlockFocus()
    # Template images pick up the control tint; these must stay literal so the
    # capture shows the shape that was drawn.
    image.setTemplate_(False)
    return image


def _gear():
    centre, teeth, inner, outer = SIDE / 2.0, 8, 7.0, 12.5
    path = NSBezierPath.bezierPath()
    for i in range(teeth * 2):
        radius = outer if i % 2 == 0 else inner
        angle = math.pi * i / teeth
        point = NSMakePoint(centre + radius * math.cos(angle),
                            centre + radius * math.sin(angle))
        path.moveToPoint_(point) if i == 0 else path.lineToPoint_(point)
    path.closePath()
    path.fill()
    NSColor.whiteColor().set()
    NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(centre - 4.2, centre - 4.2, 8.4, 8.4)).fill()


def _magnifier():
    NSColor.blackColor().set()
    ring = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(8, 12, 15, 15))
    ring.setLineWidth_(3.0)
    ring.stroke()
    handle = NSBezierPath.bezierPath()
    handle.moveToPoint_(NSMakePoint(11.5, 14.5))
    handle.lineToPoint_(NSMakePoint(6.0, 8.0))
    handle.setLineWidth_(3.4)
    handle.stroke()


def _house():
    NSColor.blackColor().set()
    roof = NSBezierPath.bezierPath()
    roof.moveToPoint_(NSMakePoint(4, 17))
    roof.lineToPoint_(NSMakePoint(17, 29))
    roof.lineToPoint_(NSMakePoint(30, 17))
    roof.closePath()
    roof.fill()
    NSBezierPath.bezierPathWithRect_(NSMakeRect(9, 5, 16, 13)).fill()
    NSColor.whiteColor().set()
    NSBezierPath.bezierPathWithRect_(NSMakeRect(14.5, 5, 5, 8)).fill()


GLYPHS = [("gear", _gear), ("magnifier", _magnifier),
          ("house", _house), ("gear", _gear)]


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(460, 400, 460, 210), NSWindowStyleMaskTitled,
        NSBackingStoreBuffered, False)
    window.setTitle_(TITLE)
    window.setBackgroundColor_(NSColor.whiteColor())

    status = NSTextField.alloc().initWithFrame_(NSMakeRect(24, 30, 412, 28))
    status.setStringValue_("WAITING")
    status.setEditable_(False)
    status.setBezeled_(False)
    status.setDrawsBackground_(False)
    status.setFont_(status.font().fontWithSize_(19))
    # Explicit black, not the default label colour.
    #
    # labelColor follows the system appearance, so in dark mode it resolves to
    # near-white — and this window's background is set to white. The result is
    # white text on white: the field renders, occupies its frame, reports
    # itself to accessibility, and is invisible to a camera. It took a
    # blank-looking capture and a failing goal check to find.
    status.setTextColor_(NSColor.blackColor())
    window.contentView().addSubview_(status)

    class Handler(objc.lookUpClass("NSObject")):
        def tapped_(self, sender):
            name = GLYPHS[sender.tag()][0] if sender.tag() < len(GLYPHS) else "save"
            status.setStringValue_(f"PRESSED {name.upper()} {sender.tag()}")
            print(f"CLICKED {name} {sender.tag()}", flush=True)

    handler = Handler.alloc().init()

    for index, (name, draw) in enumerate(GLYPHS):
        button = NSButton.alloc().initWithFrame_(
            NSMakeRect(24 + index * 54, 120, 44, 44))
        button.setImage_(_image(draw))
        button.setTitle_("")                 # no text: invisible to OCR
        button.setBordered_(True)
        button.setTag_(index)
        button.setTarget_(handler)
        button.setAction_("tapped:")
        window.contentView().addSubview_(button)

    saved = NSButton.alloc().initWithFrame_(NSMakeRect(300, 120, 130, 44))
    saved.setTitle_("Save")
    saved.setTag_(99)
    saved.setTarget_(handler)
    saved.setAction_("tapped:")
    window.contentView().addSubview_(saved)

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    print("READY", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
