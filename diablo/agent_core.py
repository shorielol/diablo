"""
D.I.A.B.L.O. Agent Core
=======================
The loop that turns a sentence into finished work, running entirely on qwen3.5:4b
through Ollama. Nothing here touches the internet.

    understand()  →  is this actionable, ambiguous, or just conversation?
    plan()        →  a short checklist, because a small model drifts without one
    run()         →  think → call a tool → read the result → repeat → report

A 4B model is not a frontier model, and the whole design leans on that:

* It gets a short menu of tools, not all twenty-nine (agent_tools.select).
* It gets a written plan to follow rather than holding one in its head.
* It is told, in the system prompt and again in the tool description, that
  asking a question is a legitimate move and guessing is not.
* Repeated identical tool calls get interrupted rather than spinning.
* Every tool failure comes back as readable text so it can adapt instead of
  crashing the run.
"""

import json
import os

from skills import config  # noqa: E402
import re  # SIGNOFF_GUARD
import sys
import time
from typing import Any, Callable, Dict, List, Optional

# The skill registry loads this file by path, so its own folder is not
# necessarily importable. Put it there before reaching for its siblings.
# The package parent, so `from skills import x` resolves whether this module
# is imported as part of the package or run directly as a script. Importing
# bare `x` here instead created a SECOND copy of each module with its own
# globals, which is why patching local_brain.OLLAMA_HOST in one place had no
# effect in the other. One module, one instance.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import agent_tools  # noqa: E402
from skills import trace  # noqa: E402
from skills import local_brain  # noqa: E402
from skills.local_brain import BrainOffline  # noqa: E402

MAX_STEPS = config.env_int("DIABLO_MAX_STEPS", 14)
PLAN_THRESHOLD_WORDS = 6

Speak = Callable[[str], None]
Ask = Optional[Callable[[str], str]]
Confirm = Optional[Callable[[str], bool]]


# ─── Prompts ─────────────────────────────────────────────────────────────────

# ─── Tidying is not a research project ───────────────────────────────────────
#
# Asked to tidy the Downloads folder, Diablo opened a piece of his coursework
# and said "I'm reading the ESS IA - FDI and PM2.5 Final Draft.md file to
# understand the project's current state."
#
# Nothing in the prompt asked for that. It is the coding-agent reflex the model
# brings with it — orient in the repo, read the files, then act — and a
# personal folder is not a repo. Where a file goes is decided by its extension
# and nothing else, so opening it is useless, and it is somebody's private
# document, so opening it is worse than useless.
#
# A prompt line would not hold. The 4B ignores prompt lines, reproducibly and
# measurably — that is why _ambiguous_target and _claims_unperformed_action are
# code. This is the same shape of guard: the tools are taken away, and if it
# asks for one anyway it is told no.

HOUSEKEEPING = (
    "tidy", "tidy up", "clean up", "cleanup", "clear out", "organise",
    "organize", "sort out", "sort my", "sort the", "declutter", "de-clutter",
    "make space", "free up space", "spring clean",
)

# Tools that open a document and return what is inside it.
CONTENT_TOOLS = {"read_file", "search_text", "read_page"}


def is_housekeeping(task: str) -> bool:
    """Sorting a folder out, rather than working on what is in it."""
    lowered = " " + re.sub(r"[^a-z0-9 ]+", " ", (task or "").lower()) + " "
    lowered = re.sub(r"\s+", " ", lowered)
    if not any(f" {cue}" in lowered for cue in HOUSEKEEPING):
        return False
    # "tidy up the wording of my essay" is housekeeping by phrasing and not by
    # intent, and it genuinely does need the file open.
    about_a_file = ("essay", "draft", "wording", "paragraph", "sentence",
                    "notes", "writing", "code", "function", "script")
    return not any(f" {word}" in lowered for word in about_a_file)


def _system_prompt(plan: Optional[List[str]] = None,
                   tidying: bool = False) -> str:
    # What his folders are FOR is context for working on his projects, and
    # priming for a task that is not one. Told that 10 Academics holds the Econ
    # EE and the BM IA, then asked to tidy Downloads, the model went looking
    # for a project to understand. Left out, there is nothing to look for.
    workstations = "" if tidying else """His work lives in numbered workstations: \
10 Academics is IB coursework (the Econ
EE, the Coca-Cola BM IA, TOK), 30 Code is software, 50 Finance is spreadsheets
and markets, 20 Ventures is his businesses, 40 Studio is music and design.
"""
    housekeeping = "" if not tidying else """
- This is a tidy-up, not a piece of work. Sort by what a file IS — its name,
  extension, size and date. Never open one: what is inside a document has no
  bearing on which folder it belongs in, and these are his private papers.
  survey_folder shows you what is there; tidy_folder sorts it by kind."""
    base = f"""You are Diablo, Shaurya's voice assistant, running offline on his Mac.
You do not answer questions about tasks — you carry them out, using the tools you
have been given, and then report what you actually did.

Shaurya's folders:
{agent_tools.workspace_map()}

{workstations}
How to work:{housekeeping}
- Take one action at a time. Call a tool, read what came back, then decide the
  next move from what you actually saw.
- Never invent a file path. If you are not certain a file exists, use list_files
  or find_files and look.
- The moment a step depends on a choice you were not given — which of two files,
  which person, which of two readings of a sentence — call ask_user and wait.
  This is not optional and it is not a failure. Picking one and hoping is the
  failure. The user is speaking to you and cannot see what you decided.
- After listing a folder, if more than one file plausibly matches what was asked
  for, ask which one. Do not take the closest name.
- If a tool returns an error, read it and try a different approach. Do not repeat
  the same failing call.
- Some of your tools are named mcp__<server>__<tool>. They come from connected
  external systems and work exactly like the rest. If you need a capability you
  cannot see in your list, call list_capabilities before telling him you cannot
  do something.
- Never do arithmetic in your head. Not addition, not totals, not averages,
  not percentages, however small the numbers look. Read the values with
  read_file, then compute them with python_snippet and use what it prints.
  Doing it mentally has produced confidently wrong figures in his coursework
  and his budgets, and a wrong number that looks right is worse than no answer.
- Speech-to-text is imperfect. If a sentence is garbled or makes no sense, ask
  rather than inventing an interpretation of it.

When the job is done, reply with plain text and no tool call. That reply is read
aloud, so write one or two spoken sentences: what you did, where the result is,
and any assumption you made. Keep this report flat and factual whatever your
usual manner is — he needs to know exactly what happened to his files, and a
remark here costs him a re-read. No markdown, no bullet points, no file paths longer
than a filename."""

    if plan:
        checklist = "\n".join(f"  {i}. {s}" for i, s in enumerate(plan, 1))
        base += f"\n\nYour plan for this task:\n{checklist}\n\nWork through it in order."
    return base


UNDERSTAND_SCHEMA = {
    "type": "object",
    "properties": {
        # No "why" field ahead of "action", unlike the DO planner's schema.
        # Tried on qwen3.5:4b, 2026-10-05: 103 of 117 against 104 without it,
        # 14 unnecessary questions against 13, and P95 5.5s against 3.5s.
        "action": {"type": "string", "enum": ["act", "clarify", "chat"]},
        "task": {"type": "string"},
        "question": {"type": "string"},
        "reply": {"type": "string"},
        # The operations this request contains, in the order given. Rides on
        # the call that already happens, so it costs no extra round trip —
        # route.classify() calls understand() and always did.
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "intent": {"type": "string",
                               "enum": ["direct", "research", "agent"]},
                    "depends_on": {"type": "integer"},
                },
                "required": ["text", "intent"],
            },
        },
    },
    "required": ["action"],
}

UNDERSTAND_PROMPT = """You are the intake desk of a voice assistant. Classify one
transcribed sentence. The transcription may contain mis-heard words.

"act"     — the user wants something done AND you know what. Put a cleaned-up,
            self-contained instruction in "task". Repair obvious mis-hearings.
"clarify" — a detail you would have to invent is missing: which file, which
            person, which of two readings, or the sentence came through garbled.
            Put ONE short spoken question in "question".
"chat"    — greeting, thanks, small talk or a question answerable in one sentence
            from general knowledge. Put the spoken answer in "reply".

A pronoun with no antecedent ("it", "that one", "him") almost always means
clarify. So does a bare noun where the user owns several ("the file", "my essay",
"the spreadsheet"). Guessing on a voice command means acting on a sentence that
may have been misheard, and the user cannot see what you chose.

Also split the request into "operations", in the order the user gave them.

  "text"        that operation on its own, self-contained.
  "intent"      "direct"   — something you do yourself now: a setting, a file,
                             an app, a calculation, a question you can answer,
                             anything about this machine or this screen
                "research" — a real investigation needing several outside
                             sources, not a single lookup
                "agent"    — the user asked outright for an agent, or named one
  "depends_on"  the index of the operation whose result this one needs, if any.

Most requests are ONE operation. Do not invent operations, and do not split a
single instruction into steps — "tidy my downloads" is one operation, not three.

Examples:
  "open the file"                 -> clarify, "Which file, Sir?"
  "send it to him"                -> clarify, "Send what, and to whom?"
  "fix my essay"                  -> clarify, "Which essay, Sir?"
  "turn it up a bit"              -> act, "increase the system volume"
  "what's the total in budget.csv on my desktop" -> act
  "morning Diablo"                -> chat
  "who wrote Brave New World"     -> chat
  "what do you make of this"      -> chat   (he is asking your opinion, not
                                             giving you a job; you can see his
                                             screen when you answer)
  "what's on my screen"           -> act, "read the frontmost window and say
                                     what is on it"
  "read my chrome tab"            -> act
  "what does this error say"      -> act, "read the screen and report the error"

Reply with JSON only."""

FOLLOWUP_NOTE = """
You are mid-conversation with him. The turns just before this one are context.
A short remark that only makes sense as a continuation ("and the second one?",
"why though", "no, the other way round") is "chat", not "clarify" — you already
have the antecedent from what was just said."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
    "required": ["steps"],
}


# ─── Stages ──────────────────────────────────────────────────────────────────

# ─── The fast path ───────────────────────────────────────────────────────────
# Classification costs a full model turn, measured at 3.37s, and a great many
# utterances do not need one. A greeting is a greeting. "read my econ ee draft"
# is an instruction and nothing about it is in doubt.
#
# The rule is deliberately conservative: this may only fire when getting it
# wrong is impossible. Anything carrying a dangling reference — "send it to
# him", "open that", "fix the file" — is handed to the model so the strict
# clarify loop can ask, because that is exactly the case the slow path exists
# for. A fast path that guesses would undo the work that stopped it guessing.

FAST_GREETINGS = {
    "hello": "Hello, Sir.", "hi": "Hello, Sir.", "hey": "Yes, Sir?",
    "morning": "Good morning, Sir.", "good morning": "Good morning, Sir.",
    "good afternoon": "Good afternoon, Sir.", "good evening": "Good evening, Sir.",
    "goodnight": "Goodnight, Sir.", "good night": "Goodnight, Sir.",
    "thanks": "Any time, Sir.", "thank you": "Any time, Sir.",
    "cheers": "Any time, Sir.", "nice one": "Any time, Sir.",
    "how are you": "Running well, Sir. All local.",
    "you there": "Always, Sir.", "are you there": "Always, Sir.",
    "never mind": "As you wish, Sir.", "forget it": "As you wish, Sir.",
}

ACTION_VERBS = {
    "read", "open", "show", "list", "find", "search", "create", "make", "write",
    "delete", "remove", "move", "rename", "copy", "send", "tell", "check", "run",
    "close", "set", "turn", "add", "append", "count", "calculate", "compute",
    "summarise", "summarize", "draft", "rewrite", "fix", "debug", "audit",
    "organise", "organize", "tidy", "sort", "download", "install", "start",
}

# A reference with no antecedent. The whole point of the clarify loop.
DANGLING = {"it", "that", "this", "those", "these", "them", "they", "him",
            "her", "one", "ones", "there", "here"}

# Nouns generic enough that "the <noun>" could be any of several things.
GENERIC = {"file", "files", "document", "documents", "doc", "docs", "essay",
           "draft", "drafts", "code", "script", "sheet", "spreadsheet", "note",
           "notes", "thing", "things", "stuff", "folder", "project", "message"}

DETERMINERS = {"the", "my", "a", "an", "that", "this", "some"}

# Exact phrasings common enough to be worth naming outright.
PHRASE_SHORTCUTS = [
    (re.compile(r"^what(?:'s| is| does)?\s+(?:on\s+)?(?:my |the )?screen"),
     "read the frontmost window on screen and say what is on it"),
    (re.compile(r"^(?:read|check)\s+(?:my |the )?(?:screen|display)"),
     "read the frontmost window on screen and say what is on it"),
    (re.compile(r"^what(?:'s| is| am i)?\s+(?:i(?:'m)? )?(?:looking at|reading)"),
     "read the frontmost window on screen and say what is on it"),
    (re.compile(r"^what(?:'s| is)?\s+(?:in|on)\s+my\s+(?:chrome|browser|tab)"),
     "read the browser window and say what page is open and what it says"),
]


def fast_understand(text: str) -> Optional[Dict[str, Any]]:
    """
    Classify without the model, or return None to let the model decide.

    Returning None is always safe. Returning a wrong answer is not, so every
    rule here errs towards None.
    """
    stripped = text.lower().strip().rstrip(" .!?,")
    if not stripped:
        return None

    if stripped in FAST_GREETINGS:
        return {"action": "chat", "reply": FAST_GREETINGS[stripped]}

    for pattern, task in PHRASE_SHORTCUTS:
        if pattern.match(stripped):
            return {"action": "act", "task": task}

    tokens = stripped.split()
    if len(tokens) < 3 or tokens[0] not in ACTION_VERBS:
        return None

    # Any dangling reference and this is the model's problem, not ours.
    if any(token in DANGLING for token in tokens[1:]):
        return None

    rest = [t for t in tokens[1:] if t not in DETERMINERS]
    if not rest:
        return None
    # "read the file" — a verb and a generic noun is not an instruction yet.
    if len(rest) == 1 and rest[0] in GENERIC:
        return None

    return {"action": "act", "task": text.strip()}


# What the intake desk may write back, bounded by the grammar rather than by
# asking. A cleaned-up instruction is the sentence with mis-hearings repaired,
# not a brief: asked to "research the best budget mechanical keyboards under
# 100 dollars", the 4B wrote 1,500 characters of requirements nobody asked for
# ("as of 2024 or 2025", named review sites) until num_predict cut it off. That
# was 7.4 s of the 7.8 s before a research job was even acknowledged, and the
# truncated JSON was thrown away anyway. A maxLength in the schema makes the
# string close; a task that runs into it was being embellished, so the
# person's own words are used instead.
TASK_SLACK_CHARS = 40
SPOKEN_MAX_CHARS = 400


def _bounded_schema(text: str) -> Dict[str, Any]:
    limit = len(text) + TASK_SLACK_CHARS
    props = dict(UNDERSTAND_SCHEMA["properties"])
    props["task"] = {**props["task"], "maxLength": limit}
    props["question"] = {**props["question"], "maxLength": SPOKEN_MAX_CHARS}
    props["reply"] = {**props["reply"], "maxLength": SPOKEN_MAX_CHARS}
    item = dict(props["operations"]["items"])
    item["properties"] = {**item["properties"],
                          "text": {**item["properties"]["text"], "maxLength": limit}}
    props["operations"] = {**props["operations"], "items": item}
    return {**UNDERSTAND_SCHEMA, "properties": props}


def _whole_sentences(spoken: str, closing: str = ".") -> str:
    """
    A reply cut by its bound, back to the last full sentence.

    With no sentence break to go back to, it ends at the last whole word
    instead. It used to come back empty, and an empty question made understand
    fall back to acting: a clarify turned into doing the thing it asked about.
    """
    if len(spoken) < SPOKEN_MAX_CHARS - 1:
        return spoken
    end = max(spoken.rfind(". "), spoken.rfind("? "), spoken.rfind("! "))
    if end > 0:
        return spoken[:end + 1]
    words = spoken[:spoken.rfind(" ")] if " " in spoken else spoken
    return words.rstrip(" ,;:-") + closing


def understand(text: str, in_conversation: bool = False) -> Dict[str, Any]:
    """
    Decide whether to act, ask, or just talk. Falls back to acting.

    `in_conversation` matters more than it looks. Without it, "and the second
    one?" is a pronoun with no antecedent and gets classified as clarify, so
    Diablo asks a question he already answered thirty seconds ago.
    """
    # Mid-conversation, a short remark usually depends on what was just said,
    # so the fast path is skipped and the model gets the context.
    if not in_conversation:
        quick = fast_understand(text)
        if quick is not None:
            return quick

    prompt = UNDERSTAND_PROMPT + (FOLLOWUP_NOTE if in_conversation else "")
    try:
        result = local_brain.chat_json(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": text}],
            schema=_bounded_schema(text), num_predict=300,
        )
    except BrainOffline:
        return {"action": "act", "task": text}

    task = str(result.get("task") or "")
    if task and len(task) >= len(text) + TASK_SLACK_CHARS - 1:
        trace.event("understand", kept="original", reason="task ran into its bound")
        result["task"] = text
    for spoken, closing in (("reply", "."), ("question", "?")):
        if result.get(spoken):
            result[spoken] = _whole_sentences(str(result[spoken]), closing)

    action = result.get("action")
    if action not in ("act", "clarify", "chat"):
        return {"action": "act", "task": text}
    if action == "clarify" and not result.get("question"):
        return {"action": "act", "task": text}
    if action == "chat" and not result.get("reply"):
        return {"action": "act", "task": text}
    if action == "act" and not result.get("task"):
        result["task"] = text
    return result


# Planning costs a full model turn — measured at 2.4s — and a single-action
# request gains nothing from it. Only tasks that actually look like several
# steps pay for one.
MULTI_STEP_HINTS = (" and ", " then ", " after that", " also ", ", then",
                    "each ", "every ", "all of ", "compare ", "for both")


def needs_plan(task: str) -> bool:
    words = task.split()
    if len(words) < PLAN_THRESHOLD_WORDS:
        return False
    lowered = " " + task.lower() + " "
    return len(words) >= 18 or any(h in lowered for h in MULTI_STEP_HINTS)


def plan(task: str) -> List[str]:
    """A short checklist, for tasks that are genuinely more than one action."""
    if not needs_plan(task):
        return []
    try:
        result = local_brain.chat_json(
            [{"role": "system", "content":
              "Break the user's request into at most 5 concrete steps for an "
              "assistant with file, shell and Mac-control tools. One short "
              "imperative sentence per step. If it is a single action, return one "
              "step. Reply with JSON only."},
             {"role": "user", "content": task}],
            schema=PLAN_SCHEMA, num_predict=400,
        )
        steps = [str(s).strip() for s in result.get("steps", []) if str(s).strip()]
        return steps[:5]
    except BrainOffline:
        return []


# ─── Ambiguity discovered mid-task ───────────────────────────────────────────
#
# understand() catches a request that is ambiguous on its face — "open the file"
# never reaches the loop. It cannot catch ambiguity that only appears once a
# folder has been listed: "append FINAL to the draft in my notes folder" sounds
# specific right up until the listing comes back with draft-a.md and draft-b.md.
#
# qwen3:14b handled that from the prompt line telling it to ask. qwen3:4b
# ignores that line, reproducibly: 0 asks in 4 trials, editing draft-a.md every
# time without a word. Since the whole point is that Diablo does not guess which
# of your files to change, this is enforced here rather than requested there.

MUTATING = {"write_file", "append_file", "move_file", "delete_file"}

# Words that name a kind of file rather than a file.
_AMBIGUOUS_NOUNS = GENERIC | {"draft", "drafts", "version", "copy", "report"}


def _listed_files(messages: List[Dict[str, Any]]) -> List[str]:
    """Filenames from the most recent directory listing in this conversation."""
    for entry in reversed(messages):
        if entry.get("role") != "tool" or entry.get("tool_name") not in (
                "list_files", "find_files"):
            continue
        names = []
        for line in (entry.get("content") or "").splitlines():
            line = line.strip()
            if not line or line.endswith(":") or line.endswith("/"):
                continue
            name = line.split("  (")[0].split("\t")[0].strip()
            if name and "." in name:
                names.append(os.path.basename(name))
        return names
    return []


def _ambiguous_target(task: str, name: str, args: Dict[str, Any],
                      messages: List[Dict[str, Any]]) -> Optional[str]:
    """
    The question to ask, or None if the target is not in doubt.

    Only fires when all three are true: the user named a kind of file rather
    than one file, the folder holds more than one thing that could be it, and
    the model is about to change one of them.
    """
    if name not in MUTATING:
        return None
    path = args.get("path") or args.get("source") or ""
    if not isinstance(path, str) or not path:
        return None

    spoken = set(re.findall(r"[a-z]+", task.lower()))
    nouns = spoken & _AMBIGUOUS_NOUNS
    if not nouns:
        return None

    target = os.path.basename(path).lower()
    # If the user actually said the filename, there is nothing to ask about.
    stem = os.path.splitext(target)[0]
    if stem and stem in task.lower().replace("-", " ").replace("_", " "):
        return None

    candidates = [f for f in _listed_files(messages)
                  if any(n.rstrip("s") in f.lower() for n in nouns)]
    if len(candidates) < 2:
        return None
    if target not in [c.lower() for c in candidates]:
        return None

    listed = ", ".join(candidates[:4])
    return f"There are several: {listed}. Which one did you mean?"


def _call_signature(name: str, args: Any) -> str:
    try:
        return name + json.dumps(args, sort_keys=True)
    except Exception:
        return name + str(args)


class Cancelled(Exception):
    """The person stopped this run. Raised between steps, never mid-tool."""


def run(
    task: str,
    speak: Speak,
    ask: Ask = None,
    confirm: Confirm = None,
    on_step: Optional[Callable[[int, str, str], None]] = None,
    working: Optional[List[Dict[str, str]]] = None,
    decision: Any = None,
    should_stop: Optional[Callable[[], bool]] = None,
    request: Optional[str] = None,
) -> str:
    """
    Work the task to completion. Returns the spoken summary.

    `ask` is handed to the ask_user tool, `confirm` guards the destructive ones.
    Both come from voice_bridge and reach back out to the microphone.

    `working` is a list the caller owns and this appends to: one entry per tool
    call, with what was asked and what came back. It is how "where did you get
    that?" gets an answer later. A list passed in rather than a return value or
    a global, because several agents run this at once in their own threads and
    each one needs its own; sharing it was already a bug in this project once.
    """
    # What the router already worked out about this request.
    #
    # `route.classify` produces a Decision carrying kind, confidence, risk,
    # needs_screen and needs_tools, and until now handle() passed on four
    # arguments and dropped it. Nothing below acts on it yet — this build does
    # not add a goal, a plan or an action — but the loop can no longer claim it
    # was never told. Optional and defaulted, so every existing caller is
    # unaffected.
    if decision is not None:
        # No try/except around this. trace.event swallows its own failures by
        # design, and wrapping it again hid a real one: the first version of
        # this call passed stage= as a keyword when event() already takes it
        # positionally, so every carry raised TypeError and said nothing. The
        # project rule is that no failure is silent, and a tracing call is not
        # an exception to it.
        trace.event("carry", into="agent_core.run",
                    kind=getattr(decision, "kind", ""),
                    confidence=getattr(decision, "confidence", None),
                    risk=getattr(decision, "risk", ""),
                    needs_screen=getattr(decision, "needs_screen", None),
                    needs_tools=getattr(decision, "needs_tools", None),
                    needs_plan=getattr(decision, "needs_plan", None),
                    reason=str(getattr(decision, "reason", ""))[:120])

    agent_tools.CONTEXT["ask"] = ask
    agent_tools.CONTEXT["confirm"] = confirm
    # The person's words, for the one tool that must check them rather than
    # the model's arguments: take_screenshot saves only when this asked for it.
    # `task` is the intake model's rewrite of them, so callers that still have
    # what was said pass it as `request`; background jobs only have the task.
    agent_tools.CONTEXT["request"] = task if request is None else request
    # Where a tool puts a structured result. Drained onto the working step
    # after each call, so the evidence a tool produced survives next to the
    # sentence it returned instead of only inside it.
    observations: List[Any] = []
    agent_tools.CONTEXT["observations"] = observations
    # Tools that narrate as they work — the walkthrough is the only one so far
    # — need the voice, not just a return value.
    agent_tools.CONTEXT["speak"] = speak

    steps = plan(task)
    tool_names = agent_tools.select(task)
    tidying = is_housekeeping(task)
    if tidying:
        tool_names = [n for n in tool_names if n not in CONTENT_TOOLS]
    tools = agent_tools.schemas(tool_names)

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(steps, tidying=tidying)},
        {"role": "user", "content": task},
    ]

    seen: Dict[str, int] = {}
    used_tools: set = set()
    used_any_tool = False
    asked_which = False
    performed = set()   # mutating tools that actually ran
    saw: List[str] = []          # everything the reading tools returned
    file_reads: List[str] = []   # read_file only, for the quote fallback
    challenged = False
    counted = False
    asked_aloud = False
    quoted_challenge = False
    reassured = False

    def stop_if_asked() -> None:
        # Checked before the model is asked and before each tool runs, so a
        # stop lands within one step: whatever is already running finishes,
        # and nothing new starts.
        if should_stop is not None and should_stop():
            trace.event("agent", state="cancelled", step=step)
            raise Cancelled()

    for step in range(1, MAX_STEPS + 1):
        stop_if_asked()
        if step == 4 and not reassured:
            # Long silence on a voice interface reads as a crash. Say something.
            # Step 4 rather than 7: seven tool calls is fifteen seconds or more,
            # and by then he has already asked whether it is broken.
            reassured = True
            try:
                from skills import persona as _p
                speak(_p.still_working())
            except Exception:
                speak("Still working on it, Sir.")

        try:
            message = local_brain.chat(messages, tools=tools)
        except BrainOffline as e:
            # e.spoken, never str(e). The raw message carries the status code
            # and the response body, and this line used to read them out.
            trace.event("brain", error=str(e)[:200])
            return e.spoken

        calls = message.get("tool_calls") or []

        if not calls:
            answer = (message.get("content") or "").strip()
            if not answer:
                messages.append({"role": "user", "content":
                                 "You replied with nothing. Either call a tool or "
                                 "give me your final spoken answer."})
                continue
            if not used_any_tool and step == 1 and _looks_like_a_dodge(answer):
                messages.append({"role": "user", "content":
                                 "Do not describe what could be done. Use your "
                                 "tools and actually do it, then report back."})
                continue
            # It asked out loud instead of with the tool. Ask it properly and
            # keep going, rather than ending the turn on a question nobody can
            # answer.
            if (ask and not asked_aloud and not performed
                    and _is_really_a_question(answer, performed)):
                asked_aloud = True
                trace.event("guard", guard="question_not_asked", chars=len(answer))
                asked = _speakable(answer)
                reply = ask(asked)
                if reply:
                    messages.append({"role": "user", "content":
                                     f"{reply}. Now finish the original request: {task}"})
                    continue
                # Nobody answered. The text has been said aloud already, as the
                # question, so the turn ends here. Carrying on made the caller
                # say it a second time twenty seconds later, or sent the loop
                # back to the model for a reworded version of the same answer.
                trace.event("guard", guard="question_unanswered", chars=len(asked))
                return AlreadySpoken(asked)

            if (_did_maths_in_its_head(task, answer, used_tools)
                    and not counted):
                counted = True
                trace.event("guard", guard="maths_in_its_head", chars=len(answer))
                messages.append({"role": "user", "content":
                    "You have given a number without calculating it. Call "
                    "calculate with the arithmetic written out — every sum, "
                    "total, average and percentage, however small — and report "
                    "what it returns. Do not work it out yourself."})
                continue

            if _claims_unperformed_action(answer, performed) and not challenged:
                challenged = True
                trace.event("guard", guard="claimed_unperformed", chars=len(answer))
                messages.append({"role": "user", "content":
                                 "You have not called any tool that changes "
                                 "anything, so nothing has happened yet. Do not "
                                 "report it as done. Either make the change now "
                                 "with the right tool, or say plainly that it was "
                                 "not done and why."})
                continue
            if _claims_unperformed_action(answer, performed):
                # It was told once and said it again. Do not pass the claim on.
                return _speakable(
                    "I could not complete that, Sir, and nothing was changed.")

            # Checked after the unperformed-action guard on purpose. A model
            # that claims an append it never made also invents the resulting
            # file contents, and "you have not done it yet" is the correction
            # that gets the job finished; replacing the answer with a file's
            # contents just ends the turn with the work still outstanding.
            invented = _quotes_something_unread(answer, "\n".join(saw))
            if invented and not quoted_challenge:
                quoted_challenge = True
                trace.event("guard", guard="quoted_unread", chars=len(invented))
                messages.append({"role": "user", "content":
                                 f"You put {invented[:80]!r} in quotes, and no tool "
                                 f"returned that. Quote only what the tools actually "
                                 f"gave you, or describe it without quoting."})
                continue
            if invented:
                # Told once and it did it again. Deflecting here was the first
                # attempt and it is a poor answer, because the real content is
                # sitting in `saw` — Diablo read the file correctly and only
                # the summary was invented. So report what the tool returned
                # and let the model's version go.
                # Only actual file contents. Falling back to `saw` meant a
                # directory listing could be read out as though it were the
                # contents of a file that does not exist.
                if performed:
                    # Something really was written. Reporting a file's contents
                    # instead of what changed would be a different answer to a
                    # different question.
                    return _speakable("Done, Sir. I've made that change.")
                short = [r.strip() for r in file_reads
                         if r and len(r.strip()) < 400 and not r.startswith("Error")]
                if short:
                    content = short[-1].strip()
                    trace.event("guard", guard="quoted_unread_replaced",
                                chars=len(content))
                    return _speakable(f"It says: {content}")
                return _speakable(
                    "I read it, Sir, but I can't report the contents reliably. "
                    "Open it and I'll take another look.")

            return _speakable(answer)

        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": calls,
        })

        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {}) or {}

            # Never change one of several files the user did not name.
            question = _ambiguous_target(task, name, args, messages)
            if question and ask and not asked_which:
                asked_which = True
                trace.event("guard", guard="ambiguous_target", tool=name, question=question[:120])
                print(f"  [Agent {step}] ambiguous target, asking ({len(question)} chars)")
                reply = ask(question)
                messages.append({"role": "tool", "tool_name": name, "content":
                                 (f"Not done — the target was ambiguous so I asked. "
                                  f"The user said: {reply}. Use exactly that file."
                                  if reply else
                                  "Not done — the target was ambiguous and no answer "
                                  "came back. Ask again with ask_user, or stop.")})
                continue
            if question and not ask:
                messages.append({"role": "tool", "tool_name": name, "content":
                                 f"Error: {question} Nobody is listening, so pick "
                                 f"the most likely one and say which you chose."})
                continue

            if tidying and name in CONTENT_TOOLS:
                trace.event("guard", guard="no_reads_while_tidying", tool=name)
                print(f"  [Agent {step}] refused {name} during a tidy-up")
                messages.append({"role": "tool", "tool_name": name, "content":
                    "Error: not while tidying. Where a file goes is decided by "
                    "its name, extension, size and date — never by what is "
                    "inside it, and these are his private documents. Use "
                    "survey_folder to see what is there, or tidy_folder to sort "
                    "it by kind."})
                continue

            signature = _call_signature(name, args)
            seen[signature] = seen.get(signature, 0) + 1
            if seen[signature] > 2:
                result = ("Error: you have already made this exact call twice and "
                          "it did not get you anywhere. Try a different tool, or "
                          "call ask_user, or give your final answer.")
            else:
                stop_if_asked()
                if on_step:
                    on_step(step, name, json.dumps(args)[:120])
                # Names and sizes only. The model builds arguments from what it
                # read, so a search for a line off the screen, printed here,
                # was that line in diablo.log.
                print(f"  [Agent {step}] {name}("
                      + ", ".join(f"{k}: {len(str(v))} chars" for k, v in
                                  (args.items() if isinstance(args, dict) else [("args", args)]))
                      + ")")
                began_call = time.time()
                result = agent_tools.execute(name, args)
                used_any_tool = True
                used_tools.add(name)
                if name in READING_TOOLS:
                    saw.append(result)
                    if name == "read_file":
                        file_reads.append(result)
                seen_now = list(observations)
                del observations[:]
                if working is not None:
                    step_record: Dict[str, Any] = {
                        "tool": name,
                        "args": json.dumps(args)[:200],
                        "result": result[:600],
                    }
                    if seen_now:
                        # The object itself, not a rendering of it. This list
                        # is held in memory and handed to record_task; nothing
                        # serialises it, so there is no reason to flatten a
                        # Reading here and every reason not to.
                        step_record["observation"] = seen_now[0]
                    working.append(step_record)
                for reading in seen_now:
                    best = getattr(reading, "best", None)
                    trace.event("observed", tool=name,
                                verdict=getattr(reading, "verdict", ""),
                                labelled=bool(getattr(best, "label", "")) if best else False,
                                confidence=(round(best.confidence, 3)
                                            if best is not None else None),
                                sources=("+".join(sorted(set(best.sources)))
                                         if best is not None else ""),
                                kept=working is not None)
                # The size of the result, never the result. It used to keep the
                # first 120 characters, which for read_screen was the screen,
                # for list_windows every window title and for read_file the
                # file — all written to disk in a log that rotates but stays.
                trace.event("tool", tool=name,
                            ok=not result.startswith(("Error", "Refused", "Cancelled")),
                            ms_call=round((time.time() - began_call) * 1000, 1),
                            chars=len(result))
                if name in MUTATING and not result.lower().startswith(
                        ("error", "refused", "cancelled")):
                    performed.add(name)
                # The size, and an error's wording, never the result itself:
                # stdout is diablo.log in the app, and for read_screen the
                # result is whatever was on his screen.
                failed = result.startswith(("Error", "Refused", "Cancelled"))
                print(f"  [Agent {step}] -> {result[:120] if failed else f'{len(result)} chars'}")

            messages.append({"role": "tool", "tool_name": name, "content": result})

            # A clarification is the one point where the loop reliably stalls on
            # a small model: it asks, is answered, and then treats the answer as
            # the end of the turn. Measured on qwen3:4b — 1 completion in 3.
            # A tool result carries less instructional weight than a user turn,
            # so the outstanding job is restated as one.
            if name == "ask_user" and "user said" in result.lower():
                messages.append({"role": "user", "content":
                                 f"Good. Now finish the original request with "
                                 f"that answer: {task}"})

    # Out of steps. Ask for whatever was achieved rather than returning nothing.
    messages.append({"role": "user", "content":
                     "You have run out of steps. In one or two spoken sentences, "
                     "say what you managed to do and what is still outstanding."})
    try:
        final = local_brain.chat(messages)
        return _speakable((final.get("content") or "").strip()
                          or "I ran out of steps on that one, Sir.")
    except BrainOffline:
        return "I ran out of steps on that one, Sir."


# ─── Claimed, but not done ───────────────────────────────────────────────────
#
# qwen3:4b-instruct asked which draft to edit, was told draft-b, and then
# finished with: 'Appended "FINAL" to draft-b.md. The file is now complete and
# ready for review.' It had never called append_file. The file was untouched.
#
# A voice assistant reporting a file change that did not happen is worse than
# one that fails loudly, because there is nothing on screen to contradict it.
# So the final answer is checked against what actually ran. This is not a
# prompt instruction — there is already one of those, and it was ignored.

_ACTION_CLAIM = re.compile(
    r"\b(i\s+)?(have\s+)?(appended|wrote|written|created|made|moved|renamed|"
    r"deleted|removed|saved|updated|added|edited|replaced|copied)\b", re.IGNORECASE)

# Phrasings that report an absence of action rather than an action.
_NEGATED = re.compile(
    r"\b(did\s+not|didn'?t|could\s+not|couldn'?t|unable|cannot|can'?t|"
    r"no\s+changes?|nothing\s+was|not\s+able)\b", re.IGNORECASE)


# ─── Arithmetic it did in its head ───────────────────────────────────────────
#
# The 4B totalled a three-row CSV as 135, then 125, then 111, across three runs
# of the same test. The correct answer is 115. A different wrong number each
# time is the signature of a language model doing sums, and no prompt line
# fixes it — the system prompt has said "never do arithmetic in your head"
# throughout, in those words.
#
# So it is enforced the way the other guarantees are. If the request needed an
# exact number and the answer contains one, a tool that computes must have run.

COMPUTE_TOOLS = {"calculate", "python_snippet"}

_WANTS_EXACT = re.compile(
    r"\b(total|totals|sum|subtotal|add up|adds up|average|mean|median|"
    r"multiply|multiplied|times|product|divide|divided|percentage|percent|"
    r"how much|how many|calculate|compute|work out|worked out|count up)\b",
    re.IGNORECASE)

# A number worth being right about. Not a year, not a version, not "one".
_REPORTS_A_NUMBER = re.compile(r"\b\d{1,3}(?:[,\d]{3,})?(?:\.\d+)?\b|\b\d+\.\d+\b")


def _did_maths_in_its_head(task: str, answer: str, used: set) -> bool:
    """True when it reported a number it was never entitled to compute."""
    if used & COMPUTE_TOOLS:
        return False
    if not _WANTS_EXACT.search(task or ""):
        return False
    return bool(_REPORTS_A_NUMBER.search(answer or ""))


def _claims_unperformed_action(answer: str, performed: set) -> bool:
    """True when the spoken summary reports a change that never happened."""
    if performed:
        return False
    if _NEGATED.search(answer):
        return False
    return bool(_ACTION_CLAIM.search(answer))


_ENDS_IN_QUESTION = re.compile(r"\?\s*$")
# "which" after a comma opens a relative clause, not a choice: ", which I
# completed" is a statement. A finished answer was asked aloud because of it.
_OFFERS_A_CHOICE = re.compile(
    r"\b((?<!,\s)which|would you like|do you want|should i|shall i|let me know which|"
    r"i found (two|three|several|multiple)|there are (two|three|several|multiple))\b",
    re.IGNORECASE)


class AlreadySpoken(str):
    """
    A result the user has already heard: it was asked aloud and nobody answered.

    Still a str, so every caller records it exactly as before. Only the point
    that would say it out loud checks for it, and stays quiet.
    """


def _is_really_a_question(answer: str, performed: set) -> bool:
    """
    True when the final answer is a question the model forgot to actually ask.

    A small model asked to edit "the draft" when two exist often ends its turn
    with "I found two draft files: draft-a.md and draft-b.md" instead of calling
    ask_user. That is the right instinct and the wrong mechanism: on a voice
    interface the turn ends, the sentence is spoken into the air, and the user
    has to start again. Measured on qwen3:4b — 2 turns in 4 ended this way.

    Nothing has been changed at that point, which is the condition that makes it
    safe to turn the sentence back into a real question and carry on.
    """
    if performed:
        return False
    return bool(_ENDS_IN_QUESTION.search(answer) or _OFFERS_A_CHOICE.search(answer))


# ─── Quoting things it never read ────────────────────────────────────────────
#
# Asked to read a file containing "the first line", qwen3:4b-instruct read it
# correctly — the tool returned exactly that — and then reported:
#
#   The first line of notes.txt is: "Initial evaluation notes for the..."
#
# The true content was in its context. It pattern-completed a plausible
# notes.txt instead, which is a worse failure than the earlier fabrications
# because there is no missing information to blame.
#
# So anything the answer puts in quotes has to appear in something a tool
# actually returned this turn. Cheap, and it only fires on the exact shape of
# the failure: a quoted span presented as content.

READING_TOOLS = {"read_file", "search_text", "list_files", "find_files",
                 "read_screen", "explain_screen", "run_shell", "python_snippet"}

_QUOTED = re.compile(r"[\"\u201c\u2018']([^\"\u201c\u201d\u2018\u2019']{12,200})[\"\u201d\u2019']")


def _normalise_for_compare(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _quotes_something_unread(answer: str, seen: str) -> Optional[str]:
    """
    The first quoted span in the answer that no tool ever produced, or None.

    Short quotes are ignored — a model quoting a filename or a single word is
    not claiming to have read it. Twelve characters is long enough that it is
    presenting content.
    """
    if not seen:
        return None
    haystack = _normalise_for_compare(seen)
    for candidate in _QUOTED.findall(answer):
        needle = _normalise_for_compare(candidate)
        if len(needle) < 12:
            continue
        if needle in haystack:
            continue
        # Allow a paraphrase that is mostly words it did read.
        words = [w for w in needle.split() if len(w) > 3]
        if words and sum(1 for w in words if w in haystack) / len(words) >= 0.7:
            continue
        return candidate
    return None


def _looks_like_a_dodge(answer: str) -> bool:
    """Catch the small-model habit of narrating a plan instead of executing it."""
    lowered = answer.lower()
    tells = ["i would", "i could", "you can", "you could", "here's how",
             "here is how", "to do this", "i can help", "would you like me to",
             "let me know if"]
    return any(t in lowered for t in tells)


def _speakable(text: str) -> str:
    """Strip the model's formatting habits so TTS does not read asterisks aloud."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*[-*#>]+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`#]", "", text)
    text = re.sub(r"\n{2,}", " ", text).replace("\n", " ")
    text = re.sub(r"\s{2,}", " ", text).strip()
    # Every prompt-level ban on this failed, in conversation and here alike. It
    # is read aloud at the end of every single task, so it is cut in code.
    before_strip = text
    text = re.sub(
        r"(?:^|(?<=[.!?]))\s*(?:and\s+)?(?:i\s+)?(?:"
        r"let me know\b|hope (?:that|this) helps\b|is there anything else\b|"
        r"feel free to\b|if you need (?:any |further |more )*(?:assistance|help)\b|"
        r"i'?m here if\b|don'?t hesitate\b|happy to help\b"
        r")[^.!?]*[.!?]?\s*$", "", text, flags=re.IGNORECASE).strip()
    # Never let the strip consume the whole reply. If the filler WAS the reply,
    # or the match was a false positive like "let me know what you think of the
    # draft", keep what the model actually said.
    if not text:
        text = before_strip
    if len(text.split()) > 60:
        text = local_brain.speech_summary(text, limit_words=35)
    return text


# ─── Entry point used by voice_bridge ────────────────────────────────────────

def handle(
    text: str,
    speak: Speak,
    ask: Ask = None,
    confirm: Confirm = None,
    announce: bool = True,
) -> Dict[str, Any]:
    """
    Full pipeline for one spoken sentence that the reflexes did not claim.

    Returns {"kind": "chat"|"clarify"|"task", ...}. A "clarify" result means the
    question has already been asked out loud and the answer folded back in — the
    caller does not have to do anything about it.
    """
    if not local_brain.available():
        speak("My local brain is offline, Sir. Ollama does not appear to be running.")
        return {"kind": "error", "summary": "ollama offline"}

    verdict = understand(text)

    # One round of clarification, then commit. Two rounds of questions on a voice
    # interface stops feeling like an assistant and starts feeling like a form.
    if verdict["action"] == "clarify":
        question = verdict["question"]
        if not ask:
            verdict = {"action": "act", "task": text}
        else:
            # `ask` speaks the question itself — it owns the microphone and has
            # to open it the instant the question finishes playing.
            answer = ask(question)
            if not answer:
                speak("I did not catch that, Sir. I will use my best judgement.")
                verdict = {"action": "act", "task": text}
            else:
                verdict = understand(f"{text} (clarification: {answer})")
                if verdict["action"] == "clarify":
                    verdict = {"action": "act",
                               "task": f"{text}. Clarification from the user: {answer}"}

    if verdict["action"] == "chat":
        speak(verdict["reply"])
        return {"kind": "chat", "summary": verdict["reply"]}

    task = verdict.get("task") or text
    if announce:
        speak("On it, Sir.")
    summary = run(task, speak, ask=ask, confirm=confirm)
    speak(summary)
    return {"kind": "task", "task": task, "summary": summary}


if __name__ == "__main__":
    import sys

    if not local_brain.available():
        print("Ollama is not running. Start it with `ollama serve`.")
        raise SystemExit(1)

    spoken = " ".join(sys.argv[1:]) or "list what is on my desktop"
    print(f"\nTask: {spoken}\n")
    print("Understanding:", json.dumps(understand(spoken), indent=2))
    started = time.time()
    print("\nResult:", run(spoken, print, ask=lambda q: input(f"  ASK: {q}\n  > ")))
    print(f"({time.time() - started:.1f}s)")
