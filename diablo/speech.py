"""
D.I.A.B.L.O. — turning text into something worth hearing.

Everything Diablo produces is read aloud, and a surprising amount of it is
written for the eye. A model that has been told not to use markdown will still
say "qwen3:4b-instruct-2507-q4_K_M", and a speech engine reads that as
"kwen three colon four b hyphen instruct hyphen two five zero seven hyphen q
four underscore k underscore m", which takes nine seconds and means nothing.

So this is the last thing between the model and the speaker. Three jobs:

    strip      markdown, code fences, bullets — punctuation that is silent on
               a page and absurd out loud
    translate  URLs to domains, version strings to spoken numbers, symbols to
               words, file extensions to how a person says them
    segment    split on sentence boundaries so speech can start on the first
               one while the rest is still being generated

The segmentation is the part that changes how fast Diablo feels. Speaking from
the first sentence turns a six-second wait into a two-second one, and the
remaining four seconds happen while he is already listening.
"""

import os
import re
from typing import Iterator, List

# Said aloud, these are the difference between a sentence and a spelling test.
SYMBOLS = {
    "&": " and ", "@": " at ", "%": " percent", "+": " plus ",
    "=": " equals ", "~": " about ", "|": " ", "#": " number ",
    "<": " less than ", ">": " greater than ", "*": " ", "_": " ",
    "/": " slash ", "\\": " ", "^": " ", "`": "",
}

UNITS = [
    (re.compile(r"\btok/s\b", re.IGNORECASE), " tokens per second"),
    (re.compile(r"\b(\d+)\s*GB\b", re.IGNORECASE), r"\1 gigabytes"),
    (re.compile(r"\b(\d+)\s*MB\b", re.IGNORECASE), r"\1 megabytes"),
    (re.compile(r"\b(\d+)\s*ms\b"), r"\1 milliseconds"),
]

EXTENSIONS = {
    ".md": " markdown file", ".txt": " text file", ".csv": " C S V file",
    ".xlsx": " Excel file", ".docx": " Word document", ".pdf": " P D F",
    ".py": " Python file", ".json": " JSON file", ".pptx": " PowerPoint file",
    ".png": " image", ".jpg": " image", ".sh": " shell script",
}

# Quantisation tags, model names and the like. Spelled out they are noise; a
# short spoken form carries the same meaning.
JARGON = [
    (re.compile(r"\bq(\d)_k_m\b", re.IGNORECASE),  r"four-bit"),
    (re.compile(r"\bq(\d)_0\b", re.IGNORECASE),    r"\1-bit"),
    (re.compile(r"\bfp16\b", re.IGNORECASE),       "sixteen-bit"),
    (re.compile(r"\bgguf\b", re.IGNORECASE),       "G G U F"),
    (re.compile(r"\bhttps?://\S*", re.IGNORECASE), ""),   # handled separately
]

_URL = re.compile(r"https?://([^\s/]+)(\S*)", re.IGNORECASE)
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]{1,60})`")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*+•]\s+", re.MULTILINE)
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
# The lookarounds matter: without them "q4_K_M" reads as italics around a
# K and comes out "q4KM", which then matches no jargon rule at all.
_EMPHASIS = re.compile(r"(?<![\w])(\*\*|__|\*|_)(\S.*?\S|\S)\1(?![\w])", re.DOTALL)
_PATH = re.compile(r"(?:/[\w.\-]+){2,}")
# Requires either a leading v or three components. Without that, every
# decimal in a sentence became a version number: "3.2 GB" was read aloud
# as "version 3 point 2 GB".
_VERSION = re.compile(r"\bv(\d+)\.(\d+)(?:\.(\d+))?\b|\b(\d+)\.(\d+)\.(\d+)\b")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _say_url(match: re.Match) -> str:
    """A domain is sayable. A query string is not."""
    domain = match.group(1).lower().removeprefix("www.")
    return f" {domain.replace('.', ' dot ')} "


def _say_version(match: re.Match) -> str:
    parts = [g for g in match.groups() if g]
    return "version " + " point ".join(parts)


def spoken(text: str) -> str:
    """
    The same meaning, written the way it should sound.

    Order matters: code fences go before inline code, links before emphasis,
    and paths before symbols — otherwise the slash rule shreds a file path into
    individual words.
    """
    if not text:
        return ""

    # Whole blocks that should never be read out at all.
    text = _CODE_FENCE.sub(" (code omitted) ", text)

    # Jargon first. Everything below this rewrites punctuation, and these
    # patterns are made of punctuation.
    for pattern, replacement in JARGON[:-1]:
        text = pattern.sub(replacement, text)
    for pattern, replacement in UNITS:
        text = pattern.sub(replacement, text)

    # Markdown structure.
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _NUMBERED.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _INLINE_CODE.sub(r"\1", text)

    # Things with a spoken form of their own.
    text = _URL.sub(_say_url, text)
    text = _PATH.sub(lambda m: " " + m.group(0).rstrip("/").rsplit("/", 1)[-1] + " ", text)
    text = _VERSION.sub(_say_version, text)

    for extension, said in EXTENSIONS.items():
        text = re.sub(re.escape(extension) + r"\b", said, text, flags=re.IGNORECASE)

    for symbol, said in SYMBOLS.items():
        text = text.replace(symbol, said)

    # Whitespace last, once everything else has finished inserting it.
    text = re.sub(r"\s*\n+\s*", ". ", text)
    text = re.sub(r"[:;]\s*\.", ".", text)
    text = re.sub(r"\.\s*\.+", ".", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def segments(text: str, min_chars: int = 24) -> List[str]:
    """
    Split into speakable chunks at sentence boundaries.

    `min_chars` stops the first chunk being "Right." on its own, which produces
    a clipped syllable and then a gap while the rest synthesises. Short leading
    sentences are glued to the one after them.
    """
    text = spoken(text)
    if not text:
        return []

    pieces = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
    if not pieces:
        return []

    merged: List[str] = []
    for piece in pieces:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return merged


def stream_segments(pieces: Iterator[str], min_chars: int = 24) -> Iterator[str]:
    """
    Regroup a token stream into whole sentences as they complete.

    The point of the whole file: a sentence can be spoken the moment it ends,
    while the model is still producing the next one.
    """
    buffer = ""
    for piece in pieces:
        buffer += piece
        while True:
            cut = _first_cut(buffer, min_chars)
            if cut is None:
                break
            sentence, buffer = buffer[:cut[0]].strip(), buffer[cut[1]:]
            said = spoken(sentence)
            if said:
                yield said
    tail = spoken(buffer.strip())
    if tail:
        yield tail


def _first_cut(buffer: str, min_chars: int):
    """
    Where the first speakable chunk ends: the first sentence end with at least
    `min_chars` before it, so a short opener travels with the sentence after it.

    The first version stopped at the first sentence end and, when that sentence
    was short, waited for more text, then found the same short sentence again
    on every token. "Sure. Supply chain management is..." spoke nothing until
    the whole answer existed, which is the one thing this exists to prevent.
    """
    for match in _SENTENCE_END.finditer(buffer):
        if len(buffer[:match.start()].strip()) >= min_chars:
            return match.start(), match.end()
    return None


# Where a long first sentence may be split so the first clip is short. Kokoro
# renders at about a third of real time on this Mac, and the first clip has to
# be rendered in full before anything is heard: a 35-word opening sentence was
# 3.0 s of silence on its own (Phase C baseline). A clause is a place a speaker
# pauses anyway, so the gap between the two clips lands where a pause belongs.
LEAD_WORDS = int(os.getenv("DIABLO_LEAD_WORDS", "16") or 0)
_CLAUSE = re.compile(r"(?<=[,;:])\s+|\s+(?=—|–)|(?<=—|–)\s*")


def lead_phrase(pieces: Iterator[str], max_words: int = LEAD_WORDS) -> Iterator[str]:
    """
    The same chunks, with the first one split at a clause if it is long.

    Only the first: after it, synthesis runs one chunk ahead of playback and a
    long sentence costs nothing audible. Only at a clause boundary with at
    least four words before it, never mid-phrase. `max_words` of 0 turns it off.
    """
    try:
        yield from _lead(pieces, max_words)
    finally:
        closing = getattr(pieces, "close", None)
        if closing is not None:
            closing()


def _lead(pieces: Iterator[str], max_words: int) -> Iterator[str]:
    first = True
    for piece in pieces:
        if first and max_words:
            first = False
            words = piece.split()
            if len(words) > max_words:
                for match in _CLAUSE.finditer(piece):
                    head = piece[:match.start()].strip()
                    if 4 <= len(head.split()) <= max_words:
                        tail = piece[match.end():].strip(" —–")
                        if len(tail.split()) >= 3:
                            yield head
                            yield tail
                            break
                else:
                    yield piece
                continue
            yield piece
            continue
        first = False
        yield piece


if __name__ == "__main__":
    samples = [
        "**Done.** I wrote `total.txt` to /Users/shaurya/Desktop/reports/total.txt",
        "The model is qwen3:4b-instruct-2507-q4_K_M and it uses ~3.2 GB & runs at 40 tok/s.",
        "See https://ollama.com/library/qwen3 for the full list.",
        "Here's the code:\n```python\nprint('hi')\n```\nThat's all it does.",
        "- first point\n- second point\n\n## A heading\n\n1. numbered\n2. also numbered",
        "Upgrade to v2.5.1 or later.",
    ]
    print()
    for sample in samples:
        print(f"  in   {sample[:70]!r}")
        print(f"  out  {spoken(sample)[:100]!r}")
        print(f"  segs {segments(sample)}")
        print()


# ─── What a person hears when something inside fails ────────────────────────
#
# Real use put lines naming Diablo's parts into speech. A person needs what
# happened, what could not be done and what comes next, never the machinery.
# This is the one translator: voice.speak and voice.speak_stream apply it to
# every line, so no path goes round it. It rewrites only sentences that carry
# a failure shape or an internal name; everything else passes untouched, so
# "the worker in the factory" and "relax" are left alone.

_FAILED = (r"\b(?:fail(?:ed|s|ure)?|crash(?:ed|es)?|errors?|timed? ?out|timeout|exited|"
           r"returned none|unavailable|not responding|died|broke(?:n)?|refused)\b")
# Internal names, each with what its failure means to the person.
_PARTS = [
    (re.compile(r"\bvoice ?box\b", re.I),
     "My voice had trouble there, so I'll carry on in text."),
    (re.compile(r"\bocr\b", re.I),
     "I couldn't read the screen just then, so I'll try another way."),
    (re.compile(r"\bAX\b|\baccessibility (?:api|read|tree)\b"),
     "I couldn't read that app's controls directly, so I'll use another way to find it."),
    (re.compile(r"\b(?:grounding[_ ]gate|goclick|perception|screenframe)\b", re.I),
     "I couldn't confidently locate that exact control, so I don't want to point you "
     "to the wrong place."),
    (re.compile(r"\b(?:ollama|qwen[\w.:-]*|subprocess|worker|tool [a-z_]+|[a-z]+_[a-z_]+)\b",
                re.I),
     "Something went wrong on my side."),
]
# Shapes that are never speech, failure word or not.
_RAW = [
    (re.compile(r"Errno 13|Permission denied", re.I),
     "macOS didn't give me permission for that."),
    (re.compile(r"Traceback|File \"[^\"]+\", line \d+|\[Errno \d+\]|"
                r"\b[A-Z][A-Za-z]*(?:Error|Exception)\b:?|\bobject at 0x"),
     "Something went wrong on my side."),
    (re.compile(r"\b(?:REJECT_[A-Z_]+|LOW_CONFIDENCE|NOT_FOUND|INVALID_OBSERVATION)\b"),
     "I couldn't confidently locate that exact control, so I don't want to point you "
     "to the wrong place."),
    (re.compile(r"\bqwen[\w.:-]*\b.*\bnot installed\b", re.I),
     "My local model isn't installed yet, so I can't think that through right now."),
]
_SENTENCES = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")


def user_safe(text: str) -> str:
    """Say what happened in the person's terms; never name Diablo's parts."""
    if not text:
        return text or ""
    out, seen = [], set()
    for sentence in _SENTENCES.split(text):
        plain = sentence
        for pattern, meaning in _RAW:
            if pattern.search(sentence):
                plain = meaning
                break
        else:
            if re.search(_FAILED, sentence, re.I):
                for pattern, meaning in _PARTS:
                    if pattern.search(sentence):
                        plain = meaning
                        break
        if plain is sentence or plain not in seen:
            out.append(plain)
        seen.add(plain)
    joined = " ".join(out)
    # A raw tool prefix, kept as meaning: "Error: no file at x" -> "No file at x".
    joined = re.sub(r"(^|(?<=[.!?] ))Error:\s*(\w)", lambda m: m.group(1) + m.group(2).upper(),
                    joined)
    # A command for a developer is not something to say to a person.
    joined = re.sub(r"\s*Run: ollama pull \S+\.?", "", joined)
    return joined.strip()
