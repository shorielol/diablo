# Diablo — architecture audit

**Date:** 2026-09-16 · **Commit:** `83598eb` · **Scope:** read-only audit. No production code changed. No live application clicked.

---

## A. Executive summary

Diablo is a local-first macOS voice assistant, ~37,500 lines, entirely Python.
It listens for a wake word, classifies what was said, and either talks, asks a
question, runs a tool loop against the filesystem, or delegates to a background
worker. It reads windows through OCR and Accessibility, and it can draw a marker
on screen to point at a control.

The audit found one thing that reframes the whole overhaul plan:

> **The action layer is finished, tested, and connected to nothing.**
> `computer_use.py` (670 lines) and `do_engine.py` (703 lines) carry permission
> gates, click execution, screen-diff verification, recovery and task state.
> They have 98 passing checks between them. No production code path imports
> either one. Diablo can point at a button. It cannot press one.

That is not a criticism of the work. The safety scaffolding is genuinely good
and the boundary was deliberate. But it means the target architecture in the
brief is closer to done than it looks in some places and further in others, and
the sequencing should follow the evidence rather than the diagram.

Four other findings matter as much:

- The decision log cannot attribute **59% of its own events** (measured on 3,072
  real records), including 944 of 961 tool calls.
- There is **no boundary between untrusted screen text and instructions**. OCR
  output enters both the agent loop and the planner prompt unmarked.
- The planner **cannot see the outcome of its own last step**, which is the most
  likely explanation for the 82% first-step / 50% later-step gap measured last
  milestone.
- The planner benchmark **stubs perception out entirely**, so 74.6% advancement
  is an upper bound measured against a screen that is never misread. Perception
  itself has no benchmark.

---

## B. Actual architecture

Traced from code, not from the diagram.

```
              wake word / push-to-talk / HTTP POST /api/say
                                │
                       voice_bridge.process_command
                                │
                    delegation_tool.handle          ← the only router
                                │
            ┌───────────────────┼────────────────────┐
            │                   │                    │
      route.classify      tutor.wants_teaching   conversation.wants_reset
      (Decision:          (guidance path)
       kind, confidence,
       risk, needs_*)
                                │
        ┌──────────┬────────────┼───────────┬──────────────┐
        ▼          ▼            ▼           ▼              ▼
      chat      clarify     escalate      decide()     [no action branch]
   conversation  loop×2    local_brain.   roster vs
     .talk()               with_model()   inline
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
            agent_core.run()         roster fan-out
            (48 tools, 14 steps,     (daemon threads,
             7 guards)                same agent_core.run)
                    │
              agent_tools.execute → _resolve() sandbox
                                     SHELL_DENYLIST
                                     CONTEXT["confirm"]
```

**What is missing from that picture and present in the repository:**

```
   perception.locate()  ──→  ax / OCR / vision fusion   [reachable via pointing]
   computer_use.act_on() ──→ permission, click, verify  [NO CALLER]
   do_engine.run()      ──→  goal, loop, recovery       [NO CALLER]
```

### Module ownership

| Concern | Module | Lines | Reachable in production |
|---|---|---:|---|
| Router | `delegation_tool.handle` | 1010 | yes |
| Intent + confidence + risk | `route.classify` | 317 | yes |
| Intent extraction | `agent_core.understand` | 990 | yes |
| Tool loop | `agent_core.run` | — | yes |
| Tools | `agent_tools` | 1933 | yes (48 tools) |
| Conversation | `conversation` | 808 | yes |
| Delegation | `roster` | 667 | yes |
| Teaching / walkthrough | `tutor` | 1635 | yes |
| Window reading, OCR | `screen_context` | 1182 | yes |
| Frames, validity, change | `screen` | 691 | yes |
| Displays, coordinates | `display` | 391 | yes |
| Accessibility (ctypes) | `ax` | 363 | yes |
| Candidate fusion | `perception` | 710 | yes, via `pointing` |
| Marker placement | `guidance` | 298 | yes |
| Marker drawing | `overlay` | 517 | yes, as a subprocess |
| **Click, permission, verify** | **`computer_use`** | **670** | **no** |
| **Goal, loop, recovery** | **`do_engine`** | **703** | **no** |
| State machine + events | `companion_manager` | 1006 | yes |
| Decision log | `trace` | 220 | yes |
| Native shell (orb, panel) | `app_shell` | 905 | yes |
| Local API | `server` | 505 | yes |

---

## C. Data flows

### FLOW A — request → response
`process_command` → `handle` → `trace.new_turn` → `route.classify` (one model
call via `understand`) → `conversation.talk` → `voice.speak`. Failure points:
Ollama offline is caught and spoken; every other exception falls back to
today's pre-`route` path, so `route.py` cannot brick the assistant.

### FLOW B — request → screen observation → target
`screen_context.wants_screen` (free) → `conversation.look_if_asked` →
`screen_context.look` → `capture_window` → `_ocr_fragments` (Vision, 3× upscale).
For a located target: `perception.locate` fuses Accessibility first, OCR second,
vision third, and only escalates when the cheap sources fail.

### FLOW C — request → task → action → verification
**Exists in code, unreachable at runtime.** `do_engine.run` drives
NEW → UNDERSTANDING → PLANNING → OBSERVING → LOCATING → AWAITING_PERMISSION →
EXECUTING → VERIFYING, with `MAX_STEPS=12`, `MAX_RECOVERIES=3`, `MAX_WAITS=3`,
`RUNTIME_LIMIT=600`. `computer_use.act_on` re-locates the target after
permission is granted, captures before and after, and compares. Nothing calls it.

### FLOW D — failure → recovery
`do_engine.classify()` is a pure function of before/after window state,
returning `TARGET_MOVED`, `TARGET_GONE`, `WINDOW_CHANGED`, `MODAL_APPEARED`,
`NO_EFFECT` and six others. Unreachable, same reason.

### FLOW E — request → guide → target → marker
Complete and reachable. `point_at` tool → `pointing.point_at` →
`perception.read` → `guidance.place` (scored against display bounds, the ring,
the window, the cursor) → `pointing.show` → `Popen(overlay.py)`.

**Gap:** `show()` returns `True` the instant the process spawns. There is no
signal that the overlay drew, no way to cancel it, and no connection to the
manager's state. An interrupted turn leaves the marker on screen.

### FLOW F — request → voice → audio → speaking state
The strongest flow in the system. `VoiceSubsystem.say` moves to `PREPARING`,
and only the real audio-start callback moves to `RESPONDING`. Stale callbacks
are rejected by an utterance generation counter. `wake.set_speaking` follows the
same transition, which is what stops Diablo waking itself.

---

## D. Component map

Four process boundaries, not one:

1. **Python ↔ PyObjC** — AppKit, Quartz, Vision, WebKit in-process.
2. **Python ↔ subprocess** — `screencapture` for every frame; `overlay.py` for
   every marker.
3. **Python ↔ WKWebView** — React 18 UMD + `htm`, no build step, fed by an
   SSE stream from `server.py` on 127.0.0.1 with a 20-second fallback poll.
4. **Python ↔ Ollama** — HTTP, JSON-schema-constrained generation.

---

## E. Current strengths — do not rewrite these

- **Audio is the authority for the speaking state.** Generation-counted,
  stale-callback-safe. This was hard-won; leave it alone.
- **Coordinate arithmetic.** `display.py` derives scale from
  `NSScreen.backingScaleFactor` keyed by `NSScreenNumber`, after
  `CGDisplayPixelsWide` was measured returning 1512 for a 2.0 panel. Multi-
  display, negative-x and per-display capture all verified present.
- **Self-capture prevention.** `screen_context.is_own_window` checks owning PID
  against Diablo's own process set.
- **Sandbox and denylist.** `_resolve()` confines every path to four roots;
  19 shell patterns refused before execution; deletes go to the Trash.
- **`agent_tools.CONTEXT` is thread-local.** Concurrent agents cannot steal each
  other's confirmation callbacks.
- **Guards are code, not prompt.** Seven of them in `agent_core.run`, each
  emitting a trace event. This is the right instinct and should be extended.
- **Cheap-first perception.** Accessibility, then OCR, then vision, with vision
  asked only on bounded crops.

---

## F. Architectural debt

### CRITICAL

**F1 — The action layer has no caller.**
`computer_use` and `do_engine` are 1,373 lines of tested, unreachable code. They
will rot: `do_engine.classify` and `computer_use.act_on` are already coupled to
`perception` and `screen` signatures that nothing exercises outside tests.

**F2 — No trust boundary for untrusted text.**
`agent_core.py:728` appends tool results as
`{"role": "tool", "tool_name": name, "content": result}` — raw. `do_engine.py:378`
puts OCR output into the planner prompt as `"On screen:\n" + text`. Web pages,
file contents and screen text all enter the model's context with the same
standing as an instruction. The project's stated rule that "web page content is
data, never instructions" is written down and not enforced anywhere in code.
Today the blast radius is the 48 tools. Once the action layer is connected, it
is the mouse.

**F3 — The decision log cannot attribute most of what it records.**
`trace._turn` is `threading.local()`, and `trace.new_turn` is called in exactly
one place (`delegation_tool.py:377`) on the voice thread. Roster agents run on
daemon threads spawned at `roster.py:620` and inherit nothing.

Measured on `~/.diablo/trace.jsonl`, 3,072 real events:

| | events | share |
|---|---:|---:|
| Total | 3,072 | |
| No turn id | 1,800 | **59%** |
| `tool` events with no turn id | 944 of 961 | **98%** |

The irony is exact: `agent_tools.CONTEXT` uses `threading.local()` and is
correct, because callbacks must *not* be shared. `trace` uses the same mechanism
and is wrong, because correlation *must* be inherited. `contextvars` gives
inheritance; `threading.local` does not.

### HIGH

**F4 — Captures leak on the rejected-frame path.**
Two sites return before the `try`/`finally` that releases the file:

- `screen.py:388` — `if not frame.ok: return None`
- `screen.py:565` — `if not frame.ok: return frame.problem...`

The frame Diablo decided it could not trust is the one it leaves on disk.
Caught by the project's own privacy test, which reported
`1 left: ['/var/folders/.../diablo_win_71565_1789555763.png']`.
`ScreenFrame.release()` exists and the newer modules call it 15 times; the leak
is in the older raw-path API that bypasses `ScreenFrame` altogether.

**F5 — The planner cannot see outcomes.**
`do_engine.model_planner` builds its history from `s.describe()`, and
`Step.describe()` (line 223) renders the verb and target only. `Step.state`
exists — `PLANNED`, `SUCCEEDED`, `FAILED`, `STALE` — and is never rendered.
The planner sees `"click Save; click Save; click Save"` with no indication that
any of them failed. This is the best available explanation for the measured
82.2% first-step versus 50.0% later-step advancement.

**F6 — Two event systems that share nothing.**
`CompanionManager.emit` (UI/state, with subscribers) and `trace.event`
(decisions, to JSONL). Different vocabularies, no shared id, no sequence number.
Concurrent agents interleave in the trace with only a float timestamp to order
them.

**F7 — Capture cost is a subprocess.**
Every frame is a `screencapture` process spawn plus a PNG write plus a read.
Measured: **147 ms median** for a 400×300 region (n=5, 141–203 ms). A verified
action needs two frames, so roughly 300 ms of pure capture per step before OCR,
Accessibility or the planner's 1.57 s.

### MEDIUM

**F8 — Duplicated routing block.** `delegation_tool.py` lines 434–441 and
467–475 are the same "named agent takes the conversation" block plus the same
`verdict["action"] == "chat"` branch. The first runs on every turn; the second
runs again after the clarify loop.

**F9 — Job store is memory-authoritative.** `JobStore._flush` writes the last
100 jobs to `jobs.json`, but on restart a job still marked `running` has no
thread behind it. Nothing reconciles.

**F10 — Guidance is fire-and-forget.** `pointing.show` spawns and returns
`True` without knowing whether anything drew.

**F11 — Documentation drift.** README says 14B and 33 tools. `claude.md` says
4B and 39 tools. The code has **48**. `memory.md` ends at 2026-09-13 and is
missing three days containing the screen overhaul, the UI redesign, the
animation work, DO 2.0 and the planner benchmark — against a project rule that
says append to it.

### LOW

**F12 — `doctor.py` bare imports.** `import local_brain` and `import voice`
outside the package path. Isolated to a CLI diagnostic; the dual-import problem
`claude.md` warns about is otherwise fixed.

---

## G. Agent capability gaps

Measured against the target architecture in the brief.

| Layer | State | Where |
|---|---|---|
| Intent Engine | **EXISTS** | `route.classify` + `agent_core.understand` |
| Context Engine | **PARTIAL** | assembled ad hoc per call site; no owner |
| Goal Engine | **PARTIAL** | `do_engine.Goal` exists; unreachable |
| Planner | **EXISTS** | `agent_core.plan` (live), `do_engine.model_planner` (orphan) |
| Perception | **EXISTS** | `perception.locate`, three sources, fused |
| Target Fusion | **EXISTS** | `perception._fuse`, non-mutating |
| Policy | **EXISTS** | `computer_use.risk_of` / `decide` — unreachable |
| Permission | **EXISTS** | `Grant`, 600 s expiry — unreachable |
| Executor | **EXISTS** | `computer_use.act_on` — unreachable |
| Verification | **PARTIAL** | screen-diff unreachable; 7 answer guards live |
| Recovery | **EXISTS** | `do_engine` 11 failure classes — unreachable |
| Memory | **MISSING** | in-memory session only; no persistence |
| Report | **EXISTS** | `Task.summary`, spoken |
| Tool Contracts | **NEEDS REFACTOR** | name, fn, tags, schema — no risk, no reversibility, no verification hook |
| Skills | **MISSING** | `skill_registry` indexes modules; it is not a skill engine |
| Missions | **MISSING** | one `threading.Timer`, for shutdown |
| Delegation | **EXISTS** | `roster`, fan-out, per-agent working state |
| Model Routing | **PARTIAL** | `local_brain.with_model` escalation, 4B → 14B |

**Event spine (§18):** correlation ids exist but do not cross threads;
an emitter exists in the manager; sequence numbers, a reducer, and a single
`AgentEvent` type do not.

---

## H. GUI gaps

Present today: `Rail`, `Stage`, `Turn`, `Head`, `Running`, `Trace`, `Health`,
`Settings`, `Mascot`, `Needs`, plus the native `OrbView`.

Against PRESENCE → CONVERSATION → WORK → GUIDANCE:

| Surface | Component | Note |
|---|---|---|
| Presence | `OrbView` (native) + `Mascot` (web) | two renderings of one identity |
| Conversation | `Stage` + `Turn` | reusable |
| Work | `Rail` + `Running` | coupled to the roster's shape |
| Guidance | `overlay.py` | **a separate process with no shared state** |

Guidance is the gap. It shares no component, no event stream and no state with
the other three, and the panel cannot show that pointing is happening.

**Open defect:** `test_app.py` asserts the orb window is 92×92. The code
computes `CANVAS = ORB(44) + HALO(11)×2 = 66`, and the running window measures
66×66. The source is self-consistent; the test hardcodes a stale number.

---

## I. Python / Swift

**§24's premise does not hold: there is no Swift in this repository.** No
`.swift`, no `.m`, no `.xcodeproj`. Everything native goes through PyObjC, and
Accessibility goes through raw `ctypes` because
`pyobjc-framework-ApplicationServices` is not installed.

The boundaries that actually exist and their condition:

| Boundary | Condition |
|---|---|
| Python ↔ PyObjC (AppKit, Quartz, Vision) | good; the selector rules are understood and documented in-code |
| Python ↔ `screencapture` subprocess | **weak** — 147 ms per frame, files on disk, F4 leak |
| Python ↔ `overlay.py` subprocess | acceptable for isolation, **no feedback channel** (F10) |
| Python ↔ WKWebView over localhost | good; SSE, no build step |

A Swift migration is not indicated by anything in this audit. The one place
native code would pay is capture — ScreenCaptureKit would remove the subprocess,
the disk write and the leak at once. That is an argument for one component, not
for a language migration.

---

## J. Testing gaps

**Measured this session, full suite, 665 s:**

| | |
|---|---|
| Suites | 33 |
| Passed | 31 |
| Failed | 2 |

The historical baseline of "28/30 suites, 1,156 checks, known failures
`test_app` + `test_agent`" is **out of date, and the known-failure list is
wrong**:

- `test_agent.py` **passed** this run.
- `test_screen.py` **failed** — 3 of 68 under the venv. Two are screen-state
  dependent (a window captured black). One is the real leak in F4.
- `test_app.py` **failed** — 1 of 53, and *not* because Diablo was not running.
  Diablo was running and 52 checks passed. The failure is the stale 92×92
  constant in H.

Running `test_screen.py` under system `python3` instead of the venv gives 6
failures with `ModuleNotFoundError: No module named 'AppKit'` — the suite is
sensitive to which interpreter invokes it, and nothing says so.

**Coverage gaps that matter more than the count:**

1. **Perception has no benchmark.** `grep -rl perception evals/` returns
   nothing. Nobody knows how often `locate()` returns the right control.
2. **The planner benchmark stubs the screen.** `run_planner_eval.py:310`
   replaces `engine._screen_summary` with a fixture string. The 74.6%
   advancement figure is measured against perception that is never wrong, so it
   is an upper bound, not an estimate.
3. **Nothing measures the closed loop.** `run_eval.py` covers 15 cases at the
   router level; `run_planner_eval.py` covers 59 at the planner level. Neither
   runs observe → locate → act → verify.
4. Unmeasured from §19's list: target correctness, false completion,
   verification accuracy, end-to-end step count.

---

## K. Proposed build sequence

Ordered by dependency, not by the diagram. Each build is separately shippable
and separately reversible.

| # | Build | Why here | Depends on |
|---|---|---|---|
| **1** | **Perception ground truth** | the missing number that gates everything downstream | — |
| 2 | Trust boundary for untrusted text (F2) | must precede any action capability | — |
| 3 | Event spine: `contextvars` correlation, sequence numbers, one `AgentEvent` (F3, F6) | measurement needs attribution | 1 |
| 4 | Capture ownership: one API, one lifecycle (F4, F7) | removes the leak and the 147 ms | — |
| 5 | Planner state representation: render outcomes (F5) | the measured first/later gap | 3 |
| 6 | Tool contracts: risk, reversibility, verification hook | prerequisite for connecting actions | 2 |
| 7 | Connect the action layer behind a flag (F1) | only safe once 1, 2 and 6 land | 1, 2, 6 |
| 8 | Closed-loop benchmark | needs something connected to measure | 7 |
| 9 | Context Engine | shape it from what 5 and 8 show is missing | 5, 8 |
| 10 | Memory / persistence | | 3 |
| 11 | Skills | | 6 |
| 12 | GUI identity + guidance as a real surface (F10) | independent workstream, can run in parallel | — |
| 13 | Missions, delegation, model routing | | 10, 11 |

### Dependency graph

```
        ┌──────────────────────────────┐
        │ 1. Perception ground truth   │────┐
        └──────────────────────────────┘    │
        ┌──────────────────────────────┐    │
        │ 2. Trust boundary            │──┐ │
        └──────────────────────────────┘  │ │
        ┌──────────────────────────────┐  │ │
        │ 4. Capture ownership         │  │ │   (independent)
        └──────────────────────────────┘  │ │
                      │                   │ │
                      ▼                   │ │
        ┌──────────────────────────────┐  │ │
   1 ─▶ │ 3. Event spine               │  │ │
        └──────────────────────────────┘  │ │
                      │                   │ │
                      ▼                   ▼ ▼
        ┌──────────────┐        ┌──────────────────┐
        │ 5. Planner   │        │ 6. Tool contracts│
        │    state     │        └──────────────────┘
        └──────────────┘                  │
                      │                   ▼
                      │        ┌──────────────────────┐
                      │        │ 7. Connect actions   │
                      │        └──────────────────────┘
                      │                   │
                      └────────┬──────────┘
                               ▼
                    ┌──────────────────────┐
                    │ 8. Closed-loop bench │
                    └──────────────────────┘
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
              9. Context            10. Memory ──▶ 13. Missions
                                    11. Skills ──▶

        12. GUI / guidance surface — parallel, no dependency
```

---

## L. Risk register

| # | Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|---|
| R1 | Prompt injection through screen or web text reaches an action tool | **critical** | medium, rising with build 7 | build 2 before build 7, no exceptions |
| R2 | Connecting the action layer while target correctness is unmeasured | **critical** | high if sequence ignored | build 1 gates build 7 |
| R3 | Stale computer-use grant reused after context changed | high | low | 600 s expiry exists; re-verify on connect |
| R4 | Screenshots accumulating in `/var/folders` | high | **confirmed happening** | build 4 |
| R5 | Refactoring the router breaks the clarify loop | high | medium | `route.py` already has a fallback path; keep it |
| R6 | Event spine turns into a rewrite | medium | medium | `contextvars` swap plus a sequence field is ~20 lines; resist more |
| R7 | The 33-suite baseline drifts unnoticed | medium | **confirmed happening** | pin the interpreter, fix the stale constant |
| R8 | Orphaned modules rot further against changing signatures | medium | high | build 7, or delete them; not both states forever |
| R9 | Delegated work stays unattributable in the trace | medium | confirmed | build 3 |
| R10 | Live-application clicks introduced without authorization | high | low | the standing boundary holds: fixtures only |

---

## M. Immediate next build

### Build 1 — Perception ground truth

**Objective.** Answer one question with a number: *how often does
`perception.locate()` return the right control?*

**Why this one.** Everything the overhaul wants to do depends on it and nobody
knows the answer. The planner scored 74.6% against a stubbed screen that is
never misread, so that figure is an upper bound on a system whose real
perception accuracy is unmeasured. Connecting the action layer without this
number means clicking on targets of unknown correctness. It is also the only
candidate first build that is entirely read-only: it needs fixtures and
read-only inspection of real windows, and not a single synthetic click.

**Current state.** `evals/` contains a 15-case router benchmark and a 59-case
planner benchmark. Neither touches perception. `grep -rl perception evals/`
returns nothing.

**Change.**
- A labelled corpus of windows and the controls in them, built from the existing
  `tests/fixtures/target_app.py` and `icon_app.py`, plus read-only captures of
  real applications with hand-labelled ground truth.
- `evals/run_perception_eval.py`, scoring: top-1 correctness, top-3
  correctness, wrong-target rate, abstention rate, per-source attribution
  (Accessibility / OCR / vision), and latency per source.
- Fold in the `contextvars` correlation fix from F3, because a benchmark that
  cannot attribute its own events cannot be debugged.

**Tests.** A suite for the scorer itself, in the pattern of
`test_planner_eval.py` — the planner benchmark shipped a scorer bug that
flattered the model by 100%, and that must not happen twice.

**Metrics.** Baseline recorded for all six scores, per source, before any
perception code changes.

**Regression check.** Full 33-suite run. No production module modified except
`trace.py`.

**Definition of done.** A committed baseline JSON, a table of the six metrics
per source, and an explicit written answer to: *is target correctness high
enough to justify connecting the action layer?*

**Explicitly not in this build.** No clicks in live applications. No changes to
`perception.py`. No action layer wiring. No event spine beyond the correlation
fix.

---

## N. Decisions taken in this audit

1. **No big-bang rewrite.** Nothing found justifies replacing the router, the
   planner, the perception stack or the GUI framework.
2. **No Swift migration.** There is no Swift to migrate from, and the one place
   native code would pay is capture alone.
3. **The orphaned action layer is kept, not deleted.** It is good code with a
   real path to being connected, at build 7.
4. **Measurement precedes connection.** Build 1 is a benchmark, not a feature.
5. **The real-click boundary stands.** Fixtures only, until explicitly
   authorized otherwise.

---

## Appendix — commands used

```bash
python3 tests/run_all.py                       # 33 suites, 665s, 31 pass
~/.diablo/venv/bin/python3 tests/test_screen.py # 3 of 68 fail
~/.diablo/venv/bin/python3 tests/test_app.py    # 1 of 53 fails
```

Trace analysis (3,072 events) and the capture-latency measurement (147 ms
median, n=5) were run as one-off probes and are reproduced in §F3 and §F7.
