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
