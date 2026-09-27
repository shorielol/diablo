#!/usr/bin/env python3
"""
Phase C — the fast path: decide cheaply, answer early, never skip a gate.

Measured before this existed: "What is supply chain management?" was 7.4 s
from release to the first word. 1.8 s went on a model call to learn it was a
question, 1.7 s on waiting for the whole answer, 3.0 s on synthesising a
35-word first sentence before any of it could play.

These tests pin down the contract that fixed that, with fakes for the model,
the synthesiser and the speaker, so nothing here needs Ollama, Voicebox, a
network or a microphone:

  - the decision engine is Diablo's, bounded, advisory, and never raises;
  - INSTANT and FAST turns skip the router and touch nothing on the screen;
  - anything about the screen, an action, or a tool still reaches the router;
  - replies stream: the first chunk is heard while the rest is being written;
  - the guards on a whole reply hold chunk by chunk;
  - a superseded turn stops talking and stops generating;
  - the microphone is still the push-to-talk worker's alone.
"""

import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

TRACE = os.path.join(tempfile.mkdtemp(prefix="diablo-fastpath-"), "trace.jsonl")
os.environ["DIABLO_TRACE"] = TRACE

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

import voice_bridge  # noqa: E402
from skills import (agent_core, computer_use, conversation, decision,  # noqa: E402
                    delegation_tool, local_brain, route, speech, trace, voice)

voice_bridge.set_ui_state = lambda state: None


class Patch:
    """Replace attributes for the length of a block, then put them back."""

    def __init__(self):
        self.saved = []

    def __call__(self, owner, name, value):
        self.saved.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for owner, name, value in reversed(self.saved):
            setattr(owner, name, value)


def tokens_of(text, delay=0.0, record=None):
    """A fake model stream: the text, a few characters at a time."""
    def stream(*a, **k):
        closed = {"closed": False}
        if record is not None:
            record.append(closed)
        try:
            for i in range(0, len(text), 6):
                if delay:
                    time.sleep(delay)
                yield text[i:i + 6]
        finally:
            closed["closed"] = True
    return stream


class Speaker:
    """Stands in for synthesis and playback. Records when each clip plays."""

    def __init__(self, play_seconds=0.0):
        self.played, self.synth = [], []
        self.play_seconds = play_seconds
        self.t0 = time.perf_counter()

    def install(self, p):
        p(voice, "cached_path", lambda text: None)
        p(voice, "synthesize", self._synth)
        p(voice, "repace", lambda path, text, wpm: path)
        p(voice, "_play", self._play)
        p(voice, "_say_locally", lambda text, pace=None, generation=None: "played")
        return self

    def _synth(self, text, force=False):
        self.synth.append(text)
        return f"/tmp/fake-{len(self.synth)}.wav"

    def _play(self, path, generation=None):
        if not voice.is_current_generation(generation):
            return "stale"
        self.played.append((time.perf_counter() - self.t0, path))
        if self.play_seconds:
            time.sleep(self.play_seconds)
        return "played"


def interaction_speaker(generation):
    """voice_bridge.speak, bound to one interaction, as process_command binds it."""
    def speak(message, **kwargs):
        kwargs.setdefault("generation", generation)
        return voice_bridge.speak(message, **kwargs)
    speak.streams = True
    return speak


def forbid(p, owner, names, log):
    for name in names:
        if hasattr(owner, name):
            p(owner, name, (lambda n: (lambda *a, **k: log.append(n) or ""))(name))


def routed(seen, action):
    def classify(text, **k):
        seen.append("classify")
        return SimpleNamespace(route=action, goal=text, question="", show=None,
                               confidence=0.9, reason="", kind="", risk="low",
                               operations=[])
    return classify


CORPUS = [
    # (utterance, lane or None; None means "not decided here": the router's)
    ("Hello Diablo.", decision.INSTANT),
    ("Thanks.", decision.INSTANT),
    ("How are you?", decision.INSTANT),
    ("What is 2 + 2?", decision.INSTANT),
    ("What is two plus two?", decision.INSTANT),
    ("What's twenty five times four?", decision.INSTANT),
    ("What is GDP?", decision.FAST),
    ("Explain inflation simply.", decision.FAST),
    ("What is supply chain management?", decision.FAST),
    ("Why is the sky blue?", decision.FAST),
    ("How does compound interest work?", decision.AGENT),   # the tutor's walkthrough
    ("What's the difference between a recession and a depression?", decision.FAST),
    ("How's it going?", decision.FAST),
    ("I'm knackered.", decision.FAST),
    ("Explain this graph.", decision.AGENT),
    ("What am I looking at?", decision.AGENT),
    ("Where is File?", decision.AGENT),
    ("Click File.", decision.AGENT),
    ("Open Chrome.", decision.AGENT),
    ("Research this company.", decision.AGENT),
    ("Plan a business strategy.", decision.AGENT),
    ("Start a mission.", decision.AGENT),
    ("Delete all my files.", decision.AGENT),
    ("Where is the welfare loss region?", decision.AGENT),
    ("Show me how the subsidy causes welfare loss.", decision.AGENT),
    ("What is the", decision.AGENT),
    ("Can you open the", decision.AGENT),
    ("Do it.", decision.AGENT),
    ("do the thing", None),
    ("Do dogs dream?", decision.FAST),
    ("What's the weather today?", None),
    ("What's on my calendar?", None),
    ("What's the latest news about Apple?", None),
    ("Who won the match yesterday?", None),
    ("What was that file called again?", None),
    ("And the second one?", None),
    ("Asmo, what do you think?", None),
    ("My extended essay deadline moved to Friday.", None),
    ("How do I export this as a PDF?", decision.AGENT),
]


def run() -> int:
    r = Results()

    # ── B. The contract ──────────────────────────────────────────────────
    r.head("B  the decision contract")
    with guard(r, "bounded"):
        v = decision.decide("What is GDP?")
        r.check("a verdict for a plain question", v is not None and v.lane == decision.FAST,
                str(v))
        r.check("  its lane and intent are from the bounded sets",
                v.lane in decision.LANES and v.intent in decision.INTENTS)
        r.check("  it carries no natural-language answer for a FAST turn", v.reply == "")
        r.check("nothing said: no opinion", decision.decide("") is None)
    with guard(r, "never raises"):
        class Broken(decision.Engine):
            name = "broken"

            def decide(self, text, in_conversation=False):
                raise RuntimeError("down")
        with Patch() as p:
            p(decision, "_engine", Broken())
            r.check("a failing engine is None, not an exception",
                    decision.decide("What is GDP?") is None)
    with guard(r, "advisory only"):
        source = open(decision.__file__).read()
        r.check("the decision module cannot reach tools, the computer or a permission gate",
                not any(name in source for name in (
                    "agent_tools.run", "computer_use", "confirm(", "execute(",
                    "subprocess", "CONTEXT[")))

    # ── E. The corpus ────────────────────────────────────────────────────
    r.head("E  routing corpus (rules)")
    with guard(r, "corpus"):
        false_fast, wrong = [], []
        for utterance, want in CORPUS:
            v = decision.decide(utterance)
            got = v.lane if v else None
            fast = got in (decision.INSTANT, decision.FAST)
            if fast and want not in (decision.INSTANT, decision.FAST):
                false_fast.append((utterance, got))
            if want in (decision.INSTANT, decision.FAST) and got != want:
                wrong.append((utterance, got, want))
            if want == decision.AGENT and got not in (decision.AGENT, None):
                wrong.append((utterance, got, want))
        r.check("no screen, tool, action or ambiguous request is put on a fast lane",
                not false_fast, str(false_fast))
        r.check("every simple request lands on its lane", not wrong, str(wrong))
        v = decision.decide("Explain inflation simply.")
        r.check("'explain inflation' is fast only once the screen is checked (B.3)",
                v is not None and v.lane == decision.FAST and v.needs_screen, str(v))
        v = decision.decide("What is the")
        r.check("an utterance cut off mid-thought is marked incomplete",
                v is not None and v.complete is False)
        v = decision.decide("What is two plus two?")
        r.check("arithmetic is exact and needs no model", v is not None and "4" in v.reply,
                str(v))

    # ── A. Instrumentation ───────────────────────────────────────────────
    r.head("A  instrumentation")
    with guard(r, "trace"):
        open(TRACE, "w").close()
        trace.new_turn("test")
        decision.decide("What is supply chain management?")
        records = trace.read(limit=5)
        decided = [x for x in records if x.get("stage") == "decide"]
        r.check("each decision is traced with its lane, engine and time",
                bool(decided) and decided[-1].get("lane") == "fast"
                and decided[-1].get("engine") == "rules"
                and isinstance(decided[-1].get("ms"), (int, float)), str(decided[-1:]))
        r.check("  and with no words of what was said",
                bool(decided) and "supply" not in str(decided[-1]).lower())

    # ── C. Laya adapter ──────────────────────────────────────────────────
    r.head("C  the Laya adapter, behind the same interface")
    with guard(r, "adapter"):
        class Router:
            def __init__(self, answers):
                self.answers, self.calls, self.questions = answers, 0, {}

            def predict(self, text, questions):
                self.calls += 1
                self.questions = questions
                return {"answers": self.answers}
        factual = Router({"intent": {"choice": "factual"}, "needs_screen": {"noul": 0.1},
                          "complete": {"noul": 0.9}})
        v = decision.Laya(router=factual).decide("What is GDP?")
        r.check("one predict call answers every question in the contract", factual.calls == 1
                and set(factual.questions) == set(decision.LAYA_QUESTIONS))
        r.check("  a factual question with no screen and no tool is FAST",
                v is not None and v.lane == decision.FAST and v.engine == "laya", str(v))
        screen = Router({"intent": {"choice": "factual"}, "needs_screen": {"noul": 0.8},
                         "complete": {"noul": 0.9}})
        v = decision.Laya(router=screen).decide("What does this say?")
        r.check("  a question that needs the screen is not FAST", v.lane != decision.FAST)
        doing = Router({"intent": {"choice": "do"}, "needs_screen": {"noul": 0.0},
                        "complete": {"noul": 1.0}})
        r.check("  an action is AGENT",
                decision.Laya(router=doing).decide("Open Chrome").lane == decision.AGENT)
        odd = Router({"intent": {"choice": "banana"}, "needs_screen": {"noul": 0.1},
                      "complete": {"noul": 0.9}})
        r.check("  an answer outside the bounded set is UNKNOWN, not trusted",
                decision.Laya(router=odd).decide("x").intent == decision.UNKNOWN)
        partial = Router({"intent": {"choice": "factual"}})
        r.check("  an answer missing a question is no opinion, not a guess",
                decision.Laya(router=partial).decide("x") is None)
    with guard(r, "not installed"):
        had = "laya" in sys.modules
        if not had:
            r.check("with no Laya package, the adapter has no opinion",
                    decision.Laya().decide("What is GDP?") is None)
        r.check("the rules engine is the one in use", decision.engine().name == "rules")
    with guard(r, "invisible"):
        for module in (delegation_tool, conversation, voice, voice_bridge, speech):
            r.check(f"{module.__name__} never names it",
                    "laya" not in open(module.__file__).read().lower())

    # ── G, H. The fast lane ──────────────────────────────────────────────
    r.head("G-H  the fast lane skips the router and the screen")
    answer = ("Gross domestic product is the total value of goods and services a "
              "country produces in a year. It is the usual measure of the size of "
              "an economy.")
    with guard(r, "fast"):
        calls = []
        with Patch() as p:
            Speaker().install(p)
            p(local_brain, "available", lambda: True)
            p(local_brain, "chat_stream", tokens_of(answer))
            p(local_brain, "chat", lambda *a, **k: calls.append("chat") or {"content": "x"})
            p(route, "classify", lambda *a, **k: calls.append("classify"))
            p(agent_core, "understand", lambda *a, **k: calls.append("understand"))
            from skills import perception, pointing, screen_context, showing, tutor
            forbid(p, screen_context, ["capture_window", "capture_screen", "look",
                                       "structured", "explain", "describe_visually",
                                       "ocr_image", "read_image"], calls)
            forbid(p, perception, ["read"], calls)
            forbid(p, showing, ["prepare", "present"], calls)
            forbid(p, tutor, ["walk"], calls)
            forbid(p, pointing, ["point_at", "locate"], calls)
            forbid(p, agent_core, ["run"], calls)
            conversation.session.reset()
            generation = voice.begin_interaction()
            outcome = delegation_tool.handle("What is GDP?", interaction_speaker(generation),
                                             generation=generation)
            said = [t["content"] for t in conversation.session.snapshot()["turns"]]
        r.check("answered as conversation", outcome == "chat", outcome)
        r.check("no router, no understand(), no second model call",
                not any(c in calls for c in ("classify", "understand", "chat")), str(calls))
        r.check("no screen capture, OCR, vision, guidance, tutor or agent",
                not [c for c in calls if c not in ("classify", "understand", "chat")],
                str(calls))
        r.check("the turn is remembered as it was said",
                len(said) == 2 and said[1].startswith("Gross domestic product"), str(said))
        chain = [(x.get("stage"), x.get("lane") or x.get("route") or x.get("phase"))
                 for x in trace.read(limit=40)
                 if x.get("turn") == trace.current_turn()]
        r.check("one turn traces its whole chain: decided, fast, first chunk, complete",
                all(step in chain for step in (("decide", "fast"), ("route", "fast"),
                                                ("speech", "first_chunk"),
                                                ("speech", "complete"))), str(chain))
    with guard(r, "instant"):
        with Patch() as p:
            sp = Speaker().install(p)
            p(local_brain, "available", lambda: True)
            p(route, "classify", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
            conversation.session.reset()
            generation = voice.begin_interaction()
            delegation_tool.handle("What is two plus two?", interaction_speaker(generation),
                                   generation=generation)
        r.check("a sum is answered exactly, with no model", sp.synth == ["That's 4, Sir."],
                str(sp.synth))

    with guard(r, "screen first"):
        for shown_result, expect_chat in (("shown", False), (None, True)):
            seen = []
            with Patch() as p:
                Speaker().install(p)
                p(local_brain, "available", lambda: True)
                p(local_brain, "chat_stream", tokens_of("Prices rise over time. That is all."))
                p(route, "classify", lambda *a, **k: seen.append("classify"))
                p(delegation_tool, "_show", lambda text, asked, *a, **k:
                  seen.append(("show", getattr(asked.show, "intent", None))) or shown_result)
                conversation.session.reset()
                generation = voice.begin_interaction()
                outcome = delegation_tool.handle("Explain inflation simply.",
                                                 interaction_speaker(generation),
                                                 generation=generation)
            r.check(f"the screen is checked before answering ({shown_result or 'not there'})",
                    seen[:1] == [("show", "explain")] and "classify" not in seen, str(seen))
            r.check("  shown when it is on screen, answered aloud when it is not",
                    (outcome == "chat") == expect_chat, outcome)

    # ── I. What must still reach the router ──────────────────────────────
    r.head("I  screen, guidance and actions still reach the router")
    with guard(r, "router"):
        for utterance in ("Explain this graph.", "What am I looking at?", "Where is File?",
                          "Click File.", "Open Chrome.", "Research this company."):
            seen = []
            with Patch() as p:
                p(local_brain, "available", lambda: True)
                p(route, "classify", routed(seen, "act"))
                p(delegation_tool, "_show", lambda *a, **k: seen.append("show") or "shown")
                p(conversation, "talk_stream", lambda *a, **k: seen.append("fast") or iter(()))
                conversation.session.reset()
                delegation_tool.handle(utterance, lambda *a, **k: None)
            r.check(f"{utterance!r} goes through the router, not the fast lane",
                    seen[:1] == ["classify"] and "fast" not in seen, str(seen))

    # ── D. Fallback ──────────────────────────────────────────────────────
    r.head("D  fallback")
    with guard(r, "no verdict"):
        seen = []
        with Patch() as p:
            p(local_brain, "available", lambda: True)
            p(decision, "decide", lambda *a, **k: None)
            p(route, "classify", routed(seen, "chat"))
            p(delegation_tool, "_show", lambda *a, **k: None)
            p(conversation, "talk", lambda text, **k: seen.append("talk") or "fine")
            conversation.session.reset()
            delegation_tool.handle("What is GDP?", lambda *a, **k: None)
        r.check("no verdict: the existing router answers, as before",
                seen == ["classify", "talk"], str(seen))
    with guard(r, "stream breaks before a word"):
        with Patch() as p:
            sp = Speaker().install(p)

            def broken(*a, **k):
                raise local_brain.BrainOffline("down")
                yield  # noqa: unreachable, makes this a generator
            p(local_brain, "available", lambda: True)
            p(local_brain, "chat_stream", broken)
            conversation.session.reset()
            generation = voice.begin_interaction()
            delegation_tool.handle("What is GDP?", interaction_speaker(generation),
                                   generation=generation)
        r.check("the model's failure is said plainly, once",
                sp.synth == [local_brain.BrainOffline.SPOKEN], str(sp.synth))
    with guard(r, "a voice that cannot stream"):
        got = []
        with Patch() as p:
            p(local_brain, "available", lambda: True)
            p(conversation, "talk", lambda text, **k: "A whole answer.")
            conversation.session.reset()
            delegation_tool.handle("What is GDP?", lambda text, **k: got.append(text))
        r.check("gets the finished string, as it always did", got == ["A whole answer."],
                str(got))

    # ── K. Streaming generation ──────────────────────────────────────────
    r.head("K  streaming generation")
    with guard(r, "early"):
        stamps, record = [], []
        long_answer = ("Inflation is a general rise in prices over time. " * 3).strip()
        with Patch() as p:
            p(local_brain, "chat_stream", tokens_of(long_answer, delay=0.01, record=record))
            conversation.session.reset()
            t0 = time.perf_counter()
            for chunk in conversation.session.reply_stream("Explain inflation simply."):
                stamps.append((time.perf_counter() - t0, chunk))
        total = stamps[-1][0] if stamps else 0
        r.check("the first chunk arrives before the answer is finished",
                len(stamps) >= 2 and stamps[0][0] < total * 0.6, str([s[0] for s in stamps]))
        r.check("the model stream is closed when the reply ends",
                bool(record) and record[0]["closed"])
    with guard(r, "guards per chunk"):
        with Patch() as p:
            p(local_brain, "chat_stream", tokens_of(
                "Brew and Critics is doing really well this quarter, Sir. Sales are up."))
            conversation.session.reset()
            said = list(conversation.session.reply_stream("How is my venture doing?"))
        r.check("an invented status is never said: the honest line instead",
                len(said) == 1 and said[0] in conversation._UNKNOWN, str(said))
        with Patch() as p:
            p(local_brain, "chat_stream", tokens_of(
                "**GDP** measures a country's output over a year. "
                "Let me know if you need anything else."))
            conversation.session.reset()
            said = list(conversation.session.reply_stream("What is GDP?"))
        r.check("markdown is stripped and a trailing sign-off is dropped",
                said == ["GDP measures a country's output over a year."], str(said))
    with guard(r, "short opener"):
        chunks = list(speech.stream_segments(iter(["Sure", ". Supply chain management ",
                                                   "is how goods move. It covers ",
                                                   "sourcing and delivery."])))
        r.check("a short first sentence travels with the next instead of holding "
                "everything back", chunks[:1] == ["Sure. Supply chain management is how "
                                                   "goods move."] and len(chunks) == 2,
                str(chunks))
        split = list(speech.lead_phrase(iter([
            "Inflation is when prices rise over time, so each pound buys a little "
            "less than it used to.", "Next sentence."])))
        r.check("a long first sentence is split once, at a clause",
                split[:2] == ["Inflation is when prices rise over time,",
                              "so each pound buys a little less than it used to."], str(split))
        r.check("  and only the first", split[2:] == ["Next sentence."])

    # ── L, M. Streaming speech and the first thing heard ─────────────────
    r.head("L-M  streaming speech")
    with guard(r, "tts"):
        with Patch() as p:
            sp = Speaker().install(p)

            def slow_pieces():
                yield "First sentence here."
                time.sleep(0.6)
                yield "Second sentence, later."
            generation = voice.begin_interaction()
            sp.t0 = time.perf_counter()
            voice.speak_stream(slow_pieces(), generation=generation)
        r.check("the first clip plays before the second exists",
                len(sp.played) == 2 and sp.played[0][0] < 0.3 and sp.played[1][0] >= 0.55,
                str(sp.played))
    with guard(r, "first audible"):
        with Patch() as p:
            sp = Speaker().install(p)
            p(local_brain, "available", lambda: True)
            p(local_brain, "chat_stream", tokens_of(
                "Inflation is prices rising across the economy. " + "More detail. " * 20,
                delay=0.02))
            conversation.session.reset()
            generation = voice.begin_interaction()
            sp.t0 = time.perf_counter()
            delegation_tool.handle("Why is the sky blue?", interaction_speaker(generation),
                                   generation=generation)
            finished = time.perf_counter() - sp.t0
        first = sp.played[0][0] if sp.played else None
        r.check("the first clip plays long before the answer is complete",
                first is not None and first < finished / 3, f"first {first} of {finished:.2f}s")

    # ── J. A newer request takes over ────────────────────────────────────
    r.head("J  stale requests")
    with guard(r, "superseded mid-answer"):
        record = []
        with Patch() as p:
            sp = Speaker(play_seconds=0.15).install(p)
            p(local_brain, "available", lambda: True)
            p(local_brain, "chat_stream", tokens_of(
                "One. " + "This is a sentence that keeps going on. " * 30,
                delay=0.01, record=record))
            conversation.session.reset()
            generation = voice.begin_interaction()
            worker = threading.Thread(target=delegation_tool.handle, args=(
                "Why is the sky blue?", interaction_speaker(generation)),
                kwargs={"generation": generation})
            worker.start()
            time.sleep(0.35)
            newer = voice.begin_interaction()            # he pressed the key again
            played_at_takeover = len(sp.played)
            worker.join(timeout=5)
            time.sleep(0.3)
        r.check("nothing of the old answer plays after the new request",
                len(sp.played) <= played_at_takeover + 1,
                f"{played_at_takeover} -> {len(sp.played)}")
        r.check("  and the old answer stops being generated",
                bool(record) and record[0]["closed"])
        r.check("  and is not remembered as said",
                not conversation.session.snapshot()["turns"])
        r.check("the newer request is current", voice.is_current_generation(newer))

    # ── N, O. Residency and the microphone ───────────────────────────────
    r.head("N-O  warming, and the microphone")
    with guard(r, "warm on press"):
        warmed, workers = [], []
        with Patch() as p:
            p(voice_bridge, "_warm_the_brain", lambda: warmed.append(True))
            p(voice_bridge, "_ptt_worker", lambda on_text: workers.append(True))
            started = voice_bridge.push_to_talk_start()
            time.sleep(0.1)
            voice_bridge._ptt_active.clear()
        r.check("pressing the key starts warming the model", started and warmed == [True])
        r.check("  and the microphone is still the worker's alone", workers == [True])
    with guard(r, "microphone"):
        for module in (decision, delegation_tool, conversation, speech):
            source = open(module.__file__).read()
            r.check(f"{module.__name__} never touches the microphone",
                    not any(w in source for w in ("MIC_LOCK", "sounddevice",
                                                  "capture_speech", "command_capture_queue",
                                                  "is_capturing_command")))

    # ── P, Q. Safety and resources ───────────────────────────────────────
    r.head("P-Q  safety and resources")
    with guard(r, "safety"):
        banned = ("CGEventPost", "CGEventCreateMouseEvent", "CGEventCreateKeyboardEvent",
                  "CGWarpMouseCursorPosition", "setMouseLocation", "_post_click")
        for module in (decision, delegation_tool, conversation, speech, voice, voice_bridge):
            r.check(f"{module.__name__}: no synthetic input",
                    not any(b in open(module.__file__).read() for b in banned))
        r.check("computer use is off", computer_use.ENABLED is False)
    with guard(r, "cost"):
        samples = []
        for utterance, _ in CORPUS * 3:
            s = time.perf_counter()
            decision.decide(utterance)
            samples.append((time.perf_counter() - s) * 1000)
        samples.sort()
        median = samples[len(samples) // 2]
        r.check("a rules decision costs far less than the model call it replaces "
                "(median under 5 ms)", median < 5.0, f"median {median:.2f} ms")

    return r.report("fast path")


if __name__ == "__main__":
    sys.exit(run())
