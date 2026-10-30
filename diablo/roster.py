"""
D.I.A.B.L.O. — the roster, and how work gets handed out.

Diablo is the one you talk to. When a job is bigger than a sentence it does not
do the job itself; it hands the pieces to named subagents and collects what
comes back. That is the whole idea, and it exists for one practical reason: a
queue of jobs all reporting as "Diablo" is indistinguishable from the Diablo you
are currently speaking to, so "what is it doing" has no answer.

The names are Goetic, picked for what each demon was actually said to do rather
than for sounding sinister:

    Asmo      Asmodeus, who knows things. Research and the web.
    Marbas    answers truly of hidden things; mechanical arts. Files and numbers.
    Vassago   declares things lost or hidden. Everything to do with the screen.
    Seere     goes anywhere in an instant. Errands on the machine itself.
    Levi      Leviathan, the deep one. The hard jobs, on the larger model.

── On safety ───────────────────────────────────────────────────────────────
The three internal classes this file replaces in the interface — the reader,
the writer and the guard — have NOT gone anywhere. They were never agents;
they were a way of drawing the permission gate. That gate lives in
agent_tools.CONTEXT["confirm"] and in the shell denylist, it runs on every
mutating call regardless of which name is on the job, and nothing here touches
it. Marbas deleting a file asks exactly the same question Warden asked.
`SAFETY_CLASS` below keeps the mapping for the trace log, where it is useful,
and out of the window, where it was only ever noise.
"""

import os
import re
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import config  # noqa: E402


# ─── The roster ──────────────────────────────────────────────────────────────
#
# `tools` is ownership, not permission. Every agent can reach every tool; this
# decides whose face goes on the job and who gets handed which half of a split.

AGENTS: List[Dict[str, Any]] = [
    {
        "id": "diablo", "name": "Diablo", "role": "The one you talk to",
        "accent": "#E0233C", "eye": "#F2C230", "mouth": "grin",
        "blurb": "Listens, decides, and hands the work out.",
        "tools": [],
        "keywords": [],
    },
    {
        "id": "asmo", "name": "Asmo", "role": "Finds things out",
        "accent": "#8B5CF6", "eye": "#D6BCFA", "mouth": "smirk",
        "blurb": "The web, encyclopaedias, and anything you want looked up.",
        "tools": ["web_search", "search_wikipedia", "open_url", "generate_image"],
        "keywords": ["search", "look up", "lookup", "google", "find out", "research",
                     "who is", "what is", "news", "wikipedia", "weather", "price of",
                     "online", "website", "article", "read about", "who won",
                     "results", "latest", "how much does", "definition of"],
        "voice": (
            "You are Asmo. You find things out. You give the answer first and stop "
            "— you do not preface it, and you do not offer three more things he did"
            " not ask about. You are quietly pleased to know it."
        ),
    },
    {
        "id": "marbas", "name": "Marbas", "role": "Papers and numbers",
        "accent": "#46C46B", "eye": "#B6EEC6", "mouth": "calm",
        "blurb": "Reads, writes, moves and totals whatever is on disk.",
        "tools": ["read_file", "write_file", "append_file", "move_file", "delete_file",
                  "find_files", "list_files", "search_text", "make_folder",
                  "python_snippet", "create_presentation"],
        "keywords": ["file", "folder", "document", "draft", "note", "csv", "spreadsheet",
                     "write", "rename", "move", "delete", "total", "sum", "average",
                     "calculate", "count", "deck", "slides", "presentation", "desktop",
                     "downloads", "documents"],
        "voice": (
            "You are Marbas. You handle his files and his arithmetic. You are exact"
            ": file names, paths and numbers come out the way they went in, never r"
            "ounded and never tidied up. You say plainly when something is not ther"
            "e."
        ),
    },
    {
        "id": "vassago", "name": "Vassago", "role": "Watches the screen",
        "accent": "#3DA9FC", "eye": "#8FD3FF", "mouth": "calm",
        "blurb": "Reads the glass, points at things, and walks you through them.",
        # No take_screenshot. Every tool here reads the screen and lets the
        # capture go; that one saves a picture of every display to the
        # Desktop, which delegated work should never do on its own. A person
        # who says "take a screenshot" is answered directly, not by an agent.
        "tools": ["read_screen", "explain_screen", "describe_visually", "list_windows",
                  "point_at", "find_on_screen",
                  "walk_me_through", "whats_under_cursor", "what_changed",
                  "look_near_cursor", "screen_layout", "screen_privacy",
                  "whole_document", "research_whats_on_screen"],
        "keywords": ["screen", "window", "this page", "on my screen", "show me where",
                     "point at", "highlight", "what am i looking at", "screenshot",
                     "tab", "visible", "in front of me", "help me with",
                     "show me how", "walk me through", "teach me", "how do i",
                     "explain this", "step by step"],
        "voice": (
            "You are Vassago. You watch his screen. You describe what is actually v"
            "isible and nothing behind it, and when you cannot make something out y"
            "ou say that rather than guessing at it."
        ),
    },
    {
        "id": "seere", "name": "Seere", "role": "Runs the errands",
        "accent": "#F2913D", "eye": "#FFD8A8", "mouth": "grin",
        "blurb": "Apps, music, volume, messages. In and out quickly.",
        "tools": ["open_app", "play_music", "play_youtube", "music_transport",
                  "set_volume", "set_brightness", "system_status", "run_shell",
                  "send_imessage", "send_whatsapp", "search_contact"],
        "keywords": ["open", "launch", "quit", "play", "pause", "skip", "volume",
                     "brightness", "message", "text", "whatsapp", "imessage", "call",
                     "battery", "disk space", "memory", "shell", "terminal"],
        "voice": (
            "You are Seere. You run errands on his machine and you are quick. Short"
            " answers. Usually it is already done by the time you speak."
        ),
    },
    {
        "id": "levi", "name": "Levi", "role": "The deep one",
        "accent": "#D93AA8", "eye": "#FFC2E9", "mouth": "flat",
        "blurb": "Wakes the larger model when the small one is out of its depth.",
        "tools": [],
        "keywords": [],
        "voice": (
            "You are Levi, the deep one, woken for the hard ones. You take the ques"
            "tion seriously and answer in fewer words than it deserves."
        ),
    },
]

BY_ID: Dict[str, Dict[str, Any]] = {a["id"]: a for a in AGENTS}

# Who is offered work by the splitter. Diablo runs the show and Levi is a
# state rather than a worker, so neither is ever assigned a piece.
WORKERS: Tuple[str, ...] = ("asmo", "marbas", "vassago", "seere")

DEFAULT = "marbas"

# The old internal names, kept for the trace log. See the note at the top:
# this is a description of the permission gate, not a routing decision.
SAFETY_CLASS: Dict[str, str] = {}
for _tool in ("read_screen", "explain_screen", "describe_visually", "list_windows",
              "take_screenshot", "point_at", "find_on_screen",
              # Every screen capability reads and nothing more. Clicking and
              # typing are deliberately absent: they are not tools an agent can
              # reach, they go through computer_use, which is off by default and
              # asks every time. Looking never implies touching.
              "whats_under_cursor", "what_changed", "look_near_cursor",
              "screen_layout", "screen_privacy", "whole_document",
              "research_whats_on_screen", "read_file",
              "list_files", "find_files", "search_text", "web_search",
              "search_wikipedia", "system_status", "search_contact"):
    SAFETY_CLASS[_tool] = "reads"
for _tool in ("write_file", "append_file", "move_file", "make_folder",
              "create_presentation", "generate_image", "open_app", "open_url",
              "play_music", "play_youtube", "music_transport", "set_volume",
              "set_brightness", "python_snippet"):
    SAFETY_CLASS[_tool] = "changes"
for _tool in ("delete_file", "run_shell", "send_imessage", "send_whatsapp"):
    SAFETY_CLASS[_tool] = "guarded"

_TOOL_OWNER: Dict[str, str] = {}
for _agent in AGENTS:
    for _tool in _agent["tools"]:
        _TOOL_OWNER[_tool] = _agent["id"]

# Every tool that looks at the screen: Vassago's, and the screenshot. An answer
# built on any of them can repeat what was on the glass, so the job store keeps
# the fact that it finished and not the answer.
SCREEN_TOOLS = frozenset(
    next(a for a in AGENTS if a["id"] == "vassago")["tools"]) | {"take_screenshot"}


def voice_of(agent_id: str) -> str:
    """
    How this agent talks. Manner only, never knowledge.

    Nothing in here tells an agent that it knows something, has access to
    something, or has already done something — every line is about register.
    That boundary is deliberate: a persona that says "you are an expert
    researcher" is an invitation to answer from the persona instead of from a
    tool result, and this project spent three rewrites getting that back out.
    """
    return (BY_ID.get(agent_id) or {}).get("voice") or ""


def public(agent_id: str) -> Dict[str, Any]:
    """One agent, without the routing internals the window has no use for."""
    a = BY_ID.get(agent_id) or BY_ID["diablo"]
    return {k: a[k] for k in ("id", "name", "role", "accent", "eye", "mouth", "blurb")}


def roster() -> List[Dict[str, Any]]:
    return [public(a["id"]) for a in AGENTS]


def agent_for_tool(tool: Optional[str]) -> str:
    return _TOOL_OWNER.get(tool or "", "diablo")


# ─── Assignment ──────────────────────────────────────────────────────────────

def _default_tools() -> set:
    """
    The tools select() offers when nothing in the task matched anything.

    Worth knowing, because those same tools also turn up as "matches" on tasks
    that did match — select() puts read_file first for "open spotify" — and
    they are much weaker evidence than a tool it reached for specifically.
    Derived by asking, rather than hardcoded, so it stays true if the selector
    changes.
    """
    global _DEFAULTS
    if _DEFAULTS is None:
        try:
            from skills import agent_tools
            _DEFAULTS = set(agent_tools.select("") or [])
        except Exception:
            _DEFAULTS = set()
    return _DEFAULTS


_DEFAULTS: Optional[set] = None


def _matched_tools(task: str) -> List[str]:
    """
    The part of agent_tools.select() that is actually about this task.

    select() appends a constant tail — ask_user, list_files, read_file — to
    every result. Those look like votes and are not. The matched tools are
    whatever comes before ask_user; if ask_user is first, nothing matched.
    """
    try:
        from skills import agent_tools
        selected = agent_tools.select(task) or []
    except Exception:
        return []
    try:
        cut = selected.index("ask_user")
    except ValueError:
        cut = len(selected)
    return selected[:cut]


def assign(task: str) -> str:
    """
    Whose face goes on this job.

    Worth being clear about the stakes: this decides attribution and who gets
    handed which half of a split. It does not decide capability. Every agent
    reaches the same 35 tools through the same permission gate, so a
    misassignment shows the wrong name on a card, not a job that cannot be
    done.

    Two sources, cheapest first. The tool selector already reads the task, so
    its ranking is better evidence than any keyword list — it is the same
    judgement the agent itself will act on. Votes decay with rank and halve for
    the selector's own fallbacks, because an unweighted count hands "look up
    who won the race" to the screen reader.
    """
    votes: Dict[str, float] = {}
    fallbacks = _default_tools()

    for rank, name in enumerate(_matched_tools(task)):
        owner = _TOOL_OWNER.get(name)
        if owner:
            weight = 1.0 / (1 + rank)
            if name in fallbacks:
                weight *= 0.5
            votes[owner] = votes.get(owner, 0.0) + weight

    lowered = " " + task.lower() + " "
    hit: set = set()
    for agent in AGENTS:
        for word in agent["keywords"]:
            if word in lowered:
                # The first keyword is worth slightly more than the top-ranked
                # tool. Later ones barely count: this is meant to be one strong
                # signal, not a word count, so a sentence saying "file" five
                # times does not run away with it.
                bonus = 0.15 if agent["id"] in hit else 1.1
                hit.add(agent["id"])
                votes[agent["id"]] = votes.get(agent["id"], 0.0) + bonus

    votes.pop("diablo", None)
    votes.pop("levi", None)
    if not votes:
        return DEFAULT
    return max(votes.items(), key=lambda kv: kv[1])[0]


# ─── Splitting ───────────────────────────────────────────────────────────────
#
# The only thing that makes fan-out worth doing is that the pieces are genuinely
# independent. Split "read the CSV and then email the total" and the second
# agent emails a total nobody computed. So the bar is deliberately high and
# every doubt resolves to one agent doing the whole thing.

MAX_PARTS = config.env_int("DIABLO_MAX_PARALLEL_AGENTS", 3, low=1, high=4)

# Words that say the second half depends on the first. Any of these and the
# task is sequential, whatever the model thinks.
_DEPENDENT = (
    " then ", " after that", " afterwards", " once you", " once that",
    " using the result", " using that", " with the result", " based on that",
    " and send it", " and email it", " and tell me the difference",
    " compare ", " combine ", " merge them", " same file", " it to ", " that to ",
)

# A split is only ever attempted on a sentence that looks like two errands.
_CONJUNCTIONS = (" and also ", " and ", ", also ", "; ", " plus ", " as well as ")

SPLIT_SCHEMA = {
    "type": "object",
    "properties": {"parts": {"type": "array", "items": {"type": "string"}}},
    "required": ["parts"],
}

# The model is asked to segment, and nothing else.
#
# The first version asked it to judge independence too, and it was bad at it:
# "look up the weather in Pune and tell me what's on my screen" came back
# independent=false with the screen half deleted. Two obviously separate
# errands, refused, and one of them silently dropped. Segmenting text is
# something a 4B does well; deciding whether one errand waits on another is
# reasoning, and that judgement is made below in code, from markers that do not
# vary with sampling.
SPLIT_PROMPT = (
    "Split the request into the separate errands it contains.\n"
    "Use the user's own words for each errand, including file names and paths, "
    "exactly as written. Do not add an errand. Do not leave one out. Do not "
    "rephrase.\n"
    "If the request is one errand, return it unchanged as the only part.\n"
    "Reply with JSON only."
)

_STOPWORDS = {
    "the", "and", "then", "also", "that", "this", "with", "from", "into", "your",
    "what", "when", "have", "just", "tell", "give", "make", "some", "them",
    "they", "will", "would", "should", "there", "here", "about", "please",
}


def _content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9_.\-/]{4,}", text.lower())
            if w not in _STOPWORDS}


def _covers(task: str, parts: List[str]) -> bool:
    """
    Every errand in the request must survive the split.

    This is the check that replaced the model's own independence verdict. A
    dropped half shows up as content words in the original that appear in no
    part, which is cheap to see and does not depend on the model admitting it.
    """
    wanted = _content_words(task)
    if not wanted:
        return False
    got = set()
    for part in parts:
        got |= _content_words(part)
    missing = wanted - got
    return len(missing) <= max(1, len(wanted) // 6)


def _looks_splittable(task: str) -> bool:
    lowered = " " + task.lower() + " "
    if len(task.split()) < 8:
        return False
    if any(marker in lowered for marker in _DEPENDENT):
        return False
    return any(c in lowered for c in _CONJUNCTIONS)


def _distinct(parts: List[str]) -> bool:
    """Two parts that are 80% the same text are one part the model repeated."""
    normal = [re.sub(r"[^a-z0-9 ]", "", p.lower()).split() for p in parts]
    for i in range(len(normal)):
        for j in range(i + 1, len(normal)):
            a, b = set(normal[i]), set(normal[j])
            if not a or not b:
                return False
            if len(a & b) / len(a | b) > 0.8:
                return False
    return True


def _why(reason: str, **fields) -> None:
    """
    Record why a split did or did not happen.

    Without this the answer to "why did that not fan out" is a guess: the
    structural gate, the model, the coverage check and the invention check all
    fail the same way, by quietly returning one piece.
    """
    try:
        from skills import trace
        trace.event("split", reason=reason, **fields)
    except Exception:
        pass


# The strongest conjunction wins: "and also" is almost always two errands,
# a bare "and" is often one ("bread and butter", "up and running").
_SPLIT_MARKERS = (" and also ", "; ", ", also ", " as well as ", " plus ", ", and ", " and ")


def _structural_split(task: str, min_words: int = 3) -> List[str]:
    """
    Two halves, cut at the strongest conjunction, with no model involved.

    This exists because the 4B will not do it. Asked to split "check how much
    disk space I have left and also tell me what window is in front of me" it
    returns an empty list, in under half a second, with complete confidence.
    The 14B splits the same sentence correctly every time. Since the small
    model is the default, a splitter that only works on the large one is a
    splitter that does not work.

    Cutting on a conjunction is crude, and it is allowed to be: the result goes
    through exactly the same coverage, distinctness and invention checks as the
    model's answer, and is thrown away if it fails any of them.
    """
    lowered = " " + task.lower() + " "
    for marker in _SPLIT_MARKERS:
        index = lowered.find(marker)
        if index < 1:
            continue
        # -1 for the space prepended to `lowered`.
        left = task[:index - 1].strip()
        right = task[index - 1 + len(marker):].strip()
        if len(left.split()) >= min_words and len(right.split()) >= min_words:
            return [left, right]
    return []


def structural_parts(task: str) -> List[str]:
    """
    The errands in a sentence, cut on conjunctions, or [].

    split() asks a different question — is this worth running several agents in
    parallel — and its gate is deliberately conservative, because splitting a
    single request in two and answering both halves separately is worse than
    not splitting it. Separating work that must be delegated from work that
    must not is not that question: "research this company and lower my volume"
    is one errand too few for a fan-out and one errand too many to hand to an
    agent whole. This applies the same cut without the fan-out gate.
    """
    # Two words is enough here, where three is the right floor for split().
    # The two serve different purposes: split() is deciding whether to run
    # several agents at once, and splitting a single request in half there is
    # worse than not splitting it. This is deciding which half is Diablo's, and
    # "open Downloads" and "pause music" are two-word errands that genuinely
    # must not be dragged into a research task. Both results still go through
    # the same coverage and distinctness checks below.
    parts = _structural_split(task, min_words=2)
    if len(parts) < 2:
        return []
    if not _covers(task, parts) or not _distinct(parts):
        return []
    return parts


def split(task: str) -> List[Tuple[str, str]]:
    """
    The task as a list of (subtask, agent_id).

    A single-item list is the normal answer and means "one agent, whole job".
    Anything longer has passed a structural test, a model judgement and three
    sanity checks, in that order.
    """
    if MAX_PARTS < 2:
        return [(task, assign(task))]
    if not _looks_splittable(task):
        _why("nothing to split", words=len(task.split()))
        return [(task, assign(task))]

    try:
        from skills import local_brain
        result = local_brain.chat_json(
            [{"role": "system", "content": SPLIT_PROMPT},
             {"role": "user", "content": task}],
            schema=SPLIT_SCHEMA, num_predict=200,
        )
    except Exception as e:
        _why("the splitter did not answer", error=f"{type(e).__name__}: {e}")
        return [(task, assign(task))]

    parts = [str(p).strip() for p in (result.get("parts") or []) if str(p).strip()]
    parts = parts[:MAX_PARTS]

    if len(parts) < 2:
        parts = _structural_split(task)
        if len(parts) < 2:
            _why("one errand", parts=len(parts))
            return [(task, assign(task))]
        _why("split on the conjunction, the model would not", parts=parts)
    if not _distinct(parts):
        _why("the pieces repeat each other", parts=parts)
        return [(task, assign(task))]
    if not _covers(task, parts):
        _why("a piece of the request went missing", parts=parts,
             missing=sorted(_content_words(task) - set().union(
                 *[_content_words(p) for p in parts])))
        return [(task, assign(task))]

    # Nothing may appear in a part that the user did not say. This catches a
    # model helpfully adding "and let me know how it went".
    said = _content_words(task)
    for part in parts:
        words = _content_words(part)
        invented = [w for w in words - said if not w.isdigit()]
        if len(invented) > max(3, len(words) // 3):
            _why("a piece said something the request did not", part=part,
                 invented=invented)
            return [(task, assign(task))]

    # Two halves that belong to the same agent are not worth separating: they
    # would run one after the other anyway, and a cut that lands inside a
    # single errand is exactly what a conjunction split gets wrong. This is
    # the check that makes the crude cut above safe to keep.
    if len({assign(p) for p in parts}) < len(parts):
        _why("the pieces are all the same agent's work", parts=parts)
        return [(task, assign(task))]

    assigned: List[Tuple[str, str]] = []
    taken: set = set()
    for part in parts:
        who = assign(part)
        # Two errands for the same agent would run one after the other, which
        # is the thing fan-out exists to avoid. Move the second to whoever else
        # is free rather than losing the parallelism.
        if who in taken:
            spare = [w for w in WORKERS if w not in taken]
            who = spare[0] if spare else who
        taken.add(who)
        assigned.append((part, who))
    _why("fanned out", agents=[a for _p, a in assigned],
         parts=[p for p, _a in assigned])
    return assigned


# ─── Running them at once ────────────────────────────────────────────────────
#
# One microphone, one speaker, one person. Whatever runs in parallel, only one
# agent may hold the user's attention at a time, so `ask` and `confirm` are
# serialised through a lock and the sub-agents are handed a silent `speak` that
# collects rather than talks. The merged answer is the only thing said out loud.

class Attention:
    """The user's attention, as a resource exactly one agent may hold."""

    def __init__(self, ask: Optional[Callable] = None,
                 confirm: Optional[Callable] = None):
        self._lock = threading.Lock()
        self._ask = ask
        self._confirm = confirm

    def ask_for(self, name: str) -> Optional[Callable]:
        if self._ask is None:
            return None

        def ask(question: str) -> str:
            with self._lock:
                return self._ask(f"{name} is asking. {question}")
        return ask

    def confirm_for(self, name: str) -> Optional[Callable]:
        if self._confirm is None:
            return None

        def confirm(question: str) -> bool:
            with self._lock:
                return self._confirm(f"{name} wants to {question}")
        return confirm


def run_parallel(
    pieces: List[Tuple[str, str]],
    runner: Callable[[str, str, Callable, Optional[Callable], Optional[Callable]], str],
    attention: Attention,
    on_start: Optional[Callable[[str, str], None]] = None,
    on_finish: Optional[Callable[[str, str, str, bool], None]] = None,
) -> List[Tuple[str, str, str]]:
    """
    Run every piece at once and return (agent_id, subtask, result) in the
    order they were handed out, not the order they finished. Order matters
    because the merged answer is read aloud and should follow the request.

    `on_finish` is told whether the agent finished (True) or failed (False).
    A failure still comes back as a sentence to be read out, but the record of
    it must not say done: the sentence alone was all that said otherwise.
    """
    results: List[Optional[Tuple[str, str, str]]] = [None] * len(pieces)
    threads: List[threading.Thread] = []

    def work(index: int, subtask: str, agent_id: str) -> None:
        name = BY_ID[agent_id]["name"]
        if on_start:
            try:
                on_start(agent_id, subtask)
            except Exception:
                pass
        collected: List[str] = []

        def quiet(text: str) -> None:
            # Sub-agents narrate into a list. Four voices at once is noise.
            if text:
                collected.append(str(text))

        ok = True
        try:
            answer = runner(subtask, agent_id, quiet,
                            attention.ask_for(name), attention.confirm_for(name))
        except Exception as e:
            ok = False
            answer = f"{name} could not finish that: {type(e).__name__}: {e}"
        results[index] = (agent_id, subtask, (answer or "").strip() or
                          (collected[-1] if collected else "Nothing came back."))
        if on_finish:
            try:
                on_finish(agent_id, subtask, results[index][2], ok)
            except Exception:
                pass

    for i, (subtask, agent_id) in enumerate(pieces):
        t = threading.Thread(target=work, args=(i, subtask, agent_id), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    return [r for r in results if r is not None]


def merge(results: List[Tuple[str, str, str]]) -> str:
    """
    One spoken answer from several.

    Deliberately not a model call. Summarising three answers with the same 4B
    that produced them is a fourth chance to invent something, and the parts are
    already short. Attribution is kept because "who found this" is the question
    the agents panel exists to answer.
    """
    if not results:
        return "Nothing came back, Sir."
    if len(results) == 1:
        return results[0][2]
    lines = []
    for agent_id, _subtask, answer in results:
        name = BY_ID.get(agent_id, BY_ID["diablo"])["name"]
        answer = answer.strip().rstrip(".")
        lines.append(f"{name}: {answer}.")
    return " ".join(lines)


def announce(pieces: List[Tuple[str, str]]) -> str:
    """What Diablo says before it goes quiet for a while."""
    names = [BY_ID[a]["name"] for _t, a in pieces]
    if len(names) == 1:
        return f"{names[0]} is on it, Sir."
    if len(names) == 2:
        return f"{names[0]} and {names[1]} are on it, Sir."
    return f"{', '.join(names[:-1])} and {names[-1]} are on it, Sir."


if __name__ == "__main__":
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
        for part, who in split(text):
            print(f"{BY_ID[who]['name']:>8}  {part}")
    else:
        for a in AGENTS:
            print(f"{a['name']:>8}  {a['role']:<24} {len(a['tools'])} tools")
