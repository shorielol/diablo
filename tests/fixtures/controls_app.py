"""
A real application holding the control types the other two fixtures cannot show.

`target_app.py` has two labelled buttons with confusable names. `icon_app.py`
has four unlabelled icons and one labelled button. Between them they cover
labelled, unlabelled, icon, duplicate and visually-similar targets, and nothing
else — so the perception corpus had no text field, no checkbox, no disabled
control, no selected control and nothing sitting against a window edge.

This is that, and nothing more. It is a third fixture rather than an extension
of the other two because both are already asserted against by name in
`test_vision_targets.py` and `golden_path.py`, and a corpus is a bad reason to
disturb a suite that clicks a real mouse.

    python3 controls_app.py              light background
    python3 controls_app.py --dark       dark background

On startup it prints its own layout as one JSON line, then READY. The layout is
the corpus's ground truth: these rectangles are where the application was told
to put its controls, converted to Quartz space through AppKit. Perception has
to arrive at them independently, through the Accessibility API, OCR or vision.

    LAYOUT {"title": "...", "controls": [...]}
    READY
"""

# json is used by _layout, not here
import sys

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSButton, NSButtonTypeSwitch,
                    NSColor, NSMakeRect, NSTextField, NSView, NSWindow,
                    NSWindowStyleMaskTitled)

TITLE = "Diablo Controls"

# The window, in AppKit screen space. Fixed so a capture of it is the same
# rectangle every run, which is what makes the corpus deterministic.
WINDOW = (420.0, 300.0, 520.0, 360.0)

# Every control, in the content view's own coordinates: AppKit, bottom-left
# origin. `kind` is what the corpus calls the target type; `role` is what
# Accessibility should report if it is behaving.
#
# Two of these sit hard against the content view's edges on purpose — the
# audit's corpus needed a target near a boundary, and a control whose frame
# touches the window edge is the case where a crop or a clamp goes wrong.
CONTROLS = [
    # id             kind          label                 rect (x, y, w, h)
    ("name_field",   "text_field", "",                   (24, 292, 300, 24)),
    ("email_field",  "text_field", "",                   (24, 252, 300, 24)),
    ("subscribe",    "checkbox",   "Subscribe",          (24, 210, 160, 22)),
    ("notify",       "checkbox",   "Notify me",          (24, 180, 160, 22)),
    ("save",         "button",     "Save",               (24, 130, 90, 30)),
    ("save_copy",    "button",     "Save a Copy",        (124, 130, 130, 30)),
    ("submit",       "button",     "Submit",             (264, 130, 90, 30)),
    ("archived",     "button",     "Archive",            (24, 88, 90, 30)),
    ("edge_left",    "button",     "Left Edge",          (0, 44, 96, 30)),
    ("edge_right",   "button",     "Right Edge",         (424, 44, 96, 30)),
    ("edge_bottom",  "button",     "Bottom Edge",        (200, 0, 120, 30)),
]

# "Archive" is disabled and "Notify me" is on, so the corpus has a case for
# each. Both are states Accessibility publishes and neither OCR nor vision can
# see reliably, which is the point of measuring the sources separately.
DISABLED = {"archived"}
CHECKED = {"notify"}

ROLES = {"text_field": "AXTextField", "checkbox": "AXCheckBox", "button": "AXButton"}


def main() -> int:
    dark = "--dark" in sys.argv
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    # The appearance is pinned to the variant. Unpinned, a light fixture on a
    # Mac in dark mode drew its button and checkbox titles white on its own
    # white background: Accessibility still saw them, and OCR and every vision
    # model were handed a picture with no Save button in it.
    from AppKit import NSAppearance
    app.setAppearance_(NSAppearance.appearanceNamed_(
        "NSAppearanceNameDarkAqua" if dark else "NSAppearanceNameAqua"))

    sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
    import _layout as _place
    window = _place.new_window(WINDOW, NSWindowStyleMaskTitled)
    window.setTitle_(TITLE + (" Dark" if dark else ""))

    ground = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WINDOW[2], WINDOW[3]))
    ground.setWantsLayer_(True)
    back = NSColor.blackColor() if dark else NSColor.whiteColor()
    ground.layer().setBackgroundColor_(back.CGColor())
    window.setContentView_(ground)

    ink = NSColor.whiteColor() if dark else NSColor.blackColor()
    made = {}

    for ident, kind, label, rect in CONTROLS:
        if kind == "text_field":
            field = NSTextField.alloc().initWithFrame_(NSMakeRect(*rect))
            # Explicit colours in both directions. labelColor follows the system
            # appearance, which on a background this fixture sets itself has
            # already produced white text on white once in this project.
            field.setStringValue_("")
            field.setTextColor_(ink)
            field.setBackgroundColor_(
                NSColor.darkGrayColor() if dark else NSColor.whiteColor())
            field.setBezeled_(True)
            field.setEditable_(True)
            ground.addSubview_(field)
            made[ident] = field
        elif kind == "checkbox":
            box = NSButton.alloc().initWithFrame_(NSMakeRect(*rect))
            box.setButtonType_(NSButtonTypeSwitch)
            box.setTitle_(label)
            box.setState_(1 if ident in CHECKED else 0)
            ground.addSubview_(box)
            made[ident] = box
        else:
            button = NSButton.alloc().initWithFrame_(NSMakeRect(*rect))
            button.setTitle_(label)
            button.setEnabled_(ident not in DISABLED)
            ground.addSubview_(button)
            made[ident] = button

    # ── Ground truth ────────────────────────────────────────────────────────
    # Shared with the other two fixtures so the conversion is identical
    # everywhere. Nothing here consults Accessibility, which is the source
    # being measured.
    import os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import _layout

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    _layout.emit(window, [
        (ident, kind, label, ROLES[kind], made[ident],
         {"enabled": ident not in DISABLED, "checked": ident in CHECKED})
        for ident, kind, label, _rect in CONTROLS
    ], dark=dark)
    print("READY", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
