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
    from skills import (doing, lesson, pointing, reading, screen_context, server,
                        teaching, tutor, voice)

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

    def point(x, y, label="", seconds=3.0, marker=True, width=0.0, height=0.0):
        # `width` and `height` are an area's extent, and they have to reach the
        # shell. Phase B added them to pointing.show, but this wrapper still
        # had the old signature, so in the running app every region answer
        # raised TypeError here and came back as a failure; the validation
        # runs replaced pointing.show with a stub and never passed through it.
        try:
            generation = voice.current_interaction_generation()
        except Exception:
            generation = None

        def spawn(with_marker):
            show(x, y, label, seconds, marker=with_marker, width=width, height=height)

        if app_shell.notify("target", x=x, y=y, label=label, seconds=seconds,
                            spawn=spawn, generation=generation,
                            w=float(width or 0.0), h=float(height or 0.0)):
            return True
        return show(x, y, label, seconds, marker=marker, width=width, height=height)

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
        elif event == tutor.ACTED:
            phase = lesson.ACTED
            app_shell.notify("lesson_acted", seq=seq)
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

    # ── teaching with the cursor ─────────────────────────────────────────
    #
    # teaching.run_path's view of the shell. It numbers its events from the
    # same counter as the walkthrough above, because they land in the same
    # Lesson: two counters would let a teaching step look older than a
    # walkthrough event it came after, and be dropped as stale.

    class _Teaching:
        """One lesson's worth of teaching, reported to the shell and surface."""

        def __init__(self):
            self.generation = _generation()
            self.first = 0
            self.sent = 0

        def _next(self) -> int:
            counted[0] += 1
            self.sent = counted[0]
            return self.sent

        def _push(self, phase, index=None, total=None, line=""):
            try:
                server.push("lesson", phase=phase, seq=self.sent, index=index,
                            total=total, line=line, fault=lesson.is_fault(phase))
            except Exception:
                pass

        def begin(self, total):
            self.first = self._next()
            app_shell.notify("teach_begin", total=int(total), seq=self.first)

        def step(self, place, index, total, hold=False):
            arrived = threading.Event()
            cx, cy = place.centre
            sent = app_shell.notify(
                "teach_step", x=cx, y=cy, w=place.w, h=place.h, label=place.label,
                index=index, total=total, mode=place.mode, say=place.say,
                seq=self._next(), generation=self.generation, arrived=arrived,
                hold=bool(hold), act=bool(place.act))
            if not sent:
                # No shell yet: the old overlay, and nothing to wait for.
                show(cx, cy, place.label, 3.0, width=place.w if place.is_region else 0.0,
                     height=place.h if place.is_region else 0.0)
                arrived.set()
            elif not hold:
                # A single thing is explained by the reply itself; the surface
                # line is for a lesson with steps.
                self._push(lesson.GUIDING, index, total,
                           teaching.announce(place.label, index, total))
            return arrived

        def await_action(self, place, index, total):
            """The existing closed loop: armed click, then the screen check."""
            shell = getattr(app_shell, "_SHELL", None)
            if shell is None:
                return teaching.UNVERIFIED
            from skills import screen
            window = screen_context.frontmost_window()
            before = screen.capture_this_window(window) if window else None
            app_shell.notify("lesson_waiting", seq=self._next())
            self._push(lesson.WAITING, index, total,
                       lesson.line_for(lesson.WAITING, lesson.Step(index, total)))
            cx, cy = place.centre
            try:
                shell.guide.arm(cx, cy)
            except Exception:
                pass
            pressed = shell.guide.wait_for_click(cx, cy, tutor.CLICK_RADIUS,
                                                 tutor.CLICK_TIMEOUT,
                                                 should_stop=self.cancelled)
            if not pressed:
                if before is not None:
                    before.release()
                return teaching.UNVERIFIED
            app_shell.notify("lesson_acted", seq=self._next())
            changed = False
            try:
                after = screen.capture_this_window(window) if window else None
                if before is not None and after is not None and before.ok and after.ok:
                    changed = not screen.compare(before, after).same
                if after is not None:
                    after.release()
            except Exception:
                changed = False
            finally:
                if before is not None:
                    before.release()
            if changed:
                app_shell.notify("lesson_verified", seq=self._next())
                return teaching.VERIFIED
            app_shell.notify("lesson_unverified", seq=self._next(),
                             note="That didn't seem to change anything.")
            return teaching.UNVERIFIED

        def finish(self, why):
            app_shell.notify("teach_finish", why=why, seq=self._next())
            self._push(why, line=lesson.line_for(why))

        def cancelled(self):
            if self.generation is not None and _generation() != self.generation:
                return True
            shell = getattr(app_shell, "_SHELL", None)
            if shell is None:
                return False
            # notify() reaches the shell on the main thread, later. Until the
            # shell has seen this lesson begin, the closed lesson it still
            # holds is the previous one's and says nothing about this.
            seen = shell.lesson.seq
            if seen > self.sent:
                return True            # overtaken by something newer
            if seen < self.first:
                return False           # not caught up yet
            return not shell.lesson.live

        def wait(self, seconds):
            threading.Event().wait(seconds)
            return self.cancelled()

    teaching.CHANNEL = _Teaching

    # ── doing something ──────────────────────────────────────────────────
    #
    # The DO engine is complete and silent: two state machines, eleven named
    # failures, verification against the screen, and not one print. Rather
    # than edit it, `Task.to` — the single place every task state change goes
    # through — is wrapped here, the same way the tutor's marker is. The
    # engine keeps deciding; this only reports.
    #
    # Nothing below starts a task. There is no do_engine.run here, and
    # computer_use stays disabled: the GUI can render every DO state without
    # any of them being reachable in a live application.

    acted = [0]

    def _doing(phase: str, **data) -> None:
        acted[0] += 1
        seq = acted[0]
        app_shell.notify("do_%s" % phase, seq=seq, **data)
        try:
            server.push("doing", phase=phase, seq=seq,
                        line=data.get("line", ""),
                        fault=doing.is_fault(phase),
                        index=data.get("index"), total=data.get("total"))
        except Exception:
            pass

    try:
        from skills import do_engine
    except Exception:
        do_engine = None

    if do_engine is not None:
        moved = do_engine.Task.to

        def reported(self, state, detail=""):
            before = self.state
            moved(self, state, detail)
            if self.state is before:
                return
            try:
                phase = doing.phase_for(self.state)
                steps = getattr(self, "steps", None)
                intent = doing.Intent()
                if steps:
                    last = steps[-1]
                    intent = doing.Intent(
                        verb=str(getattr(last, "verb", "") or "").title(),
                        target=str(getattr(last, "target", "") or ""))
                _doing(phase, task_id=self.id,
                       line=doing.line_for(phase, intent, note=detail or ""),
                       note=detail or "")
            except Exception:
                pass

        do_engine.Task.to = reported

    # Answering the question, from the window. Only ever resolves a request
    # the backend made; it cannot begin one.
    def _consent(allow: bool) -> bool:
        shell = getattr(app_shell, "_SHELL", None)
        if shell is None or shell.doing.phase != doing.ASKING:
            return False
        _on_main(shell.do_granted if allow else shell.do_denied)
        return True

    server.ON_CONSENT = _consent

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
