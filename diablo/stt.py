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
    """
    NVIDIA Parakeet TDT through MLX.

    Every call into the model runs on one dedicated thread, always the same
    one. MLX keeps its streams per thread, so a model loaded during boot and
    then used from the push-to-talk worker raises "There is no Stream(cpu, 1)
    in current thread" — which arrives as a failed transcription and looks
    exactly like not hearing anything. A lock is not enough: it serialises the
    calls but does not move them onto the thread that owns the streams.
    """

    name = "parakeet"

    def __init__(self, model_id: str = PARAKEET_MODEL):
        from concurrent.futures import ThreadPoolExecutor
        self._pool = ThreadPoolExecutor(max_workers=1,
                                        thread_name_prefix="parakeet")
        self.model_id = model_id
        self.model = self._pool.submit(self._load, model_id).result()

    @staticmethod
    def _load(model_id: str):
        from parakeet_mlx import from_pretrained
        return from_pretrained(model_id)

    def _run(self, fn):
        return self._pool.submit(fn).result()

    def transcribe(self, audio, **_ignored) -> Dict[str, Any]:
        """
        Whisper's shape, so nothing upstream has to know which engine ran.

        `_ignored` swallows fp16, beam_size and the rest of Whisper's keyword
        arguments. They are meaningless here, and the alternative is editing
        every call site to ask which engine it is talking to, which is exactly
        the coupling this module exists to prevent.

        Audio already in memory goes straight to the mel spectrogram. The
        obvious version wrote a temp WAV and handed over the path, which meant
        Parakeet decoded it by shelling out to ffmpeg — and an application
        launched from Finder has no /opt/homebrew/bin on PATH, so every single
        utterance failed with "FFmpeg is not installed" while ffmpeg sat there
        on disk. Diablo was deaf for a day because of a temp file it did not
        need to write.
        """
        if isinstance(audio, (str, os.PathLike)):
            return {"text": self._run(
                lambda: self.model.transcribe(str(audio)).text)}

        samples = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)

        def work():
            import mlx.core as mx
            from parakeet_mlx.audio import get_logmel
            mel = get_logmel(mx.array(samples), self.model.preprocessor_config)
            results = self.model.generate(mel)
            return results[0].text if results else ""

        return {"text": self._run(work)}


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
