#!/usr/bin/env python3
"""
Moves the assistant out of the Jarvis tree and leaves no trace of the old name.

Before:  ~/.gemini/antigravity/scratch/JARVIS/openclaw
After:   ~/.diablo

That path is not cosmetic. It sits inside another tool's *scratch* directory,
which is a folder that exists to be cleared out. Moving to `~/.diablo` fixes
the name and stops the whole assistant living somewhere disposable.

What it does, in order:

  1. Moves the tree, keeping `source_repo` alongside as `_source_repo`.
  2. Repoints the venv — `pyvenv.cfg` and every shebang under `venv/bin`, which
     are absolute paths and would otherwise all break on the move.
  3. Rewrites the old path in the handful of files that hardcode it.
  4. Renames `start_jarvis.sh` → `diablo.sh`, `jarvis.log` → `diablo.log`,
     `.jarvis.pid` → `.diablo.pid`.
  5. Renames the identity in the assistant's own files. Third-party skill
     folders under `skills/` are left alone — they are other people's code.
  6. Installs `~/bin/diablo` and removes `~/bin/jarvis`.
  7. Rewrites `.env`: DIABLO_ prefixes, an ElevenLabs slot, and it drops
     OPENROUTER_API_KEY, which nothing has read since the local agent landed.

Nothing happens without --go. Run it bare first and read what it intends to do.

    python3 rebrand.py            # dry run, changes nothing
    python3 rebrand.py --go       # do it
    python3 rebrand.py --undo     # move it back
"""

import argparse
import os
import re
import shutil
import stat
import sys

HOME = os.path.expanduser("~")
OLD_ROOT = os.path.join(HOME, ".gemini/antigravity/scratch/JARVIS/openclaw")
OLD_PARENT = os.path.dirname(OLD_ROOT)
NEW_ROOT = os.getenv("DIABLO_HOME", os.path.join(HOME, ".diablo"))
BIN = os.path.join(HOME, "bin")

# Only these are ours to rename. Everything else under skills/ is a vendored
# third-party tool and renaming inside it would be both wrong and destructive.
OUR_FILES = [
    "voice_bridge.py", "start_jarvis.sh", ".env",
    "skills/llm_tool.py", "skills/cache_audio.py", "skills/calendar_tool.py",
    "skills/personality.py", "skills/browser_tool.py", "skills/comms_tool.py",
    "skills/contacts_skill.py", "skills/image_tool.py", "skills/music_tool.py",
    "skills/ocr_tool.py", "skills/ppt_tool.py", "skills/search_tool.py",
    "skills/sys_info_tool.py", "skills/system_skill.py", "skills/wiki_tool.py",
    "skills/whatsapp_tool.py", "skills/instagram_tool.py", "skills/skill_registry.py",
    "skills/local_brain.py", "skills/agent_tools.py", "skills/agent_core.py",
    "skills/delegation_tool.py", "skills/conversation.py", "skills/screen_context.py",
    "skills/wake_word.py", "skills/voice.py",
    "tools/llm_tool.py", "tools/cache_audio.py", "tools/calendar_tool.py",
    "visualizer/widget_launcher.py",
]

RENAMES = [
    ("start_jarvis.sh", "diablo.sh"),
    ("jarvis.log", "diablo.log"),
    (".jarvis.pid", ".diablo.pid"),
]

LAUNCHER = '''#!/bin/bash
# D.I.A.B.L.O. — voice assistant launcher
#   diablo           start listening
#   diablo stop      shut everything down, HUD included
#   diablo status    is it alive
#   diablo logs      follow the stream
#   diablo restart   clean restart
#   diablo voice     re-cache the common spoken lines

DIABLO_HOME="{new_root}"

if [ "${{1:-start}}" = "voice" ]; then
    exec "$DIABLO_HOME/venv/bin/python3" "$DIABLO_HOME/skills/voice.py" prewarm
fi

exec bash "$DIABLO_HOME/diablo.sh" "${{1:-start}}"
'''


def say(action, detail=""):
    print(f"  {action:<46} {detail}")


# ─── venv ────────────────────────────────────────────────────────────────────

def repoint_venv(root, old, new, dry):
    """
    A virtualenv stores absolute paths. Moving one without fixing these leaves
    every console script pointing at a directory that no longer exists.
    """
    venv = os.path.join(root, "venv")
    if not os.path.isdir(venv):
        say("no venv found, skipping", venv)
        return 0

    touched = 0
    cfg = os.path.join(venv, "pyvenv.cfg")
    if os.path.exists(cfg):
        text = open(cfg).read()
        if old in text:
            if not dry:
                open(cfg, "w").write(text.replace(old, new))
            touched += 1
            say("repoint pyvenv.cfg")

    bindir = os.path.join(venv, "bin")
    if os.path.isdir(bindir):
        fixed = 0
        for name in os.listdir(bindir):
            path = os.path.join(bindir, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as f:
                    if f.read(2) != b"#!":
                        continue
                text = open(path, errors="surrogateescape").read()
            except (OSError, UnicodeDecodeError):
                continue
            if old not in text:
                continue
            if not dry:
                mode = os.stat(path).st_mode
                with open(path, "w", errors="surrogateescape") as f:
                    f.write(text.replace(old, new))
                os.chmod(path, mode)
            fixed += 1
        touched += fixed
        say("rewrite venv shebangs", f"{fixed} scripts")
    return touched


# ─── content ─────────────────────────────────────────────────────────────────

IDENTITY = [
    ("J.A.R.V.I.S.", "D.I.A.B.L.O."),
    ("JARVIS", "DIABLO"),
    ("Jarvis", "Diablo"),
    ("jarvis", "diablo"),
]


def rewrite_file(path, old_path, new_path, dry):
    """Swap the path, the env prefix and the name, in that order."""
    try:
        text = original = open(path, errors="surrogateescape").read()
    except OSError:
        return False

    text = text.replace(old_path, new_path)
    text = text.replace(os.path.join(HOME, ".gemini/antigravity/scratch/JARVIS"),
                        new_path)
    text = re.sub(r"\bJARVIS_", "DIABLO_", text)

    for before, after in IDENTITY:
        # The wake-word model filename is a real artefact on disk that belongs
        # to openWakeWord, not to us. Renaming the string would break the import
        # that still references it.
        text = re.sub(r"(?<!hey_)" + re.escape(before), after, text)

    if text == original:
        return False
    if not dry:
        with open(path, "w", errors="surrogateescape") as f:
            f.write(text)
    return True


# ─── env ─────────────────────────────────────────────────────────────────────

def rewrite_env(root, dry):
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    lines = open(path).read().splitlines()
    out, seen = [], set()
    for line in lines:
        # Nothing has read this since the agent moved to a local model.
        if line.startswith("OPENROUTER_API_KEY"):
            say("drop OPENROUTER_API_KEY", "unused since the local agent landed")
            continue
        line = re.sub(r"^JARVIS_", "DIABLO_", line)
        key = line.split("=", 1)[0].strip()
        if key:
            seen.add(key)
        out.append(line)

    additions = [
        ("", ""),
        ("# Voice — ElevenLabs. Leave the key blank and Diablo uses the local", None),
        ("# macOS voice instead. Every line spoken is cached to disk, so a", None),
        ("# phrase costs one request ever.", None),
        ("ELEVENLABS_API_KEY", ""),
        ("DIABLO_VOICE_ID", "qbkH1EDealYs8PUoNNuB"),
        ("DIABLO_VOICE_MODEL", "eleven_turbo_v2_5"),
        ("", ""),
        ("# Set to 1 for strictly local: no ElevenLabs, no web tools.", None),
        ("DIABLO_OFFLINE", "0"),
        ("DIABLO_HOME", NEW_ROOT),
    ]
    for key, value in additions:
        if value is None or key == "":
            out.append(key)
        elif key not in seen:
            out.append(f"{key}={value}")

    if not dry:
        open(path, "w").write("\n".join(out) + "\n")
    say("rewrite .env", "DIABLO_ prefixes, ElevenLabs slot")


# ─── main ────────────────────────────────────────────────────────────────────

def do_rebrand(dry):
    if os.path.isdir(NEW_ROOT) and not os.path.isdir(OLD_ROOT):
        print(f"= Already rebranded. Diablo lives at {NEW_ROOT}")
        return 0
    if not os.path.isdir(OLD_ROOT):
        print(f"✗ Cannot find {OLD_ROOT}")
        return 1
    if os.path.isdir(NEW_ROOT):
        print(f"✗ {NEW_ROOT} already exists and the old tree is still there.")
        print("  Move or remove one of them and run this again.")
        return 1

    print(f"\n{'DRY RUN — nothing will change' if dry else 'REBRANDING'}\n")
    print(f"  {OLD_ROOT}")
    print(f"  → {NEW_ROOT}\n")

    # 1. move
    say("move the tree", f"{len(os.listdir(OLD_ROOT))} entries")
    if not dry:
        os.makedirs(os.path.dirname(NEW_ROOT), exist_ok=True)
        shutil.move(OLD_ROOT, NEW_ROOT)
        source_repo = os.path.join(OLD_PARENT, "source_repo")
        if os.path.isdir(source_repo):
            shutil.move(source_repo, os.path.join(NEW_ROOT, "_source_repo"))
        try:
            os.rmdir(OLD_PARENT)
        except OSError:
            pass
    say("keep source_repo as _source_repo")

    root = NEW_ROOT if not dry else OLD_ROOT

    # 2. venv
    repoint_venv(root, OLD_ROOT, NEW_ROOT, dry)

    # 3 + 5. content
    changed = 0
    for relative in OUR_FILES:
        path = os.path.join(root, relative)
        if os.path.exists(path) and rewrite_file(path, OLD_ROOT, NEW_ROOT, dry):
            changed += 1
    say("rewrite paths and names", f"{changed} files")

    # 4. filenames
    for before, after in RENAMES:
        source = os.path.join(root, before)
        if os.path.exists(source):
            say(f"rename {before}", f"→ {after}")
            if not dry:
                shutil.move(source, os.path.join(root, after))

    # 7. env
    rewrite_env(root, dry)

    # 6. launcher
    launcher = os.path.join(BIN, "diablo")
    say("install ~/bin/diablo")
    if not dry:
        os.makedirs(BIN, exist_ok=True)
        with open(launcher, "w") as f:
            f.write(LAUNCHER.format(new_root=NEW_ROOT))
        os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IEXEC |
                 stat.S_IXGRP | stat.S_IXOTH)

    old_launcher = os.path.join(BIN, "jarvis")
    if os.path.exists(old_launcher):
        say("remove ~/bin/jarvis")
        if not dry:
            os.unlink(old_launcher)

    print()
    if dry:
        print("✓ Dry run complete. Re-run with --go to make these changes.")
    else:
        print(f"✓ Rebranded. Diablo now lives at {NEW_ROOT}")
        print("\n  Next:")
        print(f"    bash \"{os.path.dirname(os.path.abspath(__file__))}/install.sh\"")
        print("    diablo restart")
    return 0


def do_undo(dry):
    if not os.path.isdir(NEW_ROOT):
        print(f"✗ Nothing at {NEW_ROOT} to move back.")
        return 1
    print(f"\n{'DRY RUN' if dry else 'UNDOING'}: {NEW_ROOT} → {OLD_ROOT}\n")
    repoint_venv(NEW_ROOT, NEW_ROOT, OLD_ROOT, dry)
    for relative in OUR_FILES + [r[1] for r in RENAMES]:
        path = os.path.join(NEW_ROOT, relative)
        if os.path.exists(path):
            rewrite_file(path, NEW_ROOT, OLD_ROOT, dry)
    if not dry:
        os.makedirs(OLD_PARENT, exist_ok=True)
        shutil.move(NEW_ROOT, OLD_ROOT)
    print("\n✓ Moved back. The name changes inside the files are not reverted.")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--go", action="store_true", help="actually make the changes")
    parser.add_argument("--undo", action="store_true", help="move the tree back")
    args = parser.parse_args()
    if args.undo:
        return do_undo(not args.go)
    return do_rebrand(not args.go)


if __name__ == "__main__":
    sys.exit(main())
