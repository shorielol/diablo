"""
D.I.A.B.L.O. Voice (Voicebox Edition)
=====================================
How Diablo sounds.

This module routes TTS requests to a local Voicebox server instead of ElevenLabs.
It streams the generated WAV audio directly and caches it to disk.

  1. **Cache everything.** Every line synthesised is written to disk.
  2. **Pre-warm the common lines.** Synthesised once at install and played from disk forever.
  3. **Degrade, never fail.** Cache, then Voicebox API, then macOS `say`.
"""

import hashlib
import os
import queue
import subprocess
import sys
import threading
from typing import List, Optional

from skills import config
import urllib.request
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
CACHE_DIR = os.path.join(BASE_DIR, "voice_cache")

VOICE_ID = os.getenv("DIABLO_VOICE_PROFILE", os.getenv("DIABLO_VOICE_ID", "Diablo"))
VOICEBOX_URL = os.getenv("DIABLO_VOICEBOX_URL", "http://127.0.0.1:17493")

_warned = False
_warned_ffmpeg = False
_voice_fingerprint = None
_voicebox_profile_id = None
_profile_lock = threading.Lock()


def offline() -> bool:
    return os.getenv("DIABLO_OFFLINE", "0") \
        not in ("0", "", "false", "False")


def available() -> bool:
    """
    Can we synthesise a line we have never said before?

    This used to return True whenever offline mode was off, without ever
    contacting Voicebox — so it reported a healthy voice while the app was shut,
    and `diablo doctor` agreed with it. Ask the server.
    """
    if offline():
        return False
    try:
        req = urllib.request.Request(f"{VOICEBOX_URL}/profiles",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def _get_profile_id() -> Optional[str]:
    """Resolve the profile name to a Voicebox UUID."""
    global _voicebox_profile_id, _warned
    with _profile_lock:
        if _voicebox_profile_id is not None:
            return _voicebox_profile_id

        # If it looks like a UUID already, assume it is one
        if len(VOICE_ID) == 36 and "-" in VOICE_ID:
            _voicebox_profile_id = VOICE_ID
            return _voicebox_profile_id

        # Otherwise, fetch all profiles and match by name (case-insensitive)
        try:
            req = urllib.request.Request(f"{VOICEBOX_URL}/profiles", headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                profiles = json.loads(response.read())
                for p in profiles:
                    if p.get("name", "").lower() == VOICE_ID.lower():
                        _voicebox_profile_id = p["id"]
                        return _voicebox_profile_id
                # Reaching here means Voicebox answered but has no profile by
                # that name. Falling back silently is how this went unnoticed.
                if not _warned:
                    names = ", ".join(repr(p.get("name")) for p in profiles) or "none"
                    print(f"  [Voice] No Voicebox profile named {VOICE_ID!r}. "
                          f"Available: {names}. Using the macOS voice instead.")
                    _warned = True
                return None
        except Exception as e:
            if not _warned:
                print(f"  [Voice] Failed to resolve profile '{VOICE_ID}': {e}")
                _warned = True

        return None


# ─── Cache ───────────────────────────────────────────────────────────────────

def voice_fingerprint() -> str:
    """
    Identify the actual voice, not just the profile name.

    The profile is a container: "Diablo" can point at af_alloy today and
    bm_daniel tomorrow. Keying the cache on the name alone meant changing the
    voice changed nothing you had already heard — the old clips kept playing,
    in the old voice, for as long as the cache lived.
    """
    global _voice_fingerprint
    if _voice_fingerprint is not None:
        return _voice_fingerprint
    _voice_fingerprint = VOICE_ID
    try:
        req = urllib.request.Request(f"{VOICEBOX_URL}/profiles",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as response:
            for p in json.loads(response.read()):
                if p.get("name", "").lower() == VOICE_ID.lower():
                    parts = [VOICE_ID, str(p.get("preset_engine") or ""),
                             str(p.get("preset_voice_id") or ""),
                             str(p.get("default_engine") or "")]
                    _voice_fingerprint = "|".join(parts)
                    break
    except Exception:
        pass
    return _voice_fingerprint


def _cache_key(text: str) -> str:
    """Hash the text together with the voice actually being used."""
    fingerprint = f"{voice_fingerprint()}|{text.strip()}"
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:32]


def cached_path(text: str) -> Optional[str]:
    path = os.path.join(CACHE_DIR, _cache_key(text) + ".wav")
    return path if os.path.exists(path) and os.path.getsize(path) > 512 else None


def cache_size() -> int:
    try:
        return len([f for f in os.listdir(CACHE_DIR) if f.endswith(".wav")])
    except OSError:
        return 0


# ─── Synthesis ───────────────────────────────────────────────────────────────

def synthesize(text: str, force: bool = False) -> Optional[str]:
    """
    Produce a wav of `text` in the cache and return its path, or None.
    Hits the local Voicebox server.
    """
    text = (text or "").strip()
    if not text:
        return None

    if not force:
        hit = cached_path(text)
        if hit:
            return hit

    if not available():
        return None

    profile_id = _get_profile_id()
    if not profile_id:
        return None

    try:
        payload = json.dumps({
            "profile_id": profile_id,
            "text": text,
            "engine": "kokoro"
        }).encode("utf-8")
        
        req = urllib.request.Request(
            f"{VOICEBOX_URL}/generate/stream",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.status != 200:
                raise Exception(f"HTTP {response.status}")
            data = response.read()
    except Exception as e:
        global _warned
        if not _warned:
            print(f"  [Voice] Voicebox unavailable ({type(e).__name__}: {e}). "
                  f"Is it running? Falling back to local voice.")
            _warned = True
        return None

    if not data:
        return None

    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, _cache_key(text) + ".wav")
    tmp = path + ".partial"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


# ─── Pace ────────────────────────────────────────────────────────────────────
#
# Measured: the Kokoro voice delivers 205 words per minute. Ordinary
# conversational English is about 150, an audiobook about 150, and someone
# explaining a method to you slows to 110-130. At 205 an explanation is
# technically audible and useless — you cannot follow a step while the next one
# is already arriving.
#
# Voicebox has no speed parameter, so the rate is changed after synthesis with
# ffmpeg's atempo, which stretches time without moving pitch. It costs about
# 55ms per phrase and the result is cached, so a line said twice is slowed
# once.

EXPLAIN_WPM = config.env_int("DIABLO_EXPLAIN_WPM", 120, low=80, high=200)
NORMAL_WPM = config.env_int("DIABLO_NORMAL_WPM", 165, low=100, high=260)


def _duration(path: str) -> Optional[float]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None


def repace(path: str, text: str, target_wpm: int) -> str:
    """
    The same audio at a given words-per-minute, or the original unchanged.

    The rate is measured from this clip rather than assumed, so it holds if the
    voice, the engine or the profile changes. Anything already within ten per
    cent of the target is left alone — a 1.05 stretch is inaudible and not
    worth a re-encode.
    """
    words = len((text or "").split())
    if words < 3 or not target_wpm:
        return path
    seconds = _duration(path)
    if not seconds or seconds <= 0:
        return path
    current = words / seconds * 60.0
    factor = target_wpm / current
    if 0.9 < factor < 1.1:
        return path
    # atempo handles 0.5 to 2.0 in one pass; chain it for anything beyond.
    chain, remaining = [], factor
    while remaining < 0.5:
        chain.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        chain.append(2.0)
        remaining /= 2.0
    chain.append(remaining)
    filters = ",".join(f"atempo={f:.4f}" for f in chain)

    out = path.replace(".wav", f".{target_wpm}wpm.wav")
    if os.path.exists(out):
        return out
    if not config.require("ffmpeg"):
        # Said once. This used to fail silently and return the original audio,
        # which meant the slower explaining pace simply never happened inside
        # the app bundle and nothing anywhere said so.
        global _warned_ffmpeg
        if not _warned_ffmpeg:
            _warned_ffmpeg = True
            print("  [Voice] ffmpeg not found, so speech cannot be re-paced. "
                  "Install it with: brew install ffmpeg")
        return path
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", path, "-filter:a", filters, out],
            capture_output=True, timeout=30)
        if result.returncode != 0 or not os.path.exists(out):
            return path
        return out
    except Exception:
        # ffmpeg missing or failing is not a reason to go silent.
        return path


# ─── Playback ────────────────────────────────────────────────────────────────

# Called the instant audio actually starts, with no arguments.
#
# The orb used to begin its speaking animation the moment speak() was entered,
# which is before synthesis — a network round trip to Voicebox, or a cache
# lookup, or a fallback to the system voice. On a cold line that is upwards of
# a second of a mouth moving in silence, and it reads as a glitch rather than
# as speech. The state belongs to the sound, so the sound announces it.
on_audio_start = None
on_audio_end = None


def _play(path: str) -> bool:
    try:
        if on_audio_start:
            try:
                on_audio_start()
            except Exception:
                pass
        subprocess.run(["afplay", path], timeout=120)
        return True
    except Exception as e:
        print(f"  [Voice] playback failed: {e}")
        return False
    finally:
        if on_audio_end:
            try:
                on_audio_end()
            except Exception:
                pass


def _say_locally(text: str, pace: Optional[int] = None) -> None:
    """macOS speech synthesis. Never fails, never leaves the machine."""
    command = ["say", "-v", os.getenv("DIABLO_FALLBACK_VOICE", "Daniel")]
    if pace:
        # `say` takes words per minute directly, so the fallback voice gets the
        # same pacing without going near ffmpeg.
        command += ["-r", str(int(pace))]
    command.append(text)
    if on_audio_start:
        try:
            on_audio_start()
        except Exception:
            pass
    try:
        subprocess.run(command, timeout=120)
    finally:
        if on_audio_end:
            try:
                on_audio_end()
            except Exception:
                pass


def speak(text: str, pace: Optional[int] = None) -> str:
    """
    Say it. `pace` is a words-per-minute target; None leaves the voice alone.

    Explanations pass EXPLAIN_WPM. Everything else is unchanged, because a
    one-line acknowledgement does not need slowing down and would sound
    laboured if it were.
    """
    text = (text or "").strip()
    if not text:
        return "empty"

    hit = cached_path(text)
    if hit:
        if pace:
            hit = repace(hit, text, pace)
        if _play(hit):
            return "cache"

    path = synthesize(text)
    if path and pace:
        path = repace(path, text, pace)
    if path and _play(path):
        return "voicebox"

    _say_locally(text, pace)
    return "local"



# ─── Streaming speech ────────────────────────────────────────────────────────

# Set while Diablo is speaking, cleared when it stops. The wake listener sets
# STOP when it hears speech during playback, which is how barge-in works.
SPEAKING = threading.Event()
STOP = threading.Event()


def interrupt() -> None:
    """Stop talking, now. Safe to call from any thread."""
    STOP.set()


def speak_stream(pieces, should_stop=None, pace: Optional[int] = None) -> str:
    """
    Speak sentence by sentence as they arrive, instead of waiting for the lot.

    `pieces` is an iterable of text — sentences from the model, or one string.
    Each is synthesised and played in order, and the next is prepared while the
    current one is still playing, so the gap between sentences is silence only
    for as long as the cache miss takes.

    The reason this exists: a six-sentence answer takes about six seconds to
    generate and the old path said nothing for all six. Speaking from the first
    sentence turns that into roughly two.

    Returns the route the last segment took, or "interrupted".
    """
    STOP.clear()
    SPEAKING.set()
    route = "empty"

    # Synthesis runs one segment ahead of playback. Without this, each sentence
    # waits for the previous one to finish playing before it starts rendering,
    # and a four-sentence answer took 22.6s to say when a single synthesis of
    # the whole thing would have taken about 6. The first word still arrives
    # early, which is the point, but the gaps in between were worse than the
    # wait it replaced.
    ahead: "queue.Queue" = queue.Queue(maxsize=2)
    failed: List[BaseException] = []

    def render() -> None:
        try:
            for piece in pieces:
                if STOP.is_set():
                    break
                piece = (piece or "").strip()
                if not piece:
                    continue
                path = None
                try:
                    path = cached_path(piece) or synthesize(piece)
                    if path and pace:
                        path = repace(path, piece, pace)
                except BaseException as e:
                    failed.append(e)
                ahead.put((piece, path))
        except BaseException as e:
            failed.append(e)
        finally:
            ahead.put(None)

    worker = threading.Thread(target=render, daemon=True)
    worker.start()

    try:
        while True:
            if STOP.is_set() or (should_stop is not None and should_stop()):
                return "interrupted"
            try:
                item = ahead.get(timeout=30)
            except Exception:
                return route
            if item is None:
                return route
            piece, path = item
            if STOP.is_set():
                return "interrupted"
            if path and _play(path):
                route = "voicebox"
            else:
                # Whatever went wrong with this segment, say it some other way
                # rather than dropping a sentence out of the middle of an answer.
                route = speak(piece, pace=pace)
    finally:
        STOP.set()          # unblock the renderer if it is mid-queue
        SPEAKING.clear()
        worker.join(timeout=2)
        STOP.clear()


def say_long(text: str, should_stop=None, pace: Optional[int] = None) -> str:
    """
    Speak a whole answer, split at sentence boundaries first.

    The convenience wrapper for the common case: one finished string that is
    long enough to be worth starting early.
    """
    try:
        from skills import speech
        pieces = speech.segments(text)
    except Exception:
        pieces = [text]
    if not pieces:
        return speak(text, pace=pace)
    if len(pieces) == 1:
        return speak(pieces[0], pace=pace)

    # First sentence alone, then everything else as one block.
    #
    # Measured on this machine for a four-sentence answer:
    #   whole answer in one synthesis   2.79s before the first word, 12.1s total
    #   every sentence separately       0.95s before the first word, 17.1s total
    #   first sentence, then the rest   0.95s before the first word, ~13s total
    #
    # Splitting every sentence pays a synthesis round trip per sentence and the
    # look-ahead only hides part of it. Splitting once buys nearly all of the
    # head start and almost none of the tax.
    return speak_stream([pieces[0], " ".join(pieces[1:])],
                        should_stop=should_stop, pace=pace)

# ─── Pre-warm ────────────────────────────────────────────────────────────────

COMMON_LINES = [
    "At your service, Sir.",
    "Yes, Sir?",
    "I'm listening.",
    "Go ahead.",
    "On it, Sir.",
    "Right away.",
    "Consider it done.",
    "Done, Sir.",
    "All finished.",
    "Still working on it, Sir.",
    "One moment.",
    "I missed that, Sir.",
    "Say that again?",
    "Forgotten, Sir.",
    "Proceeding.",
    "Cancelled.",
    "No matter, Sir. I will use my best judgement.",
    "Screenshot saved to your Desktop, Sir.",
    "My local brain is offline, Sir. Ollama does not appear to be running.",
    "Something went wrong handling that, Sir.",
    "That one came apart on me, Sir. The details are in the log.",
    "Nothing on my plate at the moment, Sir.",
    "Powering down, Sir. It's been a pleasure.",
    "Good morning, Sir.",
    "Good evening, Sir.",
]


def prewarm(lines=None, verbose: bool = True) -> int:
    lines = lines or COMMON_LINES
    if not available():
        if verbose:
            print("  Cannot pre-warm: offline mode.")
        return 0

    made = 0
    for line in lines:
        if cached_path(line):
            continue
        if synthesize(line):
            made += 1
            if verbose:
                print(f"    ✓ {line[:58]}")
        elif verbose:
            print(f"    ✗ {line[:58]}")
    return made


if __name__ == "__main__":
    print(f"Voice    {VOICE_ID}")
    print(f"Voicebox {VOICEBOX_URL}")
    print(f"Cache    {CACHE_DIR}  ({cache_size()} lines)")
    print(f"Offline  {offline()}")
    print()

    if len(sys.argv) > 1 and sys.argv[1] == "prewarm":
        print(f"Pre-warming {len(COMMON_LINES)} lines...")
        made = prewarm()
        print(f"\n{made} new, {cache_size()} cached in total.")
    else:
        line = " ".join(sys.argv[1:]) or \
            "Good evening, Sir. I'm Diablo. Everything's running locally, so take your time."
        print(f"Speaking: {line!r}")
        print(f"Route: {speak(line)}")
