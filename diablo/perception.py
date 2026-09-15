"""
D.I.A.B.L.O. — finding the thing the user meant.

One locate() for everything
---------------------------
Guidance pointed at a control found by OCR. Computer use would have clicked a
coordinate found the same way. Two paths to the same question is how an
assistant ends up marking one button and pressing another, so there is one
path, and both the marker and the mouse take the target it returns.

It returns CANDIDATES, not a coordinate. A coordinate is an answer with the
uncertainty thrown away, and the uncertainty is the part that decides whether
Diablo should act, ask, or decline.

Three sources, none of them authoritative
-----------------------------------------
    accessibility   the application's own account of its controls: role,
                    label, enabled, exact bounds. Sees the unlabelled gear
                    icon that OCR cannot. Lies by omission — Electron apps
                    often publish one enormous group — and occasionally by
                    staleness.

    ocr             what is actually painted on the glass right now. Cannot
                    see an unlabelled control at all, but it cannot be stale
                    and it cannot describe something that is not there.

    vision          not wired in here yet. The shape is left for it.

They disagree in both directions, which is the point. A control that
accessibility describes as "Settings" and OCR reads as "Settings" in the same
place is two independent witnesses, and that is worth more than either alone.
"""

import os
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import ax  # noqa: E402
from skills import config  # noqa: E402
from skills import pointing  # noqa: E402
from skills import screen_context  # noqa: E402

# Below this, a candidate is not worth offering at all. Tuned to the same
# floor pointing.py already uses, because it is the same matcher underneath.
FLOOR = config.env_float("DIABLO_LOCATE_FLOOR", 0.45, low=0.1, high=0.95)

# Two candidates closer together than this are not distinguishable by
# confidence, and choosing between them is a guess wearing a number.
AMBIGUOUS_WITHIN = config.env_float("DIABLO_AMBIGUOUS_WITHIN", 0.12, low=0.01, high=0.5)

# How near two candidates must be, in points, to be treated as the same
# control seen by two different instruments.
SAME_CONTROL = config.env_float("DIABLO_SAME_CONTROL", 26.0, low=4.0, high=200.0)


@dataclass
class Candidate:
    """One possible answer, with the evidence that produced it."""

    label: str = ""
    role: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    confidence: float = 0.0
    sources: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    enabled: Optional[bool] = None
    app: str = ""

    @property
    def centre(self) -> Tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    @property
    def corroborated(self) -> bool:
        """Seen by more than one instrument."""
        return len(set(self.sources)) > 1

    def describe(self) -> str:
        what = (self.role or "control").replace("AX", "").lower()
        name = f"“{self.label}”" if self.label else "an unlabelled control"
        return f"{what} {name}"

    def why(self) -> str:
        """One sentence a person can check, made of what was observed."""
        return "; ".join(self.evidence) if self.evidence else "no evidence recorded"


def _from_accessibility(intent: str, window: Dict[str, Any]) -> List[Candidate]:
    """Candidates the application published about itself."""
    out = []
    for element in ax.elements_for_window(window):
        # The same matcher pointing.py uses, so a phrase scores the same
        # however it was found. Two scorers would drift.
        score = 0.0
        matched = ""
        for text in (element.title, element.description, element.help, element.value):
            if not text:
                continue
            here = pointing._score(intent, text)
            if here > score:
                score, matched = here, text

        # An unlabelled control cannot be matched by name, but it is still a
        # real control that exists where nothing else can see one. It enters
        # with a low confidence so it can be corroborated by position or by
        # the vision layer, and never wins on its own.
        if not element.named and not matched:
            role_hint = pointing._score(intent, element.role.replace("AX", ""))
            if role_hint < FLOOR:
                continue
            score, matched = min(role_hint, 0.55), element.role

        if score < FLOOR:
            continue
        evidence = [f"the application calls it {element.describe()}"]
        if element.enabled is False:
            evidence.append("it reports itself as disabled")
        out.append(Candidate(
            label=element.label, role=element.role,
            x=element.x, y=element.y, width=element.width, height=element.height,
            confidence=score, sources=["accessibility"], evidence=evidence,
            enabled=element.enabled, app=element.app))
    return out


def _from_ocr(intent: str, window: Dict[str, Any]) -> List[Candidate]:
    """Candidates read off the glass."""
    out = []
    try:
        hits = pointing.locate(intent, window.get("app", ""), limit=8)
    except Exception:
        return []
    for hit in hits:
        # pointing returns a point; OCR fragments have a box, and the box is
        # what lets an OCR hit be matched against an accessibility element.
        out.append(Candidate(
            label=hit.get("text", ""), role="text",
            x=float(hit["x"]) - 30.0, y=float(hit["y"]) - 10.0,
            width=60.0, height=20.0,
            confidence=float(hit.get("score", 0.0)),
            sources=["ocr"],
            evidence=[f"the words “{hit.get('text','')[:40]}” are on screen there"],
            app=window.get("app", "")))
    return out


def _same_control(a: Candidate, b: Candidate) -> bool:
    """
    Whether two candidates are one control seen by two instruments.

    Containment first, distance second. Measured on a real page: the
    accessibility element for a link spanned its whole row and reported its
    centre at x=306, while the OCR hit landed on the first word at x=249 —
    57 points apart, which no sane distance threshold would merge, and yet
    plainly the same link. One box containing the other's centre settles it
    where a radius cannot.
    """
    for outer, inner in ((a, b), (b, a)):
        cx, cy = inner.centre
        if (outer.width > 0 and outer.height > 0
                and outer.x <= cx <= outer.x + outer.width
                and outer.y <= cy <= outer.y + outer.height):
            return True
    ax_, ay = a.centre
    bx, by = b.centre
    return ((ax_ - bx) ** 2 + (ay - by) ** 2) ** 0.5 <= SAME_CONTROL


def _fuse(candidates: List[Candidate]) -> List[Candidate]:
    """
    Merge candidates that are plainly the same control seen twice.

    This is where two weak signals become one strong one. An accessibility
    element scoring 0.62 and an OCR hit scoring 0.58 in the same place are not
    two mediocre guesses; they are two instruments agreeing, and that is worth
    more than either. A single source never reaches the corroborated band.
    """
    # Copies, not the candidates that were handed in.
    #
    # The first version merged in place, so a caller holding a reference to a
    # candidate watched its confidence change underneath it — which a test
    # caught by comparing a pre-fusion score against its own mutated self.
    # Perception describes what it saw; it does not edit the caller's copy of
    # what it saw.
    merged: List[Candidate] = []
    for original in sorted(candidates, key=lambda c: -c.confidence):
        candidate = replace(original,
                            sources=list(original.sources),
                            evidence=list(original.evidence))
        for existing in merged:
            if not _same_control(existing, candidate):
                continue
            if set(candidate.sources) <= set(existing.sources):
                break
            existing.sources.extend(candidate.sources)
            existing.evidence.extend(candidate.evidence)
            # Agreement raises confidence toward certainty without ever
            # reaching it, and never lowers it.
            existing.confidence = min(
                0.99, existing.confidence + (1.0 - existing.confidence) * candidate.confidence * 0.6)
            if not existing.label and candidate.label:
                existing.label = candidate.label
            if existing.enabled is None:
                existing.enabled = candidate.enabled
            break
        else:
            merged.append(candidate)
    return sorted(merged, key=lambda c: -c.confidence)


def locate(intent: str, window: Optional[Dict[str, Any]] = None,
           limit: int = 5) -> List[Candidate]:
    """
    Everything that might be what the user meant, best first.

    An empty list is a real answer and the right one when nothing matches.
    Returning a low-scoring coordinate so that something comes back is how an
    assistant ends up clicking the wrong thing confidently.
    """
    if not intent or not intent.strip():
        return []
    if window is None:
        window = screen_context.frontmost_window()
    if not window or window.get("sensitive"):
        return []

    # Accessibility first, because it is twenty times cheaper: 80ms against
    # 1.1 seconds for a capture and an OCR pass. OCR is asked for only when
    # the cheap answer is not already decisive — when nothing was found, when
    # the best is not confident, or when the top two are too close to
    # separate and a second opinion is exactly what is needed.
    found = _from_accessibility(intent, window)
    fused = _fuse(found)
    if _needs_a_second_opinion(fused):
        fused = _fuse(found + _from_ocr(intent, window))
    return fused[:limit]


def _needs_a_second_opinion(candidates: List[Candidate]) -> bool:
    if not candidates:
        return True
    if candidates[0].confidence < 0.92:
        return True
    if len(candidates) > 1 and (candidates[0].confidence
                                - candidates[1].confidence) < AMBIGUOUS_WITHIN:
        return True
    return False


@dataclass
class Reading:
    """What locate() found, and whether it is decisive."""

    candidates: List[Candidate] = field(default_factory=list)
    ambiguous: bool = False
    margin: float = 0.0

    @property
    def best(self) -> Optional[Candidate]:
        return self.candidates[0] if self.candidates else None

    @property
    def found(self) -> bool:
        return bool(self.candidates)

    def question(self) -> str:
        """What to ask when two controls are equally plausible."""
        if not self.ambiguous or len(self.candidates) < 2:
            return ""
        first, second = self.candidates[0], self.candidates[1]
        return (f"I can see two things that could be it — "
                f"{first.describe()} and {second.describe()}. Which do you mean?")


def read(intent: str, window: Optional[Dict[str, Any]] = None) -> Reading:
    """locate(), plus whether the answer is decisive enough to act on."""
    candidates = locate(intent, window)
    if not candidates:
        return Reading()
    margin = (candidates[0].confidence - candidates[1].confidence
              if len(candidates) > 1 else 1.0)
    return Reading(candidates=candidates,
                   ambiguous=len(candidates) > 1 and margin < AMBIGUOUS_WITHIN,
                   margin=margin)


if __name__ == "__main__":
    import time
    from skills import screen_context as sc
    window = sc.frontmost_window()
    print(f"window: {window['app']} — {window['title'][:44]}")
    for intent in sys.argv[1:] or ["back", "reload", "settings", "search"]:
        started = time.time()
        reading = read(intent, window)
        ms = (time.time() - started) * 1000
        print(f"\n  {intent!r}  ({ms:.0f}ms)  "
              f"{'AMBIGUOUS' if reading.ambiguous else ''}")
        for candidate in reading.candidates[:3]:
            marks = "+".join(sorted(set(candidate.sources)))
            print(f"    {candidate.confidence:.2f} {marks:22} {candidate.describe()[:46]:48}"
                  f" at ({candidate.centre[0]:.0f},{candidate.centre[1]:.0f})")
