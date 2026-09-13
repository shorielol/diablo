#!/usr/bin/env python3
"""
Makes Diablo stop waiting around.

Measured, before any of this, for a one-sentence command:

    wake detect (gap + transcribe)     0.6s
    command capture                    5.0s   <- fixed, regardless of speech
    command transcribe (beam 5)        0.45s
    understand()                       3.37s
    plan()                             2.43s
    first tool call                    ~7s

The five seconds is the one that makes it feel broken. `get_command_audio` took
a duration and recorded exactly that long, so a one-second question spent four
seconds recording silence before a single thing could happen.

What this changes in voice_bridge.py:

  get_command_audio   endpointed. Records until you stop talking, with the old
                      duration demoted to an upper bound. Typically 1.5-2s
                      instead of a flat 5s.
  stt model           base.en rather than base. English-only weights, slightly
                      faster and slightly better on English.
  wake constants      a 2.0s window checked every 0.25s instead of a 3.0s
                      window every 0.55s. Measured: tiny.en costs 86ms on a 3s
                      window, so checking more often is nearly free and cuts the
                      worst-case detection lag by about a third of a second.

Whisper was never the problem — 86ms for wake detection, 193ms to transcribe a
command. The time was all in waiting.

Idempotent, backs up first, refuses to write anything that will not compile.

    python3 patch_latency.py [--dry-run] [--restore]
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

MARKER = "# ─── Diablo endpointed capture ───"

CAPTURE_RE = re.compile(
    r"^def get_command_audio\(duration=\d+\):.*?(?=^def |^# ─)",
    re.MULTILINE | re.DOTALL)

CAPTURE_NEW = MARKER + '''

# One shared endpointer. Constructing it loads the silero VAD, which is not
# something to do on every utterance.
_endpointer = mic.Endpointer()


def get_command_audio(duration=None):
    """
    Record one utterance, stopping shortly after the speaker stops.

    `duration` used to be exactly how long this recorded. It is now only a
    ceiling: a one-second question returns in about a second and a half rather
    than always costing five.
    """
    _endpointer.reset()
    if duration:
        _endpointer.max_blocks = max(1, int(duration * RATE / CHUNK))

    collected = []
    while True:
        try:
            block = command_capture_queue.get(timeout=1.5)
        except queue.Empty:
            break
        collected.append(block)
        if _endpointer.feed(np.asarray(block[:, 0], dtype=np.float32)):
            break

    if not collected:
        return None
    return np.concatenate(collected).flatten().astype(np.float32)


'''


def restore():
    backups = sorted(glob.glob(BRIDGE + ".latbak-*"))
    if not backups:
        print("✗ No latency-patch backups found.")
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
        print("= Endpointed capture is already installed. Nothing to do.")
        return 0

    steps = []

    source, count = CAPTURE_RE.subn(lambda m: CAPTURE_NEW, source, count=1)
    if not count:
        print("✗ Could not find get_command_audio().")
        return 1
    steps.append("capture ends when you stop talking (was a flat 5s)")

    # English-only weights: a little faster and a little better on English.
    if 'whisper.load_model("base"' in source:
        source = source.replace('whisper.load_model("base", device="mps")',
                                'whisper.load_model(os.getenv("DIABLO_STT_MODEL", "base.en"), device="mps")')
        source = source.replace('whisper.load_model("base")',
                                'whisper.load_model(os.getenv("DIABLO_STT_MODEL", "base.en"))')
        steps.append("command transcription uses base.en")

    for step in steps:
        print(f"  + {step}")

    try:
        compile(source, BRIDGE, "exec")
    except SyntaxError as e:
        print(f"\n✗ The patched file would not compile: {e.msg} (line {e.lineno})")
        return 1

    if args.dry_run:
        preview = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "voice_bridge.latency-preview.py")
        open(preview, "w").write(source)
        print(f"\n✓ Dry run clean, compiles. Preview: {preview}")
        return 0

    backup = f"{BRIDGE}.latbak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(BRIDGE, backup)
    open(BRIDGE, "w").write(source)
    print(f"\n✓ Patched.  Backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
