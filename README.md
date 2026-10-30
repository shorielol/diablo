# Diablo

An offline voice assistant for macOS that does the work on your own machine, and never phones home.

![platform](https://img.shields.io/badge/platform-macOS%2013%2B%20%C2%B7%20Apple%20silicon-black)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![model](https://img.shields.io/badge/model-qwen3.5%3A4b%20via%20Ollama-8A2BE2)
![license](https://img.shields.io/badge/license-MIT-green)

Hold Control+Option, say what you want, let go. Diablo works out what you meant and then does it: reads the file, runs the numbers, moves the folder, opens the right Settings pane, reads the window you are looking at. One model runs all of it in Ollama on your own Mac. That model is `qwen3.5:4b`, and it plans, answers and reads images. A second, much smaller one, GoClick-Large, is optional and has a single job, which is finding controls on screen. No API keys, no account, and nothing leaves the laptop apart from the web searches you ask for.

It is built around one idea: an assistant that only talks is a toy. Diablo has 50 tools and an MCP client, so when you ask it to total a spreadsheet it opens the spreadsheet.

```
you    > what youtube video am i watching
diablo > The video is "How a Minecraft SMP Drove Me Insane".

you    > total the revenue in sales.csv and put it in total.txt
         [read_file]       item,units,price / pens,10,2 / books,3,15 / bags,2,25
         [python_snippet]  115
         [write_file]      Wrote total.txt
diablo > That comes to 115. It's in total.txt in the same folder.
```

## Status

Diablo is a personal project and a work in progress. The current build is a 2.1 release candidate ([release notes](docs/RELEASE_NOTES.md)) and has only ever run on one Mac, an 18 GB M3 Pro. `tools/install.sh` cannot set up a clean Mac yet, so for now Diablo runs from an existing `~/.diablo` install, and this repository is more useful to read than to install.

On test fixtures it holds up well. On real apps it is not there yet. The last real-world run went through Chrome, Zen, Safari, Final Cut Pro, System Settings, Finder, Notes, TextEdit, Calculator and Preview, and scored 62.5% strict, or 79% if partial answers count. My target is 95%. What is still failing is listed in [the real-world report](docs/benchmarks/real_world_gauntlet.md).

## Why local

Running a model on a laptop is slower than calling a frontier model, and for this particular job the trade is worth making. A voice assistant hears everything said near it. Mine sees my coursework, my business files and whatever happens to be on my screen. I don't want to send that to a third party in exchange for a better paraphrase.

Here is the practical cost, measured on the M3 Pro in October 2026. A warm agent turn takes about a second (P50 0.94 s for a 2,736-token prompt). Ollama lets the model go after ten idle minutes, and the first request after a break pays a cold load of about 12 seconds. A question about an image of the window takes 6 to 11 seconds, depending on what is asked. Resident, the model uses 4.3 GB at a 32K context, and the GoClick worker adds 1.7 GB while it is running. Every number is in [the final model gauntlet](docs/benchmarks/final_model_gauntlet.md).

## Requirements

- macOS 13 or later, on Apple silicon
- Python 3.10+ (3.14 recommended; the MCP client needs 3.10 for its typing)
- [Ollama](https://ollama.com) with `qwen3.5:4b` pulled. `tools/install.sh` pulls it if it is missing.
- GoClick-Large (0.8B), optional. `tools/install.sh --grounder` fetches its pinned weights into `~/.diablo/models/goclick-large`, checks their sha256, and installs `mlx-vlm` to run them. Without it, Diablo finds controls through accessibility, on-screen text and qwen3.5.
- [Voicebox](https://voicebox.dev) for speech, optional. Without it Diablo falls back to the macOS system voice.
- Node 18+ if you want MCP servers

The brain has a history. Until 2026-10-05 it was `qwen3:4b-instruct-2507`, with Qwen3 14B for hard requests and Qwen2.5-VL for images. The 14B went once switching escalation off turned out to change one intake decision in 117, and qwen3.5 reads images itself, so one model now does all three jobs. The very first 4B tried, `qwen3:4b` with thinking on, took 9.97 seconds and emitted no tool call at all. The mistakes a 4B still makes (picking one of two files, reporting a change it never made) are caught by guards in code rather than by prompting, which is what "Why it does not make things up" below is about.

## Install

**Not yet packaged for a clean Mac.** `tools/install.sh` predates the current layout. It expects an existing `~/.diablo`, copies modules from the wrong folder and patches a bridge that no longer exists. Until it is rewritten, Diablo runs from an existing install at `~/.diablo`, and `diablo/make_app.py` builds `Diablo.app` around it:

```bash
~/.diablo/venv/bin/python3 diablo/make_app.py    # writes ~/Applications/Diablo.app
```

Building the app matters more than it looks. macOS grants microphone access to the *responsible process*, and for a shell command that process is your terminal. That is why access seems to come and go depending on which terminal you used, and disappears completely when permissions are reset. Launched as an app, Diablo holds the grant itself.

Start it from Finder the first time so macOS has somewhere to show the prompt:

```bash
open ~/Applications          # then double-click Diablo, and click Allow
```

After that, `diablo` works from anywhere. Copy `.env.example` to `~/.diablo/.env` to change the model, the voice or the character dials.

## Using it

```
diablo            start listening, and follow the log
diablo stop       shut it down
diablo status     is it alive
diablo doctor     find what is broken, and fix what can be fixed
diablo mic        re-probe the microphone and pick a working device
```

Hold Control+Option while you speak and let go when you are done. The microphone belongs to that hotkey, so nothing you say reaches Diablo unless the keys are held, and there is no always-on listening. The old wake-word mode is still in the code as a rollback and stays off unless `DIABLO_PTT_ONLY=0`. You can also type into the window from the menu bar.

Long work runs in the background and shows up in the window with a Stop button. A stopped task finishes the step it is on, starts nothing new, and its result is not read out.

## What it can do

**Files.** Read, write, append, move, find, search inside. Deletes go to the Trash, never `unlink`.

**Real work.** Shell commands, Python snippets, and spreadsheets through pandas and openpyxl. Arithmetic always goes through code and never through the model, because a model asked to total three rows in its head returned 350 for a figure that was 115, and the 4B later managed 135, 125 and 111 for the same sum.

**Apps and menus.** "Open chrome" resolves against the apps that are actually installed, whether you say a name, an alias, initials like "fcp" or a typo, and Diablo asks when two apps match. For a how-to question it reads the app's own menus and the 52 panes of System Settings instead of guessing. Asked how to open settings in the Zen browser, it answered "In Zen, it's Zen › Settings… (⌘,). I've marked the Zen menu." in 11.8 seconds. Before it could read menus it gave up on that question, because there was no gear icon on screen.

**Your screen.** Diablo reads one window at a time with Apple's Vision framework, using a 2D occupancy grid to find the real column boundaries. Reading per window keeps the browser's text out of the editor's, and that is the difference between screen context being useful and being noise. Password managers and banking windows are refused outright, and so is Diablo's own interface.

Every capture records where it came from and when, meaning which display, at what scale factor and how many seconds ago, and most of what follows depends on that. Diablo knows which monitor a window is on and maps a coordinate correctly onto a Retina panel sitting at a negative x. It checks that each capture is a screen at all before reading it, because a window on another desktop captures as a perfectly valid PNG of solid black, and "there is no text in this window" is a confident wrong answer nobody can see is wrong. It can read just the patch around your pointer, tell you what is under the cursor, and say what has changed since it last looked. When the words on screen cannot answer the question (what a chart shows, which button is greyed out), the window goes to qwen3.5 as an image at a capped resolution.

**Conversation.** It holds a thread while you work and knows what is on your screen when you refer to it. Past about twelve exchanges the oldest half is folded into a short third-person note and carried forward. It used to be dropped outright, which meant that twenty minutes in, "what did I ask you about first" had no answer and the model would make one up.

**Anything else.** The MCP client speaks the protocol directly over stdio, so any MCP server works without writing Python. Tool schemas are flattened first, because a small model does not reliably fill in a nested `$ref`.

## Clicking, and the gate in front of it

Looking is not touching. Clicking and typing sit behind a separate switch that is off by default. A "yes" covers one click, in the task it was given for, and anything that cannot be undone gets asked about again.

Every click goes through `grounding_gate`, and `click()` will not take a target the gate did not issue. Whatever proposed the point, whether accessibility, on-screen text, GoClick or qwen3.5, the gate checks it against the live window before anything is pressed. The control has to exist, be enabled, carry the label that was asked for, be the only control with that label, sit in the window that was asked about, and still be where it was when Diablo looked. An unnamed control is never pressed on a model's word, so Diablo asks first. Typing is refused when no text field has focus, and the field is read back afterwards to confirm what landed in it.

On live fixture windows the gate gave the right verdict in 18 of 18 cases. Those included a point on Delete All when the target was Save, targets that moved, vanished or were disabled after the look, and another window dragged over the target.

## Safety

Diablo acts on speech, and speech gets misheard. "Delete the old drafts" and "delete the older drafts" are one phoneme apart. There are three defences:

1. **A confined filesystem.** Desktop, Documents, Downloads and one project folder. Traversal, symlinks and absolute paths out of them are all refused, and 44 tests cover this boundary alone.
2. **A shell denylist.** 19 patterns, including `rm -rf`, `sudo`, `dd`, `mkfs`, `diskutil erase`, curl piped to a shell, fork bombs and `git push`.
3. **Spoken confirmation** before any delete, overwrite, message or shell command.

Deletes go to the Trash, so a misheard sentence can still be undone.

## What it keeps

Being local only helps if the machine itself does not fill up with what Diablo has seen and said. So, by default:

| What | Kept |
|---|---|
| Screen captures | Never. The code that takes a capture reads it and deletes it, and anything a crash leaves behind is removed the next time the screen starts. |
| Screenshots | Only when you ask for one ("take a screenshot", "save a screenshot to my Downloads"), and only in the folder you named, or the Desktop. |
| Spoken answers | Deleted once played. A line nobody heard is kept for up to ten minutes in case of a retry. Only Diablo's fixed phrases ("On it, Sir.") are kept as audio. |
| Task results (`jobs.json`) | What ran, when, and how it ended. An answer read off the screen is never written. Any other answer is cut to 32 KB and deleted after 24 hours. Readable by your user only. |
| `trace.jsonl`, `diablo.log` | Decisions, sizes, timings and errors, and only the kind of error where the message could carry content. Never the text read off the screen, a tool's output or arguments, or an answer. The log shows how long a spoken line was, and the words only when Diablo runs in a terminal. The trace does record what you said to Diablo, and a failed file operation names the file. |

The checks behind each row live in `tests/test_retention.py`, `tests/test_capture_lifetime.py` and `tests/test_trace_privacy.py`, and they read the files Diablo actually writes.

## Who does the work

Diablo does. Questions, conversation, arithmetic, short scripts, lookups, opening apps, media, messages and one-step automation all run on the main thread, and none of it ever creates an agent.

An agent shows up for exactly two reasons: you asked for one ("put an agent on this"), or the request is a substantial piece of research. The work then runs in the background under one of five names, Asmo, Marbas, Vassago, Seere and Levi. They come from the Ars Goetia and were picked for what each demon was said to do rather than for sounding sinister. A name carries a voice and an accent colour and nothing else. Each agent reaches the same 50 tools through the same permission gate, and `agent_core.run` takes no agent id, so the name on a job cannot change what that job is allowed to do. `tests/test_architecture.py` checks all of this.

When Diablo is unsure, it keeps the job. A wrong delegation costs a pointless task, an announcement and a row in the window. A wrong "do it here" costs nothing, since Diablo could do the work anyway, so the fallback in `delegation_tool.decide()` is direct, and it is written in code. Mixed requests are routed per operation. "Lower the volume and research this company" turns the volume down straight away and hands off the research, because judging the sentence as a whole kept losing one half or the other.

## Character

Diablo has dials, set in `.env`:

```
DIABLO_HUMOUR=2      # 0 none, 1 dry, 2 wry, 3 cheeky
DIABLO_WARMTH=1
DIABLO_FORMALITY=2
DIABLO_ADDRESS=Sir
```

Humour belongs in conversation and never in a task report. When Diablo has just edited a file in your coursework folder, all you want back is exactly what it did, so that report stays flat whatever the dial says.

It will also never tell you it has done something it has not done. That sounds obvious, and it took three rewrites. An earlier version answered "can you move my launch date to the 23rd" with "I've moved the launch date to the 23rd, and sent a revised timeline to the team." None of that happened. Conversation mode now has no tools at all, and the prompt says so in those words.

## Why it does not make things up

Ask a small model to edit "the draft" when two exist and it will pick one. Ask it to read a file and it will sometimes report contents it invented while the real ones sit right there in its context. Prompting fixed neither, so guards in code do, and each one holds whatever model sits behind it:

| Guard | What it stops |
|---|---|
| `_ambiguous_target` | editing one of several files you did not name |
| `_claims_unperformed_action` | reporting a change no tool made |
| `_quotes_something_unread` | quoting file contents no tool returned |
| `_parent_must_exist` | inventing a folder from a misheard name |
| `_is_really_a_question` | ending the turn on a question it forgot to ask |
| `_invents_a_status` | reporting the state of something it cannot see |

That last guard is why "how is Brew and Critics doing" gets "no idea, want me to check?" instead of a confident paragraph about a backend Diablo has never touched. A separate guard refuses to report a number that no code computed.

Each guard in code let me delete a prohibition from the prompt. The conversation prompt went from 924 words with 40 prohibitions to 515 words with 16, and that is most of the reason Diablo stopped sounding defensive.

## Measured

All of these ran on the build machine on 2026-10-05 and 06. The harnesses are in `evals/` and the fixtures in `tests/fixtures/`.

| Benchmark | Result |
|---|---|
| `grounding_gate`, live fixture windows | 18/18 correct verdicts |
| Finding controls, shipped pipeline (25 cases, light and dark) | 23/25 correct, 0 wrong targets, P95 1.3 s |
| The same with accessibility and OCR only | 19/25 |
| Intake, 39 requests × 3 | 0 of 57 false actions, 13 of 60 unnecessary questions |
| Planner, 118 decisions | 75.4% advancing |
| Multi-step tasks with real clicks (5, 7 and 10 steps) | all three on the sixth run: 16 s, 33 s, 56 s, no wrong presses |
| Real apps, run 3 | 62.5% strict, 79% with partials |

The multi-step result took six runs, and every fix along the way was deterministic: no new model and no prompt tuning. Run 5 has the only dishonest completion in the whole gauntlet. The 10-step goal named only its last save, so the engine reported done with half the work undone, and goals can now require several outcomes. GoClick is weaker on native macOS chrome than on fixtures, landing inside the right control 6 times out of 16. The failure log with every finding is [docs/quality/gauntlet/GAUNTLET_LOG.md](docs/quality/gauntlet/GAUNTLET_LOG.md).

## Tests

```bash
diablo test        # every suite; --fast skips the ones that use the model
diablo eval        # score it on clear, ambiguous, risky and impossible asks
diablo bench       # latency, throughput and memory, per model
diablo trace       # why it decided what it decided, turn by turn
diablo state       # what every subsystem is doing right now
```

`tests/run_all.py` runs 79 suites, 10 of which call the model. They use the real model, the real filesystem and a real Trash rather than mocks, because the failures worth catching are the ones a small local model produces and a scripted stand-in never will: narrating a plan instead of running it, calling the same tool forever, or reporting success after doing nothing. Every agent test checks the disk afterwards rather than what Diablo said about the disk. A few suites need something running outside them (`test_voice` needs Voicebox, `test_app` needs Diablo on its UI port, `test_scenarios` reaches Wikipedia), and when that thing is down the suite fails instead of skipping, on purpose. The suites that open apps or use the real microphone only run when `DIABLO_TEST_INTRUSIVE=1` or `DIABLO_TEST_DEVICE=1` is set.

`diablo eval` is the other half. The suites answer "is it broken", and the eval answers "is it any good" across seven categories: clear, ambiguous, impossible, invented path, tool failure, risky, and should escalate. Hallucination is judged from the filesystem and a list of forbidden phrases, never by asking the model whether it was honest, since the whole failure mode is confidence while wrong.

## Architecture

```
companion_manager.py   the state machine: owns every subsystem, one health view
voice_bridge.py        the service: push-to-talk capture, reflexes, dispatch
diablo/
  agent_core.py        understand, plan, ReAct loop, clarify
  agent_tools.py       the 50 tools, path confinement, denylist
  delegation_tool.py   the one place that decides whether work gets an agent
  do_engine.py         multi-step work on screen: plan, act, verify, recover
  local_brain.py       Ollama client, stdlib only
  mcp_client.py        JSON-RPC 2.0 over stdio, no SDK
  apps.py              which app you meant, from what is installed
  app_menus.py         an app's own menu commands, routes and shortcuts
  settings_panes.py    the System Settings panes, discovered from extensions
  task_context.py      how-to answers built from real menu routes
  screen_context.py    per-window OCR, column detection, image routing
  screen.py            captures that know their own provenance, and what changed
  display.py           the monitors, and the arithmetic between coordinate spaces
  grounding_gate.py    the checks every click target has to pass
  goclick.py           the optional GoClick-Large worker, on MLX
  computer_use.py      permission to click, and acting on gate-issued targets
  pointing.py          finding things on screen by measured coordinates
  overlay.py           the on-screen marker, drawn in its own process
  conversation.py      rolling memory, anti-fabrication
  persona.py           the character dials
  voice.py             Voicebox, disk cache, system fallback
  mic.py               device selection by evidence, endpointed capture
  wake_word.py         wake word by transcription (rollback only, off by default)
app/                   the window, one WKWebView with vendored React
evals/                 benchmark harnesses: intake, planner, perception, real world
tests/                 the suites, and run_all.py to run them
research/laya_diablo/  the routing dataset and a fine-tuning experiment
docs/                  architecture notes, benchmarks, release notes, the gauntlet log
```

`companion_manager.py` is the one place that knows what Diablo is doing. It owns the lifecycle of the mic, voice, brain, screen and conversation, runs an explicit state machine (`cold → init → idle → listening → processing → responding`), and emits events anything can subscribe to:

```python
from companion_manager import create_manager

manager = create_manager()
manager.on("state_changed", lambda e: print(e["old_state"], "->", e["new_state"]))
manager.start()
print(manager.health())
```

```bash
python3 companion_manager.py     # full diagnostic report
```

Invalid state transitions are logged and refused rather than raised. A voice assistant that throws on an odd transition goes silent, and silence is the one failure with no symptom.

## Credits

Diablo stands on [Ollama](https://ollama.com), Qwen3.5 from the Qwen team, GoClick-Large for grounding, Apple's Vision and Accessibility frameworks, [Voicebox](https://voicebox.dev) for speech, and Whisper for transcription.

## Licence

MIT. See [LICENSE](LICENSE).
