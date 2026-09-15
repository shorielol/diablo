"""
Finding a control, deciding whether to touch it, and admitting when it cannot.

The rule underneath every check: uncertainty makes Diablo hesitate, never
improvise. A locate() that always returns something is worse than one that
returns nothing, because the caller cannot tell the difference between a good
answer and a shrug with coordinates attached.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import ax, computer_use, perception, screen_context  # noqa: E402


def candidate(label="", conf=0.9, x=0.0, y=0.0, w=40.0, h=20.0,
              sources=None, enabled=True, role="AXButton"):
    return perception.Candidate(
        label=label, role=role, x=x, y=y, width=w, height=h,
        confidence=conf, sources=list(sources or ["accessibility"]),
        evidence=[f"seen as {label}"], enabled=enabled)


class FakeReading:
    def __init__(self, best=None, ambiguous=False, found=None):
        self.best = best
        self.ambiguous = ambiguous
        self.found = best is not None if found is None else found
        self.candidates = [best] if best else []

    def question(self):
        return "Which one?"


def run():
    r = Results()

    # ── Accessibility ───────────────────────────────────────────────────────
    r.head("What the application says about itself")
    with guard(r, "accessibility is reachable"):
        r.check("the accessibility API answers",
                isinstance(ax.available(), bool),
                f"trusted={ax.available()}")
        if not ax.available():
            r.skip("controls can be enumerated", "accessibility not granted")
        else:
            window = screen_context.frontmost_window()
            if not window:
                r.skip("controls can be enumerated", "no window visible")
            else:
                started = time.time()
                elements = ax.elements_for_window(window)
                took = (time.time() - started) * 1000
                r.check("a real window publishes controls",
                        isinstance(elements, list),
                        f"{len(elements)} from {window['app']} in {took:.0f}ms")
                if elements:
                    one = elements[0]
                    r.check("each control has a role and a rectangle",
                            bool(one.role) and one.width > 0 and one.height > 0,
                            one.describe())
                    r.check("its centre is a real point",
                            isinstance(one.centre, tuple) and len(one.centre) == 2)
                    r.check("unlabelled controls are still reported",
                            all(isinstance(e.named, bool) for e in elements),
                            f"{sum(1 for e in elements if not e.named)} unlabelled — "
                            f"these are exactly what OCR cannot see")

    with guard(r, "the walk is bounded"):
        r.check("there is a ceiling on elements", ax.MAX_ELEMENTS <= 20000,
                f"{ax.MAX_ELEMENTS}; a rendered page is tens of thousands of nodes")
        r.check("and on depth", ax.MAX_DEPTH <= 40, str(ax.MAX_DEPTH))

    with guard(r, "it degrades rather than raising"):
        r.check("an empty window gives an empty list",
                ax.elements_for_window({}) == [])
        r.check("a window with no owning process gives an empty list",
                ax.elements_for_window({"id": 0, "app": "nothing"}) == [])

    # ── Candidates, not coordinates ─────────────────────────────────────────
    r.head("Candidates, not coordinates")
    with guard(r, "locate returns evidence"):
        r.check("an empty intent finds nothing", perception.locate("") == [])
        r.check("a blank intent finds nothing", perception.locate("   ") == [])
        window = screen_context.frontmost_window()
        if window:
            found = perception.locate("a control that does not exist anywhere", window)
            r.check("nonsense returns nothing rather than a low-scoring guess",
                    found == [],
                    "returning something so that something comes back is how an "
                    "assistant clicks the wrong thing confidently")

    with guard(r, "a candidate carries why"):
        one = candidate("Save")
        r.check("it can explain itself", "Save" in one.why(), one.why())
        r.check("it describes itself in words", "save" in one.describe().lower(),
                one.describe())
        r.check("a single source is not corroborated", not one.corroborated)
        two = candidate("Save", sources=["accessibility", "ocr"])
        r.check("two sources are", two.corroborated)

    # ── Fusion ──────────────────────────────────────────────────────────────
    r.head("Two weak signals becoming one strong one")
    with guard(r, "the same control seen twice is merged"):
        # Measured case: an accessibility element spanning a whole row, and an
        # OCR hit on its first word 57 points away. A radius would not merge
        # them; containment does.
        wide = candidate("Search chats", conf=0.62, x=240, y=146, w=140, h=22)
        word = candidate("Search chats", conf=0.58, x=219, y=146, w=60, h=20,
                         sources=["ocr"], role="text")
        fused = perception._fuse([wide, word])
        r.check("one control, not two", len(fused) == 1, f"{len(fused)} candidates")
        if fused:
            r.check("agreement raises confidence above either alone",
                    fused[0].confidence > max(wide.confidence, word.confidence),
                    f"{wide.confidence:.2f} + {word.confidence:.2f} -> "
                    f"{fused[0].confidence:.2f}")
            r.check("and never reaches certainty", fused[0].confidence < 1.0)
            r.check("both sources are recorded",
                    set(fused[0].sources) == {"accessibility", "ocr"},
                    str(fused[0].sources))

    with guard(r, "different controls stay separate"):
        here = candidate("Save", x=100, y=100)
        far = candidate("Save As", x=600, y=400)
        r.check("two controls far apart are not merged",
                len(perception._fuse([here, far])) == 2)
        r.check("containment is what decides",
                perception._same_control(here, candidate("x", x=110, y=105, w=4, h=4))
                and not perception._same_control(here, far))

    # ── Ambiguity ───────────────────────────────────────────────────────────
    r.head("Two plausible targets is a question, not a coin toss")
    with guard(r, "close scores are ambiguous"):
        close = perception.Reading(
            candidates=[candidate("Save", 0.90), candidate("Save As", 0.86)],
            ambiguous=True, margin=0.04)
        r.check("the question names both", "Save" in close.question()
                and "Save As" in close.question(), close.question())
        clear = perception.Reading(
            candidates=[candidate("Save", 0.97), candidate("Save As", 0.40)],
            ambiguous=False, margin=0.57)
        r.check("a clear winner asks nothing", clear.question() == "")

    # ── Should Diablo act? ──────────────────────────────────────────────────
    r.head("Can I, and should I")
    with guard(r, "risk classes"):
        r.check("an ordinary button is low risk",
                computer_use.risk_of("Continue") == computer_use.LOW)
        r.check("anything destructive is high risk",
                computer_use.risk_of("Delete all") == computer_use.HIGH)
        r.check("typing is at least medium",
                computer_use.risk_of("hello", "type") == computer_use.MEDIUM)
        r.check("the bar rises with the risk",
                computer_use.NEEDED[computer_use.LOW]
                < computer_use.NEEDED[computer_use.MEDIUM]
                < computer_use.NEEDED[computer_use.HIGH])
        r.check("the high-risk bar is unreachable on purpose",
                computer_use.NEEDED[computer_use.HIGH] > 1.0,
                "an irreversible action is confirmed out loud every time, "
                "however certain the machine claims to be")

    with guard(r, "the decision"):
        sure = FakeReading(candidate("Back", 0.97))
        r.check("a confident, unambiguous, low-risk target is acted on",
                computer_use.decide(sure, computer_use.LOW).verdict == computer_use.ACT)
        r.check("the same target at high risk is still asked about",
                computer_use.decide(sure, computer_use.HIGH).verdict == computer_use.ASK,
                "confidence never buys past an irreversible action")

        unsure = FakeReading(candidate("Back", 0.62))
        r.check("a weak target is asked about, not guessed",
                computer_use.decide(unsure, computer_use.LOW).verdict == computer_use.ASK)

        two = FakeReading(candidate("Save", 0.9), ambiguous=True)
        r.check("an ambiguous target is asked about",
                computer_use.decide(two, computer_use.LOW).verdict == computer_use.ASK)

        gone = FakeReading(None, found=False)
        r.check("nothing found is a refusal, not a guess",
                computer_use.decide(gone, computer_use.LOW).verdict == computer_use.REFUSE)

        off = FakeReading(candidate("Back", 0.99, enabled=False))
        decision = computer_use.decide(off, computer_use.LOW)
        r.check("a disabled control is refused",
                decision.verdict == computer_use.REFUSE, decision.reason[:60])

    with guard(r, "every refusal says what stopped it"):
        for reading, risk in [(FakeReading(None, found=False), computer_use.LOW),
                              (FakeReading(candidate("x", 0.5)), computer_use.LOW),
                              (FakeReading(candidate("x", 0.99, enabled=False)),
                               computer_use.LOW)]:
            decision = computer_use.decide(reading, risk)
            if not decision.reason or len(decision.reason) < 12:
                r.bad("a refusal is explained", repr(decision.reason))
                break
        else:
            r.ok("a refusal is explained",
                 "'I am not confident' is not a reason anyone can check")

    # ── The record ──────────────────────────────────────────────────────────
    r.head("Why did Diablo do that")
    with guard(r, "an action record explains itself"):
        record = computer_use.ActionRecord(
            intent="go back", target="button “Back”",
            evidence=["the application calls it button “Back”"],
            action="click", verified=True)
        said = record.explain()
        r.check("the explanation names the target", "Back" in said, said[:70])
        r.check("and the evidence", "the application calls it" in said)
        r.check("and the outcome", "expected" in said)
        r.check("no internal reasoning leaks",
                "confidence=" not in said and "0." not in said, said[:70])

        refused = computer_use.ActionRecord(intent="go back",
                                            spoken="I cannot find that control.")
        r.check("a refusal is also a record",
                "did not act" in refused.explain(), refused.explain()[:60])

    # ── Safety regressions ──────────────────────────────────────────────────
    r.head("The safety floor holds")
    with guard(r, "computer use stays off"):
        r.check("control is off unless explicitly enabled",
                computer_use.ENABLED is False
                or os.getenv("DIABLO_COMPUTER_USE") == "1")
        record = computer_use.act_on("anything at all")
        r.check("the whole loop refuses while control is off",
                record.verdict == computer_use.REFUSE
                and "not allowed" in record.spoken.lower(),
                record.spoken[:60])
        r.check("and the refusal is recorded",
                computer_use.last_action() is not None)

    with guard(r, "a password manager is refused before perception runs"):
        vault = {"id": 0, "app": "1Password", "title": "Vault", "x": 0, "y": 0,
                 "width": 800, "height": 600, "sensitive": True}
        r.check("locate will not look inside one",
                perception.locate("password", vault) == [])

    with guard(r, "retries are bounded"):
        r.check("there is at most one automatic retry",
                computer_use.MAX_RETRIES <= 1,
                f"{computer_use.MAX_RETRIES}; a second blind attempt makes one "
                f"wrong coordinate into two wrong clicks")

    with guard(r, "guidance and action share one target"):
        import inspect
        source = inspect.getsource(__import__(
            "skills.pointing", fromlist=["x"]).point_at)
        r.check("pointing goes through the shared perception layer",
                "perception.read" in source,
                "a marker from OCR and a click from accessibility could land on "
                "different controls, and the user watching the ring could not tell")

    return r.report("perception")


if __name__ == "__main__":
    sys.exit(run())
