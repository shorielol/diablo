"""
D.I.A.B.L.O. Conversation
=========================
Talking to Diablo while you work, rather than issuing it commands.

The difference is memory and shared reference. A command interface answers each
sentence from nothing, so "what about the second one" is meaningless to it. A
conversation keeps the last few turns, knows what is on your screen when you say
"this", and lets you think out loud without re-stating context every time.

Three things it does that the command path does not:

  Memory      the last dozen turns, per session. A session goes stale after
              fifteen minutes of silence and starts fresh, because a reference
              to "it" from an hour ago is worse than no reference at all.

  Sight       when you say "this", "on screen", "my chrome tab", the window you
              are looking at is read and attached to the turn. It is not
              attached otherwise — sending a screenshot's worth of OCR into
              every reply is how a 14B model loses the thread.

  Restraint   it answers in one or two spoken sentences. Anything longer is
              being read aloud at you while you are trying to work.
"""

import os

from skills import config  # noqa: E402
import random
import re  # SIGNOFF_GUARD
import sys
import time
from typing import Any, Dict, List, Optional

# The package parent, so `from skills import x` resolves whether this module
# is imported as part of the package or run directly as a script. Importing
# bare `x` here instead created a SECOND copy of each module with its own
# globals, which is why patching local_brain.OLLAMA_HOST in one place had no
# effect in the other. One module, one instance.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import local_brain  # noqa: E402
from skills import persona  # noqa: E402
from skills import screen_context  # noqa: E402

ASSISTANT_NAME = os.getenv("DIABLO_NAME", "Diablo")
# Measured, not guessed. At 12 a session held about 18 turns and 1,000
# characters — 3% of the ~32,000 the 8192-token window allows — and then
# started folding the beginning away. The compaction was not protecting the
# context window from anything; it was throwing away the conversation while
# the window sat empty. 30 turns is roughly 3,000 characters, still under a
# tenth of the budget, and pushes the first compaction far out of the way of
# an ordinary session.
MAX_TURNS = config.env_int("DIABLO_MEMORY_TURNS", 30)
SESSION_TIMEOUT = config.env_int("DIABLO_SESSION_TIMEOUT", 900)  # 15 minutes

# How much of an agent's working is kept, and how much of it reaches a prompt.
#
# The context is 4096 tokens and read_page returns whole articles, so none of
# this can be unbounded. The numbers are chosen against what the tools really
# return: a search result is a couple of hundred characters, a page read is
# capped at 600 here, and twelve calls covers a two-source research run with
# room over. The prompt budget is smaller again, because the working goes in
# alongside the conversation rather than instead of it.
OPENING_CHARS = config.env_int("DIABLO_OPENING_CHARS", 300, low=40, high=2000)
EVIDENCE_CHARS = config.env_int("DIABLO_EVIDENCE_CHARS", 400, low=80, high=2000)
TASK_SOURCES = config.env_int("DIABLO_TASK_SOURCES", 8, low=1, high=40)
TASK_FINDINGS = config.env_int("DIABLO_TASK_FINDINGS", 6, low=1, high=30)
TASK_FINDING_CHARS = config.env_int("DIABLO_TASK_FINDING_CHARS", 400, low=80, high=2000)
TASK_RESULT = config.env_int("DIABLO_TASK_RESULT", 600, low=100, high=4000)
WORKING_STEPS = config.env_int("DIABLO_WORKING_STEPS", 12, low=2, high=40)
WORKING_RESULT = config.env_int("DIABLO_WORKING_RESULT", 600, low=100, high=4000)
WORKING_BUDGET = config.env_int("DIABLO_WORKING_BUDGET", 1800, low=200, high=8000)

# Character comes from persona.py so it can be retuned from .env without
# touching code. The mechanics of speaking aloud stay here, because they are not
# a matter of taste — they are what makes a reply usable through a speaker.
PERSONA = persona.style_block() + """

You are speaking out loud while he works, so everything you write is heard
rather than read. One paragraph, no lists, no headings.

WHAT YOU KNOW, IN FULL
Two sources, and this is the complete list:
  1. what he has said to you in this conversation
  2. the screen text attached above, if any was attached this turn

That is everything. In this mode you have no tools, no files, no inbox, no
calendar, no analytics and no memory of previous days. You cannot open
anything, change anything or send anything, so nothing has been done.

That makes two kinds of answer honest. If it is in those two sources, answer
it. If it is not, say you do not know and offer to go and look — "no idea,
want me to check?" is a complete answer. Any status, number, date or
percentage that did not come from those two sources is one you made up, and
he cannot tell the difference from the other side of a speaker.

The same goes for actions. "Want me to open it?" is right. "I've opened it"
is a lie, because you have no way to open anything from here.
"""


# Older turns are summarised rather than dropped.
#
# This used to be `self.turns = self.turns[-MAX_TURNS * 2:]` — the oldest half
# of a long conversation simply vanished, so twenty minutes in, "what did I ask
# you about that first" had no answer and the model would cheerfully invent
# one. A one-paragraph note costs about 90 tokens and keeps the thread.
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}

SUMMARY_PROMPT = (
    "Compress this earlier part of a spoken conversation into at most three "
    "short sentences, written in the third person: what he asked about, what "
    "was decided, and anything named that later turns might refer back to — "
    "files, people, numbers.\n"
    "Use only what is in the transcript. Do not add, guess or conclude "
    "anything that is not written there. If there is nothing worth keeping, "
    "return an empty string.\n"
    "Reply with JSON only."
)


# Tools whose results carry provenance, and what to take from each.
# Both the registered tool name and the underlying function name, because the
# two differ for search and the mismatch is silent: a name not in this set
# simply contributes no evidence, and the follow-up comes back empty with
# nothing anywhere saying why. test_architecture asserts every registered name
# here still exists.
_SOURCE_TOOLS = {"research", "web_search", "search_web", "read_page",
                 "search_wikipedia", "wiki", "browse"}
_NUMBER_TOOLS = {"python_snippet", "solve", "calculate"}
_URL = re.compile(r"https?://[^\s,)\]\"']+")


_KNOWN_KINDS = (
    (".gov", "government"), (".gov.uk", "government"), (".gov.in", "government"),
    ("wikipedia.org", "reference"), ("docs.", "official documentation"),
    ("support.", "official documentation"), ("developer.", "official documentation"),
    ("reddit.com", "community discussion"), ("quora.com", "community discussion"),
    ("stackoverflow.com", "community discussion"), (".ac.uk", "academic"),
    (".edu", "academic"), ("arxiv.org", "academic"),
)


def _provider_of(url: str) -> str:
    """
    A rough kind for the source, for §19.

    Not a ranking and not a truth claim — a government page can be out of date
    and a forum post can be right. It is kept so that a disagreement between
    two sources can be described with something more useful than "one page said
    otherwise".
    """
    lowered = (url or "").lower()
    for marker, kind in _KNOWN_KINDS:
        if marker in lowered:
            return kind
    return "web"


def _near(text: str, url: str, width: int) -> str:
    """The sentence the URL was sitting in."""
    position = (text or "").find(url)
    if position < 0:
        return ""
    start = max(0, position - width)
    return " ".join(text[start:position + len(url) + width].split())


def _title_near(text: str, url: str) -> str:
    return _near(text, url, 60)[:120]


def _evidence_near(text: str, url: str) -> str:
    """What the source actually said, as far as we saw it."""
    return _near(text, url, 200)[:EVIDENCE_CHARS]


def _absorb_observation(record: Dict[str, Any], tool: str, reading: Any) -> None:
    """
    Keep what perception established, as evidence rather than as a sentence.

    Read with getattr throughout so this module does not depend on the
    perception types. It stores what the reading actually said — which control,
    how sure, which instruments, and which frame it came from — so a follow-up
    is answered from the observation instead of from the wording of the reply.

    Frame identity is kept for the same reason the source list keeps
    `retrieved_at`: an observation is a statement about one screen at one
    moment, and a later question has to be able to tell whether that moment
    has passed.
    """
    try:
        verdict = str(getattr(reading, "verdict", "") or "")
        best = getattr(reading, "best", None)
        unreadable = list(getattr(reading, "unreadable", []) or [])

        if best is None:
            note = ("could not read the screen (" + ", ".join(unreadable) + ")"
                    if unreadable else "looked at the screen and found nothing")
            record["findings"].append(f"{tool}: {note}")
            record["observations"].append({
                "tool": tool, "verdict": verdict or "not_found",
                "label": "", "confidence": None, "sources": [],
                # Carried here too. The first version dropped it in this branch
                # and only in this branch, which is the one where it decides
                # the answer: without it, "I could not see the screen" was
                # rendered back as "I looked and found nothing".
                "unreadable": unreadable,
                "observed_at": time.time(),
            })
            return

        sources = sorted(set(getattr(best, "sources", []) or []))
        frame = {}
        note = (getattr(reading, "observation", None) or {}).get("ocr") or {}
        if isinstance(note, dict) and isinstance(note.get("frame"), dict):
            seen = note["frame"]
            frame = {
                "window_id": seen.get("window_id"),
                "app": seen.get("app"),
                "title": seen.get("title"),
                "fingerprint": list(seen.get("fingerprint") or [])[:16],
                "display_scale": seen.get("display_scale"),
                "captured_at": seen.get("captured_at"),
            }

        record["observations"].append({
            "tool": tool,
            "verdict": verdict,
            "label": str(getattr(best, "label", "") or "")[:80],
            "role": str(getattr(best, "role", "") or ""),
            "confidence": round(float(getattr(best, "confidence", 0.0)), 3),
            "source_confidence": dict(getattr(best, "source_confidence", {}) or {}),
            "sources": sources,
            "evidence": list(getattr(best, "evidence", []) or [])[:4],
            "bounds": {"x": getattr(best, "x", 0.0), "y": getattr(best, "y", 0.0),
                       "width": getattr(best, "width", 0.0),
                       "height": getattr(best, "height", 0.0)},
            "app": str(getattr(best, "app", "") or ""),
            "ambiguous": bool(getattr(reading, "ambiguous", False)),
            "margin": round(float(getattr(reading, "margin", 0.0)), 3),
            "unreadable": unreadable,
            "frame": frame,
            "observed_at": time.time(),
        })
        record["findings"].append(
            f"{tool}: {verdict} — {getattr(best, 'label', '') or 'a control'} "
            f"at {int(getattr(best, 'x', 0))},{int(getattr(best, 'y', 0))} "
            f"({', '.join(sources) or 'unknown source'}, "
            f"{float(getattr(best, 'confidence', 0.0)):.2f})")
    except Exception:
        # Evidence keeping must never break the turn that produced it.
        pass


def _absorb(record: Dict[str, Any], step: Dict[str, str]) -> None:
    """
    Pull the durable part out of one tool result.

    Sources, findings and numbers survive. Everything else — the arguments, the
    full page text, the listings that led nowhere — is dropped here rather than
    carried around for the rest of the session.
    """
    tool = str(step.get("tool", ""))
    result = str(step.get("result", ""))

    # A structured perception result, if the tool produced one. Read before the
    # error check, because a reading that could not see the screen is exactly
    # the thing worth remembering: "I could not look" and "I looked and found
    # nothing" are different answers to "why did you say that?".
    observed = step.get("observation")
    if observed is not None:
        _absorb_observation(record, tool, observed)

    if not result or result.startswith(("Error", "Refused", "Cancelled")):
        return
    if tool in _SOURCE_TOOLS:
        seen = {s["url"] for s in record["sources"]}
        # The arguments as well as the result. read_page's source is the URL it
        # was asked for, which never appears in the text it returns.
        for url in (_URL.findall(str(step.get("args", "")))
                    + _URL.findall(result))[:6]:
            url = url.rstrip(".,;")
            if url not in seen:
                seen.add(url)
                # §15: enough to answer "where did this come from, what did it
                # actually say, and when did we get it". The id is what a
                # follow-up refers to — "check source two" has to mean
                # something stable.
                record["sources"].append({
                    "source_id": len(record["sources"]) + 1,
                    "url": url[:200],
                    "title": _title_near(result, url),
                    "provider": str(step.get("provider", "")) or _provider_of(url),
                    "retrieved_at": time.time(),
                    # When the URL came from the arguments — read_page asking
                    # for a page — the whole result is what that page said, and
                    # there is nothing to find "near" it in the text.
                    "evidence": (_evidence_near(result, url)
                                 or " ".join(result.split())[:EVIDENCE_CHARS]),
                })
        first = " ".join(result.split())[:TASK_FINDING_CHARS]
        if first:
            record["findings"].append(first)
    elif tool in _NUMBER_TOOLS:
        record["calculations"].append(
            f"{step.get('args', '')[:80]} -> {' '.join(result.split())[:160]}")
    elif tool == "read_the_question":
        record["findings"].append(" ".join(result.split())[:TASK_FINDING_CHARS])


class Session:
    """One rolling conversation. Not thread-safe by design — there is one voice."""

    def __init__(self) -> None:
        self.turns: List[Dict[str, str]] = []
        self.earlier: str = ""        # the summarised part, if any
        # The first thing he said, kept word for word and never summarised.
        #
        # Measured: with the opening turn left to the summariser, "what was the
        # original objective?" was answered wrongly at every level of session
        # length tested — 0 for 5 — and three of those answers were invented
        # outright ("to discuss the weather", "to generate a list of names").
        # The summary prompt already forbids that in those words and the 4B did
        # it anyway, which is the whole argument for putting the guarantee in
        # code. An opening turn is small and disproportionately the objective,
        # so it is simply kept.
        self.opening: str = ""
        self.last_spoke_at: float = 0.0
        self.last_screen_app: Optional[str] = None
        # Who is being spoken to. None is Diablo. Set when a job is handed to
        # one agent, so "and what about the other one" reaches whoever just
        # answered rather than starting again with the orchestrator.
        self.focus: Optional[str] = None
        # What each agent actually did to get its answer, keyed by agent id.
        #
        # Deliberately not in `turns`. Turns are sent to the model on every
        # single exchange and rendered in the window, and a research task's
        # working is thousands of characters against a 4096-token context — it
        # would push the conversation itself out of the window to answer a
        # question he has not asked yet. This sits to one side and is pulled in
        # only when he asks how the answer was reached.
        # Task state, keyed by task id. Structure, not a transcript: what was
        # established and where it came from. See record_task.
        self.tasks: Dict[str, Dict[str, Any]] = {}

    def _summarise_oldest(self) -> None:
        """
        Fold the oldest half of the conversation into `earlier`.

        Runs on the turn that overflows, which is a real cost — measured at
        about 700ms on the 4B — but it happens once every twelve exchanges and
        the alternative is losing the first half of a conversation silently.
        On any failure the old behaviour applies and the turns are dropped,
        because a conversation that keeps growing is worse than one that
        forgets.
        """
        keep = MAX_TURNS
        old, self.turns = self.turns[:-keep], self.turns[-keep:]
        if not old:
            return
        transcript = "\n".join(
            f"{'He' if t['role'] == 'user' else 'You'}: {t['content']}" for t in old)
        if self.earlier:
            transcript = f"(Even earlier: {self.earlier})\n{transcript}"
        try:
            from skills import local_brain
            result = local_brain.chat_json(
                [{"role": "system", "content": SUMMARY_PROMPT},
                 {"role": "user", "content": transcript[:4000]}],
                schema=SUMMARY_SCHEMA, num_predict=180)
            summary = str(result.get("summary") or "").strip()
        except Exception:
            summary = ""
        # A summary longer than what it replaced is not a summary.
        if summary and len(summary) < len(transcript):
            self.earlier = summary

    def _expire_if_stale(self) -> None:
        if self.last_spoke_at and time.time() - self.last_spoke_at > SESSION_TIMEOUT:
            self.turns = []
            self.earlier = ""
            self.opening = ""
            self.last_screen_app = None
            self.focus = None
            self.tasks = {}

    def remember(self, role: str, content: str) -> None:
        if role == "user" and not self.opening and content.strip():
            self.opening = content.strip()[:OPENING_CHARS]
        self.turns.append({"role": role, "content": content})
        if len(self.turns) > MAX_TURNS * 2:
            self._summarise_oldest()
        self.last_spoke_at = time.time()

    def record_task(self, agent_id: str, title: str,
                    steps: List[Dict[str, str]], result: str = "",
                    task_id: str = "") -> Dict[str, Any]:
        """
        Keep what a task actually established, as structure rather than as a
        transcript of the model thinking.

        The first version of this stored the raw tool calls and replayed them
        into the prompt on a follow-up. That works and it is the wrong shape:
        it makes answering "where did you get that?" depend on keeping the
        model's own working, it grows without limit, and most of what it keeps
        is noise — the arguments of a listing, the text of a page that turned
        out to be irrelevant.

        What a follow-up actually needs is what was established and where it
        came from: the sources, the findings, the numbers. Those are extracted
        here, once, at the point where the tool results are still structured,
        and the raw calls are dropped.
        """
        record = self.tasks.get(task_id or agent_id) or {
            "task_id": task_id or agent_id,
            "agent": agent_id,
            "title": (title or "")[:120],
            "objective": (title or "")[:400],
            "status": "running",
            "created_at": time.time(),
            "sources": [],
            "findings": [],
            "calculations": [],
            # What perception established, kept beside the web sources rather
            # than mixed into them: a control on screen is not a citation.
            "observations": [],
            "result": "",
        }
        for step in steps or []:
            _absorb(record, step)
        if result:
            record["result"] = result[:TASK_RESULT]
            record["status"] = "done"
        record["updated_at"] = time.time()
        record["sources"] = record["sources"][:TASK_SOURCES]
        record["findings"] = record["findings"][-TASK_FINDINGS:]
        record["calculations"] = record["calculations"][-TASK_FINDINGS:]
        record.setdefault("observations", [])
        record["observations"] = record["observations"][-TASK_FINDINGS:]
        self.tasks[record["task_id"]] = record
        self.last_spoke_at = time.time()
        return record

    def task_for(self, agent_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """The most recent task this agent ran, or None."""
        mine = [t for t in self.tasks.values() if t["agent"] == agent_id]
        return max(mine, key=lambda t: t.get("updated_at", 0)) if mine else None

    def evidence_for(self, agent_id: Optional[str]) -> str:
        """That task's provenance, written out for a prompt, or empty."""
        record = self.task_for(agent_id)
        if not record:
            return ""
        lines = [f"Task: {record['title']} ({record['status']})"]
        if record["sources"]:
            lines.append("Sources actually opened, numbered:")
            for source in record["sources"]:
                lines.append(
                    f"  [{source.get('source_id', '?')}] {source['url']}"
                    f" ({source.get('provider', 'web')})")
                if source.get("evidence"):
                    lines.append(f"      said: {source['evidence']}")
        else:
            # §17. The honest answer to "which source supports that" when
            # there is none, said plainly so the model has no gap to fill.
            lines.append("No sources were opened for this task. If he asks "
                         "which source supports something, say there is none.")
        for seen in record.get("observations") or []:
            age = time.time() - float(seen.get("observed_at") or 0)
            # Said plainly, with its age, because a reading is a statement
            # about one screen at one moment. Whether it still holds is a
            # question about now, and the answer must not be assumed from the
            # fact that the record exists.
            if seen.get("label"):
                lines.append(
                    f"Seen on screen {int(age)}s ago: {seen['label']!r} "
                    f"({seen.get('verdict', '?')}, "
                    f"{', '.join(seen.get('sources') or []) or 'unknown source'}, "
                    f"confidence {seen.get('confidence')})")
                for why in (seen.get("evidence") or [])[:2]:
                    lines.append(f"      because {why}")
            else:
                lines.append(
                    f"Looked at the screen {int(age)}s ago and "
                    f"{'could not read it' if seen.get('unreadable') else 'found nothing'}.")
            lines.append("      That was the screen then, not necessarily now. "
                         "Look again before saying it is still there.")
        if record["findings"]:
            lines.append("What came back:")
            lines += [f"  {f}" for f in record["findings"]]
        if record["calculations"]:
            lines.append("Worked out:")
            lines += [f"  {c}" for c in record["calculations"]]
        if record["result"]:
            lines.append(f"Reported: {record['result']}")
        return "\n".join(lines)[:WORKING_BUDGET]

    def reset(self) -> None:
        self.turns = []
        self.earlier = ""
        self.opening = ""
        self.last_screen_app = None
        self.focus = None
        self.tasks = {}

    def is_followup(self) -> bool:
        """True if he is still mid-conversation rather than starting one."""
        self._expire_if_stale()
        return bool(self.turns)

    def reply(self, text: str, screen: Optional[str] = None) -> str:
        """
        Answer one conversational turn.

        `screen` is pre-fetched window context, or None. It is passed as a
        separate system message rather than glued onto his sentence, so the model
        does not start quoting the screen back at him as if he had said it.
        """
        self._expire_if_stale()

        # No history and a question about earlier: answer honestly without
        # asking the model, which will otherwise invent the thing being
        # referred to.
        if not self.turns and _asks_about_earlier(text):
            answer = random.choice(NO_HISTORY)
            self.remember("user", text)
            self.remember("assistant", answer)
            return answer

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": PERSONA},
        ]
        # Whoever is being spoken to. Their manner goes in after the shared
        # rules, never instead of them: an agent may sound different, but the
        # part about not inventing things applies to all of them equally.
        if self.focus:
            try:
                from skills import roster
                manner = roster.voice_of(self.focus)
                if manner:
                    messages.append({"role": "system", "content": manner})
            except Exception:
                pass
        messages += [
            {"role": "system", "content": time.strftime(
                "Background fact, for your own reference only: the current date "
                "and time is %A %d %B %Y, %H:%M. Do not say the date or the "
                "time back to him and do not open with it — he can see a clock. "
                "Use it only to pick the right greeting for the hour, to read "
                "words like 'tomorrow' correctly, and if he asks outright.")},
        ]
        if screen:
            messages.append({
                "role": "system",
                "content": ("What is on his screen right now, read by OCR. OCR "
                            "makes character errors, so read it for sense and say "
                            "so if it is unreadable. He has not typed any of "
                            f"this to you.\n\n{screen}"),
            })
        # An empty history looks identical to a forgotten one from inside the
        # model: it sees a question about "earlier" and no earlier, and fills
        # the hole. Asked "what's that file called again" with no prior turns it
        # invented a plausible filename, which is the kind of answer he would
        # act on. Say plainly that there is nothing behind him.
        if not self.turns:
            messages.append({"role": "system", "content":
                "This is the first thing he has said. There is no earlier "
                "conversation, nothing was discussed before now, and you have "
                "no memory of previous sessions. If he refers to something "
                "earlier — what he said, what you decided, a file he mentioned "
                "— you genuinely do not have it. Say so and ask him to remind "
                "you. Do not reconstruct it."})
        # How the last answer was actually arrived at, when he is asking about
        # that and not about something new.
        #
        # Pulled in on the question rather than kept in the history, which is
        # what makes it affordable: a research run's working is thousands of
        # characters and the context is 4096 tokens, so carrying it on every
        # turn would evict the conversation to answer a question he may never
        # ask. Here it costs nothing until he asks, and then it is the only
        # thing he wants.
        if self.focus and asks_how_you_know(text):
            notes = self.evidence_for(self.focus)
            if notes:
                messages.append({"role": "system", "content":
                    "He is asking how you reached your answer. This is what you "
                    "actually did — the calls you made and what came back. Answer "
                    "from it and name the real sources. Anything not here, you did "
                    "not do, so say so rather than filling the gap.\n\n" + notes})
            else:
                messages.append({"role": "system", "content":
                    "He is asking how you reached your answer and you have no "
                    "record of the working. Say plainly that you cannot retrace "
                    "it rather than reconstructing something plausible."})

        # The part of the conversation that has already been folded down. It
        # goes in as its own system message rather than as a fake turn, so the
        # model cannot quote it back as something one of them actually said.
        # Before the summary, because it outranks it: this is what he said,
        # not what a 4B made of what he said.
        if self.opening and len(self.turns) > 4:
            messages.append({"role": "system", "content":
                "The first thing he said in this conversation, word for word. "
                "If he asks what he was working on, what the objective was, or "
                "why he started, answer from this and not from the summary "
                "below.\n\n" + self.opening})

        if self.earlier:
            messages.append({"role": "system", "content":
                "Earlier in this same conversation, summarised. Treat it as "
                "your own recollection, not as something he said just now, and "
                "do not repeat it back to him unless he asks.\n\n"
                + self.earlier})
        messages.extend(self.turns)
        messages.append({"role": "user", "content": text})

        try:
            message = local_brain.chat(messages, num_predict=400, temperature=0.5)
        except local_brain.BrainOffline as e:
            # e.spoken says which of the two it was. "Ollama has stopped
            # responding" is a lie when the daemon answered perfectly and
            # refused the prompt for being too long — and it sends him looking
            # at the wrong thing.
            return e.spoken

        answer = _speakable((message.get("content") or "").strip())
        if not answer:
            answer = "I have nothing useful to say to that, Sir."
        elif _invents_a_status(answer, text, screen):
            answer = random.choice(_UNKNOWN)

        self.remember("user", text)
        self.remember("assistant", answer)
        return answer


# Questions that only make sense if something was said before. With an empty
# history there is no honest answer to any of them, but a 14B model asked
# "what's that file called again" will produce a filename rather than a shrug —
# observed output included "IB2027ResearchDraft.docx", which does not exist and
# which he could easily have gone looking for.
#
# Three prompt revisions did not stop it. The model is not reasoning about
# whether it knows; it is completing a pattern. So this is enforced in code
# instead, the same way the sign-off filler is: when there is no history and he
# is plainly asking about earlier, the model never sees the question.
BACKREFERENCE = re.compile(
    r"\b(again|earlier|last time|before|previously)\b"
    r"|\bremind me\b"
    r"|\b(we|you|i)\s+(said|told|decided|agreed|discussed|mentioned|talked about)\b"
    r"|\bwhat\s+(was|were)\s+(that|those|it|i|we|the)\b"
    r"|\bwhat\s+did\s+(i|we|you)\b",
    re.IGNORECASE)

NO_HISTORY = [
    "We haven't spoken yet, Sir, so I have nothing to go back to. Remind me?",
    "This is the first thing you've said to me. What was it?",
    "Nothing behind us yet — I've no memory of earlier sessions. Tell me again?",
    "I don't have that, Sir. We're starting fresh. What was it?",
]


# ─── Claims about things it cannot see ───────────────────────────────────────
#
# Conversation mode has exactly two sources: what he has said in this session,
# and the screen text attached this turn. Anything else is invention.
#
# The prompt says so plainly and that is not sufficient on a 4B. Asked "how is
# Brew and Critics doing", with no screen attached and no prior mention, it
# answered: "running smoothly, no issues on the backend, and the latest
# review..." — fluent, confident, and entirely made up. He has no way to tell.
#
# So the reply is checked against the two sources. A confident present-tense
# claim about a named thing that appears in neither is replaced with the honest
# answer. This is narrow on purpose: it only fires on a claim about a proper
# noun that was never mentioned, which is the shape of the failure.

_STATUS_CLAIM = re.compile(
    r"\b(is|are|has|have|was|were|seems?|looks?|remains?)\s+"
    r"(running|going|doing|performing|sitting|tracking|trending|up|down|fine|"
    r"good|great|well|smooth\w*|quiet|busy|steady|stable|on track|behind|ahead|"
    r"complete\w*|finish\w*|ready|live|active|stalled|delayed)\b",
    re.IGNORECASE)

_PROPER_NOUN = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+(?:and\s+)?[A-Z][a-z]{2,})*)\b")

_UNKNOWN = [
    "I've no way to check that from here, Sir. Want me to go and look?",
    "No idea — that isn't something I can see. Shall I find out?",
    "I can't see that from where I am. Want me to check?",
    "Not something I have in front of me, Sir. I can look if you like.",
]


def _invents_a_status(reply: str, asked: str, screen: Optional[str]) -> bool:
    """
    True when the reply reports the state of something it has no access to.

    Narrow by design. It needs all three: a present-tense status phrase, a
    proper noun carrying it, and that noun appearing in neither the
    conversation so far nor the screen text.
    """
    if not _STATUS_CLAIM.search(reply):
        return False

    # The question is deliberately not a source. He names the thing he is
    # asking about — "how is Brew and Critics doing" — and naming it is not
    # telling Diablo how it is doing. Counting the question let every invented
    # status through, because the subject was always present in it.
    known = " ".join(turn["content"] for turn in session.turns).lower()
    known += " " + (screen or "").lower()

    for noun in _PROPER_NOUN.findall(reply):
        if noun.lower() in ("sir", "i", "diablo"):
            continue
        if noun.lower() not in known:
            return True
    return False


def _asks_about_earlier(text: str) -> bool:
    """
    True only when he is asking Diablo to recall, not merely using a past word.

    The first version fired on any sentence containing "again", which caught
    "my launch got pushed again" — a statement, answered with "we haven't
    spoken yet, so I have nothing to go back to". A back-reference has to be a
    question or an instruction to remember, not a word.
    """
    if not BACKREFERENCE.search(text):
        return False
    stripped = text.strip()
    if stripped.endswith("?"):
        return True
    # The trailing \b meant "whats that file called again" did not match, because
    # the character after "what" is a letter. Spoken transcription drops
    # apostrophes constantly, so the contracted forms have to be spelled out.
    return bool(re.match(
        r"\s*(what'?s?|who'?s?|when|where|which|how'?s?|why|did|do|does|had|"
        r"was|were|remind|tell me|say|repeat)\b",
        stripped, re.IGNORECASE))


def _speakable(text: str) -> str:
    """
    Flatten a reply into something a speaker can deliver.

    Markdown and line breaks survive a text chat and ruin a spoken one: a bullet
    is read as "asterisk", a paragraph break as a dead pause mid-thought.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*[-*#>]+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`#]", "", text)
    text = re.sub(r"\s*\n+\s*", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()

    # The "anything else?" sign-off survived every attempt to ban it in the
    # prompt, so it is removed here instead. Spoken aloud after every single
    # reply, it is the fastest way to make an assistant grating.
    text = re.sub(
        r"\s*(?:and\s+)?(?:just\s+)?(?:do\s+)?"
        r"(?:let me know|just let me know|feel free to ask|don'?t hesitate)"
        r"[^.!?]*[.!?]\s*$", "", text, flags=re.IGNORECASE)
    before = text
    text = re.sub(r"\s*(?:i'?m here if[^.!?]*|hope (?:that|this) helps"
                  r"|(?:is there |was there |need )?anything else[^.!?]*"
                  r"|happy to help[^.!?]*|what else can i[^.!?]*)[.!?]?\s*$",
                  "", text, flags=re.IGNORECASE)
    # Never let the strip take the whole reply. "Anything else?" as the entire
    # answer is rare, and a genuine question like "let me know what you think"
    # should survive.
    return (text or before).strip()


session = Session()


def look_if_asked(text: str) -> Optional[str]:
    """
    Read the screen, but only when the sentence actually points at it.

    Attaching OCR to every turn drowns a 14B model in text it did not need and
    makes it answer questions nobody asked.
    """
    if not screen_context.wants_screen(text):
        return None

    target = ""
    lowered = text.lower()
    for phrase, window in [
        ("chrome", "chrome"), ("browser", "browser"), ("tab", "browser"),
        ("code", "code"), ("terminal", "terminal"), ("excel", "excel"),
        ("spreadsheet", "spreadsheet"), ("word", "doc"), ("email", "email"),
    ]:
        if phrase in lowered:
            target = window
            break

    context = screen_context.look(target)
    if context.startswith(("Error:", "Refused:")):
        return context
    session.last_screen_app = context.splitlines()[0] if context else None
    return context


# Him asking how an answer was reached, rather than asking something new.
#
# Kept narrow on purpose. Every phrase here has to be about the provenance of
# something already said — "where did you get that", "what did you read". A
# looser list would pull a research task's whole working into the prompt on any
# turn containing the word "how", which is most of them.
HOW_YOU_KNOW = (
    "where did you get", "where did that come from", "where does that come from",
    "where did you find", "how do you know", "how did you know",
    "what makes you say", "what are your sources", "what was your source",
    "what did you read", "what did you look at", "which sites",
    "what did you search", "how did you work that out", "how did you get that",
    "walk me through your", "show your working", "show me your working",
    "show me how you", "what did you do to", "how did you do that",
    "can you back that up", "prove it", "says who", "source for that",
    "cite that", "how sure are you", "did you check",
)


def asks_how_you_know(text: str) -> bool:
    """True when he is asking about the working, not asking a new question."""
    lowered = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()) + " "
    lowered = re.sub(r"\s+", " ", lowered)
    return any(phrase in lowered for phrase in HOW_YOU_KNOW)


def addressed_to(text: str) -> Optional[str]:
    """
    Which agent he just named, if he named one.

    Only a leading address or an explicit "ask X" counts. Mentioning Marbas in
    passing — "what did Marbas say about that" — is a question for whoever he
    is already talking to, not a request to switch, and treating every mention
    as a switch makes the conversation hop around under him.
    """
    try:
        from skills import roster
    except Exception:
        return None
    lowered = text.strip().lower()
    for agent in roster.AGENTS:
        name = agent["name"].lower()
        if lowered.startswith(f"{name},") or lowered.startswith(f"{name} "):
            return agent["id"]
        for opener in (f"ask {name} ", f"tell {name} ", f"get {name} to ",
                       f"put {name} on ", f"switch to {name}",
                       f"talk to {name}", f"speak to {name}"):
            if lowered.startswith(opener) or f" {opener}" in f" {lowered}":
                return agent["id"]
    return None


def strip_address(text: str, agent_id: str) -> str:
    """Take the name off the front, so the agent is not asked about itself."""
    try:
        from skills import roster
        name = roster.BY_ID[agent_id]["name"]
    except Exception:
        return text
    pattern = re.compile(
        rf"^\s*(?:ask|tell|get|put|switch to|talk to|speak to)?\s*{re.escape(name)}\s*"
        rf"(?:,|to|on)?\s*", re.IGNORECASE)
    stripped = pattern.sub("", text, count=1).strip()
    return stripped or text


def talk(text: str, agent: Optional[str] = None) -> str:
    """
    One conversational turn, with the screen attached if he pointed at it.

    `agent` is who is being spoken to. Passed explicitly by the window when a
    name is selected in the rail; worked out from the sentence otherwise.
    """
    if agent is not None:
        session.focus = agent or None
    else:
        named = addressed_to(text)
        if named:
            session.focus = None if named == "diablo" else named
            text = strip_address(text, named)
    return session.reply(text, screen=look_if_asked(text))


RESET_PHRASES = ["forget that", "start over", "new conversation", "clear your head",
                 "never mind all that", "wipe that"]


def wants_reset(lower: str) -> bool:
    return any(p in lower for p in RESET_PHRASES)


if __name__ == "__main__":
    if not local_brain.available():
        print("Ollama is not running.")
        raise SystemExit(1)
    print(f"{ASSISTANT_NAME} — type to talk, Ctrl-C to stop.\n")
    try:
        while True:
            line = input("you > ").strip()
            if not line:
                continue
            if wants_reset(line.lower()):
                session.reset()
                print(f"{ASSISTANT_NAME.lower()} > Forgotten, Sir.\n")
                continue
            print(f"{ASSISTANT_NAME.lower()} > {talk(line)}\n")
    except (KeyboardInterrupt, EOFError):
        print()
