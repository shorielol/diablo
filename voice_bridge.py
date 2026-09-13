import os
import subprocess
import time
import numpy as np
import whisper
from dotenv import load_dotenv
load_dotenv()
from skills import conversation, screen_context, wake_word, voice, skill_registry, local_brain, mcp_client
# Added by patch_mic: these are called below but were never imported.
from skills import delegation_tool, agent_tools, mic, persona, browser_tool  # ─── Diablo persona wiring ───
import queue
import threading
import json
import re
import difflib
from typing import List
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer

# Import D.I.A.B.L.O. Skill Engine
registry = skill_registry.registry
registry.discover_skills()

# Legacy direct imports for core performance logic
# llm_tool is deliberately not imported. Nothing in the bridge calls it any
# more, and the shipped replacement routes through Ollama rather than
# OpenRouter, so the import would only be a dead reference to a cloud provider.

# Diablo: an offline tool-calling loop on qwen3:14b via Ollama, a rolling
# conversation, and per-window screen understanding. All local.

# ─── Load Environment ───────────────────────────────────────────────────────
ENV_PATH = "/Users/shaurya/.diablo/.env"
load_dotenv(ENV_PATH)

# ─── Audio Settings ──────────────────────────────────────────────────────────
CHUNK = 1280
CHANNELS = 1
RATE = 16000
COMMAND_DURATION = 5
CACHE_DIR = "/Users/shaurya/.diablo/audio_cache"

# Global Queues and Locks
audio_queue = queue.Queue()
command_capture_queue = queue.Queue()
is_capturing_command = threading.Event()

# Set when the user starts talking while Diablo is mid-answer. Checked between
# spoken segments, so speech stops within a sentence rather than at the end of
# a paragraph nobody is listening to.
barge_in = threading.Event()
COMMAND_LOCK = threading.Lock()

# Exclusive hold on the microphone for the length of one utterance. Reentrant
# because a clarifying question can be asked from inside a capture that already
# owns it. Everything that listens goes through capture_speech, so the wake-word
# handler and a background job can never drain the same audio queue at once.
MIC_LOCK = threading.RLock()

# ─── Paths & Context ────────────────────────────────────────────────────────
VISUALIZER_DIR = "/Users/shaurya/.diablo/visualizer"
UI_STATE_PATH = os.path.join(VISUALIZER_DIR, "state.json")
USER_CITY = "Pune"  # Default fallback

# ─── Constants & Personality ────────────────────────────────────────────────
VOICE_EN = "en-GB-RyanNeural"
VOICE_HI = "hi-IN-MadhurNeural"

CONTACT_MAPPING = {
    "pankaj singh": "corporate leaader dad",
    "pankaj": "corporate leaader dad",
    "neelima singh": "don ji",
    "neelima": "don ji",
    "mom": "don ji",
    "dad": "corporate leaader dad"
}

# ─── UI State Management ────────────────────────────────────────────────────

def set_ui_state(state):
    """
    Sets the visualizer state: 'idle', 'speaking', or 'listening'.

    Also gates the wake word. Diablo hears itself through the speakers, and
    without this it says its own name during a reply and instantly wakes itself
    up. Every state change funnels through here, so this is the one place it has
    to be done.
    """
    if state == "speaking":
        wake_word.SPEAKING.set()
    else:
        wake_word.SPEAKING.clear()
        listener = globals().get("wake_listener")
        if listener is not None:
            # A short tail, because the room and the microphone buffer are still
            # carrying the last syllable after afplay returns.
            listener.mute_for(0.7)
    try:
        with open(UI_STATE_PATH, "w") as f:
            json.dump({"state": state}, f)
    except Exception as e:
        print(f"  [UI State Error] {e}")

# ─── Tool Functions ─────────────────────────────────────────────────────────

def speak(text, lang="en", cached_key=None):
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
    barge_in.clear()
    set_ui_state("speaking")
    print(f"\n  Diablo: {text}")
    try:
        # Long answers start on the first sentence instead of after the whole
        # thing has been synthesised: 0.95s to the first word rather than
        # 2.79s. say_long falls back to a single call for anything short, so
        # the cached one-liners are unaffected.
        if len(text) > 120:
            voice.say_long(text, should_stop=lambda: barge_in.is_set())
        else:
            voice.speak(text)
    except Exception as e:
        print(f"  [TTS fallback: {e}]")
        subprocess.run(["say", "-v", "Daniel", text])
    finally:
        set_ui_state("idle")


def open_app(app_name):
    ack = persona.ack()
    speak(f"Opening {app_name}. {ack}")
    subprocess.Popen(["open", "-a", app_name])

def open_url(url):
    """Opens a URL in Zen Browser, attempting to switch to an existing tab first."""
    site_keyword = url.split("//")[-1].split("/")[0].replace("www.", "").split(".")[0]
    ack = persona.ack()
    
    if browser_tool.switch_to_site(site_keyword):
        speak(f"Switching to your {site_keyword.title()} tab, Sir.")
        return

    speak(f"{ack} Opening {site_keyword.title()}.")
    browser_tool.run_browser_navigation(url)

def take_screenshot():
    desktop = os.path.expanduser("~/Desktop")
    filename = f"DIABLO_Screenshot_{int(time.time())}.png"
    path = os.path.join(desktop, filename)
    subprocess.run(["screencapture", "-t", "png", path])
    speak(f"Screenshot saved to Desktop, Sir.", cached_key="ack_2")

# ─── Audio Engine ───────────────────────────────────────────────────────────

# ─── Diablo microphone layer (mic.py) ───

# Roughly 0.24s of sustained speech above the threshold before Diablo yields.
BARGE_IN_LEVEL = float(os.getenv("DIABLO_BARGE_IN_LEVEL", "0.22"))
BARGE_IN_BLOCKS = int(os.getenv("DIABLO_BARGE_IN_BLOCKS", "3"))
_barge: list = []


def audio_callback(block):
    """
    Called from the audio thread with 1280 float32 samples at 16 kHz.

    mic.py has already resampled from whatever rate the device actually runs at,
    so everything downstream still sees the chunk size it was written against.
    """
    audio_queue.put((block * 32767).astype(np.int16))
    if is_capturing_command.is_set():
        command_capture_queue.put(block.reshape(-1, 1).copy())

    # Barge-in. While Diablo is speaking, a sustained loud block is the user
    # talking over it. The threshold is deliberately high: Diablo hears itself
    # through the speakers, and a low one would make it interrupt its own
    # sentence every time.
    if voice.SPEAKING.is_set() and not barge_in.is_set():
        if float(np.abs(block).max()) > BARGE_IN_LEVEL:
            _barge.append(1)
            if len(_barge) >= BARGE_IN_BLOCKS:
                barge_in.set()
                voice.interrupt()
                _barge.clear()
        else:
            _barge.clear()


# ─── Diablo endpointed capture ───

# One shared endpointer. Constructing it loads the silero VAD, which is not
# something to do on every utterance.
_endpointer = mic.Endpointer()


def get_command_audio(duration=None):
    """
    Record one utterance, stopping shortly after the speaker stops.

    `duration` used to be exactly how long this recorded. It is now only a
    ceiling: a one-second question returns in about a second and a half rather
    than always costing five.
    """
    _endpointer.reset()
    if duration:
        _endpointer.max_blocks = max(1, int(duration * RATE / CHUNK))

    collected = []
    while True:
        try:
            block = command_capture_queue.get(timeout=1.5)
        except queue.Empty:
            break
        collected.append(block)
        if _endpointer.feed(np.asarray(block[:, 0], dtype=np.float32)):
            break

    if not collected:
        return None
    return np.concatenate(collected).flatten().astype(np.float32)


# ─── Intelligence & Fuzzy Logic ──────────────────────────────────────────────

def fuzzy_match(input_text: str, targets: List[str], threshold: float = 0.6) -> bool:
    """Returns True if the input text contains a close match to any target."""
    words = input_text.split()
    for word in words:
        matches = difflib.get_close_matches(word, targets, n=1, cutoff=threshold)
        if matches:
            return True
    return False

# ─── Core Logic ─────────────────────────────────────────────────────────────

# ─── Diablo v10.0 — Local Agent Core ───

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
    print(f"\n  [ASK] {question}")
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
        request_confirmation("shut down my systems", shutdown_diablo, [])
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
        number = re.search(r"\d+", lower)
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
        r"^(?:please\s+)?(?:open|launch|start)\s+(?:up\s+)?(?:my\s+|the\s+)?(.+)$",
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
                r"\.(com|org|net|io|dev|ai|co|in|uk)\b", target):
            speak(agent_tools.open_url(target.replace(" ", "")))
            return
        # Anything else ("open my Econ EE folder") needs judgement. Fall through.

    # ── Everything else: the agent. Real tools, and it can ask you a question. ──
    delegation_tool.handle(text, speak, ask=ask_user_voice, confirm=confirm_voice)


# ─── Diablo warm-on-wake ───

def _warm_the_brain():
    """
    Start loading the model the instant the wake word fires.

    A cold start costs 6.54s. Capturing and transcribing what you just said
    costs about 2.7s, and that happens anyway — so beginning the load now means
    the model is at worst 3.8s behind instead of 6.5s, and usually ready before
    the transcription finishes. Failures are ignored on purpose: this is an
    optimisation, and the real call will report any genuine problem.
    """
    try:
        local_brain.warm()
    except Exception:
        pass


def handle_utterance(command):
    threading.Thread(target=_warm_the_brain, daemon=True).start()

    try:
        _handle_utterance(command)
    except BaseException as e:
        # This used to die on a daemon thread and take the whole reply with it,
        # which reads as a microphone fault rather than a crash. Say it out loud.
        import traceback
        print(f"  [Utterance error] {type(e).__name__}: {e}")
        traceback.print_exc()
        try:
            speak("Something went wrong there, Sir.")
        except BaseException:
            pass


def _handle_utterance(command):

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
    text = capture_speech(COMMAND_DURATION, prompt=persona.greeting())
    print(f'  [USER] "{text}"')
    if len(text) > 2:
        try:
            process_command(text)
        except Exception as e:
            print(f"  [Command error] {type(e).__name__}: {e}")
            speak("Something went wrong handling that, Sir.")
    else:
        speak("I missed that, Sir.", cached_key="error_1")


def shutdown_diablo():
    """Powers down all D.I.A.B.L.O. systems gracefully."""
    speak("Powering down, Sir. It's been a pleasure.", cached_key="ack_1")
    time.sleep(1.5)
    
    # Terminate the visualizer widget
    subprocess.run(["pkill", "-f", "widget_launcher.py"])
    
    # Terminate the main bridge
    print("\n  [SHUTDOWN] D.I.A.B.L.O. Offline.")
    os._exit(0) # Force exit to bypass any thread joins


# ─── Main Loop ──────────────────────────────────────────────────────────────

def start_visualizer_server():
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


def start_listening_loop():
    set_ui_state("idle")
    # threading.Thread(target=start_visualizer_server, daemon=True).start()
    # time.sleep(1)
    # 2. Launch Visualizer (Desktop Widget)
    # time.sleep(1) # Wait for server
    # print("  Launching Visualizer Widget...")
    # widget_script = os.path.join(VISUALIZER_DIR, "widget_launcher.py")
    # subprocess.Popen([sys.executable, widget_script])
    
    # Use cached system_online for instant boot sound
    if local_brain.available():
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
        speak("I'm running on reflexes only, Sir. Ollama is not responding.")
    
    # The microphone is opened through mic.py, which has already verified that
    # this device produces sound. If none does, say so once and stop rather than
    # pretending to listen to a stream of zeros.
    try:
        microphone = mic.Microphone(on_block=audio_callback)
        microphone.start()
    except mic.MicError as e:
        print(f"\n{e}\n")
        speak("I cannot hear anything, Sir. My microphone access is blocked.")
        return
    except Exception as e:
        print(f"\n  [Mic] Could not open any microphone: {type(e).__name__}: {e}\n")
        speak("I cannot open the microphone, Sir.")
        return

    deaf_reported = False

    try:
        print(f"  Say 'Hey {wake_word.PHRASE.title()}' to activate...\n")
        while True:
            audio_data = audio_queue.get()

            # Permission can be revoked, or a device unplugged, while running.
            # macOS reports neither; the stream just turns to zeros.
            if microphone.went_deaf() and not deaf_reported:
                deaf_reported = True
                print("\n  [Mic] The microphone has gone silent. Run:  diablo doctor\n")
                speak("I've stopped hearing anything, Sir. My microphone went quiet.")

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
            threading.Thread(target=handle_utterance, args=(command,), daemon=True).start()
    except KeyboardInterrupt:
        print("\n  Stopping.")
    except BaseException:
        # An abrupt exit with no explanation is what made the double-start bug
        # so hard to find. Never again: whatever ends this loop gets written down.
        import traceback
        print("\n  [Fatal] the listening loop ended unexpectedly:")
        traceback.print_exc()
    finally:
        microphone.stop()

if __name__ == "__main__":
    print("\n  ╔══════════════════════════════════════════════════╗")
    print("  ║     D.I.A.B.L.O. v8.0 — The Synaptic Core        ║")
    print("  ╚══════════════════════════════════════════════════╝\n")
    
    # Context Detection...
    # (Removed: geocoder.ip('me') phoned an IP-geolocation service on every boot
    #  and USER_CITY was never read by anything.)
        
    # Synaptic Overload: Core hearing using 'base' model with M3 Pro acceleration
    print("  [INIT] Calibrating Synaptic Sensors (Base @ MPS)...")
    try:
        # Force Metal Performance Shaders (MPS) for M3 Pro
        stt_model = whisper.load_model(os.getenv("DIABLO_STT_MODEL", "base.en"), device="mps")
    except:
        print("  ⚠ MPS acceleration fails. Falling back to CPU...")
        stt_model = whisper.load_model(os.getenv("DIABLO_STT_MODEL", "base.en"))
        
    # Wake word without a trained model: silero VAD decides if anyone is
    # speaking, tiny.en transcribes a rolling window, and the phrase is matched
    # fuzzily. Costs about 7ms a second idle. The phrase is a config value now,
    # not an ONNX file, so changing it needs no dataset and no training run.
    # The command itself is re-transcribed with the larger model above, because
    # tiny.en heard "open my econ folder" as "open my econ soldier".
    print(f"  [INIT] Wake phrase: '{wake_word.PHRASE}' "
          f"(detect: {wake_word.WAKE_MODEL}, command: base)")
    wake_listener = wake_word.WakeListener(command_model=stt_model)
    try:
        start_listening_loop()
    except KeyboardInterrupt:
        print("\nLogging off, Sir.")
        set_ui_state("idle")
