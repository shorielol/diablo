"""
D.I.A.B.L.O. Wake Word
======================
Listens for "Hey Diablo" without a trained wake-word model.

openWakeWord ships exactly one pretrained model on this machine,
`hey_jarvis_v0.1.onnx`. Training a "Diablo" model needs a synthetic-speech
generator, several gigabytes of negative audio, and a training run. This does
the same job with what is already installed:

    silero VAD      is anyone speaking?            ~4ms   (bundled with openwakeword)
    whisper tiny.en what did they say?             ~120ms (already cached)
    fuzzy match     was "diablo" in there?         ~0ms

Measured on this machine: 122ms to transcribe a 2-second window. The VAD gate
means that only runs while someone is actually talking, so the idle cost is
about seven milliseconds a second.

Two things this buys that a trained model cannot:

1. **The phrase is a config value.** Change DIABLO_WAKE_PHRASE and it is done —
   no dataset, no training, no ONNX export.
2. **One-shot commands.** A trained wake word only tells you *that* you were
   addressed. This gives you the whole sentence, so "Hey Diablo, what's on my
   screen" is heard and answered in one breath instead of a wake, a beep, and a
   second five-second capture.
"""

import difflib
import os

from skills import config  # noqa: E402
import re
import sys
import threading
import time
from collections import deque
from typing import List, Optional

import numpy as np

SAMPLE_RATE = 16000
PHRASE = os.getenv("DIABLO_WAKE_PHRASE", "diablo").lower().strip()
WAKE_MODEL = os.getenv("DIABLO_WAKE_MODEL", "tiny.en")
# In low-memory mode the command transcriber reuses this model rather than
# loading base.en alongside it, which is about 145 MB of the bridge's 255 MB.
SHARE_MODEL = os.getenv("DIABLO_LOW_MEMORY", "0") not in ("0", "", "false", "False")
WINDOW_SECONDS = config.env_float("DIABLO_WAKE_WINDOW", 3.0)
VAD_THRESHOLD = config.env_float("DIABLO_VAD_THRESHOLD", 0.08)
MIN_GAP = config.env_float("DIABLO_WAKE_MIN_GAP", 0.55)   # seconds between transcriptions
COOLDOWN = config.env_float("DIABLO_WAKE_COOLDOWN", 2.5)  # seconds muted after a hit
FUZZ = config.env_float("DIABLO_WAKE_FUZZ", 0.72)
END_OF_SPEECH = config.env_float("DIABLO_END_OF_SPEECH", 0.9)   # silence that ends a sentence
MAX_UTTERANCE = config.env_float("DIABLO_MAX_UTTERANCE", 12.0)  # hard stop on one request

# Diablo hears itself through the speakers. Without this it says its own name
# during a reply and immediately wakes itself up.
SPEAKING = threading.Event()

# What Whisper actually produces for a given wake word when it is unsure.
# Cheaper than lowering the fuzzy threshold far enough to catch these, which
# starts matching "diagram" and "the app low".
#
# Keyed by phrase, because these spellings are specific to "diablo" — applying
# them to whatever DIABLO_WAKE_PHRASE happens to be set to would mean a custom
# phrase still woke on "diablo".
NEAR_MISSES_BY_PHRASE = {
    "diablo": {
        "diablo", "diabolo", "diablos", "diabolos", "dablo", "diablo's",
        "deeablo", "dieblo", "diabo", "dia blo", "the ablo", "d'ablo",
    },
}


def _normalise(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z' ]+", " ", text)
    return " ".join(text.split())


def phrase_position(text: str, phrase: str = PHRASE, fuzz: float = FUZZ) -> Optional[int]:
    """
    Index of the token that is the wake word, or None.

    Whisper punctuates and capitalises, so matching is done on normalised tokens
    with a similarity floor rather than on an exact string. The floor sits at
    0.72 because "diabolo" scores 0.86 and "diagram" scores 0.57 — comfortably
    either side.
    """
    tokens = _normalise(text).split()
    if not tokens:
        return None
    near_misses = NEAR_MISSES_BY_PHRASE.get(phrase, set())
    for index, token in enumerate(tokens):
        if token == phrase or token in near_misses:
            return index
        if difflib.SequenceMatcher(None, token, phrase).ratio() >= fuzz:
            return index
    # Whisper sometimes splits it: "the ablo", "di ablo".
    for index in range(len(tokens) - 1):
        joined = tokens[index] + tokens[index + 1]
        if joined == phrase or difflib.SequenceMatcher(None, joined, phrase).ratio() >= fuzz:
            return index + 1
    return None


def trailing_command(text: str, phrase: str = PHRASE) -> str:
    """Whatever was said after the wake word, which is usually the actual request."""
    tokens = _normalise(text).split()
    position = phrase_position(text, phrase)
    if position is None:
        return ""
    rest = tokens[position + 1:]
    # "hey diablo, please ..." — drop the politeness so the classifier sees the verb.
    while rest and rest[0] in ("please", "can", "could", "you", "um", "uh", "so", "okay"):
        rest.pop(0)
    return " ".join(rest).strip()


class WakeListener:
    """
    Feed it microphone chunks; it tells you when it heard its name.

    Two states. Idle, where it transcribes a rolling window twice a second and
    looks for the phrase. Capturing, which it enters the moment it hears the
    phrase and leaves when you stop talking — because detection fires partway
    through a sentence, and returning "what is" when you said "what is on my
    screen" is worse than useless.

    Not thread-safe. Drive it from the one loop that owns the audio queue.
    """

    def __init__(self, phrase: str = PHRASE, model_name: str = WAKE_MODEL,
                 whisper_model=None, command_model=None, verbose: bool = True):
        self.phrase = phrase
        self.verbose = verbose
        # Two models on purpose. `tiny.en` runs twice a second forever, so it has
        # to be cheap; it only has to spot one word. The command is transcribed
        # once, and getting it wrong wastes a whole agent run — tiny.en heard
        # "open my econ folder" as "open my econ soldier" — so that pass uses the
        # bigger model voice_bridge has already loaded.
        self.command_model = command_model
        self.buffer: deque = deque(maxlen=int(WINDOW_SECONDS * SAMPLE_RATE))
        self.last_transcribed = 0.0
        self.muted_until = 0.0
        self.recent_vad: deque = deque(maxlen=8)

        # Capture state: set when the phrase is heard, cleared when the sentence
        # finishes. `capture` grows without a cap, so a long request is not
        # truncated by the rolling window's three seconds.
        self.capturing = False
        self.capture: List[np.ndarray] = []
        self.capture_started = 0.0
        self.silence_started: Optional[float] = None

        # Every interval below is measured in seconds of audio fed, not wall
        # clock. Behaviour then does not depend on how fast chunks arrive, which
        # makes it identical live and under test — and stops a busy CPU
        # silently changing how often the wake window is checked.
        self.clock = 0.0

        if whisper_model is not None:
            self.model = whisper_model
        else:
            import whisper
            self.model = whisper.load_model(model_name)

        try:
            from openwakeword.vad import VAD
            self.vad = VAD()
        except Exception as e:
            self.vad = None
            if verbose:
                print(f"  [Wake] silero VAD unavailable ({e}); falling back to energy gating.")

    # ── gating ───────────────────────────────────────────────────────────────

    def _is_speech(self, chunk: np.ndarray) -> bool:
        if self.vad is not None:
            try:
                score = float(self.vad.predict(chunk))
            except Exception:
                score = 0.0
            self.recent_vad.append(score)
            if max(self.recent_vad) >= VAD_THRESHOLD:
                return True
        # Energy fallback, and a second chance when the VAD is being shy. Loud
        # enough to be someone talking at a laptop, quiet enough to ignore fans.
        return float(np.sqrt(np.mean(np.square(chunk)))) > 0.012

    # ── the loop calls this ──────────────────────────────────────────────────

    def _transcribe(self, audio: np.ndarray, precise: bool = False) -> str:
        model = (self.command_model or self.model) if precise else self.model
        try:
            result = model.transcribe(audio, fp16=False,
                                      beam_size=5 if precise else 1, language="en")
        except Exception as e:
            if self.verbose:
                print(f"  [Wake] transcription failed: {e}")
            return ""
        return (result.get("text") or "").strip()

    def feed(self, chunk, final: bool = False) -> Optional[str]:
        """
        Push one chunk of audio.

        Returns None while nothing has happened, and while a sentence is still
        being spoken. On completion returns whatever followed the wake word — an
        empty string if the wake word was all you said, in which case the caller
        should open the microphone and listen properly.

        `final=True` forces the capture to close, for feeding a fixed recording.
        """
        if isinstance(chunk, np.ndarray) and chunk.dtype == np.int16:
            chunk = chunk.astype(np.float32) / 32768.0
        chunk = np.asarray(chunk, dtype=np.float32).flatten()

        self.clock += len(chunk) / SAMPLE_RATE
        now = self.clock
        speech = self._is_speech(chunk)

        # ── already awake: collect until the sentence ends ────────────────────
        if self.capturing:
            self.capture.append(chunk)
            if speech:
                self.silence_started = None
            elif self.silence_started is None:
                self.silence_started = now

            long_enough = self.silence_started is not None and \
                now - self.silence_started >= END_OF_SPEECH
            too_long = now - self.capture_started >= MAX_UTTERANCE
            if not (final or long_enough or too_long):
                return None

            audio = np.concatenate(self.capture) if self.capture else np.zeros(1, np.float32)
            text = self._transcribe(audio, precise=True)
            if self.verbose:
                print(f"  [WAKE] full utterance: {text!r}")
            self._end_capture()
            return trailing_command(text, self.phrase)

        # ── idle: look for the phrase in a rolling window ────────────────────
        self.buffer.extend(chunk)
        if SPEAKING.is_set() or now < self.muted_until:
            return None
        # Half a second is enough for Whisper, which pads internally anyway.
        # Requiring a full second missed "hey Diablo" said into silence.
        if len(self.buffer) < SAMPLE_RATE // 2:
            return None
        if not speech:
            return None
        if now - self.last_transcribed < MIN_GAP:
            return None

        self.last_transcribed = now
        text = self._transcribe(np.array(self.buffer, dtype=np.float32))
        if not text or phrase_position(text, self.phrase) is None:
            return None

        if self.verbose:
            print(f"\n  [WAKE] heard: {text!r}")

        # Keep the audio that woke it. The phrase and the request are usually
        # one breath, so throwing this away loses the first half of the command.
        self.capturing = True
        self.capture = [np.array(self.buffer, dtype=np.float32)]
        self.capture_started = now
        self.silence_started = None
        self.buffer.clear()
        if final:
            return self.feed(np.zeros(0, dtype=np.float32), final=True)
        return None

    def _end_capture(self) -> None:
        self.capturing = False
        self.capture = []
        self.silence_started = None
        self.muted_until = self.clock + COOLDOWN
        self.reset()

    def reset(self) -> None:
        self.buffer.clear()
        self.recent_vad.clear()
        if self.vad is not None:
            try:
                self.vad.reset_states()
            except Exception:
                pass

    def mute_for(self, seconds: float) -> None:
        """
        Stop listening for the wake word for a stretch of audio.

        Used while a command or a clarifying question is being captured
        elsewhere, so the same words are not treated as a second wake.
        """
        self.muted_until = max(self.muted_until, self.clock + seconds)


if __name__ == "__main__":
    # Offline check against synthesised speech. No microphone needed.
    import subprocess
    import tempfile
    import wave

    print(f"Wake phrase: {PHRASE!r}   model: {WAKE_MODEL}\n")

    positives = [
        "hey diablo",
        "hey diablo what is on my screen",
        "diablo can you read my chrome tab",
        "okay diablo open my econ folder",
    ]
    negatives = [
        "the weather today is quite nice actually",
        "I need to finish this diagram before tomorrow",
        "let me open the app below the fold",
        "what did the examiner say about criterion b",
    ]

    import whisper
    print("Loading tiny.en (detection) and base (commands)...")
    listener = WakeListener(
        whisper_model=whisper.load_model(WAKE_MODEL),
        command_model=whisper.load_model(os.getenv("DIABLO_COMMAND_MODEL", "base")),
        verbose=False,
    )
    passed = failed = 0

    def run_clip(listener: WakeListener, audio: np.ndarray) -> Optional[str]:
        """Play a fixed recording through the listener and close the capture."""
        listener.capturing = False
        listener.capture = []
        listener.reset()
        listener.muted_until = 0.0
        listener.last_transcribed = 0.0
        listener.clock = 0.0
        for i in range(0, max(len(audio) - 1280, 0), 1280):
            hit = listener.feed(audio[i:i + 1280])
            if hit is not None:
                return hit
        # The clip ended mid-sentence; close it out the way real silence would.
        return listener.feed(np.zeros(1280, dtype=np.int16), final=True)

    def synth(sentence: str) -> np.ndarray:
        with tempfile.TemporaryDirectory() as tmp:
            aiff, wav = os.path.join(tmp, "a.aiff"), os.path.join(tmp, "a.wav")
            subprocess.run(["say", "-v", "Daniel", "-o", aiff, sentence], check=True)
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                            aiff, wav], check=True, capture_output=True)
            with wave.open(wav) as w:
                return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)

    for sentence in positives:
        audio = synth(sentence)
        hit = run_clip(listener, audio)
        ok = hit is not None
        print(f"  {'✓' if ok else '✗'} wakes on {sentence!r}"
              + (f"  → command {hit!r}" if ok else ""))
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)

    for sentence in negatives:
        audio = synth(sentence)
        hit = run_clip(listener, audio)
        ok = hit is None
        print(f"  {'✓' if ok else '✗'} ignores {sentence!r}"
              + ("" if ok else f"  → FALSE WAKE, command {hit!r}"))
        passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)

    print(f"\n{passed}/{passed + failed} passed")
    sys.exit(0 if failed == 0 else 1)
