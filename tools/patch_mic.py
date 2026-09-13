#!/usr/bin/env python3
"""
Replaces the microphone layer in an already-installed voice_bridge.py.

Three changes, all of them removing a way to fail silently:

  get_target_mic_index()   guessed the device from its name. Gone. mic.py picks
                           by recording from each candidate and keeping the one
                           that produced sound.

  sd.InputStream(16000)    demanded a rate the built-in mic does not run at.
                           Now the device's own rate is used and the audio is
                           resampled to 16 kHz in Python.

  (nothing)                there was no check that audio was arriving. Now a
                           sustained run of pure silence is reported once, with
                           the fix, instead of scrolling past forever.

Idempotent, backs up first, refuses to write anything that will not compile.

    python3 patch_mic.py [--dry-run] [--restore]
"""

import argparse
import glob
import os
import re
import shutil
import sys
import time

BRIDGE = os.getenv("DIABLO_BRIDGE", os.path.join(
    os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")), "voice_bridge.py"))

MARKER = "# ─── Diablo microphone layer (mic.py) ───"

# Names the bridge calls and therefore must import. `delegation_tool` and
# `agent_tools` are used in eleven places and were not imported at all — the
# file still compiles, because a missing name is a runtime error, so this only
# showed up as a crash the moment anything was actually said.
REQUIRED_SKILL_IMPORTS = [
    "conversation", "screen_context", "wake_word", "voice", "skill_registry",
    "local_brain", "mcp_client", "delegation_tool", "agent_tools", "mic",
]
SKILL_IMPORT_RE = re.compile(r"^from skills import ([^\n]+)$", re.MULTILINE)

CALLBACK_RE = re.compile(
    r"^def audio_callback\(indata, frames, time_info, status\):.*?"
    r"(?=^def |^# ─)", re.MULTILINE | re.DOTALL)

CALLBACK_NEW = MARKER + '''

def audio_callback(block):
    """
    Called from the audio thread with 1280 float32 samples at 16 kHz.

    mic.py has already resampled from whatever rate the device actually runs at,
    so everything downstream still sees the chunk size it was written against.
    """
    audio_queue.put((block * 32767).astype(np.int16))
    if is_capturing_command.is_set():
        command_capture_queue.put(block.reshape(-1, 1).copy())


'''

# get_target_mic_index guessed by name; mic.py decides by evidence.
TARGET_RE = re.compile(
    r"^def get_target_mic_index\(\):.*?(?=^def |^# ─)", re.MULTILINE | re.DOTALL)

STREAM_RE = re.compile(
    r"[ \t]*mic_index = get_target_mic_index\(\)\n\n?"
    r"[ \t]*with sd\.InputStream\([^)]*\):\n"
    r"([ \t]*print\(f?\"[^\n]*\n)+")

STREAM_NEW = '''    # The microphone is opened through mic.py, which has already verified that
    # this device produces sound. If none does, say so once and stop rather than
    # pretending to listen to a stream of zeros.
    try:
        microphone = mic.Microphone(on_block=audio_callback)
        microphone.start()
    except mic.MicError as e:
        print(f"\\n{e}\\n")
        speak("I cannot hear anything, Sir. My microphone access is blocked.")
        return
    except Exception as e:
        print(f"\\n  [Mic] Could not open any microphone: {type(e).__name__}: {e}\\n")
        speak("I cannot open the microphone, Sir.")
        return

    deaf_reported = False

    try:
        print(f"  Say 'Hey {wake_word.PHRASE.title()}' to activate...\\n")
'''

# Woven into the listening loop so a mid-run revocation is noticed.
DEAF_ANCHOR = """            audio_data = audio_queue.get()"""
DEAF_NEW = """            audio_data = audio_queue.get()

            # Permission can be revoked, or a device unplugged, while running.
            # macOS reports neither; the stream just turns to zeros.
            if microphone.went_deaf() and not deaf_reported:
                deaf_reported = True
                print("\\n  [Mic] The microphone has gone silent. Run:  diablo doctor\\n")
                speak("I've stopped hearing anything, Sir. My microphone went quiet.")
"""


def restore() -> int:
    backups = sorted(glob.glob(BRIDGE + ".micbak-*"))
    if not backups:
        print("✗ No microphone-layer backups found.")
        return 1
    shutil.copy2(backups[-1], BRIDGE)
    print(f"✓ Restored from {backups[-1]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()

    if args.restore:
        return restore()
    if not os.path.exists(BRIDGE):
        print(f"✗ Cannot find {BRIDGE}")
        return 1

    source = original = open(BRIDGE).read()
    if MARKER in source:
        print("= The microphone layer is already installed. Nothing to do.")
        return 0

    steps = []

    matches = list(SKILL_IMPORT_RE.finditer(source))
    if not matches:
        print("✗ Could not find any 'from skills import' line.")
        return 1
    already = set()
    for match in matches:
        already.update(n.strip() for n in match.group(1).split(","))
    missing = [n for n in REQUIRED_SKILL_IMPORTS if n not in already]
    if missing:
        first = matches[0]
        source = (source[:first.end()] +
                  "\n# Added by patch_mic: these are called below but were never "
                  "imported.\nfrom skills import " + ", ".join(missing) +
                  source[first.end():])
        steps.append("import " + ", ".join(missing))
    else:
        steps.append("skill imports already complete")

    source, count = CALLBACK_RE.subn(lambda m: CALLBACK_NEW, source, count=1)
    if not count:
        print("✗ Could not find audio_callback().")
        return 1
    steps.append("audio_callback now takes resampled 16 kHz blocks")

    source, count = TARGET_RE.subn("", source, count=1)
    if count:
        steps.append("remove get_target_mic_index() (guessed by name)")

    source, count = STREAM_RE.subn(lambda m: STREAM_NEW, source, count=1)
    if not count:
        print("✗ Could not find the InputStream block.")
        return 1
    steps.append("open the verified device at its native rate")

    if DEAF_ANCHOR in source:
        source = source.replace(DEAF_ANCHOR, DEAF_NEW, 1)
        steps.append("notice if the microphone goes silent mid-run")

    for step in steps:
        print(f"  + {step}")

    try:
        compile(source, BRIDGE, "exec")
    except SyntaxError as e:
        print(f"\n✗ The patched file would not compile: {e.msg} (line {e.lineno})")
        return 1

    if args.dry_run:
        preview = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "voice_bridge.mic-preview.py")
        open(preview, "w").write(source)
        print(f"\n✓ Dry run clean, compiles. Preview: {preview}")
        return 0

    backup = f"{BRIDGE}.micbak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(BRIDGE, backup)
    open(BRIDGE, "w").write(source)
    print(f"\n✓ Patched ({len(original.splitlines())} → {len(source.splitlines())} lines)")
    print(f"  Backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
