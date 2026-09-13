#!/usr/bin/env python3
"""
`diablo doctor` — finds out why Diablo is not working, and fixes what it can.

Written after a long chase where the microphone was blamed on permissions, then
on the Python version, then on PortAudio, then on which terminal was being used.
Only one of those was close, and none of it was measured. This measures.

It checks, in the order that matters:

  1. The interpreter and the packages that must be importable.
  2. The app bundle, because that is what owns the microphone permission.
  3. The microphone itself — by recording from every input device and looking at
     what comes back. Silence and a timeout mean different things and are
     reported differently.
  4. Ollama and the model, without which nothing is understood.
  5. The voice, which is allowed to be missing.
  6. The Whisper weights and the wake word.

Anything it can repair on its own, it repairs. Anything it cannot, it prints as
one concrete instruction rather than a list of possibilities.

    python3 doctor.py            check, and fix what is safe to fix
    python3 doctor.py --check    report only
"""

import argparse
import json
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.join(HOME, ".diablo"))
APP = os.path.join(HOME, "Applications", "Diablo.app")
VENV_PY = os.path.join(DIABLO_HOME, "venv", "bin", "python3")

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

problems = []


def ok(label, detail=""):
    print(f"  {GREEN}✓{RESET} {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def bad(label, fix, detail=""):
    print(f"  {RED}✗{RESET} {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    problems.append((label, fix))


def warn(label, detail=""):
    print(f"  {YELLOW}!{RESET} {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def run(args, timeout=20):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


# ─── 1. Interpreter ──────────────────────────────────────────────────────────

def check_python():
    print("\nInterpreter")
    if not os.path.exists(VENV_PY):
        bad("no virtualenv", f"The venv is missing. Reinstall: bash install.sh",
            VENV_PY)
        return False
    version = run([VENV_PY, "--version"])
    ok("virtualenv", version.stdout.strip() if version else "")

    required = ["numpy", "sounddevice", "whisper", "openwakeword", "scipy"]
    missing = []
    for module in required:
        probe = run([VENV_PY, "-c", f"import {module}"], timeout=60)
        if probe is None or probe.returncode != 0:
            missing.append(module)
    if missing:
        bad(f"cannot import: {', '.join(missing)}",
            f"{os.path.join(DIABLO_HOME, 'venv/bin/pip')} install " + " ".join(missing))
        return False
    ok("audio and speech packages import", ", ".join(required))
    return True


# ─── 2. App bundle ───────────────────────────────────────────────────────────

def check_app(fix=True):
    print("\nApp identity (this is what owns the microphone permission)")
    if not os.path.isdir(APP):
        if fix:
            here = os.path.dirname(os.path.abspath(__file__))
            built = run([sys.executable, os.path.join(here, "make_app.py")], timeout=60)
            if built and built.returncode == 0 and os.path.isdir(APP):
                ok("built Diablo.app", APP)
            else:
                bad("Diablo.app is missing", "python3 make_app.py")
                return False
        else:
            bad("Diablo.app is missing", "python3 make_app.py")
            return False
    else:
        ok("Diablo.app exists", APP)

    signature = run(["codesign", "-dv", APP])
    blob = ((signature.stderr or "") + (signature.stdout or "")) if signature else ""
    if "Identifier=com.shaurya.diablo" in blob:
        ok("code signed", "ad-hoc" if "adhoc" in blob else "signed")
    else:
        bad("the bundle is not signed",
            "python3 make_app.py   (TCC will not remember an unsigned bundle)")

    plist = os.path.join(APP, "Contents", "Info.plist")
    check = run(["plutil", "-extract", "NSMicrophoneUsageDescription", "raw", plist])
    if check and check.returncode == 0:
        ok("declares why it needs the microphone")
    else:
        bad("no NSMicrophoneUsageDescription",
            "python3 make_app.py   (macOS kills apps that record without one)")
    return True


# ─── 3. The microphone ───────────────────────────────────────────────────────

def check_microphone(fix=True):
    print("\nMicrophone")
    here = os.path.dirname(os.path.abspath(__file__))
    mic_py = os.path.join(DIABLO_HOME, "skills", "mic.py")
    if not os.path.exists(mic_py):
        mic_py = os.path.join(here, "mic.py")
    if not os.path.exists(mic_py):
        bad("mic.py is not installed", "cp mic.py ~/.diablo/skills/")
        return False

    probe = run([VENV_PY, mic_py], timeout=180)
    output = (probe.stdout or "") if probe else ""
    if probe is None:
        bad("the probe hung", "Launch Diablo.app once and allow the microphone")
        return False

    for line in output.splitlines():
        stripped = line.strip()
        if "SIGNAL" in stripped:
            ok(stripped.split(" ... ")[0].strip(), stripped.split("...")[-1].strip())
        elif "pure silence" in stripped:
            warn(stripped.split(" ... ")[0].strip(), "opened, but silent")
        elif "timed out" in stripped:
            warn(stripped.split(" ... ")[0].strip(), "timed out")

    if probe.returncode == 0 and "✓ Using" in output:
        chosen = [l for l in output.splitlines() if "✓ Using" in l][0]
        ok("a working microphone was found", chosen.split("✓ Using")[-1].strip())
        return True

    # A hardware mic that will not open, next to a virtual one that will, is the
    # signature of a TCC denial: virtual devices are not permission-gated.
    hardware_blocked = ("hardware mic returning zeros" in output
                        or "timed out" in output)

    fix = ["Open Finder > Applications and DOUBLE-CLICK Diablo.",
           "  macOS only shows the microphone dialog for an app you launch",
           "  yourself. Starting it from a terminal, a script or an AI agent",
           "  attributes the request to that process instead, and it is",
           "  refused without ever asking you.",
           "Then: click Allow."]
    if hardware_blocked:
        fix.append("If no dialog appears:  sudo killall coreaudiod   and retry.")
    bad("no microphone produced any audio", "\n      ".join(fix))
    return False


# ─── 4. Brain ────────────────────────────────────────────────────────────────

def check_coreaudio():
    print("\nCoreAudio")
    proc = run(["ps", "-eo", "etime,comm"])
    line = next((l for l in (proc.stdout or "").splitlines()
                 if "coreaudiod" in l), None) if proc else None
    if not line:
        bad("coreaudiod is not running", "sudo killall coreaudiod   (it respawns)")
        return
    uptime = line.split()[0]
    # A daemon that has been up for days is the usual suspect when every device
    # opens cleanly and every device returns silence.
    stale = "-" in uptime
    if stale:
        warn(f"coreaudiod has been up {uptime}",
             "if the mic stays silent: sudo killall coreaudiod")
    else:
        ok("coreaudiod running", uptime)


def check_brain():
    print("\nLocal model")
    here = os.path.dirname(os.path.abspath(__file__))
    brain = os.path.join(DIABLO_HOME, "skills", "local_brain.py")
    if not os.path.exists(brain):
        brain = os.path.join(here, "local_brain.py")
    probe = run([VENV_PY, "-c", f"""
import sys; sys.path.insert(0, {os.path.dirname(brain)!r})
import local_brain, json
print(json.dumps({{"up": local_brain.available(),
                   "models": local_brain.installed_models(),
                   "want": local_brain.CHAT_MODEL}}))
"""], timeout=30)
    if probe is None or probe.returncode != 0:
        bad("could not query Ollama", "Start Ollama, then rerun")
        return False
    try:
        state = json.loads((probe.stdout or "").strip().splitlines()[-1])
    except Exception:
        bad("could not read Ollama's reply", "ollama serve")
        return False

    if not state["up"]:
        bad("Ollama is not responding", "open -a Ollama   (or: ollama serve)")
        return False
    ok("Ollama is up", f"{len(state['models'])} models")
    if state["want"] not in state["models"]:
        bad(f"{state['want']} is not installed", f"ollama pull {state['want']}")
        return False
    ok("reasoning model present", state["want"])
    return True


# ─── 5. Voice and speech ─────────────────────────────────────────────────────

def check_voice():
    print("\nVoice")
    voicebox = run(["curl", "-s", "--max-time", "3",
                    os.getenv("DIABLO_VOICEBOX_URL", "http://127.0.0.1:17493") + "/profiles"])
    if voicebox and voicebox.returncode == 0 and voicebox.stdout.strip():
        ok("Voicebox is running")
        # Running is not the same as usable. A profile name that matches nothing
        # falls back to the macOS voice without saying so, which is how the
        # wrong voice went unnoticed for a whole session.
        probe = run([VENV_PY, "-c", f"""
import sys; sys.path.insert(0, {os.path.join(DIABLO_HOME, 'skills')!r})
import voice
print(voice.VOICE_ID, voice._get_profile_id())
"""], timeout=30)
        line = (probe.stdout or "").strip() if probe else ""
        if line and not line.endswith("None"):
            ok("voice profile resolves", line.split()[0])
        else:
            bad(f"no Voicebox profile named {line.split()[0] if line else '?'}",
                "Open Voicebox > Voices and check the profile name matches\n"
                "      DIABLO_VOICE_PROFILE in ~/.diablo/.env")
    else:
        warn("Voicebox is not responding", "Diablo will use the macOS voice")

    if run(["which", "say"]):
        ok("macOS fallback voice available", "say -v Daniel")


def check_speech():
    print("\nSpeech recognition")
    cache = os.path.join(HOME, ".cache", "whisper")
    models = sorted(f for f in os.listdir(cache)) if os.path.isdir(cache) else []
    if not models:
        bad("no Whisper weights cached",
            "They download on first run; make sure you are online once.")
    else:
        ok("Whisper weights cached", ", ".join(m.replace(".pt", "") for m in models))

    here = os.path.dirname(os.path.abspath(__file__))
    wake = os.path.join(DIABLO_HOME, "skills", "wake_word.py")
    if not os.path.exists(wake):
        wake = os.path.join(here, "wake_word.py")
    probe = run([VENV_PY, "-c", f"""
import sys; sys.path.insert(0, {os.path.dirname(wake)!r})
import wake_word
print(wake_word.PHRASE)
"""], timeout=60)
    if probe and probe.returncode == 0:
        ok("wake word loaded", f"'Hey {probe.stdout.strip().title()}'")
    else:
        bad("wake_word failed to load",
            (probe.stderr or "").strip().splitlines()[-1] if probe and probe.stderr
            else "check the logs")


# ─── Report ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report only, fix nothing")
    args = parser.parse_args()
    fix = not args.check

    print(f"\n{'Diablo doctor':^64}")
    print("=" * 64)
    print(f"  home   {DIABLO_HOME}")

    if check_python():
        check_app(fix=fix)
        check_microphone(fix=fix)
        check_coreaudio()
        check_brain()
        check_voice()
        check_speech()

    print("\n" + "=" * 64)
    if not problems:
        print(f"{GREEN}Everything checks out.{RESET}")
        print("\n  Start it:  diablo")
        return 0

    print(f"{RED}{len(problems)} problem{'s' if len(problems) > 1 else ''} to fix, "
          f"most important first:{RESET}\n")
    for index, (label, fix_text) in enumerate(problems, 1):
        print(f"  {index}. {label}")
        for line in fix_text.splitlines():
            print(f"      {line}")
        print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
