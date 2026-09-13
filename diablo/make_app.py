#!/usr/bin/env python3
"""
Builds Diablo.app, so the microphone prompt has somewhere to appear.

macOS assigns a *responsible process* when a process is spawned, and children
inherit it. Started from a shell, that is the terminal — which is why access
worked in Terminal.app but not Cursor's, why `tccutil reset Microphone` wiped it,
and why the replacement prompt never came back: prompts are only raised against
GUI apps. Denied access is not an error either; macOS simply hands over endless
zeros, which was "AUDIO IS ALL ZEROS".

An app bundle has its own identity, so everything it spawns is attributed to
Diablo rather than to whatever shell was involved.

One design was tried and abandoned, and the note is here so it is not tried
again: copying the Python interpreter into the bundle so the running *image*
would be bundle-internal. That binary would not start under LaunchServices at
all — no output, not even a trace written from `sitecustomize`. It was also
solving a problem that does not exist, since responsibility is fixed at spawn
time and `exec` does not change it. The launcher is a shell script again.

The bundle alone is not sufficient. mic.py must also touch the microphone from
the app's own long-lived process (`request_access`), because a prompt raised
against a probe subprocess that gets killed seconds later never reaches the
screen.

    python3 make_app.py            build or rebuild
    python3 make_app.py --run      build, then launch
"""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys

HOME = os.path.expanduser("~")
DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.join(HOME, ".diablo"))
APP_DIR = os.path.join(HOME, "Applications", "Diablo.app")
BUNDLE_ID = "com.shaurya.diablo"
VENV_PY = os.path.join(DIABLO_HOME, "venv", "bin", "python3")

UNUSED_SITECUSTOMIZE = '''"""
Started automatically when the bundle's interpreter boots.

LaunchServices runs a bundle executable with no arguments, so there is no script
name to hand it. `sitecustomize` is imported by `site` during startup, which
gives us a documented place to take over.

The argv guard matters: the microphone probe re-invokes this same interpreter
with `python -c ...`, and without the guard every probe would boot a second
assistant.
"""

import os
import sys

if sys.argv == [""]:
    os.environ.setdefault("DIABLO_HOME", {home!r})
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    os.environ["DIABLO_IN_BUNDLE"] = "1"

    home = {home!r}
    log = os.path.join(home, "diablo.log")
    bridge = os.path.join(home, "voice_bridge.py")

    os.chdir(home)
    sys.path.insert(0, home)

    # Load .env by hand. python-dotenv is available, but a broken line in .env
    # should not stop the assistant from starting.
    env_path = os.path.join(home, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path) as handle:
                for line in handle:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        except OSError:
            pass

    stream = open(log, "a", buffering=1)
    sys.stdout = stream
    sys.stderr = stream
    print("\\n=== Diablo starting (in-bundle interpreter) ===")
    print("  microphone access belongs to Diablo.app, not to any terminal")

    import runpy

    try:
        runpy.run_path(bridge, run_name="__main__")
    except SystemExit:
        pass
    except BaseException:
        import traceback
        traceback.print_exc()
    finally:
        stream.flush()
        os._exit(0)
'''


LAUNCHER = """#!/bin/bash
# Diablo.app launcher.
#
# A shell script, deliberately. An earlier version copied the Python
# interpreter into the bundle to make the *running image* bundle-internal; that
# binary would not start under LaunchServices at all. It was also unnecessary:
# TCC assigns a responsible process when a process is spawned, and children
# inherit it, so everything below is attributed to Diablo.app regardless of
# which interpreter ends up executing.

DIABLO_HOME="{home}"
LOG="$DIABLO_HOME/diablo.log"
PY="$DIABLO_HOME/venv/bin/python3"

cd "$DIABLO_HOME" || exit 1

if [ -f "$DIABLO_HOME/.env" ]; then
    set -a
    . "$DIABLO_HOME/.env" 2>/dev/null || true
    set +a
fi

export DIABLO_HOME
export PYTHONUNBUFFERED=1

echo "" >> "$LOG"
echo "=== Diablo starting $(date) ===" >> "$LOG"

exec "$PY" -u "$DIABLO_HOME/voice_bridge.py" >> "$LOG" 2>&1
"""

def real_interpreter() -> str:
    if not os.path.exists(VENV_PY):
        raise SystemExit(f"✗ No interpreter at {VENV_PY}")
    return os.path.realpath(VENV_PY)


def build(verbose: bool = True) -> str:
    if not os.path.isdir(DIABLO_HOME):
        raise SystemExit(f"✗ Diablo is not installed at {DIABLO_HOME}")

    interpreter = real_interpreter()

    # Always rebuild. A stale executable inside a signed bundle is exactly the
    # sort of mismatch that makes TCC refuse without explaining itself.
    if os.path.exists(APP_DIR):
        shutil.rmtree(APP_DIR)

    contents = os.path.join(APP_DIR, "Contents")
    macos_dir = os.path.join(contents, "MacOS")
    os.makedirs(macos_dir)
    os.makedirs(os.path.join(contents, "Resources"))

    launcher_path = os.path.join(macos_dir, "Diablo")
    with open(launcher_path, "w") as f:
        f.write(LAUNCHER.format(home=DIABLO_HOME))
    os.chmod(launcher_path, 0o755)

    info = {
        "CFBundleName": "Diablo",
        "CFBundleDisplayName": "Diablo",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": "1.1.0",
        "CFBundleShortVersionString": "1.1",
        "CFBundleExecutable": "Diablo",
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "12.0",
        # Required. A process that touches the microphone without one is killed
        # outright, and there would be nothing to show in the prompt.
        "NSMicrophoneUsageDescription":
            "Diablo listens for the wake word and for the commands you speak to "
            "it. Audio is transcribed on this Mac and never leaves it.",
        "NSSpeechRecognitionUsageDescription":
            "Diablo transcribes your speech locally using Whisper.",
        "NSAppleEventsUsageDescription":
            "Diablo controls the apps you ask it to, such as opening a browser "
            "tab or sending a message.",
        # Deliberately NOT LSUIElement. A background-only agent is the less
        # reliable place for a permission prompt to appear, and a visible app is
        # something you can see running and quit with Cmd-Q.
    }
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        plistlib.dump(info, f)

    with open(os.path.join(contents, "PkgInfo"), "w") as f:
        f.write("APPL????")

    # TCC keys its decisions on the bundle signature. Unsigned means re-prompted
    # at best and silently denied at worst.
    signed = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", APP_DIR],
        capture_output=True, text=True)

    lsregister = ("/System/Library/Frameworks/CoreServices.framework/Versions/A/"
                  "Frameworks/LaunchServices.framework/Versions/A/Support/lsregister")
    if os.path.exists(lsregister):
        subprocess.run([lsregister, "-f", APP_DIR], capture_output=True)

    if verbose:
        print(f"✓ Built {APP_DIR}")
        print(f"  identity     {BUNDLE_ID}")
        print(f"  runs         {interpreter}")
        print(f"  signature    {'ad-hoc' if signed.returncode == 0 else 'FAILED: ' + signed.stderr.strip()[:100]}")
    return APP_DIR


def verify() -> bool:
    """Prove the bundle actually starts and reaches Python."""
    launcher_path = os.path.join(APP_DIR, "Contents", "MacOS", "Diablo")
    if not (os.path.exists(launcher_path) and os.access(launcher_path, os.X_OK)):
        print("✗ The bundle has no executable launcher.")
        return False
    probe = subprocess.run(
        [os.path.join(DIABLO_HOME, "venv", "bin", "python3"), "-c",
         "import sys, numpy, sounddevice; print('python OK', sys.version.split()[0],"
         "'| numpy', numpy.__version__, '| sounddevice', sounddevice.__version__)"],
        capture_output=True, text=True, timeout=180)
    if probe.returncode != 0:
        print("✗ The interpreter cannot import what it needs:")
        print("   ", (probe.stderr or "").strip().splitlines()[-1:])
        return False
    print(f"  {probe.stdout.strip()}")
    return True


def launch() -> None:
    subprocess.run(["open", "-a", APP_DIR], check=False)
    print("\n▸ Diablo launched.")
    print("  macOS will ask for microphone access — click Allow. It asks once,")
    print("  in Diablo's own name, and it sticks from then on.")
    print(f"\n  Watch it:  tail -f {os.path.join(DIABLO_HOME, 'diablo.log')}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="launch after building")
    args = parser.parse_args()
    build()
    print()
    if not verify():
        return 1
    if args.run:
        launch()
    else:
        print("\n  Launch it with:  open -a Diablo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
