# Diablo's model architecture

Decided 2026-10-05. Diablo runs one general model and one specialist.
Everything that decides whether something happens on the screen is ordinary
code.

| Part | Is | Does | Never |
|---|---|---|---|
| **qwen3.5:4b** (Ollama, Q4_K_M, 4.2B, 3.3 GB on disk) | the only general model | understands requests, plans, converses, reads text, tables, charts and diagrams in captures, chooses the next step of a task | supplies a coordinate that is clicked; decides permission |
| **GoClick-Large** (0.8B, Florence-2, MLX, fp16) | the grounding specialist | proposes where a described control is, as a point in a capture | plans, names what it pointed at, or presses anything |
| **Diablo** (accessibility, OCR, `ScreenFrame`, `grounding_gate`, `computer_use`, `do_engine`) | deterministic | finds controls, validates every point, asks permission, acts, verifies, recovers | trusts a model's label, confidence or agreement |

There is no escalation model, no separate vision model and no second
opinion. The previous stack was Qwen3 4B, Qwen2.5-VL 7B for vision and
Qwen3 14B for escalation. It needed up to 21.5 GB to hold all three, and in
practice swapped models in and out of an 18 GB machine.

## Model routing

One route. `local_brain.CHAT_MODEL` resolves once, from `DIABLO_MODEL` or the
default in `local_brain.DEFAULT_BRAIN_MODEL`, and every model call goes
through `local_brain` to Ollama. An image question is the same call with an
image attached. GoClick is reached only through `perception._from_grounder`,
and only when the cheaper sources have not settled the target.

```
request ─► route.classify (qwen3.5) ─► chat │ act │ clarify
                                         │
                                         act ─► agent_core (tools) ─► answer
                                         │
                       computer use ─► do_engine (qwen3.5 plans one step)
                                         ─► computer_use.act_on ─► perception ─► gate ─► click ─► verify
```

## Context

`num_ctx` is sent with every request: 32,768 by default (`DIABLO_NUM_CTX`),
not baked into a custom Ollama model. Requests carry `truncate: false`. At
8,192, a 12,539-token prompt was not refused; Ollama silently kept 4,098
tokens and answered what was left. Now an overflow is an error, reported as
`ContextFull`, never a shorter question.

Measured for qwen3.5:4b on this M3 Pro, with the same 2,736-token agent turn:

| `num_ctx` | resident | P50 | P95 |
|---|---|---|---|
| 8,192 | 3.41 GB | 1.28 s | 1.33 s |
| 16,384 | 3.71 GB | 1.31 s | 1.33 s |
| 32,768 | 4.27 GB | 0.94 s | 0.99 s |

Only one layer in four keeps a full KV cache, so the larger window costs
0.86 GB, not the 2 GB or more it would cost a plain transformer.

## Prompts

The agent's first turn is 2,736 tokens on qwen3.5: a 2,846-character system
prompt plus the tools `agent_tools.select` chooses for the request (15 for a
typical one). Sending all 48 tool schemas would be 5,242 tokens. The
conversation prompt is 722 tokens. The roughly 8,200-token prompt mentioned
when this work began did not reproduce in either the repository or the live
install.

Prompt changes in this build:

- **Planner prompt.** It said both "name a control the way a person would
  say it: 'the Save button'" and "copied from the list of controls". It now
  says to copy the name. It also now says that `type` goes to the focused
  control, so a field has to be clicked first, and to check whether the goal
  already holds before doing anything.
- **Planner schema.** `why` is now generated before `verb`. Before, qwen3.5
  committed to "wait" and then wrote "the goal is already satisfied".
- **Stale tool wording.** The "local vision model" and "extra model" phrasing
  in `describe_visually` is gone.

Planner benchmark, 59 goals run twice each:

| Planner | advancing | invalid | regressing |
|---|---|---|---|
| qwen3.5, old prompt | 63% | 20% | 12% |
| qwen3.5, clarified prompt | 65% | 11% | 15% |
| qwen3.5, `why` first | **75%** | **8%** | **9%** |
| qwen3:4b (the old brain), for reference | 73% | – | – |

The thresholds are a 70% floor and 10% ceilings. Guards in `point_at`'s
description and elsewhere were measured into existence and were not trimmed
for tokens.

## Perception pipeline

`perception.locate(intent, window)` asks its sources in order of cost, and
stops as soon as the answer is decisive:

1. **Accessibility** (about 80 ms): the application's own controls, with
   role, label, enabled state and exact bounds.
2. **OCR** (about 1 s), only if accessibility was not decisive: the words
   actually painted.
3. **GoClick behind the gate**, only if both left it unsettled. The
   candidate is the accessibility element under GoClick's point: its
   rectangle, its label. If GoClick is not installed, qwen3.5 picks from
   numbered accessibility rectangles instead. Never both.

Arbitration (`perception.read`) turns candidates into one of five verdicts.
Ambiguity and low confidence become questions, not clicks.

**Deterministic fast path.** A request that names a published label ("click
Save") is settled by accessibility at confidence 0.97 or more and never pays
for OCR, a model or GoClick. GoClick's cost is paid only where it earns it.

## Grounding pipeline

```
capture (ScreenFrame + the window's published controls, together: grounding_gate.observe)
   ─► GoClick worker: "Where is the <target> element?" ─► "(<loc_x>,<loc_y>)"
   ─► GroundingProposal(frame_id, FRAME_NORMALISED, x, y, target, source_model)
   ─► grounding_gate.validate ─► ValidatedTarget | CONFIRM_REQUIRED | REJECT_*
```

GoClick runs in its own process (`goclick.serve`). It starts on first use
and stops after `DIABLO_GROUNDER_KEEP_ALIVE` seconds idle (600 by default).
The model code is MLX-VLM's Florence-2 implementation, so nothing from the
model's repository is executed. Weights are the pinned revision's
safetensors, checked by sha256 at install.

Diablo owns the decoder. GoClick's generation config includes
`no_repeat_ngram_size=3`, and without it three answers in four were an
endless run of `<s>`. With it, the MLX output matched the reference PyTorch
implementation token for token on every case measured.

## Validation

`grounding_gate.validate` resolves a point to the control macOS says is
frontmost there. It refuses on any of these: coordinate space, staleness,
wrong window, gone/moved/changed, role, label, disabled, ambiguous. See
`security_boundaries.md` for each check, where it is enforced and the test
that proves it.

## Action

- `computer_use.act_on(intent)` is the one action path the planner uses: perceive, decide, ask permission, perceive again, check the frontmost control at the point, post, verify.
- `computer_use.click(target, frame)` accepts only a sealed `ValidatedTarget`, and revalidates immediately before posting.
- Computer use is off unless `DIABLO_COMPUTER_USE=1`.
- Permission is per click or per task, and expires.
- Destructive controls, and controls with no name, need their own yes every time.
- `computer_use.type_text` types only when a text field has keyboard focus
  (`ax.focused_element`), and refuses with `no_field` otherwise. A focused
  button would take a space as a press.
- A planned `type` step that names exactly one text field ("type 7" into
  "Quantity") clicks that field first through `act_on`, with every gate in
  place, then types. In the gauntlet the planner kept forgetting the click.

## Verification

After every click, the window is captured again and compared with the
capture before it. The outcome is one of five:

- `confirmed`: the expected text appeared;
- `unverifiable`: something changed, but nothing proves what;
- `suspected_noop`: nothing readable changed;
- `partial`: only part of the input was sent;
- `refused`: nothing was sent, with a code saying which gate stopped it.

A click is never reported as working because no exception was raised.

Some clicks change no words: focusing a text field, ticking a checkbox. For
those, `act_on` asks accessibility about the control it clicked
(`computer_use._state_change`). A click is confirmed when the field now has
the keyboard focus, or when the checkbox's value differs from the value read
by the last check before the click. Before this, both counted as no-ops. That
put "Quantity" on the do-not-press-again list, which then blocked focusing the
same field on the next order.

Typed text is read back from the focused field. It is confirmed only when the
field holds exactly what was typed. Otherwise the record says what the field
actually reads. Unconfirmed typing once made the planner type "3" a second
time, and "33" was saved.

The planner's view of the screen includes what each text field contains and
whether each checkbox is on, and "Already done" carries each step's outcome
(confirmed, sent but not confirmed, failed).

A checkbox or radio button wider than it is tall is pressed at its box,
the leading square of its frame. AppKit ignores a click on the empty part of
the frame, and the centre of a 160-point "Priority" checkbox did nothing.

## Failure recovery

The DO engine (`do_engine.run`) holds explicit task state: the goal, a
`TaskState` machine, every planned `Step`, and every `ActionRecord` with its
expected text, what was observed and the outcome. It also keeps a timeline
and a count of recoveries.

After each step it looks again, checks the goal, and plans the next step from
what is on screen now. The limits are hard:

- 12 steps (`DIABLO_TASK_STEPS`);
- 3 consecutive waits (`DIABLO_TASK_WAITS`);
- a runtime limit;
- no automatic retry of a click (`MAX_RETRIES = 0`), because a second click
  on a toggle undoes the first;
- an `avoid` list that refuses pressing, again, a control that already failed
  to produce the result.

A task that cannot proceed ends `BLOCKED` with a plain reason, or
`WAITING_FOR_USER` with a question. It never loops.

## Memory

| | resident |
|---|---|
| qwen3.5:4b at 32K context | 4.27 GB |
| GoClick worker, fp16, while warm | 1.66 GB active; 3.99 GB peak while loading, because the fp32 weights are cast in memory |
| total while both are warm | about 6 GB |
| the previous stack: 4B + Qwen2.5-VL 7B (+ 14B on escalation) | 3.86 + 7.3 (+ 10.3) GB, swapped in and out |

Brain and vision are one model now, so a vision question no longer evicts the
brain. The old stack paid a 5.6 s swap in and 1.7 s back for every image
question.

## Latency

Measured on this machine, M3 Pro, 18 GB:

| | |
|---|---|
| qwen3.5 agent turn (2,736-token prompt), warm | P50 0.94 s, P95 0.99 s |
| qwen3.5 cold load | about 12 s, which keep-alive (10 min) and the wake word hide |
| qwen3.5 answering a question about an image | about 5 s |
| GoClick worker start | 4.7 s, once per keep-alive period |
| GoClick, one proposal, warm | 0.6 to 1.1 s |
| accessibility lookup | about 80 ms |

The full end-to-end and per-path figures are in
`docs/benchmarks/final_model_gauntlet.md`.

## How the 14B was removed

1. The fresh-install default (`qwen3:14b` in `local_brain`, echoed by doctor
   and the installer) became `qwen3.5:4b`. There is now one constant, which
   the installer reads.
2. The escalation route, the model swap (`with_model`, `unload`,
   `resident`), the delegation caller and the settings dial were deleted.
3. Measured before removal: turning escalation off changed 1 intake decision
   in 117 (106 against 107 passing) and no false action. The one place the
   14B helped, state-dependent planning, is the DO planner. That planner now
   meets its thresholds on qwen3.5 (75% advancing).
4. `tests/test_default_model.py` fails if any default, fallback or `getenv`
   in the runtime names a 14B, Qwen2.5-VL or Fara again, or reads
   `DIABLO_ESCALATE_MODEL` or `DIABLO_VISION_MODEL`.

## Installation

- `tools/install.sh` resolves and, if missing, pulls the brain model.
- `tools/install.sh --check` resolves it and pulls nothing.
- `tools/install.sh --grounder` fetches GoClick and installs `mlx-vlm==0.7.4`. Its weights are about 3.3 GB on disk.
- `diablo doctor` reports the brain model, its context, and whether GoClick and its runtime are present.

The full installer still predates the current layout, as the README says;
these three modes work on their own.
