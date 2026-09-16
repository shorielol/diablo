# Diablo — runtime connectivity audit

**Build 1.5 · read-only · 2026-09-16 · commit `b58a7c0`**

No production code was modified. No live application was interacted with.

> **Documents named in the brief that do not exist:** `diablo_overhaul.md`,
> `work_assignments.md`, `agent_coordination.md`. `docs/architecture/` contains
> only `diablo_overhaul_audit.md` and `docs/commands.md`. The repository was
> used as the source of truth, as the brief also directs.

---

## 0. The methodological finding that governs everything below

The brief warns that a Graphify edge does not prove runtime connectivity. On
this repository the **inverse** turned out to be the larger problem:

> **Absence of a Graphify edge does not prove disconnection.**

Graphify's module graph reports `agent_tools.py`, `route.py`,
`screen_context.py`, `trace.py`, `config.py`, `persona.py`, `display.py` and
`pointing.py` as **isolated** — zero cross-module edges. All eight are core
production modules. `config.py` alone has 23 production importers.

The cause is specific and repo-wide. The repository ships `diablo/` and the
installer copies it to `~/.diablo/skills/`, so every internal import is written
`from skills import x`. Graphify's AST pass emits an edge to a node named
`skills`, which does not exist in the corpus, and the build drops it.
`delegation_tool.py` contains eight such imports and contributes **zero**
surviving import edges.

| | cross-module pairs |
|---|---:|
| Graphify, as built | 82 |
| Resolved (`skills.x` → `diablo/x.py`) | 248 static + 18 dynamic |

Roughly **70% of this codebase's static connectivity is invisible to the graph.**
The 686 "dangling edges" in the graph health check are the same phenomenon seen
from the other end: 111 of them point at `skills`, the rest at the standard
library.

Everything in this audit therefore comes from resolved static imports plus
source-level execution tracing. Graphify was used for discovery — community
structure, god nodes, hub identification — and never as connectivity evidence.

---

## 1. Graph A — static dependency findings

Resolved import graph over 124 modules.

### Consumers by caller kind

| Status | Count | Modules |
|---|---:|---|
| PRODUCTION | 41 | the live system |
| TEST/EVAL ONLY | 1 | `do_engine.py` |
| NO IMPORT CONSUMER | 10 | see below |

The ten with no import consumer split into three genuinely different cases, and
conflating them is how a healthy module gets called dead:

| Module | Actual reachability |
|---|---|
| `bench.py`, `doctor.py`, `make_app.py`, `trace.py`* | **CLI entry points** — `~/bin/diablo` runs them by path |
| `overlay.py` | **Subprocess** — `pointing.show()` does `Popen([sys.executable, OVERLAY, ...])` |
| `ocr_tool.py` | **Retired** — its own docstring says "Retired. Use screen_context.look()" |
| `cache_audio.py`, `calendar_tool.py`, `instagram_tool.py`, `llm_tool.py`, `personality.py` | **No reachability found by any mechanism** |

\* `trace.py` is both a CLI entry point and imported by five production modules.

### God nodes (from Graphify, and they are not what you would hope)

```
1. Results       58 edges     tests/harness.py
2. guard()       51           tests/harness.py
3. _failed()     38           diablo/agent_tools.py
4. _skill()      31           diablo/agent_tools.py
5. CompanionManager 22        companion_manager.py
```

The two most connected nodes in the repository are the test harness. `Results`
bridges sixteen communities; no production module bridges more than two. That is
a real structural statement: **the test suite is the most densely connected
subsystem in Diablo**, and the production modules are comparatively solitary.

### Circular dependencies

One, handled deliberately: `perception` ↔ `pointing`. `pointing.point_at`
imports `perception` inside the function body with a comment saying an
import-time cycle "would take the whole assistant down rather than just this
feature." Not a defect.

---

## 2. Graph B — runtime execution findings

Traced in source from the entry point. Line references are `b58a7c0`.

### Path A — a normal question · **CLOSED**

```
voice_bridge.process_command
  → delegation_tool.handle                    delegation_tool.py:377
  → trace.new_turn("voice")
  → route.classify                            delegation_tool.py:399
  → conversation.talk                         delegation_tool.py:440
  → voice.speak → audio callback → DiabloState.RESPONDING
```

### Path B — inspect the screen · **CLOSED**

```
route.classify (needs_screen via screen_context.wants_screen)
  → conversation.look_if_asked
  → screen_context.look → capture_window → _ocr_fragments (Vision framework)
  → text into the conversation turn
```

### Path C — "show me how" · **CLOSED, with a one-way segment**

```
delegation_tool.handle
  → tutor.wants_teaching                      delegation_tool.py:417
  → tutor.walk(text, speak, ask, working)
  → session.record_task("vassago", ...)
```

### Path D — "do this" · **THIS IS THE CRITICAL PATH**

```
delegation_tool.handle
  → route.classify                 → Decision(route="act")
  → decide(task, operations, said) → RoutingDecision
  → _run_inline                                delegation_tool.py:548
  → agent_core.run(task, speak, ask, confirm)  delegation_tool.py:567
  → agent_tools.select / schemas
  → loop ≤ 14 steps:
        local_brain.chat(messages, tools)
        7 answer guards
        agent_tools.execute(name, args)        agent_core.py:708
        messages.append({"role":"tool", ...})  agent_core.py:728
  → spoken summary
```

`_run_inline` takes no goal, constructs no task record, and returns a string.

### Paths E–J

| Path | Status |
|---|---|
| E. Perception → candidate | **REAL**, via `point_at` tool → `pointing.point_at` → `perception.read` |
| F. Planner → step | **TEST-ONLY** — `do_engine.model_planner` has no production caller |
| G. Action executed | **DISCONNECTED** — `computer_use.act_on` is called from `do_engine.py:605` only |
| H. Verification of an action | **TEST-ONLY** |
| I. Verification fails | **TEST-ONLY** |
| J. Recovery | **TEST-ONLY** |

### Graphify says vs actual runtime

```
GRAPHIFY SAYS:   agent_tools.py is an isolated node
ACTUAL RUNTIME:  agent_core.run → agent_tools.execute → 48 tools
THEREFORE:       graphify's isolation = ARTEFACT of the skills/ namespace

GRAPHIFY SAYS:   computer_use ← do_engine  (an edge exists)
ACTUAL RUNTIME:  nothing calls do_engine.run
THEREFORE:       that edge = IMPORT-ONLY, and the path behind it is dead

GRAPHIFY SAYS:   perception ← computer_use, perception ← pointing
ACTUAL RUNTIME:  only pointing reaches perception in production
THEREFORE:       perception ← computer_use = IMPORT-ONLY
                 perception ← pointing     = REAL RUNTIME

GRAPHIFY SAYS:   overlay.py is isolated
ACTUAL RUNTIME:  pointing.show() spawns it as a subprocess
THEREFORE:       REAL RUNTIME, across a process boundary graphify cannot see
```

---

## 3. Graph C — agent capability graph

| Edge | Status | Evidence |
|---|---|---|
| USER INTENT → CONTEXT | PARTIALLY CONNECTED | `route.classify` + ad-hoc screen attach; no Context owner |
| CONTEXT → GOAL | DISCONNECTED | no production code constructs `do_engine.Goal` |
| GOAL → PLAN | TEST-ONLY | `do_engine.model_planner` |
| INTENT → PLAN (task) | CONNECTED | `agent_core.plan`, prompt-level only |
| PLAN → OBSERVATION | DISCONNECTED | the task plan is never re-read after step 1 |
| OBSERVATION → PERCEPTION | CONNECTED | `perception.locate` |
| PERCEPTION → TARGET | CONNECTED | `Candidate` / `Reading` |
| **TARGET → DECISION** | **TEST-ONLY** | `computer_use.decide` unreached in production |
| DECISION → POLICY | TEST-ONLY | `computer_use.risk_of` |
| POLICY → PERMISSION | TEST-ONLY | `Grant`; the live gate is `CONTEXT["confirm"]`, a different mechanism |
| **PERMISSION → ACTION** | **DISCONNECTED** | no click/type/press tool exists |
| ACTION → NEW OBSERVATION | TEST-ONLY | |
| NEW OBSERVATION → VERIFICATION | TEST-ONLY | |
| VERIFICATION → RECOVERY | TEST-ONLY | |
| RECOVERY → NEXT PLAN | TEST-ONLY | |
| MEMORY → CONTEXT | PARTIALLY CONNECTED | `session.turns` in RAM only |

---

## 4. Connectivity matrix

Machine-readable: `docs/architecture/runtime_connectivity.json`.

| Capability | Production entry | Production consumer | Feedback path | Status |
|---|---|---|---|---|
| Intent | `route.classify` | `delegation_tool.handle` | yes | **CONNECTED** |
| Conversation | `conversation.talk` | voice_bridge, server | yes | **CONNECTED** |
| Context | `_system_prompt` / `look_if_asked` | `agent_core.run` | no owner | PARTIAL |
| Memory | `conversation.Session` | conversation, roster | RAM only | PARTIAL |
| Task state | `do_engine.TaskState` | **NONE** | no | TEST-ONLY |
| Goal | `do_engine.Goal` | **NONE** | no | TEST-ONLY |
| Planner (task) | `agent_core.plan` | `agent_core.run` | never revisited | PARTIAL |
| Planner (action) | `do_engine.model_planner` | **NONE** | no | TEST-ONLY |
| Tool selection | `agent_tools.select` | `agent_core.run` | yes | **CONNECTED** |
| Tool execution | `agent_tools.execute` | `agent_core.run` | yes | **CONNECTED** |
| Skills | `skill_registry` | dynamic `_skill()` | n/a | PLANNED |
| ScreenFrame | `screen.capture_*` | screen_context, perception, agent_tools | yes | **CONNECTED** |
| Accessibility | `ax.elements_for_window` | `perception._from_accessibility` | yes | **CONNECTED** |
| OCR | `_ocr_fragments` | `pointing.locate`, `screen_context.look` | yes | **CONNECTED** |
| Vision | `perception._from_vision` | `perception.locate` | yes | **CONNECTED** |
| Candidate / Fusion | `perception._fuse` | `perception.locate` | yes | **CONNECTED** |
| Policy | `computer_use.risk_of` | **NONE** | no | TEST-ONLY |
| Permission | `computer_use.Grant` | **NONE** | no | TEST-ONLY |
| Computer use | `computer_use.act_on` | `state()` only | no | **DISCONNECTED** |
| Action executor | `_post_click` | **NONE** | no | **DISCONNECTED** |
| Verification (action) | `do_engine.classify` | **NONE** | no | TEST-ONLY |
| Verification (answer) | 7 guards in `agent_core.run` | `agent_core.run` | yes | **CONNECTED** |
| Recovery | `do_engine` (11 classes) | **NONE** | no | TEST-ONLY |
| Guide | `pointing.point_at` | `agent_core.run` | string only | PARTIAL |
| Voice | `voice.speak` | voice_bridge, manager | yes | **CONNECTED** |
| Trace | `trace.event` | 5 modules | 59% unattributed | PARTIAL |
| Missions | — | — | — | PLANNED |

**12 CONNECTED · 5 PARTIAL · 8 TEST-ONLY · 2 DISCONNECTED · 2 PLANNED**

---

## 5. Architectural islands

### Island 1 — the live assistant (CONNECTED)
`voice_bridge` · `delegation_tool` · `route` · `agent_core` · `agent_tools` ·
`conversation` · `roster` · `local_brain` · `persona` · `trace`
Closed loop for conversation and tools. This is the working product.

### Island 2 — perception and guidance (CONNECTED, one-way at the exit)
`screen` · `screen_context` · `display` · `ax` · `perception` · `pointing` ·
`guidance` · `overlay`
Reached from Island 1 through exactly one door: the `point_at` tool. Information
flows in richly and leaves as an English sentence.

### Island 3 — the action layer (ISLAND, deliberately)
`computer_use` · `do_engine`
1,373 lines, 98 tests, no production caller for the execution path.
**This separation is intentional**, not accidental: `ENABLED` defaults off,
`golden_path.py` is the sanctioned harness, and the standing project rule
forbids clicks in live applications. It is a quarantine, and it is holding.

### Island 4 — measurement (ISLAND, correctly)
`evals/run_eval.py` · `run_planner_eval.py` · `run_perception_eval.py`
Should not be production-connected.

### Island 5 — unreachable
`cache_audio` · `calendar_tool` · `instagram_tool` · `llm_tool` · `personality` ·
`ocr_tool` (retired)

---

## 6. Broken loops

| Loop | Present | Missing |
|---|---|---|
| INTENT → PLAN | yes (`agent_core.plan`) | **PLAN → OBSERVATION** — the plan is prompt text, never re-read |
| PLAN → ACTION | **no action exists** | the whole edge |
| ACTION → VERIFICATION | test-only | production action |
| VERIFICATION → PLAN | test-only | production verification |
| FAILURE → RECOVERY | test-only | production failure signal |
| PERCEPTION → TARGET | yes | **TARGET → ACTION** — target becomes a sentence |
| GUIDE → USER | yes | **USER RESULT → GUIDANCE STATE** — `show()` is `Popen` + `return True` |
| MEMORY → CONTEXT | partial | persistence; nothing survives restart |
| MISSION → TASK | no mission layer | everything |

---

## 7. Duplicate and unsynchronised state

| Model | Where | Live | Verdict |
|---|---|---|---|
| `DiabloState` (9) | `companion_manager.py:91` | yes | **(A)** genuinely distinct — attention/voice lifecycle |
| `JobStore` status (4 strings) | `delegation_tool.py:53` | yes | **(A)** distinct — delegated work, persisted |
| `TaskState` (14) | `do_engine.py:51` | no | **(E)** dead |
| `ActionState` (8) | `do_engine.py:72` | no | **(E)** dead |
| `Session` | `conversation.py:230` | yes | **(A)** distinct, RAM only |
| `Grant` | `computer_use.py:75` | no | **(B)** duplicates `CONTEXT["confirm"]`'s job with a different model |

`DiabloState` and `JobStore` are **(D) not synchronised**: `delegation_tool`
never calls `manager.transition`, so a job can read `running` while the
assistant reads `idle`. Both are correct about their own concern; nothing
reconciles them. `JobStore` is also memory-authoritative — after a restart a row
still says `running` with no thread behind it.

---

## 8. Representation breaks

**R1 — the guidance exit, and the most consequential one.**
`perception.read` produces a `Candidate` carrying label, role, box, confidence,
`sources`, `evidence`, `enabled` and `vision` evidence. `pointing.point_at`
converts it to `"Top right of the toolbar, marked — 'Save'."` and returns that
string into the tool loop. Everything structured is discarded at the boundary.
The agent that asked cannot ask how sure the answer was.

**R2 — two capture APIs, one gated.**
The vision source uses `screen.capture_this_window()` → `ScreenFrame` →
`_measure()` → `validity()`. The OCR source uses
`screen_context.capture_window()` raw (`pointing.py:163`), with no `ScreenFrame`
and no `validity()` call. A window on another Space captures as well-formed
solid black; vision rejects it, OCR reports "no text found", and both feed the
same fusion. The blank-frame gate exists in two places and not on the live OCR
candidate path.

**R3 — tool results are untyped strings.**
`agent_core.py:728` appends `{"role":"tool", "content": result}` where `result`
is prose. Success is `not result.startswith(("Error","Refused","Cancelled"))`.

**R4 — untrusted screen and web text enters unmarked** (carried from the Build 1
audit, unchanged). Same line, `agent_core.py:728`.

**R5 — permission is expressed twice.** `CONTEXT["confirm"]` is a thread-local
callback returning a bool. `computer_use.Grant` is a dataclass with scope and a
600-second expiry. They model the same idea and never meet.

---

## 9. The minimum real end-to-end agent loop

```
wake word / push-to-talk / POST /api/say
  → voice_bridge.process_command
  → delegation_tool.handle          ── trace.new_turn
  → route.classify                  ── Decision{kind, confidence, risk}
  → decide()                        ── inline or delegate
  → agent_core.run
       ├── agent_tools.select
       └── ≤14 × [ local_brain.chat → 7 guards → agent_tools.execute ]
  → spoken summary → voice.speak → audio → DiabloState.RESPONDING
```

That loop is **genuinely closed** for conversation, files, arithmetic, lookups
and screen *reading*. It verifies (seven guards), it retries, it asks when
uncertain, and the answer returns to the user through a state machine driven by
real audio.

---

## 10. Where the loop first breaks

> **`_run_inline` → `agent_core.run`, at [delegation_tool.py:567](../../diablo/delegation_tool.py#L567).**

`handle()` has, at that moment, a `Decision` carrying kind, confidence, risk,
`needs_screen` and `needs_tools`. It passes **four arguments** onward:
`task, speak, ask, confirm`. The Decision is dropped.

Downstream of that call there is no goal, so nothing can be verified as
achieved; no task record, so nothing can be recovered; and no action tool, so a
screen target cannot be acted on. Everything after it is a single-shot tool loop
that reports what it did rather than checking what happened.

The break is **not** that `computer_use` is disconnected — that quarantine is
deliberate and correct. The break is that the production path never constructs
the representation the rest of the architecture is built to consume.

### The missing connection

A production caller that turns a `Decision` into a `Goal` and runs a loop
against it. Concretely: `_run_inline` would pass `decision` into a task object
that `agent_core.run` reports into, so that "did this work?" has somewhere to be
asked. `do_engine` already implements exactly that shape. Nothing calls it.

---

## 11. Top 10 connectivity gaps

Ranked by architectural dependency, not by quality judgement.

**G1 — `Decision` is discarded at the inline boundary.**
Evidence: `delegation_tool.py:567`. Affects: goal, verification, recovery, task
state, context. Everything downstream is blind to confidence and risk that were
already computed. Minimum fix: thread `decision` into `agent_core.run` as an
optional argument.

**G2 — no production code constructs a `Goal`.**
Evidence: `do_engine.Goal` has zero non-test importers. Without a goal,
completion cannot be checked, only claimed.

**G3 — no action tool exists in the 48-tool menu.**
Evidence: no `click`, `type`, or `press` in `agent_tools.TOOLS`. `TARGET → ACTION`
cannot be traversed regardless of permission state. *(Closing this requires the
explicit authorization the safety rule reserves — see §13.)*

**G4 — perception's output is flattened to prose at the only production exit.**
Evidence: `pointing.py:305-312`. Confidence, sources and evidence are dropped
where a caller would need them most.

**G5 — the OCR candidate path bypasses the validity gate.**
Evidence: `pointing.py:163` vs `screen.py:214`. A black frame becomes "no text"
instead of a rejection, inside the fusion input.

**G6 — the task plan is never re-read.**
Evidence: `agent_core.plan` output goes into `_system_prompt` and is not
consulted again. `PLAN → OBSERVATION` has no mechanism.

**G7 — `JobStore` and `DiabloState` are unsynchronised, and `JobStore` is stale
after restart.** Evidence: no `manager.transition` call in `delegation_tool`;
`_flush` writes `running` rows with no reconciliation on load.

**G8 — permission is modelled twice and the models never meet.**
Evidence: `CONTEXT["confirm"]` vs `computer_use.Grant`. Connecting the action
layer later would introduce a second, differently-shaped consent path.

**G9 — 59% of trace events cannot be attributed.**
Evidence: measured on 3,072 real records; `roster.py:620` spawns threads without
`trace.carry()`. Any future loop debugging starts blind on delegated work.

**G10 — no persistence for conversation or task evidence.**
Evidence: `Session` is RAM; only `jobs.json` and `trace.jsonl` survive.
`MEMORY → CONTEXT` cannot span a restart.

---

## 12. Should Build 2 (Fusion) proceed?

**A prerequisite correction is required first, and it is small.**

Build 2 would change how `perception._fuse` and `AMBIGUOUS_WITHIN` arbitrate
between sources. The Build 1 baseline justifies that work: fusion measured 56%
top-1 against accessibility's 72%, and four of five regressions were false
ambiguity at confidence 0.93–1.00.

Two findings here bear on it directly:

1. **G5 — the OCR source feeds fusion through an ungated capture path.** Tuning
   the arbitration while one input can silently contribute "no text" from a
   black frame means tuning against a partly unknown input. This is a small,
   contained fix in `pointing.locate`.

2. **G4 — fusion's output is flattened to a string at the only production
   exit.** An improvement to fusion's confidence arithmetic cannot be observed
   by any production consumer, because no production consumer receives the
   confidence. Build 2 would be measurable by the benchmark and invisible to the
   running assistant.

Neither blocks the work. Both mean that without them, Build 2 improves a number
in `run_perception_eval.py` and changes nothing a user could experience.

**Recommendation: fix G5 first** (one capture call, inside the perception
pipeline, with the benchmark already in place to prove no regression), then
Build 2. G4 is the larger question and belongs with G1, since both are the same
problem — structured information collapsing at a boundary.

---

## 13. Safety boundary — unchanged

Nothing in this audit recommends connecting `computer_use` or `do_engine`. The
quarantine is correct and holding: `ENABLED` defaults off, the only live reach
is a read-only `state()` call from `screen_privacy`, and `golden_path.py` remains
the sanctioned interactive harness. G3 records that the action edge cannot be
traversed; it does not propose traversing it. Live applications remain
read-only, and real interaction still requires explicit future authorization.
