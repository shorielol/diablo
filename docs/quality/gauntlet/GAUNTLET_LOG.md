# Diablo gauntlet log

Resume from here after a context reset. Newest state at the top of each section.

## Baseline (2026-10-01)

- Commit `6c00241` on `master`. Working tree clean apart from untracked
  `tests/fixtures/real/rendered/` (generated renders, deliberately not committed).
- Regression run against an isolated scratch home built from HEAD
  (`git archive` + a copy of the installed skills overlaid with the repo's
  `diablo/*.py`; nothing written to `~/.diablo`). Model suites included,
  Ollama up with qwen3:4b-instruct, qwen3:14b, qwen2.5vl:7b.
- **55 suites: 48 passed, 7 failed, 0 skipped, 1021 s.**

| Suite | Baseline failure | Classification | Resolution |
|---|---|---|---|
| mic_wake | `mic_device.json exists` | environment (scratch home lacked the gitignored runtime file) | passes 19/19 with the file present; scratch builder copies it |
| manager | "it still reached idle" | environment (same missing file: "cannot start without: mic") | passes 63/63 with the file present |
| pointing | 2 "On the real screen" checks | **real defect** P2: a nonsense request marked the word "not" on a web page | fixed in `pointing._score` (G-5) |
| grounding | welfare-loss "it is refused" | test regression from `6c00241` (dropped `DIABLO_VISION_COORDS=pixels` setup; also weakened "first crop") | test setup restored (G-6) |
| boundaries | "no production module imports do_engine" | brittle test (grep matched a docstring) | AST import scan (G-7) |
| handoff | "against Diablo, not an agent" | brittle test (literal match on a call wrapped across lines) | whitespace-normalised match (G-7) |
| agent | "it edited the one it was told" | environment-coupled, 3/3 fail while a leaked screenshot sat on the Desktop; 4/4 pass after it was removed | root cause of the leak fixed (G-8); model sensitivity to listing noise remains a risk |

Earlier report reconciliation (eb87e4e run of 55: 50 pass, 5 fail):
screen (dark display) passed 102/102 this time; system ("Error" on screen)
passed; boundaries and handoff were brittle tests, not pre-existing product
defects; grounding was the test-setup regression above.

## Privacy baseline (Reference C)

- Pre-run image inventory: 1,919 image files across Desktop, Downloads,
  Documents, Pictures, `~/.diablo`, `~/Library/Caches`, `$TMPDIR`,
  `/private/tmp`, the repo (metadata only; scratch dirs excluded).
- Pre-existing leftovers found: `DIABLO_Screenshot_1790856822_main.png` on
  the Desktop (17:43, before this run — left in place, reported) and
  `diablo_win_6994_1790689368.png` in `$TMPDIR` (2026-09-29).
- `trace.jsonl` 1.89 MB before the run.

## Findings and fixes

| ID | Sev | Class | Finding | Fix | Verification |
|---|---|---|---|---|---|
| G-1 | P2 | confirmed (dormant code) | `act_on` re-clicked automatically when nothing visibly changed (toggle undone) | removed the retry; `MAX_RETRIES = 0`; suspected no-op is reported | `test_action_outcomes` "exactly one click" |
| G-2 | P2 | confirmed (dormant) | any screen change counted as verified; a failed after-capture counted too | four outcomes (`confirmed`/`unverifiable`/`suspected_noop`/`refused`); only the expected words confirm | same suite |
| G-3 | P2 | confirmed (dormant) | `click()` ran with `frame=None`; a stale frame whose fresh capture failed skipped the check | frame required and checked before anyone is asked; unreadable fresh or before-frame refuses | same suite |
| G-4 | P2 | confirmed (dormant) | refusals were prose; `do_engine.classify` searched the sentence, so "declined", "no confirmation" and password-manager refusals re-planned and asked again | 15 refusal codes; `classify` reads codes first, prose fallback kept | same suite, "asked once" |
| G-4b | P2 | confirmed (dormant) | `do_engine` re-planned after a no-effect click and the planner named the same control again | same control after a no-op stops the task; a different control is still allowed | same suite + `test_do_engine` 51/51 unchanged |
| G-4c | P3 | confirmed (dormant) | typed text counted as verified because the keystroke was posted | `_as_record` carries the outcome; typing is unverifiable | same suite |
| G-5 | P2 | confirmed (live) | reverse containment scored a lone word of a long request 0.85 | held to the same ≥50% coverage rule as partial overlap | `test_pointing` 31/31 (2 new rejects); perception benchmark 0/75 records changed, 49/75 good before and after |
| G-6 | P3 | confirmed | `test_grounding` setup regression | restored | 135/135 |
| G-7 | P3 | confirmed | two brittle source-text tests | AST scan / normalised match; AST scan verified to flag 3 import forms and ignore the docstring | 56/56, 48/48 |
| G-8 | **P1** | confirmed (live) | `test_system` wrote a capture of every display to the real Desktop and deleted one | test redirects the tool's Desktop to a private temp dir, removes all, asserts the real Desktop gained nothing | 29/29; leaked file from this run moved to Trash |
| G-9 | P2 | evidence-backed risk | Vassago (delegated screen agent) could call `take_screenshot`, which writes to the Desktop | removed from Vassago's tools; explicit "take a screenshot" stays direct (`test_architecture`) | roster 55/55, architecture 154/154 |
| G-10 | **P1** | confirmed (live, in the real trace) | `trace.jsonl` stored 120 chars of every tool result (15 read_screen, 6 explain_screen, 14 list_windows events), control labels, evidence, window titles and answer text | sizes and flags only; `test_trace_privacy` drives agent_core and `_record` with a marker and scans every `trace.event` call statically | new suite 8/8; fails 5/8 on the old code |
| G-11 | P2 | confirmed | `ppt_tool` imported `tools.llm_tool`, which exists only in a legacy untracked install folder (and differs from the shipped module) | `from skills import llm_tool` | imports in a home without `tools/` |
| G-12 | P2 | confirmed | `screen_context.structured()` left its capture in `$TMPDIR` if OCR raised; `explain()` leaked it if the vision check raised; crash/kill leftovers were never removed (one found, two days old) | one owner per capture with `finally` clean-up; `sweep_stale_captures()` (Diablo's own `diablo_*.png`, older than 10 min) on screen-subsystem start | new `test_capture_lifetime` 14/14; fails 5/9 on the old code |

### Independent critic A (fresh-context agent, read-only) and the second round

Verdicts on the first version: closed set pass but `partial` missing; specific
evidence partial (substring match); unverifiable-never-success pass; refusal
before input **fail** (input_unavailable after posting); retry advice-only
pass at computer_use, **fail** at the loop; stale/missing capture partial;
permission pass; tests mostly meaningful. Every claim below was reproduced by
a failing test before the fix (18 new checks, all red first).

| ID | Sev | Finding (critic) | Fix |
|---|---|---|---|
| G-13 | **P1** | regression from round one: an unverifiable click with an unmet expectation skipped the recovery count, so one toggle could be clicked 12 times (old code: 4) | unverifiable with an expectation goes through the counted failure path again; reproduced 12 clicks → now 1 |
| G-14 | **P1** | the no-repeat guard keyed on the planner's words; "dark mode switch" got the same control clicked again | `do_engine` keeps the resolved control (description, x, y) of every unproven click and passes it to `act_on(avoid=…)`, which refuses `already_tried` before posting; same name ≥24 pt away is a different control |
| G-15 | **P1** (older) | `act_on`'s fresh re-read only checked `found`; a different, ambiguous or now-disabled control was clicked on the earlier yes | fresh read must be the approved control (`target_changed`), unambiguous and enabled |
| G-16 | P2 | partial delivery (mouse-down only, typing cut off) reported as refused with no action | `PARTIAL` outcome; `_post_click` returns events posted; partial stops the DO task |
| G-17 | P2 | spoken "something changed" when the after-capture failed | one `_report()` wording table; failed after-look says it could not look |
| G-18 | P2 | `expect="on"` confirmed by "Notification" | whole-word match |
| G-19 | P2 | refused typing kept `action="type"`; typing explained as "I clicked"; a `press` step typed the key's name ("Return") every step until the step ceiling (24 key events in the test) | `_as_record` settles one outcome; `explain()` says "typed"; `press` stops the task ("I can't press keys yet") |
| G-20 | P3 | `click()`: no target binding, one frame authorised many clicks; a declined destructive or uncertain click did not revoke a standing grant | point must be inside the frame (`frame_mismatch`); `ScreenFrame.spent` makes a frame single-use (`frame_used`); declines revoke |

Also found while checking the C workload: an unsliced `label=` in the
`observed` trace event (`agent_core`) that the first static check missed; the
check now forbids content field names outright. `showing.py`'s `window=` (an
id) renamed `window_id`. Two suites left synthetic images in temp folders
(`test_signal` icon render, `test_shapes` diagram): cleaned up.

`test_action_outcomes`: 103/103 on the fix; the same file against HEAD fails
36 of the 44 checks it reaches.

Critic A, round two (re-review of the above): requirements 1-6 and 8 pass;
7 (permission) **fail** on one P1:

| ID | Sev | Finding | Fix |
|---|---|---|---|
| G-21 | **P1** | an unspent one-time approval survived a refusal (target changed, gone, already tried…) and covered the next click on any control, in any task | `_drop_unspent_once()` on every refusal in `click`, `type_text`, `act_on` |
| G-22 | P2 | the already-tried check ran after the user had been asked | also checked against the first reading, before `_permission` |
| G-23 | P3 | an approved uncertain click that changed nothing was put to the user as a question | `classify` treats `ask` as ambiguity only while nothing was sent |

Accepted and recorded, not fixed: R3 (typing has no repeat guard; each
attempt still needs its own yes), R5 (point-in-frame skipped for a frame of
zero size; readable frames always carry their size), no hard frame TTL or
capture-generation binding (Cua has both; Diablo's `click()` has no
production caller and `act_on` re-perceives instead of trusting a frame).
`test_action_outcomes` 107/107 after G-21..G-23.

### Independent critic C (privacy, fresh context)

Verdict on the state after G-1..G-23: **Reference C not met.** Scan clean
(0 new images, 1921 files before and after), trace and capture fixes
confirmed, but other sinks remained. Correction to an earlier line of this
log: the static trace check did NOT "forbid content field names outright" —
it banned a fixed list and missed `what`, `dropped`, `quote`.

| ID | Sev | Finding | Status |
|---|---|---|---|
| G-24 | **P1** | `agent_core` printed `result[:160]` of every tool to stdout, which is `~/.diablo/diablo.log` in the app (41 such lines in the run log, one after `read_screen`) | fixed: size, or an error's wording; test captures stdout |
| G-25 | **P1** | `tutor.walk` captured the window but its clean-up began only at the first step: six early returns, exceptions and the wait for an answer left the capture | fixed: `walk()` owns the capture and deletes it in `finally`; body moved to `_walk` (`test_walkthrough` source check pointed at `_walk`) |
| G-26 | **P1** | `show` trace events wrote the model's step names (generated from screen text); `quoted_unread` wrote the quote; `tutor` printed menu-bar text and the equation read off screen | fixed: counts and lengths; banned names extended (`what`, `dropped`, `quote`) |
| G-27 | P2 | `voice.py` caches every synthesised utterance as `voice_cache/<hash>.wav` with no eviction, including spoken screen answers (only when Voicebox runs) | **open**: product decision (latency cache vs persistence) |
| G-28 | P2 | `jobs.json` keeps agent answers (≤400 chars / full summary); a delegated screen answer persists | **open**: the side panel reads it; needs a retention decision |
| G-29 | P3 | `screencapture` timeout/non-zero leaves a partial file; `screen.py` capture leaks if `browser_tab`/`_measure` raises; `writeToFile` temp names outside the sweep pattern | open (the startup sweep covers `diablo_*.png` leftovers) |
| G-30 | P3 | the main agent's `take_screenshot` writes to the Desktop without checking that the user asked to save one | open |

### Independent critic B (UI, fresh context)

Raycast could not be inspected (its window is hidden; showing it means
driving the live app), so every Raycast comparison is **Not verified**.
Rubric on Diablo's own renders (`tests/ui_shot.py`, real server + WKWebView,
seeded in memory; renders deleted after review): first launch pass;
typography/contrast pass (all text tokens ≥4.5:1, computed); reduced motion
pass (CSS, shell, and the overlay change reviewed correct); resizing pass
(the 1040-wide compact render is not reachable: the compact surface is fixed
at 380); focus/keyboard, Escape, empty/error states and ARIA had defects.

| ID | Sev | Finding | Status |
|---|---|---|---|
| G-31 | P2 | Health showed `null` permissions ("macOS has not said") as "denied" with a Grant button, contradicting the Needs strip's own rule | fixed: three states, "not asked yet", no Grant for null |
| G-32 | P2 | Escape during IME composition folded the whole surface | fixed: `isComposing` guard |
| G-33 | P2 | no on-screen way to cancel a running job (only "Stop speaking") | open |
| G-34 | P2 | Escape closing the menu leaves focus on a hidden item; `role=menu` without arrow keys; `aria-pressed` on `menuitem` | open |
| G-35 | P3 | `--focus` blue also marks "needs you" (token contract reserves amber); light-mode focus fallback 2.98:1; answer not in a live region; overlay reads Reduce Motion once per session | open |

Disclosure: verifying G-12, `test_manager` starts the real manager, whose
sweep removed `$TMPDIR/diablo_win_6994_1790689368.png` (an orphaned capture
of a window, 2026-09-29). It is Diablo's own leftover and exactly what the
sweep is for; it was present in the first baseline scan and absent after.

Also: `do_engine`'s refusal map is built from `computer_use`'s constants
rather than repeated string literals (one definition).

## Open

- Single-use capture binding (one frame authorising several clicks): still
  open by design; record as follow-up once DO is wired into routing.
- Test temp-folder hygiene (P3): suites leave `diablo-fastpath-*`,
  `diablo-laya-*`, `diablo-test-*`, `diablo-mcp-*`, `diablo-delegation-*`
  folders in the private temp folder (72 present). No images; not fixed.
- Reference B: Raycast cannot be inspected without bringing its launcher up
  on the live Mac (its window is ordered out); not done under the live-apps
  read-only rule.
- Trace still records user utterances (`heard`), request text and Diablo's
  questions: not screen content, but a privacy decision for the owner.
- Job store (`jobs.json`) keeps agent answers (≤400 chars) by design for the
  side panel.

## Round two (2026-10-01 to 2026-10-04): privacy gaps, P2 interface, verification

Note: the machine rebooted between 2026-10-01 and 2026-10-04 and `/private/tmp`
was cleared, taking the scratch homes, the first latency baseline and the Cua
reference clone with it. Evidence that must survive now lives under
`docs/quality/gauntlet/`.

### Retention policy (decided by the owner in the round-two brief)

| Sink | Policy now enforced | Code | Evidence |
|---|---|---|---|
| `voice_cache` | Only the fixed phrases in `voice.COMMON_LINES` are kept (`voice_cache/stock/`). Every other line is written to `voice_cache/transient/` and deleted when playback ends (played, interrupted, superseded or delivered by the fallback voice). A line nobody heard is kept for a retry for at most 10 minutes. The old unbounded top-level cache and any orphan are swept when the voice starts and after every line; only files named the way Diablo names them are touched. | `voice.py` | `test_retention` voice sections; 35 of 52 checks fail on the old code |
| `jobs.json` | Status, timestamps, ids, agent and parts kept. An answer from a screen-reading run (any `roster.SCREEN_TOOLS` tool, or Vassago) is never written (`summary_withheld: "screen"`); any other answer is cut to 32 KB of UTF-8 and dropped 24 h after it was written, in memory and on disk. Answers loaded with no expiry (written before this policy) are dropped at startup (`"legacy"`). A failure records the exception class, never its message. Written atomically, mode 0600. Interrupted work is written as interrupted at startup. | `delegation_tool.py` | same suite, job sections |
| Screenshots | The model's `take_screenshot` saves only when the person's own request (set by `agent_core.run`, not by the model) asks for one; otherwise it returns "Not saved" and captures nothing. The destination is the Desktop, or Downloads / Documents / Pictures when the request names it. The voice reflex uses the same check and now says what happened instead of always "saved". The unused `voice_bridge.take_screenshot()` (bare `screencapture` to the Desktop) is removed. | `agent_tools.py`, `voice_bridge.py`, `agent_core.py` | same suite, screenshot sections; `test_system` (ordinary request writes no file) |

Upgrade note: on the first start of this build the live install's 708-file
`voice_cache` (spoken answers since September) and every stored answer in
`~/.diablo/jobs.json` will be removed by the policy above. That is the policy
working, and it is irreversible; it has not been run against the live install
during the gauntlet.

### Findings, round two

| ID | Sev | Finding | Status |
|---|---|---|---|
| G-27 | P2 | `voice_cache` kept every spoken answer forever | **fixed** (policy above) |
| G-28 | P2 | `jobs.json` kept agent answers, screen-derived included | **fixed** (policy above) |
| G-30 | P3→P2 | model-callable `take_screenshot` saved without being asked; reflex claimed success regardless | **fixed** |
| G-33 | P2 | no on-screen way to cancel a running job | **fixed**: Stop on every running or queued task in the compact and larger surfaces; `cancel_job:<id>`; `agent_core.run(should_stop=…)` stops before the next step; final `cancelled` state for job and parts; result not announced; queued jobs never start |
| G-34 | P2 | menu: focus lost after Escape, no arrow keys, `aria-pressed` on menuitem | **fixed**: Down/Up/Home/End with wrap, Down opens the closed menu, focus on first item when opened, Escape returns focus to the button, `aria-current` |
| G-35a | P3 | "needs you" drawn in the focus blue | **fixed**: amber `--warn` |
| G-35b | P3 | light-mode focus ring contrast, no live region for answers, overlay reads Reduce Motion once | open |
| G-36 | P2 | (critic A4) a one-time yes leaked through exceptions and was not bound to its task | **fixed**: exception-safe wrappers drop it; `ALLOW_ONCE` covers its own task only; a refusal drops it whether or not a post was attempted |
| G-37 | P2 | (critic A4) confirmation was lexical: "Message not sent" confirmed `sent`; a re-rendered timestamp confirmed `saved`; the task goal was a substring check ("on" in "Notifications") | **fixed**: a line naming the result and a failure word is not evidence; a line that only re-rendered (same words, other numbers) is not evidence; the goal uses the same per-line test |
| G-38 | P2 | (critic A4) an exception after the click escaped with no outcome; the DO engine then refused new tasks for up to 300 s | **fixed**: a failed comparison is `unverifiable`; `run()` ends FAILED with an honest sentence |
| G-39 | P2 | (critic A4) the already-tried check compared the exact description and 24 pt; the same toggle re-found by OCR, or 30 pt off, was pressed again | **fixed**: same label (role ignored) within 64 pt, or same description within 24 pt |
| G-40 | P3 | (critic A4) a refusal after an approved uncertain click was read as a question; the user was asked twice for one click | **fixed** |
| G-41 | P3 | (critic A4) grants were not revoked when a task ended or was cancelled | **fixed** |
| G-42 | P1 (release) | `voice_bridge`, `calendar_tool`, `cache_audio` hard-coded `/Users/shaurya/.diablo`: any other install read and wrote this one | **fixed**: derived from `DIABLO_HOME` |
| G-43 | P2 | a queued job was invisible until the 20 s poll | **fixed**: `job_queued` event |
| G-44 | P1 (release) | `tools/install.sh` cannot install on a clean Mac (requires an existing `~/.diablo` or a legacy Jarvis tree, copies modules from the wrong folder, runs tests from the wrong folder, patches a legacy bridge, describes a wake word and ElevenLabs) | open |
| G-45 | P2 | README states a 14B default, says 4B models "did not work", 27 suites, and `./install.sh` at the root | **fixed** (docs): 4B default and its history, measured latency and memory, 70 suites, install marked not yet packaged, "What it keeps" table; `docs/RELEASE_NOTES.md`, `docs/DEMO.md` added |
| G-46 | P2 | "open a new browser tab" opens or focuses Safari and reports success; no tab is opened | open |
| G-48 | P2 | (perf) the intake parse ran to its 300-token cap on research requests, writing an invented brief ("as of 2024 or 2025", named review sites) that was then discarded: 7.4 s of the 7.8 s before a research job was acknowledged | **fixed**: string fields bounded in the schema (`maxLength`), a task that reaches its bound falls back to the user's words, spoken fields cut back to whole sentences; same call 1.3 s |
| G-49 | P2 | (stress) the conversation folds by turn count, not size; at a 110% fill Ollama silently truncated the prompt (8,106 of ~9,000 tokens sent) and nothing in Diablo noticed. Recall held at 84/91/99 % fill; a near-full context costs ~20 s per turn on a cache miss | open |
| G-50 | P2 | (perf) a screen question that reaches the vision model takes ~40 s: vision load 5.2 s, image prompt 8.0 s, answer 2.3 s, 4B reload 2.2 s plus a cold prompt cache. Ollama holds one model at a time on this 18 GB Mac. Answered from the window list it takes 2.3 s | open (hardware limit; image size not reduced, to protect accuracy) |
| G-51 | P3 | comments in `app_shell`, `companion_manager`, `pointing`, `screen_context` name another assistant project, against the owner's no-competitor-names rule; one credits an open-source repository by name | open: removing attribution is the owner's decision |
| G-47 | P3 | (critic A4, accepted) half-sent click never releases the button; the 16-column fingerprint can miss a notification banner; `CGEventPost` without Accessibility may drop events silently so `input_unavailable` is rarely reachable | open; `click()` has no production caller |
| G-52 | P1 (release) | the app launcher sourced `.env` after setting `DIABLO_HOME`, so a `DIABLO_HOME` line in `.env` won: a bundle built for one home ran another home's code. **This happened during the gauntlet** (see "Launch incident" below) | **fixed**: `make_app.LAUNCHER` re-asserts the home it was built for after `.env`; dry run and two tripwired launches |
| G-53 | **P1** (privacy) | every spoken line was printed in full, and in the app stdout is `diablo.log`: a screen answer ("You are currently looking at…", 217 chars) was on disk after the first controlled launch. `[ASK]` questions and the push-to-talk transcript were printed the same way. G-24 had closed tool results only | **fixed**: `voice_bridge._echo` prints the words to a terminal and only `<N chars>` to a file; `test_trace_privacy` +3 checks (a static check that no `print` interpolates a spoken line, and `_echo` run against a file and a terminal), red on the old code (2 of 11 failed), 12/12 after; relaunch log shows `Diablo: <203 chars>` |
| G-54 | P3 | a fresh home has no `visualizer/` folder, so every state change logged `[UI State Error] No such file or directory` and the state file was never written | **fixed**: `set_ui_state` creates the folder; relaunch: 0 errors, `visualizer/state.json` written |
| G-55 | **P1** (privacy) | (final critic) G-53 was not the last path into `diablo.log`: `agent_core` printed every tool call's arguments (`json.dumps(args)[:110]`), so a search or a file built from `read_screen` put the screen's text in the log one step later (reproduced: secret in stdout, not in the trace). Also printed in full: the answer to an `[ASK]`, the one-shot `[USER]` command, and exception messages on the speech, reflex and command paths; a failed delivery's exception text also went into the trace (`error=`) | **fixed**: arguments printed as names and sizes, the ambiguous-target question as its length, `[ANSWER]`/`[USER]` through `_echo`, those exceptions as their class only (trace too). `test_trace_privacy` +2 behavioural checks (read the screen, then search for it) and the static check widened to `agent_core` and to `answer`, `command`, `args`; red on the old code, 14/14 after; the critic's probe now prints 0 lines carrying the secret |
| G-56 | **P1** (latent) | (final critic) the failure-word guard knew only ASCII apostrophes, and macOS writes them curly: "The document couldn’t be saved." confirmed `saved`, "Message wasn’t sent" confirmed `sent`; "Sent — tap to retry", "Will be sent when online" and "Saved changes pending" confirmed too. The same test decides `Goal.verified` | **fixed**: text is normalised (NFKC, one apostrophe, one hyphen) before matching; retry, pending, "will be", waiting, queued, unsent count as not done. +6 checks, red first (8 of 147 failed with G-59), 147/147 after. **Still lexical**: "Turn on Bluetooth to use AirDrop" confirms `on`. Computer use is off by default and has no production caller |
| G-57 | P2 (privacy) | (final critic) `wants_saved_screenshot` matched any save-verb within 30 characters of "screenshot": "take a look at the screenshot on my desktop", "can you get the text from this screenshot", "grab the screenshots folder and tidy it" each made the reflex save captures of every display to the Desktop. And `CONTEXT["request"]` was the intake model's rewrite, not the person's words | **fixed**: only a request for a new one counts ("a", "another", "a quick" or nothing before the noun; not "the screenshots folder"); inline and escalated turns pass the person's own words as `request`. Background jobs still check the task, which is a rewrite (recorded in the release notes). `test_retention` +3 checks, red first (5 of 6 sentences wrongly accepted; `request` did not exist), 67/67 after |
| G-58 | P2 (evidence) | (final critic) the first latency runs' warm multi-step times were refusals: the file existed from the cold run, the confirm stub declined the overwrite, and "the file was written correctly in every run" was false | **fixed** in the harness (empty Desktop per run, `file_written` per run) and re-measured: 6 of 6 runs wrote the file (`perf/multi_rerun.json`) |
| G-59 | P2 | (final critic) the already-tried check missed the same control spelled differently: "Wi‑Fi" (U+2011, Apple's) against OCR's "Wi-Fi", or a trailing colon | **fixed** with the same normalisation; 2 checks, red first |
| G-60 | P2 (latent) | (final critic, reasoned) Stop revokes the grant but does not interrupt a click already past its permission check: the fresh look and the post go ahead | open; computer use is off by default and `act_on` has no production caller |
| G-61 | P3 | (final critic) a clarify question that ran into its 400-character bound with no sentence break was emptied, and an empty question made `understand` act on the request instead of asking | **fixed**: cut to the last whole word and closed with "?"; new suite `test_intake_bounds` (8 checks, the first tests G-48 had), red first (2 of 8) |
| G-62 | P3 | (final critic) open: the "Stopping…" label is local state that the next refresh erases; menu items are all tab stops, Tab does not close the menu, and `aria-current` stands in for `menuitemradio`/`aria-checked`; in a fan-out, two parts on one agent can overwrite each other's screen flag; any process importing `delegation_tool` rewrites `jobs.json` at import; the 10-minute retry is enforced only when Diablo next speaks or starts; contact aliases are written into `agent_tools` | open |

### Verification done outside the suites

- **Restart recovery** (real process, `kill -9` mid-job, temp store): before
  the kill, `running` + `queued` on disk; the next process marks both
  `interrupted` on disk at startup, and they stay final through a second
  restart. Diablo does not resume interrupted work; the person asks again.
  The first run's driver was lost with the scratch folder; it was rewritten
  as `scripts/restart_probe.py` and re-run on 2026-10-05 with the same
  result (`launch/restart_probe_2026-10-05.txt`).
- **Context stress** (`session.reply` with the 4B, planted fact, filler from
  the repo's own docs): prompt tokens 6,862 / 7,455 / 8,125 of 8,192 (84, 91,
  99 %), fact recalled every time, 18.6 / 2.5 / 21.3 s (2.5 s with the prefix
  cached). Target 110 %: 8,106 tokens received, silently truncated (G-49).
  Results: `docs/quality/gauntlet/perf/context_stress.json`; script:
  `scripts/context_stress.py`.
- **Second-display black captures**: not reproducible this session, because
  only the built-in panel is connected (1512x982 @2x) after the reboot. The
  earlier failures (regressions at `eb87e4e` and `03dec66`) were all on the
  built-in panel while it was the secondary display at x = -1512, zero-width
  capture, passing in every later run on the same day. Classified as probably
  environmental (panel asleep or lid state), not as a confirmed defect and not
  as fixed.
- **Live delegated research**: through `voice_bridge.process_command` with
  real routing, model and web search (`docs/quality/gauntlet/perf`): 3 of 3
  jobs reached `done` (38.9 s cold, 24.8 and 25.7 s warm). The acknowledgement
  and the result were both recorded in the two warm runs; in the cold run only
  one line had been recorded when the job turned final (the store is written
  just before the result is spoken and the tool stops at the final state), so
  delivery in that run is not verified. Accuracy of the research content was
  not judged. A failing speaker leaves the job `done` (`test_retention`,
  `test_delegation_lifecycle`).
- **Raycast** (Reference B): running on this Mac but its window is only shown
  by its hotkey or URL scheme, both of which drive a live application. Not
  inspected; every comparison with it remains **not verified**.

Every fixed finding above was reproduced by a failing check first:
`test_action_outcomes` 18 of 136 red before the A4 fixes, 138/138 after, and
six mutations of the fixes (drop on refusal, verdict after a yes, post-approval
already-tried, failure-word guard, task binding, exception wrapper) each turn a
check red. `test_retention` 64/64 (35 of 52 fail on the old code).
`test_cancel` 21/21. `test_ui_keyboard` 22/22 in a real WKWebView against the
real server (13 of 19 fail on the old app).

### End-to-end latency (round two)

`tests/perf_e2e.py`, through `voice_bridge.process_command` with real routing
and the real 4B, on this 18 GB M3 Pro. One cold run, then five warm runs
(two for research). Commands that would change the Mac (`open`, `osascript`)
are stubbed and recorded rather than run. Voicebox was down for both runs, so
"first audio" is macOS `say` rendering the whole first sentence to a file and
overstates the time to first sound. For the app and the tab, "complete" is
that rendering of the reply: the launch itself was stubbed. With five warm
runs, nearest-rank P95 is the slowest run; with two (research), P50 and P95
are the faster and the slower. Raw data: `perf/baseline.json` (21:22) and
`perf/final.json` (22:12, after G-48), with logs beside them; the multi-step
row was re-measured on the final tree after G-58 (`perf/multi_rerun.json`).

| Request | Warm P50 (P95) complete, baseline | Final | Time to first text, final | Cold, final |
|---|---|---|---|---|
| open an app | 1.04 (1.05) s | 1.09 (1.49) s | 0.00 s | 1.78 s |
| new browser tab | 2.60 (2.82) s | 2.74 (2.82) s | 0.25 s | 8.99 s |
| deterministic calculation | 1.03 (1.04) s | 1.07 (1.08) s | 0.01 s | 1.33 s |
| simple screen question | 39.6 (41.1) s | 4.70 (4.73) s | 1.06 s | 11.6 s |
| multi-step file task | not valid (G-58) | 12.4 (17.6) s, 6 of 6 wrote the file | 2.04 s | 20.8 s |
| delegated research | 24.8 (25.7) s | 18.6 (19.8) s | 1.03 s (was 7.79) | 32.5 s |

Model cold load: 2.45 s (baseline), 1.18 s (final). The direct operations
are under the 5 s target warm; "new tab" is not a fair entry, since it opens
the browser and no tab (G-46). The one fix made for speed is G-48: research is
acknowledged in 1.0 s instead of 7.8 s, and finishes about 6 s sooner. The
screen-question change is **not** a speed fix: every baseline run went
through the vision model (G-50), and every final run was answered from the
window list. The intake change in G-48 is the likely reason the path changed,
because the task now reaches the agent in the user's words. For "what app am
I looking at" that is the right tool, but a question about what a window
contains still pays the ~40 s vision cost. The first multi-step figures
(14.8 s baseline, 16.8 s final) are withdrawn: after the cold run every warm
run met the file it had already written, the confirm stub declined the
overwrite, and what was timed was a refusal (G-58). The re-measured warm
figure, 12.4 s, misses the 5 s target; it is three model turns on a 4B and
is not a direct operation.

### Launch incident (disclosed)

The first controlled launch of the scratch bundle ran the **live**
`~/.diablo/diablo_app.py` (G-52): the scratch `.env`, copied from the live
one, names `DIABLO_HOME`, and the launcher sourced it after setting its own.
For about a minute the live code wrote to the live install:

- `~/.diablo/trace.jsonl`: 16 rows (2,660 bytes) appended, carrying the old
  code's fields. Truncated back to exactly 1,894,133 bytes after asserting the
  size and the line boundary; the removed rows were not kept anywhere.
- `~/.diablo/visualizer/state.json`: rewritten. Same size (17 bytes); what it
  said before is not known. It holds only the orb's state word.

Nothing else in the live install changed (`jobs.json` 2 bytes, `voice_cache`
708 files, `diablo.log` 173,330 bytes, all checked). After the fix, every
launch ran a tripwire that compares the live install before, after startup
and after shutdown, and quits at once on any difference.

### Controlled build and launch

Built by `make_app` into a scratch folder (`$S/gauntlet/apphome2`) with its
own bundle id (`com.shaurya.diablo.gauntlet`), no LaunchServices
registration, against a scratch `DIABLO_HOME` whose `venv` links to the live
one. `~/Applications/Diablo.app` was not touched. Run log:
`launch/launch_2026-10-05.log` (10:04); the suite summary is
`launch/run_all_summary_2026-10-04.txt`.

- Bundle (`launch/bundle_2026-10-05.txt`): `codesign --verify` valid
  (ad-hoc), `Diablo.icns` present, `LSUIElement` true, the launcher sets
  `DIABLO_HOME` again after `.env` (lines 11 and 34). `.env` can still
  override `PY`, `LOG` and `PATH`. The bundle is a launcher script around the venv; it is not
  self-contained (G-44).
- First launch: server answering at 1.6 s, manager attached at 4.9 s, state
  `idle`, `degraded: []`. Brain, mic, wake word (push-to-talk only), screen
  and conversation ready; voice ready with "Voicebox unreachable, using macOS
  voice". Permissions reported in `/api/state`: accessibility, screen and
  microphone granted (`launch/state_2026-10-05.json`). The log also has one
  `ConnectionResetError` traceback, most likely the launch script's
  one-second readiness poll closing its connection early (not confirmed);
  the server carried on.
- `test_app` against the launched app: **53/53**.
- Core conversation: "what is 17 times 23" answered "That's 391, Sir."
- Screen understanding: "what app am I looking at right now" answered in 203
  characters naming the frontmost app, through `list_windows`, 11.6 s cold.
  The answer was not recorded here; G-53 was found by reading where it went.
- Escape and folding: not driven in the launched window (no synthetic
  keystrokes); covered by `test_ui_keyboard` in a real WKWebView against the
  real server.
- Error state: the voice fallback above is the one exercised live. Brain and
  network failures are covered by `test_brain_errors`, `test_model_failures`
  and `test_resilience`, not by this launch.
- Shutdown: `quit` over the control API; launcher exited, port closed, no
  scratch processes, 0 `diablo_*.png` in the temp folder.
- What the app wrote: `diablo.log` (lengths only after G-53), `trace.jsonl`,
  `visualizer/state.json` and `.env` (rewritten by `test_app`'s settings
  check, same size). `jobs.json` stayed `[]`, mode 0600. No `voice_cache` was
  created, because the fallback voice speaks directly; the Voicebox retention
  path is covered by `test_retention`, and by `test_voice` when Voicebox is up.
- Live install unchanged before, during and after: tripwire clean.

### Round two runs

First run (`w9`, before G-53 to G-61; superseded by the final run below):

`run_all.py`, 70 suites in 842 s: **66 passed, 4 failed**. Counting the
per-check marks the suites print, 4,096 checks passed, 12 failed and 68 were
skipped.

- `test_voice`: 9 of 23 failed, all because Voicebox was down after the reboot
  (environmental). It passed 23/23 with Voicebox up earlier in round two.
- `test_app`: no app on its port during the run. It passed **53/53** against
  the launched bundle above.
- `test_system`: 1 of 31. A window title contained the word "Error", and the
  check matched it anywhere in the output. The check now tests the prefix, as
  its two neighbours already did. Re-run 31/31.
- `test_scenarios`: 2 of 112, `URLError` reaching Wikipedia. Re-run 112/112.

After G-53 and G-54, the six suites touching `voice_bridge` were re-run:
`test_trace_privacy` 12/12, `test_fast_path` 72/72, `test_ptt_mic_gate`
30/30, `test_appkit_ptt_integration` 24/24, `test_mic_wake` 19/19 and
`test_phase3b_ownership` 25/25.

**Final run** (`w9` synced to the final working tree and checked with
`diff -rq`, 2026-10-05; summary `launch/run_all_summary_final_2026-10-05.txt`):
`run_all.py`, **71 suites in 686 s: 68 passed, 3 failed**. Counting the
per-check marks, 4,137 checks passed, 10 failed and 54 were skipped.

- `test_voice`: Voicebox down (environmental, as above).
- `test_app`: no app on its port during the run. It passed **53/53** against
  the bundle relaunched on the final tree (`launch/launch_final_2026-10-05.log`,
  10:49): first launch idle with nothing degraded, "what is 17 times 23"
  answered 391, the screen question answered (214 characters, via
  `list_windows`), clean shutdown, port closed, 0 captures left, live install
  unchanged. Its `diablo.log` shows `Diablo: <214 chars>` and
  `[Agent 1] list_windows()`, no content.
- `test_teaching`: 1 of 136, "the pointer is where it was before". The suite
  checks that the real mouse pointer did not move, and the Mac was in use
  during the run; re-run alone, 136/136. Recorded as environmental, not fixed.

Restart recovery and the multi-step latency were re-measured on 2026-10-05
(above). The perception benchmark with vision was not re-run in round two.

The image scan taken before the first run and after the final launch found
**0 new and 0 changed** images (1,803 before, 1,586 after). All 217 removals
are one browser's own update cache
(`Library/Caches/company.thebrowser.dia/…Sparkle`). The temp folder has 0
`diablo_*.png`. The live `trace.jsonl` is 1,894,133 bytes, its size before
the launches; it held that size after the truncation above and through both
later launches.
The pre-existing `~/Desktop/DIABLO_Screenshot_1790856822_main.png` (1 Oct,
17:43, 360 KB) was left in place, as the brief asks.

## Definitive run, round one (2026-10-01; superseded by round two)

- `run_all.py`: **58/58 suites passed** in 829 s (55 original + action
  outcomes, trace privacy, capture lifetime), model suites included.
- Perception benchmark with vision: 53/100 measurements good (no-vision
  sub-run earlier: 49/75 before and after the scorer change, 0 records
  changed).
- Image scan around it: **0 new, 0 changed** (2 removed = my own synthetic
  test renders from an earlier check, deleted deliberately); 0 `diablo_*.png`
  in the temp folder; real `~/.diablo/trace.jsonl` unchanged (1,889,850 bytes
  throughout the session).
- Workload trace: 402 rows, **0 content-bearing fields** (HEAD workload: 98).
  Agent stdout: 39 tool-result lines, all sizes or error wording.
- Not covered by the run: G-21..G-23 and later were re-verified by the
  affected suites, and all are in this definitive run.

## Gate status (round two, 2026-10-05)

| Gate | Status | Evidence | What is not proven |
|---|---|---|---|
| A core functionality | **pass in tested environments** | 71-suite run; launched bundle answered a calculation and a screen question; live research reached `done` 3 of 3 | live voice (push-to-talk with a person speaking) and live DO not exercised; research content accuracy not judged |
| B safety | **pass** | computer use off before and after every suite; fixtures and stubs only; no click or keystroke sent to a real app; G-36..G-41, G-56, G-59 fixed | G-60 (Stop during a click) and the lexical limit of G-56 open, both latent while computer use is off |
| C regression | **pass with three recorded environmental failures** | 68/71; `test_app` 53/53 against the launched app; `test_teaching` 136/136 alone | `test_voice` not re-run with Voicebox up after the final changes |
| Reference C privacy | **pass on what was checked** | retention enforced (G-27, G-28); `diablo.log` lengths only (G-53, G-55); image scan 0 new, 0 changed; files a launched app wrote read back | the trace records what the person said (by design, documented); a failed file operation names the file; `~/.diablo/command.wav` (April, a recorded utterance) predates the policy |
| D reliability and performance | **partly verified** | baseline and final latency, restart probe, context stress | multi-step task 12.4 s warm and vision questions ~40 s miss the 5 s target; context folding by turn count (G-49) |
| E UX | **partial** | G-33, G-34, G-35a fixed; `test_ui_keyboard` 22/22 in a real WKWebView | G-35b and G-62's UI items open; Escape was not driven in the launched window |
| F build and presentation | **partial** | bundle built and launched in isolation twice; README, release notes and demo guide corrected against measurements | no clean-Mac install (G-44); the bundle is a launcher around a venv |
| G independent review | **done, findings acted on** | final fresh-context critic across eight areas: 2 P1 and 3 P2 reproduced, 1 P2 reasoned; both P1s and the three reproduced P2s fixed, each with a failing test first | the critic did not re-review the fixes |
| Reference B (Raycast) | **not verified** | | not inspected; it can only be opened by driving a live app |

Diablo is **not release-ready**: G-44 means it cannot be installed on a clean
Mac, and gates D, E and F are partial.

## Skills used

- `gauntlet-loop` (set the reference bars: Cua contract + run_all, zero-persistence scan, Raycast).

## Next action

Make `tools/install.sh` set up a clean Mac (G-44): a fresh user account with
no `~/.diablo`, ending in the same controlled launch this round used. Until
that works, nobody but the owner can run Diablo, and no other gate changes
that.

## Round three (2026-10-05): model invocation, and the Nex-N2.5-mini brief

### What the brief asked, and what was decided

The brief asked whether Nex-N2.5-mini should become Diablo's visual and
agentic engine, and why the local 4B "sometimes does not run at all".

Nex-N2.5-mini, from its official README and model card: a 35B mixture of
experts with about 3B active per token, Qwen3.5-based, Apache 2.0, multimodal,
trained for computer and browser use, 262K context, three reasoning modes
(`reasoning_effort` none, medium, high), tool calls in the `qwen3_coder`
format, and an official deployment of SGLang on 2 x H100. Community GGUF and
MLX quants exist; the smallest (Q2_K) is 12.94 GB before vision weights and
KV cache. Hosted access is through OpenRouter.

Neither route was open without the owner. This Mac has 18 GB with about 1 GB
free in normal use, so a local quant means a 13 GB+ download and heavy memory
pressure. The hosted route needs an API key, of which there is none here, and
it sends screen content off the Mac, against Diablo's local-first rule. Both
are on the brief's own stop list. Asked, the owner chose **neither for now**.
Nex-N2.5-mini was therefore not run, and its classification is
**C, EXPERIMENTAL: not evaluated**. No benchmark number for it exists.

### Where "the model did not run" came from

Counted from `~/.diablo/trace.jsonl` (stage names and counts only):

- 593 turns from 2026-09-14 to 2026-10-02. **387 are test runs**: bursts of
  several turns in one second, each finished in under 2 ms with a stubbed
  model. The suites that wrote them did not set `DIABLO_TRACE`. Read naively,
  they make 28% of turns look like model failures.
- Of the 206 turns paced like a person, **6 (2.9%)** had a failed model call,
  each 4.5 to 38 s into the turn: real calls that failed. The trace recorded
  only the class name `BrainOffline`, so whether each was a timeout, a refusal
  or unusable JSON could not be told.
- **40 real turns (19%)** ended in a clarifying question that the 4B chose.
  Labelled by hand: 18 were right to ask (fragments, overheard speech,
  unintelligible). 22 were not: 5 pointed at the screen ("this graph"), 5
  were follow-ups needing conversation context, 6 were complete research or
  how-to requests asked "which type?", and 5 were plain questions.

So the main failure was not invocation. It was the intake model choosing to
ask, on a complete request.

### What was tried, measured and kept or reverted

The real cases stay in scratch, because they are the owner's words. The
committed benchmark is `evals/intake_cases.json` (39 cases in five groups) run
by `evals/run_intake_eval.py` through the production `route.classify`.

| Configuration | Clear real requests handled (turn) | Real fragments acted on | Synthetic: unnecessary clarify / false action | Research recall (`test_routing_live`) | Kept |
|---|---|---|---|---|---|
| Baseline 4B | 7 of 17 (measured with the chat-only override in place, which no real case reached) | 0 of 18 | 5 of 60 / 3 of 57 | 8 of 12 | |
| 14B, same prompt (control) | 9 of 17 at route level | 0 (12 fragments chatted) | not run | not run | no: 5.3 s per decision against 1.0 s |
| Prompt rewrite of "clarify" | 7 of 17 at route level | 0 | broad requests fell from 39 to 31 of 42 | not run | **reverted**: the 4B began asking "which type" of clean requests |
| `missing` field in the intake schema | 10 of 17 at route level | 0 | 3 of 60 / 3 of 57 | **5 to 6 of 12** | **reverted**: cost research recall |
| Separate clarify classifier | 9 of 17 handled | **7 of 18 acted on** | 3 of 60 / **10 of 57** | 8 of 12 | **reverted**: unsafe |
| Chat-to-research override, chat and clarify | 11 of 17 | 0 of 18 | unchanged | 8 of 12 | **narrowed**: broke `test_repeated_clarification`, whose part B is accepted behaviour (a research request may be asked "which news?" and "what dates?") |
| Final: override on chat only | 9 of 17 (the 2 over baseline are screen turns, not touched by the override: variance) | 0 of 18 | 4 of 60 / 3 of 57 | 8 of 12 | yes |

The 3 synthetic false actions in every row are one case, "rename the
document", escalated to the 14B by `route.classify` in the baseline too.
The first baseline report showed 0 because the eval counted only `act`; it
now counts `escalate` and `delegate` as acting as well.

Net: no measured improvement in over-clarification survived. The kept
override is a safety net: an explicit research request that the 4B calls chat
is researched, not answered from memory (`tests/test_research_override.py`,
3 of 8 red without it). On the real cases it moved nothing, because the 4B
labels those requests clarify, not chat.

### Fixed: what a model call leaves behind (G-63)

`local_brain` now writes one `model` row per call: purpose (the function
that called), model, outcome, call time, Ollama's load time and token counts,
and time to first token for a stream. It never writes content. Outcomes are
`ok`, `timeout`, `unreachable`, `refused`, `context_full`, `unreadable`,
`invalid_json` (a second row for a call that came back unusable), `stopped`
(a stream closed early) and `broken` (a connection lost mid-answer).
`test_model_failures` has 11 new checks, 10 red on the committed
`local_brain`, 48/48 after. On the live 4B, an `understand` call reads
`outcome ok, call_ms 601 to 1,620, load_ms 2 to 4, prompt_tokens about 610`.

Of the brief's failure classes A to I, these rows now separate D, E and F
(input not sent, timed out, invalid output). A (no model needed) was already
in the `decide` rows as `via: regex` or `phrasing`. H and I are the action
outcomes from rounds one and two. B and G (needed but not selected; a valid
answer ignored downstream) are **not** instrumented.

### Not done in round three, and why

- The rest of the Nex brief (vision, guidance, table and PDF workflows, hybrid
  configurations A to D, the Laya comparison) depends on running Nex, which
  was deferred.
- Test pollution of the live trace is recorded, not fixed: 387 of 593 turns.

Round three regression (`w9` synced to the final tree, 72 suites): **70
passed, 2 failed**, both environmental: `test_app` (no app on its port; not
relaunched after round three's changes) and `test_voice` (Voicebox down, the
same 9 of 23). The full run was stopped by its 40-minute limit inside
`test_voice`, after "it notices Voicebox is unreachable". That suite usually
takes 4 s. Re-run alone it took 4 s and did not hang, so the cause was not
diagnosed. The suites the stopped run had not reached (`test_brain` 28/28,
`test_agent` 18/18, `test_resilience` 20/20, `live_check` 11/11,
`live_screen_check` 19/19) were run separately and passed. New suites:
`test_research_override` 8/8, and `test_model_failures` now 48/48.

## Next action, after round three

Unchanged from round two: G-44, a clean-Mac install. Round three opened one
product question for the owner: should an explicit research request skip
scoping questions? Four of the real unnecessary clarifications were those,
and `test_repeated_clarification` part B currently accepts them.
