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
import subprocess
import sys
import threading
from typing import Optional
import urllib.request
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
CACHE_DIR = os.path.join(BASE_DIR, "voice_cache")

VOICE_ID = os.getenv("DIABLO_VOICE_PROFILE", os.getenv("DIABLO_VOICE_ID", "Diablo"))
VOICEBOX_URL = os.getenv("DIABLO_VOICEBOX_URL", "http://127.0.0.1:17493")

_warned = False
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


# ─── Playback ────────────────────────────────────────────────────────────────

def _play(path: str) -> bool:
    try:
        subprocess.run(["afplay", path], timeout=120)
        return True
    except Exception as e:
        print(f"  [Voice] playback failed: {e}")
        return False


def _say_locally(text: str) -> None:
    """macOS speech synthesis. Never fails, never leaves the machine."""
    subprocess.run(["say", "-v", os.getenv("DIABLO_FALLBACK_VOICE", "Daniel"), text],
                   timeout=120)


def speak(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "empty"

    hit = cached_path(text)
    if hit and _play(hit):
        return "cache"

    path = synthesize(text)
    if path and _play(path):
        return "voicebox"

    _say_locally(text)
    return "local"


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
