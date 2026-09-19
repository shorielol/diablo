#!/usr/bin/env python3
"""Deterministic Phase 3B interaction and speech-ownership checks.

The native player is replaced with a controllable Popen double.  This keeps the
tests focused on the real admission/termination code without requiring a
speaker, Voicebox, or an Ollama model.
"""

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402
from skills import delegation_tool, voice  # noqa: E402


class FakePopen:
    active = 0
    maximum_active = 0
    created = []
    lock = threading.Lock()

    def __init__(self, command):
        self.command = command
        self.stop = threading.Event()
        self.finished = False
        with self.lock:
            type(self).created.append(command)
            type(self).active += 1
            type(self).maximum_active = max(type(self).maximum_active,
                                            type(self).active)

    def wait(self, timeout=None):
        if timeout is None:
            self.stop.wait(0.05)
        elif not self.stop.wait(timeout):
            raise TimeoutError("fake process still running")
        self._finish()
        return 0

    def terminate(self):
        self.stop.set()

    def kill(self):
        self.stop.set()

    def _finish(self):
        with self.lock:
            if not self.finished:
                self.finished = True
                type(self).active -= 1


def _reset_voice():
    voice.forget_listeners()
    voice.on_audio_start = None
    voice.on_audio_end = None
    voice._current_generation = 0
    voice._generation_context.set(None)
    voice._interrupt_serial = 0
    voice._admitted_speech_ids.clear()
    voice._active_process = None
    voice.STOP.clear()
    voice.SPEAKING.clear()
    FakePopen.active = 0
    FakePopen.maximum_active = 0
    FakePopen.created = []


def run():
    results = Results()
    original_popen = voice.subprocess.Popen
    original_cached_path = voice.cached_path
    voice.subprocess.Popen = FakePopen
    voice.cached_path = lambda _text: "/tmp/phase3b-fake.wav"
    try:
        _reset_voice()

        results.head("A. stale user response")
        with guard(results, "old generation is suppressed"):
            old = voice.begin_interaction()
            current = voice.begin_interaction()
            out = voice.speak("old answer", generation=old)
            results.check("stale result does not play", out == "stale",
                          f"route={out}")
            results.check("current generation remains newer", current != old)
            results.check("no native player was started", not FakePopen.created)

        results.head("B. serialized playback")
        with guard(results, "two speech callers cannot overlap"):
            _reset_voice()
            generation = voice.begin_interaction()
            outputs = []
            threads = [threading.Thread(
                target=lambda text=text: outputs.append(
                    voice.speak(text, generation=generation)))
                       for text in ("first", "second")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            results.check("both requests completed", outputs.count("cache") == 2,
                          repr(outputs))
            results.check("native playback maximum was one",
                          FakePopen.maximum_active == 1,
                          f"maximum={FakePopen.maximum_active}")

        results.head("C. ordinary playback interruption")
        with guard(results, "interrupt terminates native playback"):
            _reset_voice()
            generation = voice.begin_interaction()
            result = []
            worker = threading.Thread(
                target=lambda: result.append(
                    voice.speak("interrupt me", generation=generation)))
            worker.start()
            deadline = time.time() + 1
            while not FakePopen.created and time.time() < deadline:
                time.sleep(0.005)
            newer_generation = voice.begin_interaction()
            worker.join(2)
            newer_result = voice.speak("new current response",
                                       generation=newer_generation)
            results.check("speech returned interrupted", result == ["interrupted"],
                          repr(result))
            results.check("new current speech played", newer_result == "cache",
                          newer_result)
            results.check("only the newer speech followed interruption",
                          len(FakePopen.created) == 2,
                          f"created={len(FakePopen.created)}")
            results.check("native process exited", FakePopen.active == 0,
                          f"active={FakePopen.active}")

        results.head("D. stale queued result")
        with guard(results, "queued work may finish silently"):
            _reset_voice()
            old = voice.begin_interaction()
            result = []
            release = threading.Event()

            def finish_late():
                release.wait(1)
                result.append(voice.speak(
                    "late task result", generation=old,
                    speech_id="job:late:result"))

            worker = threading.Thread(target=finish_late)
            worker.start()
            voice.begin_interaction()
            release.set()
            worker.join(2)
            results.check("late result was suppressed", result == ["stale"],
                          repr(result))
            results.check("late result made no audio", not FakePopen.created)
            metadata = []

            def callback(_text, **kwargs):
                metadata.append(kwargs)

            delegation_tool._speak_with_metadata(
                callback, "late task result", speech_id="job:late:result",
                speech_kind="TASK_RESULT")
            results.check("queued completion carries an admission identity",
                          metadata == [{"speech_id": "job:late:result",
                                        "speech_kind": "TASK_RESULT"}],
                          repr(metadata))

        results.head("E. duplicate completion")
        with guard(results, "one completion identity is admitted once"):
            _reset_voice()
            generation = voice.begin_interaction()
            first = voice.speak("one final answer", generation=generation,
                                speech_id="job:1:result")
            second = voice.speak("one final answer", generation=generation,
                                 speech_id="job:1:result")
            results.check("first completion played", first == "cache", first)
            results.check("duplicate completion was rejected", second == "duplicate",
                          second)
            results.check("only one native player was started",
                          len(FakePopen.created) == 1,
                          f"created={len(FakePopen.created)}")

        results.head("F. normal PTT interaction")
        with guard(results, "current PTT response still speaks once"):
            _reset_voice()
            generation = voice.begin_interaction()
            out = voice.speak("current answer", generation=generation)
            results.check("current result played", out == "cache", out)
            results.check("one response occupied the speaker",
                          len(FakePopen.created) == 1,
                          f"created={len(FakePopen.created)}")

        results.head("G. background task isolation")
        with guard(results, "background work cannot interrupt current response"):
            _reset_voice()
            old = voice.begin_interaction()
            late_result = []
            release = threading.Event()

            def background_task():
                release.wait(1)
                late_result.append(voice.speak(
                    "background completion", generation=old,
                    speech_id="job:background:result"))

            worker = threading.Thread(target=background_task)
            worker.start()
            current = voice.begin_interaction()
            current_result = voice.speak("new PTT response", generation=current)
            release.set()
            worker.join(2)
            results.check("current response played", current_result == "cache",
                          current_result)
            results.check("background completion became stale",
                          late_result == ["stale"], repr(late_result))
            results.check("background task made no second playback",
                          len(FakePopen.created) == 1,
                          f"created={len(FakePopen.created)}")

        results.head("PTT contract guard")
        with guard(results, "PTT contract remains present"):
            path = Path(os.getenv("DIABLO_VOICE_BRIDGE",
                                 Path.home() / ".diablo" / "voice_bridge.py"))
            source = path.read_text()
            results.check("PTT-only defaults on",
                          'config.env_bool("DIABLO_PTT_ONLY", True)' in source)
            results.check("explicit PTT APIs remain",
                          "def push_to_talk_start" in source
                          and "def push_to_talk_stop" in source)

        results.head("H. rapid generation changes")
        with guard(results, "only the newest generation can speak"):
            _reset_voice()
            generations = [voice.begin_interaction() for _ in range(3)]
            stale = [voice.speak(f"stale-{generation}", generation=generation)
                     for generation in generations[:-1]]
            current = voice.speak("newest", generation=generations[-1])
            results.check("all older results are stale", stale == ["stale", "stale"],
                          repr(stale))
            results.check("newest result plays", current == "cache", current)
            results.check("only newest result reached audio",
                          len(FakePopen.created) == 1,
                          f"created={len(FakePopen.created)}")

        return results.report("phase3b ownership")
    finally:
        voice.subprocess.Popen = original_popen
        voice.cached_path = original_cached_path
        _reset_voice()


if __name__ == "__main__":
    raise SystemExit(run())
