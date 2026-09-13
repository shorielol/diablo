#!/usr/bin/env python3
"""
The listening path, hammered.

Two crashes came out of this layer and both were silent. The first was a double
start: Microphone.start() followed by `with microphone:` opened a second stream,
orphaned the first, and the garbage collector closed it from inside a running
callback — the process aborted with no traceback at all. The second was a wake
listener that latched into capture mode and never came out.

So the tests below are about termination and idempotency rather than accuracy.
Everything is driven with synthetic audio, so no microphone is needed and the
results do not depend on how quiet the room is.

The long run at the end feeds twenty minutes of audio through the listener in a
few seconds. It is there to catch state that only goes wrong after a while:
buffers that grow, clocks that drift, a mute that never lifts.
"""

import gc
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard, Timer  # noqa: E402

from skills import mic, wake_word  # noqa: E402

r = Results()
SR = 16000
BLOCK = 1280


def silence(n):
    return [np.zeros(BLOCK, np.float32) for _ in range(n)]


def speechlike(n, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        t = np.arange(BLOCK) / SR
        sig = (0.3 * np.sin(2 * np.pi * 150 * t + i)
               + 0.15 * np.sin(2 * np.pi * 430 * t)
               + 0.04 * rng.standard_normal(BLOCK))
        out.append(sig.astype(np.float32))
    return out


def run():
    # ── the endpointer ──────────────────────────────────────────────────────
    r.head("Endpointer")
    e = mic.Endpointer()
    r.check("the VAD loaded", e.vad is not None,
            f"threshold {mic.VAD_THRESHOLD}")

    def feed_until_stop(blocks, ep):
        ep.reset()
        for i, b in enumerate(blocks):
            if ep.feed(b):
                return i + 1
        return len(blocks)

    with guard(r, "stops on silence"):
        n = feed_until_stop(silence(400), e)
        secs = n * BLOCK / SR
        r.check("pure silence ends quickly", secs < 3.0, f"{secs:.2f}s")

    with guard(r, "captures a short utterance and stops"):
        n = feed_until_stop(speechlike(19) + silence(300), e)
        secs = n * BLOCK / SR
        r.check("a 1.5s utterance ends in 2-3s", 1.5 < secs < 3.5, f"{secs:.2f}s")

    with guard(r, "a long utterance is capped"):
        n = feed_until_stop(speechlike(400), e)
        secs = n * BLOCK / SR
        # The ceiling is stored as a block count, not seconds — max_seconds is
        # only a constructor argument.
        cap = e.max_blocks * BLOCK / SR
        r.check("never records past the ceiling", secs <= cap + 1.0,
                f"{secs:.2f}s, cap {cap:.1f}s")

    with guard(r, "it always terminates"):
        worst = 0
        for trial in range(40):
            blocks = speechlike(trial % 25, seed=trial) + silence(trial % 17) + \
                speechlike(trial % 9, seed=trial + 99) + silence(300)
            n = feed_until_stop(blocks, e)
            worst = max(worst, n * BLOCK / SR)
        r.check("40 mixed patterns all ended", True, f"worst case {worst:.2f}s")

    with guard(r, "reset really resets"):
        e.reset()
        r.check("counters are zero", e.blocks == 0 and e.speech_blocks == 0,
                f"blocks={e.blocks} speech={e.speech_blocks}")

    with guard(r, "odd input does not raise"):
        for bad in (np.zeros(1, np.float32), np.zeros(BLOCK * 3, np.float32),
                    np.zeros(BLOCK, np.float64), np.full(BLOCK, np.nan, np.float32)):
            e.reset()
            e.feed(bad.astype(np.float32))
        r.ok("short, long, wrong dtype and NaN all survive")

    # ── the wake listener ───────────────────────────────────────────────────
    r.head("Wake listener")
    listener = wake_word.WakeListener()

    with guard(r, "idle on silence"):
        fired = sum(1 for b in silence(300) if listener.feed(b) is not None)
        r.check("silence never wakes it", fired == 0, f"{fired} false wakes")

    with guard(r, "idle on noise"):
        listener.reset()
        fired = sum(1 for b in speechlike(300, seed=7) if listener.feed(b) is not None)
        r.check("synthetic noise never wakes it", fired == 0, f"{fired} false wakes")

    with guard(r, "the capture state always ends"):
        listener.capturing = True
        listener.capture = [np.zeros(BLOCK, np.float32)]
        listener.capture_started = listener.clock
        listener.silence_started = None
        ended = False
        for i, b in enumerate(silence(400)):
            if listener.feed(b) is not None:
                ended = True
                break
        r.check("a latched capture always closes", ended,
                f"after {(i + 1) * BLOCK / SR:.2f}s" if ended else "NEVER CLOSED")

    with guard(r, "mute lifts by itself"):
        listener.reset()
        listener.capturing = False
        listener.mute_for(2.0)
        muted_at = listener.clock
        for b in silence(200):
            listener.feed(b)
            if listener.clock >= listener.muted_until:
                break
        r.check("the mute expires as audio flows",
                listener.clock >= listener.muted_until,
                f"lifted after {listener.clock - muted_at:.2f}s of audio")

    with guard(r, "mute_for never shortens an existing mute"):
        listener.mute_for(10.0)
        far = listener.muted_until
        listener.mute_for(0.1)
        r.check("a short mute cannot cancel a long one", listener.muted_until == far)

    with guard(r, "repeated wakes do not accumulate state"):
        listener.reset()
        listener.muted_until = 0.0
        before = len(listener.buffer)
        for cycle in range(30):
            listener.capturing = True
            listener.capture = [np.zeros(BLOCK, np.float32)]
            listener.capture_started = listener.clock
            listener.silence_started = None
            for b in silence(60):
                if listener.feed(b) is not None:
                    break
        r.check("the rolling buffer stays bounded", len(listener.buffer) <= SR * 5,
                f"{len(listener.buffer)} samples ({len(listener.buffer) / SR:.1f}s)")
        r.check("capture list is emptied between wakes", len(listener.capture) <= 2,
                f"{len(listener.capture)} chunks held")

    # ── the long run ────────────────────────────────────────────────────────
    r.head("Twenty minutes of audio")
    listener.reset()
    listener.muted_until = 0.0
    blocks_needed = int(20 * 60 * SR / BLOCK)
    clock_before = listener.clock
    rng = np.random.default_rng(1)
    with guard(r, "sustained feeding"):
        with Timer() as t:
            wakes = 0
            for i in range(blocks_needed):
                if i % 900 < 30:
                    chunk = speechlike(1, seed=i)[0]
                else:
                    chunk = (0.001 * rng.standard_normal(BLOCK)).astype(np.float32)
                if listener.feed(chunk) is not None:
                    wakes += 1
        r.check("it kept up", t.elapsed < 240, f"{t.elapsed:.1f}s for 20 min of audio")
        r.check("no false wakes across 20 minutes", wakes == 0, f"{wakes} wakes")
        # The clock is a monotonic audio clock for the life of the listener —
        # reset() deliberately leaves it alone, because muted_until is measured
        # against it. So check the delta across this section, not the absolute.
        advanced = listener.clock - clock_before
        r.check("the clock advanced by exactly the audio fed",
                abs(advanced - blocks_needed * BLOCK / SR) < 2,
                f"advanced {advanced:.0f}s for {blocks_needed * BLOCK / SR:.0f}s of audio")
        r.check("the buffer never grew unbounded", len(listener.buffer) <= SR * 5,
                f"{len(listener.buffer)} samples")

    # ── the device layer ────────────────────────────────────────────────────
    #
    # Opening a real input stream can block forever rather than failing:
    # PortAudio waits inside C for a TCC decision that never arrives, and the
    # wait is not interruptible from Python. A first run of this file sat at 0%
    # CPU for twenty-one minutes inside start(). So every real-device call below
    # is wrapped in a SIGALRM, and a timeout is reported as a skip rather than
    # hanging the suite — the same defence mic.py uses for its probe subprocess.
    r.head("Microphone device")

    import signal

    class Blocked(Exception):
        pass

    def with_deadline(seconds, fn, *args):
        def fire(signum, frame):
            raise Blocked()
        old_handler = signal.signal(signal.SIGALRM, fire)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return fn(*args)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)

    with guard(r, "a remembered device is on file"):
        state = os.path.join(mic.HOME, "mic_device.json")
        r.check("mic_device.json exists", os.path.exists(state), state)

    if os.getenv("DIABLO_TEST_DEVICE") != "1":
        r.skip("opening the real microphone",
               "needs a granted process; set DIABLO_TEST_DEVICE=1 in a terminal "
               "that already has microphone access")
        return r.report("microphone and wake word")

    opened = True
    with guard(r, "start() is idempotent"):
        m = mic.Microphone(on_block=lambda b: None)
        try:
            try:
                with_deadline(20, m.start)
            except Blocked:
                opened = False
                r.skip("start() is idempotent",
                       "the stream did not open within 20s — this process has no "
                       "microphone grant")
            if opened:
                first = m.stream
                m.start()
                r.check("a second start does not open a second stream",
                        m.stream is first, "this crashed the process before")
                m.start()
                r.check("nor does a third", m.stream is first)
        finally:
            try:
                with_deadline(10, m.stop)
            except Blocked:
                pass
            gc.collect()
        if opened:
            r.ok("stopped cleanly with no abort")

    if not opened:
        return r.report("microphone and wake word")

    with guard(r, "stop() is idempotent"):
        m = mic.Microphone(on_block=lambda b: None)
        with_deadline(20, m.start)
        m.stop(); m.stop(); m.stop()
        r.ok("stopping repeatedly is safe")

    with guard(r, "it can be restarted"):
        m = mic.Microphone(on_block=lambda b: None)
        with_deadline(20, m.start); m.stop(); with_deadline(20, m.start)
        alive = m.stream is not None
        m.stop()
        r.check("start after stop works", alive)

    with guard(r, "real audio arrives"):
        got = []
        m = mic.Microphone(on_block=lambda b: got.append(float(np.abs(b).max())))
        with_deadline(20, m.start)
        time.sleep(2.0)
        m.stop()
        r.check("blocks were delivered", len(got) > 10, f"{len(got)} blocks in 2s")
        peak = max(got) if got else 0.0
        if peak < 1e-6:
            r.bad("the microphone is not silent", f"peak {peak:.6f} — all zeros")
        else:
            r.ok("the microphone is not silent", f"peak {peak:.4f}")

    with guard(r, "went_deaf only fires on sustained silence"):
        m = mic.Microphone(on_block=lambda b: None)
        r.check("a fresh microphone is not deaf", not m.went_deaf())

    return r.report("microphone and wake word")


if __name__ == "__main__":
    sys.exit(run())
