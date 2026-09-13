#!/usr/bin/env python3
"""
Trades the 9 GB permanent resident for a warm-up that starts when you speak.

Measured on this machine:

    llama-server holding qwen3:14b      9,078 MB   (97% of Diablo's footprint)
    voice_bridge.py (whisper + torch)     255 MB
    everything else (MCP, Voicebox)       <70 MB

So there is exactly one thing worth optimising. `keep_alive=30m` — which I set
to kill reload latency — is what pins it there whether you are using Diablo or
not.

The numbers that decide the design:

    warm turn                0.10s
    tool-calling turn warm   1.86s
    cold start               6.54s
    natural cover available  ~2.7s   (2.3s capture + 0.4s transcribe)

Pre-warming the moment the wake word fires hides 2.7s of a cold start behind
time that was being spent anyway. Combined with a shorter keep_alive, the model
stays resident while you are actually talking to Diablo and gives the memory
back when you are not.

DIABLO_LOW_MEMORY=1 goes further: the model is released after every command and
the wake and command transcription share one Whisper model. Idle footprint drops
to roughly 150 MB, and every command pays the full cold start.

    python3 patch_memory.py [--dry-run] [--restore]
"""

import argparse
import glob
import os
import shutil
import sys
import time

BRIDGE = os.getenv("DIABLO_BRIDGE", os.path.join(
    os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")), "voice_bridge.py"))

MARKER = "# ─── Diablo warm-on-wake ───"

ANCHOR = '''def handle_utterance(command):'''

NEW = MARKER + '''

def _warm_the_brain():
    """
    Start loading the model the instant the wake word fires.

    A cold start costs 6.54s. Capturing and transcribing what you just said
    costs about 2.7s, and that happens anyway — so beginning the load now means
    the model is at worst 3.8s behind instead of 6.5s, and usually ready before
    the transcription finishes. Failures are ignored on purpose: this is an
    optimisation, and the real call will report any genuine problem.
    """
    try:
        local_brain.warm()
    except Exception:
        pass


def handle_utterance(command):
    threading.Thread(target=_warm_the_brain, daemon=True).start()
'''


def restore():
    backups = sorted(glob.glob(BRIDGE + ".membak-*"))
    if not backups:
        print("✗ No memory-patch backups found.")
        return 1
    shutil.copy2(backups[-1], BRIDGE)
    print(f"✓ Restored from {backups[-1]}")
    return 0


def main():
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
        print("= Warm-on-wake is already installed. Nothing to do.")
        return 0
    if ANCHOR not in source:
        print("✗ Could not find handle_utterance().")
        return 1

    source = source.replace(ANCHOR, NEW, 1)
    print("  + start loading the model the moment the wake word fires")

    try:
        compile(source, BRIDGE, "exec")
    except SyntaxError as e:
        print(f"\n✗ The patched file would not compile: {e.msg} (line {e.lineno})")
        return 1

    if args.dry_run:
        preview = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "voice_bridge.memory-preview.py")
        open(preview, "w").write(source)
        print(f"\n✓ Dry run clean, compiles. Preview: {preview}")
        return 0

    backup = f"{BRIDGE}.membak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(BRIDGE, backup)
    open(BRIDGE, "w").write(source)
    print(f"\n✓ Patched.  Backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
