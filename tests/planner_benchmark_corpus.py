"""
Goals a single desktop action can actually satisfy.

The existing planner corpus (`evals/planner_cases.json`) scores the planner
against a stubbed screen, and its goals are `text_on_screen` phrases naming
things no click produces — "the Save button" is on the screen whether or not
anything is pressed. Measured during Build 3: a planner asked to make that true
answers `wait` six times out of six, and it is right to. A corpus like that
measures the corpus.

Every case here names a fixture state that a real click really does produce:

    target_app   WAITING -> FINISHED        by pressing Mark Done
                 WAITING -> POSTPONED       by pressing Mark Done Later
    icon_app     WAITING -> PRESSED SAVE 99 by pressing Save
                 WAITING -> PRESSED MAGNIFIER 1 by pressing the magnifier

`expect_control` is a control id from the fixture's own LAYOUT manifest, never a
coordinate, so a window that opens somewhere else does not invalidate a case.
`probe` is the phrase a person would use for that control; it exists only so the
runner can check, before measuring anything, that perception can reach the
target at all. A case whose target perception cannot resolve is not a planner
failure and must not be scored as one.

Five cases, not fifty. Each one is here because it covers something the others
do not.
"""

CORPUS_VERSION = "1.0"

CASES = [
    {
        "id": "done-labelled",
        "fixture": "target_app",
        "variant": "light",
        "intent": "mark the task as done",
        "goal_kind": "text_on_screen",
        "goal_value": "FINISHED",
        "expect_control": "mark_done",
        "expect_verb": "click",
        "probe": "Mark Done",
        "coverage": "labelled target, with a confusable neighbour present",
        "why": ("'Mark Done' is a complete prefix of 'Mark Done Later'. Choosing "
                "the neighbour is a meaningful failure: it sets POSTPONED, which "
                "is the opposite of the goal."),
    },
    {
        "id": "later-labelled",
        "fixture": "target_app",
        "variant": "light",
        "intent": "postpone the task for now",
        "goal_kind": "text_on_screen",
        "goal_value": "POSTPONED",
        "expect_control": "mark_done_later",
        "expect_verb": "click",
        "probe": "Mark Done Later",
        "coverage": "visually similar pair, the longer label",
        "why": ("The mirror of done-labelled. A planner biased toward the shorter "
                "or first-listed label fails one of the two and passes the other, "
                "which one case alone would not reveal."),
    },
    {
        "id": "save-among-icons",
        "fixture": "icon_app",
        "variant": "light",
        "intent": "save",
        "goal_kind": "text_on_screen",
        "goal_value": "PRESSED SAVE 99",
        "expect_control": "save_label",
        "expect_verb": "click",
        "probe": "Save",
        "coverage": "the one labelled control in a toolbar of unlabelled ones",
        "why": ("Accessibility publishes four buttons with no title and one with "
                "'Save'. The named one should be reached without any visual "
                "reasoning at all."),
    },
    {
        "id": "search-icon",
        "fixture": "icon_app",
        "variant": "light",
        "intent": "open the search",
        "goal_kind": "text_on_screen",
        "goal_value": "PRESSED MAGNIFIER 1",
        "expect_control": "magnifier",
        "expect_verb": "click",
        "probe": "the search icon",
        "coverage": "target reachable only through perception",
        "why": ("The magnifier has no title and no text. Accessibility can say a "
                "button is there and not what it is, so resolving it needs the "
                "vision source. The planner's own account of the screen lists it "
                "as an unlabelled button, so this case measures whether the "
                "planner can name a control it has no name for."),
    },
    {
        "id": "archive-disabled",
        "fixture": "controls_app",
        "variant": "light",
        "intent": "archive the item",
        "goal_kind": "text_on_screen",
        "goal_value": "ARCHIVED",
        "expect_control": "archived",
        "expect_verb": "click",
        "probe": "Archive",
        "coverage": "disabled target",
        "why": ("'Archive' exists, is resolvable, and is disabled, so no single "
                "click can reach the goal. The corpus keeps it precisely so the "
                "validator has something to reject: it must come back as "
                "INVALID_GOAL and must not be counted against the planner."),
    },
]
