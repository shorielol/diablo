"""
D.I.A.B.L.O. Agent Core
=======================
The loop that turns a sentence into finished work, running entirely on qwen3:14b
through Ollama. Nothing here touches the internet.

    understand()  →  is this actionable, ambiguous, or just conversation?
    plan()        →  a short checklist, because a 14B model drifts without one
    run()         →  think → call a tool → read the result → repeat → report

A 14B model is not a frontier model, and the whole design leans on that:

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
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

# The skill registry loads this file by path, so its own folder is not
# necessarily importable. Put it there before reaching for its siblings.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_tools  # noqa: E402
import local_brain  # noqa: E402
from local_brain import BrainOffline  # noqa: E402

MAX_STEPS = int(os.getenv("DIABLO_MAX_STEPS", "14"))
PLAN_THRESHOLD_WORDS = 6

Speak = Callable[[str], None]
Ask = Optional[Callable[[str], str]]
Confirm = Optional[Callable[[str], bool]]


# ─── Prompts ─────────────────────────────────────────────────────────────────

def _system_prompt(plan: Optional[List[str]] = None) -> str:
    base = f"""You are Diablo, Shaurya's voice assistant, running offline on his Mac.
You do not answer questions about tasks — you carry them out, using the tools you
have been given, and then report what you actually did.

Shaurya's folders:
{agent_tools.workspace_map()}

His work lives in numbered workstations: 10 Academics is IB coursework (the Econ
EE, the Coca-Cola BM IA, TOK), 30 Code is software, 50 Finance is spreadsheets
and markets, 20 Ventures is his businesses, 40 Studio is music and design.

How to work:
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
        "action": {"type": "string", "enum": ["act", "clarify", "chat"]},
        "task": {"type": "string"},
        "question": {"type": "string"},
        "reply": {"type": "string"},
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
            schema=UNDERSTAND_SCHEMA, num_predict=300,
        )
    except BrainOffline:
        return {"action": "act", "task": text}

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


def _call_signature(name: str, args: Any) -> str:
    try:
        return name + json.dumps(args, sort_keys=True)
    except Exception:
        return name + str(args)


def run(
    task: str,
    speak: Speak,
    ask: Ask = None,
    confirm: Confirm = None,
    on_step: Optional[Callable[[int, str, str], None]] = None,
) -> str:
    """
    Work the task to completion. Returns the spoken summary.

    `ask` is handed to the ask_user tool, `confirm` guards the destructive ones.
    Both come from voice_bridge and reach back out to the microphone.
    """
    agent_tools.CONTEXT["ask"] = ask
    agent_tools.CONTEXT["confirm"] = confirm

    steps = plan(task)
    tool_names = agent_tools.select(task)
    tools = agent_tools.schemas(tool_names)

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(steps)},
        {"role": "user", "content": task},
    ]

    seen: Dict[str, int] = {}
    used_any_tool = False
    reassured = False

    for step in range(1, MAX_STEPS + 1):
        if step == 7 and not reassured:
            # Long silence on a voice interface reads as a crash. Say something.
            reassured = True
            speak("Still working on it, Sir.")

        try:
            message = local_brain.chat(messages, tools=tools)
        except BrainOffline as e:
            return f"My local brain stopped responding, Sir. {e}"

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

            signature = _call_signature(name, args)
            seen[signature] = seen.get(signature, 0) + 1
            if seen[signature] > 2:
                result = ("Error: you have already made this exact call twice and "
                          "it did not get you anywhere. Try a different tool, or "
                          "call ask_user, or give your final answer.")
            else:
                if on_step:
                    on_step(step, name, json.dumps(args)[:120])
                print(f"  [Agent {step}] {name}({json.dumps(args)[:110]})")
                result = agent_tools.execute(name, args)
                used_any_tool = True
                print(f"  [Agent {step}] -> {result[:160]}")

            messages.append({"role": "tool", "tool_name": name, "content": result})

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
