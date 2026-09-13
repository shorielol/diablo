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
MAX_TURNS = int(os.getenv("DIABLO_MEMORY_TURNS", "12"))
SESSION_TIMEOUT = int(os.getenv("DIABLO_SESSION_TIMEOUT", "900"))  # 15 minutes

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


class Session:
    """One rolling conversation. Not thread-safe by design — there is one voice."""

    def __init__(self) -> None:
        self.turns: List[Dict[str, str]] = []
        self.last_spoke_at: float = 0.0
        self.last_screen_app: Optional[str] = None

    def _expire_if_stale(self) -> None:
        if self.last_spoke_at and time.time() - self.last_spoke_at > SESSION_TIMEOUT:
            self.turns = []
            self.last_screen_app = None

    def remember(self, role: str, content: str) -> None:
        self.turns.append({"role": role, "content": content})
        if len(self.turns) > MAX_TURNS * 2:
            self.turns = self.turns[-MAX_TURNS * 2:]
        self.last_spoke_at = time.time()

    def reset(self) -> None:
        self.turns = []
        self.last_screen_app = None

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
        messages.extend(self.turns)
        messages.append({"role": "user", "content": text})

        try:
            message = local_brain.chat(messages, num_predict=400, temperature=0.5)
        except local_brain.BrainOffline:
            return "My brain is offline, Sir. Ollama has stopped responding."

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


def talk(text: str) -> str:
    """One conversational turn, with the screen attached if he pointed at it."""
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
