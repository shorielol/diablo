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
_PROCEDURE = re.compile(r"\bhow\s+(?:do|can|should|would)\s+(?:i|we|you)\b|\bhow\s+to\b|"
                        r"\bwhat(?:'s|\s+is)\s+the\s+way\s+to\b", re.I)

# Asking which part of a picture something is, in words target.py has no
# phrase for (the decision corpus, v1 train split): "which part is the surplus".
_WHICH_PART = re.compile(r"\bwhich\s+(?:part|bit|area|region|section|line|curve)\b", re.I)

# Words that only mean something on the screen: the pointer, and the named
# things a window holds. "What's the error in Preview" has no answer in the
# model's weights (the decision corpus, v1 train split).
_SCREEN_WORDS = {"pointer", "cursor", "mouse", "hovering", "hover", "error", "errors",
                 "popup", "pop-up", "button", "icon", "menu", "preview", "keynote", "zen",
                 "terminal", "finder", "pages", "numbers", "excel", "word", "slack",
                 "notes", "mail", "vs", "code"}

# Spoken tails and stutters that hide the shape of what was said: "how is
# bitcoin doing real quick", "how do do I empty the trash".
_TAIL = re.compile(r"(?:\s+(?:please|for me|real quick|quickly|thanks|thank you|mate|then))+$",
                   re.I)

# A turn that continues the last one: its meaning is in the history, which
# only the router carries.
_CONTINUES = re.compile(r"^(?:and|also|but|so)\b|^(?:what|how)\s+about\b", re.I)

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
    "if", "in", "on", "at", "about", "between", "from", "just",
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
        plain = _TAIL.sub("", re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", plain, flags=re.I))
        lowered = plain.lower().strip(" ,.!?")
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

        if in_conversation and _CONTINUES.match(lowered):
            return None                      # its meaning is in the history
        if (set(tokens) & _SCREEN_WORDS or _WHICH_PART.search(lowered)) and not maybe:
            return Verdict(intent=SCREEN, lane=AGENT, needs_screen=True, engine=self.name,
                           reason="names something only the screen shows")
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
#
# The contract a Laya checkpoint is asked, trained and evaluated on. One
# choice and two yes/no questions, because those are the only decisions with
# a consumer: the intent (lane and pace), whether the screen must be looked at
# first, and whether the utterance is finished. research/laya_diablo/
# contract.json holds the same definition and a test keeps the two identical.

LAYA_CONTRACT_VERSION = "diablo-decision-v1"
LAYA_INTENTS = {
    CASUAL: "small talk, greetings, thanks, feelings or an opinion",
    FACTUAL: "a general knowledge question with a short answer",
    EXPLANATION: "asks for a general idea or concept to be explained",
    SCREEN: "about what is on the user's screen, window, page or under the cursor",
    GUIDE: "asks where something is on screen, or how to do something in an app",
    DO: "asks the assistant to do something on the computer, a file, an app or a message",
    MISSION: "a multi-step job: research, plan, draft, compare or organise",
    COMPLEX: "needs live data, the user's own data, or what was said earlier",
    UNKNOWN: "unclear, cut off, or points at something with nothing to point at",
}
LAYA_QUESTIONS = {
    "intent": {"type": "choice", "instructions": "What does the user want?",
               "criteria": LAYA_INTENTS},
    "needs_screen": {"type": "noul",
                     "instructions": "Does answering require looking at the user's screen?"},
    "complete": {"type": "noul",
                 "instructions": "Is this a complete request rather than cut off "
                                 "mid-sentence?"},
}
_CONVERSATION = (CASUAL, FACTUAL, EXPLANATION)


def laya_state(text: str, history: Optional[List[str]] = None, app: str = "",
               window: str = "") -> Dict[str, Any]:
    """
    The minimum a decision needs: the request, at most two earlier lines, and
    which application is in front. Never OCR, the tool list, or plans.
    """
    state: Dict[str, Any] = {"request": (text or "").strip()[:400]}
    if history:
        state["history"] = [str(h)[:160] for h in history[-2:]]
    if app:
        state["app"] = str(app)[:40]
    if window:
        state["window"] = str(window)[:80]
    return state


def lane_for(intent: str, needs_screen: bool, complete: bool) -> str:
    """The lane a Laya judgement implies. Only conversation can be FAST."""
    if intent in _CONVERSATION and not needs_screen and complete:
        return FAST
    return AGENT if intent in (SCREEN, GUIDE, DO, MISSION) else FULL


CHECKPOINT_MANIFEST = "diablo_manifest.json"
_MANIFEST_KEYS = ("contract_version", "base_checkpoint", "laya_version", "dataset_version",
                  "split_hashes", "training", "calibration", "weights_sha256")


def verify_checkpoint(path: str, hash_weights: bool = True):
    """
    (True, "") for a Diablo-trained checkpoint that can be trusted, or
    (False, why).

    It must carry Diablo's manifest, be trained on this contract, and hold
    exactly the temperatures its calibration fitted: a temperature inherited
    from the base checkpoint that is still in the config would be used
    instead of the fitted one, per option-count bucket, without a word.
    """
    import hashlib
    import json
    try:
        with open(os.path.join(path, CHECKPOINT_MANIFEST)) as f:
            manifest = json.load(f)
        with open(os.path.join(path, "rl_agent_config.json")) as f:
            cfg = json.load(f)
    except Exception as e:
        return False, f"unreadable checkpoint: {type(e).__name__}"
    missing = [k for k in _MANIFEST_KEYS if k not in manifest]
    if missing:
        return False, f"manifest lacks {missing}"
    if manifest["contract_version"] != LAYA_CONTRACT_VERSION:
        return False, "trained on another contract"
    calibration = manifest.get("calibration") or {}
    if not calibration.get("fitted_on_split") == "calibration":
        return False, "not calibrated on the calibration split"
    fitted = calibration.get("temperature")
    if fitted is None or [round(float(t), 6) for t in cfg.get("temperature", [])] != \
            [round(float(t), 6) for t in fitted]:
        return False, "config temperatures are not the fitted ones"
    declared = calibration.get("temperature_by_options") or {}
    stale = {k: v for k, v in (cfg.get("temperature_by_options") or {}).items()
             if k not in declared or round(float(v), 6) != round(float(declared[k]), 6)}
    if stale:
        return False, f"inherited temperature overrides {sorted(stale)}"
    if hash_weights:
        digest = hashlib.sha256()
        try:
            with open(os.path.join(path, "model.safetensors"), "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    digest.update(block)
        except Exception as e:
            return False, f"no weights: {type(e).__name__}"
        if digest.hexdigest() != manifest["weights_sha256"]:
            return False, "weights do not match the manifest"
    return True, ""


class Laya(Engine):
    """
    The Laya decision model behind Diablo's interface.

    One `predict` call answers every question in LAYA_QUESTIONS. The model is
    loaded on first use, never at import. A checkpoint is loaded only if it
    passes verify_checkpoint; a missing package, a refused checkpoint or a
    failed load is reported as None so the caller falls back. Choices are
    read by name, never by position, so option order cannot change a verdict.
    """

    name = "laya"

    def __init__(self, router: Any = None, threshold: float = 0.5,
                 checkpoint: Optional[str] = None, device: Optional[str] = None):
        self._router = router
        self._threshold = threshold
        self._checkpoint = checkpoint
        self._device = device
        self._failed = False

    def _get(self):
        if self._router is None and not self._failed:
            try:
                if self._checkpoint:
                    ok, why = verify_checkpoint(self._checkpoint)
                    if not ok:
                        raise ValueError(why)
                    from laya import Agent
                    self._router = Agent(self._checkpoint, device=self._device)
                else:
                    from laya import Router
                    self._router = Router(device=self._device)
            except Exception as e:
                self._failed = True
                trace.event("decide", engine=self.name,
                            error=f"{type(e).__name__}: {e}"[:120])
        return self._router

    def raw(self, text: str, context: Optional[Dict[str, Any]] = None
            ) -> Optional[Dict[str, Any]]:
        router = self._get()
        if router is None:
            return None
        state = laya_state(text, **(context or {}))
        result = router.predict(state, LAYA_QUESTIONS)
        return (result or {}).get("answers")

    def judge(self, text: str, context: Optional[Dict[str, Any]] = None):
        """(Verdict, probabilities) or None. Never raises."""
        try:
            answers = self.raw(text, context)
            if not isinstance(answers, dict) or not answers:
                return None
            intent_answer = answers.get("intent") or {}
            intent = str(intent_answer.get("choice") or UNKNOWN)
            if intent not in LAYA_INTENTS:
                intent = UNKNOWN
            p_intent = float((intent_answer.get("probabilities") or {}).get(intent, 0.0))

            def p(key: str) -> Optional[float]:
                value = (answers.get(key) or {}).get("noul")
                return None if value is None else float(value)

            p_screen, p_complete = p("needs_screen"), p("complete")
            if p_screen is None or p_complete is None:
                return None
            screen = p_screen >= self._threshold
            complete = p_complete >= self._threshold
            verdict = Verdict(intent=intent, lane=lane_for(intent, screen, complete),
                              needs_screen=screen, needs_tools=intent in (DO, MISSION, COMPLEX),
                              complete=complete, engine=self.name, reason="laya")
            return verdict, {"intent": round(p_intent, 3), "needs_screen": round(p_screen, 3),
                             "complete": round(p_complete, 3)}
        except Exception as e:
            trace.event("decide", engine=self.name, error=f"{type(e).__name__}: {e}"[:120])
            return None

    def decide(self, text: str, in_conversation: bool = False) -> Optional[Verdict]:
        judged = self.judge(text)
        return judged[0] if judged else None


# ─── Shadow ──────────────────────────────────────────────────────────────────
#
# A second engine that judges the same utterance and changes nothing. It runs
# on one background thread with a queue of one: when it is busy the next
# utterance is not judged (and that is counted), so it can never delay a turn
# or pile up. What it logs is decisions and probabilities, never the words.

SHADOW = os.getenv("DIABLO_DECISION_SHADOW", "off").strip().lower()
LAYA_CHECKPOINT = os.getenv("DIABLO_LAYA_CHECKPOINT", "").strip()


class Shadow:
    """Judge in the background, log the comparison, never answer."""

    def __init__(self, factory, context=None, slow_ms: float = 2000.0):
        import queue
        self._factory = factory
        self._context = context
        self._slow_ms = slow_ms
        self._queue = queue.Queue(maxsize=1)
        self._engine = None
        self._disabled = False
        self._thread = None
        self._lock = __import__("threading").Lock()
        self.stats = {"seen": 0, "judged": 0, "dropped": 0, "failed": 0, "agreed": 0}

    def observe(self, text: str, in_conversation: bool, verdict: Optional[Verdict],
                turn: Optional[str]) -> None:
        if self._disabled:
            return
        import queue
        self.stats["seen"] += 1
        try:
            self._queue.put_nowait((text, in_conversation, verdict, turn))
        except queue.Full:
            self.stats["dropped"] += 1
            trace.event("decide_shadow", turn=turn, status="dropped")
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                import threading
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                 name="decision-shadow")
                self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop judging. Whatever is queued is dropped, not judged."""
        self._disabled = True
        try:
            while True:
                self._queue.get_nowait()
        except Exception:
            pass
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None or self._disabled:
                return
            text, in_conversation, verdict, turn = item
            self._judge(text, in_conversation, verdict, turn)

    def _judge(self, text, in_conversation, verdict, turn) -> None:
        if self._engine is None:
            try:
                self._engine = self._factory()
            except Exception as e:
                self._fail(turn, f"load: {type(e).__name__}")
                return
        context = {}
        if self._context is not None:
            try:
                context = self._context() or {}
            except Exception:
                context = {}
        started = time.perf_counter()
        judged = self._engine.judge(text, context)
        ms = round((time.perf_counter() - started) * 1000, 1)
        if judged is None:
            self._fail(turn, "no judgement")
            return
        shadow, probabilities = judged
        rules_lane = verdict.lane if verdict else "none"
        agree = shadow.lane == rules_lane or (
            rules_lane == "none" and shadow.lane != FAST)
        self.stats["judged"] += 1
        self.stats["agreed"] += int(agree)
        trace.event("decide_shadow", turn=turn, status="slow" if ms > self._slow_ms else "ok",
                    rules_lane=rules_lane, rules_intent=verdict.intent if verdict else "none",
                    shadow_lane=shadow.lane, shadow_intent=shadow.intent,
                    p_intent=probabilities["intent"], p_screen=probabilities["needs_screen"],
                    p_complete=probabilities["complete"], agree=agree, ms=ms,
                    in_conversation=bool(in_conversation))

    def _fail(self, turn, why: str) -> None:
        self.stats["failed"] += 1
        trace.event("decide_shadow", turn=turn, status="failed", reason=why)
        # A model that will not load does not get retried on every turn.
        if why.startswith("load") or (self._engine is not None and
                                       getattr(self._engine, "_failed", False)):
            self._disabled = True


def _shadow_context() -> Dict[str, Any]:
    """Which application is in front and the last two lines said, for the shadow only."""
    context: Dict[str, Any] = {}
    try:
        from skills import screen_context
        window = screen_context.frontmost_window() or {}
        context["app"] = window.get("app", "")
        context["window"] = window.get("title", "")
    except Exception:
        pass
    try:
        from skills import conversation
        turns = conversation.session.snapshot().get("turns") or []
        context["history"] = [t.get("content", "") for t in turns[-2:]]
    except Exception:
        pass
    return context


_shadow: Optional[Shadow] = None


def shadow() -> Optional[Shadow]:
    """The shadow engine, when DIABLO_DECISION_SHADOW=laya asks for one."""
    global _shadow
    if _shadow is None and SHADOW == "laya":
        _shadow = Shadow(lambda: Laya(checkpoint=LAYA_CHECKPOINT or None),
                         context=_shadow_context)
    return _shadow


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
    if verdict is not None:
        verdict = Verdict(**{**verdict.__dict__, "ms": ms})
        trace.event("decide", engine=verdict.engine, lane=verdict.lane,
                    intent=verdict.intent, needs_screen=verdict.needs_screen,
                    needs_tools=verdict.needs_tools, complete=verdict.complete, ms=ms,
                    reason=verdict.reason)
    else:
        trace.event("decide", engine=engine().name, lane="none", ms=ms)
    # The shadow sees the same utterance and the verdict that was used. It
    # cannot change it: this return value is decided above and nowhere else.
    try:
        second = shadow()
        if second is not None:
            second.observe(text, in_conversation, verdict, trace.current_turn())
    except Exception:
        pass
    return verdict
