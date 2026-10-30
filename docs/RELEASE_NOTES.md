# Release notes

## 2.1 candidate (October 2026, not released)

This build is a release candidate in progress, not a release. It has not been installed over a working copy, and the installer for a clean Mac is still the old one (see "Known gaps"). Everything below was checked by the gauntlet logged in `docs/quality/gauntlet/GAUNTLET_LOG.md`.

### What Diablo keeps, and for how long

Diablo used to keep far more than it needed. Spoken answers were cached as audio forever (708 files on the development machine), finished tasks kept their answers in `jobs.json` with no limit, and the model could save a screenshot of every display to the Desktop on any turn. Now:

- Only Diablo's fixed phrases ("On it, Sir.") are kept as audio. Any other line is deleted once it has been played. If nobody heard it, it is kept for a retry for ten minutes at most. The old cache is cleared the first time this build starts.
- `jobs.json` keeps what ran, when and how it ended. An answer that came from reading the screen is never written to it. Other answers are cut to 32 KB and deleted after 24 hours. The file is readable by your user only.
- A screenshot is saved only when you ask for a new one, and only where you ask ("save a screenshot to my Downloads"). Talking about one that already exists ("what's in the screenshot I just took") does not take another. Looking at the screen to answer a question captures, reads and deletes; it never leaves a file.
- The decision log (`trace.jsonl`) and `diablo.log` record sizes, timings, decisions and errors, with only the kind of error where its message could carry content. They do not record the text Diablo read off your screen, what it said, or what a tool was handed. Until this build, `diablo.log` kept every spoken reply in full, screen answers included, and the arguments of every tool call. The trace still records what you said to Diablo.

### Stopping things

Every running or queued task has a Stop button. A stopped task finishes the step it is on and starts nothing new, and its result is not read out. A task that was cut off by a crash or a restart is marked interrupted; Diablo does not resume it, so ask again if you still want it.

### Clicking, when you have turned it on

Computer control is still off by default. When it is on:

- A "yes" covers one click, in the task it was given for.
- A result counts as confirmed only when a new line on screen names it and nothing else on that line says it failed or has not happened yet. "Message not sent", "couldn’t be saved" and "Sent — tap to retry" no longer confirm anything. This is still matching words, not watching the effect: a line that uses the expected word in another sense ("Turn on Bluetooth" for "on") can confirm it.
- A control that has already been pressed without the wanted result is not pressed again, even if it is found a different way the second time.
- A task that hits an error stops and says so, instead of blocking the next request.

### The window

- Arrow keys, Home and End move through the menu.
- Escape closes the menu and returns focus to its button.
- The "needs you" marker is amber, so it no longer looks like keyboard focus.
- A queued task shows up at once instead of at the next refresh.

### Faster

The intake step that decides what a request means could spend 7.4 seconds writing a long, invented brief for a one-line research request, hit its length limit and throw the result away. Its output is now bounded by the grammar, and the same call takes about 1.3 seconds.

### Fixed for other machines

Three modules had this machine's home folder written into them, so a copy installed anywhere else read and wrote the original. They now follow `DIABLO_HOME`.

### Known gaps

- `tools/install.sh` cannot set up a clean Mac. It assumes an existing `~/.diablo`.
- "Open a new browser tab" opens or focuses the browser but does not open a tab.
- A screen question that needs the vision model takes about 40 seconds on an 18 GB Mac. Ollama holds one model at a time there, so the swap to the vision model and back costs 7 to 11 seconds of that.
- A long conversation is folded by number of turns, not by size. Very long turns can overflow the model's context, and Ollama then drops the oldest text without saying so.
- Diablo.app is a launcher around the Python environment in `~/.diablo`, not a self-contained bundle.
- With computer control on, Stop revokes permission for the next click but does not interrupt a click that is already being made.
- Background tasks check a screenshot request against the intake model's restatement of what you asked, not your exact words. Requests handled straight away use your words.
