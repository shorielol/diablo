"""
A Notes-like window, for "show me how to open a new note".

Built from the screen where the failure was seen: a note called "Diablo Notes"
is open, its name is printed large at the top of the note and again as a
clickable title, and the control that makes a NEW note is a pencil in the
toolbar with no text on it at all. Asked how to open a new note, Diablo rang
the title. Every distractor here is about notes and none of them makes one:

    title_button   an AXButton reading "Diablo Notes" — a real control that
                   names the subject and performs nothing asked for
    note_title     the same words, large, as the note's heading
    folder         "Notes", the sidebar folder
    sync_line      a sentence that mentions new notes
    lock           a disabled "Lock Note" button

and the one right answer:

    new_note       the pencil, AX description "New Note", no visible text

Nothing is ever clicked by anything that reads this; the handler only reports.

    python3 notes_app.py
"""

import os
import sys

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular,
                    NSBackingStoreBuffered, NSBezierPath, NSButton, NSColor,
                    NSFont, NSImage, NSMakePoint, NSMakeRect, NSSearchField,
                    NSTextField, NSWindow, NSWindowStyleMaskTitled)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _layout  # noqa: E402

TITLE = "Notes"
SIDE = 28.0


def _image(draw) -> NSImage:
    image = NSImage.alloc().initWithSize_((SIDE, SIDE))
    image.lockFocus()
    NSColor.darkGrayColor().set()
    draw()
    image.unlockFocus()
    return image


def _pencil():
    # A square with a pencil across its corner: the usual compose glyph.
    box = NSBezierPath.bezierPathWithRect_(NSMakeRect(4, 4, 18, 18))
    box.setLineWidth_(1.8)
    box.stroke()
    pencil = NSBezierPath.bezierPath()
    pencil.moveToPoint_(NSMakePoint(11, 10))
    pencil.lineToPoint_(NSMakePoint(25, 24))
    pencil.setLineWidth_(3.4)
    pencil.stroke()


def _trash():
    NSBezierPath.bezierPathWithRect_(NSMakeRect(8, 4, 12, 16)).stroke()
    lid = NSBezierPath.bezierPath()
    lid.moveToPoint_(NSMakePoint(6, 21))
    lid.lineToPoint_(NSMakePoint(22, 21))
    lid.setLineWidth_(2.0)
    lid.stroke()


def _label(text, frame, size=13.0, bold=False, colour=None):
    field = NSTextField.alloc().initWithFrame_(frame)
    field.setStringValue_(text)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                   else NSFont.systemFontOfSize_(size))
    if colour is not None:
        field.setTextColor_(colour)
    return field


def _origin(x: float, y: float):
    """`FIXTURE_ORIGIN="x,y"` places the window without touching the pointer."""
    raw = os.getenv("FIXTURE_ORIGIN", "")
    if raw:
        try:
            left, bottom = [float(v) for v in raw.split(",")[:2]]
            return (left, bottom)
        except ValueError:
            pass
    return (x, y)


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(*_origin(300.0, 220.0), 900, 560), NSWindowStyleMaskTitled,
        NSBackingStoreBuffered, False)
    window.setTitle_(TITLE)
    window.setBackgroundColor_(NSColor.whiteColor())
    content = window.contentView()

    class Handler(objc.lookUpClass("NSObject")):
        def tapped_(self, sender):
            print(f"CLICKED {sender.tag()}", flush=True)

    handler = Handler.alloc().init()

    def button(frame, title="", image=None, label="", tag=0, enabled=True,
               bordered=True):
        made = NSButton.alloc().initWithFrame_(frame)
        made.setTitle_(title)
        if image is not None:
            made.setImage_(image)
        if label:
            made.setAccessibilityLabel_(label)
        made.setBordered_(bordered)
        made.setEnabled_(enabled)
        made.setTag_(tag)
        made.setTarget_(handler)
        made.setAction_("tapped:")
        content.addSubview_(made)
        return made

    # Sidebar and note list.
    folder = _label("Notes", NSMakeRect(20, 480, 150, 22), size=13.0, bold=True)
    content.addSubview_(folder)
    content.addSubview_(_label("iCloud", NSMakeRect(20, 506, 150, 20), size=11.0,
                               colour=NSColor.grayColor()))
    title_button = button(NSMakeRect(190, 470, 200, 30), title="Diablo Notes", tag=1,
                          bordered=False)
    content.addSubview_(_label("Meeting notes for Thursday", NSMakeRect(196, 448, 220, 20),
                               size=11.0, colour=NSColor.grayColor()))

    # Toolbar: the pencil is the only thing that makes a note.
    new_note = button(NSMakeRect(700, 512, 34, 30), image=_image(_pencil),
                      label="New Note", tag=2)
    trash = button(NSMakeRect(650, 512, 34, 30), image=_image(_trash),
                   label="Delete", tag=3)
    lock = button(NSMakeRect(540, 512, 96, 30), title="Lock Note", tag=4,
                  enabled=False)
    search = NSSearchField.alloc().initWithFrame_(NSMakeRect(750, 514, 130, 26))
    search.setPlaceholderString_("Search")
    content.addSubview_(search)

    # The open note.
    note_title = _label("Diablo Notes", NSMakeRect(440, 440, 400, 40), size=26.0,
                        bold=True)
    content.addSubview_(note_title)
    body = ["Meeting notes for Thursday", "Agenda: grounding, guidance, voice",
            "New notes sync across your devices automatically"]
    lines = []
    for index, text in enumerate(body):
        line = _label(text, NSMakeRect(440, 400 - index * 28, 420, 22))
        content.addSubview_(line)
        lines.append(line)

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    controls = [
        ("new_note", "icon", "New Note", "AXButton", new_note, {"answer": True}),
        ("title_button", "button", "Diablo Notes", "AXButton", title_button, {}),
        ("trash", "icon", "Delete", "AXButton", trash, {}),
        ("lock", "button", "Lock Note", "AXButton", lock, {"enabled": False}),
        ("search", "field", "Search", "AXTextField", search, {}),
    ]
    _layout.emit(window, controls, labels=[
        {"id": "note_title", "text": "Diablo Notes", **_layout.rect_in(window, note_title)},
        {"id": "folder", "text": "Notes", **_layout.rect_in(window, folder)},
        {"id": "sync_line", "text": body[2], **_layout.rect_in(window, lines[2])},
    ])
    print("READY", flush=True)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
