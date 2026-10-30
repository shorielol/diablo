"""
D.I.A.B.L.O. — the conversational door to the teaching system.

GUI-X taught Diablo four ways to show something: a point, an outline, a
teaching cursor, and a path through several places. All of it sat behind the
`point_at` tool, and a real turn almost never reached it. The router sent
"where is the welfare loss region?" to a clarifying question; the planner
answered "explain the subsidy" with a web search and "show me the equilibrium"
by asking whether chemical equilibrium was meant; "show me how the subsidy
causes welfare loss" went to the tutor, which anchors each step on printed
words and so landed on the legend.

This module is only the door. It does not find anything, draw anything or
decide how to show it — grounding, `point_at` and `teaching` still do all of
that. It takes a request `target.asked_to_show` has already read, and:

    LOCATE     hands the named thing to point_at, as the planner would have
               if it had not made up a window called "chrome"
    EXPLAIN    asks for steps — what to show, and one sentence each — and
    TRACE      hands them to point_at, which grounds them one by one

The steps come from one constrained generation against `teaching.STEP_ITEM`,
the same object point_at's own schema uses. It names things. It has no field
for where they are, and `teaching.clean_steps` drops any a model adds.

When there is nothing to show — no steps, or none of them could be found —
the answer is the existing screen explanation, spoken. Not "there is nothing I
can show you", which is true and useless to someone who asked what a thing
means.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from skills import target, teaching, trace

# A spoken lesson, not a lecture: four places is already about a minute.
PLAN_STEPS = 4
# How much of the window's text the step writer sees. It is there to say what
# the picture is and what its parts are called, not to be read in full.
SCREEN_CHARS = 2500

PLAN_PROMPT = """You are showing someone something on their screen, one place at a time, while you explain it aloud.

You are given their question and the text visible in the window they are looking at.

List the places to show, in the order that explains it best. At most four.

For each place:
  "what"  WHICH thing, in your own words and specifically enough that
          someone could find it: what it looks like and which lines or shapes
          it lies between or on. A kind on its own, such as "region" or
          "curve", names nothing. Name the thing, not the caption printed next
          to it, and never where it is: no coordinates, no "top left", no
          "next to the legend".
  "say"   one short spoken sentence about it, in plain words.
  "kind"  "region" for an area or point of a picture, "figure" for a whole
          chart, "control" for a button or menu, "text" for words. Leave it
          empty if unsure.

The text on screen tells you what the parts are called. It is not the list of
places to show and not the order to show them in: a legend lists everything,
and the question needs only the parts that answer it, in the order the
explanation runs — how it was, what changed, what that caused. Many parts of a
picture have no label at all, such as the point where two curves cross or the
gap between two lines. Name those by what they are.

If nothing on the screen relates to the question, give no places at all."""

# Added to the request when the question is about a picture: its parts are the
# answer, and the words printed beside them are not. The 4B ended "explain the
# subsidy" on "the text labeled 'Subsidy'" in 5 runs of 5.
PICTURE_ONLY = ("Every place must be something drawn in the picture itself. "
                "Never a caption, a legend entry or a button.")
# And when it asked about one shape: "what does this triangle mean" came back
# as the triangle and three other areas, 5 runs of 5.
ONE_PLACE = "They asked about one thing, so give exactly one place: that one."

# Kinds a picture's explanation may not show. A caption grounds to the words,
# and the words are the thing this whole path exists not to ring.
NOT_PICTURE = (target.TEXT, target.CONTROL)


@dataclass
class Prepared:
    """A request that will be shown, and what was read to decide that."""

    question: str
    shown: Any                        # target.Shown
    window: Optional[Dict[str, Any]]
    screen_text: str = ""
    refused: str = ""                 # said instead: a private window, or no clear one


def _screen_text(window: Optional[Dict[str, Any]]) -> str:
    """The text OCR reads off one window, one fragment per line. "" if none."""
    if not window:
        return ""
    try:
        from skills import tutor
        path, fragments, _ = tutor.read_window(window)
    except Exception as e:
        trace.event("show", phase="read", error=f"{type(e).__name__}: {e}"[:120])
        return ""
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass
    return "\n".join(str(f.get("text") or "").strip() for f in fragments
                     if str(f.get("text") or "").strip())


def _on_screen(semantic: str, screen_text: str) -> bool:
    """
    Whether what was named is plausibly what this window is about.

    Half the named words, each found as a word on screen or sharing its first
    five letters with one ("subsidy" and "subsidised"). Enough to tell a
    subsidy diagram from an inbox, which is the whole job: this only decides
    whether to try, and grounding still decides whether it is really there.
    """
    named = [w for w in target._named(semantic) if len(w) > 2]
    if not named or not screen_text:
        return False
    seen = set(re.findall(r"[a-z']+", screen_text.lower()))
    stems = {w[:5] for w in seen if len(w) >= 5}

    def found(word: str) -> bool:
        return word in seen or (len(word) >= 5 and word[:5] in stems)

    hits = sum(1 for w in named if found(w))
    return hits * 2 >= len(named)


def _pointed(question: str) -> bool:
    lowered = (question or "").lower()
    return any(phrase in lowered for phrase in target.CURSOR_PHRASES)


def _listed_or(names: List[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _content(question: str, shown: Any):
    """
    Which window this turn is about, decided once, before anything looks (B.5.1).

    In order: a window the question names ("in Preview", "in the Subsidy and
    Welfare document"), then the window under the pointer for "what am I
    pointing at", then the one in front. Named content outranks both, and a
    name that matches two windows is asked about rather than guessed.

    Returns (question, shown, window, refused). When a name was read off the
    question, `question` and `shown` come back without it, so "in Preview"
    is never looked for as part of the thing.
    """
    from skills import screen_context
    scope = target.scope_of(question)
    window = None
    if scope is not None and not scope.generic:
        found = screen_context.matching_windows(scope.name,
                                                apps_only=not scope.explicit)
        if len(found) > 1:
            names = [w.get("title") or w.get("app") or "" for w in found[:3]]
            return question, shown, None, (f"Which one do you mean, Sir, "
                                           f"{_listed_or(names)}?")
        if found:
            window = found[0]
        elif scope.explicit:
            return question, shown, None, f"I can't see {scope.said} on screen, Sir."
        else:
            scope = None          # "in consumer surplus": part of the thing
    if scope is not None:
        question = scope.body
        shown = target.asked_to_show(question) or shown
    if window is None and _pointed(question):
        try:
            from skills import display
            cursor = display.cursor()
        except Exception:
            cursor = None
        if cursor:
            window = screen_context.window_at(cursor[0], cursor[1])
    if window is None:
        window = screen_context.frontmost_window()
    if window is None:
        return question, shown, None, "I can't see a window to look at, Sir."
    return question, shown, window, ""


def prepare(question: str, shown: Any) -> Optional[Prepared]:
    """
    Whether this turn will be shown, and what it will be shown from.

    None means no: the turn goes on exactly as it would have without this.
    Nothing is spoken or drawn here, so a request that turns out not to be
    about the screen costs one window read and nothing the user notices.
    """
    if shown is None:
        return None
    try:
        question, shown, window, refused = _content(question, shown)
    except Exception as e:
        trace.event("show", phase="scope", error=f"{type(e).__name__}: {e}"[:120])
        return Prepared(question, shown, None,
                        refused="I couldn't tell which window you meant, Sir.")
    trace.event("show", phase="scope", window_id=(window or {}).get("id", 0),
                app=(window or {}).get("app", ""), refused=bool(refused))
    if refused:
        return Prepared(question, shown, window, refused=refused)
    if window and window.get("sensitive"):
        return Prepared(question, shown, window,
                        refused=(f"I won't read a {window.get('app', '')} window, "
                                 f"Sir. Password managers and banking stay private."))
    if (shown.intent in (target.LOCATE, target.GUIDE) and shown.certain) or \
            _pointed(question):
        return Prepared(question, shown, window)

    text = _screen_text(window)
    if not shown.certain and not _on_screen(shown.target.semantic, text):
        trace.event("show", phase="declined", intent=shown.intent,
                    reason="what was named is not on the screen in front")
        return None
    return Prepared(question, shown, window, screen_text=text)


def _about_a_picture(shown: Any) -> bool:
    return shown is not None and shown.target.kind in (target.REGION, target.FIGURE)


def _about_one_shape(shown: Any) -> bool:
    """ "What does this triangle mean": one area, named, asked about."""
    return (shown is not None and shown.intent == target.EXPLAIN
            and shown.target.kind in (target.REGION, target.OBJECT))


def plan_steps(question: str, screen_text: str, title: str = "",
               shown: Any = None) -> List[dict]:
    """
    What to show and what to say at each place. [] if it cannot say.

    One constrained generation, shaped by teaching.STEP_ITEM. It never
    raises: a model that is down means an explanation without a path, not a
    turn that fails. What the request was about is enforced here as well as
    asked for, because asking was measured and the 4B did not listen.
    """
    from skills import local_brain
    schema = {"type": "object",
              "properties": {"steps": {"type": "array", "items": teaching.STEP_ITEM}},
              "required": ["steps"]}
    window = f"Window: {title}\n" if title else ""
    rules = [PICTURE_ONLY] if _about_a_picture(shown) else []
    if _about_one_shape(shown):
        rules.append(ONE_PLACE)
    extra = ("\n\n" + " ".join(rules)) if rules else ""
    try:
        result = local_brain.chat_json(
            [{"role": "system", "content": PLAN_PROMPT},
             {"role": "user", "content":
              f"Their question: {question}\n\n{window}"
              f"Text visible in the window:\n{screen_text[:SCREEN_CHARS]}{extra}"}],
            schema=schema, num_predict=600)
    except Exception as e:
        trace.event("show", phase="plan", error=f"{type(e).__name__}: {e}"[:120])
        return []
    steps = teaching.clean_steps((result or {}).get("steps"), limit=PLAN_STEPS)
    if _about_a_picture(shown):
        dropped = [s["what"] for s in steps if s.get("kind") in NOT_PICTURE]
        steps = [s for s in steps if s.get("kind") not in NOT_PICTURE]
        if dropped:
            # Counts, not names: a step's name is read off his screen.
            trace.event("show", phase="plan", dropped_count=len(dropped),
                        reason="a caption or control is not part of the picture")
    if _about_one_shape(shown):
        steps = steps[:1]
    trace.event("show", phase="plan", steps=len(steps))
    return steps


def _record(working: Optional[List[dict]], tool: str, args: dict, result: str,
            readings: List[Any]) -> None:
    """The same shape agent_core.run keeps for a tool call."""
    if working is None:
        return
    step: Dict[str, Any] = {"tool": tool, "args": json.dumps(args)[:200],
                            "result": (result or "")[:600]}
    if readings:
        step["observation"] = readings[0]
    working.append(step)


def _grounded_any(readings: List[Any]) -> bool:
    return any(getattr(r, "candidates", None) for r in readings)


def _explain(prepared: Prepared, working: Optional[List[dict]]) -> str:
    """The existing screen explanation, for when there is nothing to show."""
    from skills import screen_context
    # The window the turn was about, by name. Left empty, explain() reads
    # whatever is in front when it runs, which can be a minute after the
    # question: live, that explained the code editor instead of the diagram.
    window = prepared.window or {}
    pinned = window.get("title") or window.get("app") or ""
    try:
        answer = screen_context.explain(prepared.question, pinned)
    except Exception as e:
        answer = (f"I couldn't make sense of the screen just now, Sir "
                  f"({type(e).__name__}).")
    _record(working, "explain_screen", {"question": prepared.question}, answer, [])
    return answer


def _by_menu(prepared: Prepared, shown: Any, working: Optional[List[dict]]) -> str:
    """
    The app's own command for it, when the screen alone will not do.

    "how do I open settings in Zen" ended at "I can't see a control on screen"
    with Zen › Settings… (⌘,) in Zen's menu bar. A how-to goes to the app's
    commands first; a "where is" goes there only when the control is not
    decisively on screen, so a visible Save button is still pointed at.
    Returns "" to let the screen-only path carry on.
    """
    from skills import perception, task_context
    try:
        context = task_context.about(prepared.question, front=prepared.window)
        if context.source != task_context.FRONT:
            task_context.bring_up(context)
        # A control decisively on screen is pointed at the usual way, for a
        # how-to as much as a where-is: the visible New Note button beats a
        # menu path. The menus are for when the screen does not settle it.
        if context.source == task_context.FRONT and prepared.window is not None:
            seen = perception.read(shown.target.semantic, prepared.window)
            if seen.verdict == perception.Verdict.RESOLVED:
                return ""
        answer = task_context.guide(prepared.question, context) or ""
    except Exception as e:         # the menu route is extra; its failure is not the turn's
        trace.event("show", phase="menu_failed", reason=f"{type(e).__name__}: {e}"[:120])
        return ""
    if answer:
        _record(working, "menu_route", {"question": prepared.question}, answer, [])
        trace.event("show", phase="menu", intent=shown.intent,
                    app=(context.app.name if context.app else ""))
    return answer


def present(prepared: Prepared, speak: Callable[..., Any],
            working: Optional[List[dict]] = None) -> str:
    """Show it, through the existing point_at. Returns what to say last."""
    from skills import pointing
    if prepared.refused:
        return prepared.refused
    shown = prepared.shown
    wanted = shown.target

    if shown.intent in (target.LOCATE, target.GUIDE) or _pointed(prepared.question):
        # The thing under the pointer is found from the question, which is
        # what says "the pointer"; anything named is found by its name. An
        # action ("show me how to open a new note") is a control found by what
        # it does, so its operation goes too (B.3).
        pointed = _pointed(prepared.question)
        if not pointed:
            routed = _by_menu(prepared, shown, working)
            if routed:
                return routed
        description = prepared.question if pointed else wanted.semantic
        kind = target.OBJECT if pointed else wanted.kind
        extra = {"operation": wanted.operation} if wanted.operation else {}
        readings: List[Any] = []
        answer = pointing.point_at(description, "", reading_out=readings, kind=kind,
                                   window=prepared.window, **extra)
        _record(working, "point_at", {"description": description, "kind": kind},
                answer, readings)
        trace.event("show", phase="shown", intent=shown.intent, kind=kind,
                    grounded=_grounded_any(readings))
        return answer

    title = (prepared.window or {}).get("title", "")
    steps = plan_steps(prepared.question, prepared.screen_text, title, shown)
    if not steps:
        trace.event("show", phase="explained", intent=shown.intent,
                    reason="no steps to show")
        return _explain(prepared, working)

    readings = []
    answer = pointing.point_at(prepared.question, "", reading_out=readings,
                               steps=steps, speak=speak, window=prepared.window)
    _record(working, "point_at", {"description": prepared.question,
                                  "steps": steps}, answer, readings)
    if not _grounded_any(readings):
        # Every step named something the screen does not have. Saying so is
        # honest and no help to someone who asked what a thing means.
        trace.event("show", phase="explained", intent=shown.intent,
                    reason="no step could be found on screen")
        return _explain(prepared, working)
    trace.event("show", phase="shown", intent=shown.intent, steps=len(steps))
    return answer
