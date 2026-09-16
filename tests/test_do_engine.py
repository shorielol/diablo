"""
Taking responsibility for an outcome, rather than performing one click.

These are state-machine tests. The planner and the action layer are both
injected, because a suite whose result depends on what a 4B felt like saying
measures the 4B, and a suite that needs a real click measures the window
server. What is under test here is the loop: that it observes before acting,
that it stops when the GOAL is true rather than when a click succeeded, that
an unexpected screen causes a rethink rather than a repeat, and that nothing
can talk it into claiming success it did not see.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Results, guard  # noqa: E402

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
sys.path.insert(0, DIABLO_HOME)

from skills import computer_use, do_engine as engine  # noqa: E402
from skills.do_engine import ActionState, Goal, Step, Task, TaskState  # noqa: E402

WINDOW = {"id": 1, "app": "Fixture", "title": "Fixture", "x": 0.0, "y": 0.0,
          "width": 500.0, "height": 300.0, "sensitive": False}


class World:
    """
    A screen that changes only when something acts on it.

    Deterministic on purpose: every test states what the world does in
    response to each step, so a failure is about the engine and never about
    what was on the real display at the time.
    """

    def __init__(self, goal_after=None, effects=None, window=None):
        self.clicks = []
        self.typed = []
        self.goal_after = goal_after          # steps needed before the goal holds
        self.effects = effects or {}          # target -> verified True/False
        self.window = window or dict(WINDOW)
        self.observations = 0

    def goal_holds(self, _window=None):
        self.observations += 1
        if self.goal_after is None:
            return False
        return len(self.clicks) + len(self.typed) >= self.goal_after

    def act(self, intent, task="", expect="", window=None, confirm=None, **kw):
        self.clicks.append(intent)
        outcome = self.effects.get(intent, True)
        record = computer_use.ActionRecord(
            task=task, at=time.time(), intent=intent, target=intent,
            action="click", expected=expect)
        if outcome == "ask":
            record.verdict = "ask"
            record.action = ""
            record.spoken = "I can see two things that could be it."
        elif outcome == "refuse":
            record.verdict = "refuse"
            record.action = ""
            record.spoken = "I am not allowed to control the computer, Sir."
        else:
            record.verdict = "act"
            record.verified = bool(outcome)
            record.observed = ("New on screen: \"next\"." if outcome
                               else "Nothing I can read has changed.")
        return record

    def install(self, monkey):
        monkey(computer_use, "act_on", self.act)


def patched(target, name, value):
    """Replace an attribute and remember how to put it back."""
    original = getattr(target, name)
    setattr(target, name, value)
    return lambda: setattr(target, name, original)


def run_task(world, plan, goal=None, **kw):
    """Run one task against a deterministic world."""
    undo = [patched(computer_use, "act_on", world.act),
            patched(Goal, "verified", lambda self, window=None: world.goal_holds(window)),
            patched(engine, "_active", None)]
    try:
        from skills import screen_context
        undo.append(patched(screen_context, "frontmost_window", lambda: world.window))
        undo.append(patched(screen_context, "window_by_id", lambda i: world.window))
        undo.append(patched(screen_context, "has_moved", lambda w, slack=2.0: None))
        return engine.run("do the thing", goal or Goal("text_on_screen", "done"),
                          window=world.window, planner=plan, **kw)
    finally:
        for put_back in reversed(undo):
            put_back()
        engine._active = None


def one_step(target="the button", expect="next"):
    return lambda task, window: Step(verb="click", target=target,
                                     expect=expect, why="it is the next thing")


def run():
    r = Results()

    # ── The task, and the action, are different things ──────────────────────
    r.head("A task is not an action")
    with guard(r, "the states are separate"):
        r.check("the task has its own states",
                {s.value for s in TaskState} >= {
                    "new", "planning", "observing", "executing", "verifying",
                    "recovering", "waiting_for_user", "completed", "blocked",
                    "failed", "cancelled"})
        r.check("an action has its own, and they are not the same",
                {s.value for s in ActionState} >= {
                    "planned", "executing", "verifying", "succeeded", "failed",
                    "cancelled", "stale"}
                and TaskState.EXECUTING.value in {s.value for s in TaskState})
        r.check("a task knows when it is over",
                Task(state=TaskState.COMPLETED).over
                and not Task(state=TaskState.EXECUTING).over)

    # ── The goal ends the task, not the click ───────────────────────────────
    r.head("Done means the goal is true")
    with guard(r, "an already-satisfied goal acts zero times"):
        world = World(goal_after=0)
        task = run_task(world, one_step())
        r.check("it completes without touching anything",
                task.state is TaskState.COMPLETED and not world.clicks,
                f"{len(world.clicks)} clicks")
        r.check("and says what it verified", "done" in task.summary().lower(),
                task.summary())

    with guard(r, "a one-action task"):
        world = World(goal_after=1)
        task = run_task(world, one_step())
        r.check("one action, then complete",
                task.state is TaskState.COMPLETED and len(world.clicks) == 1,
                f"{len(world.clicks)} clicks")

    with guard(r, "a multi-action task"):
        world = World(goal_after=3)
        task = run_task(world, one_step())
        r.check("it keeps going until the goal holds",
                task.state is TaskState.COMPLETED and len(world.clicks) == 3,
                f"{len(world.clicks)} clicks")
        r.check("and it observed before each one",
                world.observations >= len(world.clicks),
                f"{world.observations} observations for {len(world.clicks)} actions")

    with guard(r, "a successful click is not a finished task"):
        world = World(goal_after=None)          # the goal never becomes true
        task = run_task(world, one_step())
        r.check("every action succeeded and the task still did not complete",
                task.state is not TaskState.COMPLETED,
                f"ended {task.state.value} after {len(world.clicks)} clicks")
        r.check("and it did not claim success",
                "done" not in task.summary().lower(), task.summary())

    with guard(r, "the planner cannot declare victory"):
        world = World(goal_after=None)
        task = run_task(world, lambda t, w: Step(verb="done", why="I think so"))
        r.check("a planner saying done against an unverified goal blocks",
                task.state is TaskState.BLOCKED, task.state.value)
        r.check("and says so honestly",
                "cannot confirm" in task.summary(), task.summary())

    # ── Bounds ──────────────────────────────────────────────────────────────
    r.head("Nothing runs forever")
    with guard(r, "step ceiling"):
        world = World(goal_after=None)
        task = run_task(world, one_step())
        r.check("it stops at the step ceiling",
                len(world.clicks) <= engine.MAX_STEPS,
                f"{len(world.clicks)} of at most {engine.MAX_STEPS}")

    with guard(r, "runtime limit"):
        world = World(goal_after=None)
        task = run_task(world, one_step(), runtime=-1.0)
        r.check("an expired task fails rather than continuing",
                task.state is TaskState.FAILED, task.state.value)
        r.check("and says it ran out of time",
                "time" in task.summary().lower(), task.summary())

    with guard(r, "recovery ceiling"):
        world = World(goal_after=None, effects={"the button": False})
        task = run_task(world, one_step())
        r.check("repeated failure stops rather than looping",
                task.state is TaskState.BLOCKED
                and task.recoveries <= engine.MAX_RECOVERIES + 1,
                f"{task.recoveries} recoveries, ended {task.state.value}")
        r.check("and explains what it saw, not a status code",
                "nothing happened" in task.summary().lower()
                or "rather stop" in task.summary().lower(), task.summary())

    # ── Failure classification ──────────────────────────────────────────────
    r.head("Naming the failure, so recovery is a decision")
    with guard(r, "classes"):
        step = Step(verb="click", target="Save", expect="Saved")
        nothing = computer_use.ActionRecord(verdict="act", verified=False,
                                            observed="Nothing I can read has changed.")
        r.check("a click that changed nothing is NO_EFFECT",
                engine.classify(nothing, WINDOW, WINDOW, step) == engine.NO_EFFECT)

        asked = computer_use.ActionRecord(verdict="ask", spoken="which one?")
        r.check("an unresolved target is AMBIGUITY",
                engine.classify(asked, WINDOW, WINDOW, step) == engine.AMBIGUITY)

        denied = computer_use.ActionRecord(
            verdict="refuse", spoken="I am not allowed to control the computer.")
        r.check("a refusal on permission is PERMISSION_FAILURE",
                engine.classify(denied, WINDOW, WINDOW, step)
                == engine.PERMISSION_FAILURE)

        r.check("no record at all is UNKNOWN, not success",
                engine.classify(None, WINDOW, WINDOW, step) == engine.UNKNOWN)

    with guard(r, "an unexpected screen is a new situation, not a failure"):
        world = World(goal_after=2, effects={"Save": False})
        seen = []

        def planner(task, window):
            seen.append(len(task.steps))
            return Step(verb="click", target="Save" if not task.steps else "Confirm",
                        expect="Saved", why="save it")

        task = run_task(world, planner)
        r.check("the plan is revised after the unexpected result",
                len(seen) > 1 and world.clicks[:2] == ["Save", "Confirm"],
                f"clicked {world.clicks[:3]}")
        r.check("and it re-observed before the second attempt",
                world.observations >= len(seen))

    # ── Asking, rather than guessing ────────────────────────────────────────
    r.head("It stops to ask")
    with guard(r, "ambiguity ends the loop with a question"):
        world = World(goal_after=None, effects={"the button": "ask"})
        task = run_task(world, one_step())
        r.check("an ambiguous target stops the task",
                task.state is TaskState.WAITING_FOR_USER, task.state.value)
        r.check("the question is what reaches the user",
                "could be it" in task.summary(), task.summary())
        r.check("and it did not keep clicking", len(world.clicks) == 1,
                f"{len(world.clicks)} clicks")

    with guard(r, "no permission stops the task"):
        world = World(goal_after=None, effects={"the button": "refuse"})
        task = run_task(world, one_step())
        r.check("a permission refusal blocks rather than retries",
                task.state is TaskState.BLOCKED and len(world.clicks) == 1,
                f"{task.state.value}, {len(world.clicks)} clicks")

    with guard(r, "it will not type what it was not told"):
        world = World(goal_after=None)
        task = run_task(world, lambda t, w: Step(verb="type", value="", why="?"))
        r.check("an empty value blocks instead of typing nothing",
                task.state is TaskState.BLOCKED, task.state.value)

    # ── One at a time, and stoppable ────────────────────────────────────────
    r.head("One task at a time")
    with guard(r, "a second task is refused while one is running"):
        engine._active = Task(intent="first", state=TaskState.EXECUTING)
        try:
            second = engine.run("second", Goal("text_on_screen", "x"),
                                planner=one_step())
            r.check("the second task is blocked, not started",
                    second.state is TaskState.BLOCKED, second.state.value)
            r.check("and says so plainly",
                    "already in the middle" in second.summary(), second.summary())
        finally:
            engine._active = None

    with guard(r, "cancellation"):
        engine._active = Task(intent="running", state=TaskState.EXECUTING)
        try:
            before = engine._active.generation
            r.check("cancelling a running task works", engine.cancel())
            r.check("it moves to cancelled",
                    engine._active.state is TaskState.CANCELLED)
            r.check("and the generation moves, so late callbacks are stale",
                    engine._active.generation > before,
                    f"{before} -> {engine._active.generation}")
            r.check("cancelling twice is not an error", not engine.cancel())
        finally:
            engine._active = None

    with guard(r, "a superseded task stops mid-loop"):
        world = World(goal_after=None)

        def cancel_after_first(task, window):
            if task.steps:
                task.generation += 1        # something else took over
            return Step(verb="click", target="the button", why="go")

        task = run_task(world, cancel_after_first)
        r.check("it stops once it has been superseded",
                len(world.clicks) <= 2, f"{len(world.clicks)} clicks")

    with guard(r, "stale context expires"):
        old = Task(intent="ancient")
        old.updated_at = time.time() - engine.CONTEXT_EXPIRY - 5
        r.check("an old task is not the context for the next sentence",
                old.stale_context())
        fresh = Task(intent="current")
        r.check("a recent one is", not fresh.stale_context())

    # ── What it tells you ───────────────────────────────────────────────────
    r.head("The timeline, and what the user hears")
    with guard(r, "a task records what happened"):
        world = World(goal_after=2)
        task = run_task(world, one_step())
        trace = task.trace()
        r.check("every stage is on the timeline",
                "observing" in trace and "planning" in trace
                and "action" in trace and "completed" in trace,
                f"{len(task.timeline)} moments")
        r.check("each is timestamped from the start", "+" in trace.splitlines()[0])
        r.check("the goal is on it", "goal" in trace)

    with guard(r, "the user hears sentences, not stages"):
        heard = []
        world = World(goal_after=2)
        run_task(world, one_step(), say=heard.append)
        r.check("progress is spoken in ordinary words", heard,
                "; ".join(heard[:2]))
        r.check("and never names an internal state",
                not any(s.value in line.lower() for line in heard
                        for s in TaskState),
                "; ".join(heard[:2]))
        r.check("one line per action at most", len(heard) <= len(world.clicks),
                f"{len(heard)} lines for {len(world.clicks)} actions")

    with guard(r, "a partial result is neither done nor failed"):
        r.check("blocked is its own outcome, distinct from failed",
                TaskState.BLOCKED is not TaskState.FAILED
                and TaskState.BLOCKED in engine.OVER)
        r.check("waiting for the user is not failure",
                TaskState.WAITING_FOR_USER not in engine.OVER,
                "a task that stopped to ask can still be continued")

    # ── Two bugs that only a real run found ─────────────────────────────
    r.head("Waiting is not progress")
    with guard(r, "consecutive waits are bounded"):
        world = World(goal_after=None)
        task = run_task(world, lambda t, w: Step(verb="wait", why="still settling"))
        r.check("a planner stuck on wait is stopped",
                task.state is TaskState.BLOCKED, task.state.value)
        r.check("and stopped by the wait ceiling, not the runtime limit",
                "stopped changing" in task.summary()
                or "ran out of things" in task.summary(), task.summary())
        r.check("there is a ceiling at all", engine.MAX_WAITS <= 20,
                f"{engine.MAX_WAITS}; waiting does not change the screen, so a "
                f"planner stuck on it makes no progress however long it is given")

    with guard(r, "a wait between real steps does not count against it"):
        world = World(goal_after=2)
        order = []

        def alternating(task, window):
            order.append(len(task.steps))
            if len(order) % 2 == 1 and len(task.steps) < 2:
                return Step(verb="wait", why="settling")
            return Step(verb="click", target="the button", why="go")

        task = run_task(world, alternating)
        r.check("waits interleaved with progress are not fatal",
                task.state is TaskState.COMPLETED, task.state.value)

    r.head("Reading a window that will not photograph")
    with guard(r, "the goal falls back to a region capture"):
        import inspect
        source = inspect.getsource(engine._read_window_text)
        r.check("there is a fallback when the window capture reads as empty",
                "capture_region" in source,
                "measured: screencapture -l returned a valid image with a title "
                "bar and a blank body, while the same rectangle off the screen "
                "showed everything")
        r.check("the title bar alone does not count as having read the window",
                "window.get(\"title\")" in source)

    return r.report("do engine")


if __name__ == "__main__":
    sys.exit(run())
