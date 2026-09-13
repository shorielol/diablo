# Diablo

A voice assistant for macOS that does the work, and never phones home.

Diablo listens for "Hey Diablo", works out what you meant, and then actually does it — reads the file, runs the numbers, moves the folder, reads the window you are looking at. The model is a 14B running in Ollama on your own machine. No API keys, no account, no request leaving the laptop.

It is built around one idea: an assistant that only talks is a toy. Diablo has 33 tools and an MCP client, so when you ask it to total a spreadsheet it opens the spreadsheet.

```
you    > hey diablo, what youtube video am i watching
diablo > The video is "How a Minecraft SMP Drove Me Insane".

you    > hey diablo, total the revenue in sales.csv and put it in total.txt
         [read_file]       item,units,price / pens,10,2 / books,3,15 / bags,2,25
         [python_snippet]  115
         [write_file]      Wrote total.txt
diablo > That comes to 115. It's in total.txt in the same folder.
```

## Why local

Running a 14B on a laptop is slower than calling a frontier model, and the trade is worth making for this particular job. A voice assistant hears everything said near it. Mine sees my coursework, my business files and whatever is on my screen at the time. That is not data I want to post to a third party in exchange for a better paraphrase.

The practical cost is about 1.9 seconds for a warm tool-calling turn and 9.3 GB of memory while the model is resident. Ollama releases it after ten idle minutes, and Diablo starts reloading the moment the wake word fires, which hides most of the cold start behind the time you spend talking.

## Requirements

- macOS 13 or later, Apple silicon
- Python 3.10+ (3.14 recommended; the MCP client needs 3.10 for its typing)
- [Ollama](https://ollama.com) with `qwen3:14b` pulled
- [Voicebox](https://voicebox.dev) for speech, optional — without it Diablo falls back to the macOS system voice
- Node 18+ if you want the MCP servers

Smaller models were tested and did not work. `qwen3:4b` took 9.97 seconds and emitted no tool call at all, so it was both slower and less capable than the 14B on the only task that matters here.

## Install

```bash
git clone https://github.com/YOUR_USERNAME/diablo.git
cd diablo
./install.sh
```

The installer builds a venv at `~/.diablo/venv`, copies the modules into `~/.diablo`, writes `~/bin/diablo`, and builds `Diablo.app`.

That last part matters more than it looks. macOS grants microphone access to the *responsible process*, and for a shell command that is your terminal — which is why access appears to come and go depending on which terminal you used, and vanishes entirely when permissions are reset. Launched as an app, Diablo owns the grant itself.

Start it from Finder the first time so macOS has somewhere to show the prompt:

```bash
open ~/Applications          # then double-click Diablo, and click Allow
```

After that, `diablo` works from anywhere.

## Using it

```
diablo            start listening, and follow the log
diablo stop       shut it down
diablo status     is it alive
diablo doctor     find what is broken, and fix what can be fixed
diablo mic        re-probe the microphone and pick a working device
```

Say "Hey Diablo" and then your request, or say the whole thing in one breath. One breath is faster: the wake word is detected by transcription rather than by a trained model, so the sentence arrives whole and there is no second round of listening.

## What it can do

**Files.** Read, write, append, move, find, search inside. Deletes go to the Trash, never `unlink`.

**Real work.** Shell commands, Python snippets, spreadsheets through pandas and openpyxl. Arithmetic always goes through Python, never through the model, because a 14B asked to total three rows in its head returned 350 for a figure that was 115.

**Your screen.** Diablo reads one window at a time using Apple's Vision framework, with a 2D occupancy grid that finds the real column boundaries. Reading per-window rather than whole-screen is what keeps the browser's text out of the editor's, which is the difference between screen context being useful and being noise. Password managers and banking windows are refused outright.

**Conversation.** It holds a thread while you work, remembers twelve turns, and knows what is on your screen when you refer to it.

**Anything else.** An MCP client speaks the protocol directly over stdio, so any MCP server works without writing Python. Tool schemas are flattened first, because a 14B does not reliably fill in a nested `$ref`.

## Safety

Diablo acts on speech, and speech is misheard. "Delete the old drafts" and "delete the older drafts" are one phoneme apart. Three defences:

1. **A confined filesystem.** Desktop, Documents, Downloads and one project folder. Traversal, symlinks and absolute paths out are all refused. There are 44 tests on this boundary alone.
2. **A shell denylist.** 19 patterns, including `rm -rf`, `sudo`, `dd`, `mkfs`, `diskutil erase`, curl piped to a shell, fork bombs and `git push`.
3. **Spoken confirmation** before any delete, overwrite, message or shell command.

Deletes go to the Trash so a misheard sentence stays recoverable.

## Character

Diablo has dials, set in `.env`:

```
DIABLO_HUMOUR=2      # 0 none, 1 dry, 2 wry, 3 cheeky
DIABLO_WARMTH=1
DIABLO_FORMALITY=2
DIABLO_ADDRESS=Sir
```

Humour lives in conversation and never in a task report. When Diablo has just edited a file in your coursework folder, the only thing you want back is exactly what it did, so that report stays flat whatever the dial says.

It will also never tell you it has done something it has not done. That sounds obvious; it took three rewrites. An earlier version answered "can you move my launch date to the 23rd" with "I've moved the launch date to the 23rd, and sent a revised timeline to the team." None of it happened. Conversation mode has no tools at all, and the prompt now says so in those words.

## Tests

```bash
python3 tests/run_all.py
```

Thirteen suites, 297 checks. They use the real model, the real filesystem and a real Trash rather than mocks, because the failures worth catching are the ones a 14B produces and a scripted stand-in never will: narrating a plan instead of running it, calling the same tool forever, or reporting success having done nothing.

Every agent test checks the disk afterwards, not what Diablo said about the disk.

## Architecture

```
voice_bridge.py      the service: audio in, wake detection, dispatch
diablo/
  wake_word.py       rolling-window detection by transcription
  mic.py             device selection by evidence, endpointed capture
  agent_core.py      understand, plan, ReAct loop, clarify
  agent_tools.py     the 33 tools, path confinement, denylist
  local_brain.py     Ollama client, stdlib only
  mcp_client.py      JSON-RPC 2.0 over stdio, no SDK
  screen_context.py  per-window OCR, column detection
  conversation.py    rolling memory, anti-fabrication
  persona.py         the character dials
  voice.py           Voicebox, disk cache, system fallback
```

## Licence

MIT. See [LICENSE](LICENSE).
