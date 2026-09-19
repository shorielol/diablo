#!/usr/bin/env python3
"""Build 4C AppKit Control+Option PTT integration coverage.

The deterministic path invokes the production AppKit monitor callbacks captured
from ``Shell.install_push_to_talk``.  It does not synthesize macOS keystrokes:
posting keyboard events would require CGEventPost, which is deliberately out of
scope for Diablo.  With Accessibility granted, set
``DIABLO_TEST_APPKIT_PTT_INTERACTIVE=1`` and physically hold/release
Control+Option to exercise the real global monitor too.
"""

import ast
import os
import queue
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

import app_shell  # noqa: E402
import voice_bridge  # noqa: E402
from skills import conversation, permissions, voice  # noqa: E402


DIABLO_HOME = Path(os.getenv("DIABLO_HOME", Path.home() / ".diablo"))


class _FlagsEvent:
    """The AppKit callback surface used by the production monitor."""

    def __init__(self, flags):
        self.flags = flags

    def modifierFlags(self):
        return self.flags


class _CapturedMonitors:
    """Captures the real production callback registered with each monitor."""

    def __init__(self):
        self.global_calls = []
        self.local_calls = []

    def addGlobalMonitorForEventsMatchingMask_handler_(self, mask, handler):
        self.global_calls.append((mask, handler))
        return ("global", len(self.global_calls))

    def addLocalMonitorForEventsMatchingMask_handler_(self, mask, handler):
        self.local_calls.append((mask, handler))
        return ("local", len(self.local_calls))


@contextmanager
def _captured_monitor_install(on_start, on_stop):
    """Install Shell's production monitor logic with callbacks we can invoke."""
    monitors = _CapturedMonitors()
    shell = type("PttShellFixture", (), {})()
    original_event = app_shell.NSEvent
    original_accessibility = permissions.accessibility
    app_shell.NSEvent = monitors
    permissions.accessibility = lambda: True
    try:
        installed = app_shell.Shell.install_push_to_talk(shell, on_start, on_stop)
        yield monitors, shell, installed
    finally:
        app_shell.NSEvent = original_event
        permissions.accessibility = original_accessibility


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


def _wake_feed_is_gated():
    source = (DIABLO_HOME / "voice_bridge.py").read_text()
    tree = ast.parse(source)
    feeds = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "feed"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "wake_listener"
    ]
    if len(feeds) != 1:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not (isinstance(node.test, ast.UnaryOp)
                and isinstance(node.test.op, ast.Not)
                and isinstance(node.test.operand, ast.Name)
                and node.test.operand.id == "PTT_ONLY"):
            continue
        if any(call is feeds[0] for statement in node.body for call in ast.walk(statement)):
            return True
    return False


def _run_interactive_physical_monitor(results):
    """Optional real-key exercise; never posts synthetic keyboard events."""
    try:
        trusted = permissions.accessibility()
    except Exception as error:
        results.skip("physical AppKit monitor", f"Accessibility check failed: {error}")
        return
    if not trusted:
        results.skip("physical AppKit monitor",
                     "Accessibility permission unavailable to this test process. "
                     "Grant it to Diablo/test runner, then rerun with "
                     "DIABLO_TEST_APPKIT_PTT_INTERACTIVE=1.")
        return
    if os.getenv("DIABLO_TEST_APPKIT_PTT_INTERACTIVE") != "1":
        results.skip("physical AppKit monitor",
                     "Accessibility is available, but interactive mode was not requested. "
                     "Set DIABLO_TEST_APPKIT_PTT_INTERACTIVE=1, then physically "
                     "hold and release Control+Option within 10 seconds.")
        return

    from Foundation import NSDate, NSRunLoop

    calls = []
    shell = type("PhysicalPttShellFixture", (), {})()
    installed = app_shell.Shell.install_push_to_talk(
        shell,
        lambda: calls.append("down"),
        lambda: calls.append("up"),
    )
    try:
        if not installed:
            results.bad("physical AppKit monitor", "monitor registration was refused")
            return
        print("\n  Hold Control+Option, then release it within 10 seconds.")
        deadline = time.monotonic() + 10.0
        run_loop = NSRunLoop.currentRunLoop()
        while time.monotonic() < deadline and calls != ["down", "up"]:
            run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
        results.check("physical key caused exactly one down/up pair",
                      calls == ["down", "up"], repr(calls))
    finally:
        for token in (getattr(shell, "_ptt_global", None),
                      getattr(shell, "_ptt_local", None)):
            if token is not None:
                try:
                    app_shell.NSEvent.removeMonitor_(token)
                except Exception:
                    pass


def run():
    results = Results()
    control = app_shell.NSEventModifierFlagControl
    option = app_shell.NSEventModifierFlagOption
    shift = app_shell.NSEventModifierFlagShift
    held = control | option

    results.head("A. AppKit local/global monitor path")
    with guard(results, "production monitor callbacks are installed"):
        calls = []
        with _captured_monitor_install(
                lambda: calls.append("down"), lambda: calls.append("up")) as fixture:
            monitors, _shell, installed = fixture
            results.check("monitor installation succeeds", installed)
            results.check("one global flags monitor is registered",
                          len(monitors.global_calls) == 1)
            results.check("one local flags monitor is registered",
                          len(monitors.local_calls) == 1)
            global_handler = monitors.global_calls[0][1]
            local_handler = monitors.local_calls[0][1]
            results.check("both monitor registrations watch flag changes",
                          monitors.global_calls[0][0] == app_shell.NSEventMaskFlagsChanged
                          and monitors.local_calls[0][0] == app_shell.NSEventMaskFlagsChanged)

            # Control then Option: start only when the complete chord is down.
            control_event = _FlagsEvent(control)
            held_event = _FlagsEvent(held)
            results.check("local monitor returns its event", local_handler(control_event) is control_event)
            global_handler(control_event)
            global_handler(held_event)
            # The same physical transition can be seen by both monitors as
            # focus changes; the shared _ptt_down flag must absorb the echo.
            local_handler(held_event)
            global_handler(held_event)
            results.check("Control then Option starts exactly once", calls == ["down"], repr(calls))

            # Extra modifiers are not the PTT chord and releasing through one
            # monitor must be absorbed when echoed by the other.
            global_handler(_FlagsEvent(held | shift))
            local_handler(_FlagsEvent(control))
            global_handler(_FlagsEvent(control))
            local_handler(_FlagsEvent(0))
            results.check("release stops exactly once", calls == ["down", "up"], repr(calls))

    results.head("B. modifier ordering and focus changes")
    with guard(results, "reverse ordering remains one session"):
        calls = []
        with _captured_monitor_install(
                lambda: calls.append("down"), lambda: calls.append("up")) as fixture:
            monitors, _shell, _installed = fixture
            global_handler = monitors.global_calls[0][1]
            local_handler = monitors.local_calls[0][1]
            global_handler(_FlagsEvent(option))
            local_handler(_FlagsEvent(held))
            # Global monitor sees the same state after focus moves away.
            global_handler(_FlagsEvent(held))
            global_handler(_FlagsEvent(option))
            # Local monitor's release echo must not stop a second time.
            local_handler(_FlagsEvent(0))
            results.check("Option then Control starts and stops once",
                          calls == ["down", "up"], repr(calls))

    results.head("C. PTT capture, generation, and release boundary")
    with guard(results, "hotkey drives one complete PTT session"):
        original_session = conversation.session
        original_state = voice_bridge.set_ui_state
        original_speak = voice_bridge.speak
        original_model = getattr(voice_bridge, "stt_model", None)
        original_listener = getattr(voice_bridge, "wake_listener", None)
        original_handle = voice_bridge.delegation_tool.handle
        original_interrupt = voice_bridge.voice.interrupt
        original_generation = voice._current_generation
        original_context = voice._generation_context.get()
        spoken, handled, stt_audio = [], [], []
        completed = threading.Event()

        class _Stt:
            def transcribe(self, audio, **_kwargs):
                stt_audio.append(np.asarray(audio).copy())
                return {"text": "explain the PTT integration test"}

        try:
            conversation.session = conversation.Session()
            voice._current_generation = 0
            voice._generation_context.set(None)
            voice_bridge.set_ui_state = lambda _state: None
            voice_bridge.speak = lambda text, **kwargs: spoken.append((text, kwargs)) or "test"
            voice_bridge.stt_model = _Stt()
            voice_bridge.wake_listener = None
            voice_bridge.voice.interrupt = lambda: None
            voice_bridge.MIC_LIVE.set()
            voice_bridge._ptt_release.clear()
            voice_bridge._ptt_active.clear()
            voice_bridge.is_capturing_command.clear()
            _drain(voice_bridge.command_capture_queue)
            voice_bridge._preroll.clear()
            voice_bridge._preroll.append(np.ones((voice_bridge.CHUNK, 1), dtype=np.float32))

            def fake_handle(text, speak, ask=None, confirm=None, generation=None):
                committed = conversation.session.commit_turn(
                    generation, text, "one PTT response")
                speech_result = speak("one PTT response", speech_id="ptt:test:result",
                                      speech_kind="USER_RESPONSE")
                handled.append((text, generation, committed, speech_result))
                completed.set()
                return "chat"

            voice_bridge.delegation_tool.handle = fake_handle
            starts, stops = [], []

            def start():
                starts.append(voice_bridge.push_to_talk_start(
                    on_text=voice_bridge.process_command))

            def stop():
                stops.append(True)
                voice_bridge.push_to_talk_stop()

            with _captured_monitor_install(start, stop) as fixture:
                monitors, _shell, _installed = fixture
                global_handler = monitors.global_calls[0][1]
                local_handler = monitors.local_calls[0][1]
                global_handler(_FlagsEvent(control))
                global_handler(_FlagsEvent(held))
                results.check("hotkey down starts one active PTT session",
                              _wait_for(voice_bridge.push_to_talk_active)
                              and starts == [True], repr(starts))
                # Repeated local/global notifications while held cannot start a
                # second worker.
                local_handler(_FlagsEvent(held))
                global_handler(_FlagsEvent(held))
                time.sleep(max(voice_bridge.PTT_MIN_SECONDS + 0.05, 0.30))
                results.check("hold preserves one active session", starts == [True])
                local_handler(_FlagsEvent(control))
                global_handler(_FlagsEvent(control))

            results.check("hotkey release occurs once", len(stops) == 1, repr(stops))
            results.check("release reaches STT and process_command", completed.wait(4.0))
            results.check("exactly one captured audio payload reached STT",
                          len(stt_audio) == 1 and stt_audio[0].size > 0,
                          f"captures={len(stt_audio)}")
            results.check("one command was dispatched", len(handled) == 1, repr(handled))
            generation = handled[0][1] if handled else None
            state = conversation.session.snapshot()
            results.check("PTT process command owns generation N",
                          generation is not None and voice.is_current_generation(generation))
            results.check("conversation commit owns the same generation",
                          bool(handled) and handled[0][2]
                          and [turn["content"] for turn in state["turns"]] ==
                          ["explain the PTT integration test", "one PTT response"],
                          repr(state["turns"]))
            results.check("speech admission carries that same generation",
                          bool(spoken) and spoken[-1][1].get("generation") == generation,
                          repr(spoken))
            time.sleep(0.25)
            results.check("release leaves no active or duplicate PTT worker",
                          not voice_bridge.push_to_talk_active() and len(handled) == 1)
        finally:
            _drain(voice_bridge.command_capture_queue)
            voice_bridge._ptt_release.set()
            voice_bridge.is_capturing_command.clear()
            voice_bridge._ptt_active.clear()
            voice_bridge.set_ui_state = original_state
            voice_bridge.speak = original_speak
            voice_bridge.stt_model = original_model
            voice_bridge.wake_listener = original_listener
            voice_bridge.delegation_tool.handle = original_handle
            voice_bridge.voice.interrupt = original_interrupt
            conversation.session = original_session
            voice._current_generation = original_generation
            voice._generation_context.set(original_context)

    results.head("D. no-hotkey negative path")
    with guard(results, "ambient audio cannot create a command session"):
        original_listener = getattr(voice_bridge, "wake_listener", None)
        original_active = voice_bridge.is_capturing_command.is_set()

        class _WakeProbe:
            def __init__(self):
                self.feed_calls = 0

            def feed(self, _block):
                self.feed_calls += 1
                return None

        probe = _WakeProbe()
        try:
            voice_bridge.wake_listener = probe
            voice_bridge._ptt_active.clear()
            voice_bridge._ptt_release.clear()
            voice_bridge.is_capturing_command.clear()
            _drain(voice_bridge.command_capture_queue)
            block = np.full(voice_bridge.CHUNK, 0.25, dtype=np.float32)
            for _source in ("ambient speech", "another application", "Diablo audio"):
                voice_bridge.audio_callback(block)
            results.check("audio alone does not activate PTT",
                          not voice_bridge.push_to_talk_active())
            results.check("audio alone does not open a command capture",
                          not voice_bridge.is_capturing_command.is_set()
                          and voice_bridge.command_capture_queue.empty())
            results.check("wake feed remains absent without a hotkey", probe.feed_calls == 0)
            results.check("production listener dispatch is gated by PTT_ONLY",
                          _wake_feed_is_gated())
        finally:
            _drain(voice_bridge.audio_queue)
            _drain(voice_bridge.command_capture_queue)
            voice_bridge.wake_listener = original_listener
            if original_active:
                voice_bridge.is_capturing_command.set()
            else:
                voice_bridge.is_capturing_command.clear()

    results.head("E. app lifecycle wiring")
    with guard(results, "normal app startup owns the AppKit PTT path"):
        app_source = (DIABLO_HOME / "diablo_app.py").read_text()
        launcher_source = (Path.home() / "Applications" / "Diablo.app" / "Contents"
                           / "MacOS" / "Diablo").read_text()
        results.check("app passes PTT callbacks to AppKit shell",
                      "push_to_talk=(talk_start, voice_bridge.push_to_talk_stop)" in app_source)
        results.check("installed launcher starts Diablo's AppKit app path",
                      'exec "$PY" -u "$DIABLO_HOME/diablo_app.py"' in launcher_source)

    results.head("F. optional physical Control+Option exercise")
    with guard(results, "permission-gated physical monitor run"):
        _run_interactive_physical_monitor(results)

    return results.report("AppKit PTT integration")


if __name__ == "__main__":
    raise SystemExit(run())
