"""
What a how-to is about, made real on screen, and the app's own route to it.

Three real failures had one cause. Asked how to color grade in Final Cut Pro,
Diablo taught steps for the browser in front of it. Asked how to connect a
Bluetooth device, it taught against the Voicebox window. Asked how to open
settings in Zen, it found no gear on screen and stopped. Each time the window
in front stood in for what the person asked about, and what the screen showed
stood in for what the application can do.

So, before teaching or pointing:

    about(request)       which app (or System Settings pane) the task is about:
                         named in the request, else a Settings pane named in
                         it, else the window in front
    bring_up(context)    make that true on screen: open the app or the pane,
                         or bring it forward. Both reversible; nothing else.
    menu_route(app, request)
                         the command in the app's own menu bar that does it.
                         Matched by words first; failing that, the model picks
                         one from the app's real, enabled commands, and its
                         pick is accepted only if it is one of them.
"""

import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from skills import app_menus, apps, ax, settings_panes

NAMED, PANE, FRONT = "named", "pane", "front"
BRING_UP_WAIT = 8.0
# The model sees at most this many commands. Final Cut Pro publishes 522.
CHOICE_LIMIT = 600


@dataclass
class Context:
    app: Optional[apps.App] = None
    pane: Optional[settings_panes.Pane] = None
    source: str = FRONT
    window: Optional[Dict[str, Any]] = None


def _resolve(name: str, registry: Optional[Sequence[apps.App]]) -> Optional[apps.App]:
    injected = registry is not None
    found = apps.resolve(name, registry=registry,
                         running=(lambda app: False) if injected else None,
                         front=(lambda: "") if injected else None)
    return found.app if found.status == apps.RESOLVED else None


def about(text: str, front: Optional[Dict[str, Any]] = None,
          registry: Optional[Sequence[apps.App]] = None,
          panes: Optional[Sequence[settings_panes.Pane]] = None) -> Context:
    """Which application, or System Settings pane, a request is about."""
    named = apps.mentioned(text, registry=registry)
    if named:
        return Context(named[0], None, NAMED)
    pane = settings_panes.mentioned(text, panes=panes)
    if pane:
        return Context(_resolve("System Settings", registry), pane[0], PANE)
    if front is None:
        from skills import screen_context
        front = screen_context.frontmost_window()
    app = _resolve(front.get("app", ""), registry) if front else None
    return Context(app, None, FRONT, front)


def bring_up(context: Context, wait: float = BRING_UP_WAIT) -> Optional[Dict[str, Any]]:
    """
    Put the task's app (or pane) on screen, in front, and return its window.

    Opening an application or a Settings pane, or bringing one forward, is
    reversible and changes nothing inside it. Nothing else happens here.
    """
    from skills import screen_context
    app = context.app
    if app is None:
        return context.window
    if context.pane is not None:
        command = ["open", settings_panes.url(context.pane)]
    elif app.bundle_id:
        command = ["open", "-b", app.bundle_id]
    else:
        command = ["open", app.path]
    try:
        subprocess.run(command, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    deadline = time.time() + wait
    while True:
        window = screen_context.find_window(app.name)
        if window is not None:
            context.window = window
            apps.note_in_use(app)
            return window
        if time.time() >= deadline:
            return None
        time.sleep(0.3)


def _core(app: apps.App, request: str) -> str:
    """The request without the app's own name: 'settings in the Zen browser' -> 'settings'."""
    drop = {w for name in app.names() for w in apps.normalise(name).split()}
    drop |= {"app", "application", "browser"}
    return " ".join(w for w in apps.normalise(request).split() if w not in drop)


def _model_choice(request: str, lines: List[str]) -> str:
    """The model picks one command from the list, or 'none'."""
    from skills import local_brain
    listing = "\n".join(lines)
    reply = local_brain.chat_json(
        [{"role": "system", "content": (
            "You are given an application's menu commands, one per line, and what a "
            "person wants to do. Reply with the ONE command, copied exactly as it is "
            "written in the list, that does it or opens the place where it is done. "
            "If no command in the list does it, the command is \"none\". Never write "
            "a command that is not in the list.")},
         {"role": "user", "content": f"Wants to: {request}\n\nCommands:\n{listing}"}],
        schema={"type": "object", "properties": {"command": {"type": "string"}},
                "required": ["command"]},
        temperature=0.0, num_predict=60)
    return str((reply or {}).get("command", "none"))


# Verbs and interface nouns that say how, not what: "add", "open", "show".
# Ignored when looking for the command that shares the request's own words.
_HOW = {"add", "new", "show", "open", "make", "change", "set", "use", "see", "get", "go",
        "turn", "put", "create", "do", "view", "edit", "file", "window", "item", "menu",
        "where", "find"}
# A few commands sharing the request's words is a short list the model can
# choose from reliably. Beyond this, the words did not narrow it enough.
NARROW = 8

KNOWN, PARTIAL, INFERRED = "known", "partial", "inferred"


@dataclass(frozen=True)
class Route:
    """A command in the app's own menus, and how sure the match is."""
    item: ax.MenuItem
    method: str       # known: every word matched; partial: most words, unique;
                      # inferred: the model chose it, so it is said as a belief


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _ask(chooser, request: str, offered: Sequence[ax.MenuItem]) -> Optional[ax.MenuItem]:
    if chooser is None or not offered:
        return None
    lines = [" › ".join(item.path) for item in offered]
    try:
        picked = chooser(request, lines)
    except Exception:              # a model that fails is no route, not a crash
        return None
    wanted = re.sub(r"\s+", " ", (picked or "").strip().lower())
    for item, line in zip(offered, lines):
        if line.lower() == wanted:
            return item
    return None


def find_route(app: apps.App, request: str,
               items: Optional[Sequence[ax.MenuItem]] = None,
               chooser: Optional[Callable[[str, List[str]], str]] = _model_choice
               ) -> Optional[Route]:
    """
    The app's own command for what was asked, or None. Never an invented one.

      1. known    every word of the request (less the app's name) on the path
      2. partial  the one command whose title shares the most of the request's
                  own words; if a few tie, the model chooses among only those
      3. inferred the model chooses from all the app's enabled commands
    A command the model writes that is not in the list it was given is dropped.
    """
    items = list(items) if items is not None else app_menus.menu_items(app)
    if not items:
        return None
    core = _core(app, request)
    found = app_menus.find(app, core, items=items)
    if found:
        enabled = [item for item in found if item.enabled]
        return Route((enabled or found)[0], KNOWN)

    wanted = {_stem(w) for w in app_menus._content(core)} - _HOW
    enabled = [item for item in items if item.enabled]
    if wanted:
        scored = []
        for item in enabled:
            title = {_stem(w) for w in app_menus._words(item.title)}
            shared = len(wanted & title)
            if shared:
                scored.append((-shared, len(item.title), item))
        scored.sort(key=lambda entry: (entry[0], entry[1]))
        if scored:
            top = [entry for entry in scored if entry[0] == scored[0][0]]
            if len(top) == 1:
                return Route(top[0][2], PARTIAL)
            # The request's own words found commands: the choice is among the
            # best of those, never the whole menu bar. Offered all 522 of Final
            # Cut Pro's commands for "export my video", the model chose
            # File › Import › Media….
            picked = _ask(chooser, request, [entry[2] for entry in scored[:NARROW]])
            return Route(picked, INFERRED) if picked is not None else None
    # No word of the request is on any command: only then the whole list,
    # for a different word for the same thing ("color grade" -> Color Inspector).
    picked = _ask(chooser, request, enabled[:CHOICE_LIMIT])
    return Route(picked, INFERRED) if picked is not None else None


def menu_route(app: apps.App, request: str,
               items: Optional[Sequence[ax.MenuItem]] = None,
               chooser: Optional[Callable[[str, List[str]], str]] = _model_choice
               ) -> Optional[ax.MenuItem]:
    """find_route's command alone."""
    route = find_route(app, request, items=items, chooser=chooser)
    return route.item if route else None


# ─── Guidance: the route, shown where it can be seen ────────────────────────

# How many other running applications are searched for a command the front
# one does not have. Reading a menu bar costs 150-250 ms.
OTHER_APPS = 6


def _other_route(request: str, skip: Optional[apps.App]) -> Tuple[Optional[apps.App],
                                                                  Optional[Route]]:
    """
    The one other running app whose menus have exactly this command.

    "show me how to open a new note" with Chrome in front: Chrome has no New
    Note, Notes does. Only a KNOWN match counts, and only if exactly one app
    has it; two apps with the command is a question, not a guess.
    """
    from skills import screen_context
    seen, hits = set(), []
    for window in screen_context.list_windows():
        if len(seen) >= OTHER_APPS:
            break
        name = window.get("app", "")
        if name in seen or window.get("sensitive"):
            continue
        seen.add(name)
        app = _resolve(name, None)
        if app is None or (skip is not None and app.bundle_id == skip.bundle_id):
            continue
        route = find_route(app, request, chooser=None)
        if route is not None and route.method == KNOWN:
            hits.append((app, route))
    return hits[0] if len(hits) == 1 else (None, None)


def _bare(title: str) -> str:
    return title.replace("…", "").replace("...", "").strip()


def guide(question: str, context: Context) -> Optional[str]:
    """
    Where to find what was asked, from the app's own commands, shown on screen.

    Returns what to say, or None when no command of the app does it, so the
    caller can go on to look at the screen or explain. Marks the on-screen
    control only when perception finds it decisively under the app's own name
    for the command; otherwise marks the menu's title in the menu bar.
    """
    from skills import perception, pointing
    app = context.app
    route = find_route(app, question) if app is not None else None
    if route is None and context.source == FRONT:
        other, route = _other_route(question, app)
        if other is not None:
            app = other
            context.app, context.window = other, bring_up(Context(other)) or context.window
    if route is None or app is None:
        return None

    where = app_menus.route(route.item)
    shown = ""
    window = context.window
    if window is not None and window.get("app") and \
            apps.normalise(window["app"]) in {apps.normalise(n) for n in app.names()}:
        try:
            reading = perception.read(_bare(route.item.title), window)
        except Exception:          # no reading is no marker, not a failure
            reading = None
        best = getattr(reading, "best", None)
        # The same name, not a shared word: "Settings – Passwords" is not
        # Chrome › Settings…, and marking it as such was wrong.
        if reading is not None and reading.verdict == perception.Verdict.RESOLVED and best \
                and app_menus._content(best.label) == app_menus._content(route.item.title):
            x, y = best.centre
            pointing.show(x, y, best.label)
            shown = f" It's also on screen: I've marked {best.label}."
    if not shown and route.item.bar_frame:
        x, y, w, h = route.item.bar_frame
        pointing.show(x + w / 2, y + h / 2, route.item.path[0])
        shown = f" I've marked the {route.item.path[0]} menu."

    if route.method == INFERRED:
        return (f"I think it's {where} in {app.name}. That's my best match among its "
                f"menu commands, so check it's the one you mean.{shown}")
    return f"In {app.name}, it's {where}.{shown}"
