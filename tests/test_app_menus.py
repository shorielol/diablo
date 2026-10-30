#!/usr/bin/env python3
"""
An application's own menus, as a route to what a person asks for.

"how do I open settings in Zen" failed because Diablo looked for a gear and,
finding none, had nothing else to try. Every Mac app publishes its commands in
its menu bar, with their exact names and shortcuts, whether or not anything
on screen looks like them. These checks hold the menu route to that: find a
command by what it does, say exactly where it is, and carry it out only
through the action layer, with permission.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import app_menus, apps, ax, computer_use as cu  # noqa: E402

Item = ax.MenuItem


def tree(app_name):
    """A browser's menus, the way accessibility publishes them."""
    return [
        Item((app_name, f"About {app_name}"), True, "", 0),
        Item((app_name, "Settings…"), True, ",", 0),
        Item((app_name, f"Quit {app_name}"), True, "Q", 0),
        Item(("File", "New Tab"), True, "T", 0),
        Item(("File", "New Window"), True, "N", 0),
        Item(("File", "New Private Window"), True, "N", 1),
        Item(("History", "Show Full History"), True, "Y", 0),
        Item(("Window", "Downloads"), True, "J", 2),
        Item(("Edit", "Delete"), False, "", 0),
        Item(("File", "Export", "PDF…"), True, "", 0),
    ]


CHROME = apps.App("Google Chrome", "com.google.Chrome", "/Applications/Google Chrome.app",
                  ("Chrome",))


def find(what, items=None):
    return app_menus.find(CHROME, what, items=items if items is not None else tree("Chrome"))


def run():
    r = Results()

    r.head("A command is found by what it does, not by an icon")
    with guard(r, "finding"):
        for said, path in (("settings", ("Chrome", "Settings…")),
                           ("open settings", ("Chrome", "Settings…")),
                           ("preferences", ("Chrome", "Settings…")),
                           ("new tab", ("File", "New Tab")),
                           ("a new tab", ("File", "New Tab")),
                           ("history", ("History", "Show Full History")),
                           ("downloads", ("Window", "Downloads")),
                           ("new window", ("File", "New Window")),
                           ("export as pdf", ("File", "Export", "PDF…"))):
            found = find(said)
            r.check(f"{said!r} is {' › '.join(path)}",
                    bool(found) and found[0].path == path,
                    " › ".join(found[0].path) if found else "nothing")

    r.head("What is not in the menus is not invented")
    with guard(r, "absent"):
        for said in ("color wheels", "bluetooth", ""):
            r.check(f"{said!r} finds nothing", not find(said),
                    str([" › ".join(i.path) for i in find(said)]))

    r.head("Where it is, said the way a person reads a Mac")
    with guard(r, "route"):
        route = app_menus.route(find("settings")[0])
        r.check("the menu, the item and the shortcut",
                "Chrome" in route and "Settings" in route and "⌘," in route, route)
        route = app_menus.route(find("downloads")[0])
        r.check("modifiers in Mac order", "⌥⌘J" in route, route)
        route = app_menus.route(find("export as pdf")[0])
        r.check("a submenu is a step of its own", route.count("›") == 2, route)

    r.head("Doing it goes through the action layer")
    with guard(r, "gates"):
        saved = (cu.ENABLED, app_menus.menu_items, app_menus._press, apps.resolve)
        pressed = []
        try:
            app_menus.menu_items = lambda app: tree("Chrome")
            app_menus._press = lambda item: pressed.append(item.path) or True
            apps.resolve = lambda ref, **k: apps.Resolution(ref, apps.RESOLVED, app=CHROME,
                                                            method="exact", running=True)
            cu.ENABLED = False
            result = cu.choose_menu("chrome", "new tab", confirm=lambda q: True)
            r.check("off by default: refused, nothing pressed",
                    result.outcome == cu.REFUSED and result.refusal == cu.SWITCHED_OFF
                    and not pressed, f"{result.refusal} {pressed}")
            cu.ENABLED = True
            cu.revoke()
            result = cu.choose_menu("chrome", "new tab")
            r.check("nobody to ask: refused", result.refusal == cu.NO_CONFIRMATION
                    and not pressed, result.refusal)
            asked = []
            result = cu.choose_menu("chrome", "new tab",
                                    confirm=lambda q: asked.append(q) or True)
            r.check("asked, then pressed File › New Tab",
                    pressed == [("File", "New Tab")] and asked, f"{pressed} {asked}")
            r.check("the question names the app and the command",
                    bool(asked) and "Google Chrome" in asked[0] and "New Tab" in asked[0],
                    str(asked))
            r.check("and it is not claimed as confirmed without evidence",
                    result.outcome != cu.CONFIRMED, result.outcome)
            again = cu.choose_menu("chrome", "new tab", confirm=lambda q: True)
            r.check("the same command twice in one turn is refused, not pressed again",
                    again.refusal == cu.ALREADY_TRIED and pressed == [("File", "New Tab")],
                    f"{again.refusal} {pressed}")
            pressed.clear()
            result = cu.choose_menu("chrome", "delete", confirm=lambda q: True)
            r.check("a disabled command is refused, not pressed",
                    result.outcome == cu.REFUSED and not pressed, result.refusal)
            app_menus.menu_items = lambda app: tree("Chrome") + [
                Item(("Edit", "Delete All History"), True, "", 0)]
            cu.revoke()
            questions = []
            cu.choose_menu("chrome", "delete all history",
                           confirm=lambda q: questions.append(q) or True)
            r.check("a destructive command is asked about by name, as not undoable",
                    any("delete all history" in q.lower() and "undo" in q.lower()
                        for q in questions), str(questions))
            pressed.clear()
            result = cu.choose_menu("chrome", "colour wheels", confirm=lambda q: True)
            r.check("a command the app does not have is refused, nothing pressed",
                    result.outcome == cu.REFUSED and not pressed, result.spoken)
        finally:
            cu.ENABLED, app_menus.menu_items, app_menus._press, apps.resolve = saved
            cu.revoke()

    r.head("Real menus on this Mac, read only")
    with guard(r, "real"):
        for name, what, wanted in (("Google Chrome", "new tab", "New Tab"),
                                   ("Zen", "settings", "Settings"),
                                   ("Notes", "new note", "New Note")):
            got = apps.resolve(name)
            if got.status != apps.RESOLVED or not got.running:
                continue
            found = app_menus.find(got.app, what)
            r.check(f"{name}: {what!r} is in its menus",
                    bool(found) and wanted.lower() in found[0].path[-1].lower(),
                    app_menus.route(found[0]) if found else "nothing")

    return r.report("app menus")


if __name__ == "__main__":
    sys.exit(run())
