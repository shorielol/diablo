#!/usr/bin/env python3
"""
A system task, resolved to the System Settings pane that does it.

"can you show me how to connect a new Bluetooth device?" names no app. Diablo
taught against whatever window was in front (Voicebox), found nothing and
gave up. The panes of System Settings are installed extensions with names and
bundle ids, so "Bluetooth" can be resolved to System Settings › Bluetooth by
reading the Mac, not by a table of phrases.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import settings_panes as panes  # noqa: E402

Pane = panes.Pane
FAKE = [Pane("Bluetooth", "com.apple.BluetoothSettings", ("bluetooth",)),
        Pane("Wi Fi", "com.apple.wifi-settings-extension", ("wi-fi", "wireless")),
        Pane("Displays", "com.apple.Displays-Settings.extension", ("brightness",)),
        Pane("Sound", "com.apple.Sound-Settings.extension", ("volume", "output")),
        Pane("Keyboard", "com.apple.Keyboard-Settings.extension", ("shortcuts",))]


def run():
    r = Results()

    r.head("A request about a system feature finds its pane")
    with guard(r, "mentioned"):
        for said, wanted in (
                ("can you show me how to connect a new Bluetooth device?", "Bluetooth"),
                ("how do I change my wifi network", "Wi Fi"),
                ("my wi-fi keeps dropping", "Wi Fi"),
                ("make the screen brightness lower", "Displays"),
                ("where do I change the sound output", "Sound"),
                ("change keyboard shortcuts", "Keyboard")):
            found = panes.mentioned(said, panes=FAKE)
            r.check(f"{said!r} -> {wanted}", bool(found) and found[0].name == wanted,
                    found[0].name if found else "nothing")

    r.head("A request about something else finds none")
    with guard(r, "none"):
        for said in ("how do I color grade in Final Cut Pro", "open chrome",
                     "what have I been working on", ""):
            r.check(f"{said!r} -> nothing", not panes.mentioned(said, panes=FAKE),
                    str([p.name for p in panes.mentioned(said, panes=FAKE)]))

    r.head("Opening a pane is a link System Settings understands")
    with guard(r, "url"):
        r.check("Bluetooth's link", panes.url(FAKE[0])
                == "x-apple.systempreferences:com.apple.BluetoothSettings", panes.url(FAKE[0]))

    r.head("This Mac's own panes")
    with guard(r, "real"):
        real = panes.discover()
        names = {p.name for p in real}
        r.check("discovered from the installed extensions", len(real) >= 20, str(len(real)))
        r.check("Bluetooth is one, with its bundle id",
                any(p.name == "Bluetooth" and p.bundle_id == "com.apple.BluetoothSettings"
                    for p in real), str(sorted(names))[:200])
        r.check("internal names are made readable",
                not any(n.endswith(("Extension", "Ext", "SettingPane")) for n in names),
                str(sorted(n for n in names if n.endswith(("Extension", "Ext"))))[:200])
        got = panes.mentioned("connect a new bluetooth device")
        r.check("and a Bluetooth request lands on it here",
                bool(got) and got[0].name == "Bluetooth", got[0].name if got else "nothing")

    return r.report("settings panes")


if __name__ == "__main__":
    sys.exit(run())
