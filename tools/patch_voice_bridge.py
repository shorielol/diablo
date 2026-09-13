#!/usr/bin/env python3
"""
Rebuilds Diablo's voice_bridge.py around the local agent.

This is a bigger surgery than a few inserts, because the old process_command was
a 200-line if/elif ladder with dead branches, two unbound names and no way to
finish anything it did not have a hardcoded handler for. Whole functions get
replaced rather than patched around.

What changes:

  process_command          rewritten — reflexes, then the agent
  on_wake_word_detected    replaced by handle_utterance — one-shot commands
  listen_for_response      rewritten — one microphone owner, via capture_speech
  request_confirmation     rewritten — sits on top of confirm_voice
  start_visualizer_server  fixed — no process-wide chdir, port reuse allowed
  + capture_speech, ask_user_voice, confirm_voice   (new)
  + MIC_LOCK                                        (new)

Bugs it removes on the way past:

  1. `music_skill` unbound — "previous track" and "pause" crashed their thread.
  2. `wiki_tool` unbound — every Wikipedia lookup crashed.
  3. The whole image-generation branch sat after a `return` inside the contacts
     handler, so it was unreachable. "Generate an image of X" did nothing.
  4. `open_app`, `open_url` and `take_screenshot` were never called by anything.
     "Open Spotify" fell through to a handler that asked which app to open and
     then recursed into itself.
  5. A second, identical `if "google" in lower` block that could never run.
  6. `start_visualizer_server` called `os.chdir` on a worker thread, moving the
     whole interpreter's working directory into the visualizer folder.
  7. `TCPServer` bound port 8000 without SO_REUSEADDR, so a quick restart died
     on "address already in use".
  8. The wake-word loop kept scanning the remaining scores after firing, so one
     utterance could trigger twice.

Idempotent. Backs up first. Refuses to write anything that will not compile.

    python3 patch_voice_bridge.py [--dry-run] [--restore]
"""

import argparse
import glob
import os
import re
import shutil
import sys
import time

BRIDGE = os.getenv("DIABLO_BRIDGE", os.path.join(
    os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo")), "voice_bridge.py"))

MARKER = "# ─── Diablo v10.0 — Local Agent Core ───"


# ─── New source ──────────────────────────────────────────────────────────────

IMPORT_ANCHOR = "from skills import llm_tool, personality, browser_tool"
IMPORT_NEW = """from skills import personality, browser_tool
# llm_tool is deliberately not imported. Nothing in the bridge calls it any
# more, and the shipped replacement routes through Ollama rather than
# OpenRouter, so the import would only be a dead reference to a cloud provider.

# Diablo: an offline tool-calling loop on qwen3:14b via Ollama, a rolling
# conversation, and per-window screen understanding. All local.
from skills import delegation_tool, agent_tools, local_brain
from skills import conversation, screen_context, wake_word, voice
from skills import mcp_client"""

UI_STATE_ANCHOR = """def set_ui_state(state):
    \"\"\"Sets the visualizer state: 'idle', 'speaking', or 'listening'.\"\"\"
    try:"""
UI_STATE_NEW = """def set_ui_state(state):
    \"\"\"
    Sets the visualizer state: 'idle', 'speaking', or 'listening'.

    Also gates the wake word. Diablo hears itself through the speakers, and
    without this it says its own name during a reply and instantly wakes itself
    up. Every state change funnels through here, so this is the one place it has
    to be done.
    \"\"\"
    if state == "speaking":
        wake_word.SPEAKING.set()
    else:
        wake_word.SPEAKING.clear()
        listener = globals().get("wake_listener")
        if listener is not None:
            # A short tail, because the room and the microphone buffer are still
            # carrying the last syllable after afplay returns.
            listener.mute_for(0.7)
    try:"""

LOCK_ANCHOR = "COMMAND_LOCK = threading.Lock()"
LOCK_NEW = """COMMAND_LOCK = threading.Lock()

# Exclusive hold on the microphone for the length of one utterance. Reentrant
# because a clarifying question can be asked from inside a capture that already
# owns it. Everything that listens goes through capture_speech, so the wake-word
# handler and a background job can never drain the same audio queue at once.
MIC_LOCK = threading.RLock()"""


CAPTURE_BLOCK = MARKER + '''

# Spoken shorthand that must never wait on a language model.

TRANSPORT_COMMANDS = {
    "next": "next", "next track": "next", "next song": "next", "skip": "next",
    "skip this": "next", "skip song": "next", "skip track": "next",
    "previous": "previous", "previous track": "previous", "previous song": "previous",
    "go back": "previous", "back a track": "previous",
    "pause": "toggle", "pause music": "toggle", "pause the music": "toggle",
    "resume": "toggle", "unpause": "toggle", "play": "toggle",
    "stop": "toggle", "stop music": "toggle", "stop the music": "toggle",
}

KNOWN_APPS = {
    "spotify": "Spotify", "safari": "Safari", "chrome": "Google Chrome",
    "zen": "Zen Browser", "zen browser": "Zen Browser", "notes": "Notes",
    "finder": "Finder", "terminal": "Terminal", "mail": "Mail",
    "messages": "Messages", "imessage": "Messages", "calendar": "Calendar",
    "excel": "Microsoft Excel", "word": "Microsoft Word",
    "powerpoint": "Microsoft PowerPoint", "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code", "code": "Visual Studio Code",
    "obsidian": "Obsidian", "discord": "Discord", "whatsapp": "WhatsApp",
    "preview": "Preview", "music": "Music", "photos": "Photos",
    "reminders": "Reminders", "settings": "System Settings",
    "system settings": "System Settings", "calculator": "Calculator",
}

KNOWN_SITES = {
    "youtube": "https://youtube.com", "gmail": "https://mail.google.com",
    "github": "https://github.com", "google": "https://google.com",
    "notion": "https://notion.so", "drive": "https://drive.google.com",
    "classroom": "https://classroom.google.com",
    "managebac": "https://managebac.com", "instagram": "https://instagram.com",
    "reddit": "https://reddit.com", "linkedin": "https://linkedin.com",
    "chatgpt": "https://chat.openai.com",
}

AFFIRMATIVE = ["yes", "yeah", "yep", "sure", "go ahead", "do it", "affirmative",
               "please do", "haan", "correct", "confirm", "proceed", "okay", "ok"]


def capture_speech(duration=5, prompt=None, precision=True):
    """
    Take exclusive hold of the microphone for one utterance and transcribe it.

    Every listener in the system funnels through here — the wake-word handler,
    the agent's clarifying questions, the confirmation prompts — so two of them
    can never be pulling from the same audio queue at the same time.
    """
    listener = globals().get("wake_listener")
    if listener is not None:
        listener.mute_for(duration + 2)

    with MIC_LOCK:
        if prompt:
            speak(prompt)
        while not command_capture_queue.empty():
            command_capture_queue.get()
        is_capturing_command.set()
        set_ui_state("listening")
        audio = get_command_audio(duration)
        is_capturing_command.clear()
        set_ui_state("idle")
        while not command_capture_queue.empty():
            command_capture_queue.get()

    if audio is None:
        return ""
    result = stt_model.transcribe(audio, fp16=False, beam_size=5 if precision else 1)
    return result["text"].strip()


def listen_for_response(duration=3):
    return capture_speech(duration).lower()


def ask_user_voice(question):
    """
    How the agent gets unstuck instead of guessing.

    Handed to agent_core as the `ask` callback. It speaks the question and opens
    the microphone in one move, because a gap between the two means the answer
    lands before anything is listening. Usually called from a worker thread,
    which is exactly why the microphone is locked.
    """
    print(f"\\n  [ASK] {question}")
    answer = capture_speech(5, prompt=question)
    print(f"  [ANSWER] {answer!r}")
    return answer


def confirm_voice(description):
    """Spoken yes/no in front of anything that cannot simply be undone."""
    answer = capture_speech(
        4, prompt=f"Sir, I'm about to {description}. Shall I proceed?").lower()
    approved = any(word in answer for word in AFFIRMATIVE)
    if approved:
        speak("Proceeding.", cached_key="ack_1")
    else:
        speak("Cancelled.")
    return approved


def request_confirmation(description, action_fn, args):
    if confirm_voice(description):
        action_fn(*args)


def process_command(text, is_sub_command=False):
    """
    Reflexes first, then the agent.

    The reflexes are the handful of things that must never wait on a language
    model — volume, playback, a screenshot, opening a known app, shutting down.
    They are pattern matches and they answer in under a second.

    Everything else goes to the local agent, which has twenty-nine real tools, a
    written plan, and permission to stop and ask you a question. That is the
    whole difference from the old ladder: an unrecognised sentence used to end
    in "I'll look into it", which meant nothing happened at all.
    """
    lower = text.lower().strip()
    stripped = lower.rstrip(" .!?,")

    # ── Power ──
    shutdown_targets = ["shut down diablo", "go to sleep", "deactivate", "power down"]
    if any(t in lower for t in shutdown_targets) or fuzzy_match(lower, shutdown_targets):
        request_confirmation("shut down my systems", SHUTDOWN_FN, [])
        return

    # ── "What are you working on" ──
    if delegation_tool.is_status_query(lower):
        speak(delegation_tool.status_report())
        return

    # ── Playback transport ──
    if stripped in TRANSPORT_COMMANDS:
        agent_tools.music_transport(TRANSPORT_COMMANDS[stripped])
        speak("Done, Sir.", cached_key="ack_1")
        return

    # ── Volume and brightness ──
    if fuzzy_match(lower, ["volume", "louder", "quieter", "mute", "brightness"]):
        number = re.search(r"\\d+", lower)
        try:
            if "mute" in lower:
                speak(agent_tools.set_volume(0))
            elif "brightness" in lower and number:
                speak(agent_tools.set_brightness(int(number.group())))
            elif number:
                speak(agent_tools.set_volume(int(number.group())))
            else:
                louder = any(w in lower for w in ["up", "louder", "raise", "increase"])
                registry.get_skill("system_skill").adjust_volume("up" if louder else "down")
                speak("Done, Sir.", cached_key="ack_2")
        except Exception as e:
            print(f"  [Reflex error] {e}")
            speak("I could not adjust that, Sir.")
        return

    # ── Screenshot ──
    if "screenshot" in lower or "screen shot" in lower or "screengrab" in lower:
        agent_tools.take_screenshot()
        speak("Screenshot saved to your Desktop, Sir.", cached_key="ack_2")
        return

    # ── Open a known app or site ──
    opener = re.match(
        r"^(?:please\\s+)?(?:open|launch|start)\\s+(?:up\\s+)?(?:my\\s+|the\\s+)?(.+)$",
        stripped)
    if opener:
        target = opener.group(1).strip()
        if target in KNOWN_APPS:
            speak(agent_tools.open_app(KNOWN_APPS[target]))
            return
        if target in KNOWN_SITES:
            speak(agent_tools.open_url(KNOWN_SITES[target]))
            return
        if target.startswith(("http", "www.")) or re.search(
                r"\\.(com|org|net|io|dev|ai|co|in|uk)\\b", target):
            speak(agent_tools.open_url(target.replace(" ", "")))
            return
        # Anything else ("open my Econ EE folder") needs judgement. Fall through.

    # ── Everything else: the agent. Real tools, and it can ask you a question. ──
    delegation_tool.handle(text, speak, ask=ask_user_voice, confirm=confirm_voice)


def handle_utterance(command):
    """
    One wake. `command` is whatever was said after the wake word, or "".

    A trained wake word can only tell you THAT you were addressed, so the old
    flow was always: wake, greet, record for a fixed five seconds, transcribe,
    act. Detecting by transcription hands over the whole sentence instead, so
    when the phrase and the request arrive in one breath — which is how people
    actually talk — none of that second round is needed.
    """
    if command and len(command.split()) >= 2:
        print(f'  [USER] "{command}"  (one-shot)')
        try:
            process_command(command)
        except Exception as e:
            print(f"  [Command error] {type(e).__name__}: {e}")
            speak("Something went wrong handling that, Sir.")
        return

    # Only the name was said. Acknowledge, then listen properly. The greeting is
    # spoken before the capture rather than alongside it, so Diablo is not
    # recording itself saying hello.
    greet_id = random.randint(1, 3)
    text = capture_speech(
        COMMAND_DURATION,
        prompt=personality.RESPONSES_GREETING[greet_id - 1],
    )
    print(f'  [USER] "{text}"')
    if len(text) > 2:
        try:
            process_command(text)
        except Exception as e:
            print(f"  [Command error] {type(e).__name__}: {e}")
            speak("Something went wrong handling that, Sir.")
    else:
        speak("I missed that, Sir.", cached_key="error_1")
'''

SPEAK_NEW = '''def speak(text, lang="en", cached_key=None):
    """
    Say something out loud.

    All the routing lives in skills/voice.py: the disk cache first, then
    ElevenLabs, then edge-tts, then macOS `say`. Nothing here can fail loudly —
    the worst case is that Diablo sounds like the system voice.

    `cached_key` is a leftover from the old scheme of hand-rendered clips for a
    dozen fixed phrases. It is ignored now, because voice.py caches on the text
    itself and therefore covers every line, not just the ones someone
    remembered to pre-render. The multilingual model handles the Devanagari
    that VOICE_HI used to be needed for.
    """
    set_ui_state("speaking")
    print(f"\\n  Diablo: {text}")
    try:
        voice.speak(text)
    except Exception as e:
        print(f"  [TTS fallback: {e}]")
        subprocess.run(["say", "-v", "Daniel", text])
    finally:
        set_ui_state("idle")
'''

VISUALIZER_NEW = '''def start_visualizer_server():
    """
    Serve the HUD.

    The old version called os.chdir on a worker thread, which moved the whole
    interpreter's working directory into the visualizer folder and quietly broke
    every relative path used anywhere else. SimpleHTTPRequestHandler takes a
    directory argument instead. allow_reuse_address stops a quick restart dying
    on a socket still in TIME_WAIT.
    """
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=VISUALIZER_DIR, **kwargs)

        def log_message(self, *args):
            pass  # the HUD polls constantly; its access log is pure noise

    TCPServer.allow_reuse_address = True
    try:
        with TCPServer(("", 8000), Handler) as httpd:
            print("  [UI Server] Hosting at http://localhost:8000")
            httpd.serve_forever()
    except OSError as e:
        print(f"  [UI Server] Port 8000 unavailable ({e}). The HUD will not load.")
'''

# The whole openWakeWord loop goes. It could only ever fire on "Hey Jarvis",
# and it only ever told you THAT you were addressed, never what was said.
LOOP_ANCHOR = """    WAKE_THRESHOLD = 0.28  
    mic_index = get_target_mic_index()
    
    with sd.InputStream(device=mic_index, samplerate=RATE, channels=CHANNELS, callback=audio_callback, blocksize=CHUNK, dtype='float32'):
        print(f"\\n  🎙️  Listening via Device {mic_index if mic_index is not None else 'Default'}...")
        print("  Say 'Hey Jarvis' to activate...\\n")
        while True:
            audio_data = audio_queue.get()
            prediction = ww_model.predict(audio_data)
            for score in prediction.values():
                if score > WAKE_THRESHOLD:
                    while not command_capture_queue.empty(): command_capture_queue.get()
                    threading.Thread(target=on_wake_word_detected, daemon=True).start()
                    time.sleep(COMMAND_DURATION + 0.5)
                    while not audio_queue.empty(): audio_queue.get_nowait()
                    ww_model.reset()"""
LOOP_NEW = """    mic_index = get_target_mic_index()

    with sd.InputStream(device=mic_index, samplerate=RATE, channels=CHANNELS, callback=audio_callback, blocksize=CHUNK, dtype='float32'):
        print(f"\\n  🎙️  Listening via Device {mic_index if mic_index is not None else 'Default'}...")
        print(f"  Say 'Hey {wake_word.PHRASE.title()}' to activate...\\n")
        while True:
            audio_data = audio_queue.get()
            # Something else already owns the microphone — a command capture, a
            # clarifying question. Do not try to wake on the same words.
            if is_capturing_command.is_set():
                continue
            try:
                command = wake_listener.feed(audio_data)
            except Exception as e:
                print(f"  [Wake] {type(e).__name__}: {e}")
                continue
            if command is None:
                continue
            threading.Thread(target=handle_utterance, args=(command,), daemon=True).start()"""

# Two things still left the machine on every boot. edge-tts is a round trip to
# Microsoft for each spoken sentence, and geocoder.ip('me') is an IP-geolocation
# lookup whose result (USER_CITY) is never read by anything.
TTS_ANCHOR = """    # 2. Dynamic TTS Generation
    voice = VOICE_HI if (lang == "hi" or any("\\u0900" <= c <= "\\u097F" for c in text)) else VOICE_EN"""
TTS_NEW = """    # 2a. Strict offline mode: macOS speech synthesis, no round trip to Microsoft.
    #     edge-tts sounds better but it is a network call on every sentence, which
    #     makes a nonsense of running the model locally.
    if os.getenv("DIABLO_OFFLINE", "0") not in ("0", "", "false", "False"):
        try:
            subprocess.run(["say", "-v", "Daniel", text], timeout=60)
        finally:
            set_ui_state("idle")
        return

    # 2b. Dynamic TTS Generation
    voice = VOICE_HI if (lang == "hi" or any("\\u0900" <= c <= "\\u097F" for c in text)) else VOICE_EN"""

GEO_ANCHOR = """    try:
        g = geocoder.ip('me')
        if g.city: USER_CITY = g.city
    except: pass"""
GEO_NEW = """    # (Removed: geocoder.ip('me') phoned an IP-geolocation service on every boot
    #  and USER_CITY was never read by anything.)"""

# Boot banner: say out loud whether the local brain is actually up.
WW_ANCHOR = """    ww_model = Model(wakeword_models=["hey_jarvis_v0.1", "jarvis"], inference_framework="onnx")"""
WW_NEW = """    # Wake word without a trained model: silero VAD decides if anyone is
    # speaking, tiny.en transcribes a rolling window, and the phrase is matched
    # fuzzily. Costs about 7ms a second idle. The phrase is a config value now,
    # not an ONNX file, so changing it needs no dataset and no training run.
    # The command itself is re-transcribed with the larger model above, because
    # tiny.en heard "open my econ folder" as "open my econ soldier".
    print(f"  [INIT] Wake phrase: '{wake_word.PHRASE}' "
          f"(detect: {wake_word.WAKE_MODEL}, command: base)")
    wake_listener = wake_word.WakeListener(command_model=stt_model)"""

BOOT_ANCHOR = '''    speak("At your service, Sir.", cached_key="system_online")'''
BOOT_NEW = '''    if local_brain.available():
        models = local_brain.installed_models()
        if local_brain.CHAT_MODEL not in models:
            print(f"  ⚠ {local_brain.CHAT_MODEL} is not installed. "
                  f"Run: ollama pull {local_brain.CHAT_MODEL}")
            speak(f"My reasoning model is missing, Sir. Please pull {local_brain.CHAT_MODEL}.")
        else:
            print(f"  [Brain] {local_brain.CHAT_MODEL} via {local_brain.OLLAMA_HOST}")
            threading.Thread(target=local_brain.warm, daemon=True).start()
            # MCP servers boot in the background. A cold npx server can take ten
            # seconds and the wake word must not wait on it; the tools simply
            # appear in the agent's menu once each server answers.
            mcp_client.registry.start_all(background=True)
            speak("At your service, Sir.", cached_key="system_online")
    else:
        print("  ⚠ Ollama is not responding. Reflexes only.")
        speak("I'm running on reflexes only, Sir. Ollama is not responding.")'''


# ─── Machinery ───────────────────────────────────────────────────────────────

def replace_function(source, name, new_source):
    """Swap a whole top-level function definition for a new one."""
    pattern = re.compile(
        r"^def " + re.escape(name) + r"\(.*?(?=^(?:def |#\s*─|if __name__))",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(source)
    if not match:
        return source, False
    return source[:match.start()] + new_source.rstrip() + "\n\n\n" + source[match.end():], True


def cut_function(source, name):
    """Delete a top-level function outright."""
    pattern = re.compile(
        r"^def " + re.escape(name) + r"\(.*?(?=^(?:def |#\s*─|if __name__))",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(source)
    if not match:
        return source, False
    return source[:match.start()] + source[match.end():], True


def restore():
    backups = sorted(glob.glob(BRIDGE + ".bak-*"))
    if not backups:
        print("✗ No backups found.")
        return 1
    newest = backups[-1]
    shutil.copy2(newest, BRIDGE)
    print(f"✓ Restored {BRIDGE} from {newest}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true",
                        help="put the newest backup back")
    args = parser.parse_args()

    if args.restore:
        return restore()

    if not os.path.exists(BRIDGE):
        print(f"✗ Cannot find {BRIDGE}")
        return 1

    with open(BRIDGE) as f:
        source = original = f.read()

    if MARKER in source:
        print("= voice_bridge.py already runs the local agent. Nothing to do.")
        print("  (Re-patching from scratch: python3 patch_voice_bridge.py --restore, then run this again.)")
        return 0

    steps = []

    # 1. imports
    if IMPORT_NEW not in source and IMPORT_ANCHOR in source:
        source = source.replace(IMPORT_ANCHOR, IMPORT_NEW, 1)
        steps.append("import the agent modules")
    elif IMPORT_ANCHOR not in source:
        print("✗ Could not find the skill import line. voice_bridge.py has drifted.")
        return 1

    # 2. echo suppression + microphone lock
    if UI_STATE_ANCHOR in source:
        source = source.replace(UI_STATE_ANCHOR, UI_STATE_NEW, 1)
        steps.append("gate the wake word while Diablo is speaking")

    if LOCK_ANCHOR in source:
        source = source.replace(LOCK_ANCHOR, LOCK_NEW, 1)
        steps.append("add MIC_LOCK")
    else:
        print("✗ Could not find COMMAND_LOCK.")
        return 1

    # 3. the old ladder and its neighbours go; the new core arrives in its place
    for name in ("process_command", "on_wake_word_detected", "listen_for_response",
                 "request_confirmation"):
        source, ok = cut_function(source, name)
        if not ok:
            print(f"✗ Could not find {name}() to replace.")
            return 1
        steps.append(f"remove old {name}()")

    # rebrand.py renames this function, so find whatever it is actually called
    # and bake that name into the generated code rather than assuming.
    shutdown_match = re.search(r"^def (shutdown_\w+)\(", source, re.MULTILINE)
    if not shutdown_match:
        print("✗ Could not find the shutdown function to anchor the new core against.")
        return 1
    shutdown_name = shutdown_match.group(1)
    core_anchor = f"def {shutdown_name}("
    source = source.replace(
        core_anchor,
        CAPTURE_BLOCK.replace("SHUTDOWN_FN", shutdown_name) + "\n\n" + core_anchor, 1)
    steps.append("install the reflex table, capture_speech, ask_user_voice, "
                 "confirm_voice, process_command and handle_utterance")

    # 4. the voice, and the visualizer server
    source, ok = replace_function(source, "speak", SPEAK_NEW)
    if ok:
        steps.append("route speech through voice.py (ElevenLabs + cache)")
    else:
        print("✗ Could not find speak() to replace.")
        return 1

    source, ok = replace_function(source, "start_visualizer_server", VISUALIZER_NEW)
    if ok:
        steps.append("fix start_visualizer_server (chdir + port reuse)")

    # 5. the wake word itself
    #
    # These two anchors are matched loosely on purpose. rebrand.py rewrites the
    # assistant's name everywhere, including inside the prompt string and the
    # wakeword_models list, so an exact match would work before a rebrand and
    # silently fail after one. Matching the name as \w+ makes the patcher
    # independent of whether the rename has already happened.
    # re.escape escapes spaces and apostrophes on some Python versions and not
    # others, so the loose bit is applied to the escaped string by replacing the
    # one bare word rather than a whole phrase.
    loop_pattern = re.compile(re.escape(LOOP_ANCHOR).replace("Jarvis", r"\w+"))
    source, count = loop_pattern.subn(lambda m: LOOP_NEW, source, count=1)
    if count:
        steps.append("replace the openWakeWord loop with 'Hey Diablo'")
    else:
        print("✗ Could not find the openWakeWord listening loop.")
        return 1

    ww_pattern = re.compile(
        r'    ww_model = Model\(wakeword_models=\["hey_jarvis_v0\.1", "\w+"\], '
        r'inference_framework="onnx"\)')
    source, count = ww_pattern.subn(lambda m: WW_NEW, source, count=1)
    if count:
        steps.append("build the wake listener instead of loading hey_jarvis.onnx")
    else:
        print("✗ Could not find the openWakeWord model construction.")
        return 1

    # 6. close the two remaining network leaks
    if GEO_ANCHOR in source:
        source = source.replace(GEO_ANCHOR, GEO_NEW, 1)
        steps.append("drop the IP-geolocation call at boot")

    # 7. boot banner
    if BOOT_ANCHOR in source:
        source = source.replace(BOOT_ANCHOR, BOOT_NEW, 1)
        steps.append("report brain status at boot")

    for step in steps:
        print(f"  + {step}")

    try:
        compile(source, BRIDGE, "exec")
    except SyntaxError as e:
        print(f"\n✗ The patched file would not compile: {e.msg} (line {e.lineno})")
        print("  Nothing was written.")
        return 1

    if args.dry_run:
        preview = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "voice_bridge.patched-preview.py")
        with open(preview, "w") as f:
            f.write(source)
        print(f"\n✓ Dry run clean, compiles. Preview written to {preview}")
        return 0

    backup = f"{BRIDGE}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(BRIDGE, backup)
    with open(BRIDGE, "w") as f:
        f.write(source)

    print(f"\n✓ Patched ({len(original.splitlines())} → {len(source.splitlines())} lines)")
    print(f"  Backup: {backup}")
    print(f"  Undo:   python3 {os.path.basename(__file__)} --restore")
    return 0


if __name__ == "__main__":
    sys.exit(main())
