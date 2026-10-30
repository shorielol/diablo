#!/usr/bin/env python3
"""
What a person calls an application, resolved to the application.

"open chrome" used to fail with Google Chrome installed and running: the name
went to `open -a` exactly as spoken, and macOS has no app called "chrome". The
same confusion ran the other way through the window list, where "chrome"
matched Zen windows and an app with no window open read as no app at all.

These checks hold the resolver to one rule: identity comes from the
applications installed on the Mac, never from the windows that happen to be
open, and a name that could mean two apps is asked about, not guessed.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import apps, screen_context  # noqa: E402

App = apps.App

REGISTRY = [
    App("Google Chrome", "com.google.Chrome", "/Applications/Google Chrome.app"),
    App("Safari", "com.apple.Safari", "/Applications/Safari.app"),
    App("Zen", "app.zen-browser.zen", "/Applications/Zen.app"),
    App("Final Cut Pro", "com.apple.FinalCut", "/Applications/Final Cut Pro.app"),
    App("Logic Pro", "com.apple.logic10", "/Applications/Logic Pro.app"),
    App("Finder", "com.apple.finder", "/System/Library/CoreServices/Finder.app"),
    App("Notes", "com.apple.Notes", "/System/Applications/Notes.app"),
    App("Sticky Notes", "com.example.sticky", "/Applications/Sticky Notes.app"),
    App("System Settings", "com.apple.systempreferences",
        "/System/Applications/System Settings.app"),
    App("Preview", "com.apple.Preview", "/System/Applications/Preview.app"),
    App("Microsoft Edge", "com.microsoft.edgemac", "/Applications/Microsoft Edge.app"),
    App("Visual Studio Code", "com.microsoft.VSCode", "/Applications/Visual Studio Code.app"),
    App("Brave Browser", "com.brave.Browser", "/Applications/Brave Browser.app"),
    App("Calculator", "com.apple.calculator", "/System/Applications/Calculator.app"),
]


def resolve(reference, running=(), front=""):
    return apps.resolve(reference, registry=REGISTRY,
                        running=lambda app: app.bundle_id in running,
                        front=lambda: front)


def run():
    r = Results()

    r.head("What people call an app is the app")
    with guard(r, "names"):
        for said, wanted in (
                ("chrome", "Google Chrome"), ("Chrome", "Google Chrome"),
                ("google chrome", "Google Chrome"), ("CHROME ", "Google Chrome"),
                ("chrome.app", "Google Chrome"), ("the chrome app", "Google Chrome"),
                ("gc", "Google Chrome"), ("chorme", "Google Chrome"),
                ("safari", "Safari"), ("zen", "Zen"), ("zen browser", "Zen"),
                ("the Zen browser", "Zen"),
                ("final cut", "Final Cut Pro"), ("final cut pro", "Final Cut Pro"),
                ("fcp", "Final Cut Pro"), ("finder", "Finder"), ("notes", "Notes"),
                ("settings", "System Settings"), ("system settings", "System Settings"),
                ("system preferences", "System Settings"), ("edge", "Microsoft Edge"),
                ("vscode", "Visual Studio Code"), ("vs code", "Visual Studio Code"),
                ("brave", "Brave Browser"), ("brave browser", "Brave Browser"),
                ("calculator", "Calculator")):
            got = resolve(said)
            r.check(f"{said!r} is {wanted}",
                    got.status == apps.RESOLVED and got.app.name == wanted,
                    f"{got.status} {got.app.name if got.app else got.candidates}")

    r.head("An exact name wins over a longer one that contains it")
    with guard(r, "exact first"):
        got = resolve("notes")
        r.check("'notes' is Notes, not a question about Sticky Notes",
                got.status == apps.RESOLVED and got.app.name == "Notes" and got.method == "exact",
                f"{got.status} {got.method}")

    r.head("A name that fits two apps is asked about")
    with guard(r, "ambiguity"):
        got = resolve("pro")
        r.check("'pro' is Final Cut Pro or Logic Pro: ambiguous",
                got.status == apps.AMBIGUOUS
                and {a.name for a in got.candidates} == {"Final Cut Pro", "Logic Pro"},
                f"{got.status} {[a.name for a in got.candidates]}")
        r.check("and the question names both", "Final Cut Pro" in apps.describe(got)
                and "Logic Pro" in apps.describe(got), apps.describe(got))

    r.head("An app that is not installed is not invented")
    with guard(r, "unknown"):
        for said in ("photoshop", "an app that does not exist", "", "   "):
            got = resolve(said)
            r.check(f"{said!r} is unknown", got.status == apps.UNKNOWN and got.app is None,
                    got.status)
        message = apps.describe(resolve("photoshop"))
        r.check("and is said plainly, without claiming more than the search found",
                "photoshop" in message.lower() and "couldn't find" in message.lower(),
                message)

    r.head("Installed, running and in front are separate facts")
    with guard(r, "state"):
        closed = resolve("chrome")
        r.check("installed and not running is still resolved",
                closed.status == apps.RESOLVED and closed.installed and not closed.running,
                f"{closed.status} running={closed.running}")
        r.check("and is not described as missing",
                "not installed" not in apps.describe(closed).lower()
                and "couldn't find" not in apps.describe(closed).lower(),
                apps.describe(closed))
        open_ = resolve("chrome", running={"com.google.Chrome"},
                        front="com.google.Chrome")
        r.check("running and in front", open_.running and open_.focused,
                f"running={open_.running} focused={open_.focused}")
        behind = resolve("chrome", running={"com.google.Chrome"}, front="com.apple.Safari")
        r.check("running but not in front", behind.running and not behind.focused)

    r.head("The window list has no say in whether an app exists")
    with guard(r, "windows"):
        saved = (screen_context.list_windows, screen_context.find_window)

        def no_windows(*_a, **_k):
            raise AssertionError("the resolver asked the window list")
        screen_context.list_windows = screen_context.find_window = no_windows
        try:
            got = resolve("chrome")
        finally:
            screen_context.list_windows, screen_context.find_window = saved
        r.check("resolved without looking at a single window",
                got.status == apps.RESOLVED and got.app.name == "Google Chrome", got.status)
        tree = ast.parse(open(apps.__file__).read())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        window_reads = names & {"list_windows", "find_window", "frontmost_window",
                                "matching_windows"}
        r.check("apps.py never reads windows", not window_reads, str(window_reads))

    r.head("Opening an app: resolve, launch, check it started")
    with guard(r, "open"):
        launched = []

        def launcher(app):
            launched.append(app.bundle_id)
            return True
        said = apps.open_app("chrome", registry=REGISTRY, launcher=launcher,
                             running=lambda app: app.bundle_id in launched, wait=0.0)
        r.check("'chrome' launches Google Chrome by bundle id",
                launched == ["com.google.Chrome"], str(launched))
        r.check("and says the app's real name", "Google Chrome" in said, said)
        launched.clear()
        said = apps.open_app("pro", registry=REGISTRY, launcher=launcher,
                             running=lambda app: False, wait=0.0)
        r.check("an ambiguous name launches nothing", not launched, str(launched))
        r.check("and asks", "?" in said, said)
        said = apps.open_app("photoshop", registry=REGISTRY, launcher=launcher,
                             running=lambda app: False, wait=0.0)
        r.check("an unknown name launches nothing and says so",
                not launched and "couldn't find" in said.lower(), said)
        said = apps.open_app("chrome", registry=REGISTRY, launcher=lambda app: True,
                             running=lambda app: False, wait=0.0)
        r.check("a launch that did not start is not reported as opened",
                not said.lower().startswith("opened"), said)

    r.head("The app just opened is the one a follow-up is about")
    with guard(r, "in use"):
        apps.open_app("chrome", registry=REGISTRY, launcher=lambda app: True,
                      running=lambda app: True, wait=0.0)
        r.check("opening Chrome makes it the app in use",
                apps.in_use() is not None and apps.in_use().name == "Google Chrome",
                str(apps.in_use()))
        from skills import delegation_tool
        said = delegation_tool._with_app_in_use("open a new tab")
        r.check("'open a new tab' then carries it", "Google Chrome" in said, said)
        saved = apps._registry
        apps._registry = lambda refresh=False: REGISTRY
        try:
            said = delegation_tool._with_app_in_use("open a new tab on Safari")
        finally:
            apps._registry = saved
        r.check("a request that names its own app is left as it is",
                said == "open a new tab on Safari", said)

    r.head("The tool the model calls goes through the resolver")
    with guard(r, "wiring"):
        from skills import agent_tools
        tree = ast.parse(open(agent_tools.__file__).read())
        body = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "open_app")
        calls = {ast.unparse(n.func) for n in ast.walk(body) if isinstance(n, ast.Call)}
        r.check("agent_tools.open_app calls apps.open_app",
                any(c.endswith("apps.open_app") for c in calls), str(calls))
        text = ast.unparse(body)
        r.check("and never passes the spoken name to `open -a`",
                "'-a'" not in text and '"-a"' not in text, text[:120])

    r.head("This Mac's own applications")
    with guard(r, "real registry"):
        real = apps.registry()
        r.check("installed apps are discovered", len(real) > 10, str(len(real)))
        present = {a.name for a in real}
        for said, wanted in (("chrome", "Google Chrome"), ("zen", "Zen"),
                             ("final cut", "Final Cut Pro"), ("finder", "Finder"),
                             ("notes", "Notes"), ("system settings", "System Settings"),
                             ("safari", "Safari"), ("calculator", "Calculator")):
            if wanted not in present:
                continue
            got = apps.resolve(said)
            r.check(f"{said!r} is {wanted} on this Mac",
                    got.status == apps.RESOLVED and got.app.name == wanted,
                    f"{got.status} "
                    f"{got.app.name if got.app else [a.name for a in got.candidates]}")
        got = apps.resolve("an application that is definitely not installed")
        r.check("a made-up app is unknown here too", got.status == apps.UNKNOWN, got.status)

    return r.report("app resolution")


if __name__ == "__main__":
    sys.exit(run())
