# Real-world gauntlet

Run 2026-10-06, 20:00 to 21:00, on the build machine. Three runs of
`evals/run_real_world.py` against the real applications on the Mac, through
`voice_bridge.process_command`, the function a spoken request reaches. Each
run started only after 240 s without keyboard or mouse input, and stopped the
moment either was used (run 2 was stopped early that way).

**Environment.**

| | |
|---|---|
| Mac | M3 Pro, 18 GB, Darwin 27.0 |
| Display | built-in 1512 × 982 at 2x (the external display was disconnected during this work) |
| Brain | qwen3.5:4b at 32K context |
| Grounder | GoClick-Large, behind `grounding_gate` |
| Installed and tested | Google Chrome, Zen, Safari, Final Cut Pro, System Settings, Finder, Notes, TextEdit, Calculator, Preview |
| Not installed | Photoshop, used as the negative case |

**What the driver may do.**
- Open applications, Settings panes, tabs and menus; point; read.
- Answer yes to a permission question only when it names nothing destructive; every question is recorded.
- Afterwards, quit only the test apps it launched itself.

Every result below is what the person heard, recorded verbatim in the run's
JSON. The JSON stays outside the repository, because it quotes real screens.

## The four reported failures

| Request | Before (live install, earlier today) | Run 1 (resolver only) | Run 3 (all fixes) |
|---|---|---|---|
| "what have I been working on the past few days?" | 50 s of invented history | "I don't know… I can't see your files" | no invention; offers to check recently changed files. **Partial**: the follow-up "yes, please check" was not carried out |
| "how do I color grade on Final Cut Pro, can you show me?" | 4 invented steps, 90 s | steps for the browser in front | Window › Workspaces › Color & Effects, from FCP's own menus. **Partial**: right place, but no mention of selecting a clip or adding a correction |
| "how do I open settings in the Zen browser? can you show me?" | gave up: no gear on screen | gave up | **"In Zen, it's Zen › Settings… (⌘,). I've marked the Zen menu."** 11.8 s |
| "can you show me how to connect a new Bluetooth device?" | taught the Voicebox window | invented "Settings link… account options" | opened System Settings at **Bluetooth**; "find your new device in the list, click its name". **Partial**: does not say to put the device in pairing mode |

## Run 3, every request

| Request | Result | Verdict |
|---|---|---|
| open safari / calculator / textedit / preview (were closed) | "Opened Safari." etc., 0.2–2.3 s | PASS |
| open google chrome / final cut / system settings / finder / notes / zen | "Brought … to the front.", 0.3–1.5 s | PASS |
| open pro | "Did you mean DockDoor Pro or Final Cut Pro?" | PASS |
| open photoshop | "not installed on your Mac", after two irrelevant questions, 45 s | FAIL (latency, questions) |
| show me how to open a new note (Chrome in front) | "In Notes, it's File › New Note (⌘N). It's also on screen: I've marked New Note." | PASS (found the right app on its own) |
| show me how to add subtitles in Final Cut Pro | "I think it's File › Import › Captions…" | FAIL; fixed after the run (see below) |
| show me where settings are (Chrome) | right route, but marked a "Settings – Passwords" bookmark | FAIL; fixed after the run |
| open a new tab on Zen | chose File › New Tab once (the repeat guard held); said it could not confirm the change; asked an unneeded question first | PARTIAL |
| open chrome, then "open a new tab" | asked which browser, then opened tabs in Chrome and in Zen | FAIL (wrong app) |
| what am I looking at? | accurate description of the window | PASS |
| what does this say? / "that's not what I meant" | summarised rather than read; the follow-up got a settings route | FAIL (follow-up) |
| what have we done so far today? | accurate summary of the session, slightly overstated ("set up Bluetooth connections") | PASS, with a caveat |

Final Cut Pro's follow-ups ("the actual color wheels", "where is that?",
"that's not what I see") kept FCP as the subject and led to Window › Go To ›
Color Inspector, though once they said "View menu". They are judged with
their request above.

**Counts, run 3: 24 requests; 15 PASS, 4 PARTIAL, 5 FAIL.** Useful-task
accuracy is 62.5% strict, or 79% if partial answers count as useful. Both are
below the 95% target.

## Fixed after run 3, verified read-only against the real menus

The route finder was changed in two places. It now matches "subtitles" to the
word "captions". And when the request's own words match some commands, the
model chooses only among the best 8 of those, never all 522 of Final Cut
Pro's. The routes were then checked again against the real running apps (no
action taken):

| Request | Route |
|---|---|
| add subtitles in Final Cut Pro | Edit › Captions › Add Caption (⌥C), matched by name |
| color grade / color wheels | Window › Go To › Color Inspector (⌘6), chosen by the model |
| add a title | Edit › Connect Title › Basic Title (⌃T), chosen by the model |
| export my video | no route, instead of the wrong File › Import › Media… |
| Zen history / settings | Show All History (⌘Y) / Zen › Settings… (⌘,) |
| Chrome downloads / new tab | Window › Downloads (⇧⌘J) / File › New Tab (⌘T) |

The on-screen marker now needs the control's name to say the same thing as
the command, so "Settings – Passwords" is no longer marked for "Settings…".
These fixes have not been through a further end-to-end run.

## Latency, run 3

| Request kind | n | P50 | P95 |
|---|---|---|---|
| open or bring up an app | 12 | 0.3 s | 1.9 s |
| how-to / show-me | 6 | 26.1 s | 75.0 s |
| screen question | 2 | 25.6 s | 28.8 s |
| menu action (new tab) | 2 | 34.6 s | 35.2 s |
| memory question | 2 | 6.3 s | 8.4 s |
| follow-up turn | 5 | 34.5 s | 49.0 s |
| unknown app (Photoshop) | 1 | 45.0 s | |

First words came in a median 0.1 s for how-tos ("One moment, Sir.") and 6.1 s
for screen questions. Only opening apps meets the ~5 s target. How-tos are
slow because the walkthrough captures and reads the window, plans with the
model, and then waits on each step.

## Safety and privacy

**Wrong presses.** In run 1, the harness answered a clarifying question
"next", and Diablo heard it as "skip to the next track" and skipped a music
track. That was a harness defect; it now answers clarifications with silence.
In run 3, opening tabs in both Chrome and Zen was a wrong-app action. Nothing
destructive was ever offered or approved; every approval was File › New Tab.

**Screenshots.** No image file was written by Diablo in any run. The places
scanned were: Desktop, Downloads, Documents, ~/Library/Caches, /private/tmp,
the system temp folder, the run's own home, ~/.diablo and the repository.

**State left behind.** The apps launched for the test were quit. A few new
browser tabs were not closed. System Settings was left open once.

**The live install.** `~/.diablo/.env` was rewritten once, at 20:44, by the
pre-existing `tests/test_app.py`. That suite posts to whichever Diablo app is
running, and the live one was. Its settings checks change a dial and restore
it. Every comparable value is unchanged, but it was a write to the live
install. `test_app.py` now makes those checks opt-in
(`DIABLO_TEST_WRITES_SETTINGS=1`).

## Regression layer

The full suite after run 3 passed 76 of 79 suites:
- `test_screen` and `live_screen_check` saw a sleeping display (black captures); both pass with the display awake.
- `test_conversation_teaching` failed on one check, a follow-up rule that ignored conversation boundaries. That is fixed.

After the last fixes, the 24 suites these changes touch all pass. The new
suites:

| Suite | Checks |
|---|---|
| `test_app_resolution` | 61 |
| `test_app_menus` | 27 |
| `test_settings_panes` | 15 |
| `test_task_context` | 23 |
| `test_user_safe` | 22 |

`live_screen_check` still fails 1 of 19 checks: a conversational reply over
45 words. It's model verbosity and varies between runs.
