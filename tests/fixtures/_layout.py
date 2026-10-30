"""
What a fixture says about where it put its controls.

The perception corpus needs ground truth that does not come from the thing
being measured. A fixture is the only honest source for that: it chose the
rectangles, so it can report them, and perception then has to arrive at the
same rectangles through the Accessibility API, OCR or a vision model without
ever being told.

The conversion is the only fiddly part and it lives here so all three fixtures
do it identically:

    view bounds (the view's own space, bottom-left)
        → convertRect_toView_(None)      the window's space
        → convertRectToScreen_           AppKit screen space, bottom-left
        → main_height - (y + height)     Quartz screen space, top-left

Quartz because that is what `ax._frame`, `Candidate` and every coordinate in
Diablo's perception stack use. Getting this wrong flips the y axis and produces
a corpus that is wrong by exactly the height of the display, which is a very
loud failure and therefore a safe one.
"""

import json
import os
import sys


def _main_height() -> float:
    sys.path.insert(0, os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))
    from skills import display
    return display.main_height()


def new_window(rect, style, at=None):
    """
    The fixture's window: where the fixture says, or at DIABLO_FIXTURE_AT.

    `at` and DIABLO_FIXTURE_AT are "x,y" in AppKit screen coordinates
    (bottom-left origin). The window is created for the screen that holds that
    point; without naming the screen, AppKit moved a window asked for on the
    Retina display at x=-1512 back onto the main display. Unset, nothing
    changes: the window opens where every suite expects it.
    """
    from AppKit import NSBackingStoreBuffered, NSMakeRect, NSScreen, NSWindow
    x, y, width, height = rect
    place = at or os.getenv("DIABLO_FIXTURE_AT", "").strip()
    if place:
        x, y = (float(v) for v in str(place).split(","))
    home = next((screen for screen in NSScreen.screens()
                 if screen.frame().origin.x <= x < screen.frame().origin.x + screen.frame().size.width
                 and screen.frame().origin.y <= y < screen.frame().origin.y + screen.frame().size.height),
                None)
    if home is None:
        return NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, width, height), style, NSBackingStoreBuffered, False)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_screen_(
        NSMakeRect(x, y, width, height), style, NSBackingStoreBuffered, False, home)
    window.setFrameOrigin_((x, y))
    return window


def rect_in(window, view, rect=None) -> dict:
    """
    One rectangle of one view, in Quartz screen points.

    `rect` is in the view's own coordinates (bottom-left, like AppKit); leave
    it out for the whole view. Split out of `describe` so a fixture that draws
    rather than lays out controls — a diagram, a chart — can report where it
    put a shape without inventing a second conversion.
    """
    bounds = view.bounds() if rect is None else rect
    screen = window.convertRectToScreen_(view.convertRect_toView_(bounds, None))
    height = _main_height()
    box_height = float(screen.size.height)
    return {"x": round(float(screen.origin.x), 1),
            "y": round(height - (float(screen.origin.y) + box_height), 1),
            "width": round(float(screen.size.width), 1),
            "height": round(box_height, 1)}


def describe(window, controls) -> dict:
    """
    `controls` is a list of (id, kind, label, role, view, extra) tuples.

    `extra` is a dict merged into the record — enabled, checked, whatever the
    fixture knows and the Accessibility API is supposed to agree with.
    """
    out = []
    for ident, kind, label, role, view, extra in controls:
        out.append({
            "id": ident, "kind": kind, "label": label, "role": role,
            **rect_in(window, view),
            **(extra or {}),
        })
    return {"title": str(window.title()), "controls": out}


def emit(window, controls, **fields) -> dict:
    """
    Print the layout, then let the caller print READY.

    Printed before READY on purpose: every existing consumer reads lines until
    it sees READY and ignores anything else, so putting this first leaves
    `golden_path.py` and `test_vision_targets.py` exactly as they were.
    """
    layout = describe(window, controls)
    layout.update(fields)
    print("LAYOUT " + json.dumps(layout), flush=True)
    return layout
