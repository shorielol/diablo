# Security boundaries: from a model's answer to a click

A model can be wrong in ways that are dangerous to act on. It can point at
"Delete All" and call it "Save". It can answer about a screen that changed a
second ago. It can point at a control that another window has since covered.
This document lists every boundary between a model's output and an input
event, where each one is enforced, and the test that proves it.

The rule underneath all of them: **the application's own account of the
control under the point decides what is pressed, never a model's account.**

## The path

```
GoClick (or perception)            grounding_gate                 computer_use
───────────────────────            ──────────────                 ────────────
screenshot + "Save"   ─proposal─►  validate(): the control   ─ValidatedTarget─►  click():
                                   really under the point,                       gates, permission,
                                   by accessibility, now                         revalidate(), post
```

- A model produces a `GroundingProposal` (`diablo/grounding_gate.py:87`): a
  point, its coordinate space, and the capture it was read from. Nothing
  else.
- `grounding_gate.validate` (`:358`) turns a proposal into a
  `ValidatedTarget` or one `REJECT_*` verdict. The target's rectangle and
  label come from accessibility (`:468`), never from the proposal.
- `computer_use.click` (`diablo/computer_use.py:432`) accepts only a target
  the gate issued. `act_on`, the path perception and the planner use, makes a
  last frontmost check with `grounding_gate.check_point` before it posts
  (`computer_use.py:1121`).
- Only `computer_use.py` posts input events.

## The invariants

| Invariant | Enforced at | Proven by (`tests/test_grounding_gate.py` unless named) |
|---|---|---|
| A model coordinate cannot execute without validation | `click()` refuses anything `grounding_gate.issued()` rejects (`computer_use.py:474`); every target carries an HMAC seal over all its fields, keyed per process (`grounding_gate.py:143`, `:190`) | "a bare coordinate", "a relabelled target", "built by hand: not issued", "an edited copy of a validated target is not issued" |
| A model label cannot decide destructive permission | the target's label is the element's own (`grounding_gate.py:468`); `click()` judges `is_destructive` on it (`computer_use.py:528`) | "point on Delete All, labelled Save" → `REJECT_LABEL_MISMATCH`; "a destructive control needs its own yes, by its own label" |
| A stale coordinate cannot execute | `validate` compares the control under the point now with the one the capture recorded (`grounding_gate.py:437`); `click()` calls `revalidate` immediately before posting (`computer_use.py:574`) | cases 6, 7, 8 and 11; "a target that went stale between permission and click"; live in `evals/run_grounding_gauntlet.py validation` |
| A wrong-window coordinate cannot execute | the hit test asks the window server what is frontmost; a different process or window frame is `REJECT_WRONG_WINDOW` (`grounding_gate.py:421`); the same check runs in `revalidate` and in `act_on`'s `check_point` (`:552`) | case 4 (three ways); "covered by another application"; "act_on's last look" |
| A disabled control cannot execute | `REJECT_DISABLED` in `validate`, `revalidate` and `check_point` | case 3; "became disabled" |
| An ambiguous target cannot execute | two controls with the target's name → `REJECT_AMBIGUOUS`, whatever the model's confidence (`grounding_gate.py:459`) | case 5 |
| An invalid coordinate space cannot execute | `Space` has four members and no default; each has one conversion, bounded first (`grounding_gate.py:282`); a point outside its capture is refused | case 9 (six variants); "each space converts the one way it should" |
| A model cannot call computer-control APIs | `grounding_gate.py` and `goclick.py` import no action layer and name no click or event API; GoClick runs in a separate process with no access to `computer_use` | the "architecture" section, checked by parsing the source |
| A control with no name is never pressed on a model's word | `CONFIRM_REQUIRED`, and `click()` asks the person every time (`computer_use.py:521`) | "an unnamed control needs a person's yes" |
| Keystrokes go only into a text field | `type_text` refuses with `no_field` unless the focused control is editable, before anyone is asked (`computer_use.py:662`) | `tests/test_action_outcomes.py`: "a button has focus, so a space would press it", "focus cannot be read" |

The same verdicts on real windows, not stand-ins, come from
`evals/run_grounding_gauntlet.py validation` against `tests/fixtures/gate_app.py`.
That fixture has Save beside Delete All, a disabled Export, a duplicate Save,
an unlabelled gear, and stdin commands that hide, move, rename or disable a
control after it has been looked at.

## What validation is

`validate` runs these checks in order and stops at the first that fails:

1. **The coordinate.** Its space is named, finite and inside its own bounds.
   It comes from the capture the observation holds (`frame_id`) and lands
   inside that capture and on a display.
2. **The observation.** The capture is readable and at most
   `DIABLO_PROPOSAL_MAX_AGE` seconds old (10 by default). The window still
   exists, has not moved, and contains the point.
3. **What is there now.** The accessibility hit test answers for the
   frontmost window at the point, in the expected process and window.
4. **The same control.** The element now under the point has the role, name
   and rectangle the observation recorded there.
5. **What was asked for.** The role is pressable and compatible with any role
   word in the request ("the Save checkbox" is not a button). The element's
   own name is exactly the requested name: "Delete" is not "Delete All".
6. **Pressable.** It is enabled.
7. **Unique.** No other control in the window has that name.

The age limit is a ceiling, not the test. The test is step 4, repeated by
`revalidate` immediately before the click. A screen can change in a tenth of
a second or stay the same for a minute; asking the screen beats reading a
clock.

## Where each model sits

- **qwen3.5:4b** plans and reads. Its plan names a control by text
  (`do_engine.Step.target`). Perception resolves that text, and its
  candidates come from accessibility, OCR or GoClick behind the gate. When
  GoClick is absent, the numbered-box pick asks the model which of several
  known accessibility rectangles it means, never for a coordinate.
- **GoClick-Large** proposes a point and nothing else. It is used only when
  accessibility and OCR have not settled the target. In practice that means
  unlabelled controls and descriptions that match no published name.

## Known limits, stated plainly

- **No accessibility, no click.** Every click needs an element under the
  point. A canvas, a game, or an Electron app that publishes one big group
  cannot be clicked at all, however confident GoClick is. That is deliberate,
  and it means some real interfaces are out of reach.
- **Accessibility is trusted.** An application that publishes a false label
  defeats the label check. Nothing on the machine can know better than the
  application itself.
- **Names must match exactly.** A target described by function ("the button
  that adds a row") is refused when the control is named "Add". Diablo then
  asks rather than guessing. It is safe, and sometimes it asks more than a
  person would.
- **A few milliseconds remain.** `revalidate` runs just before
  `_post_click`. The window between them is the time it takes to build two
  events. A window could appear in that gap; nothing in macOS closes it.
- **Only ordinary windows.** `screen_context.list_windows` keeps layer-0
  windows, so a floating panel is not a window Diablo can be asked to act in.
- **Displays.** The coordinate path was checked live on both displays of the
  test Mac: the 2x built-in panel at origin x = -1512, and a 1x external
  display at origin 0. Frame pixels resolved to the right control on each
  (`docs/benchmarks/final_model_gauntlet.md`). A third display, rotation,
  mirroring and non-integer scaling were not tested.
