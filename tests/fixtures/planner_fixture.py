"""
Ten labelled buttons, each producing a distinct, observable result.

Built for the planner benchmark and nothing else. The existing fixtures could
not supply a pristine corpus: `target_app` offers two achievable goals,
`icon_app` one, and `controls_app` none at all because no click there changes
any text on screen. Neither of the other two can be extended, because the
perception baseline in `evals/perception_baseline_v1.json` is measured against
their exact contents.

Every control here is:

  labelled          so accessibility alone resolves it, which measured 100%
                    precision on what it answers
  distinctly named  so the goal implies one control and no other
  enabled           so a single click really does reach the goal
  observable        each press writes its own word to the status line, so the
                    goal is a state a click produces rather than a phrase that
                    was already true

Two pairs are deliberately confusable — "Save Draft" against "Save Draft Copy",
and "Cancel Order" against "Confirm Order" — because a benchmark where no wrong
answer is tempting does not measure target selection.

    python3 planner_fixture.py

Prints its layout, then READY, the same protocol as the other fixtures.
"""

import sys

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSButton, NSColor, NSMakeRect,
                    NSTextField, NSWindow, NSWindowStyleMaskTitled)

TITLE = "Diablo Planner Fixture"
WINDOW = (380.0, 260.0, 560.0, 380.0)

# id, label, the word the status line shows once it is pressed, rect
BUTTONS = [
    ("send_invoice",   "Send Invoice",     "SENT",      (24, 300, 240, 32)),
    ("save_draft",     "Save Draft",       "SAVED",     (288, 300, 240, 32)),
    ("save_draft_copy", "Save Draft Copy", "COPIED",    (24, 256, 240, 32)),
    ("delete_row",     "Delete Row",       "DELETED",   (288, 256, 240, 32)),
    ("add_row",        "Add Row",          "ADDED",     (24, 212, 240, 32)),
    ("export_pdf",     "Export PDF",       "EXPORTED",  (288, 212, 240, 32)),
    ("print_preview",  "Print Preview",    "PREVIEWED", (24, 168, 240, 32)),
    ("refresh_data",   "Refresh Data",     "REFRESHED", (288, 168, 240, 32)),
    ("cancel_order",   "Cancel Order",     "CANCELLED", (24, 124, 240, 32)),
    ("confirm_order",  "Confirm Order",    "CONFIRMED", (288, 124, 240, 32)),
]


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(*WINDOW), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False)
    window.setTitle_(TITLE)
    window.setBackgroundColor_(NSColor.whiteColor())

    status = NSTextField.alloc().initWithFrame_(NSMakeRect(24, 56, 504, 34))
    status.setStringValue_("WAITING")
    status.setEditable_(False)
    status.setBezeled_(False)
    status.setDrawsBackground_(False)
    status.setFont_(status.font().fontWithSize_(22))
    # Explicit black. labelColor follows the system appearance and resolves to
    # near-white in dark mode, on a background this fixture sets to white.
    status.setTextColor_(NSColor.blackColor())
    window.contentView().addSubview_(status)

    results = {ident: word for ident, _label, word, _rect in BUTTONS}

    class Handler(objc.lookUpClass("NSObject")):
        def pressed_(self, sender):
            ident = list(results)[sender.tag()]
            status.setStringValue_(results[ident])
            print(f"CLICKED {ident}", flush=True)

    handler = Handler.alloc().init()

    made = {}
    for index, (ident, label, _word, rect) in enumerate(BUTTONS):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(*rect))
        button.setTitle_(label)
        button.setTag_(index)
        button.setTarget_(handler)
        button.setAction_("pressed:")
        window.contentView().addSubview_(button)
        made[ident] = button

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _layout
    _layout.emit(window, [
        (ident, "button", label, "AXButton", made[ident], {"produces": word})
        for ident, label, word, _rect in BUTTONS
    ])
    print("READY", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
