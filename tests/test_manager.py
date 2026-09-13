#!/usr/bin/env python3
"""
The state machine, the event system, and what happens when a subsystem is gone.

The manager's whole job is to be the one place that knows what Diablo is doing,
which means its failure mode is subtle: if it reports IDLE while the pipeline is
stuck, or drops an event a UI is bound to, nothing crashes and nothing is
visibly wrong. So most of this file checks that it tells the truth under
conditions that are awkward rather than broken — a listener that raises, a
subsystem that will not start, two threads reading at once, a transition that
should never have been attempted.

The degradation tests matter most. Diablo is supposed to keep listening when
Ollama stops, and the only way to know it does is to stop Ollama.
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard, Timer  # noqa: E402

sys.path.insert(0, os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))
from companion_manager import (  # noqa: E402
    CompanionManager, DiabloState, TRANSITIONS, create_manager,
)

r = Results()


def run():
    # ── the state machine ───────────────────────────────────────────────────
    r.head("State machine")
    m = CompanionManager()

    r.check("starts cold", m.state is DiabloState.COLD, m.state.value)

    with guard(r, "legal transitions are taken"):
        r.check("cold -> initializing", m.transition(DiabloState.INITIALIZING))
        r.check("initializing -> idle", m.transition(DiabloState.IDLE))
        r.check("idle -> listening", m.transition(DiabloState.LISTENING))
        r.check("listening -> processing", m.transition(DiabloState.PROCESSING))
        r.check("processing -> responding", m.transition(DiabloState.RESPONDING))
        r.check("responding -> idle", m.transition(DiabloState.IDLE))

    with guard(r, "illegal transitions are refused, not raised"):
        m.transition(DiabloState.IDLE)
        before = m.state
        r.check("idle -> cold is refused", not m.transition(DiabloState.COLD))
        r.check("and the state did not move", m.state is before, m.state.value)

    with guard(r, "every state is reachable and every edge is declared"):
        declared = set(TRANSITIONS)
        r.check("all states have a transition rule",
                declared == set(DiabloState), sorted(s.value for s in set(DiabloState) - declared))
        reachable = set()
        for targets in TRANSITIONS.values():
            reachable |= targets
        unreachable = set(DiabloState) - reachable - {DiabloState.COLD}
        r.check("no state is stranded", not unreachable,
                ", ".join(s.value for s in unreachable) or "all reachable")

    with guard(r, "a transition to the current state is a no-op, not a refusal"):
        m.transition(DiabloState.IDLE)
        r.check("idle -> idle returns True", m.transition(DiabloState.IDLE))

    with guard(r, "error is recoverable"):
        m.transition(DiabloState.ERROR)
        r.check("error -> idle is allowed", m.transition(DiabloState.IDLE))

    with guard(r, "history is kept and bounded"):
        for _ in range(150):
            m.transition(DiabloState.LISTENING)
            m.transition(DiabloState.IDLE)
        r.check("history does not grow without limit",
                len(m._state_history) <= 200, f"{len(m._state_history)} entries")

    # ── the wake gate ───────────────────────────────────────────────────────
    r.head("The self-wake gate")
    with guard(r, "speaking deafens the wake word"):
        from skills import wake_word
        m.transition(DiabloState.IDLE)
        m.transition(DiabloState.RESPONDING)
        r.check("SPEAKING is set while responding", wake_word.SPEAKING.is_set(),
                "without this Diablo hears its own name and wakes itself")
        m.transition(DiabloState.IDLE)
        r.check("and cleared when it stops", not wake_word.SPEAKING.is_set())

    # ── events ──────────────────────────────────────────────────────────────
    r.head("Event system")
    m2 = CompanionManager()

    with guard(r, "subscribe and receive"):
        got = []
        listener = m2.on("state_changed", lambda p: got.append(p))
        m2.transition(DiabloState.INITIALIZING)
        r.check("the listener fired", len(got) == 1, f"{len(got)} events")
        r.check("with old and new state",
                got and got[0].get("old_state") == "cold"
                and got[0].get("new_state") == "init", str(got[:1])[:70])
        r.check("and a timestamp", got and isinstance(got[0].get("ts"), float))

    with guard(r, "unsubscribe stops delivery"):
        m2.off(listener)
        count = len(got)
        m2.transition(DiabloState.IDLE)
        r.check("no further events after off()", len(got) == count, f"{len(got)}")

    with guard(r, "off() on an unknown id is harmless"):
        m2.off("not-a-real-listener")
        r.ok("unknown listener id ignored")

    with guard(r, "wildcard hears everything"):
        everything = []
        m2.on("*", lambda p: everything.append(p["event"]))
        m2.transition(DiabloState.LISTENING)
        m2.emit("custom_event", value=1)
        r.check("wildcard caught both", len(everything) >= 2,
                ", ".join(everything))

    with guard(r, "a listener that raises does not break the others"):
        survived = []
        m2.on("boom", lambda p: (_ for _ in ()).throw(RuntimeError("listener is broken")))
        m2.on("boom", lambda p: survived.append(p))
        m2.emit("boom")
        r.check("the second listener still ran", len(survived) == 1,
                "a bad UI callback cannot take the assistant down")

    with guard(r, "a listener may unsubscribe itself from inside its callback"):
        ids = {}
        def once(payload):
            m2.off(ids["id"])
        ids["id"] = m2.on("reentrant", once)
        with Timer() as t:
            m2.emit("reentrant")
        r.check("no deadlock", t.elapsed < 2.0, f"{t.elapsed:.2f}s")

    # ── threads ─────────────────────────────────────────────────────────────
    r.head("Thread safety")
    with guard(r, "concurrent readers and one writer"):
        m3 = CompanionManager()
        m3.transition(DiabloState.INITIALIZING)
        m3.transition(DiabloState.IDLE)
        errors = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    _ = m3.state
                    _ = m3.health()
            except BaseException as e:
                errors.append(f"{type(e).__name__}: {e}")

        def writer():
            try:
                for _ in range(40):
                    m3.transition(DiabloState.LISTENING)
                    m3.transition(DiabloState.IDLE)
            except BaseException as e:
                errors.append(f"{type(e).__name__}: {e}")

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
        for thread in readers:
            thread.start()
        w = threading.Thread(target=writer)
        w.start(); w.join(timeout=60)
        stop.set()
        for thread in readers:
            thread.join(timeout=5)
        r.check("no thread raised", not errors, "; ".join(errors[:2]) or "4 readers + 1 writer clean")

    # ── lifecycle ───────────────────────────────────────────────────────────
    r.head("Lifecycle")
    m4 = create_manager()

    with guard(r, "create_manager is a singleton"):
        r.check("same object twice", create_manager() is m4,
                "two managers would fight over the microphone")

    with guard(r, "start brings it to idle"):
        with Timer() as t:
            started = m4.start()
        r.check("start() returned", isinstance(started, bool), f"{t.elapsed:.1f}s")
        r.check("reached idle or reported why not",
                m4.state in (DiabloState.IDLE, DiabloState.ERROR), m4.state.value)
        if m4.state is DiabloState.ERROR:
            r.check("and named the blocker", bool(m4._last_error), m4._last_error)

    with guard(r, "health has the shape a UI can bind to"):
        health = m4.health()
        for key in ("state", "uptime", "degraded", "subsystems", "permissions",
                    "recent_transitions"):
            r.check(f"health has {key!r}", key in health)
        r.check("every subsystem reports",
                set(health["subsystems"]) == {"brain", "voice", "mic", "wake_word",
                                              "screen", "conversation"},
                ", ".join(sorted(health["subsystems"])))
        r.check("each has a ready flag",
                all("ready" in d for d in health["subsystems"].values()))

    with guard(r, "permissions are three booleans"):
        perms = m4.check_permissions()
        r.check("mic, screen, accessibility", set(perms) == {"mic", "screen", "accessibility"},
                str(perms))
        r.check("all booleans", all(isinstance(v, bool) for v in perms.values()))

    with guard(r, "config exposes the DIABLO_ knobs"):
        config = m4.config
        r.check("model is in there", any("MODEL" in k for k in config),
                ", ".join(list(config)[:4]))
        # The snapshot is everything from .env plus the DIABLO_ variables, and
        # .env carries some legacy names (TTS_VOICE, PROJECT_ROOT) that are
        # genuinely Diablo's settings. What must never appear is the rest of
        # the process environment — a config panel that renders this should not
        # be able to show someone's PATH or an unrelated token.
        leaked = [k for k in config
                  if k in ("PATH", "HOME", "USER", "SHELL", "PWD", "LANG",
                           "TERM", "SSH_AUTH_SOCK")
                  or "SECRET" in k or "TOKEN" in k or "PASSWORD" in k]
        r.check("no unrelated environment leaks into config", not leaked,
                ", ".join(leaked) or f"{len(config)} settings, all Diablo's")
        r.check("and it says where they came from", "_env_file" in config,
                config.get("_env_file"))

    with guard(r, "stop returns it to cold"):
        m4.stop()
        r.check("cold after stop", m4.state is DiabloState.COLD, m4.state.value)

    with guard(r, "restart works"):
        m4.restart()
        r.check("idle or error, not stuck",
                m4.state in (DiabloState.IDLE, DiabloState.ERROR), m4.state.value)

    # ── degradation ─────────────────────────────────────────────────────────
    r.head("It degrades rather than failing")
    with guard(r, "a missing brain does not stop it listening"):
        from skills import local_brain
        was = local_brain.OLLAMA_HOST
        local_brain.OLLAMA_HOST = "http://127.0.0.1:9"
        try:
            m5 = CompanionManager()
            started = m5.start()
            r.check("it still reached idle", m5.state is DiabloState.IDLE, m5.state.value)
            r.check("and says the brain is degraded", "brain" in m5.health()["degraded"],
                    ", ".join(m5.health()["degraded"]))
            m5.stop()
        finally:
            local_brain.OLLAMA_HOST = was

    with guard(r, "a required subsystem failing is an error, not a crash"):
        m6 = CompanionManager()
        m6.mic.start = lambda: False        # pretend there is no microphone
        m6.mic.required = True
        started = m6.start()
        r.check("start() returns False", started is False)
        r.check("state is error", m6.state is DiabloState.ERROR, m6.state.value)
        r.check("and it named the blocker", "mic" in (m6._last_error or ""),
                m6._last_error)
        m6.stop()

    with guard(r, "recover() is tried before a restart"):
        m7 = CompanionManager()
        m7.transition(DiabloState.INITIALIZING)
        m7.transition(DiabloState.ERROR)
        m7.recover()
        r.check("recover moved it or left it in error",
                m7.state in (DiabloState.IDLE, DiabloState.ERROR), m7.state.value)

    with guard(r, "handle_utterance always returns to idle"):
        m8 = CompanionManager()
        m8.transition(DiabloState.INITIALIZING)
        m8.transition(DiabloState.IDLE)
        # Force the pipeline to explode; the finally clause must still run.
        import skills.delegation_tool as dt
        original = dt.handle
        dt.handle = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pipeline exploded"))
        try:
            m8.voice.say = lambda text: "suppressed"
            m8.handle_utterance("do something impossible")
            r.check("back to idle after a crash mid-pipeline",
                    m8.state is DiabloState.IDLE, m8.state.value)
        finally:
            dt.handle = original

    with guard(r, "an empty utterance is ignored"):
        m9 = CompanionManager()
        m9.transition(DiabloState.INITIALIZING)
        m9.transition(DiabloState.IDLE)
        r.check("empty string returns nothing", m9.handle_utterance("") == "")
        r.check("and the state did not move", m9.state is DiabloState.IDLE)

    return r.report("companion manager")


if __name__ == "__main__":
    sys.exit(run())
