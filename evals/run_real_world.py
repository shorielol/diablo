#!/usr/bin/env python3
"""
Diablo against the real Mac: real applications, real windows, real requests.

    REAL_WORLD_GAUNTLET=true python3 evals/run_real_world.py --authorized \
        [--only ID,ID] [--json out.json]

Development only. Every request goes through voice_bridge.process_command,
the function a spoken request reaches, with what Diablo says recorded instead
of spoken. It reuses Diablo's own perception, permission and action layers;
nothing here clicks or types by itself.

What it may do, and nothing else:
  * open applications, windows, tabs, settings and menus, point and highlight;
  * computer use is switched on inside this process only;
  * a permission question is answered yes only when it names nothing
    destructive (delete, send, buy, pay, erase, remove, quit, pair...), and
    every question is recorded with the answer given;
  * it starts only when the Mac has been idle, and stops between requests the
    moment the keyboard or mouse is used.

Before: the running applications and the front app are recorded. After:
applications this run launched are quit (only those), and the places a
screenshot could land are searched for image files written during the run.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

IDLE_BEFORE = 240.0
TURN_TIMEOUT = 150.0

# Words that make a permission question something this harness never says yes to.
NEVER_APPROVE = re.compile(r"\b(delete|erase|remove|send|buy|pay|purchase|empty|trash|"
                           r"quit|log ?out|sign ?out|pair|forget|reset|format|"
                           r"password|uninstall)\b", re.I)

# Internal names a person must never hear. AX only in capitals, so "relax" and
# "tax" are not leaks.
LEAKS = re.compile(r"(?i:\b(?:voice ?box|goclick|ocr|ollama|qwen|grounding[ _]gate|"
                   r"screenframe|subprocess|traceback|stack trace|exception|tool call|"
                   r"mcp|[a-z_]+\.py)\b)|\bAX\b")

# A question that asks whether to go on, which a person answers "next".
CONTINUE = re.compile(r"\b(next|ready|continue|carry on|go on|keep going)\b", re.I)

# (id, what the person says, the person's next turns)
REQUESTS = [
    ("memory", "what have I been working on the past few days?", ["yes, please check"]),
    ("fcp-grade", "how do I color grade on Final Cut Pro, can you show me?",
     ["no, I mean the actual color wheels", "where is that?", "that's not what I see"]),
    ("zen-settings", "how do I open settings in the Zen browser? can you show me?", []),
    ("bluetooth", "can you show me how to connect a new Bluetooth device?", []),
    ("fcp-subtitles", "show me how to add subtitles in Final Cut Pro", []),
    ("new-note", "show me how to open a new note", []),
    ("zen-tab", "open a new tab on Zen", []),
    ("chrome", "open chrome", ["open a new tab"]),
    ("where-settings", "show me where settings are", []),
    ("looking-at", "what am I looking at?", []),
    ("what-says", "what does this say?", ["that's not what I meant"]),
    ("open-safari", "open safari", []),
    ("open-calculator", "open calculator", []),
    ("open-textedit", "open textedit", []),
    ("open-preview", "open preview", []),
    ("open-google-chrome", "open google chrome", []),
    ("open-final-cut", "open final cut", []),
    ("open-system-settings", "open system settings", []),
    ("open-finder", "open finder", []),
    ("open-notes", "open notes", []),
    ("open-zen", "open zen", []),
    ("open-photoshop", "open photoshop", []),
    ("open-ambiguous", "open pro", []),
    # Last, so the session has turns behind it: what is known comes from them.
    ("memory-session", "what have we done so far today?", []),
]


# The applications these requests may open. Finder is never quit.
TEST_APPS = {"Safari", "Calculator", "TextEdit", "Preview", "Google Chrome",
             "Final Cut Pro", "System Settings", "Notes", "Zen"}


# ─── The Mac, before and after ──────────────────────────────────────────────

def idle_seconds() -> float:
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True,
                         text=True).stdout
    found = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(found.group(1)) / 1e9 if found else 0.0


def running_bundles() -> set:
    out = subprocess.run(["lsappinfo", "list"], capture_output=True, text=True).stdout
    return set(re.findall(r'bundleID="([^"]+)"', out))


IMAGE_PLACES = [os.path.expanduser(p) for p in (
    "~/Desktop", "~/Downloads", "~/Documents", "~/Library/Caches", "~/.diablo")] + [
    "/private/tmp", tempfile.gettempdir(), DIABLO_HOME, REPO]
# Other scratch homes the developer's unit tests build: not this run's output.
NOT_THIS_RUN = re.compile(r"/g6/t\.[A-Za-z0-9]+/")


def new_images(stamp: str) -> list:
    """Image files written since the stamp file, where a screenshot could land."""
    found = []
    for place in IMAGE_PLACES:
        if not os.path.isdir(place):
            continue
        out = subprocess.run(
            ["find", place, "-type", "f", "-newer", stamp, "(", "-iname", "*.png",
             "-o", "-iname", "*.jpg", "-o", "-iname", "*.jpeg", "-o", "-iname", "*.tiff",
             "-o", "-iname", "*.heic", ")"],
            capture_output=True, text=True, timeout=120).stdout
        found.extend(line for line in out.splitlines()
                     if line and not NOT_THIS_RUN.search(line))
    return sorted(set(found))


# ─── One request ────────────────────────────────────────────────────────────

def run_request(vb, delegation_tool, ident: str, text: str, follow: list) -> dict:
    said, questions, approvals, turns = [], [], [], []
    first = {"at": None}

    def speak(message, *a, **k):
        if first["at"] is None:
            first["at"] = time.time()
        said.append(str(message))

    def ask(question, *a, **k):
        # Diablo asking mid-turn. A walkthrough waiting to go on gets "next";
        # anything else gets silence, as from a person who did not answer.
        # Answering every question "next" once made "where is that?" skip a
        # music track: the harness, not Diablo, chose that answer.
        speak(question)
        questions.append(question)
        return "next" if CONTINUE.search(question or "") else ""

    def confirm(description, *a, **k):
        ok = not NEVER_APPROVE.search(description or "")
        approvals.append({"question": description, "approved": ok})
        return ok

    vb.speak, vb.ask_user_voice, vb.confirm_voice = speak, ask, confirm
    started = time.time()
    for line in [text] + list(follow):
        turn_start, before = time.time(), len(said)
        first["at"] = None
        try:
            vb.process_command(line)
        except Exception as e:      # recorded: a crash is a finding, not a stop
            said.append(f"[crashed: {type(e).__name__}: {e}]")
        deadline = time.time() + TURN_TIMEOUT
        while delegation_tool._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.2)
        turns.append({"person": line, "diablo": said[before:],
                      "seconds": round(time.time() - turn_start, 1),
                      "first_words_s": (round(first["at"] - turn_start, 1)
                                        if first["at"] else None)})
    words = " ".join(said)
    return {"id": ident, "request": text, "turns": turns, "questions": questions,
            "approvals": approvals, "seconds": round(time.time() - started, 1),
            "leaks": sorted({m.group(0) for m in LEAKS.finditer(words)})}


# ─── Run ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--authorized", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    if os.getenv("REAL_WORLD_GAUNTLET") != "true" or not args.authorized:
        print("Development only: set REAL_WORLD_GAUNTLET=true and pass --authorized.")
        return 2
    if idle_seconds() < IDLE_BEFORE:
        print(f"Not run: the Mac was in use {idle_seconds():.0f}s ago.")
        return 3

    os.chdir(DIABLO_HOME)
    import voice_bridge as vb                         # noqa: E402
    from skills import computer_use, delegation_tool  # noqa: E402

    handle, stamp = tempfile.mkstemp(prefix="realworld_stamp_")
    os.close(handle)
    began = time.time()
    before = {"running": sorted(running_bundles()),
              "front": subprocess.run(["lsappinfo", "front"], capture_output=True,
                                      text=True).stdout.strip()}
    computer_use.ENABLED = True
    wanted = set(filter(None, args.only.split(",")))
    results, stopped = [], ""
    try:
        for ident, text, follow in REQUESTS:
            if wanted and ident not in wanted:
                continue
            # Diablo's own clicks happen through accessibility and `open`, not
            # through input events, so any input here is a person.
            if idle_seconds() < 1.0:
                stopped = f"someone used the Mac before {ident}"
                break
            row = run_request(vb, delegation_tool, ident, text, follow)
            results.append(row)
            print(json.dumps(row, ensure_ascii=False)[:600], flush=True)
    finally:
        computer_use.ENABLED = False
        computer_use.revoke()

    # Put the Mac back: quit only test applications this run launched. Never
    # Finder, never anything else that happened to start meanwhile.
    from skills import apps
    tested = {a.bundle_id for a in apps.registry() if a.name in TEST_APPS}
    launched = sorted((running_bundles() - set(before["running"])) & tested)
    for bundle in launched:
        subprocess.run(["osascript", "-e", f'quit app id "{bundle}"'],
                       capture_output=True, timeout=20)
    time.sleep(2)
    after = {"launched_and_quit": launched,
             "still_running_from_run": sorted(running_bundles() - set(before["running"])),
             "new_images": new_images(stamp)}
    os.unlink(stamp)
    report = {"started": began, "stopped_early": stopped, "before": before,
              "after": after, "results": results}
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1, ensure_ascii=False)
    print(json.dumps(after, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
