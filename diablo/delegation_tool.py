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
from skills import conversation  # noqa: E402
from skills import local_brain  # noqa: E402

BASE_DIR = os.getenv("DIABLO_BASE_DIR", os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")))
JOBS_PATH = os.path.join(BASE_DIR, "jobs.json")
MAX_CONCURRENT = int(os.getenv("DIABLO_MAX_CONCURRENT_AGENTS", "1"))
LONG_TASK_WORDS = 4  # anything shorter is answered inline rather than queued

# How many times Diablo may come back with a question before it either commits
# or gives up. Two is the ceiling that still feels like a conversation.
CLARIFY_ROUNDS = int(os.getenv("DIABLO_CLARIFY_ROUNDS", "2"))
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


def _worker() -> None:
    while True:
        job, speak, ask, confirm = _queue.get()
        try:
            store.update(job["id"], status="running", started_at=time.time())
            summary = agent_core.run(job["task"], speak, ask=ask, confirm=confirm)
            store.update(job["id"], status="done", finished_at=time.time(),
                         summary=summary)
            # Fold the result into the conversation so a follow-up like "what
            # did you find?" has something to refer back to.
            conversation.session.remember("assistant", summary)
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
) -> dict:
    """Queue a task and return immediately."""
    _ensure_workers()
    job = {
        "id": uuid.uuid4().hex[:8],
        "task": task,
        "status": "queued",
        "created_at": time.time(),
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
    if not local_brain.available():
        speak("My local brain is offline, Sir. Ollama does not appear to be running.")
        return "error"

    lower = text.lower()
    if conversation.wants_reset(lower):
        conversation.session.reset()
        speak("Forgotten, Sir.")
        return "chat"

    verdict = agent_core.understand(text, in_conversation=conversation.session.is_followup())

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
        answer = ask(verdict["question"])
        if not answer:
            speak("I did not catch that, Sir.")
            break
        gathered = f"{gathered}\n(I asked: {verdict['question']} — you said: {answer})"
        verdict = agent_core.understand(gathered,
                                        in_conversation=conversation.session.is_followup())
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

    if depth < LONG_TASK_WORDS and not store.active():
        # Short and nothing else queued: run it here so the reply is immediate.
        summary = agent_core.run(task, speak, ask=ask, confirm=confirm)
        conversation.session.remember("user", text)
        conversation.session.remember("assistant", summary)
        speak(summary)
        job = {"id": uuid.uuid4().hex[:8], "task": task, "status": "done",
               "created_at": time.time(), "finished_at": time.time(),
               "summary": summary}
        store.add(job)
        return "inline"

    speak("On it, Sir.")
    submit(task, speak, ask=ask, confirm=confirm)
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
