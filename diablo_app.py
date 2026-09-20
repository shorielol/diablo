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
    from skills import (lesson, pointing, reading, screen_context, server,
                        tutor, voice)

    def _generation():
        try:
            return voice.current_interaction_generation()
        except Exception:
            return None

    def _screen(phase: str, **data) -> None:
        """
        Tell the window what the screen work is doing, as it happens.

        The same events the orb already gets, sent to the other surface that
        needs them. The page keeps no clock of its own for this: every line it
        shows came from one of these, so a read that stalls shows the last
        true thing rather than a convincing animation.

        The wording is worked out here rather than in the page, so the spoken
        side and the written side cannot drift into describing the same
        moment two different ways.
        """
        try:
            app = str(data.get("app") or "")
            label = str(data.get("label") or "")
            if phase == reading.FAILED:
                line = str(data.get("message") or "")
                source = ""
            else:
                line = reading.status_for(phase, app, label)
                source = (reading.source_line(app, str(data.get("title") or ""), label)
                          if phase == reading.DONE else "")
            server.push("screen", phase=phase, line=line, source=source, **data)
        except Exception:
            pass

    # Reading the screen: a span from the first call in to the last call out.
    # The notification is sent under the lock so the depths reach the shell
    # in the order they happened; two threads reading at once cannot leave
    # it stuck on "reading".
    depth = [0]
    lock = threading.Lock()

    def span(fn):
        def spanned(*args, **kwargs):
            with lock:
                depth[0] += 1
                app_shell.notify("reading", depth=depth[0])
            try:
                return fn(*args, **kwargs)
            finally:
                with lock:
                    depth[0] -= 1
                    app_shell.notify("reading", depth=depth[0])
        return spanned

    for name in ("capture_screen", "capture_window", "look", "look_everything",
                 "describe_visually", "structured"):
        original = getattr(screen_context, name, None)
        if callable(original):
            setattr(screen_context, name, span(original))

    # The region being read. When one request reads one window (look,
    # describe_visually, structured), the first window it captures is the
    # content he is looking at, and he may go and stand beside it. A scan of
    # many windows reports nothing, so he never darts from window to window.
    local = threading.local()

    def _ended(result) -> None:
        """
        How the read finished, in the terms the surface shows.

        Read off what perception returned rather than off whether an
        exception escaped: `look` and `structured` report "could not capture
        this window" and "no readable text" as ordinary values, and those two
        are exactly the distinction §12 asks the interface to keep. A read
        that raised has no result at all, which is the same situation as
        having nothing legible and says so.
        """
        window = getattr(local, "window", None) or {}
        app = str(window.get("app") or "")
        outcome = reading.outcome_of(result)
        if outcome == reading.OK:
            _screen(reading.DONE, app=app, title=str(window.get("title") or ""),
                    label=getattr(local, "label", "") or "")
            return
        # Nowhere to stand and nothing to outline: he stays put, and the
        # surface carries the sentence. No movement invented from a failure.
        app_shell.notify("unreadable")
        _screen(reading.FAILED, app=app, outcome=outcome,
                message=reading.message_for(outcome, app))

    def reading_one(fn):
        def one(*args, **kwargs):
            outer = not getattr(local, "open", False)
            if outer:
                local.open, local.reported = True, False
                local.window, local.label = None, ""
                _screen(reading.LOOKING)
            result = None
            try:
                result = fn(*args, **kwargs)
                return result
            finally:
                if outer:
                    local.open = False
                    try:
                        _ended(result)
                    except Exception:
                        pass
        return one

    for name in ("look", "describe_visually", "structured"):
        original = getattr(screen_context, name, None)
        if callable(original):
            setattr(screen_context, name, reading_one(original))

    captured = screen_context.capture_window

    def capture_window(window_id, *args, **kwargs):
        if getattr(local, "open", False) and not getattr(local, "reported", True):
            local.reported = True
            try:
                window = screen_context.window_by_id(window_id)
                if window and window.get("width") and window.get("height"):
                    # Kept for the rest of this read: which window the text
                    # came out of is what turns a normalised box into a place
                    # on the desktop, and it is resolved once, here.
                    local.window = window
                    app_shell.notify("region", x=float(window["x"]), y=float(window["y"]),
                                     w=float(window["width"]), h=float(window["height"]),
                                     generation=_generation())
                    _screen(reading.READING, app=str(window.get("app") or ""),
                            title=str(window.get("title") or ""))
            except Exception:
                pass
        return captured(window_id, *args, **kwargs)

    screen_context.capture_window = capture_window

    # What he is reading, once it is known — which is only after the text has
    # been grouped, never before. The window notified above says "this is the
    # window I am looking at"; this narrows it to the part that carries the
    # content, so a full-screen document stops producing no visible focus at
    # all. `focus_of` returns nothing when the page has no single subject,
    # and nothing is then what happens: he stays where he is.
    grouped = screen_context.ocr_regions

    def ocr_regions(path, window=None, *args, **kwargs):
        result = grouped(path, window, *args, **kwargs)
        if not getattr(local, "open", False) or not isinstance(result, list):
            return result
        known = window or getattr(local, "window", None)
        try:
            found = reading.focus_of(result, known) if known else None
            if found is not None and reading.still_shows(known):
                local.label = found.label
                x, y, w, h = found.rect
                app_shell.notify("focus", x=x, y=y, w=w, h=h,
                                 label=found.label, generation=_generation())
                _screen(reading.UNDERSTOOD, app=found.app, title=found.title,
                        label=found.label)
        except Exception:
            pass
        return result

    screen_context.ocr_regions = ocr_regions

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

    # ── teaching ─────────────────────────────────────────────────────────
    #
    # The walkthrough already plans steps, finds them, waits for the click and
    # asks the screen whether anything changed. All of that stays exactly
    # where it is. Two things are added: the lifecycle is reported to the orb
    # and the window, and the drawing is moved onto the one overlay the shell
    # already owns, so the orb is the marker instead of standing at home
    # while a second mark does the pointing.

    counted = [0]

    def _lesson(event: str, **data) -> None:
        """One lifecycle moment, to the orb and to the surface."""
        counted[0] += 1
        seq = counted[0]
        phase, payload = None, {}
        if event == tutor.STARTED:
            phase = lesson.GUIDING
            payload = {"total": int(data.get("total") or 0),
                       "app": str(data.get("app") or "")}
            app_shell.notify("lesson_begin", seq=seq, **payload)
        elif event == tutor.PRESENTED:
            phase = lesson.GUIDING
            payload = {k: data.get(k) for k in
                       ("x", "y", "label", "say", "index", "total", "on_light",
                        "found")}
            app_shell.notify("lesson_step", seq=seq,
                             generation=_generation(), **payload)
        elif event == tutor.WAITING:
            phase = lesson.WAITING
            app_shell.notify("lesson_waiting", seq=seq)
        elif event == tutor.VERIFIED:
            phase = lesson.VERIFIED
            app_shell.notify("lesson_verified", seq=seq)
        elif event == tutor.NOT_VERIFIED:
            phase = lesson.UNVERIFIED
            payload = {"note": str(data.get("note") or "")}
            app_shell.notify("lesson_unverified", seq=seq, **payload)
        elif event in (tutor.COMPLETED, tutor.CANCELLED):
            phase = (lesson.CANCELLED if event == tutor.CANCELLED
                     else lesson.DONE)
            app_shell.notify("lesson_finish", seq=seq, why=phase)
        if phase is None:
            return
        step = None
        if data.get("index"):
            step = lesson.Step(index=int(data.get("index") or 1),
                               total=int(data.get("total") or 1),
                               label=str(data.get("label") or ""))
        try:
            server.push("lesson", phase=phase, seq=seq,
                        index=data.get("index"), total=data.get("total"),
                        line=lesson.line_for(phase, step,
                                             str(data.get("note") or "")),
                        fault=lesson.is_fault(phase))
        except Exception:
            pass

    tutor.SINK = _lesson

    # One mark on screen. The tutor's own overlay drew a marker head of its
    # own; with the orb travelling to the step as well that is two things
    # pointing at one target. When a shell exists the shell's overlay does
    # the drawing and the click watching, and the tutor's marker becomes a
    # thin wrapper over it.
    class ShellMarker(tutor.Marker):
        """The tutor's marker, drawn by the shell so there is only one."""

        def _shell(self):
            shell = getattr(app_shell, "_SHELL", None)
            return shell if shell is not None and shell.lesson.live else None

        def move(self, x, y, label, step, on_light=False):
            # The step event has already reached the shell and started the
            # approach; the shell draws when he arrives, not before.
            if self._shell() is not None:
                return True
            return super().move(x, y, label, step, on_light=on_light)

        def ready(self, timeout: float = 5.0) -> bool:
            shell = self._shell()
            if shell is None:
                return super().ready(timeout)
            return shell.guide.ready(timeout)

        def wait_for_click(self, x, y, radius=tutor.CLICK_RADIUS,
                           timeout=tutor.CLICK_TIMEOUT, should_stop=None):
            shell = self._shell()
            if shell is None:
                return super().wait_for_click(x, y, radius, timeout, should_stop)
            return shell.guide.wait_for_click(x, y, radius, timeout, should_stop)

        def close(self):
            if self._shell() is None:
                super().close()

    tutor.Marker = ShellMarker

    # Looked for something to point at and found nothing: say so visibly,
    # rather than leaving him to wander or invent a place.
    locate = pointing.locate

    def located(*args, **kwargs):
        hits = locate(*args, **kwargs)
        if not hits:
            app_shell.notify("no_target")
        return hits

    pointing.locate = located


if __name__ == "__main__":
    sys.exit(main())
