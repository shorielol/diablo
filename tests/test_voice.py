#!/usr/bin/env python3
"""
The voice path, and what happens when each piece of it disappears.

Diablo is only useful if it talks, and the talking runs through four things that
can each be absent: the Voicebox app, the named profile inside it, the on-disk
cache, and macOS `say` as the floor. A previous bug had VOICE_ID set to a
profile name that did not exist, which resolved to None and fell through to the
system voice — silently. It sounded wrong and nothing anywhere said why.

So these tests care less about "does audio come out" and more about "when it
cannot, does it say so". Synthesis is exercised with the speaker muted.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard, Timer  # noqa: E402

from skills import voice  # noqa: E402

r = Results()


def run():
    r.head("Voicebox")
    with guard(r, "reachable"):
        up = voice.available()
        r.check("Voicebox is running", up, voice.VOICEBOX_URL)

    with guard(r, "the configured profile exists"):
        pid = voice._get_profile_id()
        r.check(f"profile {voice.VOICE_ID!r} resolves to an id", pid is not None,
                str(pid))
        if pid is None:
            r.bad("profile mismatch", "this is the bug that silently fell back to "
                                      "the macOS voice")

    with guard(r, "a wrong profile name is reported, not swallowed"):
        was, cached = voice.VOICE_ID, voice._voicebox_profile_id
        voice.VOICE_ID = "NoSuchProfileZZZ"
        voice._voicebox_profile_id = None   # the resolved id is cached per process
        try:
            pid = voice._get_profile_id()
            r.check("an unknown profile resolves to None", pid is None, str(pid))
        finally:
            voice.VOICE_ID, voice._voicebox_profile_id = was, cached
        r.check("and the real profile still resolves afterwards",
                voice._get_profile_id() is not None)

    with guard(r, "the cache is keyed on the voice, not the profile name"):
        fp = voice.voice_fingerprint()
        r.check("the fingerprint names the actual preset", "|" in fp and len(fp) > len(voice.VOICE_ID),
                fp)
        key_now = voice._cache_key("hello")
        saved = voice._voice_fingerprint
        voice._voice_fingerprint = "Diablo|kokoro|bm_daniel|kokoro"
        try:
            r.check("changing the preset changes the cache key",
                    voice._cache_key("hello") != key_now,
                    "old clips are bypassed after a voice change")
        finally:
            voice._voice_fingerprint = saved

    # ── synthesis ───────────────────────────────────────────────────────────
    r.head("Synthesis and cache")
    phrase = f"Diablo self test {int(time.time())}"
    with guard(r, "synthesize produces a file"):
        with Timer() as t:
            path = voice.synthesize(phrase)
        if r.check("audio was produced", path is not None and os.path.exists(path),
                   f"{t.elapsed:.1f}s — {os.path.basename(path or 'none')}"):
            r.check("and it is not empty", os.path.getsize(path) > 500,
                    f"{os.path.getsize(path)} bytes")

    with guard(r, "the second time it is cached"):
        with Timer() as t:
            again = voice.synthesize(phrase)
        r.check("a repeat phrase comes from disk", t.elapsed < 0.25,
                f"{t.elapsed * 1000:.0f}ms")
        r.check("and it is the same file", again == path)

    with guard(r, "cached_path finds it"):
        r.check("cached_path reports the hit", voice.cached_path(phrase) is not None)
        r.check("and misses an unseen phrase",
                voice.cached_path(f"never said {time.time()}") is None)

    with guard(r, "the cache has real content"):
        r.check("prewarmed lines are on disk", voice.cache_size() > 0,
                f"{voice.cache_size()} clips")

    with guard(r, "empty text does not crash"):
        for bad in ("", "   ", "\n"):
            out = voice.synthesize(bad)
            r.check(f"synthesize({bad!r}) is handled", out is None or isinstance(out, str))

    with guard(r, "very long text is handled"):
        out = voice.synthesize("word " * 400)
        r.check("a long line returns something or None, never raises",
                out is None or isinstance(out, str))

    # ── degradation ─────────────────────────────────────────────────────────
    r.head("When Voicebox is gone")
    was_url = voice.VOICEBOX_URL
    voice.VOICEBOX_URL = "http://127.0.0.1:9"
    try:
        with guard(r, "available() goes false"):
            r.check("it notices Voicebox is unreachable", not voice.available())

        with guard(r, "speak still returns"):
            with Timer() as t:
                out = voice.speak("Testing the fallback path.")
            r.check("speak does not raise with no Voicebox", isinstance(out, str),
                    f"{t.elapsed:.1f}s — {out[:40]}")
            r.check("and does not hang", t.elapsed < 20, f"{t.elapsed:.1f}s")
    finally:
        voice.VOICEBOX_URL = was_url

    with guard(r, "it recovers when Voicebox comes back"):
        r.check("available() is true again", voice.available(), voice.VOICEBOX_URL)

    # ── the system floor ────────────────────────────────────────────────────
    r.head("The macOS floor")
    with guard(r, "`say` exists"):
        which = subprocess.run(["which", "say"], capture_output=True, text=True)
        r.check("macOS say is available as a last resort",
                which.returncode == 0, which.stdout.strip())

    return r.report("voice")


if __name__ == "__main__":
    sys.exit(run())
