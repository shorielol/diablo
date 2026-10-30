#!/usr/bin/env python3
"""
Which application a how-to is about, and the real route to what it asks for.

Asked how to color grade in Final Cut Pro, Diablo taught a lesson about the
browser in front of it. Asked how to connect a Bluetooth device, it taught
against the Voicebox window. Asked how to open settings in Zen, it found no
gear and stopped. The fix is the same for all three: decide what the task is
about from the request and the Mac, then reach for the app's own commands.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import apps, ax, settings_panes, task_context  # noqa: E402

App = apps.App
REG = [App("Final Cut Pro", "com.apple.FinalCut", "/Applications/Final Cut Pro.app"),
       App("Zen", "app.zen-browser.zen", "/Applications/Zen.app"),
       App("Google Chrome", "com.google.Chrome", "/Applications/Google Chrome.app",
           ("Chrome",)),
       App("Notes", "com.apple.Notes", "/System/Applications/Notes.app"),
       App("Shortcuts", "com.apple.shortcuts", "/System/Applications/Shortcuts.app"),
       App("System Settings", "com.apple.systempreferences",
           "/System/Applications/System Settings.app")]
PANES = [settings_panes.Pane("Bluetooth", "com.apple.BluetoothSettings"),
         settings_panes.Pane("Keyboard", "com.apple.Keyboard-Settings.extension")]
FRONT = {"app": "Voicebox", "title": "Voicebox", "id": 7}

Item = ax.MenuItem
FCP_MENUS = [Item(("Edit", "Captions", "Add Caption"), True, "C", 2),
             Item(("Window", "Go To", "Color Inspector"), True, "6", 0),
             Item(("Edit", "Add Color Board"), True, "E", 2),
             Item(("File", "Import", "Captions…"), True, "", 0)]
ZEN_MENUS = [Item(("Zen", "Settings…"), True, ",", 0), Item(("File", "New Tab"), True, "T", 0)]


def about(text):
    return task_context.about(text, front=FRONT, registry=REG, panes=PANES)


def run():
    r = Results()

    r.head("The app a request names, not the window in front")
    with guard(r, "named"):
        for said, wanted in (
                ("how do I color grade on Final Cut Pro, can you show me?", "Final Cut Pro"),
                ("how do I open settings in the Zen browser?", "Zen"),
                ("show me how to add subtitles in final cut", "Final Cut Pro"),
                ("open a new tab on chrome", "Google Chrome"),
                ("show me how to make a checklist in the Notes app", "Notes"),
                ("how do I pin a note in Notes", "Notes")):
            got = about(said)
            r.check(f"{said!r} is about {wanted}",
                    got.app is not None and got.app.name == wanted and got.source == "named",
                    f"{got.source} {got.app.name if got.app else None}")

    r.head("A system feature is System Settings, at its pane")
    with guard(r, "pane"):
        got = about("can you show me how to connect a new Bluetooth device?")
        r.check("Bluetooth -> System Settings › Bluetooth",
                got.app is not None and got.app.name == "System Settings"
                and got.pane is not None and got.pane.name == "Bluetooth"
                and got.source == "pane", f"{got.source} {got.pane}")
        got = about("change keyboard shortcuts")
        r.check("'keyboard shortcuts' is the Keyboard pane, not the Shortcuts app",
                got.pane is not None and got.pane.name == "Keyboard", f"{got.source}")

    r.head("Ordinary words are not app names on their own")
    with guard(r, "common words"):
        for said in ("show me how to open a new note", "what does this say?",
                     "where are my shortcuts to work"):
            got = about(said)
            r.check(f"{said!r} stays with the window in front", got.source == "front",
                    f"{got.source} {got.app.name if got.app else None}")

    r.head("The real route, from the app's own menus")
    with guard(r, "menus"):
        fcp, zen = REG[0], REG[1]
        route = task_context.menu_route(fcp, "show me how to add a caption in Final Cut Pro",
                                        items=FCP_MENUS, chooser=None)
        r.check("words that match: Edit › Captions › Add Caption",
                route is not None and route.path == ("Edit", "Captions", "Add Caption"),
                str(route.path if route else None))
        route = task_context.menu_route(zen, "how do I open settings in the Zen browser?",
                                        items=ZEN_MENUS, chooser=None)
        r.check("the app's own name is not part of the command: Zen › Settings…",
                route is not None and route.path == ("Zen", "Settings…"),
                str(route.path if route else None))

        def chooser(request, lines):
            return "Edit › Captions › Add Caption"
        route = task_context.menu_route(fcp, "how do I add subtitles in Final Cut Pro",
                                        items=FCP_MENUS, chooser=chooser)
        r.check("other words: the model chooses among the real commands",
                route is not None and route.path == ("Edit", "Captions", "Add Caption"),
                str(route.path if route else None))

        def inventor(request, lines):
            return "Color › Color Wheels"
        route = task_context.menu_route(fcp, "show me the colour wheels in Final Cut Pro",
                                        items=FCP_MENUS, chooser=inventor)
        r.check("a command the app does not have is not accepted from the model",
                route is None, str(route.path if route else None))

        def no_answer(request, lines):
            return "none"
        route = task_context.menu_route(fcp, "how do I make coffee",
                                        items=FCP_MENUS, chooser=no_answer)
        r.check("and 'none' is none", route is None)

    r.head("A correction of the last how-to stays that how-to")
    with guard(r, "follow-ups"):
        import time
        from skills import delegation_tool as dt
        heard = []
        saved = (dt.handle, dict(dt._HOWTO), dt.conversation.session)

        class Ongoing:
            def __init__(self, ongoing):
                self.ongoing = ongoing

            def is_followup(self, generation=None):
                return self.ongoing
        try:
            dt.conversation.session = Ongoing(True)
            dt.handle = lambda text, speak, ask=None, confirm=None, generation=None: (
                heard.append(text) or "taught")
            dt._HOWTO.update(question="how do I color grade on Final Cut Pro?",
                             at=time.time())
            for said in ("no, I mean the actual color wheels", "where is that?",
                         "that's not what I see"):
                heard.clear()
                got = dt._follow_up(said, None, None, None, None)
                r.check(f"{said!r} is asked again with the original question",
                        got == "taught" and heard and heard[0].startswith(
                            "how do I color grade on Final Cut Pro?")
                        and said in heard[0], str(heard))
            r.check("and the question does not grow with each correction",
                    dt._HOWTO["question"] == "how do I color grade on Final Cut Pro?",
                    dt._HOWTO["question"])
            r.check("a new question is not taken for a follow-up",
                    dt._follow_up("open safari", None, None, None, None) is None)
            dt._HOWTO.update(at=time.time())
            dt.conversation.session = Ongoing(False)
            r.check("nor one in a new conversation",
                    dt._follow_up("where is that?", None, None, None, None) is None)
            dt.conversation.session = Ongoing(True)
            dt._HOWTO.update(at=time.time() - 3600)
            r.check("nor one long after the how-to",
                    dt._follow_up("where is that?", None, None, None, None) is None)
        finally:
            dt.handle = saved[0]
            dt.conversation.session = saved[2]
            dt._HOWTO.clear()
            dt._HOWTO.update(saved[1])

    return r.report("task context")


if __name__ == "__main__":
    sys.exit(run())
