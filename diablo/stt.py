"""
Turning what he said into words.

One interface, two engines behind it, chosen by measurement rather than
preference. Benchmarked on 36 clips at three speaking rates:

    base.en (Whisper)             WER 7.6%   733 ms per clip
    parakeet-tdt-0.6b-v3          WER 5.0%   247 ms per clip

A third fewer errors and three times faster. Several of Parakeet's remaining
"errors" are it being right and the reference being sloppy — it renders "open
github dot com" as "Open GitHub.com", which is what was meant.

Memory, measured rather than taken from a blog: 950 MB resident after a
transcription. A figure of 16 GB was quoted at one point in this project's
history; that was a recommended machine specification and it was repeated here
without checking, which was wrong of me.

Whisper stays as the fallback and is not going anywhere. Parakeet covers
English and 24 European languages; Whisper covers about a hundred, so anything
outside that set — Hindi, most obviously — still needs it.
"""

import os
import sys
import tempfile
import threading
import wave
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from skills import config  # noqa: E402

ENGINE = os.getenv("DIABLO_STT_ENGINE", "auto").strip().lower()
PARAKEET_MODEL = os.getenv("DIABLO_PARAKEET_MODEL",
                           "mlx-community/parakeet-tdt-0.6b-v3")
WHISPER_MODEL = os.getenv("DIABLO_STT_MODEL", "base.en")
SAMPLE_RATE = 16000

_lock = threading.Lock()


def _write_wav(audio: np.ndarray, path: str) -> None:
    """Float32 mono at 16 kHz to a real file, because Parakeet reads paths."""
    clipped = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes((clipped * 32767).astype(np.int16).tobytes())


class Parakeet:
    """NVIDIA Parakeet TDT through MLX."""

    name = "parakeet"

    def __init__(self, model_id: str = PARAKEET_MODEL):
        from parakeet_mlx import from_pretrained
        self.model = from_pretrained(model_id)
        self.model_id = model_id

    def transcribe(self, audio, **_ignored) -> Dict[str, Any]:
        """
        Whisper's shape, so nothing upstream has to know which engine ran.

        `_ignored` swallows fp16, beam_size and the rest of Whisper's keyword
        arguments. They are meaningless here and the alternative is editing
        every call site to ask which engine it is talking to, which is exactly
        the coupling this module exists to prevent.
        """
        if isinstance(audio, (str, os.PathLike)):
            with _lock:
                return {"text": self.model.transcribe(str(audio)).text}
        # A temp file per utterance, which costs a few milliseconds against
        # the 486 this engine saves.
        handle, path = tempfile.mkstemp(suffix=".wav", prefix="diablo_stt_")
        os.close(handle)
        try:
            _write_wav(audio, path)
            with _lock:
                return {"text": self.model.transcribe(path).text}
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class Whisper:
    """OpenAI Whisper, local. The fallback, and the multilingual one."""

    name = "whisper"

    def __init__(self, model_name: str = WHISPER_MODEL):
        import whisper
        self.model = whisper.load_model(model_name)
        self.model_id = model_name

    def transcribe(self, audio, **kwargs) -> Dict[str, Any]:
        kwargs.setdefault("fp16", False)
        with _lock:
            return self.model.transcribe(audio, **kwargs)


def load(prefer: Optional[str] = None):
    """
    The best engine available, or the one asked for.

    Never raises for a missing engine: a machine without MLX still has
    Whisper, and an assistant that cannot hear is worse than a slower one.
    """
    wanted = (prefer or ENGINE or "auto").lower()

    if wanted in ("auto", "parakeet"):
        try:
            engine = Parakeet()
            print(f"  [STT] parakeet {engine.model_id}")
            return engine
        except Exception as e:
            if wanted == "parakeet":
                print(f"  [STT] parakeet unavailable ({type(e).__name__}: {e}); "
                      f"falling back to whisper")
            elif os.getenv("DIABLO_DEBUG"):
                print(f"  [STT] parakeet unavailable: {type(e).__name__}: {e}")

    engine = Whisper()
    print(f"  [STT] whisper {engine.model_id}")
    return engine


if __name__ == "__main__":
    import time
    clip = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "command.wav")
    for prefer in ("parakeet", "whisper"):
        try:
            began = time.time()
            engine = load(prefer)
            loaded = time.time() - began
            began = time.time()
            text = engine.transcribe(clip)["text"].strip()
            print(f"    load {loaded:5.2f}s  run {time.time()-began:5.2f}s  {text!r}\n")
        except Exception as e:
            print(f"    {prefer} failed: {type(e).__name__}: {e}\n")
