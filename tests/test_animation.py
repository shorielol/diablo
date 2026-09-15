"""
The animation follows reality, or it is lying.

Every check here is about one rule: Diablo must not look like it is doing
something before it is doing it, and must stop looking like it the moment it
stops. The bug this suite was written after was measured, not guessed —
VoiceSubsystem.say() entered RESPONDING before voice.speak() was called, and
on this machine that was 2,576 milliseconds of a moving mouth in silence while
Voicebox synthesised the line.

These are state-machine tests, not animation tests. Asserting that an
animation exists proves nothing; asserting that it cannot begin before the
audio-start event is the whole point.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

import companion_manager as cm  # noqa: E402
from companion_manager import DiabloState, TRANSITIONS  # noqa: E402
from skills import voice  # noqa: E402


class Recorder:
    """A manager that only remembers where it was told to go."""

    def __init__(self):
        self.moves = []

    def transition(self, state, reason=""):
        self.moves.append(state.value)
        return True

    @property
    def last(self):
        return self.moves[-1] if self.moves else None


def fresh_voice_subsystem():
    """A VoiceSubsystem wired to a recorder, with no real audio anywhere."""
    voice.forget_listeners()
    subsystem = cm.VoiceSubsystem.__new__(cm.VoiceSubsystem)
    subsystem.manager = Recorder()
    subsystem.module = voice
    subsystem._generation = 0
    subsystem._active = 0
    subsystem._listening_registered = False
    subsystem.is_speaking = False
    subsystem.voice_route = None
    subsystem._register(voice)
    return subsystem


def run():
    r = Results()

    # ── The state machine ───────────────────────────────────────────────────
    r.head("The states exist and connect")
    with guard(r, "preparing is a real state"):
        r.check("there is a state between thinking and speaking",
                hasattr(DiabloState, "PREPARING"),
                "the gap between an answer existing and a sound starting")
        r.check("every state has somewhere to go",
                all(s in TRANSITIONS for s in DiabloState),
                ", ".join(s.value for s in DiabloState if s not in TRANSITIONS))
        r.check("thinking can reach preparing",
                DiabloState.PREPARING in TRANSITIONS[DiabloState.PROCESSING])
        r.check("preparing can reach speaking",
                DiabloState.RESPONDING in TRANSITIONS[DiabloState.PREPARING])
        r.check("preparing can also be abandoned",
                {DiabloState.IDLE, DiabloState.LISTENING}
                <= TRANSITIONS[DiabloState.PREPARING],
                "synthesis fails, and users interrupt in the half second before "
                "the sound starts")

    # ── The rule ────────────────────────────────────────────────────────────
    r.head("Speaking cannot begin before the sound does")
    with guard(r, "say() does not claim to be speaking"):
        import inspect
        source = inspect.getsource(cm.VoiceSubsystem.say)
        r.check("say() enters preparing, not speaking",
                "DiabloState.PREPARING" in source
                and "DiabloState.RESPONDING" not in source,
                "RESPONDING belongs to the audio callback and nowhere else")
        whole = inspect.getsource(cm.VoiceSubsystem)
        r.check("exactly one place enters the speaking state",
                whole.count("DiabloState.RESPONDING") == 1,
                f"{whole.count('DiabloState.RESPONDING')} places")

    with guard(r, "the speaker drives the state"):
        v = fresh_voice_subsystem()
        v._generation = v._active = 1
        r.check("nothing is speaking before the audio event",
                "responding" not in v.manager.moves, str(v.manager.moves))
        voice._fire("start")
        r.check("the audio event is what enters speaking",
                v.manager.last == "responding", str(v.manager.moves))

    with guard(r, "the ordering is preparing then speaking"):
        v = fresh_voice_subsystem()
        v._generation += 1
        v._active = v._generation
        v.manager.transition(DiabloState.PREPARING)
        voice._fire("start")
        r.check("preparing comes before speaking, never after",
                v.manager.moves == ["preparing", "responding"],
                str(v.manager.moves))

    # ── Interruption and stale events ───────────────────────────────────────
    r.head("A superseded reply cannot move the state")
    with guard(r, "an interruption makes later callbacks stale"):
        v = fresh_voice_subsystem()
        v._generation = v._active = 1
        v.interrupt()
        before = list(v.manager.moves)
        voice._fire("start")
        r.check("audio from an interrupted reply does not enter speaking",
                v.manager.moves == before,
                "a late callback from a cancelled utterance must change nothing")
        r.check("and the discard is recorded, not silent",
                any(e["event"] == "stale_audio_start" for e in voice.timeline(10)),
                "a dropped event nobody can see is indistinguishable from a bug")

    with guard(r, "a newer question wins"):
        v = fresh_voice_subsystem()
        v._generation = v._active = 1          # reply A is speaking
        v._generation = 2                      # question B has arrived
        before = list(v.manager.moves)
        voice._fire("start")
        r.check("reply A's audio event cannot speak over question B",
                v.manager.moves == before, str(v.manager.moves))
        v._active = 2                          # B is now the one being spoken
        voice._fire("start")
        r.check("and B's own audio event still works",
                v.manager.last == "responding", str(v.manager.moves))

    with guard(r, "interrupting bumps the generation"):
        v = fresh_voice_subsystem()
        v._generation = v._active = 3
        v.interrupt()
        r.check("the generation moves past the active utterance",
                v._generation > v._active, f"{v._active} -> {v._generation}")

    # ── The listener mechanism ──────────────────────────────────────────────
    r.head("One slot, two writers, was a race")
    with guard(r, "several listeners can register"):
        voice.forget_listeners()
        heard = []
        voice.listen(lambda: heard.append("a"), None)
        voice.listen(lambda: heard.append("b"), None)
        voice._fire("start")
        r.check("every listener hears the event", heard == ["a", "b"],
                "assigning voice.on_audio_start meant whoever imported last won")

    with guard(r, "one broken listener cannot silence the others"):
        voice.forget_listeners()
        heard = []

        def explodes():
            raise RuntimeError("no")

        voice.listen(explodes, None)
        voice.listen(lambda: heard.append("still ran"), None)
        voice._fire("start")
        r.check("a listener that raises does not stop the next one",
                heard == ["still ran"])

    with guard(r, "the legacy single callback still fires"):
        voice.forget_listeners()
        fired = []
        voice.on_audio_start = lambda: fired.append("legacy")
        try:
            voice._fire("start")
            r.check("voice_bridge's existing assignment keeps working",
                    fired == ["legacy"],
                    "the wake-word gate rides on this and must not break")
        finally:
            voice.on_audio_start = None

    # ── The timeline ────────────────────────────────────────────────────────
    r.head("The timeline")
    with guard(r, "events are timestamped"):
        voice.forget_listeners()
        voice._timeline.clear()
        voice.mark("say_requested", utterance=1)
        voice._fire("start")
        voice._fire("end")
        events = [e["event"] for e in voice.timeline(10)]
        r.check("a whole utterance is visible in order",
                events == ["say_requested", "audio_started", "audio_ended"],
                str(events))
        r.check("each carries a wall-clock time",
                all(isinstance(e["at"], float) and e["at"] > 0
                    for e in voice.timeline(10)))
        r.check("detail passed to mark() survives",
                voice.timeline(10)[0].get("utterance") == 1)

    with guard(r, "the timeline cannot grow without bound"):
        voice._timeline.clear()
        for i in range(900):
            voice.mark("noise", i=i)
        r.check("it is a ring buffer, not a leak", len(voice._timeline) <= 400,
                f"{len(voice._timeline)} entries after 900 marks")

    with guard(r, "tracing is off unless asked for"):
        r.check("the timeline does not print in normal use",
                voice._TRACE == (os.getenv("DIABLO_ANIM_TRACE") == "1"),
                "DIABLO_ANIM_TRACE=1 turns it on")

    # ── Restraint ───────────────────────────────────────────────────────────
    r.head("Idle costs nothing")
    with guard(r, "only the states that need it animate"):
        import app_shell
        view = app_shell.OrbView.alloc().initWithFrame_(
            ((0, 0), (app_shell.CANVAS, app_shell.CANVAS)))
        for state, should in [("idle", False), ("cold", False),
                              ("preparing", False), ("listening", True),
                              ("processing", True), ("responding", True)]:
            view.update_({"state": state})
            if view.animating() != should:
                r.bad("the animation loop runs only where it is needed",
                      f"{state} animating={view.animating()}, expected {should}")
                break
        else:
            r.ok("the animation loop runs only where it is needed",
                 "idle and preparing hold still; the 24fps loop does not run")

        view.update_({"state": "preparing"})
        r.check("preparing is drawn in the thinking colour, not the speaking one",
                view.accent == app_shell.GOLD,
                "it is still 'working on it' to the user; what matters is that "
                "it is not the speaking colour")

    return r.report("animation")


if __name__ == "__main__":
    sys.exit(run())
