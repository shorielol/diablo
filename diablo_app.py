#!/usr/bin/env python3
"""
D.I.A.B.L.O. — what the app bundle actually runs.

Order matters and is not obvious:

  1. the manager, so there is something to report state to
  2. the local API, so the window has something to load
  3. the voice loop, on a background thread
  4. AppKit, on the main thread, forever

Three and four are the wrong way round from how this used to run. AppKit will
not share the main thread, and a window created from a background thread does
not draw — it reports isVisible as true and composites nothing, which is a
genuinely horrible thing to debug because every API call succeeds.

The same lesson applies to the bundle itself: a bare Python process launched
from a terminal cannot show a window at all, whatever its activation policy.
It has to be inside the .app. That is why `diablo run` stays headless and the
GUI only ever comes up through Diablo.app.
"""

import os
import sys
import threading
import time
import traceback

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, HOME)


def main() -> int:
    from companion_manager import create_manager
    from skills import server

    manager = create_manager()
    manager.start()

    url = server.start()
    if url is None:
        print("  [App] The API port is taken. The window will not load.")

    # Import late: this pulls in whisper and torch, which is several seconds,
    # and the menu bar should appear before that rather than after.
    import voice_bridge

    server.attach(manager=manager,
                  speak=voice_bridge.speak,
                  handle=voice_bridge.process_command)

    # Let the voice loop drive the pill and the window.
    _bridge_to_manager(voice_bridge, manager)
    # And tell the presence the things the manager has no state for.
    _bridge_presence(voice_bridge)

    def listen():
        try:
            voice_bridge.boot_and_listen()
        except BaseException:
            traceback.print_exc()
            manager.emit("error", subsystem="voice", message="the listener stopped")

    # Hold Control+Option to talk.
    def talk_start():
        voice_bridge.push_to_talk_start(on_text=voice_bridge.process_command)

    import app_shell
    app_shell.run(listen=listen, manager=manager, url=url,
                  push_to_talk=(talk_start, voice_bridge.push_to_talk_stop))
    return 0


def _bridge_to_manager(voice_bridge, manager) -> None:
    """
    Make the old set_ui_state() drive the new state machine.

    voice_bridge has funnelled every state change through set_ui_state since
    long before the manager existed, including the part that stops Diablo
    waking itself up. Wrapping it is less invasive than rewriting the call
    sites, and it means nothing can change state without the UI hearing about
    it — which was the whole problem with the old globals.
    """
    from companion_manager import DiabloState

    original = voice_bridge.set_ui_state
    mapping = {
        "idle": DiabloState.IDLE,
        "listening": DiabloState.LISTENING,
        "speaking": DiabloState.RESPONDING,
        "thinking": DiabloState.PROCESSING,
    }

    def wrapped(state):
        original(state)
        target = mapping.get(state)
        if target is not None:
            manager.transition(target, "voice loop")

    voice_bridge.set_ui_state = wrapped

    # The audio callback knows the input level; the pill wants it for the
    # waveform. One assignment per block, which is atomic and costs nothing.
    original_callback = voice_bridge.audio_callback

    def with_level(block):
        try:
            import numpy as np
            manager.mic.note_level(float(np.abs(block).max()))
        except Exception:
            pass
        return original_callback(block)

    voice_bridge.audio_callback = with_level


def _bridge_presence(voice_bridge) -> None:
    """
    Tell the orb what is really happening, from where it actually happens.

    Each of these wraps a module attribute, exactly as set_ui_state and the
    audio callback are wrapped above: nothing in perception, the agent, the
    voice or the job system changes, and every caller that reaches these
    functions through their module is reported. Until the shell exists,
    app_shell.notify() returns False and everything behaves as it did.
    """
    import app_shell
    from skills import pointing, screen_context, server, voice

    # Reading the screen: a span from the first call in to the last call out.
    # The notification is sent under the lock so the depths reach the shell
    # in the order they happened; two threads reading at once cannot leave
    # it stuck on "reading".
    depth = [0]
    lock = threading.Lock()

    def span(fn):
        def reading(*args, **kwargs):
            with lock:
                depth[0] += 1
                app_shell.notify("reading", depth=depth[0])
            try:
                return fn(*args, **kwargs)
            finally:
                with lock:
                    depth[0] -= 1
                    app_shell.notify("reading", depth=depth[0])
        return reading

    for name in ("capture_screen", "capture_window", "look", "look_everything",
                 "describe_visually", "structured"):
        original = getattr(screen_context, name, None)
        if callable(original):
            setattr(screen_context, name, span(original))

    # A question asked and waiting for the next press to answer it.
    asked = voice_bridge._answer_by_press

    def waiting(prompt, generation):
        app_shell.notify("waiting", waiting=True)
        try:
            return asked(prompt, generation)
        finally:
            app_shell.notify("waiting", waiting=False)

    voice_bridge._answer_by_press = waiting

    # Cut off mid-sentence. The key press interrupts on every press, speaking
    # or not, so only an interruption of actual speech is shown as one.
    stop = voice.interrupt

    def interrupt():
        if getattr(voice_bridge, "_ui_state", "") == "speaking":
            app_shell.notify("interrupted")
        return stop()

    voice.interrupt = interrupt

    # Background jobs: running, finished, failed.
    running = set()
    push = server.push

    def pushed(event, **data):
        job = data.get("id")
        if event == "job_started" and job:
            running.add(job)
            app_shell.notify("jobs", running=len(running))
        elif event in ("job_done", "job_failed") and job in running:
            running.discard(job)
            app_shell.notify("jobs", running=len(running))
            app_shell.notify("success" if event == "job_done" else "failure")
        return push(event, **data)

    server.push = pushed

    # Something found on screen and about to be pointed at. The orb goes to
    # stand beside it and the overlay follows once he has arrived; without a
    # shell, the old overlay with its own marker is drawn as before.
    show = pointing.show

    def point(x, y, label="", seconds=3.0, marker=True):
        try:
            generation = voice.current_interaction_generation()
        except Exception:
            generation = None

        def spawn(with_marker):
            show(x, y, label, seconds, marker=with_marker)

        if app_shell.notify("target", x=x, y=y, label=label, seconds=seconds,
                            spawn=spawn, generation=generation):
            return True
        return show(x, y, label, seconds, marker=marker)

    pointing.show = point


if __name__ == "__main__":
    sys.exit(main())
