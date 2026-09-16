# Diablo — project memory

Append only. Newest at the bottom.

## 2026-09-13 — repository created

Extracted Diablo from `~/.diablo`, which turned out to be openclaw's entire
source tree (1 GB of node_modules, 56 MB of src, their Dockerfiles, README and
LICENSE) with about thirty Diablo files dropped on top. Diablo itself is 608 KB.

`~/.diablo/.git` was a 466 MB clone of `github.com/openclaw/openclaw` with
30,974 commits, and `origin` pointed at their repository. No Diablo file had
ever been committed. Moved to `.git.openclaw-backup`.

## 2026-09-13 — full test sweep, 13 suites

Ten real defects found. The three that mattered:

- **Arithmetic done mentally.** Totalling a three-row CSV returned 350, then 335
  on a retry. The answer was 115. `python_snippet` was never offered to the
  model because the tool's tags lacked `total`, `sum`, `revenue`, `average`.
  Fixed in both the tags and the system prompt.
- **Fabricated status and filenames.** With no conversation history, "what's
  that file called again" returned `IB2027ResearchDraft.docx`, which has never
  existed. The prompt contradicted itself: one bullet said Diablo had tools, a
  later one said it had none. Fixed, plus a code-level guard that stops
  back-reference questions reaching the model when there is no history.
- **Voice cache keyed on profile name, not voice.** Changing the Voicebox
  preset would have left every previously spoken line in the old voice forever.

Also: the VAD had never run (undefined `VAD_THRESHOLD` swallowed by a bare
`except`), `voice.available()` never contacted Voicebox, and four screen tools
worked only because `agent_core` mutated `sys.path` as a side effect.

## 2026-09-13 — measurements worth keeping

- qwen3:14b: warm reply 0.26s, warm tool-calling turn 1.86s, cold start 6.54s
- qwen3:4b: 9.97s and emits no tool call. Not a viable alternative.
- Model resident: 9.3 GB, which is 97% of Diablo's footprint. Bridge is 250 MB.
- Whisper tiny.en 86ms per 3s window; base.en 193ms at beam 1
- Endpointed capture: 2.24s for a 1.5s utterance, 1.76s on silence
- `fast_understand` regex path: 3.4µs, fires 16/30, zero false fires
- Wake listener: 20 minutes of audio in 15.7s, zero false wakes

## 2026-09-13 — clicky

Read `farzaa/clicky`. Its headline feature is a cursor that flies to a UI
element, driven by `[POINT:x,y:label:screenN]` tags the model emits. It gets
those coordinates from Claude's Computer Use API, over the network, after
downscaling the screen to 1280x800 — its own comment notes higher resolutions
"degrade precision".

Diablo does not need to guess. `_ocr_fragments()` already returns exact boxes
from Apple's Vision framework and `CGWindowList` already has window bounds, so
absolute screen coordinates are arithmetic. Spiked and verified, including a
window at x=-1512 on a second display, which worked without special handling.

## 2026-09-16 — architecture audit, before the overhaul

Read-only audit at `83598eb`. Full writeup in
`docs/architecture/diablo_overhaul_audit.md`. The five findings worth
remembering:

- **The action layer has no caller.** `computer_use` and `do_engine` are 1,373
  tested lines that nothing in production imports. Diablo can point at a button
  and cannot press one. Deliberate, but they will rot if left in this state.
- **The trace cannot attribute 59% of its own events** (1,800 of 3,072 real
  records; 944 of 961 tool calls). `trace._turn` is `threading.local()` and
  roster agents run on their own threads. `agent_tools.CONTEXT` uses the same
  mechanism and is *correct*, because callbacks must not be shared — correlation
  must be inherited, which needs `contextvars`.
- **No boundary between untrusted text and instructions.** OCR and web content
  enter `agent_core` messages and the planner prompt raw. The rule is written in
  claude.md and enforced nowhere.
- **The planner never sees outcomes.** `Step.describe()` renders the verb only;
  `Step.state` is never rendered. Best explanation so far for 82% first-step
  against 50% later-step advancement.
- **The planner benchmark stubs perception** (`run_planner_eval.py:310`), so
  74.6% is an upper bound. Perception itself has no benchmark at all.

Baseline corrections: the suite is **33 suites, 31 passing**, and the two
failures are not the pair recorded on 2026-09-13. `test_agent` passed.
`test_screen` failed on a real capture leak — `screen.py:388` and `:565` return
before the `finally` that releases the frame, so the capture Diablo rejected as
untrustworthy is the one it leaves in `/var/folders`. `test_app` failed on a
stale 92×92 constant; the orb is 66×66 by construction (`ORB 44 + HALO 11×2`)
and the source is self-consistent.

Also: 48 tools, not the 33 in README or the 39 in claude.md. No Swift anywhere.
Every frame costs a `screencapture` subprocess, measured at 147 ms median.

## 2026-09-16 — Build 1: perception ground truth

DIABLO PERCEPTION BASELINE V1. 25 cases, 10 target categories, four sources
measured separately, 100 measurements. Artefacts:
`evals/perception_cases.json`, `evals/run_perception_eval.py`,
`evals/perception_baseline_v1.json`, `tests/test_perception_eval.py` (66
checks), `tests/fixtures/controls_app.py`, `tests/fixtures/_layout.py`.

| source | top-1 | precision | answered | wrong target | median |
|---|---|---|---|---|---|
| accessibility | 72% | 100% | 18 | 0 | 6.5 ms |
| OCR | 24% | 100% | 6 | 0 | 180 ms |
| vision | 12% | 100% | 3 | 0 | 6344 ms when it engages |
| fusion | 56% | **67%** | 21 | 0 | 189 ms |

The two findings worth keeping:

- **No source ever pointed at the wrong control. Zero wrong targets in 100
  measurements.** Perception either gets it right or says nothing. Every error
  in the baseline comes from the fusion layer, and every one of those is a
  mis-flagged ambiguity rather than a mis-identified control.
- **Fusion scores lower than accessibility alone (56% against 72%)** because
  `AMBIGUOUS_WITHIN = 0.12` is an *absolute* margin. A perfect label match at
  confidence 1.000 with a runner-up at 0.910 is treated exactly like two vague
  matches at 0.50 and 0.41, so "save" is flagged ambiguous while the vaguer
  "the save control" (margin 0.125) is treated as decisive. Five of 25 cases
  regress under fusion; four are false ambiguity at confidence 0.93–1.00.

Unlabelled `AXTextField`s are unfindable by every source — no title, no text,
no distinguishing shape. The only 0% category.

Vision's 12% is not a verdict on the model. It is asked about all 25 cases and
is only for unlabelled icons; on those it is 100%, and it correctly declines
the rest. Measure a source on precision over what it answered, not top-1 over
everything.

Cache: 6531 ms cold, 337 ms warm, 94.8% saved, keyed on the frame fingerprint
so a changed screen invalidates it.

Two process notes that cost real time:

- **Tests and evals import from `~/.diablo/skills/`, not the repo.** There is no
  `install.sh` in this tree and the launcher has no sync command, so the two
  are kept identical by hand. A fix edited in the repo did not exist as far as
  the tests were concerned. `run_perception_eval.py` now refuses to run when
  the measured modules differ, because a benchmark measuring stale code is
  worse than no benchmark.
- **contextvars does not cross a plain `threading.Thread`.** Swapping
  `threading.local()` for a `ContextVar` fixes nothing on its own; the spawn
  site has to carry `copy_context()`. `trace.carry()` exists for that and
  `roster.py:620` is not yet using it, so the 59% attribution gap is still
  open outside the benchmark. Both behaviours are asserted in the suite so
  nobody assumes the swap was sufficient.
