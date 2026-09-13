"""
D.I.A.B.L.O. — deciding what kind of request this is, and how sure we are.

Diablo's router used to branch on one field: `understand()` returned "act",
"clarify" or "chat" and that was the whole decision. It works, but it cannot
express the two things that actually matter on a small local model:

    how sure are we           a 4B that is 40% sure and acts anyway is how
                              files get edited that nobody asked about
    how bad is being wrong    reading a file and deleting one are not the
                              same mistake, and should not pass the same gate

So this produces a Decision — one dataclass carrying goal, kind, confidence,
risk and what it needs — and the router gates on that instead.

Nothing here replaces `understand()`. It calls it, and adds the two axes it
does not have. The cheap regex path (`fast_understand`, 3.4 microseconds, zero
false positives across 30 real phrasings) still runs first and still wins.

Confidence is built from evidence, not asked for:

    the regex path fired                     0.95   it matched a known shape
    the model said "act" on a clear sentence 0.75   baseline for a clean parse
    a dangling pronoun is present            -0.30  "send it to him"
    a generic noun with no name              -0.20  "open the file"
    the model itself asked to clarify         0.25  it is telling us it is lost

Asking a model to rate its own confidence was tried first and produced 0.9 for
everything, which is the usual result. Counting the things that actually make a
sentence ambiguous is duller and works.
"""

import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import agent_core, agent_tools, screen_context, trace  # noqa: E402

# Below this, ask rather than act.
CLARIFY_BELOW = float(os.getenv("DIABLO_CONFIDENCE_CLARIFY", "0.45"))
# Below this on a task that touches files, hand it to the stronger model.
ESCALATE_BELOW = float(os.getenv("DIABLO_CONFIDENCE_ESCALATE", "0.70"))


class Kind:
    CONVERSATIONAL = "conversational"   # chat, remarks, thinking aloud
    GENERAL = "general"                 # a question with a factual answer
    SCREEN = "screen"                   # about what is visible
    ACTION = "action"                   # do something to files or the Mac
    AMBIGUOUS = "ambiguous"             # cannot proceed without asking
    COMPLEX = "complex"                 # multi-step, or beyond the small model


class Risk:
    NONE = "none"          # reading, answering, looking
    LOW = "low"            # creating something new
    MEDIUM = "medium"      # changing something that exists
    HIGH = "high"          # deleting, sending, shell


RISK_ORDER = [Risk.NONE, Risk.LOW, Risk.MEDIUM, Risk.HIGH]

# What a request would DO, judged from the verb rather than the tone. "Could
# you possibly delete the old drafts" is polite and destructive.
HIGH_RISK = re.compile(
    r"\b(delete|remove|erase|wipe|trash|rm\b|drop|destroy|"
    r"send|email|message|text|whatsapp|imessage|post|publish|reply|forward|"
    r"push|deploy|install|uninstall|sudo|chmod|chown|kill)\b", re.IGNORECASE)
MEDIUM_RISK = re.compile(
    r"\b(overwrite|replace|rename|move|edit|change|update|modify|append|"
    r"rewrite|fix|clean up|tidy|reorganise|reorganize|merge|sort)\b", re.IGNORECASE)
LOW_RISK = re.compile(
    r"\b(create|make|new|write|save|add|build|generate|draft|export)\b",
    re.IGNORECASE)

# Sentences that are plainly multi-step, where a plan beats improvisation.
COMPLEX_HINTS = re.compile(
    r"\b(and then|after that|then |also |as well as|followed by|"
    r"for each|every file|all of (them|the)|one by one)\b", re.IGNORECASE)


@dataclass
class Decision:
    """One request, classified. The router gates on this and nothing else."""

    text: str
    goal: str = ""
    kind: str = Kind.CONVERSATIONAL
    route: str = "chat"                    # chat | act | clarify | escalate
    confidence: float = 0.5
    risk: str = Risk.NONE
    needs_screen: bool = False
    needs_tools: bool = False
    needs_plan: bool = False
    question: str = ""
    reason: str = ""
    entities: List[str] = field(default_factory=list)

    @property
    def should_confirm(self) -> bool:
        """Risky enough to say out loud before doing it."""
        return self.risk in (Risk.MEDIUM, Risk.HIGH)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── The pieces of the judgement ─────────────────────────────────────────────

def assess_risk(text: str) -> str:
    """
    What the worst outcome would be if this ran and was wrong.

    Ordered most severe first, because "delete the file and send her a note"
    is a high-risk sentence that also contains a medium-risk verb.
    """
    if HIGH_RISK.search(text):
        return Risk.HIGH
    if MEDIUM_RISK.search(text):
        return Risk.MEDIUM
    if LOW_RISK.search(text):
        return Risk.LOW
    return Risk.NONE


def extract_entities(text: str) -> List[str]:
    """
    The nameable things in the sentence: filenames, folders, apps, quoted text.

    Deliberately literal. This is used to tell "open budget.csv" from "open the
    file", not to understand the sentence.
    """
    found: List[str] = []
    # No spaces. A greedy character class that included them matched backwards
    # across a whole clause: "total the revenue in sales.csv" came out as one
    # filename, which then read as a very specific request.
    found += re.findall(r"\b[\w\-.]+\.(?:md|txt|csv|xlsx|docx|pdf|py|json|pptx|png|jpg)\b",
                        text, re.IGNORECASE)
    found += re.findall(r"[\"']([^\"']{2,40})[\"']", text)
    for nickname in agent_tools.WORKSPACES:
        if re.search(rf"\b{re.escape(nickname)}\b", text, re.IGNORECASE):
            found.append(nickname)
    seen, unique = set(), []
    for item in (f.strip() for f in found):
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:8]


def score_confidence(text: str, verdict: Dict[str, Any], via_regex: bool,
                     entities: List[str]) -> tuple:
    """
    How sure we are, and the one-line reason. Returns (score, reason).

    Evidence, not introspection. The model was asked to rate itself during
    development and returned 0.9 for everything including "send it to him".
    """
    words = set(re.findall(r"[a-z']+", text.lower()))

    if via_regex:
        # The regex path is confident about the *shape* of a request, not about
        # its target. "delete the old drafts" matches the shape of an action
        # perfectly and names nothing, and it was scoring 0.95 — high enough to
        # skip every question and go straight to deleting files chosen by a 4B.
        # Certainty about a destructive verb has to come from knowing what it
        # points at.
        if assess_risk(text) == Risk.HIGH and not entities:
            generic = words & (agent_core.GENERIC | agent_core.DANGLING)
            if generic:
                return 0.40, (f"destructive, and {sorted(generic)[0]!r} names "
                              f"nothing in particular")
            return 0.55, "destructive, with nothing named"
        return 0.95, "matched a known request shape"

    if verdict.get("action") == "clarify":
        return 0.25, "the classifier could not pin it down"

    score = 0.75
    reasons = []

    dangling = words & agent_core.DANGLING
    if dangling and not entities:
        score -= 0.30
        reasons.append(f"unresolved {sorted(dangling)[0]!r}")

    generic = words & agent_core.GENERIC
    if generic and not entities:
        score -= 0.20
        reasons.append(f"{sorted(generic)[0]!r} with nothing naming it")

    if entities:
        score += 0.10
        reasons.append(f"names {entities[0]!r}")

    if len(text.split()) <= 2:
        score -= 0.10
        reasons.append("very short")

    if COMPLEX_HINTS.search(text):
        score -= 0.10
        reasons.append("several steps in one sentence")

    return max(0.0, min(1.0, score)), "; ".join(reasons) or "a clean parse"


def classify_kind(text: str, verdict: Dict[str, Any], needs_screen: bool) -> str:
    action = verdict.get("action")
    if needs_screen:
        return Kind.SCREEN
    if action == "clarify":
        return Kind.AMBIGUOUS
    if action == "chat":
        # A question with an answer is not the same as thinking aloud, and the
        # difference decides whether Diablo should reach for anything.
        if re.match(r"^\s*(what|who|when|where|why|how|which|is|are|does|do|can)\b",
                    text, re.IGNORECASE):
            return Kind.GENERAL
        return Kind.CONVERSATIONAL
    if COMPLEX_HINTS.search(text) or agent_core.needs_plan(text):
        return Kind.COMPLEX
    return Kind.ACTION


# ─── The decision ────────────────────────────────────────────────────────────

def classify(text: str, in_conversation: bool = False) -> Decision:
    """
    One request in, one Decision out.

    Order matters and is the whole optimisation: the free checks run first, the
    model runs once, and nothing runs twice.
    """
    text = (text or "").strip()
    if not text:
        return Decision(text="", route="chat", confidence=1.0, reason="nothing said")

    # Free.
    needs_screen = screen_context.wants_screen(text)
    entities = extract_entities(text)
    risk = assess_risk(text)

    # Nearly free — 3.4 microseconds, and it settles about half of all requests.
    quick = agent_core.fast_understand(text)
    via_regex = quick is not None and not in_conversation
    verdict = quick if via_regex else agent_core.understand(
        text, in_conversation=in_conversation)

    confidence, reason = score_confidence(text, verdict, via_regex, entities)
    kind = classify_kind(text, verdict, needs_screen)
    action = verdict.get("action", "chat")

    needs_tools = action == "act"
    route = action

    # ── the gates ────────────────────────────────────────────────────────
    if action == "act" and confidence < CLARIFY_BELOW:
        route = "clarify"
        reason = f"only {confidence:.0%} sure — {reason}"
    elif (action == "act" and needs_tools
          and confidence < ESCALATE_BELOW
          and os.getenv("DIABLO_ESCALATE_MODEL")):
        # Not sure enough to act alone, but clear enough that a question would
        # be annoying. This is the band a bigger model is for.
        route = "escalate"
        reason = f"{confidence:.0%} sure on a task that touches things — {reason}"

    decision = Decision(
        text=text,
        goal=verdict.get("task") or text,
        kind=kind,
        route=route,
        confidence=round(confidence, 2),
        risk=risk,
        needs_screen=needs_screen,
        needs_tools=needs_tools,
        needs_plan=agent_core.needs_plan(verdict.get("task") or text),
        question=verdict.get("question", ""),
        reason=reason,
        entities=entities,
    )

    trace.event("decide", route=decision.route, kind=decision.kind,
                confidence=decision.confidence, risk=decision.risk,
                needs_screen=decision.needs_screen, reason=decision.reason,
                via="regex" if via_regex else "model")
    return decision


if __name__ == "__main__":
    probes = [
        "what's a research question",
        "i'm knackered today",
        "what am i looking at",
        "read my econ ee draft",
        "open the file",
        "send it to him",
        "delete the old drafts",
        "total the revenue in sales.csv and write it to total.txt",
        "make a folder called reports",
        "read budget.csv and then email me a summary and also back it up",
    ]
    print(f"\n  clarify below {CLARIFY_BELOW}   escalate below {ESCALATE_BELOW}\n")
    print(f"  {'request':52} {'route':9} {'kind':15} {'conf':>5} {'risk':7} scr")
    print("  " + "-" * 100)
    for probe in probes:
        d = classify(probe)
        print(f"  {probe[:50]:52} {d.route:9} {d.kind:15} {d.confidence:5.2f} "
              f"{d.risk:7} {'yes' if d.needs_screen else '-'}   {d.reason[:40]}")
    print()
