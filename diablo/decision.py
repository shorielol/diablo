"""
D.I.A.B.L.O. — which path a turn takes, decided before any model runs.

Measured before this existed (Phase C baseline): every question that was not
an exact greeting paid for a constrained model call, `understand()`, to learn
that it was conversation — 1.8 to 4 seconds on the 4B — and only then started
the answer. "What is supply chain management?" was 7.4 s from release to the
first word, and 2.5 s of that was deciding it was a question.

This decides the cheap cases without a model and says nothing about the rest.

    INSTANT   the words need no model: a greeting, the time, a sum
    FAST      conversation that needs neither the screen nor a tool: answered
              straight from the conversation model, streamed
    AGENT     the screen, being shown, doing something: the existing path
    None      no opinion. The existing router decides, exactly as before.

It is advisory and nothing else. It cannot execute, authorise or confirm
anything: it has no access to tools, to the computer-use layer or to any
permission gate, and a verdict of AGENT means "leave it to the path that owns
that", never "do it". When it is unsure it returns None, and None costs
nothing but the model call it would have saved.

The interface is Diablo's. `Rules` is the implementation in use; `Laya` is an
adapter for the Laya decision model, kept behind the same interface and only
used when DIABLO_DECISION=laya says so. Nothing outside this module knows
which one answered.
"""

import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import trace  # noqa: E402

# Lanes: how much machinery a turn needs.
INSTANT = "instant"
FAST = "fast"
FULL = "full"
AGENT = "agent"
LANES = (INSTANT, FAST, FULL, AGENT)

# Intents: what kind of turn it is. Bounded on purpose.
CASUAL = "casual"
FACTUAL = "factual"
EXPLANATION = "explanation"
SCREEN = "screen"
GUIDE = "guide"
DO = "do"
MISSION = "mission"
COMPLEX = "complex"
UNKNOWN = "unknown"
INTENTS = (CASUAL, FACTUAL, EXPLANATION, SCREEN, GUIDE, DO, MISSION, COMPLEX, UNKNOWN)

ENGINE = os.getenv("DIABLO_DECISION", "rules").strip().lower()


@dataclass(frozen=True)
class Verdict:
    """One decision about one utterance. Carries no authority."""

    intent: str
    lane: str
    needs_screen: bool = False
    needs_tools: bool = False
    complete: bool = True         # the utterance reads as finished
    reply: str = ""               # INSTANT only: the words, when no model is needed
    engine: str = ""
    reason: str = ""
    ms: float = 0.0


class Engine:
    """What a decision engine is. `decide` returns None when it has no opinion."""

    name = "none"

    def decide(self, text: str, in_conversation: bool = False) -> Optional[Verdict]:
        return None


# ─── Rules ───────────────────────────────────────────────────────────────────

_ADDRESS = re.compile(r"\b(?:diablo|sir|mate|buddy)\b", re.I)

# Asking for something to be done. Any of these anywhere keeps a turn off the
# fast lane: "what happens if I delete it" is a question, and still not one to
# answer without looking at what "it" is.
_ACTION_WORDS = {
    "open", "close", "quit", "launch", "start", "click", "press", "type", "tap",
    "scroll", "drag", "select", "delete", "remove", "move", "rename", "copy",
    "paste", "send", "email", "text", "message", "call", "ring", "reply",
    "create", "make", "write", "draft", "save", "export", "download", "upload",
    "install", "run", "execute", "play", "pause", "stop", "resume", "skip",
    "search", "google", "research", "find", "look", "check", "remind",
    "schedule", "book", "order", "buy", "set", "turn", "mute", "unmute",
    "summarise", "summarize", "read", "list", "organise", "organize", "tidy",
    "sort", "plan", "mission", "task", "agent", "fix", "debug", "build",
    "deploy", "commit", "record", "screenshot", "show", "highlight",
    "walk", "guide", "teach",
}

# Things only a tool, the screen, or the user's own data can answer. A model
# answering "what's the weather" or "what's on my calendar" from its weights
# is making it up.
_NEEDS_THE_WORLD = {
    "today", "tonight", "tomorrow", "yesterday", "latest", "news", "current",
    "currently", "now", "recent", "recently", "weather", "forecast",
    "temperature", "price", "prices", "stock", "stocks", "share", "score",
    "scores", "rate", "rates", "my", "mine", "our", "ours", "file", "files",
    "folder", "folders", "desktop", "document", "documents", "pdf", "email",
    "emails", "inbox", "calendar", "meeting", "meetings", "schedule",
    "reminder", "reminders", "message", "messages", "contact", "contacts",
    "notes", "website", "site", "link", "url", "app", "apps", "window", "tab",
    "screen", "browser", "chrome", "safari", "spotify", "youtube", "video",
    "song", "music", "playlist", "this", "that", "these", "those", "it",
    "here", "there", "above", "below", "earlier", "before", "again", "last",
    "company", "startup", "business", "strategy", "project", "essay", "ia",
    "ee", "tok", "exam", "homework", "assignment",
    # Found by the held-out check (Phase C): a fare and the time somewhere
    # else are live facts too.
    "cost", "costs", "much", "flight", "flights", "ticket", "tickets", "fare",
    "fares", "time", "times", "open", "hours",
}
_YEAR = re.compile(r"\b20[2-9]\d\b")

# The shapes of a self-contained question.
_QUESTION = re.compile(
    r"^(?:(?:so|and|ok|okay|hey|please)\s+)*(?:"
    r"what(?:'s|\s+is|\s+are|\s+was|\s+were|\s+does|\s+do)?\b|whats\b|"
    r"who(?:'s|\s+is|\s+was|\s+were)?\b|why\b|"
    r"how\s+(?:does|do|is|are|did|was|were|many|much|long|far|big|old)\b|"
    r"when\s+(?:did|was|were|is)\b|which\b|"
    r"define\b|explain\b|describe\b|tell\s+me\s+about\b|"
    r"(?:can|could|would)\s+you\s+(?:explain|define|describe|tell\s+me)\b|"
    r"give\s+me\s+(?:an?\s+)?(?:example|definition|analogy)\b)", re.I)

# Yes-or-no questions read like instructions without their question mark: "do
# the thing" is an instruction, "do dogs dream?" a question. Only with the mark.
_YES_NO = re.compile(r"^(?:(?:so|and|ok|okay)\s+)*(?:is\s+(?:a|an|the)|are|does|do)\s+\w",
                     re.I)

# How something is doing is a status, and a status is the world's to report:
# "how is Brew and Critics doing" has no answer in the model's weights.
_STATUS = re.compile(r"^how(?:'s|\s+is|\s+are|\s+was)\s+.+\s+"
                     r"(?:doing|going|getting\s+on|coming\s+along|performing|selling)$", re.I)

# A procedure is the tutor's or the agent's, never the fast lane's.
_PROCEDURE = re.compile(r"\bhow\s+(?:do|can|should|would)\s+(?:i|we|you)\b|\bhow\s+to\b",
                        re.I)

# Conversation that is plainly just conversation.
_CASUAL = re.compile(
    r"^(?:(?:hey|hi|hello|so|well|oh|ok|okay)\s+)*(?:"
    r"how(?:'s|\s+is|\s+are)\s+(?:you|it\s+going|things|life)(?:\s+doing)?|"
    r"how(?:'ve|\s+have)\s+you\s+been|what'?s\s+up|whats\s+up|"
    r"good\s+(?:morning|afternoon|evening|night)|morning|evening|"
    r"thanks?(?:\s+(?:a\s+lot|very\s+much|so\s+much|again))?|"
    r"thank\s+you(?:\s+(?:so|very)\s+much)?|"
    r"cheers|ta|hiya|howdy|yo|nice(?:\s+one)?|great|cool|brilliant|perfect|lovely|"
    r"(?:that'?s|thats)\s+(?:great|good|fine|cool|interesting|funny|fair)|"
    r"i'?m\s+(?:tired|knackered|exhausted|bored|good|fine|great|okay|ok|back)|"
    r"tell\s+me\s+a\s+joke|say\s+something\s+(?:funny|nice))$", re.I)

# An utterance that stops mid-thought, as a released key can leave one.
_DANGLING_END = {
    "and", "or", "but", "the", "a", "an", "to", "of", "with", "for", "is",
    "are", "what", "how", "my", "your", "um", "uh", "er", "because", "so",
    "if", "in", "on", "at", "about", "between", "from",
}

# Words for numbers, for the arithmetic that needs no model.
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split())}
_OPERATORS = {"plus": "+", "add": "+", "minus": "-", "times": "*", "x": "*",
              "multiplied": "*", "divided": "/", "over": "/", "+": "+", "-": "-",
              "*": "*", "/": "/", "×": "*", "÷": "/"}
_SUM = re.compile(r"^(?:what(?:'s|\s+is)|whats|calculate|compute|how\s+much\s+is)\s+(.+)$",
                  re.I)
_TIME = re.compile(r"^(?:what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it|"
                   r"whats\s+the\s+time|time\s+please)$", re.I)
_DATE = re.compile(r"^(?:what(?:'s|\s+is)\s+(?:the\s+date|today'?s\s+date|"
                   r"the\s+date\s+today)|what\s+day\s+is\s+it(?:\s+today)?|"
                   r"whats\s+the\s+date)$", re.I)


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+|[+\-*/×÷]", text.lower())


def _numbers_in(tokens: List[str]) -> Optional[str]:
    """ "twenty five plus 3" as "25+3", or None if it is not only a sum."""
    out: List[str] = []
    pending: Optional[int] = None
    operators = 0
    for token in tokens:
        if token in ("by", "and"):
            continue
        if token.isdigit():
            if pending is not None:
                return None
            pending = int(token)
            continue
        if token in _UNITS or token in _TENS:
            value = _UNITS.get(token, _TENS.get(token, 0))
            if pending is not None and pending % 10 == 0 and pending >= 20 and token in _UNITS:
                pending += value
            elif pending is None:
                pending = value
            else:
                return None
            continue
        if token in ("hundred", "thousand") and pending is not None:
            pending *= 100 if token == "hundred" else 1000
            continue
        if token in _OPERATORS:
            if pending is None:
                return None
            out += [str(pending), _OPERATORS[token]]
            pending = None
            operators += 1
            continue
        return None
    if pending is None or operators == 0:
        return None
    out.append(str(pending))
    return "".join(out)


class Rules(Engine):
    """
    Deterministic, built from the signals Diablo already trusts.

    Every rule errs towards None. A wrong FAST verdict answers a question the
    screen or a tool should have answered; a wrong None costs one model call.
    """

    name = "rules"

    def decide(self, text: str, in_conversation: bool = False) -> Optional[Verdict]:
        raw = (text or "").replace("’", "'").strip()
        if not raw:
            return None
        plain = re.sub(r"\s+", " ", _ADDRESS.sub(" ", raw)).strip(" ,.!?")
        lowered = plain.lower()
        tokens = _words(lowered)
        if not tokens:
            return None

        # The screen, being shown, being taught: the path that owns them.
        # "Explain inflation" is the one kind left open: it may be about the
        # diagram in front of him or a question for a spoken answer, and only
        # the screen can say which (B.3). That verdict says so, and the caller
        # checks the screen exactly as the router would have.
        screen, maybe = self._about_the_screen(raw)
        if screen and not maybe:
            return Verdict(intent=screen, lane=AGENT, needs_screen=True,
                           needs_tools=True, engine=self.name,
                           reason="names the screen or asks to be shown")

        if tokens[-1] in _DANGLING_END and not maybe:
            return Verdict(intent=UNKNOWN, lane=AGENT, complete=False, engine=self.name,
                           reason="stops mid-thought")

        intent, reply = self._instant(lowered) if not maybe else (UNKNOWN, "")
        if reply:
            return Verdict(intent=intent, lane=INSTANT, reply=reply, engine=self.name,
                           reason="answered without a model")

        casual = bool(_CASUAL.match(lowered))
        acting = set(tokens) & _ACTION_WORDS
        if acting and not casual:
            return Verdict(intent=DO, lane=AGENT, needs_tools=True, engine=self.name,
                           reason=f"asks for something done ({sorted(acting)[0]})")
        if self._named_agent(raw):
            return None
        if casual and not maybe:
            return Verdict(intent=CASUAL, lane=FAST, engine=self.name,
                           reason="plain conversation")
        question = bool(_QUESTION.match(lowered)) or (
            bool(_YES_NO.match(lowered)) and raw.rstrip().endswith("?"))
        if (question and not _PROCEDURE.search(lowered)
                and not _STATUS.match(lowered)
                and not (set(tokens) & _NEEDS_THE_WORLD) and not _YEAR.search(lowered)
                and 2 <= len(tokens) <= 24):
            intent = (EXPLANATION if re.match(r"^(?:explain|describe|why)\b", lowered)
                      or re.search(r"\bexplain\b", lowered) else FACTUAL)
            return Verdict(intent=intent, lane=FAST, needs_screen=maybe, engine=self.name,
                           reason=("a self-contained question, once the screen is "
                                   "checked" if maybe else "a self-contained question"))
        if maybe:
            return Verdict(intent=GUIDE, lane=AGENT, needs_screen=True, engine=self.name,
                           reason="may be about the screen")
        return None

    @staticmethod
    def _about_the_screen(raw: str):
        """(intent, uncertain): uncertain means only the screen can tell."""
        try:
            from skills import screen_context, target, tutor
            shown = target.asked_to_show(raw)
            if shown is not None:
                return GUIDE, not shown.certain
            if tutor.wants_teaching(raw):
                return GUIDE, False
            if screen_context.wants_screen(raw):
                return SCREEN, False
        except Exception:
            return SCREEN, False     # cannot tell, so not the fast lane
        return "", False

    @staticmethod
    def _named_agent(raw: str) -> bool:
        try:
            from skills import conversation
            named = conversation.addressed_to(raw)
            return bool(named) and named != "diablo"
        except Exception:
            return True

    @staticmethod
    def _instant(lowered: str):
        from skills import agent_core
        greeting = agent_core.FAST_GREETINGS.get(lowered)
        if greeting:
            return CASUAL, greeting
        if _TIME.match(lowered):
            return FACTUAL, time.strftime("It's %-I:%M %p, Sir.")
        if _DATE.match(lowered):
            day = int(time.strftime("%d"))
            suffix = ("th" if 10 <= day % 100 <= 20
                      else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th"))
            return FACTUAL, time.strftime(f"It's %A the {day}{suffix} of %B, Sir.")
        found = _SUM.match(lowered)
        if found:
            expression = _numbers_in(_words(found.group(1)))
            if expression:
                from skills import agent_tools
                value = agent_tools.calculate(expression)
                if value and not value.startswith("Error"):
                    return FACTUAL, f"That's {value}, Sir."
        return UNKNOWN, ""


# ─── Laya ────────────────────────────────────────────────────────────────────

LAYA_INTENTS = {
    CASUAL: "small talk, greetings, thanks, how someone is",
    FACTUAL: "a general knowledge question with a short factual answer",
    EXPLANATION: "asks for a concept to be explained",
    SCREEN: "about what is on the user's screen, window, page or cursor",
    GUIDE: "asks to be shown where something is or how to do it on screen",
    DO: "asks the assistant to perform an action on the computer or a file",
    MISSION: "a multi-step job to research, plan or carry out",
    COMPLEX: "needs careful multi-step reasoning or the user's own data",
}
LAYA_QUESTIONS = {
    "intent": {"type": "choice", "instructions": "What does the user want?",
               "criteria": LAYA_INTENTS},
    "needs_screen": {"type": "noul",
                     "instructions": "Does answering require looking at the user's screen?"},
    "needs_tools": {"type": "noul",
                    "instructions": "Does answering require a tool, a file, the web or "
                                    "live data rather than general knowledge?"},
    "complete": {"type": "noul",
                 "instructions": "Is this a complete request rather than cut off "
                                 "mid-sentence?"},
}


class Laya(Engine):
    """
    The Laya decision model behind Diablo's interface.

    One `predict` call answers every question in LAYA_QUESTIONS. The model is
    loaded on first use, never at import, and a missing package or a failed
    load is reported as None so the caller falls back.
    """

    name = "laya"

    def __init__(self, router: Any = None, threshold: float = 0.5):
        self._router = router
        self._threshold = threshold
        self._failed = False

    def _get(self):
        if self._router is None and not self._failed:
            try:
                from laya import Router
                self._router = Router()
            except Exception as e:
                self._failed = True
                trace.event("decide", engine=self.name,
                            error=f"{type(e).__name__}: {e}"[:120])
        return self._router

    def raw(self, text: str) -> Optional[Dict[str, Any]]:
        router = self._get()
        if router is None:
            return None
        result = router.predict(text, LAYA_QUESTIONS)
        return (result or {}).get("answers")

    def decide(self, text: str, in_conversation: bool = False) -> Optional[Verdict]:
        answers = self.raw(text)
        if not answers:
            return None
        intent = str((answers.get("intent") or {}).get("choice") or UNKNOWN)
        if intent not in INTENTS:
            intent = UNKNOWN

        def yes(key: str) -> bool:
            value = (answers.get(key) or {}).get("noul")
            return value is not None and float(value) >= self._threshold

        screen, tools, complete = yes("needs_screen"), yes("needs_tools"), yes("complete")
        if intent in (SCREEN, GUIDE, DO, MISSION):
            lane = AGENT
        elif intent in (CASUAL, FACTUAL, EXPLANATION) and not (screen or tools) and complete:
            lane = FAST
        else:
            lane = FULL
        return Verdict(intent=intent, lane=lane, needs_screen=screen, needs_tools=tools,
                       complete=complete, engine=self.name, reason="laya")


# ─── The one entry point ─────────────────────────────────────────────────────

_engine: Optional[Engine] = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = {"rules": Rules, "laya": Laya}.get(ENGINE, Engine)()
    return _engine


def decide(text: str, in_conversation: bool = False) -> Optional[Verdict]:
    """
    The verdict for one utterance, or None. Never raises.

    None is always safe: it means the existing router decides.
    """
    started = time.perf_counter()
    try:
        verdict = engine().decide(text, in_conversation=in_conversation)
    except Exception as e:
        trace.event("decide", engine=engine().name, route="fallback",
                    error=f"{type(e).__name__}: {e}"[:120])
        return None
    ms = round((time.perf_counter() - started) * 1000, 2)
    if verdict is None:
        trace.event("decide", engine=engine().name, lane="none", ms=ms)
        return None
    verdict = Verdict(**{**verdict.__dict__, "ms": ms})
    trace.event("decide", engine=verdict.engine, lane=verdict.lane, intent=verdict.intent,
                needs_screen=verdict.needs_screen, needs_tools=verdict.needs_tools,
                complete=verdict.complete, ms=ms, reason=verdict.reason)
    return verdict
