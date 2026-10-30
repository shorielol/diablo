# Model gauntlet results: MAI-UI 2B and Fara 1.5 4B against Diablo's stack, 2026-10-05

## Verdict

**NO. I would not ship MAI-UI 2B + Fara 1.5 4B in place of Diablo's models
tomorrow.** Primary verdict: **D**. The pair cannot match the incumbent normal
stack, and the brief's premise needed correcting first: the 14B is not
Diablo's brain. It is an escalation model, reached on about 4% of real turns
(`14B_DEPENDENCY_MAP.md`).

The gauntlet did find one real improvement. **Fara 1.5 4B is the best
control grounder measured, better than the production Qwen2.5-VL 7B, in half
the memory.** That makes it a candidate for the vision slot, not the brain
slot.

Confidence: moderate. The corpora are small (25 grounding cases x 2, 11 regions,
59 planner cases, 39 intake cases x 3) and every screen is a Diablo fixture,
not a third-party app. Each conclusion below states the numbers it rests on.

## How it was run

- Models: MAI-UI-2B (Tongyi-MAI, Apache-2.0, Qwen3-VL base) and Fara1.5-4B
  (Microsoft, MIT, Qwen3.5 base, browser-trained). Each was tested in two runtimes:
  - MLX-VLM 0.7.4: MAI-UI at bf16, Fara at 8-bit;
  - Ollama 0.35.1: MAI-UI at Q8_0, Fara at Q4_K_M.
- Each model had its own official prompt, message order and coordinate
  convention, taken from its repository:
  - MAI-UI: `<answer>{"coordinate": [x, y]}</answer>` on 0..999, instruction
    before the image;
  - Fara: `computer_use` tool calls on 0..1000, image before the task. The
    model card says "absolute pixels", but Fara's own `coord_spaces.py` says
    0..1000.
- Every point goes image → (u, v) → `ScreenFrame.to_quartz` → the fixture's own
  control rectangles, and is judged by `run_perception_eval.judge`. The runtime
  was checked on synthetic images first: MAI-UI was within about 1% of truth.
- Brains ran through Diablo's own `local_brain` with the same prompts and
  schemas, selected by `DIABLO_MODEL` / `DIABLO_FAST_MODEL`.
- Nothing clicked anything. Fixture windows only; computer use stayed off.

## Fixture defect found (and fixed)

The first MAI-UI run missed the plainest case there is, the Save button. The
screenshot showed why: on this Mac, which is in dark mode, the "light"
fixtures drew their button and checkbox titles white on their own white
background.
- Accessibility still saw the controls, so it scored perfectly.
- OCR and every vision model were handed a picture with no Save button in it.

The fixtures (`controls_app.py`, `icon_app.py`, `target_app.py`) now pin
their appearance to the variant they claim. This is not a change in the
challengers' favour: it handicapped the incumbent's OCR and Qwen2.5-VL
equally, and every historical pixel-based perception number taken in dark
mode is affected. All incumbent numbers below were measured after the fix.

## Scorecard

Control grounding: 25 cases x 2 on fixtures. "Unsafe" means a point on the wrong control, or a point at all when the target is absent.

| Grounder | Good | Unsafe | Absent target declined | P50 / P95 | Resident |
|---|---|---|---|---|---|
| Qwen2.5-VL 7B, production free grounding | 48/50 | 1 | 4/4 | 3.0 / 9.8 s | 7.3 GB |
| Production fusion (accessibility + OCR + set-of-marks) | 41/50 | 0 | 4/4 | 0.19 / 0.63 s | none |
| **Fara 1.5 4B, Ollama Q4_K_M** | **50/50** | **0** | **4/4** | 4.5 / 5.4 s | **3.65 GB** |
| Fara 1.5 4B, MLX 8-bit | 44/50 | 2 | 4/4 | 8.4 / 10.2 s | 6.9 GB peak |
| MAI-UI 2B, Ollama Q8_0 | 44/50 | 6 | **0/4** | 3.4 / 3.7 s | 3.25 GB |
| MAI-UI 2B, MLX bf16 | 42/50 | 8 | **0/4** | 3.1 / 3.5 s | 5.4 GB peak |

Region and figure grounding: 11 regions on the diagram, market and chart fixtures.

| Path | Hits | Notes |
|---|---|---|
| Production `grounding.ground`, Qwen2.5-VL | 9/11 | equilibrium: a wrong box; arrow missed |
| Production `grounding.ground`, Fara as `DIABLO_VISION_MODEL` | 9/11 | equilibrium: declined honestly; arrow missed |
| Fara, own prompt | 9/11 | two points outside the diagram's surplus regions |
| MAI-UI, own prompt | 5/11 | |

The production region path is mostly a deterministic shape picker. Its IoU
values were identical across the two models on most regions.

Brain, through Diablo's own code:

| Brain | Planner advancing (59) | Regressions | Unsafe steps | Intake passed (117) | Unnecessary questions | False actions | Escalated | Planner P50 | Intake P50 |
|---|---|---|---|---|---|---|---|---|---|
| Qwen3 4B | **73%** | 6 | 1 | **107** | **4/60** | 3/57 | 12 | 1.6 s | 0.8 s |
| Fara 1.5 4B (Q4) | 64% (below the 70% floor) | 12 | 1 | 100 | 17/60 | **0/57** | 0 | 2.3 s | 1.1 s |
| Qwen3 14B | 75% | 3 | 1 | 96 | 18/60 | 3/57 | 9 | 4.9 s | 2.4 s |
| Qwen3 4B, no 14B escalation | n/a | n/a | n/a | 106 | 5/60 | 3/57 | 0 | n/a | 0.9 s |

- All three brains took the same single unsafe step, in a recovery case. Diablo's
  policy layer gates it.
- The 3 false actions are one case, "rename the document", acted on (or
  escalated) without a named target.
- Fara asks on every missing target (21/21, against the 4B's 15/21), and asks
  far too often otherwise.

## Capability matrix (measured here unless marked)

| Capability | Qwen3 4B | Fara 4B | Qwen2.5-VL 7B | MAI-UI 2B |
|---|---|---|---|---|
| General reasoning / intake | best: 107/117, 4 unnecessary questions | 100/117, 17 unnecessary | not tested | not a text model |
| Screen understanding (image questions) | n/a | **not tested** | **not tested** (production) | not tested |
| UI grounding, controls | n/a | **50/50**, 0 unsafe | 48/50, 1 unsafe | 44/50, 6 unsafe |
| Regions and figures | n/a | 9/11 (own and production paths) | 9/11 | 5/11 |
| Coordinate precision when correct | n/a | 1.2 px median | 1.3 px | 2.3 px |
| Saying "not there" | n/a | yes, 4/4 | yes, 4/4 | **no, 0/4** |
| Action planning | 73% | 64% | n/a | not run |
| State-dependent planning | 5/10 | 4/10 | n/a | n/a |
| Recovery planning | 3/7 | 3/7 | n/a | n/a |
| Safety-category planning | 7/7 | 7/7 | n/a | n/a |
| Ambiguity | asks on 15/21 missing targets | asks on 21/21, but asks too often | n/a | cannot abstain |
| Latency | fastest | moderate | slowest P95 (9.8 s) | fast |
| Memory (resident) | 3.86 GB | 3.65 GB | 7.3 GB | 3.25 GB |

## The configurations

| Config | Stack | Result |
|---|---|---|
| A, control | 4B + Qwen2.5-VL + 14B escalation | the reference above |
| B | Fara brain + Qwen2.5-VL + 14B | **rejected**: planner below its floor (64%), twice the regressions, four times the unnecessary questions |
| C | 4B + MAI-UI + 14B | **rejected**: MAI-UI never declines an absent target; 6 of 50 grounding answers would be wrong clicks |
| D | Fara + MAI-UI + 14B | **rejected**: both of the above |
| E | Fara + MAI-UI, no 14B | **rejected**: D fails before escalation matters. Fara escalated 0 of 117 because it asks the user instead, including "what does this error mean", which should be answered by looking |
| F, Fara only | Fara as brain and eyes | rejected as a brain; its grounding is the best measured |
| F, MAI-UI only | MAI-UI as eyes | rejected (cannot abstain) |
| Not in the brief | **4B brain + Fara as `DIABLO_VISION_MODEL` + 14B** | **the candidate**: grounding 50/50 with 0 unsafe; regions 9/11; half the vision model's memory; cold load 3.4 s against 5.4 s; image questions not yet tested |

## Escalation (sections 10 and 11)

- **Real use:** 9 of 206 real turns escalated (4.4%), median 95 s to finish.
- **Intake eval:** the 4B escalated 12 of 117 runs. Those were "where is my
  notes app", "what have I been working on", "what does this error mean"
  (3 runs each) and "rename the document".
- **With escalation disabled:** the 4B acted on them itself, and every intake
  measure stayed within one case (106 against 107; false actions unchanged).
  At the decision layer the 14B changes who does the work, not whether it is
  safe.
- **The class where the 14B is measurably better:** state-dependent planning
  (9 of 10 against the 4B's 5 of 10), with half the regressions overall. It
  is the DO engine's planner, which has no production caller while computer
  use is off.
- **Not measured:** whether the 14B executes the escalated real tasks better
  than the 4B would. That needs end-to-end runs.

## Model disagreement (section 12)

On the 25 grounding cases, the three vision models agreed with production
fusion wherever fusion had an answer. A rule that acts only when a vision
model lands on the control fusion found, and otherwise looks again or asks,
would have let through 0 of MAI-UI's wrong clicks.

Agreement between models is not evidence. On the unlabelled email field,
Qwen2.5-VL, MAI-UI and Fara (through MLX) all chose the name field above it.
They share a failure mode. The policy that follows:

- accessibility and OCR geometry is the authority for controls;
- a model's point is accepted only inside a control that geometry can name;
- a disagreement is a reason to look again or ask, never to vote.

## Also found

- **Runtime matters as much as the weights.** The same Fara scored 44/50 at
  8-bit through MLX and 50/50 at Q4 through Ollama.
- **A single unreproduced event:** once, under memory pressure (MAI-UI's server
  and Qwen2.5-VL resident together with the owner's apps), accessibility queries
  failed on most fixture controls. Two controlled reruns did not reproduce it.
  Cause not confirmed.
- **Two hidden defaults** would make the 14B the main brain of a fresh install:
  `local_brain.CHAT_MODEL` and `tools/install.sh`.

## Decision tree (section 17)

1. Can MAI-UI 2B replace Qwen2.5-VL 7B? **NO.** It cannot say a target is
   absent, and it was less accurate in both runtimes.
2. Can Fara 1.5 4B replace Qwen3 4B as the brain? **NO.** Its planner falls
   below the 70% floor, and it asks four times as many unnecessary questions.
3. Can Fara + MAI-UI replace the normal stack? **NO.**
4. Can Fara + MAI-UI operate safely without the 14B? **NO.** The pair fails
   on its own merits first.
5. Which task classes need the 14B? On this evidence, state-dependent planning
   for the DO engine, which is off in production. No intake decision changed
   without it.
6. The smallest architecture I would ship tomorrow: **the current one** (4B +
   Qwen2.5-VL 7B, with 14B escalation). The smallest I would trial next:
   **4B + Fara 1.5 4B as the vision model**, which saves 3.6 GB and gave the
   best grounding measured.

## Not tested, and why

- Image questions: reading an error, a table, the IB statistics screenshot.
  This is the one job Qwen2.5-VL does that Fara has not been measured on, and
  it is why Qwen2.5-VL is not replaced today.
- The set-of-marks vision pick with Fara.
- Whether the 4B and Fara stay resident together without a swap.
- Live multi-step and long-horizon tasks.
- Multiple displays: only one display is connected.
- Real third-party apps, and any live clicking (policy: fixtures only).

## Cleanup

Two models were added to the owner's Ollama store and can be removed with
`ollama rm`:
- `hf.co/bartowski/Fara1.5-4B-GGUF:Q4_K_M` (3.6 GB);
- `hf.co/mradermacher/MAI-UI-2B-GGUF:Q8_0` (2.3 GB).

The MLX environment and its 8.8 GB of downloads live in the session's
temporary folder.
