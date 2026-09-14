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

    def listen():
        try:
            voice_bridge.boot_and_listen()
        except BaseException:
            traceback.print_exc()
            manager.emit("error", subsystem="voice", message="the listener stopped")

    # Hold Control+Option to talk. The wake word still works; this is the
    # path for when you want to know it is listening before you start, which
    # is most of the time.
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


if __name__ == "__main__":
    sys.exit(main())
