"""
What the user asked for, as a kind of thing rather than as a string.

Everything in Diablo's perception stack was built around controls, and it is
good at them. Accessibility publishes a rectangle and a name, OCR reads the
printing on the glass, vision says which of several published rectangles is
the gear. All three answer the same question — "which control?" — and all
three return the geometry of their own evidence.

That is exactly why "where is the welfare loss region?" failed. OCR found the
words `Welfare loss` printed on a diagram and returned the rectangle of the
words, because the rectangle of the words is the only thing OCR has. Nothing
downstream could object, because the target had been a bare string since the
tool call: `point_at("the welfare loss region")` carries no notion that the
answer should be an AREA and that a caption is not one.

So this module holds the missing distinction, and nothing else. It does not
capture, does not call a model, does not touch AppKit, and does not know what
a window is. It answers three questions about a request:

    WHAT KIND of thing would answer this?
    WHICH INSTRUMENTS could see that kind?
    DOES THIS CANDIDATE actually have the shape of what was asked for?

The third is the guard the bug needed. A label may help find a region. It may
never be returned as one.

Six kinds, not the eight that were sketched. Each one is here because some
part of the runtime can already produce or consume it:

    CONTROL   accessibility and OCR both find these; guidance rings them
    TEXT      OCR's native output; the tutor already outlines regions of type
    REGION    an area inside a picture — the kind that was missing
    FIGURE    a whole chart, diagram or image, as one object
    OBJECT    whatever is under the cursor, when the question says "this"
    SCREEN    the whole screen; never a pointing target, and that matters

DIAGRAM_COMPONENT and ICON were sketched and are not here. A diagram component
is a REGION found inside a FIGURE and needs no separate handling; an icon is a
CONTROL the vision source already identifies by shape. Adding either would be
a name with no behaviour behind it.
"""

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

# ─── The kinds ───────────────────────────────────────────────────────────────

CONTROL = "control"
TEXT = "text"
REGION = "region"
FIGURE = "figure"
OBJECT = "object"
SCREEN = "screen"

KINDS = (CONTROL, TEXT, REGION, FIGURE, OBJECT, SCREEN)

# Kinds that can be pointed at. SCREEN is deliberately not one: "what's on my
# screen" is a question about everything, and drawing a ring around everything
# says nothing.
POINTABLE = (CONTROL, TEXT, REGION, FIGURE, OBJECT)


# ─── The instruments ─────────────────────────────────────────────────────────
#
# The same three names perception.py already tags its candidates with, plus
# the one this build adds. They are strings for the same reason Candidate's
# are: they end up in evidence a person reads.

ACCESSIBILITY = "accessibility"
OCR = "ocr"
VISION = "vision"
REGION_SOURCE = "region"

# Which instruments are worth asking, in order, for each kind. The order is
# the cost order that perception.locate already follows — accessibility at
# 80ms, OCR at about a second, vision at six to thirteen.
SOURCES = {
    CONTROL: (ACCESSIBILITY, OCR, VISION),
    TEXT:    (OCR, ACCESSIBILITY),
    # No accessibility: a diagram inside a PDF or a canvas publishes no
    # elements at all, so asking would cost 80ms to learn nothing. OCR is here
    # for the labels, which anchor the look — not for the answer.
    REGION:  (OCR, VISION),
    FIGURE:  (VISION, OCR),
    OBJECT:  (OCR, VISION),
    SCREEN:  (OCR,),
}


def sources_for(kind: str) -> Tuple[str, ...]:
    """Which instruments could answer a question of this kind, cheapest first."""
    return SOURCES.get(kind, SOURCES[CONTROL])


# ─── How far to look ─────────────────────────────────────────────────────────
#
# Full-screen analysis is the most expensive thing Diablo can do and the least
# likely to be right, because a model asked about 3.5 million pixels answers
# about whichever tenth of them is loudest. These are the scopes, narrowest
# first. Nothing escalates unless the narrower look could not answer.

CURSOR = "cursor"              # a small box around the pointer
ANCHOR = "anchor"              # around the label that names the thing
PANEL = "panel"                # the surrounding figure or column
WINDOW = "window"              # the whole window
SCREEN_SCOPE = "screen"        # every display; the last resort
CONTROL_SCOPE = "control"      # no capture at all: accessibility answered

LEVELS = (CURSOR, ANCHOR, PANEL, WINDOW, SCREEN_SCOPE)


def scope_for(target: "Target") -> Tuple[str, ...]:
    """
    The scopes to try, in order, for this request.

    A named region or figure is looked for in the whole window first, and that
    is a measured correction, made twice.

    Phase B took the caption itself out of the ladder: on the fixture the
    legend reading "Welfare loss" sat 396 points from the shape it named, so a
    crop centred on the caption captured faster and could not contain it.

    B.2 found the same mistake one rung up, and B.4 measured what it cost.
    PANEL was a 620-point box centred on the caption. On a 900-point fixture
    window that happened to cover everything; on a real 1476-point Preview
    window it covered the right-hand 60%, and the region for "consumer
    surplus" ran x 235..624 while the crop ran x 594..1480. On the real
    rendered price-floor page it cut the same region in half: IoU 0.34,
    against 0.97 when the first look was the whole window, 2 trials in 2.

    The caption is still evidence: it says the concept is on this screen and
    what the thing is called. It never decides where to look.

    Only areas and figures changed. A control has its own first look, text is
    read rather than looked at, and the cursor is the exception the whole
    argument rests on: the pointer really is on the thing, so the narrowest
    look there is also the most likely to be right. An object named without
    the pointer keeps the ladder it had.
    """
    if target.kind == CONTROL:
        return (CONTROL_SCOPE, WINDOW)
    if target.cursor_local:
        return (CURSOR, PANEL, WINDOW, SCREEN_SCOPE)
    if target.kind == SCREEN:
        return (SCREEN_SCOPE,)
    if target.kind in (REGION, FIGURE):
        return (WINDOW, SCREEN_SCOPE)
    return (PANEL, WINDOW, SCREEN_SCOPE)


# ─── Reading the question ────────────────────────────────────────────────────
#
# Vocabulary, not a model. A classifier that needs a model to decide which
# model to call has bought nothing, and this runs in microseconds on the
# phrasing the user actually used.

# Words that name an AREA of a picture. The economics ones are here because
# they are what the failure was about and because they are the clearest case
# of the distinction: a surplus is a shaded shape, and the words "consumer
# surplus" printed beside it are a caption.
REGION_WORDS = {
    "region", "area", "zone", "shaded", "shading", "wedge", "band", "patch",
    "triangle", "rectangle", "square", "surplus", "loss", "deadweight",
    "curve", "line", "slope", "intersection", "equilibrium", "segment",
    "arrow", "highlighted", "highlight", "overlap", "gap", "portion",
    "section", "quadrant", "axis", "bar", "slice", "wedge",
}

# Phrases that describe an area without naming one of the words above.
REGION_PHRASES = (
    "between the", "under the curve", "above the", "below the", "inside the",
    "the bit where", "the part where", "the part of", "shaded in",
)

# A whole picture, asked about as one object.
FIGURE_WORDS = {
    "graph", "chart", "diagram", "figure", "plot", "image", "picture",
    "photo", "illustration", "drawing", "screenshot", "map", "table",
}

# Things made of words.
TEXT_WORDS = {
    "paragraph", "sentence", "line", "word", "heading", "title", "caption",
    "footnote", "quote", "quotation", "text", "passage", "clause", "bullet",
    "list", "definition", "label",
}

# Things an application publishes and a person clicks.
CONTROL_WORDS = {
    "button", "menu", "tab", "field", "box", "checkbox", "toggle", "switch",
    "slider", "link", "option", "setting", "settings", "dropdown", "control",
    "icon", "toolbar", "sidebar", "panel", "item", "entry", "row", "cell",
    "input", "search", "window",
}

# The cursor is the subject when the question points rather than names.
CURSOR_PHRASES = (
    "under my cursor", "under the cursor", "under my mouse", "under my pointer",
    "my cursor", "my pointer", "i'm hovering", "im hovering", "hovering over",
    # Every way a person actually says it. "what am i pointing at" was missing
    # and the live cursor validation caught it: the question classified as a
    # CONTROL request, searched the screen for the words "what am i pointing
    # at", found nothing, and said so.
    "pointing at", "pointing to", "i'm pointing", "im pointing",
    "am i pointing", "am i hovering", "i am pointing", "i am hovering",
)

# "this" with nothing else in the sentence to attach it to.
DEICTIC = ("this", "that", "these", "those", "it", "here", "there")

# Words that survive the question-stripping regex but still name nothing on
# the screen. Without these, "what am i pointing at" looked like a request to
# find something called "what am i pointing at", because the leading pattern
# does not match "what am" and five words came out the other side.
EMPTY_WORDS = {
    "what", "whats", "where", "wheres", "which", "who", "how", "why",
    "is", "are", "am", "was", "were", "be", "do", "does", "did", "can",
    "i", "im", "me", "my", "mine", "you", "your",
    "at", "to", "on", "in", "of", "for", "the", "a", "an",
    "point", "pointing", "hover", "hovering", "cursor", "mouse", "pointer",
    "look", "looking", "see", "seeing", "show", "tell", "explain", "thing",
    "right", "now", "currently", "exactly", "sir", "please",
}


def _named(text: str) -> set:
    """The words in a request that could actually be looked for on a screen."""
    return _words(text) - set(DEICTIC) - EMPTY_WORDS

# The whole screen, asked about as a whole.
SCREEN_PHRASES = (
    "on my screen", "on the screen", "on screen", "my whole screen",
    "everything on", "what am i looking at", "what's in front of me",
    "whats in front of me", "my display", "my desktop",
)

# The scaffolding a question is wrapped in. Stripped so that what remains is
# the thing itself, which is what gets matched against the screen.
_LEAD = re.compile(
    r"^\s*(?:(?:please|hey|ok|okay|so|and|um|er)\s+)*"
    r"(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:(?:please|just)\s+)?"
    r"(?:show\s+me\s+(?:where\s+)?|point\s+(?:me\s+)?(?:to|at)\s+|point\s+out\s+|"
    r"tell\s+me\s+(?:where|what)\s+(?:is\s+|are\s+)?|"
    r"find\s+|locate\s+|highlight\s+|mark\s+|read\s+|explain\s+|describe\s+|"
    r"what\s+(?:does|do|is|are|'s)\s+|whats\s+|where\s+(?:is|are|'s)\s+|wheres\s+|"
    r"which\s+(?:is|one\s+is)\s+|how\s+do\s+i\s+find\s+)",
    re.I)

_TRAIL = re.compile(
    r"\s*(?:\bshow(?:s|ing)?\b.*|\bmean(?:s|ing)?\b.*|\bsay(?:s|ing)?\b.*|"
    r"\bon\s+(?:my|the)\s+screen\b.*|\bin\s+(?:this|that)\s+(?:window|app|tab)\b.*|"
    r"\bfor\s+me\b.*)?\s*[?.!]*\s*$", re.I)

_ARTICLE = re.compile(r"^\s*(?:the|a|an|my|this|that|these|those)\s+", re.I)


def _strip(text: str) -> str:
    """What is left of a question once the asking is taken off it."""
    body = (text or "").strip()
    previous = None
    while body and body != previous:
        previous = body
        body = _LEAD.sub("", body, count=1).strip()
    body = _TRAIL.sub("", body, count=1).strip()
    # One article, not a loop: "the the" came out of a sentence built from
    # this, and "a list of the things" must keep its second article.
    body = _ARTICLE.sub("", body, count=1).strip()
    return body.strip(" ,.?!")


def _words(text: str) -> set:
    return set(re.findall(r"[a-z']+", (text or "").lower()))


@dataclass
class Target:
    """What is wanted, and how sure we are about what kind it is."""

    semantic: str = ""            # the thing itself, in the user's words
    kind: str = CONTROL
    cursor_local: bool = False    # the question pointed rather than named
    vague: bool = False           # it named nothing at all
    reason: str = ""              # why this kind, for the trace
    # What the user wants DONE, when they asked for an action rather than
    # about a thing: CREATE for "open a new note". Empty for every question
    # about something. It is a property of the request, not a kind: the kind
    # of an action's answer is CONTROL, and this says which control will do.
    operation: str = ""

    @property
    def pointable(self) -> bool:
        """Whether there is, in principle, one thing to put a ring around."""
        if self.vague:
            return False
        if self.kind not in POINTABLE:
            return False
        # OBJECT is the cursor's own answer and needs no name; everything else
        # has to have named something.
        return bool(self.semantic) or self.cursor_local


def read_question(text: str, stated: str = "") -> Target:
    """
    What kind of answer this question wants, and what is to be done, if anything.

    An action ("show me how to open a new note") is read first, because its
    answer is a control whatever the nouns in it are: "note" would otherwise
    be looked for, and a note is what was found.
    """
    action = _action_of(text)
    wanted = str(stated or "").strip().lower()
    if action is not None and wanted in ("", CONTROL):
        return action
    return _read_kind(text, stated)


def _read_kind(text: str, stated: str = "") -> Target:
    """
    What kind of answer this question wants.

    `stated` is the decision layer's own verdict, when it has one. The planner
    knows what the user wants better than a regular expression does, so when
    it says "region" that is taken. It is not required, because the phrasing
    carries the same information most of the time and a tool call that omits
    it must still work.
    """
    raw = (text or "").strip()
    lowered = raw.lower()
    semantic = _strip(raw)
    words = _words(semantic) | _words(lowered)

    cursor_local = any(phrase in lowered for phrase in CURSOR_PHRASES)

    if stated:
        wanted = str(stated).strip().lower()
        if wanted in KINDS:
            return Target(semantic=semantic, kind=wanted,
                          cursor_local=cursor_local,
                          vague=_is_vague(semantic, cursor_local, wanted),
                          reason="stated by the decision layer")

    # The whole screen, before anything else: "what's on my screen" contains
    # "screen", which is also a control word.
    if any(phrase in lowered for phrase in SCREEN_PHRASES) and not semantic:
        return Target(semantic="", kind=SCREEN, reason="asked about the screen itself")
    if any(phrase in lowered for phrase in SCREEN_PHRASES) and \
            not (words & (REGION_WORDS | FIGURE_WORDS | CONTROL_WORDS | TEXT_WORDS)):
        return Target(semantic="", kind=SCREEN, reason="asked about the screen itself")

    # A question that points instead of naming.
    #
    # "where is it" is not one of these, even though it is as deictic as they
    # come. The cursor can answer "what is this", because the pointer is
    # already on the thing; it cannot answer "where is it", because the answer
    # to that would be "under your pointer" and that is not what was asked.
    bare = _named(semantic)
    asks_where = bool(re.search(r"\bwhere\b", lowered))
    if cursor_local or (not bare and not asks_where
                        and _words(lowered) & set(DEICTIC)):
        # "what does this graph show" points AND names; the noun wins.
        if words & FIGURE_WORDS:
            return Target(semantic=semantic, kind=FIGURE, cursor_local=True,
                          reason="a picture, indicated rather than named")
        if not bare:
            return Target(semantic=semantic, kind=OBJECT, cursor_local=True,
                          reason="named nothing; the cursor says which thing")

    # A whole picture asked about as a whole: "what does this graph show".
    if words & FIGURE_WORDS:
        asking_about_it = any(v in lowered for v in
                              ("show", "shows", "showing", "mean", "means",
                               "about", "saying", "tell me about", "explain",
                               "describe", "what is", "what's", "whats"))
        naming_a_part = bool(words & REGION_WORDS) or \
            any(p in lowered for p in REGION_PHRASES)
        if asking_about_it and not naming_a_part:
            return Target(semantic=semantic, kind=FIGURE,
                          reason="asked what a whole figure shows")

    # An area of a picture.
    if (words & REGION_WORDS) or any(p in lowered for p in REGION_PHRASES):
        # A control word wins only when it is the head noun: "the settings
        # area" is a panel of controls, "the shaded area" is not.
        if words & CONTROL_WORDS and not (words & {"shaded", "shading",
                                                   "surplus", "deadweight",
                                                   "triangle", "highlighted"}):
            head = semantic.lower().split()[-1] if semantic.split() else ""
            if head in CONTROL_WORDS:
                return Target(semantic=semantic, kind=CONTROL,
                              reason="an area of controls, not of a picture")
        return Target(semantic=semantic, kind=REGION,
                      vague=_is_vague(semantic, cursor_local, REGION),
                      reason="named an area rather than a control")

    if words & TEXT_WORDS:
        return Target(semantic=semantic, kind=TEXT,
                      reason="asked for something made of words")

    # Asked to have something explained, and it is not a control. "Explain
    # the subsidy" came out as CONTROL, the fallback below, and a control is a
    # thing to press: grounding went looking for a button called "subsidy" and
    # found the word. An explanation is answered from the picture the thing is
    # part of, which is what FIGURE means. A control word still wins, so
    # "explain the Save button" is the button.
    if _EXPLAINING.search(lowered) and _named(semantic) and \
            not (words & CONTROL_WORDS):
        return Target(semantic=semantic, kind=FIGURE,
                      reason="asked to explain something, which the picture "
                             "it is part of shows")

    return Target(semantic=semantic, kind=CONTROL,
                  cursor_local=cursor_local,
                  vague=_is_vague(semantic, cursor_local, CONTROL),
                  reason="named a thing in the interface")


def _is_vague(semantic: str, cursor_local: bool, kind: str) -> bool:
    """Whether the question named anything a screen could be searched for."""
    if cursor_local:
        return False
    if kind == SCREEN:
        return False
    return not _named(semantic)


# ─── Does this candidate have the shape that was asked for? ──────────────────
#
# The guard the welfare-loss bug needed, and the whole reason this module
# exists. Everything above decides what to look for; this decides whether what
# came back is that thing.

# A region has to be bigger than a line of type to be a region at all. A line
# of body text on a Retina display is 14 to 22 points tall, and a caption
# accepted as a "region" is exactly the bug.
MIN_REGION_EDGE = 24.0

# How much bigger than the caption a box has to be before it is plausibly the
# thing rather than a box drawn round the caption. Three times the area of a
# line of type is still a very small region, so this refuses only the case it
# is aimed at.
CAPTION_MULTIPLE = 3.0


def boxed_the_caption(candidate: Any, anchor: Any) -> bool:
    """
    Whether a returned region is really just the caption with a border on it.

    The failure this build exists to stop has a disguised form: instead of
    returning the label's rectangle, the model returns a slightly larger
    rectangle that contains the label. Same mistake, harder to see. A region
    that swallows its own caption and is barely bigger than it is that.
    """
    if candidate is None or not anchor:
        return False
    try:
        ax0 = float(anchor["left"]); ay0 = float(anchor["top"])
        aw = float(anchor["width"]); ah = float(anchor["height"])
    except (KeyError, TypeError, ValueError):
        return False
    if aw <= 0 or ah <= 0:
        return False
    cx0, cy0 = float(candidate.x), float(candidate.y)
    cx1, cy1 = cx0 + float(candidate.width), cy0 + float(candidate.height)
    contains = (cx0 <= ax0 + 2 and cy0 <= ay0 + 2
                and cx1 >= ax0 + aw - 2 and cy1 >= ay0 + ah - 2)
    if not contains:
        return False
    return (float(candidate.width) * float(candidate.height)
            <= aw * ah * CAPTION_MULTIPLE)


def satisfies(kind: str, candidate: Any) -> bool:
    """Whether a candidate's geometry is the kind of thing that was requested."""
    if candidate is None:
        return False
    width = float(getattr(candidate, "width", 0.0) or 0.0)
    height = float(getattr(candidate, "height", 0.0) or 0.0)
    sources = set(getattr(candidate, "sources", []) or [])
    role = str(getattr(candidate, "role", "") or "").lower()

    if kind in (REGION, FIGURE):
        # The rule. A candidate whose only evidence is that some words match
        # is a label, and a label is not an area — however well it matched.
        if REGION_SOURCE not in sources:
            return False
        if width < MIN_REGION_EDGE or height < MIN_REGION_EDGE:
            return False
        return True

    if kind == OBJECT:
        # Whatever is there, as long as it is somewhere. The cursor already
        # said which thing; this only refuses an empty box.
        return width > 0.0 and height > 0.0

    if kind == TEXT:
        return role in ("text", "region", "axstatictext", "axtextarea",
                        "axtextfield") or OCR in sources or REGION_SOURCE in sources

    if kind == CONTROL:
        # Deliberately permissive. A button in an application with no
        # accessibility tree is found by its printed label and nothing else,
        # and that has worked for a long time. Refusing it here would trade a
        # real bug for a regression.
        return width > 0.0 and height > 0.0

    return width > 0.0 and height > 0.0


def why_not(kind: str, candidate: Any) -> str:
    """One sentence saying why a candidate was refused, for the trace."""
    if candidate is None:
        return "there was no candidate"
    if satisfies(kind, candidate):
        return ""
    sources = set(getattr(candidate, "sources", []) or [])
    label = str(getattr(candidate, "label", "") or "")[:40]
    if kind in (REGION, FIGURE) and REGION_SOURCE not in sources:
        return (f"“{label}” is a label read off the screen, not the area it "
                f"names — matching words is not the same as finding the thing")
    if kind in (REGION, FIGURE):
        return (f"“{label}” is {getattr(candidate, 'width', 0):.0f} by "
                f"{getattr(candidate, 'height', 0):.0f} points, too small to "
                f"be the area asked for")
    return f"“{label}” is not the kind of thing the question asked for"


def abstain(target: Target) -> str:
    """What to say when the requested thing could not be grounded."""
    if target.kind == SCREEN:
        return ("That is a question about the whole screen rather than one "
                "thing on it, so there is nothing for me to point at.")
    if target.vague:
        return "Which thing do you mean, Sir? I need something to look for."
    what = target.semantic or "that"
    if target.kind in (REGION, FIGURE):
        return (f"I can see the words but I cannot make out the {what} itself, "
                f"Sir. I would rather say so than ring the caption.")
    return f"I cannot find {what} on screen, Sir."


# ─── Is this a request to be shown something? ────────────────────────────────
#
# The conversational half of the teaching system. GUI-X could point, outline,
# lead a cursor and walk a path, and a real turn reached none of it: the
# router sent "where is the welfare loss region?" to a clarifying question,
# the 4B answered "show me the equilibrium" by asking whether that meant
# chemical equilibrium, and "show me how the subsidy causes welfare loss" went
# to the tutor, which anchors every step on printed words. The phrasing
# already said what was wanted. It is read here, in code, for the same reason
# the kinds above are: it has to be right far more often than it has to be
# clever, and the model was the thing getting it wrong.
#
# Three intents, by what the answer has to do:
#
#     LOCATE    show where one thing is             "where is the welfare loss"
#     EXPLAIN   show one thing and say what it is   "what does this triangle mean"
#     TRACE     show several, in order              "show me how X causes Y"
#
# "Certain" means the phrasing alone says the thing is on the screen: it named
# an area, a figure, a control, or the pointer. "Explain the subsidy" is not
# certain. It could be about the diagram in front of him or a question for a
# spoken answer, and only the screen can say which.

LOCATE = "locate"
EXPLAIN = "explain"
TRACE = "trace"

_OPENING = (r"^\s*(?:(?:please|hey|ok|okay|so|and|um|er|diablo|sir)[\s,]+)*"
            r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+|just\s+)?")

# How to DO something is a procedure, and procedures are the tutor's: "show me
# how to save this", "how do I export". How one thing LEADS to another is not.
_PROCEDURE = re.compile(
    r"\bhow\s+(?:to\b|(?:do|can|could|would|should|might)\s+(?:i|you|we)\b)", re.I)

_TRACE_ASK = re.compile(
    _OPENING + r"(?:(?:show|tell)\s+me\s+how|explain\s+how|"
               r"(?:walk|take|talk)\s+me\s+through|trace|"
               r"show\s+(?:me\s+)?what\s+happens)\b", re.I)
# The two of those that say "show it to me" even when nothing visual is named.
_TRACE_SHOWN = re.compile(_OPENING + r"(?:show\s+me\s+how|explain\s+how)\b", re.I)

_LOCATE_ASK = re.compile(
    _OPENING + r"(?:where\s+(?:is|are)|where's|wheres|show\s+me\s+where|"
               r"show\s+me|point\s+(?:me\s+)?(?:to|at)|point\s+out|highlight)\b", re.I)
# Verbs that only ever mean the screen.
_POINTING = re.compile(r"\bpoint\s+(?:me\s+)?(?:to|at|out)\b|\bhighlight\b", re.I)

_EXPLAINING = re.compile(_OPENING + r"(?:explain|describe)\b", re.I)
_EXPLAIN_ASK = re.compile(
    _OPENING + r"(?:explain|describe)\b|"
    r"\bwhat\s+(?:does|do)\b.+\b(?:mean|means|represent|represents|show|shows|"
    r"stand\s+for)\b|"
    r"\bwhat\s+(?:is|are|'s)\s+(?:this|that|these|those)\s+[a-z]", re.I)


@dataclass(frozen=True)
class Shown:
    """A request to be shown something, as the decision layer carries it."""

    intent: str                   # LOCATE, EXPLAIN or TRACE
    target: Target                # what was named, and what kind of thing it is
    certain: bool                 # the phrasing alone puts it on the screen
    reason: str = ""


def _visual(t: Target, lowered: str) -> bool:
    """Whether what was named is, by its own words, something on the screen."""
    if any(phrase in lowered for phrase in CURSOR_PHRASES):
        return True
    # OBJECT only counts when the pointer was named. read_question infers it
    # from a bare "this", and "explain this" is the tutor's maths request.
    if t.kind == REGION:
        return True
    if t.kind == FIGURE:
        return bool(_words(lowered) & FIGURE_WORDS)
    if t.kind == CONTROL:
        return bool(_words(t.semantic) & CONTROL_WORDS)
    return False


def asked_to_show(text: str) -> Optional[Shown]:
    """
    Whether this is a request to be shown something on screen, and which kind.

    Returns None for everything else, including requests that name nothing
    ("where is it" is still a question back), procedures (the tutor's), and
    places and things that are not on a screen at all ("where is Paris",
    "show me my calendar").
    """
    raw = (text or "").replace("’", "'").strip()
    lowered = raw.lower()
    if not lowered:
        return None
    pointed = any(phrase in lowered for phrase in CURSOR_PHRASES)

    # How to do something the screen has a control for: the control. Only
    # when the operation is one this module knows; any other procedure ("how
    # to change my wallpaper") is still the tutor's.
    if _GUIDE_ASK.search(raw):
        t = read_question(raw)
        if t.operation and not t.vague:
            return Shown(GUIDE, t, True, f"asked how to {t.operation} something, "
                                         f"which a control on screen does")

    if _TRACE_ASK.search(raw) and not _PROCEDURE.search(raw):
        # What follows the asking is the thing: "walk me through | this graph".
        body = _TRACE_ASK.sub("", raw, count=1).strip(" ,.?!")
        t = read_question(body or raw)
        if t.vague and not pointed:
            return None
        # A trace is through a picture. A control named here is a procedure
        # ("walk me through the settings") and the tutor's, not a path.
        words = _words(lowered)
        pictured = bool(words & (REGION_WORDS | FIGURE_WORDS)) or \
            any(p in lowered for p in REGION_PHRASES)
        if pointed or pictured:
            if t.kind not in (REGION, FIGURE):
                t = Target(semantic=t.semantic,
                           kind=FIGURE if words & FIGURE_WORDS else REGION,
                           reason="traced through a picture")
            return Shown(TRACE, t, True, "asked how one thing leads to another, "
                                         "in a picture on screen")
        if _TRACE_SHOWN.search(raw) and _named(t.semantic):
            return Shown(TRACE, t, False, "asked to be shown how something works; "
                                          "the screen decides whether it is there")
        return None

    if _EXPLAIN_ASK.search(raw) or pointed:
        t = read_question(raw)
        if pointed:
            return Shown(EXPLAIN, t, True, "asked about the thing under the pointer")
        if t.vague or t.kind in (TEXT, SCREEN):
            # Words are read, not shown; a bare "explain this" is the tutor's.
            return None
        if _visual(t, lowered):
            return Shown(EXPLAIN, t, True, "asked what something on screen is")
        if t.kind == FIGURE and t.semantic:
            return Shown(EXPLAIN, t, False, "asked to explain something that may "
                                            "be on screen")
        return None

    if _LOCATE_ASK.search(raw) and not _PROCEDURE.search(raw):
        t = read_question(raw)
        if t.vague or t.kind == SCREEN:
            return None
        # A bare "show me" is only about the screen when what follows is part
        # of a picture. "Show me the settings" and "show me the map" most
        # likely mean open them, and the planner is still the one for that.
        asked_where = bool(re.search(r"\bwhere\b", lowered) or _POINTING.search(raw))
        if t.kind == REGION or any(p in lowered for p in CURSOR_PHRASES):
            return Shown(LOCATE, t, True, "asked where part of a picture is")
        if asked_where and (_visual(t, lowered) or
                            (_POINTING.search(raw) and t.semantic)):
            return Shown(LOCATE, t, True, "asked where something on screen is")
        if t.kind == TEXT and re.search(r"\bwhere\b", lowered):
            return Shown(LOCATE, t, True, "asked where some words are")
        # "Where is Diablo Notes?" and "where is Paris?" have the same shape.
        # Only the screen can tell them apart, so it is asked (B.3), exactly as
        # "explain the subsidy" is: shown if the name is there, otherwise the
        # turn goes on as it would have.
        if re.search(r"\bwhere\b", lowered) and _named(t.semantic) and \
                t.kind in (CONTROL, TEXT):
            return Shown(LOCATE, t, False, "asked where something named is; the "
                                           "screen decides whether it is there")
        return None

    return None


# ─── Which content: "in Preview", "in the Subsidy and Welfare document" ─────
#
# B.5.1. A question can name where to look as well as what to look for, and
# the window it names outranks whichever one happens to be in front. It is
# read off the END of the question and nowhere else, and only when what
# follows the preposition could be a window: "the gain in consumer surplus" is
# one thing, "the welfare loss in Preview" is a thing and a place, and "the dot
# in the top left corner of the graph" is a thing and where in the picture.

# The last word of a phrase that says what sort of content it is, not which.
CONTAINER_WORDS = {
    "pdf", "diagram", "chart", "document", "doc", "page", "graph", "figure",
    "window", "tab", "screen", "app", "application", "picture", "image",
    "slide", "slides", "video", "file", "deck", "presentation", "sheet",
}

# Words that make a trailing phrase a place in the picture, not a window.
_PLACE_WORDS = {
    "of", "at", "near", "between", "under", "above", "below", "beside", "top",
    "bottom", "left", "right", "corner", "middle", "centre", "center", "side",
    "edge", "half", "part", "area", "region",
}

_SCOPE_TAIL = re.compile(
    r"^(?P<body>.*\S)\s+(?P<prep>in|on|from|inside|within)\s+"
    r"(?P<article>(?:the|this|that|my|these|those)\s+)?"
    r"(?P<name>[^?.!,]+?)\s*[?.!]*\s*$", re.I | re.S)


@dataclass(frozen=True)
class Scope:
    """The content a question named, and the question without it."""

    body: str                 # the question with the phrase taken off
    name: str                 # what names the window: "Preview", "Subsidy and Welfare"
    said: str                 # the phrase as it was said, for an answer
    generic: bool = False     # "this diagram": the content in front, by kind
    explicit: bool = False    # a proper name or "... document": said on purpose


def scope_of(text: str) -> Optional[Scope]:
    """
    The content a question names at its end, if it names one. None otherwise.

    Explicit means the phrase could only have been meant as a window: a
    capitalised name ("in Preview") or a kind of content with a name in front
    of it ("in the lecture video"). A lowercase phrase with neither ("in
    preview", as speech-to-text sometimes writes it) is only a scope if an
    application is actually called that, which the caller checks.
    """
    raw = (text or "").replace("’", "'").strip()
    found = _SCOPE_TAIL.match(raw)
    if not found:
        return None
    said = found.group("name").strip()
    words = said.split()
    if not words or len(words) > 6:
        return None
    head = words[-1].lower()
    named = words[:-1] if head in CONTAINER_WORDS else words
    if {w.lower() for w in named} & _PLACE_WORDS:
        return None
    article = (found.group("article") or "").strip()
    phrase = f"{article} {said}".strip()
    body = found.group("body").strip()
    if not named:
        # "in this diagram", "on the page": what is in front, by its kind.
        return Scope(body=body, name="", said=phrase, generic=True)
    if found.group("prep").lower() == "on" and head not in CONTAINER_WORDS:
        return None               # "the dot on the demand curve"
    explicit = head in CONTAINER_WORDS or any(w[:1].isupper() for w in named)
    return Scope(body=body, name=" ".join(named), said=phrase, explicit=explicit)


# ─── Actions: what is to be done, and which control does it ─────────────────
#
# B.3. "Show me how to open a new note" put the marker on the note's title.
# The request had a subject (a note) and an operation (create), and only the
# subject reached anything that looked at the screen, so the thing the note
# is called was found instead of the button that makes one.
#
# So an operation is carried with the request, and the answer to it has to be
# a control whose own words offer that operation. This is vocabulary about
# ACTIONS, not about any application: nothing here knows where a button is,
# what Notes looks like, or that a pencil means anything.

GUIDE = "guide"      # a fourth intent beside LOCATE, EXPLAIN and TRACE

CREATE = "create"
OPEN = "open"
SAVE = "save"
EXPORT = "export"
CLOSE = "close"
SEARCH = "search"
DELETE = "delete"
PLAY = "play"
PAUSE = "pause"
PRINT = "print"
SHARE = "share"
SEND = "send"
DOWNLOAD = "download"
UPLOAD = "upload"
UNDO = "undo"
REDO = "redo"
RENAME = "rename"
REFRESH = "refresh"

# The verbs a person uses for each operation. "find" is deliberately absent:
# "find the Save button" is a request to be shown where, not to search.
OPERATION_VERBS = {
    CREATE: ("create", "compose", "add"),
    OPEN: ("open",), SAVE: ("save",), EXPORT: ("export",), CLOSE: ("close",),
    SEARCH: ("search",), DELETE: ("delete", "remove", "trash", "erase"),
    PLAY: ("play", "resume"), PAUSE: ("pause",), PRINT: ("print",),
    SHARE: ("share",), SEND: ("send",), DOWNLOAD: ("download",),
    UPLOAD: ("upload",), UNDO: ("undo",), REDO: ("redo",),
    RENAME: ("rename",), REFRESH: ("refresh", "reload"),
}
_VERB_TO_OPERATION = {verb: op for op, verbs in OPERATION_VERBS.items()
                      for verb in verbs}

# Verbs that make something only when the thing is new: "start a new note" is
# CREATE, "start the timer" is not, and "make the text bold" is a procedure.
_NEW_VERBS = {"start", "begin", "make", "write", "get"}

# The words a control uses to say it performs an operation. Whole words only:
# "Saved" is a status, "Closed tickets" is a list, "Playlist" is a name.
AFFORDANCE_WORDS = {
    CREATE: {"new", "create", "add", "compose", "plus", "write"},
    OPEN: {"open", "browse"}, SAVE: {"save"}, EXPORT: {"export"},
    CLOSE: {"close"}, SEARCH: {"search", "find"},
    DELETE: {"delete", "remove", "trash", "bin"}, PLAY: {"play", "resume"},
    PAUSE: {"pause"}, PRINT: {"print"}, SHARE: {"share"}, SEND: {"send"},
    DOWNLOAD: {"download"}, UPLOAD: {"upload"}, UNDO: {"undo"},
    REDO: {"redo"}, RENAME: {"rename"}, REFRESH: {"refresh", "reload"},
}

# How a person asks to be taught an action rather than to have it done.
_GUIDE_ASK = re.compile(
    _OPENING + r"(?:(?:show|tell|teach)\s+me\s+how\s+(?:to|do\s+i|i\s+can)|"
               r"how\s+(?:do|can|would|should)\s+(?:i|you|we)|how\s+to|"
               r"where\s+do\s+i)\b", re.I)

# The same leads, plus a bare imperative: "Create a new note."
_ACTION_LEAD = re.compile(
    _OPENING + r"(?:(?:(?:show|tell|teach)\s+me\s+how\s+(?:to|do\s+i|i\s+can)|"
               r"how\s+(?:do|can|would|should)\s+(?:i|you|we)|how\s+to|"
               r"where\s+do\s+i(?:\s+(?:go|click))?(?:\s+to)?|help\s+me)\s+)?"
               r"(?P<verb>[a-z]+)\b(?P<rest>[^?.!]*)", re.I)

# Words in an action's object that name nothing to look for.
_OBJECT_FILLER = set(DEICTIC) | {"a", "an", "the", "my", "your", "our", "some",
                                 "one", "for", "me", "please", "now", "here", "up"}


def affords(operation: str, text: str) -> bool:
    """Whether a control's own words say it performs this operation."""
    offered = AFFORDANCE_WORDS.get(operation or "")
    if not offered or not text:
        return False
    if operation == CREATE and "+" in text:
        return True
    return bool(set(re.findall(r"[a-z]+", text.lower())) & offered)


def _action_of(text: str) -> Optional["Target"]:
    """
    The action a request asks for, as a control target, or None.

    Two shapes carry one. A verb that leads the request, taught or bare —
    "show me how to save this", "create a new note". Or a control named by
    what it does — "the export button". Anything else is a question about a
    thing, and gets no operation at all: an action is never guessed.
    """
    raw = (text or "").replace("\u2019", "'").strip()
    match = _ACTION_LEAD.match(raw)
    if match:
        verb = match.group("verb").lower()
        thing = [w for w in re.findall(r"[a-z]+", match.group("rest").lower())
                 if w not in _OBJECT_FILLER]
        operation = _VERB_TO_OPERATION.get(verb) or \
            (CREATE if verb in _NEW_VERBS and "new" in thing else "")
        if operation:
            if "new" in thing:
                # "Open a new note", "start a new tab": whatever the verb, the
                # control that answers it is the one that makes one.
                operation = CREATE
                thing = thing[thing.index("new"):]
                semantic = " ".join(thing)
            else:
                semantic = " ".join([verb] + thing)
            return Target(semantic=semantic, kind=CONTROL, operation=operation,
                          reason=f"asked how to {operation} something; a control "
                                 f"does that")
    # "Where is the export button": a control, named by its operation.
    words = re.findall(r"[a-z]+", raw.lower())
    if words and words[-1] in CONTROL_WORDS:
        for word in words[:-1]:
            operation = _VERB_TO_OPERATION.get(word) or (CREATE if word == "new" else "")
            if operation:
                return Target(semantic=_strip(raw), kind=CONTROL, operation=operation,
                              reason=f"named a control by what it does ({operation})")
    return None


def cannot_do(wanted: "Target", disabled: str = "") -> str:
    """What to say when no control on screen offers the operation."""
    if disabled:
        return (f"The {disabled} control is there, Sir, but it is disabled right "
                f"now, so it can't be used yet.")
    what = wanted.semantic or wanted.operation or "that"
    return (f"I can't see a control on screen for {what}, Sir, and I won't point "
            f"at something that only mentions it.")
