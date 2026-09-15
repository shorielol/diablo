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
import os

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
from skills import trace  # noqa: E402
try:
    from skills import route  # noqa: E402
except Exception:  # pragma: no cover
    # The router keeps working on the old path if this fails to
    # import. A classifier is an improvement, not a dependency.
    route = None
from skills import conversation  # noqa: E402
from skills import local_brain  # noqa: E402
from skills import roster  # noqa: E402
from skills import persona  # noqa: E402
from skills import tutor  # noqa: E402

BASE_DIR = os.getenv("DIABLO_BASE_DIR", os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))
JOBS_PATH = os.path.join(BASE_DIR, "jobs.json")
MAX_CONCURRENT = config.env_int("DIABLO_MAX_CONCURRENT_AGENTS", 1)
LONG_TASK_WORDS = 4  # anything shorter is answered inline rather than queued

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
        conversation.session.remember_working(agent_id, working)
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
                conversation.session.remember_working(
                    job.get("agent") or "diablo", working)
            store.update(job["id"], status="done", finished_at=time.time(),
                         summary=summary)
            # Fold the result into the conversation so a follow-up like "what
            # did you find?" has something to refer back to.
            conversation.session.remember("assistant", summary)
            # Whoever did the work is now who he is talking to, so "and the
            # other one?" reaches them rather than starting again with the
            # orchestrator. A fan-out leaves focus alone: several of them
            # answered and picking one would be a guess.
            if not job.get("parts") and job.get("agent") not in (None, "diablo"):
                conversation.session.focus = job["agent"]
            _push("job_done", id=job["id"], agent=job.get("agent", "diablo"),
                  summary=summary[:400])
            speak(summary)
        except Exception as e:
            store.update(job["id"], status="failed", finished_at=time.time(),
                         summary=str(e))
            print(f"  [Tasks] Job {job['id']} failed: {type(e).__name__}: {e}")
            speak("That one came apart on me, Sir. The details are in the log.")
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
        conversation.session.reset()
        speak("Forgotten, Sir.")
        return "chat"

    in_conversation = conversation.session.is_followup()

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
        conversation.session.remember("user", text)
        conversation.session.remember("assistant", summary)
        conversation.session.focus = "vassago"
        # What was read off the screen and what sympy did with it, so "walk me
        # through your maths" is answered from the record instead of the model
        # reconstructing a plausible method backwards from the answer.
        conversation.session.remember_working("vassago", working)
        trace.event("done", route="teach", summary=summary[:120])
        return "taught"

    # A named agent takes the conversation, whatever kind of turn this is.
    named = conversation.addressed_to(text)
    if named:
        conversation.session.focus = None if named == "diablo" else named
        trace.event("focus", agent=named)

    if verdict["action"] == "chat":
        # Not the one-shot reply the classifier produced — a real conversational
        # turn, with the last dozen exchanges behind it and the screen attached
        # if he pointed at it.
        speak(conversation.talk(text))
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
        answer = ask(verdict["question"])
        if not answer:
            speak("I did not catch that, Sir.")
            break
        gathered = f"{gathered}\n(I asked: {verdict['question']} — you said: {answer})"
        verdict = agent_core.understand(gathered,
                                        in_conversation=conversation.session.is_followup())

    # A named agent takes the conversation, whatever kind of turn this is.
    named = conversation.addressed_to(text)
    if named:
        conversation.session.focus = None if named == "diablo" else named
        trace.event("focus", agent=named)

    if verdict["action"] == "chat":
            speak(conversation.talk(gathered))
            return "chat"

    if verdict["action"] == "clarify":
        if CLARIFY_STRICT:
            # Refusing here is the point. Two rounds failed to pin it down, so
            # anything done now would be a guess dressed up as an instruction.
            speak("I still don't follow, Sir. Say it again when you have a moment.")
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
            conversation.session.remember("user", text)
            conversation.session.remember("assistant", summary)
            speak(summary)
            trace.event("done", route="escalate", summary=summary[:120])
            return "escalated"
        except Exception as e:
            trace.event("route", route="escalate_failed", reason=str(e)[:100])
            # Fall through and let the small model try rather than giving up.

    if depth < LONG_TASK_WORDS and not store.active():
        # Short and nothing else queued: run it here so the reply is immediate.
        #
        # "Immediate" still means a model round trip and one or more tools, and
        # from the other side of a speaker that is several seconds of nothing.
        # Say something first. It is cached audio, so it costs no latency of
        # its own and it starts playing while the model is still loading.
        pieces_inline = [(task, roster.assign(task))]
        trace.event("route", route="inline", words=depth, task=task[:120],
                    agent=pieces_inline[0][1])
        speak(persona.working())
        summary = agent_core.run(task, speak, ask=ask, confirm=confirm)
        trace.event("done", route="inline", summary=summary[:120])
        conversation.session.remember("user", text)
        conversation.session.remember("assistant", summary)
        if pieces_inline and pieces_inline[0][1] not in (None, "diablo"):
            conversation.session.focus = pieces_inline[0][1]
        speak(summary)
        job = {"id": uuid.uuid4().hex[:8], "task": task, "status": "done",
               "created_at": time.time(), "finished_at": time.time(),
               "agent": pieces_inline[0][1], "parts": [], "summary": summary}
        store.add(job)
        return "inline"

    # Several errands in one sentence, and none of them waiting on another.
    # roster.split() returns one piece unless it is certain, so the common case
    # falls straight through to the ordinary queue.
    pieces = roster.split(task)
    if len(pieces) > 1:
        trace.event("route", route="fan_out", task=task[:120],
                    agents=[a for _t, a in pieces])
        speak(roster.announce(pieces))
        submit(task, speak, ask=ask, confirm=confirm, pieces=pieces)
        return "delegated"

    trace.event("route", route="queued", words=depth, task=task[:120],
                agent=pieces[0][1])
    speak(roster.announce(pieces))
    submit(task, speak, ask=ask, confirm=confirm, pieces=pieces)
    return "queued"


# ─── Voice status ────────────────────────────────────────────────────────────

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
