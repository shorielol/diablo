# Demo script

About ten minutes, on one Mac, with nothing private on screen. Everything here runs locally except the research step, which searches the web.

## Before you start

- Ollama is running, with `qwen3.5:4b` pulled. It answers vision questions itself; there is no second model to pull.
- Voicebox is running if you want Diablo's own voice. Without it you get the macOS voice, which works but sounds like a Mac.
- Diablo has Microphone, Screen Recording and Accessibility permission. Open the window from the menu bar and check that the "needs you" line is gone.
- Close anything you would not want read aloud: mail, messages, banking, password managers. Diablo refuses password managers anyway, but the audience can see your screen too.
- Leave computer control off. Nothing in this script needs it.

## The run

Hold Control+Option while you speak each line, and let go when you finish.

1. **"What is 17 times 23?"** It answers 391 straight away, without calling the model. The point to make is that arithmetic goes through Python, never through the model.
2. **"Open Calculator."** This is a reflex, with no model involved: Diablo has decided within a tenth of a second, and the reply is spoken about a second later with the macOS voice. The gauntlet did not time the launch itself, because that would have opened an app on the test Mac.
3. **"What app am I looking at?"** It answers from the window list, measured at about 5 seconds when the model is warm and 12 seconds cold. If it decides to look at the pixels, it switches to the vision model and the answer takes up to 40 seconds on an 18 GB Mac, so say that before you ask.
4. **"Research the best budget mechanical keyboards under 100 dollars."** It acknowledges, and the task appears in the window with a Stop button. The acknowledgement comes in about a second and the answer in about 20 seconds (about 33 the first time) while you keep talking.
5. **"Research flights from Pune to Delhi next weekend,"** then press **Stop** on that task. It stops before its next step and the result is not read out.
6. **"Take a screenshot."** A file appears on the Desktop. Then ask **"what's on my screen?"** and show that no new file appears. Only a request to save one creates a file.
7. Open the menu with the keyboard (Tab to the "More" button, then Down). The arrows move through the items, and Escape closes the menu and returns focus to the button.

## Things not to demo yet

- "Open a new browser tab". It opens the browser but not a tab.
- Long conversations. Past roughly 8,000 tokens the model's context fills, and each turn takes about 20 seconds.
- Installing on someone else's Mac. The installer does not support a clean machine yet.
