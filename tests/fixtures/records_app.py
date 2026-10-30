"""
A small records application for multi-step tasks.

Four orders in a list. Opening one shows its detail: a Quantity field, a
Priority checkbox, Save and Back. Saving writes a line to the status log on
screen and prints it on stdout, so the harness judges a task by what the
application actually recorded, never by what Diablo says it did.

    python3 records_app.py [--confirm]

`--confirm` makes the first Save of each order ask "Confirm saving order N?"
with a Confirm button: an unexpected step the planner has to notice and deal
with, which is the recovery case.

Everything that happens is printed, one line each:

    LAYOUT {...}            on startup and after the view changes
    READY
    PRESSED <label>
    SAVED order=<id> quantity=<q> priority=<on|off>
"""

import os
import sys

from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSButton,
                    NSButtonTypeSwitch, NSColor, NSFont, NSMakeRect, NSTextField, NSView,
                    NSWindowStyleMaskTitled)
from Foundation import NSObject

TITLE = "Diablo Records"
WINDOW = (360.0, 260.0, 640.0, 420.0)
ORDERS = [("10481", "Northwind"), ("10482", "Acme Ltd"), ("10483", "Globex"),
          ("10484", "Initech")]
STATE = {"open": None, "confirmed": set(), "log": []}


def say(line: str) -> None:
    print(line, flush=True)


class Handler(NSObject):

    def open_(self, sender):
        say(f"PRESSED {sender.title()}")
        order = str(sender.title()).replace("Order ", "")
        STATE["open"] = order
        STATE["heading"].setStringValue_(f"Order {order} — {dict(ORDERS)[order]}")
        STATE["quantity"].setStringValue_("")
        STATE["priority"].setState_(0)
        # The status line used to keep saying "No order open." with an order
        # open, and the planner believed it. A fixture must not contradict
        # itself.
        status(f"Order {order} open.")
        show_detail(True)

    def save_(self, sender):
        say(f"PRESSED {sender.title()}")
        order = STATE["open"]
        if order is None:
            return
        if "--confirm" in sys.argv and order not in STATE["confirmed"]:
            STATE["status"].setStringValue_(f"Confirm saving order {order}?")
            STATE["confirm"].setHidden_(False)
            emit()
            return
        commit(order)

    def confirm_(self, sender):
        say(f"PRESSED {sender.title()}")
        order = STATE["open"]
        STATE["confirmed"].add(order)
        STATE["confirm"].setHidden_(True)
        commit(order)

    def back_(self, sender):
        say(f"PRESSED {sender.title()}")
        STATE["open"] = None
        status("No order open.")
        show_detail(False)

    def toggle_(self, sender):
        say(f"PRESSED {sender.title()}")


def status(fallback: str) -> None:
    """The last saves, if there are any; otherwise where the app is."""
    STATE["status"].setStringValue_("\n".join(STATE["log"][-3:]) or fallback)


def commit(order: str) -> None:
    quantity = str(STATE["quantity"].stringValue()).strip()
    priority = "on" if STATE["priority"].state() else "off"
    line = f"Saved order {order}: quantity {quantity or 'none'}, priority {priority}"
    STATE["log"].append(line)
    STATE["status"].setStringValue_("\n".join(STATE["log"][-3:]))
    say(f"SAVED order={order} quantity={quantity} priority={priority}")
    emit()


def show_detail(visible: bool) -> None:
    for key in ("heading", "quantity_label", "quantity", "priority", "save", "back"):
        STATE[key].setHidden_(not visible)
    if not visible:
        STATE["confirm"].setHidden_(True)
    emit()


def emit() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _layout
    rows = []
    for ident, kind, role in (("order_10481", "button", "AXButton"),
                              ("order_10482", "button", "AXButton"),
                              ("order_10483", "button", "AXButton"),
                              ("order_10484", "button", "AXButton"),
                              ("quantity", "text_field", "AXTextField"),
                              ("priority", "checkbox", "AXCheckBox"),
                              ("save", "button", "AXButton"),
                              ("back", "button", "AXButton"),
                              ("confirm", "button", "AXButton")):
        view = STATE[ident]
        if view.isHidden():
            continue
        label = "Quantity" if ident == "quantity" else str(view.title())
        rows.append((ident, kind, label, role, view, {"enabled": True}))
    _layout.emit(STATE["window"], rows)


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    from AppKit import NSAppearance
    app.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua"))

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _layout
    window = _layout.new_window(WINDOW, NSWindowStyleMaskTitled)
    window.setTitle_(TITLE)
    ground = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WINDOW[2], WINDOW[3]))
    ground.setWantsLayer_(True)
    ground.layer().setBackgroundColor_(NSColor.whiteColor().CGColor())
    window.setContentView_(ground)
    handler = Handler.alloc().init()
    STATE["handler"], STATE["window"] = handler, window

    def button(label, rect, action):
        b = NSButton.alloc().initWithFrame_(NSMakeRect(*rect))
        b.setTitle_(label)
        b.setTarget_(handler)
        b.setAction_(action)
        ground.addSubview_(b)
        return b

    def text(rect, value, size=14):
        t = NSTextField.alloc().initWithFrame_(NSMakeRect(*rect))
        t.setStringValue_(value)
        t.setEditable_(False)
        t.setBezeled_(False)
        t.setDrawsBackground_(False)
        t.setFont_(NSFont.systemFontOfSize_(size))
        t.setTextColor_(NSColor.blackColor())
        ground.addSubview_(t)
        return t

    text((20, 380, 200, 22), "Orders", 16)
    for i, (order, _customer) in enumerate(ORDERS):
        STATE[f"order_{order}"] = button(f"Order {order}", (20, 330 - i * 44, 160, 32),
                                         "open:")

    STATE["heading"] = text((220, 370, 400, 26), "", 16)
    STATE["quantity_label"] = text((220, 322, 80, 22), "Quantity")
    field = NSTextField.alloc().initWithFrame_(NSMakeRect(310, 320, 120, 24))
    field.setEditable_(True)
    field.setBezeled_(True)
    field.setTextColor_(NSColor.blackColor())
    field.setBackgroundColor_(NSColor.whiteColor())
    field.setAccessibilityLabel_("Quantity")
    ground.addSubview_(field)
    STATE["quantity"] = field
    box = NSButton.alloc().initWithFrame_(NSMakeRect(220, 280, 160, 24))
    box.setButtonType_(NSButtonTypeSwitch)
    box.setTitle_("Priority")
    box.setTarget_(handler)
    box.setAction_("toggle:")
    ground.addSubview_(box)
    STATE["priority"] = box
    STATE["save"] = button("Save", (220, 230, 90, 32), "save:")
    STATE["back"] = button("Back", (320, 230, 90, 32), "back:")
    STATE["confirm"] = button("Confirm", (420, 230, 100, 32), "confirm:")
    STATE["status"] = text((220, 40, 400, 150), "No order open.", 14)
    STATE["status"].setUsesSingleLineMode_(False)

    for key in ("heading", "quantity_label", "quantity", "priority", "save", "back",
                "confirm"):
        STATE[key].setHidden_(True)

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    emit()
    print("READY", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
