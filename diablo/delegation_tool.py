"""
D.I.A.B.L.O. Task Manager
=========================
Keeps the microphone free while work is being done.

A spoken task goes on a queue, a worker thread runs it through agent_core, and
the result is spoken when it lands. Diablo carries on listening the whole time,
so you can hand it a second job while the first is still running.

Concurrency is one by default and that is deliberate. qwen3:14b is roughly 9 GB
resident; running two at once on an M3 Pro means swapping, and a swapping model
is slower than a queue. Raise DIABLO_MAX_CONCURRENT_AGENTS only if you have
moved to a smaller model.
"""

import json
import inspect
import os
import re

from skills import config  # noqa: E402
import queue
import sys
import threading
import time
import uuid
from typing import Callable, List, Optional

# The skill registry loads this file by path, so its own folder is not
# necessarily importable. Put it there before reaching for its siblings.
# The package parent, so `from skills import x` resolves whether this module
# is imported as part of the package or run directly as a script. Importing
# bare `x` here instead created a SECOND copy of each module with its own
# globals, which is why patching local_brain.OLLAMA_HOST in one place had no
# effect in the other. One module, one instance.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import agent_core  # noqa: E402
from skills import agent_tools  # noqa: E402
from skills import trace  # noqa: E402
try:
    from skills import route  # noqa: E402
except Exception:  # pragma: no cover
    # The router keeps working on the old path if this fails to
    # import. A classifier is an improvement, not a dependency.
    route = None
try:
    from skills import showing  # noqa: E402
except Exception:  # pragma: no cover
    # Without it a request to be shown something goes the way it went before
    # GUI-X.1: through the planner. Worse, and still a working assistant.
    showing = None
from skills import conversation  # noqa: E402
from skills import local_brain  # noqa: E402
from skills import roster  # noqa: E402
from skills import persona  # noqa: E402
from skills import tutor  # noqa: E402

BASE_DIR = os.getenv("DIABLO_BASE_DIR", os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))
JOBS_PATH = os.path.join(BASE_DIR, "jobs.json")
MAX_CONCURRENT = config.env_int("DIABLO_MAX_CONCURRENT_AGENTS", 1)
# What makes a task worth its own agent.
#
# This used to be a word count: four words or more went to the queue. "Decrease
# the system volume" is four words, so turning the volume down spawned an
# agent, announced it, and left a row in the sidebar — for something that is
# one AppleScript call and over before the announcement finishes. Length was
# never the question. What the task actually needs is.
#
# Matched on phrases rather than on agent_tools.select(). That was the first
# attempt and it was wrong: select() returns a ranked MENU of up to twelve
# tools, and the research tool's tags include "the", "what", "are", "out" and
# "about", so it appears in nearly every menu. Membership meant "what time is
# it" and "pause the music" both wanted their own agent. A phrase says what a
# single word cannot — "look into" is research, "look at my screen" is not.
RESEARCH_CUES = (
    "research", "look into", "looking into", "read up on", "dig into",
    "find out about", "find out what", "find out whether", "find out if",
    "investigate", "compare the",
    "compare these", "what are the best", "which are the best",
    "options for", "gather", "summarise everything", "summarize everything",
    "go through the", "shortlist", "pros and cons of", "review the options",
    "deep research", "look up everything", "verify whether", "fact check",
    "find information about", "information on", "read up about",
    "background on", "what do we know about", "dig up",
)

# Things that are on this machine rather than on the web. A research cue
# pointing at one of these is not research — "look into my downloads folder" is
# a directory listing and Diablo does it in 200ms.
LOCAL_OBJECTS = (
    "my screen", "the screen", "this screen", "my downloads", "downloads folder",
    "my desktop", "desktop folder", "my documents", "documents folder",
    "my folder", "this folder", "that folder", "my files", "this file",
    "that file", "my drive", "this window", "that window", "my notes app",
    "my calendar", "my inbox", "my messages",
    # Demonstrative plus a thing that is already on the machine. "Compare these
    # two windows" is about the glass in front of him, not about the web.
    "these windows", "two windows", "both windows", "these tabs", "these files",
    "these folders", "these documents", "my apps", "these apps", "my photos",
    "my mac", "this mac", "my machine", "my laptop", "these two tracks",
    "this track", "my music", "my library", "my volume", "the volume",
)

AGENT_REQUESTS = (
    "spin up", "spawn", "assign an agent", "assign someone", "put someone on",
    "get someone on", "put an agent on", "delegate", "hand this to",
    "hand it to",
    # "in the background" on its own is too weak: it is also just a place, as
    # in "in the background music is playing, pause it". It has to attach to a
    # request for it to be one.
    "this in the background", "it in the background", "that in the background",
    "running in the background", "work on it in the background",
    "while i", "go away and", "take your time on",
    "create an agent", "make an agent", "new agent", "start an agent",
    "set up an agent", "an agent for this", "agent on this",
)

# How many times Diablo may come back with a question before it either commits
# or gives up. Two is the ceiling that still feels like a conversation.
CLARIFY_ROUNDS = config.env_int("DIABLO_CLARIFY_ROUNDS", 2)
# Strict: if it is still ambiguous after those rounds, decline rather than pick.
# Acting on a sentence nobody managed to pin down is how a voice assistant edits
# the wrong file. Set to 0 to let it make a best guess instead.
CLARIFY_STRICT = os.getenv("DIABLO_CLARIFY_STRICT", "1") not in ("0", "", "false", "False")


# ─── Job Store ───────────────────────────────────────────────────────────────

class JobStore:
    """Thread-safe, disk-backed record of every task."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._jobs: List[dict] = self._load()
        # Anything still marked running belongs to a dead process from a prior boot.
        for job in self._jobs:
            if job.get("status") in ("running", "queued"):
                job["status"] = "interrupted"

    def _load(self) -> List[dict]:
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception:
            return []

    def _flush(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self._jobs[-100:], f, indent=2)
        except Exception as e:
            print(f"  [Tasks] Could not write job store: {e}")

    def add(self, job: dict) -> None:
        with self._lock:
            self._jobs.append(job)
            self._flush()

    def clear(self, keep_active: bool = True) -> int:
        """
        Forget finished work. Returns how many were dropped.

        Needed because the store is disk-backed but memory-authoritative:
        _flush writes self._jobs, so emptying jobs.json by hand is undone by
        the very next add() or update(). Anything that purges the store has to
        go through here, in the running process.

        Active work is kept by default. A research job that is still going is
        the one thing in here worth looking at, and dropping its row while its
        thread carries on would leave work running with nothing on screen
        saying so.
        """
        with self._lock:
            before = len(self._jobs)
            if keep_active:
                self._jobs = [j for j in self._jobs
                              if j.get("status") in ("running", "queued")]
            else:
                self._jobs = []
            self._flush()
            return before - len(self._jobs)


    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            for job in self._jobs:
                if job["id"] == job_id:
                    job.update(fields)
                    break
            self._flush()

    def update_part(self, job_id: str, index: int, **fields) -> None:
        """One agent's slice of a fanned-out job."""
        with self._lock:
            for job in self._jobs:
                if job["id"] == job_id:
                    parts = job.get("parts") or []
                    if 0 <= index < len(parts):
                        parts[index].update(fields)
                    break
            self._flush()

    def active(self) -> List[dict]:
        with self._lock:
            return [j for j in self._jobs if j.get("status") in ("running", "queued")]

    def recent(self, limit: int = 5) -> List[dict]:
        with self._lock:
            return list(reversed(self._jobs[-limit:]))


store = JobStore(JOBS_PATH)


# ─── Worker pool ─────────────────────────────────────────────────────────────

_queue: "queue.Queue" = queue.Queue()
_workers_started = False
_workers_lock = threading.Lock()


def _push(event: str, **data) -> None:
    """
    Tell the window something happened, if a window is listening.

    Imported here rather than at the top because server.py imports this module
    to read the job store, and a circular import at load time would take the
    assistant down over a progress indicator.
    """
    try:
        from skills import server
        server.push(event, **data)
    except Exception:
        pass


def _fan_out(job: dict, ask, confirm) -> str:
    """
    Several agents on one request, at the same time.

    Every sub-agent gets a silent `speak` — four voices at once is noise — and
    they share one `Attention`, so only one of them can hold the microphone to
    ask a question. What comes back is stitched together with attribution and
    that is the only thing said out loud.
    """
    pieces = [(p["task"], p["agent"]) for p in job["parts"]]
    index_of = {(t, a): i for i, (t, a) in enumerate(pieces)}
    attention = roster.Attention(ask, confirm)

    def runner(subtask, agent_id, quiet, sub_ask, sub_confirm):
        # One list per agent, created here inside its own thread. Shared state
        # across sub-agents has bitten this project before: CONTEXT used to be
        # module-level and the last agent to start owned everyone's callbacks.
        working: list = []
        answer = agent_core.run(subtask, quiet, ask=sub_ask,
                                confirm=sub_confirm, working=working)
        conversation.session.record_task(agent_id, subtask, working, answer,
                                         task_id=f"{job['id']}:{agent_id}",
                                         generation=job.get("generation"))
        return answer

    def started(agent_id, subtask):
        i = index_of.get((subtask, agent_id), 0)
        store.update_part(job["id"], i, status="running", started_at=time.time())
        _push("agent_started", id=job["id"], agent=agent_id, task=subtask)
        trace.event("agent", agent=agent_id, task=subtask[:120], state="started")

    def finished(agent_id, subtask, answer):
        i = index_of.get((subtask, agent_id), 0)
        store.update_part(job["id"], i, status="done", finished_at=time.time(),
                          summary=answer[:400])
        _push("agent_done", id=job["id"], agent=agent_id, summary=answer[:200])
        trace.event("agent", agent=agent_id, state="done", summary=answer[:120])

    started_at = time.time()
    results = roster.run_parallel(pieces, runner, attention, started, finished)
    trace.event("fan_out", agents=[a for _t, a in pieces],
                elapsed_ms=int((time.time() - started_at) * 1000))
    return roster.merge(results)


def _speak_with_metadata(speak, text: str, **metadata):
    """Pass result identity when the speech callback supports it.

    A few offline/unit-test callers still provide a one-argument callback.  The
    production bridge accepts metadata, so final task results get a stable
    admission key without making those small callers part of this phase.
    """
    if isinstance(text, agent_core.AlreadySpoken):
        # run() asked this aloud and nobody answered. It has been heard once.
        return "already_spoken"
    try:
        parameters = inspect.signature(speak).parameters.values()
        accepts_metadata = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters
        ) or all(name in inspect.signature(speak).parameters for name in metadata)
    except (TypeError, ValueError):
        accepts_metadata = False
    if accepts_metadata:
        return speak(text, **metadata)
    return speak(text)


def _worker() -> None:
    while True:
        job, speak, ask, confirm = _queue.get()
        try:
            store.update(job["id"], status="running", started_at=time.time())
            _push("job_started", id=job["id"], task=job["task"],
                  agent=job.get("agent", "diablo"), parts=job.get("parts") or [])
            if job.get("parts"):
                summary = _fan_out(job, ask, confirm)
            else:
                # One agent is the common path, not the fan-out, so the working
                # has to be kept here too — a fix that only covered fan-outs
                # would miss almost every research request he actually makes.
                working: list = []
                summary = agent_core.run(job["task"], speak, ask=ask,
                                         confirm=confirm, working=working)
                conversation.session.record_task(
                    job.get("agent") or "diablo", job["task"], working, summary,
                    task_id=job["id"], generation=job.get("generation"))
            store.update(job["id"], status="done", finished_at=time.time(),
                         summary=summary)
            # Fold the result into the conversation so a follow-up like "what
            # did you find?" has something to refer back to.
            conversation.session.remember(
                "assistant", summary, generation=job.get("generation"))
            # Whoever did the work is now who he is talking to, so "and the
            # other one?" reaches them rather than starting again with the
            # orchestrator. A fan-out leaves focus alone: several of them
            # answered and picking one would be a guess.
            if not job.get("parts") and job.get("agent") not in (None, "diablo"):
                conversation.session.set_focus(
                    job["agent"], generation=job.get("generation"))
            _push("job_done", id=job["id"], agent=job.get("agent", "diablo"),
                  summary=summary[:400])
            _speak_with_metadata(
                speak, summary, speech_id=f"job:{job['id']}:result",
                speech_kind="TASK_RESULT")
        except Exception as e:
            store.update(job["id"], status="failed", finished_at=time.time(),
                         summary=str(e))
            # A failure is an event like a success: without it the window and
            # the orb go on showing the job as running until something else
            # happens to refresh them.
            _push("job_failed", id=job["id"], agent=job.get("agent", "diablo"))
            print(f"  [Tasks] Job {job['id']} failed: {type(e).__name__}: {e}")
            _speak_with_metadata(
                speak, "That one came apart on me, Sir. The details are in the log.",
                speech_id=f"job:{job['id']}:failure", speech_kind="TASK_RESULT")
        finally:
            _queue.task_done()


def _ensure_workers() -> None:
    global _workers_started
    with _workers_lock:
        if _workers_started:
            return
        for _ in range(max(1, MAX_CONCURRENT)):
            threading.Thread(target=_worker, daemon=True).start()
        _workers_started = True


# ─── Entry points ────────────────────────────────────────────────────────────

def submit(
    task: str,
    speak: Callable[[str], None],
    ask: Optional[Callable[[str], str]] = None,
    confirm: Optional[Callable[[str], bool]] = None,
    pieces: Optional[List[tuple]] = None,
    generation: Optional[int] = None,
) -> dict:
    """
    Queue a task and return immediately.

    `pieces` is the output of roster.split(): (subtask, agent_id) pairs. Two or
    more and the job is fanned out, which is recorded on the job itself so the
    window can show who has which half while it is still running.
    """
    _ensure_workers()
    if pieces is None:
        pieces = [(task, roster.assign(task))]
    job = {
        "id": uuid.uuid4().hex[:8],
        "task": task,
        "status": "queued",
        "created_at": time.time(),
        "generation": generation,
        "agent": "diablo" if len(pieces) > 1 else pieces[0][1],
        "parts": ([{"agent": a, "task": t, "status": "queued", "summary": ""}
                   for t, a in pieces] if len(pieces) > 1 else []),
    }
    store.add(job)
    _queue.put((job, speak, ask, confirm))
    return job


def handle(
    text: str,
    speak: Callable[[str], None],
    ask: Optional[Callable[[str], str]] = None,
    confirm: Optional[Callable[[str], bool]] = None,
    generation: Optional[int] = None,
) -> str:
    """
    The one call voice_bridge makes for anything the reflexes did not claim.

    Understanding happens inline — it is a single constrained generation and the
    user is still standing there — so greetings and clarifying questions come
    back at conversational speed. Only real work goes on the queue.

    Returns "chat", "clarify", "queued" or "error", for the log.
    """
    trace.new_turn("voice")
    trace.event("heard", text=text[:200])

    if not local_brain.available():
        trace.event("route", route="error", reason="ollama offline")
        speak("My local brain is offline, Sir. Ollama does not appear to be running.")
        return "error"

    lower = text.lower()
    if conversation.wants_reset(lower):
        conversation.session.reset(generation=generation)
        speak("Forgotten, Sir.")
        return "chat"

    in_conversation = conversation.session.is_followup(generation=generation)

    # One classification, carrying confidence and risk rather than just a verb.
    # classify() calls understand() internally, so this is not an extra model
    # round trip — it is the same one, with two more axes read off it.
    decision = None
    if route is not None:
        try:
            decision = route.classify(text, in_conversation=in_conversation)
            verdict = {"action": {"escalate": "act"}.get(decision.route, decision.route),
                       "task": decision.goal,
                       "question": decision.question,
                       "reply": ""}
        except Exception as e:
            trace.event("decide", route="fallback", reason=f"{type(e).__name__}: {e}")
            decision = None
    if decision is None:
        verdict = agent_core.understand(text, in_conversation=in_conversation)
        trace.event("understand", route=verdict["action"], via="model",
                    question=(verdict.get("question") or "")[:100])

    # Being asked to be shown something ON SCREEN: where it is, what it is, or
    # how one part of a picture leads to another.
    #
    # Ahead of the tutor, because "show me how the subsidy causes welfare
    # loss" contains "show me how" and the tutor would take it — and the tutor
    # anchors every step on printed words, which on a diagram is the legend.
    # A procedure ("show me how to save this") is never read as one of these,
    # so it still reaches the tutor below.
    shown = _show(text, decision, speak, generation)
    if shown is not None:
        return shown

    # Being asked to be shown, rather than told.
    #
    # Routed before anything else because the two are easy to confuse and the
    # cost of confusing them is exactly what prompted this: asked to help with
    # a maths question, Diablo read the screen, worked out the answer, and said
    # the answer. The request had been "help me with this".
    if tutor.wants_teaching(text) and ask is not None:
        trace.event("route", route="teach", task=text[:120])
        speak(persona.working())
        working: list = []
        summary = tutor.walk(text, speak, ask=ask, working=working)
        conversation.session.commit_turn(generation, text, summary)
        conversation.session.set_focus("vassago", generation=generation)
        # What was read off the screen and what sympy did with it, so "walk me
        # through your maths" is answered from the record instead of the model
        # reconstructing a plausible method backwards from the answer.
        conversation.session.record_task(
            "vassago", text, working, summary,
            task_id=f"turn:{trace.current_turn()}:teach",
            generation=generation)
        trace.event("done", route="teach", summary=summary[:120])
        return "taught"

    # A named agent takes the conversation, whatever kind of turn this is.
    named = conversation.addressed_to(text)
    if named:
        conversation.session.set_focus(
            None if named == "diablo" else named, generation=generation)
        trace.event("focus", agent=named)

    if verdict["action"] == "chat":
        # Not the one-shot reply the classifier produced — a real conversational
        # turn, with the last dozen exchanges behind it and the screen attached
        # if he pointed at it.
        _speak_with_metadata(
            speak, conversation.talk(text, generation=generation),
            speech_id=f"turn:{trace.current_turn()}:chat",
            speech_kind="USER_RESPONSE")
        return "chat"

    # ── Strict clarify loop ──────────────────────────────────────────────
    # Keep asking while the request is genuinely ambiguous. Each answer is
    # folded into the text so the next pass sees the whole exchange rather than
    # the latest fragment, which is what stops it asking the same question
    # twice with different words.
    rounds = 0
    gathered = text
    while verdict["action"] == "clarify" and ask and rounds < CLARIFY_ROUNDS:
        rounds += 1
        # `ask` speaks the question itself — it owns the microphone, so it has
        # to say the question and open the mic without a gap in between.
        trace.event("clarify", round=rounds, question=verdict["question"][:120])
        asked = verdict["question"]
        answer = ask(asked)
        if not answer:
            speak("I did not catch that, Sir.")
            break
        gathered = f"{gathered}\n(I asked: {asked} — you said: {answer})"
        verdict = agent_core.understand(
            gathered,
            in_conversation=conversation.session.is_followup(
                generation=generation))
        # The 4B returns the very question he just answered (5 of 5 live, on
        # "Any product update."), and asking it again spends the last round on
        # nothing. Go on with what was gathered, as the non-strict path does.
        if verdict["action"] == "clarify" and verdict.get("question") == asked:
            trace.event("clarify", round=rounds, repeated=True)
            verdict = {"action": "act", "task": gathered}
            break

    # A named agent takes the conversation, whatever kind of turn this is.
    named = conversation.addressed_to(text)
    if named:
        conversation.session.set_focus(
            None if named == "diablo" else named, generation=generation)
        trace.event("focus", agent=named)

    if verdict["action"] == "chat":
        _speak_with_metadata(
            speak, conversation.talk(gathered, generation=generation),
            speech_id=f"turn:{trace.current_turn()}:chat",
            speech_kind="USER_RESPONSE")
        return "chat"

    if verdict["action"] == "clarify":
        if CLARIFY_STRICT:
            # Refusing here is the point. Two rounds failed to pin it down, so
            # anything done now would be a guess dressed up as an instruction.
            _speak_with_metadata(
                speak, "I still don't follow, Sir. Say it again when you have a moment.",
                speech_id=f"turn:{trace.current_turn()}:clarify",
                speech_kind="USER_RESPONSE")
            return "unclear"
        verdict = {"action": "act", "task": gathered}
    elif rounds:
        verdict["task"] = verdict.get("task") or gathered

    task = verdict.get("task") or text
    depth = len(task.split())

    # Escalation: not confident enough to let the small model act alone, but
    # clear enough that another question would just be irritating.
    if decision is not None and decision.route == "escalate":
        trace.event("route", route="escalate", confidence=decision.confidence,
                    reason=decision.reason)
        speak("This one needs more thought, Sir. One moment.")
        try:
            summary = local_brain.with_model(
                os.getenv("DIABLO_ESCALATE_MODEL", ""),
                lambda: agent_core.run(task, speak, ask=ask, confirm=confirm))
            conversation.session.commit_turn(generation, text, summary)
            _speak_with_metadata(
                speak, summary, speech_id=f"turn:{trace.current_turn()}:escalated",
                speech_kind="USER_RESPONSE")
            trace.event("done", route="escalate", summary=summary[:120])
            return "escalated"
        except Exception as e:
            trace.event("route", route="escalate_failed", reason=str(e)[:100])
            # Fall through and let the small model try rather than giving up.

    # ── The routing decision ─────────────────────────────────────────────
    # One call, one recorded reason. Nothing below re-derives it.
    # The operations understand() extracted, when it managed any. No extra
    # model call: route.classify() calls understand() and always has.
    routing = decide(task, operations=verdict.get("operations"), said=text)
    trace.event("decide", route=routing.route, reason=routing.reason,
                matched=routing.matched, task=task[:120])

    if not routing.delegates:
        return _run_inline(task, text, speak, ask, confirm, routing.reason,
                           decision=decision, generation=generation)

    # It delegates — but a sentence can hold one errand for an agent and one
    # for Diablo. "Research this company and lower my volume" must not spawn an
    # agent for the volume.
    direct_parts, pieces = split_by_route(task)
    for part in direct_parts:
        trace.event("decide", route=DIRECT, reason="direct half of a mixed request",
                    task=part[:120])
        _run_inline(part, part, speak, ask, confirm,
                    "direct half of a mixed request", decision=decision,
                    generation=generation)
    if not pieces:
        return "inline"

    def background_speak(message, **kwargs):
        """Mark worker progress separately from the immediate user reply."""
        kwargs.setdefault("speech_kind", "BACKGROUND_STATUS")
        return speak(message, **kwargs)

    # Several errands in one sentence, and none of them waiting on another.
    # roster.split() returns one piece unless it is certain, so the common case
    # falls straight through to the ordinary queue.
    pieces = roster.split(task)
    if len(pieces) > 1:
        trace.event("route", route="fan_out", task=task[:120],
                    agents=[a for _t, a in pieces])
        speak(roster.announce(pieces))
        submit(task, background_speak, ask=ask, confirm=confirm, pieces=pieces,
               generation=generation)
        return "delegated"

    trace.event("route", route="queued", words=depth, task=task[:120],
                agent=pieces[0][1])
    speak(roster.announce(pieces))
    submit(task, background_speak, ask=ask, confirm=confirm, pieces=pieces,
           generation=generation)
    return "queued"


def _show(text: str, decision, speak, generation: Optional[int]) -> Optional[str]:
    """
    Show what was asked to be shown, through the existing point_at.

    None when this turn is not one, or the screen says it is not about what
    is in front: the caller carries on exactly as it would have. Otherwise
    the turn is recorded like any inline action and "shown" is returned.
    """
    request = getattr(decision, "show", None) if decision is not None else None
    if showing is None or request is None:
        return None
    try:
        prepared = showing.prepare(text, request)
    except Exception as e:
        trace.event("route", route="show_failed", reason=f"{type(e).__name__}: {e}"[:120])
        return None
    if prepared is None:
        return None

    trace.event("route", route="show", intent=request.intent,
                kind=request.target.kind, task=text[:120])
    speak(persona.working())
    working: list = []
    try:
        summary = showing.present(prepared, speak, working)
    except Exception as e:
        trace.event("route", route="show_failed", reason=f"{type(e).__name__}: {e}"[:120])
        summary = "I couldn't show you that just now, Sir."
    conversation.session.commit_turn(generation, text, summary)
    if working:
        conversation.session.record_task(
            "diablo", text, working, summary,
            task_id=f"turn:{trace.current_turn()}:show",
            generation=generation)
    _speak_with_metadata(
        speak, summary, speech_id=f"turn:{trace.current_turn()}:show",
        speech_kind="USER_RESPONSE")
    trace.event("done", route="show", summary=summary[:120])
    return "shown"


def _run_inline(task: str, text: str, speak, ask, confirm,
                reason: str = "", decision=None,
                generation: Optional[int] = None) -> str:
    """
    Diablo does it, here, on this thread.

    Running inline while other work is in flight is safe: the tool context is
    thread-local and sub-agents are on their own threads. The old gate refused
    to, which is why asking for the volume during a research job spawned a
    second agent for a one-line AppleScript call.

    Nothing is written to the job store. That was the other half of the
    cluttered sidebar — even correctly-inlined actions left a row behind, so
    every "volume down" became a permanent entry in a list meant for work worth
    going back to. A finished one-liner lives in the conversation instead.
    """
    trace.event("route", route="inline", task=task[:120], reason=reason)
    # Cached audio, so it costs no latency of its own and starts playing while
    # the model is still loading.
    speak(persona.working())
    # decision carried, not dropped. The audit's first break was exactly here:
    # handle() held kind, confidence, risk, needs_screen and needs_tools, and
    # passed four arguments on.
    #
    # `working` collected too. The delegated and taught paths have always
    # passed one and recorded it; this one never did, so an inline action —
    # which is most of them, and every "point at the Save button" — left no
    # evidence at all. "Where did you get that?" had nothing to answer from.
    working: list = []
    summary = agent_core.run(task, speak, ask=ask, confirm=confirm,
                             decision=decision, working=working)
    trace.event("done", route="inline", summary=summary[:120])
    conversation.session.commit_turn(generation, text, summary)
    if working:
        # Recorded against Diablo rather than an agent: an inline action is not
        # a task in the rail, and focus stays where it was. The record is what
        # a follow-up reads, not a row in the sidebar.
        record = conversation.session.record_task(
            "diablo", task, working, summary,
            task_id=f"turn:{trace.current_turn()}:inline:{task}",
            generation=generation)
        trace.event("recorded", into="conversation.session",
                    steps=len(working),
                    observations=sum(1 for w in working if w.get("observation")),
                    findings=len(record.get("findings") or []),
                    sources=len(record.get("sources") or []))
    # Focus is deliberately not touched. It used to be set to whichever agent
    # name assign() voted for, so turning the volume down handed the
    # conversation to Seere and the next ordinary question came back in Seere's
    # voice. Focus follows a task agent, and an inline action is not one.
    _speak_with_metadata(
        speak, summary, speech_id=f"turn:{trace.current_turn()}:inline:{task}",
        speech_kind="USER_RESPONSE")
    return "inline"


# ─── Voice status ────────────────────────────────────────────────────────────

# ─── The delegation decision ─────────────────────────────────────────────────
#
# One function decides this, and it is the only one that may. The history here
# is a sequence of proxies standing in for the decision and each being wrong in
# its own way: a word count (LONG_TASK_WORDS = 4, so "decrease the system
# volume" spawned an agent), then whether a research tool appeared in the tool
# menu (research's tags include "the", "what" and "are", so "what time is it"
# spawned one). Tool availability and delegation intent are different things
# and neither may stand in for the other.
#
# Two doors, and nothing else opens one:
#
#   1. he asked for an agent
#   2. the task is genuinely substantial research
#
# Everything else is Diablo's, including anything uncertain. A false delegation
# costs a spurious task, a row in the rail and an announcement; a false direct
# costs nothing at all, because Diablo has every capability the agents have.
# So the fallback is DIRECT and it is enforced here rather than requested in a
# prompt.

DIRECT = "direct"
DELEGATE = "delegate"


class Decision:
    """Why a request went where it went. Inspectable, and logged verbatim."""

    __slots__ = ("route", "reason", "pieces", "matched")

    def __init__(self, route: str, reason: str,
                 pieces: Optional[List[tuple]] = None, matched: str = ""):
        self.route = route
        self.reason = reason
        self.pieces = pieces or []
        self.matched = matched

    @property
    def delegates(self) -> bool:
        return self.route == DELEGATE

    def __repr__(self) -> str:
        return f"<Decision {self.route} because {self.reason!r}>"


def _flatten(text: str) -> str:
    lowered = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()) + " "
    return re.sub(r"\s+", " ", lowered)


# Words that turn a cue into its opposite, or into a description of something
# that already happened. Found by attacking the matcher rather than by
# imagining cases: "do not create an agent for this, just do it" delegated,
# "never put someone on this" delegated, and "I was researching yesterday,
# what time is it" delegated. A phrase match with no sense of negation reads
# every one of those backwards.
_NEGATORS = ("not", "dont", "don t", "do not", "never", "no need to", "without",
             "rather than", "instead of", "stop", "cancel", "avoid", "wont",
             "will not", "cannot", "can t", "shouldn t", "should not")
_PAST = ("was", "were", "had", "been", "yesterday", "earlier", "already",
         "last night", "this morning", "previously")


def _cue_is_live(flat: str, cue: str) -> bool:
    """
    Is this cue a request, or is it being negated or merely described?

    Looks at the few words in front of it. Crude, and deliberately so: the
    alternative is asking the model, and a model call to decide whether to make
    a model call is both slow and another thing that can be wrong. Crude in the
    safe direction — an unrecognised negation delegates something that could
    have stayed here, and a false negation keeps work that could have been
    delegated, which costs nothing.
    """
    position = flat.find(f" {cue}")
    if position < 0:
        return False
    before = flat[:position + 1]
    window = " ".join(before.split()[-4:])
    if any(f" {n} " in f" {window} " for n in _NEGATORS):
        return False
    if any(f" {pmark} " in f" {window} " for pmark in _PAST):
        return False
    return True


def asks_for_an_agent(text: str) -> tuple:
    """(True, the phrase that said so). He asked for someone to be put on it."""
    flat = _flatten(text)
    for phrase in AGENT_REQUESTS:
        if f" {phrase}" in flat and _cue_is_live(flat, phrase):
            return True, phrase
    named = conversation.addressed_to(text)
    if named and named != "diablo":
        return True, f"addressed {named}"
    return False, ""


def is_substantial_research(text: str) -> tuple:
    """
    (True, the phrase that said so). Real investigation, not a turn of phrase.

    "look into" is the awkward one and it is why this is not a plain phrase
    match. "Look into NVIDIA's history" is research. "Look into my Downloads
    folder" is a directory listing, and delegating it spawns an agent to do
    something Diablo does in 200ms. So a cue that can point either way has to
    be followed by something that is not on this machine.
    """
    flat = _flatten(text)
    for phrase in LOCAL_OBJECTS:
        if f" {phrase}" in flat:
            return False, ""            # it is about his own machine
    for cue in RESEARCH_CUES:
        if f" {cue}" in flat and _cue_is_live(flat, cue):
            return True, cue
    return False, ""


# ─── Hard policy, which the classifier may not overrule ──────────────────────
#
# The model interprets; this decides. A local model that has misread a request
# must not be able to turn a volume change into a research project, so the
# things that are never research are settled here, in code, after it has had
# its say. §10 of the brief, and the reason the cue list is not simply deleted:
# it is no longer how research is found, but it is still how obvious direct
# work is protected.

def _is_atomic_action(text: str) -> bool:
    """A setting, a control, a lookup — never an investigation."""
    flat = _flatten(text)
    return any(f" {word}" in flat for word in ATOMIC_ACTIONS)


# Deliberately short, and it must stay short.
#
# The first version of this list held "what is " and "open " and "screen", and
# it swallowed "what is going on with NVIDIA" as an OS action — which is the
# exact failure this whole phase exists to end. A policy list is not a way to
# understand language. It is a floor under the things that are never research
# whatever a small model decides, and nothing else belongs in it.
#
# "What is X" is not here on purpose: "what is the capital of Peru" is direct
# and "what is going on with NVIDIA" is research, and no string tells them
# apart. That is the classifier's job, and the fallback's after it.
ATOMIC_ACTIONS = (
    "volume", "louder", "quieter", "turn it down", "turn it up", "mute",
    "unmute", "brightness", "brighter", "dimmer", "screenshot",
    "pause the music", "pause music", "play music", "play some music",
    "next track", "previous track", "skip track", "resume the music",
    "time is it", "whats the time", "what time",
    "battery", "disk space",
)


def _from_operations(operations: list) -> Optional[Decision]:
    """
    A Decision built from what the classifier understood, or None.

    None means it gave us nothing usable and the caller should fall back. That
    happens: the 4B sometimes returns no operations at all, and a router that
    depended on it would simply stop working. The fallback is the structural
    path, and the fallback of that is DIRECT.
    """
    usable = [o for o in (operations or [])
              if isinstance(o, dict) and str(o.get("text", "")).strip()]
    if not usable:
        return None

    pieces, reasons = [], []
    for index, operation in enumerate(usable):
        text = str(operation["text"]).strip()
        intent = str(operation.get("intent", "direct")).lower()

        # Explicit request first: it outranks everything, including the
        # atomic-action policy, because he asked.
        asked, phrase = asks_for_an_agent(text)
        if asked:
            pieces.append((text, roster.assign(text)))
            reasons.append(f"user_requested_agent: {phrase!r}")
            continue

        # Then the policy the classifier may not overrule.
        if _is_atomic_action(text):
            reasons.append("direct_os_action")
            continue

        if intent in ("research", "agent"):
            # One more gate: the classifier is talking about something on this
            # machine, which is not research whatever it called it.
            local, _ = is_substantial_research(text)
            if not local and _mentions_local_object(text):
                reasons.append("direct_local_subject")
                continue
            pieces.append((text, roster.assign(text)))
            reasons.append("multi_source_investigation" if intent == "research"
                           else "user_requested_agent")
            continue

        reasons.append("direct_understood")

    if not pieces:
        return Decision(DIRECT, "; ".join(reasons[:3]) or "direct_understood")
    return Decision(DELEGATE, "; ".join(r for r in reasons if "direct" not in r)
                    or "multi_source_investigation", pieces=pieces)


def _mentions_local_object(text: str) -> bool:
    flat = _flatten(text)
    return any(f" {phrase}" in flat for phrase in LOCAL_OBJECTS)


# ─── The last question, asked only when nothing else knows ───────────────────
#
# Measured on the 4B: understand() returns its "operations" field about 8 times
# in 14, and when it does not, a research request phrased outside the cue list
# falls through to DIRECT. Fail-safe, and poor recall — 3 of 7 real research
# requests were caught.
#
# So one more question, and it is deliberately the smallest one that can be
# asked: yes or no, does this need outside sources. It runs only in the band
# where nothing else has an opinion — not for an explicit request, not for an
# atomic action, not for anything about this machine, not when the cue list
# already fired, not when the classifier gave us operations. Most turns never
# reach it.

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {"needs_sources": {"type": "boolean"},
                   "why": {"type": "string"}},
    "required": ["needs_sources"],
}

RESEARCH_QUESTION = """Does answering this require looking things up in several
outside sources, or is it something an assistant on this Mac answers or does by
itself?

true  — it needs real investigation: several sources, comparison, current facts
        the assistant cannot know, a claim to be checked
false — a setting, a file, an app, the screen, a calculation, a greeting, or a
        question answerable from ordinary general knowledge

Judge what answering well would take, not how the sentence is phrased. Asking
for "a picture of", "an assessment of", "the bottom of" or "what people say
about" something outside this Mac all mean several sources, however casually
they are put.

true:
  "can you figure out what is going on with NVIDIA"
  "I want a proper assessment of this company before I apply"
  "give me a thorough picture of the apprenticeship market in Delhi"
  "get to the bottom of why this keeps being recommended"
  "build me a picture of the competitive landscape here"

false:
  "who wrote Dubliners"                  one fact, generally known
  "what is the capital of Peru"          one fact, generally known
  "explain recursion"                    general knowledge, no sources needed
  "what time is it"                      this machine
  "turn it down"                         this machine
  "tidy my downloads"                    this machine

If you are still unsure, answer false.
Reply with JSON only."""


# Off switch, for tests that are measuring the deterministic layers and for a
# machine with no model running. Off means the phrase fallback decides, which
# is the behaviour this replaced — worse recall, same safety.
CLASSIFY = os.getenv("DIABLO_ROUTE_CLASSIFY", "1") not in ("0", "", "false", "False")


def _needs_outside_sources(text: str) -> tuple:
    """(bool, why). False on any failure — the safe direction is Diablo's."""
    if not CLASSIFY:
        return False, ""
    try:
        answer = local_brain.chat_json(
            [{"role": "system", "content": RESEARCH_QUESTION},
             {"role": "user", "content": text[:400]}],
            schema=RESEARCH_SCHEMA, num_predict=60, timeout=20,
            temperature=0.0)
    except Exception as e:
        trace.event("route", route="classifier_failed", reason=type(e).__name__)
        return False, ""
    return bool(answer.get("needs_sources")), str(answer.get("why", ""))[:80]


def _classify_one(text: str) -> tuple:
    """(route, reason, matched) for a single operation."""
    if not (text or "").strip():
        return DIRECT, "nothing to route", ""

    # A question put to an agent that has already done the work is a follow-up,
    # not a request for a new one. "Vassago, where did you get that?" used to
    # spawn a second research task and do the whole thing again; it should be
    # answered out of the first one's evidence. Focus moves, nothing is
    # created. Only when that agent has no task does naming it mean "start".
    named = conversation.addressed_to(text)
    if named and named != "diablo":
        if conversation.session.task_for(named) is not None:
            return DIRECT, f"follow-up to {named}'s existing task", named
        return DELEGATE, f"explicitly asked: addressed {named}", named

    asked, phrase = asks_for_an_agent(text)
    if asked:
        return DELEGATE, f"explicitly asked: {phrase!r}", phrase

    research, cue = is_substantial_research(text)
    if research:
        return DELEGATE, f"multi_source_investigation: {cue!r}", cue

    # Nothing so far has an opinion. Ask, but only here.
    if not _is_atomic_action(text) and not _mentions_local_object(text):
        needs, why = _needs_outside_sources(text)
        if needs:
            return DELEGATE, f"multi_source_investigation (classifier: {why})", ""

    return DIRECT, "ambiguous_direct_fallback", ""


def decide(text: str, operations: Optional[list] = None,
           said: str = "") -> Decision:
    """
    The single authoritative routing decision. Fails toward Diablo.

    Classifies per operation rather than per sentence, which is not a detail:
    "lower the volume and research X" judged whole came out DIRECT, because the
    local-object guard saw "the volume" and suppressed the research clause —
    and the research then vanished into an inline run. A sentence can hold one
    errand for Diablo and one for an agent, and only looking at each on its own
    gets both right.

    Deliberately ignorant of which tools were selected, how long the request is,
    and whether anything else is running. None of those is the question.
    """
    if not (text or "").strip():
        return Decision(DIRECT, "nothing to route")

    # Asking for an agent is about the words he used, and by this point `text`
    # is understand()'s rewrite of them. "Run this in the background" came back
    # as a tidied instruction with the phrase gone, and a request for an agent
    # was read as ordinary work. The original utterance is checked first.
    if said and said.strip() != (text or "").strip():
        asked, phrase = asks_for_an_agent(said)
        if asked:
            return Decision(DELEGATE, f"user_requested_agent: {phrase!r}",
                            pieces=[(text, roster.assign(text))], matched=phrase)

    # What the classifier understood, when it understood anything. This is the
    # part that generalises: "can you figure out what's going on with NVIDIA"
    # is research and matches no cue in any list.
    if operations:
        semantic = _from_operations(operations)
        if semantic is not None:
            return semantic

    parts = roster.structural_parts(text) or [text]
    routed = [(part,) + _classify_one(part) for part in parts]
    delegated = [r for r in routed if r[1] == DELEGATE]

    if not delegated:
        # Whole-sentence reasons read better when there was only ever one.
        _, _, reason, matched = routed[0]
        return Decision(DIRECT, reason, matched=matched)

    first = delegated[0]
    reason = first[2] if len(routed) == 1 else f"{first[2]} (one of {len(routed)} parts)"
    return Decision(DELEGATE, reason,
                    pieces=[(r[0], roster.assign(r[0])) for r in delegated],
                    matched=first[3])


def split_by_route(text: str) -> tuple:
    """
    (direct_parts, delegated_pieces). One classification, applied per operation.

    Uses roster.structural_parts rather than roster.split: split() answers a
    different question — is this worth several agents at once — and its gate is
    conservative for good reason. Separating work that must be delegated from
    work that must not is not that question.
    """
    parts = roster.structural_parts(text) or [text]
    direct, delegated = [], []
    for part in parts:
        route, _reason, _matched = _classify_one(part)
        if route == DELEGATE:
            delegated.append((part, roster.assign(part)))
        else:
            direct.append(part)
    return direct, delegated


def needs_its_own_agent(task: str, text: str = "") -> bool:
    """Kept as the one-line form. decide() is the authority."""
    return decide(f"{task or ''} {text or ''}".strip()).delegates


def status_report() -> str:
    active = store.active()
    if active:
        lines = [f"{len(active)} task{'s' if len(active) != 1 else ''} on the go, Sir."]
        for job in active[:3]:
            lines.append(f"Currently: {job['task'][:80]}.")
        return " ".join(lines)

    finished = [j for j in store.recent(4) if j.get("status") == "done"]
    if finished:
        return f"Nothing running, Sir. The last thing I finished: {finished[0].get('summary', 'a task')}"
    return "Nothing on my plate at the moment, Sir."


STATUS_PHRASES = [
    "status report", "what are you working on", "what are you doing",
    "any updates", "job status", "what's running", "whats running",
    "are you done", "progress report", "how's that going", "hows that going",
]


def is_status_query(lower: str) -> bool:
    return any(p in lower for p in STATUS_PHRASES)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        spoken = " ".join(sys.argv[1:])
        print(json.dumps(agent_core.understand(spoken), indent=2))
    else:
        print(status_report())
