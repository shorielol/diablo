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
import re
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import ax  # noqa: E402
from skills import config  # noqa: E402
from skills import pointing  # noqa: E402
from skills import screen_context  # noqa: E402
from skills import target as _target  # noqa: E402

# Below this, a candidate is not worth offering at all. Tuned to the same
# floor pointing.py already uses, because it is the same matcher underneath.
FLOOR = config.env_float("DIABLO_LOCATE_FLOOR", 0.45, low=0.1, high=0.95)

# Two candidates closer together than this are not distinguishable by
# confidence, and choosing between them is a guess wearing a number.
AMBIGUOUS_WITHIN = config.env_float("DIABLO_AMBIGUOUS_WITHIN", 0.12, low=0.01, high=0.5)

# ─── Arbitration ─────────────────────────────────────────────────────────────
#
# How much clear water the best candidate needs before Diablo acts on it
# instead of asking.
#
# This was a flat 0.12 for every case, and the measured consequence was that
# `_score` cannot express a strong preference. The scorer has tiers:
#
#     exact normalised match                 1.000
#     every asked-for word present           0.930 - 0.02 per extra word
#     fragment entirely inside the request   0.850
#
# so the distance from an exact match to the best possible partial match is
# 0.070, and from "save" to "Save a Copy" it is 0.090. Both are below 0.12.
# An exact match therefore could not out-resolve a partial one — not because
# the threshold was badly chosen, but because a single absolute number cannot
# separate tiers that are 0.07 apart while also being cautious about two weak
# guesses 0.09 apart. Measured on the Build 1 corpus: four of fusion's five
# regressions against accessibility alone were exactly this, at top confidence
# 0.93 to 1.00.
#
# The rule that replaces it asks for less separation as the evidence gets
# stronger:
#
#     required = SEPARATION * (1 - top_confidence)
#
# The reasoning is about what a near-tie means at each end of the scale. At
# 0.50 the top candidate is a poor match in its own right, so a runner-up
# close behind it says "I do not really know which, or whether either" — that
# deserves a question. At 1.00 the top candidate is exactly what was asked
# for, and a runner-up at 0.91 does not make it less exactly that.
#
# SEPARATION is not a new free parameter. It is set so the new rule agrees
# with the old one at the midpoint of the confidence scale: at a top
# confidence of 0.5 it requires 0.12, which is what every case required
# before. Above 0.5 it is more permissive, below 0.5 stricter. One number,
# derived from the number it replaces.
SEPARATION = config.env_float("DIABLO_SEPARATION", AMBIGUOUS_WITHIN * 2.0,
                              low=0.02, high=1.0)

# Below this a candidate is reported as weakly evidenced even when it wins.
# Reuses the bar the cascade already applies in `_needs_a_second_opinion`:
# under it, locate() does not consider accessibility decisive on its own.
DECISIVE = 0.92

# "absolute" restores the pre-Build-2 rule, for A/B measurement and rollback.
ARBITRATION = os.getenv("DIABLO_ARBITRATION", "relative").strip().lower()


class Verdict:
    """
    What arbitration concluded. Five states that used to be two.

    `ambiguous` alone could not tell a caller whether the screen had even been
    read, whether anything matched, or whether the winner was weakly evidenced.
    """

    RESOLVED = "resolved"                      # one candidate satisfies the rule
    AMBIGUOUS = "ambiguous"                    # several remain indistinguishable
    LOW_CONFIDENCE = "low_confidence"          # one wins, on weak evidence
    NOT_FOUND = "not_found"                    # nothing cleared the floor
    INVALID_OBSERVATION = "invalid_observation"  # the screen could not be read


def _discriminating(top: Candidate, runner: Optional[Candidate]) -> bool:
    """
    Whether the top candidate's corroboration actually tells the two apart.

    Measured case that produced this rule. Asked for "the done button" against
    a window holding "Mark Done" and "Mark Done Later", accessibility scored
    them 0.910 and 0.890 — a 0.020 gap. OCR then read both labels and agreed
    with both, lifting them to 0.959 and 0.949. Corroboration had raised the
    winner's confidence, which under the separation rule lowers the bar it has
    to clear, while simultaneously compressing the gap to 0.010. The two
    effects nearly cancelled and the case resolved by 0.0002 — a genuine tie
    decided by rounding.

    Agreement between instruments is only evidence about *which* candidate is
    right when the instruments do not agree equally about both. When the
    runner-up carries the same sources, the corroboration is common-mode: it
    says both labels are really on screen, which was never in doubt.
    """
    if runner is None:
        return True
    return set(top.sources) > set(runner.sources)


def _separation(top: Candidate, runner: Optional[Candidate],
                fused_margin: float) -> tuple:
    """
    The gap to judge, and the confidence to judge it against.

    When corroboration does not discriminate, it is ignored on BOTH sides of
    the comparison — the gap and the bar. Ignoring it on only one side was the
    first version of this rule and it produced a theme-dependent answer.

    The reason is a property of how `_fuse` combines agreement:

        fused = c + (1 - c) * other * 0.6

    That curve is concave, so it lifts a weaker candidate further than a
    stronger one. Two candidates corroborated by the same instruments
    therefore converge. Measured on the controls fixture asking for "the Save
    button": accessibility alone scores Save 0.930 and "Save a Copy" 0.910, a
    gap of 0.0200. In dark mode OCR can also read the labels, agrees with
    both, and the fused pair becomes 0.969 and 0.959 — the same two controls,
    the same evidence, a gap compressed to 0.0099.

    Under the light theme that case resolved and under the dark theme it asked
    a question. Whether OCR happens to be able to read the glass is not
    information about which button the user meant, so it must not change the
    answer.
    """
    if runner is None:
        return fused_margin, top.confidence
    if _discriminating(top, runner):
        return fused_margin, top.confidence
    return (top.best_single - runner.best_single), top.best_single


def required_margin(top_confidence: float) -> float:
    """
    The separation the best candidate needs over the runner-up.

    Falls linearly to zero as the top candidate approaches certainty. Bounded
    on both sides by construction: `top` is clamped to 0..1 by every source, so
    the result is always between 0 and SEPARATION.
    """
    if ARBITRATION == "absolute":
        return AMBIGUOUS_WITHIN
    top = max(0.0, min(1.0, float(top_confidence)))
    return SEPARATION * (1.0 - top)

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
    # Set when the vision layer identified this control by its appearance.
    # Carries the frame it was looking at, so the evidence can be checked for
    # staleness rather than trusted because it exists.
    vision: Any = None
    # What each instrument said on its own, before fusion combined them.
    #
    # `confidence` is the fused figure and _fuse overwrites it when sources
    # agree, which used to destroy the only record of what any single source
    # actually claimed. Keeping both means "accessibility was 0.62 and OCR was
    # 0.58, together 0.84" is answerable; before, only the 0.84 survived.
    source_confidence: Dict[str, float] = field(default_factory=dict)

    @property
    def centre(self) -> Tuple[float, float]:
        return (self.x + self.width / 2.0, self.y + self.height / 2.0)

    @property
    def corroborated(self) -> bool:
        """Seen by more than one instrument."""
        return len(set(self.sources)) > 1

    @property
    def best_single(self) -> float:
        """
        The strongest claim any one instrument made, before fusion combined
        them. Falls back to the fused figure for a candidate built before
        source confidences were recorded.
        """
        if self.source_confidence:
            return max(self.source_confidence.values())
        return self.confidence

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
            source_confidence={"accessibility": score},
            enabled=element.enabled, app=element.app))
    return out


def _from_ocr(intent: str, window: Dict[str, Any],
              observation: Optional[Dict[str, Any]] = None) -> List[Candidate]:
    """
    Candidates read off the glass.

    `observation` is filled in with whether the screen could be read at all.
    An empty candidate list answers "did anything match"; it cannot answer
    "was the frame readable", and a black frame used to produce the same
    empty list as a perfectly readable window with no matching text.
    """
    out = []
    try:
        hits = pointing.locate(intent, window.get("app", ""), limit=8,
                               observation=observation)
    except Exception as e:
        if observation is not None:
            observation["ok"] = False
            observation["problem"] = f"{type(e).__name__}: {e}"
        return []
    for hit in hits:
        # pointing returns a point; OCR fragments have a box, and the box is
        # what lets an OCR hit be matched against an accessibility element.
        out.append(Candidate(
            label=hit.get("text", ""), role="text",
            x=float(hit["x"]) - 30.0, y=float(hit["y"]) - 10.0,
            width=60.0, height=20.0,
            confidence=float(hit.get("score", 0.0)),
            sources=["ocr"], source_confidence={"ocr": float(hit.get("score", 0.0))},
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


def _same_frame(a: Candidate, b: Candidate) -> bool:
    """
    Whether two candidates were seen on the same screen.

    Only the vision layer records which frame it was looking at, so this can
    only ever catch vision evidence that has drifted — which is exactly the
    case worth catching, because a vision candidate is the one produced from a
    cached crop that may predate the current screen. When neither side carries
    a fingerprint there is nothing to compare and the merge proceeds as before.
    """
    fa = getattr(getattr(a, "vision", None), "frame_fingerprint", None)
    fb = getattr(getattr(b, "vision", None), "frame_fingerprint", None)
    if not fa or not fb:
        return True
    if len(fa) != len(fb):
        return False
    # The same tolerance ScreenFrame comparison uses for "the screen has not
    # materially changed", rather than a second notion of sameness.
    drift = sum(abs(int(x) - int(y)) for x, y in zip(fa, fb)) / float(len(fa))
    return drift <= 6.0


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
            if not _same_frame(existing, candidate):
                # Two instruments looking at different screens are not two
                # instruments agreeing. Merging them would manufacture
                # corroboration out of a stale crop, so they stay separate
                # candidates and arbitration sees the disagreement.
                continue
            existing.sources.extend(candidate.sources)
            existing.evidence.extend(candidate.evidence)
            for name, value in (candidate.source_confidence or {}).items():
                existing.source_confidence.setdefault(name, value)
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


# ─── An action's answer ──────────────────────────────────────────────────────
#
# B.3. When the user asked for something to be DONE, the answer is a control
# that does it — not the best text match for the words. "Show me how to open
# a new note" on a Notes window scores the note's title against "note", and a
# title is not a thing anyone can press to make a note.
#
# A control qualifies when its role is one a person operates and its own
# words offer the operation. Text read off the glass qualifies only as a short
# label that says the operation ("New Note"), never as a title or a sentence
# that mentions it. Vision qualifies because it only ever picks among
# controls accessibility published, and it was asked about this request.

# A control's label is short; a sentence that mentions an action is not one.
LABEL_WORDS = 3


def _offers(candidate: Candidate, operation: str) -> bool:
    """Whether this candidate is a control that performs the operation."""
    sources = set(candidate.sources)
    if sources & {"accessibility", "vision"}:
        if candidate.role and candidate.role not in ax.ACTIONABLE_ROLES:
            return False
        if "vision" in sources:
            return True
        return _target.affords(operation, candidate.label)
    words = re.findall(r"[a-z]+|\+", (candidate.label or "").lower())
    return 0 < len(words) <= LABEL_WORDS and _target.affords(operation, candidate.label)


def _for_operation(candidates: List[Candidate], operation: str,
                   observation: Optional[Dict[str, Any]] = None) -> List[Candidate]:
    """
    Only what can carry out the operation, best first. Unchanged with none.

    A disabled control that would otherwise qualify is dropped and said to be
    there, so "it is greyed out" can be answered instead of "I found nothing".
    """
    if not operation:
        return candidates
    kept, disabled = [], []
    for candidate in candidates:
        if not _offers(candidate, operation):
            continue
        if candidate.enabled is False:
            disabled.append(candidate.label or candidate.describe())
            continue
        kept.append(candidate)
    if disabled and observation is not None:
        observation.setdefault("disabled", []).extend(disabled)
        observation.setdefault("refused", []).extend(
            f"{label} is disabled" for label in disabled)
    return kept


def locate(intent: str, window: Optional[Dict[str, Any]] = None,
           limit: int = 5,
           observation: Optional[Dict[str, Any]] = None,
           operation: str = "") -> List[Candidate]:
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
    # An action's answer is filtered after every source, so a title that
    # matched the words can never be what stops the next source being asked.
    found = _from_accessibility(intent, window)
    fused = _for_operation(_fuse(found), operation, observation)
    if _needs_a_second_opinion(fused):
        seen: Dict[str, Any] = {}
        found = found + _from_ocr(intent, window, observation=seen)
        if observation is not None and seen:
            observation["ocr"] = seen
        fused = _for_operation(_fuse(found), operation, observation)
    if _needs_vision(intent, fused):
        found = found + _from_vision(intent, window)
        fused = _for_operation(_fuse(found), operation, observation)
    return fused[:limit]


def _wants_a_picture(intent: str) -> bool:
    """Whether the user described the thing by how it looks rather than by name."""
    lowered = (intent or "").lower()
    if any(phrase in lowered for phrase in _LOOKS_LIKE):
        return True
    words = set(re.findall(r"[a-z]+", lowered))
    return bool(words & SHAPE_WORDS)


def _needs_vision(intent: str, candidates: List[Candidate]) -> bool:
    """
    Whether it is worth six seconds to look.

    Vision costs six to thirteen seconds against eighty milliseconds for
    accessibility, measured, and that cost does not fall much with the size of
    the crop — it is the model waking up, not the pixels. So it is asked only
    when the cheap sources have genuinely failed to settle the question.

    "Click Save", answered by a published label at 0.97, must never pay it.
    """
    if candidates and candidates[0].confidence >= 0.92:
        # Already answered by a control that says what it is. The only thing
        # that overrides this is the user describing an appearance, because
        # "the blue one" matching a label called "blue" is luck.
        return _wants_a_picture(intent) and not candidates[0].label
    if not candidates:
        return True
    if candidates[0].confidence < 0.92:
        return True
    return False


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
    # Per-source note on whether the screen could be read at all, keyed by
    # source name. Separate from `candidates` on purpose: "I looked and found
    # nothing" and "I could not look" are different answers and a caller that
    # cannot tell them apart will report the wrong one.
    observation: Dict[str, Any] = field(default_factory=dict)

    @property
    def unreadable(self) -> List[str]:
        """Sources that could not read the screen, rather than found nothing."""
        return sorted(name for name, note in self.observation.items()
                      if isinstance(note, dict) and note.get("ok") is False)

    @property
    def required(self) -> float:
        """The separation this reading's top candidate had to clear."""
        if not self.best:
            return 0.0
        runner = self.candidates[1] if len(self.candidates) > 1 else None
        return required_margin(_separation(self.best, runner, self.margin)[1])

    @property
    def separation(self) -> float:
        """The gap arbitration actually judged, which is not always `margin`."""
        if not self.best:
            return 0.0
        runner = self.candidates[1] if len(self.candidates) > 1 else None
        return _separation(self.best, runner, self.margin)[0]

    @property
    def verdict(self) -> str:
        """
        One of five states, which `ambiguous` alone could not express.

        Order matters. An unreadable screen is reported as such even when a
        source that could see returned something, because a caller deciding
        whether to act should know one of its instruments was blind.
        """
        if self.unreadable and not self.candidates:
            return Verdict.INVALID_OBSERVATION
        if not self.candidates:
            return Verdict.NOT_FOUND
        if self.ambiguous:
            return Verdict.AMBIGUOUS
        if self.best is not None and self.best.confidence < DECISIVE:
            return Verdict.LOW_CONFIDENCE
        return Verdict.RESOLVED

    def explain(self) -> str:
        """
        Why arbitration concluded what it did. For `diablo trace` and debug
        output, never for the spoken reply.
        """
        state = self.verdict
        if state == Verdict.INVALID_OBSERVATION:
            why = "; ".join(
                f"{name}: {(self.observation.get(name) or {}).get('problem', '')}"
                for name in self.unreadable)
            return f"INVALID_OBSERVATION\n  the screen could not be read — {why}"
        if state == Verdict.NOT_FOUND:
            return ("NOT_FOUND\n  no candidate cleared the acceptance floor "
                    f"of {FLOOR}")
        best = self.best
        runner = self.candidates[1] if len(self.candidates) > 1 else None
        lines = [
            f"{state.upper()}: {best.describe()}",
            f"  confidence   {best.confidence:.3f}"
            f"  from {'+'.join(sorted(set(best.sources)))}",
            f"  runner-up    " + (f"{runner.confidence:.3f}  {runner.describe()}"
                                  if runner else "none"),
            f"  separation   {self.separation:.3f} against {self.required:.3f} required"
            + ("  (judged on single-source evidence: agreement did not "
               "separate them)" if abs(self.separation - self.margin) > 1e-9 else ""),
            f"  rule         {ARBITRATION}: "
            + (f"{SEPARATION} x (1 - {best.confidence:.3f})"
               if ARBITRATION != "absolute" else f"flat {AMBIGUOUS_WITHIN}"),
        ]
        if best.corroborated:
            lines.append("  agreement    "
                         + ", ".join(sorted(set(best.sources))) + " concur")
        for note in best.evidence:
            lines.append(f"  evidence     {note}")
        if state == Verdict.AMBIGUOUS:
            lines.append("  the evidence does not separate these two")
        if state == Verdict.LOW_CONFIDENCE:
            lines.append(f"  wins, but below the decisive bar of {DECISIVE}")
        if self.unreadable:
            lines.append("  note         " + ", ".join(self.unreadable)
                         + " could not read the screen")
        return "\n".join(lines)

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


def read(intent: str, window: Optional[Dict[str, Any]] = None,
         operation: str = "") -> Reading:
    """locate(), plus whether the answer is decisive enough to act on."""
    seen: Dict[str, Any] = {}
    candidates = locate(intent, window, observation=seen, operation=operation)
    if not candidates:
        return Reading(observation=seen)
    margin = (candidates[0].confidence - candidates[1].confidence
              if len(candidates) > 1 else 1.0)
    # Strictly greater, so two candidates that score identically are always
    # ambiguous — including two at 1.000, where the required margin is zero.
    # Two controls genuinely labelled the same thing must never resolve by
    # arriving in a particular order.
    top = candidates[0]
    runner = candidates[1] if len(candidates) > 1 else None
    separation, basis = _separation(top, runner, margin)
    decisive = separation > required_margin(basis)
    return Reading(candidates=candidates,
                   ambiguous=len(candidates) > 1 and not decisive,
                   margin=margin, observation=seen)


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


# ─── The third source: what the control looks like ───────────────────────────
#
# Accessibility says "there is a button here, and it has no name". OCR says
# nothing at all, because there is no text to read. Neither can answer "click
# the gear", and that is the whole of this section.
#
# Vision identifies; it does not locate. The coordinate keeps coming from the
# accessibility element, which is exact, published by the application, and
# cannot drift. The model is asked only which of several known rectangles is
# the one the user meant — a multiple-choice question rather than an open one,
# because an open one invites an invented coordinate.

# What an icon can be called. Deliberately short and deliberately extensible:
# a universal icon ontology is a research project, and these are the controls
# people actually ask for by shape.
ICON_WORDS = [
    "gear", "settings", "search", "home", "back", "forward", "close", "menu",
    "refresh", "reload", "download", "upload", "folder", "plus", "add",
    "minus", "remove", "play", "pause", "stop", "warning", "info", "profile",
    "account", "share", "edit", "delete", "trash", "star", "heart", "bell",
    "lock", "eye", "filter", "sort", "grid", "list", "calendar", "clock",
]

# Words that name a SHAPE rather than an action.
#
# Separate from ICON_WORDS, which is the vocabulary offered to the model, and
# separate for a reason a test caught: "click Save and then close" was being
# treated as a visual request because "close" is in the icon vocabulary. Half
# those words are ordinary verbs. A gear is a shape; closing is something you
# do, and the control that does it usually has a label.
SHAPE_WORDS = {
    "gear", "cog", "magnifier", "magnifying", "house", "star", "heart",
    "bell", "circle", "square", "triangle", "arrow", "chevron", "hamburger",
    "ellipsis", "avatar", "thumbnail", "spinner", "cross", "tick", "checkmark",
    "pencil", "bin", "cloud", "flag", "pin", "clip", "paperclip", "eye",
}

# Phrases that say the user is describing something by how it looks. These
# reach for vision even when a text match exists, because "the blue one" is
# not a label and matching it against one would be luck.
_LOOKS_LIKE = (
    "icon", "symbol", "glyph", "button that looks", "looks like", "shaped",
    "the blue", "the red", "the green", "the little", "the small",
)

# How big an unlabelled control has to be to be worth showing the model, and
# how small before it is scenery. A 4-point control is a separator.
MIN_ICON = config.env_float("DIABLO_MIN_ICON", 12.0, low=4.0, high=64.0)
MAX_ICON = config.env_float("DIABLO_MAX_ICON", 120.0, low=24.0, high=600.0)

# More than this and the sheet is unreadable and the question is unfair.
MAX_ON_SHEET = config.env_int("DIABLO_VISION_CANDIDATES", 8, low=2, high=16)

# Small crops have to be enlarged, not reduced. Measured: a 42x56 native crop
# of a real toolbar icon came back as "sunset icon", and the same control at
# four times the size was read correctly. This is the opposite of the
# full-screen rule, where 1280 is a ceiling — here it is a floor.
SHEET_EDGE = config.env_int("DIABLO_VISION_SHEET_EDGE", 720, low=200, high=1600)

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "icon": {"type": "string"},
                    "matches_request": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["number", "icon", "matches_request", "confidence"],
            },
        },
    },
    "required": ["matches"],
}


@dataclass
class VisionEvidence:
    """What the model said about one control, and which screen it was looking at."""

    label: str = ""
    confidence: float = 0.0
    reason: str = ""
    attributes: List[str] = field(default_factory=list)
    # The frame this was true of. Evidence outlives the pixels it came from,
    # and without this it would outlive its own correctness too.
    frame_fingerprint: List[int] = field(default_factory=list)
    crop: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    at: float = 0.0

    def stale_against(self, frame, drift: float = 6.0) -> bool:
        """
        Whether the screen has moved on since the model looked.

        Compared against the frame's own fingerprint rather than a clock: a
        screen that has not changed in ten seconds is still the screen that
        was analysed, and one that changed in half a second is not.
        """
        mine, theirs = self.frame_fingerprint, getattr(frame, "fingerprint", None)
        if not mine or not theirs or len(mine) != len(theirs):
            return True
        return sum(abs(a - b) for a, b in zip(mine, theirs)) > drift * len(mine)


def _sheet(elements, window) -> Optional[Tuple[str, Tuple[float, float, float, float]]]:
    """
    One image of every unlabelled candidate, each in a numbered box.

    The numbering is the whole trick. Asked "what is in this toolbar" the
    model writes a sentence nobody can map back onto a control; asked "which
    numbered box is the gear" it answers with a number that is already tied to
    an exact rectangle the application published. That is the difference
    between identification and invention.
    """
    from skills import display as _display
    from skills import screen_context as _sc

    pad = 14.0
    left = min(e.x for e in elements) - pad
    top = min(e.y for e in elements) - pad
    right = max(e.x + e.width for e in elements) + pad
    bottom = max(e.y + e.height for e in elements) + pad
    crop = (left, top, right - left, bottom - top)

    path = _display.capture_region(*crop)
    if not path:
        return None

    # Enlarge until the icons are legible. Nearest-neighbour would keep the
    # edges hard but a scaled-up icon reads better smoothed, and these are
    # shapes rather than glyphs.
    longest = max(crop[2], crop[3])
    if longest > 0 and longest < SHEET_EDGE:
        bigger = _sc._enlarge(path, SHEET_EDGE / longest, smooth=True)
        if bigger:
            try:
                os.unlink(path)
            except OSError:
                pass
            path = bigger

    try:
        from AppKit import (NSBezierPath, NSBitmapImageRep, NSColor, NSFont,
                            NSFontAttributeName, NSForegroundColorAttributeName,
                            NSGraphicsContext, NSPNGFileType, NSString)
        from Foundation import NSData, NSMakePoint, NSMakeRect
        rep = NSBitmapImageRep.imageRepWithData_(NSData.dataWithContentsOfFile_(path))
        if rep is None:
            return path, crop
        width, height = int(rep.pixelsWide()), int(rep.pixelsHigh())
        scale_x = width / crop[2] if crop[2] else 1.0
        scale_y = height / crop[3] if crop[3] else 1.0

        context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(context)
        for number, element in enumerate(elements, 1):
            bx = (element.x - crop[0]) * scale_x
            by = height - (element.y - crop[1] + element.height) * scale_y
            bw, bh = element.width * scale_x, element.height * scale_y
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.9, 0.1, 0.1, 0.95).set()
            box = NSBezierPath.bezierPathWithRect_(NSMakeRect(bx, by, bw, bh))
            box.setLineWidth_(max(2.0, 2.0 * scale_x / 3.0))
            box.stroke()
            tag = NSString.stringWithString_(str(number))
            size = max(13.0, 11.0 * scale_y / 3.0)
            attrs = {NSFontAttributeName: NSFont.boldSystemFontOfSize_(size),
                     NSForegroundColorAttributeName: NSColor.whiteColor()}
            measured = tag.sizeWithAttributes_(attrs)
            badge = NSMakeRect(bx, by + bh - measured.height,
                               measured.width + 6, measured.height)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.9, 0.1, 0.1, 0.95).set()
            NSBezierPath.bezierPathWithRect_(badge).fill()
            tag.drawAtPoint_withAttributes_(
                NSMakePoint(badge.origin.x + 3, badge.origin.y), attrs)
        NSGraphicsContext.restoreGraphicsState()
        rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(
            path, True)
    except Exception:
        pass
    return path, crop


_vision_cache: Dict[Any, List[Dict[str, Any]]] = {}


def _cache_key(intent: str, crop, fingerprint) -> tuple:
    """
    Frame identity, crop and question. All three, or none.

    "The last gear result" is not a cache key — it is a way of clicking where
    the gear used to be. The fingerprint is what ties the answer to the screen
    it was true of.
    """
    return (intent.strip().lower(), tuple(round(v) for v in crop),
            tuple(fingerprint or ()))


def _ask_vision(intent: str, elements, window, path: str) -> List[Dict[str, Any]]:
    """One narrow question, answered in a shape Ollama enforces."""
    from skills import screen_context as _sc

    listing = "\n".join(
        f"  {n}. a {int(e.width)} by {int(e.height)} control"
        for n, e in enumerate(elements, 1))
    app = window.get("app", "this application")
    question = (
        f"This is a crop of the {app} interface. {len(elements)} controls are "
        f"outlined in red and numbered:\n{listing}\n\n"
        f"The user asked to click: “{intent}”.\n\n"
        f"For each numbered control, say which common interface icon it shows "
        f"(for example: {', '.join(ICON_WORDS[:10])}), whether it is the one "
        f"the user asked for, and how sure you are from 0 to 1. Judge only what "
        f"you can actually see inside each numbered box. If a control is not "
        f"the one asked for, say so rather than guessing.")

    raw = _sc.read_image(path, question, num_predict=380, schema=VISION_SCHEMA)
    if not raw or raw.startswith("Error:"):
        return []
    try:
        import json
        parsed = json.loads(raw)
    except Exception:
        return []
    matches = parsed.get("matches")
    return matches if isinstance(matches, list) else []


def _from_vision(intent: str, window: Dict[str, Any],
                 frame=None) -> List[Candidate]:
    """
    Candidates for controls that have no text, identified by what they look like.

    Returns nothing rather than raising for every failure mode there is —
    accessibility unavailable, no unlabelled controls, no vision model, a
    capture that failed, a malformed reply. Vision improves perception; it is
    never allowed to be the reason an action cannot happen.
    """
    from skills import screen as _screen
    from skills import screen_context as _sc

    if not _sc.vision_available():
        return []
    elements = [e for e in ax.elements_for_window(window)
                if not e.named
                and MIN_ICON <= e.width <= MAX_ICON
                and MIN_ICON <= e.height <= MAX_ICON]
    if not elements:
        return []
    elements = sorted(elements, key=lambda e: (round(e.y), e.x))[:MAX_ON_SHEET]

    own_frame = None
    if frame is None:
        own_frame = frame = _screen.capture_this_window(window)
        if not frame.ok:
            frame.release()
            return []
    fingerprint = list(getattr(frame, "fingerprint", []) or [])

    built = _sheet(elements, window)
    if own_frame is not None:
        own_frame.release()
    if not built:
        return []
    path, crop = built

    key = _cache_key(intent, crop, fingerprint)
    try:
        matches = _vision_cache.get(key)
        if matches is None:
            matches = _ask_vision(intent, elements, window, path)
            if len(_vision_cache) > 24:
                _vision_cache.clear()
            _vision_cache[key] = matches
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    out: List[Candidate] = []
    for match in matches:
        try:
            number = int(match.get("number", 0))
            element = elements[number - 1]
        except Exception:
            continue
        if not match.get("matches_request"):
            # Negative evidence. Kept out of the candidate set, but it is what
            # makes the positives mean something: three controls examined and
            # one picked is a comparison, not a lucky match.
            continue
        confidence = max(0.0, min(1.0, float(match.get("confidence", 0.0))))
        if confidence < FLOOR:
            continue
        icon = str(match.get("icon", ""))[:40]
        evidence = VisionEvidence(
            label=icon, confidence=confidence,
            reason=str(match.get("reason", ""))[:160],
            attributes=[w for w in ICON_WORDS if w in icon.lower()],
            frame_fingerprint=fingerprint, crop=crop, at=time.time())
        out.append(Candidate(
            label=icon, role=element.role,
            x=element.x, y=element.y, width=element.width, height=element.height,
            # Vision scores what a thing IS, which is weaker evidence than a
            # published label. It enters below the named-control band so that
            # a real label always wins, and rises only through corroboration.
            confidence=min(confidence, 0.88),
            sources=["vision"],
            source_confidence={"vision": min(confidence, 0.88)},
            evidence=[f"it looks like {icon}" + (f" — {evidence.reason}"
                                                 if evidence.reason else "")],
            enabled=element.enabled, app=element.app, vision=evidence))
    return out


def debug(intent: str, window: Optional[Dict[str, Any]] = None) -> str:
    """
    Every candidate, every source, every score, and which one won.

    For a developer chasing a wrong target, not for a user. A user is told
    what Diablo is about to click and why in one sentence; this is the working
    underneath that sentence.
    """
    from skills import screen_context as _sc
    if window is None:
        window = _sc.frontmost_window()
    if not window:
        return "no window"

    started = time.time()
    reading = read(intent, window)
    took = (time.time() - started) * 1000

    lines = [f"TARGET DEBUG — {intent!r} in {window.get('app','?')}",
             f"  {len(reading.candidates)} candidate(s) in {took:.0f}ms"]
    for number, candidate in enumerate(reading.candidates, 1):
        lines.append(
            f"\n  [{number}] {candidate.describe()}"
            f"\n      bounds     {candidate.x:.0f},{candidate.y:.0f},"
            f"{candidate.width:.0f},{candidate.height:.0f}"
            f"\n      sources    {'+'.join(sorted(set(candidate.sources)))}"
            f"\n      confidence {candidate.confidence:.3f}"
            f"\n      enabled    {candidate.enabled}")
        if candidate.vision is not None:
            lines.append(
                f"      vision     {candidate.vision.label!r} "
                f"at {candidate.vision.confidence:.2f}"
                f"\n      reason     {candidate.vision.reason[:70]}")
        for item in candidate.evidence:
            lines.append(f"      evidence   {item[:78]}")
    lines.append(f"\n  margin     {reading.margin:.3f}"
                 f"\n  ambiguous  {reading.ambiguous}"
                 f"\n  FINAL      {reading.best.describe() if reading.best else 'none'}")
    return "\n".join(lines)
