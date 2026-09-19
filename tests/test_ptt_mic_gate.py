#!/usr/bin/env python3
"""Audio Phase 2B: the hotkey is the only owner of Diablo's microphone.

Invariant under test (PTT_ONLY, the default):

    PTT not active  ->  no block reaches any conversational consumer
    PTT active      ->  capture works exactly as before

"Conversational consumer" means the listening loop's audio queue, the command
capture queue, speech-to-text and barge-in. The microphone stream itself may
be open (it is infrastructure: the pre-roll that stops the first syllable being
clipped lives there), and the tests distinguish the two on purpose.

Nothing here opens a real microphone or plays audio: the stream, the speech
model and playback are replaced with in-memory fakes, and blocks are pushed
through the production audio_callback.
"""

import os
import queue
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

import voice_bridge  # noqa: E402
from skills import conversation, voice  # noqa: E402

LOUD = np.full(voice_bridge.CHUNK, 0.5, dtype=np.float32)


class _CountingQueue(queue.Queue):
    """A queue that remembers how many blocks were ever offered to it."""

    def __init__(self):
        super().__init__()
        self.puts = 0

    def put(self, item, *args, **kwargs):
        self.puts += 1
        return super().put(item, *args, **kwargs)


class _FakeMic:
    """Stands in for mic.Microphone: records that the stream was opened."""

    instances = []

    def __init__(self, on_block=None, **_kwargs):
        self.on_block = on_block
        self.started = False
        _FakeMic.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def went_deaf(self, *_args, **_kwargs):
        return False


class _Stt:
    def __init__(self, text="the answer"):
        self.text = text
        self.calls = []

    def transcribe(self, audio, **_kwargs):
        self.calls.append(np.asarray(audio).copy())
        return {"text": self.text}


class _WakeProbe:
    def __init__(self):
        self.feed_calls = 0

    def feed(self, _block):
        self.feed_calls += 1
        return "diablo open safari now"

    def mute_for(self, _seconds):
        pass


def _drain(work_queue):
    while True:
        try:
            work_queue.get_nowait()
        except queue.Empty:
            return


def _wait_for(predicate, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class _Sandbox:
    """Swap every collaborator the microphone path touches, then restore."""

    NAMES = ("audio_queue", "set_ui_state", "speak", "stt_model", "wake_listener")

    def __enter__(self):
        self.saved = {n: getattr(voice_bridge, n, None) for n in self.NAMES}
        self.had_wait = hasattr(voice_bridge, "PTT_ANSWER_WAIT")
        self.saved_wait = getattr(voice_bridge, "PTT_ANSWER_WAIT", None)
        self.saved_interrupt = voice.interrupt
        self.saved_session = conversation.session
        self.saved_generation = voice._current_generation
        self.saved_context = voice._generation_context.get()
        self.interrupts = []
        self.spoken = []
        self.stt = _Stt()
        self.audio_queue = _CountingQueue()

        voice_bridge.audio_queue = self.audio_queue
        voice_bridge.set_ui_state = lambda _state: None
        voice_bridge.speak = lambda text, **kw: self.spoken.append((text, kw)) or "test"
        voice_bridge.stt_model = self.stt
        voice_bridge.wake_listener = None
        voice_bridge.PTT_ANSWER_WAIT = 0.6
        voice.interrupt = lambda: self.interrupts.append(True)
        conversation.session = conversation.Session()
        voice._current_generation = 0
        voice._generation_context.set(None)

        voice_bridge.MIC_LIVE.set()
        voice_bridge._ptt_active.clear()
        voice_bridge._ptt_release.clear()
        voice_bridge.is_capturing_command.clear()
        voice_bridge.barge_in.clear()
        voice_bridge._barge.clear()
        voice.SPEAKING.clear()
        _drain(voice_bridge.command_capture_queue)
        voice_bridge._preroll.clear()
        return self

    def __exit__(self, *_exc):
        voice_bridge._ptt_release.set()
        _wait_for(lambda: not voice_bridge._ptt_active.is_set(), 2.0)
        voice_bridge._ptt_release.clear()
        voice_bridge.is_capturing_command.clear()
        voice_bridge.barge_in.clear()
        voice_bridge._barge.clear()
        voice.SPEAKING.clear()
        _drain(voice_bridge.command_capture_queue)
        for name, value in self.saved.items():
            setattr(voice_bridge, name, value)
        if self.had_wait:
            voice_bridge.PTT_ANSWER_WAIT = self.saved_wait
        else:
            del voice_bridge.PTT_ANSWER_WAIT
        voice.interrupt = self.saved_interrupt
        conversation.session = self.saved_session
        voice._current_generation = self.saved_generation
        voice._generation_context.set(self.saved_context)
        return False


def _run_in_thread(fn, *args, **kwargs):
    box = {}

    def target():
        box["value"] = fn(*args, **kwargs)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def _feed_while(thread, seconds=7.0):
    """Push loud blocks for as long as `thread` runs; report if capture opened."""
    opened = False
    deadline = time.monotonic() + seconds
    while thread.is_alive() and time.monotonic() < deadline:
        if voice_bridge.is_capturing_command.is_set():
            opened = True
        voice_bridge.audio_callback(LOUD)
        time.sleep(0.005)
    thread.join(timeout=1.0)
    return opened


def _press(on_text, blocks=6):
    """One real press-and-hold through the production PTT entry points."""
    started = voice_bridge.push_to_talk_start(on_text=on_text)
    _wait_for(voice_bridge.is_capturing_command.is_set, 2.0)
    for _ in range(blocks):
        voice_bridge.audio_callback(LOUD)
    time.sleep(voice_bridge.PTT_MIN_SECONDS + 0.05)
    voice_bridge.push_to_talk_stop()
    _wait_for(lambda: not voice_bridge._ptt_active.is_set(), 3.0)
    return started


def run():
    results = Results()
    results.head("0. product mode")
    results.check("PTT_ONLY is on by default", voice_bridge.PTT_ONLY is True)

    results.head("A. launch: infrastructure ready, no conversational capture")
    with guard(results, "launch path"), _Sandbox() as box:
        original_mic = voice_bridge.mic.Microphone
        original_available = voice_bridge.local_brain.available
        probe = _WakeProbe()
        try:
            _FakeMic.instances.clear()
            voice_bridge.mic.Microphone = _FakeMic
            voice_bridge.local_brain.available = lambda: False
            voice_bridge.wake_listener = probe
            voice_bridge.MIC_LIVE.clear()
            threading.Thread(target=voice_bridge.start_listening_loop,
                             daemon=True).start()
            results.check("audio stream initialised for future PTT",
                          _wait_for(lambda: bool(_FakeMic.instances)
                                    and _FakeMic.instances[-1].started))
            stream = _FakeMic.instances[-1]
            for _ in range(40):
                stream.on_block(LOUD)
            time.sleep(0.1)
            results.check("stream reports live, so the hotkey works at once",
                          voice_bridge.MIC_LIVE.is_set())
            results.check("idle blocks never reach the listening loop",
                          box.audio_queue.puts == 0, f"puts={box.audio_queue.puts}")
            results.check("idle blocks never reach command capture",
                          voice_bridge.command_capture_queue.empty()
                          and not voice_bridge.is_capturing_command.is_set())
            results.check("no speech-to-text at launch", not box.stt.calls)
            results.check("I. wake feed never runs under PTT_ONLY",
                          probe.feed_calls == 0, f"feeds={probe.feed_calls}")
        finally:
            voice_bridge.mic.Microphone = original_mic
            voice_bridge.local_brain.available = original_available
            voice_bridge.MIC_LIVE.set()

    results.head("B. clarify without PTT")
    with guard(results, "ask_user_voice"), _Sandbox() as box:
        thread, out = _run_in_thread(voice_bridge.ask_user_voice, "Which folder, Sir?")
        opened = _feed_while(thread)
        results.check("clarify returns an empty answer", out.get("value") == "",
                      repr(out.get("value")))
        results.check("clarify never opens command capture", not opened)
        results.check("clarify sends nothing to speech-to-text", not box.stt.calls,
                      f"stt calls={len(box.stt.calls)}")
        results.check("question is still spoken",
                      any("Which folder" in t for t, _ in box.spoken))

    results.head("C. confirm without PTT")
    with guard(results, "confirm_voice"), _Sandbox() as box:
        box.stt.text = "yes"
        thread, out = _run_in_thread(voice_bridge.confirm_voice, "delete the draft")
        opened = _feed_while(thread)
        results.check("confirm is not approved by ambient audio",
                      out.get("value") is False, repr(out.get("value")))
        results.check("confirm never opens command capture", not opened)
        results.check("confirm sends nothing to speech-to-text", not box.stt.calls,
                      f"stt calls={len(box.stt.calls)}")

    results.head("D/E/F. PTT capture, release, and no restart")
    with guard(results, "press, release, silence"), _Sandbox() as box:
        received = []
        started = _press(received.append, blocks=8)
        results.check("D. hotkey press starts capture", started)
        results.check("D. held audio reaches speech-to-text",
                      _wait_for(lambda: bool(received)) and len(box.stt.calls) == 1
                      and box.stt.calls[0].size >= 8 * voice_bridge.CHUNK,
                      f"calls={len(box.stt.calls)}")
        results.check("D. transcript dispatched once", received == ["the answer"],
                      repr(received))
        results.check("E. release leaves PTT inactive",
                      not voice_bridge.push_to_talk_active())
        results.check("E. release closes command capture",
                      not voice_bridge.is_capturing_command.is_set())
        puts_after_release = box.audio_queue.puts
        voice.SPEAKING.set()
        for _ in range(120):
            voice_bridge.audio_callback(LOUD)
        voice.SPEAKING.clear()
        time.sleep(0.1)
        results.check("F. post-release audio reaches no consumer",
                      box.audio_queue.puts == puts_after_release
                      and voice_bridge.command_capture_queue.empty(),
                      f"new puts={box.audio_queue.puts - puts_after_release}")
        results.check("F. nothing restarted capture or transcription",
                      not voice_bridge.is_capturing_command.is_set()
                      and not voice_bridge.push_to_talk_active()
                      and len(box.stt.calls) == 1)

    results.head("G. barge-in without PTT")
    with guard(results, "external audio while speaking"), _Sandbox() as box:
        voice.SPEAKING.set()
        for _ in range(voice_bridge.BARGE_IN_BLOCKS * 4):
            voice_bridge.audio_callback(LOUD)
        results.check("external audio does not interrupt Diablo", not box.interrupts,
                      f"interrupts={len(box.interrupts)}")
        results.check("external audio does not arm barge-in",
                      not voice_bridge.barge_in.is_set())

    results.head("H. barge-in with PTT (existing semantics kept)")
    with guard(results, "owned audio while speaking"), _Sandbox() as box:
        voice_bridge._ptt_active.set()
        voice.SPEAKING.set()
        for _ in range(voice_bridge.BARGE_IN_BLOCKS):
            voice_bridge.audio_callback(LOUD)
        results.check("owned loud audio still interrupts", len(box.interrupts) == 1,
                      f"interrupts={len(box.interrupts)}")
        results.check("owned loud audio still arms barge-in",
                      voice_bridge.barge_in.is_set())
        voice_bridge._ptt_active.clear()

    results.head("J. clarify is answered by a PTT press")
    with guard(results, "answer by hotkey"), _Sandbox() as box:
        box.stt.text = "the econ folder"
        voice_bridge.PTT_ANSWER_WAIT = 5.0
        generation = voice.begin_interaction()
        dispatched = []
        thread, out = _run_in_thread(voice_bridge.ask_user_voice,
                                     "Which folder, Sir?", generation=generation)
        time.sleep(0.2)
        _press(dispatched.append)
        thread.join(timeout=6.0)
        results.check("the press answers the waiting question",
                      out.get("value") == "the econ folder", repr(out.get("value")))
        results.check("the answer is not dispatched as a new command",
                      dispatched == [], repr(dispatched))
        results.check("the answer does not start a new generation",
                      voice.is_current_generation(generation))

    results.head("K. a stale question cannot take the next press")
    with guard(results, "stale waiter"), _Sandbox() as box:
        box.stt.text = "open my notes"
        voice_bridge.PTT_ANSWER_WAIT = 5.0
        old = voice.begin_interaction()
        dispatched = []
        thread, out = _run_in_thread(voice_bridge.ask_user_voice,
                                     "Which folder, Sir?", generation=old)
        time.sleep(0.2)
        voice.begin_interaction()          # something newer owns Diablo now
        _press(dispatched.append)
        thread.join(timeout=7.0)
        results.check("stale question gets no answer", out.get("value") == "",
                      repr(out.get("value")))
        results.check("the press is dispatched as a new command",
                      _wait_for(lambda: dispatched == ["open my notes"], 3.0),
                      repr(dispatched))

    return results.report("PTT microphone gate")


if __name__ == "__main__":
    raise SystemExit(run())
