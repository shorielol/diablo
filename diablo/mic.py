"""
D.I.A.B.L.O. Microphone
=======================
Getting audio out of a Mac, reliably.

The old code did this:

    sd.InputStream(device=guess_by_name(), samplerate=16000, ...)

and every part of that line is a way to fail silently on macOS:

* **Guessing the device by name.** It looked for "xm5" then "macbook ...
  microphone". A Teams or Zoom virtual device, a disconnected headset that still
  advertises an input, or a renamed device, and you are recording from something
  that will never carry your voice. Nothing errors. You just get silence.
* **Demanding 16 kHz.** The built-in mic runs at 44100. Asking CoreAudio to
  reconfigure it to 16000 works on some macOS versions and quietly yields
  nothing on others. Opening at the device's own rate and resampling in Python
  removes the negotiation entirely.
* **Never checking.** If TCC has not granted microphone access, macOS does not
  raise — it hands the process an endless stream of zeros. Anything that does
  not explicitly test for silence will sit there "listening" forever.

So this module picks the device by *evidence*: it records from each candidate
and keeps the one that produces actual signal. Probing runs in a subprocess with
a hard timeout, because PortAudio blocks inside C when TCC is undecided and no
amount of Python threading will interrupt that.
"""

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

TARGET_RATE = 16000
CHUNK = 1280                      # samples at TARGET_RATE, as the bridge expects
PROBE_SECONDS = float(os.getenv("DIABLO_MIC_PROBE_SECONDS", "1.0"))
PROBE_TIMEOUT = float(os.getenv("DIABLO_MIC_PROBE_TIMEOUT", "12"))
SILENCE_PEAK = 1e-6               # below this a block is bit-for-bit silence
# Shared with wake_word.py on purpose: one dial for "is this speech", so tuning
# it does not fix wake detection and quietly break endpointing. The reference
# below was undefined, and the bare except around it meant the VAD silently
# never ran — every utterance was endpointed on raw energy instead.
VAD_THRESHOLD = float(os.getenv("DIABLO_VAD_THRESHOLD", "0.08"))

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
STATE_PATH = os.path.join(HOME, "mic_device.json")

# Virtual devices that exist to carry conference-call audio. They present an
# input, they are happy to be opened, and they will never hear you.
VIRTUAL_HINTS = ("teams", "zoom", "blackhole", "soundflower", "loopback",
                 "aggregate", "multi-output", "obs", "krisp", "voicemeeter")


class MicError(Exception):
    """Raised with something the user can actually act on."""


# ─── Device listing ──────────────────────────────────────────────────────────

def input_devices() -> List[Dict[str, Any]]:
    import sounddevice as sd
    out = []
    for index, device in enumerate(sd.query_devices()):
        if device.get("max_input_channels", 0) < 1:
            continue
        out.append({
            "index": index,
            "name": device["name"],
            "channels": int(device["max_input_channels"]),
            "rate": int(device.get("default_samplerate") or 48000),
            "virtual": any(h in device["name"].lower() for h in VIRTUAL_HINTS),
        })
    return out


def _rank(device: Dict[str, Any]) -> Tuple[int, int]:
    """Most-likely-to-be-a-real-microphone first."""
    name = device["name"].lower()
    if device["virtual"]:
        return (3, device["index"])
    if "macbook" in name and "microphone" in name:
        return (0, device["index"])
    if any(w in name for w in ("airpods", "xm5", "wh-1000", "headset", "buds")):
        return (1, device["index"])
    return (2, device["index"])


class Endpointer:
    """
    Decides when someone has finished speaking.

    The old capture recorded for a flat five seconds regardless of what was
    said, so a one-second question cost four seconds of dead air before anything
    could even begin. This watches the blocks go past and stops shortly after
    the talking stops, which is how every assistant that feels quick does it.

    Deliberately forgiving at the start and strict at the end: it waits up to
    `lead_in` for speech to begin at all, but once it has heard something it
    only needs `hang_time` of quiet to call it finished.
    """

    def __init__(self,
                 hang_time: float = float(os.getenv("DIABLO_HANG_TIME", "0.55")),
                 lead_in: float = float(os.getenv("DIABLO_LEAD_IN", "1.8")),
                 max_seconds: float = float(os.getenv("DIABLO_MAX_UTTERANCE", "12.0")),
                 min_speech: float = 0.20):
        self.hang_blocks = max(1, int(hang_time * TARGET_RATE / CHUNK))
        self.lead_in_blocks = max(1, int(lead_in * TARGET_RATE / CHUNK))
        self.max_blocks = max(1, int(max_seconds * TARGET_RATE / CHUNK))
        self.min_speech_blocks = max(1, int(min_speech * TARGET_RATE / CHUNK))
        self.reset()

        try:
            from openwakeword.vad import VAD
            self.vad = VAD()
        except Exception:
            self.vad = None

    def reset(self) -> None:
        self.blocks = 0
        self.speech_blocks = 0
        self.quiet_run = 0
        self.started = False

    def _is_speech(self, block) -> bool:
        import numpy as np
        if self.vad is not None:
            try:
                if float(self.vad.predict(block)) >= VAD_THRESHOLD:
                    return True
            except Exception:
                pass
        return float(np.sqrt(np.mean(np.square(block)))) > 0.010

    def feed(self, block) -> bool:
        """Push one block. Returns True when the utterance is over."""
        self.blocks += 1
        if self._is_speech(block):
            self.started = True
            self.speech_blocks += 1
            self.quiet_run = 0
        else:
            self.quiet_run += 1

        if self.blocks >= self.max_blocks:
            return True
        if not self.started:
            # Nothing yet. Give them a moment to start, then give up.
            return self.blocks >= self.lead_in_blocks
        if self.speech_blocks < self.min_speech_blocks:
            return False
        return self.quiet_run >= self.hang_blocks


def who_holds_the_input() -> List[str]:
    """
    Other processes with the CoreAudio HAL open.

    Worth checking before blaming permissions: a client that hangs while holding
    the input device makes every later open return silence, and nothing in the
    logs says so.
    """
    try:
        proc = subprocess.run(["lsof"], capture_output=True, text=True, timeout=25)
    except Exception:
        return []
    names = set()
    for line in (proc.stdout or "").splitlines():
        if "CoreAudio.component/Contents/MacOS/CoreAudio" in line:
            parts = line.split()
            if parts and parts[0] not in ("coreaudiod", "Python", "python3.14"):
                names.add(parts[0])
    # Things that always have it loaded and are not holding the input.
    benign = {"WidgetWal", "PowerChim", "Google", "zen", "Music", "callservi"}
    return sorted(n for n in names if n not in benign)


# ─── Asking for access ───────────────────────────────────────────────────────

def request_access(timeout: float = 90.0) -> Dict[str, Any]:
    """
    Touch the microphone from *this* process, so macOS has somewhere to put the
    permission dialog.

    This is the step that was missing. Probing runs in subprocesses, for good
    reasons — PortAudio blocks uninterruptibly inside CoreAudio when TCC has not
    decided, and only a separate process can be killed out of that. But a prompt
    raised against a short-lived subprocess that gets killed twelve seconds later
    never makes it onto the screen. The first touch has to happen here, in the
    long-lived process that the app bundle is responsible for, and it has to be
    willing to sit there while the dialog is on screen waiting to be clicked.

    Returns {"granted": bool, ...}. Never raises.
    """
    import threading

    outcome: Dict[str, Any] = {}

    def attempt():
        try:
            import numpy as np
            import sounddevice as sd
            recording = sd.rec(int(0.3 * TARGET_RATE), samplerate=TARGET_RATE,
                               channels=1, dtype="float32", blocking=True)
            data = np.asarray(recording).flatten()
            outcome["peak"] = float(np.max(np.abs(data))) if data.size else 0.0
            outcome["opened"] = True
        except Exception as e:
            outcome["error"] = f"{type(e).__name__}: {e}"

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        # Still blocked. That is the dialog sitting on screen unanswered, or a
        # decision macOS will not make. The thread is a daemon; abandon it.
        outcome["pending"] = True
    outcome["granted"] = bool(outcome.get("opened"))
    return outcome


# ─── Probing ─────────────────────────────────────────────────────────────────

_PROBE_SCRIPT = r"""
import json, sys
import numpy as np
import sounddevice as sd

index = int(sys.argv[1]); rate = int(sys.argv[2]); seconds = float(sys.argv[3])
try:
    frames = int(rate * seconds)
    rec = sd.rec(frames, samplerate=rate, channels=1, dtype='float32',
                 device=index, blocking=True)
    data = np.asarray(rec).flatten()
    print(json.dumps({
        "ok": True,
        "peak": float(np.max(np.abs(data))) if data.size else 0.0,
        "rms": float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0,
    }))
except Exception as e:
    print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}))
"""


def probe(index: int, rate: int, seconds: float = PROBE_SECONDS,
          timeout: float = PROBE_TIMEOUT) -> Dict[str, Any]:
    """
    Record briefly from one device and report what came back.

    Runs in a subprocess on purpose. When TCC has not decided about microphone
    access, PortAudio blocks inside CoreAudio and the process cannot be
    interrupted from Python — a thread would hang forever and take the assistant
    with it. A subprocess can simply be killed.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SCRIPT, str(index), str(rate), str(seconds)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "timeout": True,
                "error": "the device did not respond (macOS is most likely "
                         "waiting on a microphone permission decision)"}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "probe crashed").strip()[-200:]}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return {"ok": False, "error": (proc.stdout or proc.stderr or "no output")[-200:]}


def probe_all(seconds: float = PROBE_SECONDS, verbose: bool = True) -> List[Dict[str, Any]]:
    """Probe every input device, best candidates first. Never raises."""
    try:
        devices = sorted(input_devices(), key=_rank)
    except Exception as e:
        raise MicError(f"Could not enumerate audio devices: {e}")

    results = []
    for device in devices:
        if verbose:
            print(f"  probing [{device['index']}] {device['name']} "
                  f"@ {device['rate']} Hz ... ", end="", flush=True)
        result = probe(device["index"], device["rate"], seconds)
        record = dict(device)
        record.update(result)
        record["silent"] = bool(result.get("ok")) and result.get("peak", 0.0) <= SILENCE_PEAK
        results.append(record)
        if verbose:
            if not result.get("ok"):
                print("failed" if not result.get("timeout") else "timed out")
            elif record["silent"]:
                print("opened, but pure silence"
                      + ("  (virtual device — nothing is routed into it)"
                         if record["virtual"] else
                         "  (hardware mic returning zeros = access denied)"))
            else:
                print(f"SIGNAL (peak {result['peak']:.4f})")
    return results


def pick(results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The best device that actually produced sound."""
    live = [r for r in results if r.get("ok") and not r["silent"]]
    if not live:
        return None
    live.sort(key=lambda r: (_rank(r)[0], -r.get("rms", 0.0)))
    return live[0]


# ─── Remembering the answer ──────────────────────────────────────────────────

def save_choice(device: Dict[str, Any]) -> None:
    os.makedirs(HOME, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump({"index": device["index"], "name": device["name"],
                   "rate": device["rate"], "chosen_at": time.time()}, f, indent=2)


def load_choice() -> Optional[Dict[str, Any]]:
    """
    The remembered device, if it is still present *under the same name*.

    Indexes are not stable across reboots or when a device is plugged in, so the
    name is what gets verified. A remembered index pointing at a different
    device is worse than having no memory at all.
    """
    try:
        with open(STATE_PATH) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    for device in input_devices():
        if device["name"] == saved.get("name"):
            return device
    return None


def forget() -> None:
    try:
        os.unlink(STATE_PATH)
    except OSError:
        pass


# ─── Resampling ──────────────────────────────────────────────────────────────

def _ratio(native_rate: int) -> Tuple[int, int]:
    """Exact up/down factors from the device rate to 16 kHz."""
    from math import gcd
    divisor = gcd(TARGET_RATE, native_rate)
    return TARGET_RATE // divisor, native_rate // divisor


def native_blocksize(native_rate: int) -> int:
    """
    Input block that resamples to exactly CHUNK samples at 16 kHz.

    44100 -> 3528 and 48000 -> 3840, both exact, so no sample ever has to be
    carried between blocks and the chunk contract the rest of the code relies on
    stays intact.
    """
    up, down = _ratio(native_rate)
    return CHUNK * down // up


def make_resampler(native_rate: int):
    """A function turning one native block into CHUNK samples at 16 kHz."""
    if native_rate == TARGET_RATE:
        return lambda block: block

    up, down = _ratio(native_rate)
    try:
        from scipy.signal import resample_poly

        def resample(block):
            return resample_poly(block, up, down).astype("float32")[:CHUNK]
        return resample
    except ImportError:
        import numpy as np

        # Linear interpolation. Audible artefacts, inaudible to Whisper.
        def resample(block):
            source = np.linspace(0, len(block) - 1, CHUNK, dtype="float32")
            return np.interp(source, np.arange(len(block)), block).astype("float32")
        return resample


# ─── The stream the bridge actually uses ─────────────────────────────────────

class Microphone:
    """
    An input stream that is known to work, at the rate the device wants, handing
    out 1280-sample blocks at 16 kHz.

    `on_block` is called from PortAudio's thread with a float32 numpy array.
    """

    def __init__(self, on_block, device: Optional[Dict[str, Any]] = None,
                 verbose: bool = True):
        self.on_block = on_block
        self.verbose = verbose
        self.device = device or self._resolve()
        self.rate = self.device["rate"]
        self.blocksize = native_blocksize(self.rate)
        self._resample = make_resampler(self.rate)
        self.stream = None
        self.blocks_seen = 0
        self.silent_blocks = 0

    def _resolve(self) -> Dict[str, Any]:
        remembered = load_choice()
        if remembered:
            if self.verbose:
                print(f"  [Mic] using remembered device: {remembered['name']}")
            return remembered

        # Ask for access from this process before probing in subprocesses, so
        # the dialog appears against the app rather than against something that
        # is about to be killed.
        if self.verbose:
            print("  [Mic] requesting microphone access...", flush=True)
        access = request_access()
        if self.verbose:
            if access.get("pending"):
                print("  [Mic] still waiting on macOS — is a permission dialog "
                      "on screen? Click Allow.")
            elif access.get("error"):
                print(f"  [Mic] access attempt failed: {access['error']}")
            else:
                print(f"  [Mic] access granted (peak {access.get('peak', 0):.4f})")

        if self.verbose:
            print("  [Mic] probing devices...")
        results = probe_all(verbose=self.verbose)
        chosen = pick(results)
        if chosen is None:
            raise MicError(_no_working_device_message(results))
        save_choice(chosen)
        if self.verbose:
            print(f"  [Mic] chose {chosen['name']} @ {chosen['rate']} Hz")
        return chosen

    def _callback(self, indata, frames, time_info, status):
        import numpy as np
        block = np.asarray(indata[:, 0], dtype="float32")
        self.blocks_seen += 1
        if float(np.max(np.abs(block))) <= SILENCE_PEAK:
            self.silent_blocks += 1
        else:
            self.silent_blocks = 0
        self.on_block(self._resample(block))

    def start(self) -> None:
        # Idempotent on purpose. Starting twice used to create a second stream,
        # orphan the first, and let the garbage collector close it while a
        # PortAudio callback was still running — which aborts the process with
        # no traceback at all.
        if self.stream is not None:
            return
        import sounddevice as sd
        self.stream = sd.InputStream(
            device=self.device["index"], samplerate=self.rate, channels=1,
            dtype="float32", blocksize=self.blocksize, callback=self._callback,
        )
        self.stream.start()
        if self.verbose:
            print(f"  [Mic] listening on {self.device['name']} "
                  f"({self.rate} Hz -> {TARGET_RATE} Hz, blocks of {self.blocksize})")

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def went_deaf(self, after_blocks: int = 400) -> bool:
        """
        True once the stream has produced nothing but digital silence for a
        sustained stretch — the signature of a permission revoked mid-run, or a
        device that unplugged itself.
        """
        return self.silent_blocks >= after_blocks

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()


def _no_working_device_message(results: List[Dict[str, Any]]) -> str:
    timed_out = [r for r in results if r.get("timeout")]
    silent = [r for r in results if r.get("ok") and r.get("silent")]

    lines = ["No microphone produced any sound.", ""]
    if timed_out:
        lines += [
            "Every device timed out, which means macOS is blocking access and "
            "waiting on a permission decision that never appeared.", ""]
    elif silent:
        lines += [
            "Devices opened successfully but returned pure digital silence. "
            "That is what macOS hands a process it has denied microphone "
            "access — it does not report an error.", ""]

    lines += [
        "Diablo.app already exists and declares why it needs the microphone, so "
        "the remaining step is one macOS will only take from you directly:",
        "",
        "    1. Open Finder > Applications and DOUBLE-CLICK Diablo.",
        "       Not `open -a` from a terminal, and not from an AI agent's shell.",
        "       macOS only shows the permission dialog for an app you launched",
        "       yourself, and it attributes the request to whatever started it.",
        "",
        "    2. Click Allow when it asks for the microphone.",
        "",
        "    3. Check it stuck: System Settings > Privacy & Security >",
        "       Microphone should now list Diablo, switched on.",
        "",
        "If no dialog appears at all, CoreAudio is likely wedged. In Terminal:",
        "",
        "    sudo killall coreaudiod",
        "",
        "then double-click Diablo again. That restarts the audio daemon; it is "
        "safe and audio comes back within a second or two.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(f"Python  {sys.version.split()[0]}")
    try:
        import sounddevice as sd
        print(f"sounddevice {sd.__version__}\n")
    except Exception as e:
        print(f"sounddevice unavailable: {e}")
        raise SystemExit(1)

    if "--forget" in sys.argv:
        forget()
        print("Forgot the remembered device.")

    # In-process first. This is the only request that can raise a permission
    # dialog, because it is the only one that stays alive long enough to be
    # answered. What it returns also tells you which state you are in:
    #
    #   hangs           macOS has not decided — a dialog should be on screen
    #   opens, zeros    a decision is on file, and it was "deny"
    #   opens, signal   working
    print("Requesting microphone access from this process...")
    access = request_access(timeout=90)
    if access.get("pending"):
        print("  → still blocked after 90s. A permission dialog is probably on")
        print("    screen waiting for you, or macOS will not decide.\n")
    elif access.get("error"):
        print(f"  → failed: {access['error']}\n")
    elif access.get("peak", 0) > SILENCE_PEAK:
        print(f"  → GRANTED and receiving audio (peak {access['peak']:.4f})\n")
    else:
        # Do not over-read this. Opening instantly and getting zeros is
        # ambiguous — it looks the same whether TCC denied the request, or TCC
        # allowed it and the device itself is producing silence. Treating the
        # two as one thing sent this investigation down a long wrong path.
        print("  → the device opened instantly and returned pure silence.")
        print("    The lack of a hang rules out 'macOS has not decided yet',")
        print("    but it cannot tell allowed-and-silent from denied. Both look")
        print("    exactly like this.")
        print(f"    Asking as: {sys.executable}\n")
        holders = who_holds_the_input()
        if holders:
            print(f"    Something else has the input device open: {', '.join(holders)}")
            print("    A process wedged on the microphone starves every other")
            print("    client. Quit it (or kill it) and try again.\n")
        print("    Settle it in five seconds without any code:")
        print("    System Settings > Sound > Input, and speak. If the level")
        print("    meter moves, the hardware is fine and this is permissions.")
        print("    If it stays flat, no permission change will ever help.\n")

    results = probe_all()
    chosen = pick(results)
    print()
    if chosen:
        save_choice(chosen)
        print(f"✓ Using [{chosen['index']}] {chosen['name']} @ {chosen['rate']} Hz")
        print(f"  peak {chosen['peak']:.4f}, rms {chosen['rms']:.4f}")
        print(f"  saved to {STATE_PATH}")
    else:
        print(_no_working_device_message(results))
        raise SystemExit(1)
