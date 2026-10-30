"""
A small, real macOS application for Diablo to act on.

Not a mock. It is a genuine NSApplication with genuine NSButtons, it publishes
a genuine accessibility tree, and it only changes when a real mouse event
reaches it. That is exactly what the golden path needs to prove: a click that
merely updates Diablo's own state proves nothing at all.

Two buttons on purpose. "Mark Done" is the target; "Mark Done Later" exists so
that the ambiguity path has something real to be ambiguous about.

    python3 target_app.py            # run it, print every click on stdout
"""

import os
import sys

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSButton, NSMakeRect, NSTextField,
                    NSWindow, NSWindowStyleMaskTitled)

TITLE = "Diablo Target"


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    # The appearance is pinned to the variant. Unpinned, a light fixture on a
    # Mac in dark mode drew its button and checkbox titles white on its own
    # white background: Accessibility still saw them, and OCR and every vision
    # model were handed a picture with no Save button in it.
    from AppKit import NSAppearance
    app.setAppearance_(NSAppearance.appearanceNamed_(
        "NSAppearanceNameDarkAqua" if False else "NSAppearanceNameAqua"))

    sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
    import _layout as _place
    window = _place.new_window((500, 420, 420, 240), NSWindowStyleMaskTitled)
    window.setTitle_(TITLE)

    status = NSTextField.alloc().initWithFrame_(NSMakeRect(24, 36, 372, 30))
    status.setStringValue_("WAITING")
    status.setEditable_(False)
    status.setBezeled_(False)
    status.setDrawsBackground_(False)
    status.setFont_(status.font().fontWithSize_(20))
    window.contentView().addSubview_(status)

    class Handler(objc.lookUpClass("NSObject")):
        def done_(self, sender):
            status.setStringValue_("FINISHED")
            print("CLICKED Mark Done", flush=True)

        def later_(self, sender):
            status.setStringValue_("POSTPONED")
            print("CLICKED Mark Done Later", flush=True)

    handler = Handler.alloc().init()

    done = NSButton.alloc().initWithFrame_(NSMakeRect(24, 150, 170, 44))
    done.setTitle_("Mark Done")
    done.setTarget_(handler)
    done.setAction_("done:")
    window.contentView().addSubview_(done)

    later = NSButton.alloc().initWithFrame_(NSMakeRect(214, 150, 182, 44))
    later.setTitle_("Mark Done Later")
    later.setTarget_(handler)
    later.setAction_("later:")
    window.contentView().addSubview_(later)

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    # Where the two buttons actually are, for the perception corpus. Printed
    # before READY so every existing consumer, which reads until READY, is
    # unaffected.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _layout
    _layout.emit(window, [
        ("mark_done",       "button", "Mark Done",       "AXButton", done,  {}),
        ("mark_done_later", "button", "Mark Done Later", "AXButton", later, {}),
    ])
    print("READY", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
