"""
D.I.A.B.L.O. — GoClick, the grounding specialist.

GoClick-Large (0.8B, Florence-2-large fine-tuned for GUI grounding, MIT) does
one thing: given a screenshot and a description, it says where the described
element is. It is not the brain, it plans nothing, and it cannot press
anything. What it returns is a GroundingProposal, and grounding_gate decides
whether that point is anything at all.

How it runs
-----------
In a worker process, started the first time it is needed and stopped after
DIABLO_GROUNDER_KEEP_ALIVE seconds idle, the way Ollama releases a model. A
separate process keeps its 1.7 GB, its GPU state and any crash out of the
assistant itself, and keeps the one process that can post a click free of a
second model.

The model code is MLX-VLM's own Florence-2 implementation, not the code that
ships in GoClick's repository: nothing from the model repository is executed,
only its weights are read. The decoding loop is here rather than MLX-VLM's
general one because GoClick's answers depend on one detail of its generation
config, no_repeat_ngram_size=3: the model emits "<s>" several times before the
coordinates and only that rule stops it repeating forever. Without it, three
targets in four came back as a run of "<s>". With it, the coordinates match the
reference PyTorch implementation token for token (measured 2026-10-05, 8 of 8).

Weights
-------
The installer fetches revision f923d830f29dab50dd324d651ddbb2be8d2d8a1d of
HongxinLi/GoClick-Large, safetensors only, and checks the sha256 below. The
pickle files in that repository (pytorch_model.bin, optimizer.pt, the rng
states) are never downloaded.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from skills import config  # noqa: E402

MODEL_NAME = "goclick-large"
REPOSITORY = "HongxinLi/GoClick-Large"
REVISION = "f923d830f29dab50dd324d651ddbb2be8d2d8a1d"
WEIGHTS_SHA256 = "e155185f315646a371088e96b68760866978e570087a84d75b134d07b53af3a7"
WEIGHTS_BYTES = 3317087180
# Everything the worker reads. Code files from the repository are not in it.
FILES = ("config.json", "generation_config.json", "preprocessor_config.json",
         "tokenizer.json", "tokenizer_config.json", "vocab.json", "model.safetensors")

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
MODEL_DIR = os.getenv("DIABLO_GOCLICK_PATH", "").strip() or os.path.join(
    DIABLO_HOME, "models", MODEL_NAME)
# The interpreter that has mlx-vlm. Diablo's own by default.
PYTHON = os.getenv("DIABLO_GROUNDER_PYTHON", "").strip() or sys.executable
KEEP_ALIVE = config.env_float("DIABLO_GROUNDER_KEEP_ALIVE", 600.0, low=10.0, high=86400.0)
TIMEOUT = config.env_float("DIABLO_GROUNDER_TIMEOUT", 60.0, low=5.0, high=600.0)

# GoClick's description-grounding prompt, from its model card.
PROMPT = "Where is the {target} element? (Output the center coordinates of the target)"
NGRAM = 3            # no_repeat_ngram_size in GoClick's generation config
DECODER_START = 2    # decoder_start_token_id
FORCED_BOS = 0       # forced_bos_token_id
EOS = 2
MAX_STEPS = 16       # "<s>" a few times, "(", two locations, ",", ")", "</s>"


def installed() -> bool:
    """The weights are where the worker will look, at the size they should be."""
    weights = os.path.join(MODEL_DIR, "model.safetensors")
    try:
        return (all(os.path.exists(os.path.join(MODEL_DIR, f)) for f in FILES)
                and os.path.getsize(weights) == WEIGHTS_BYTES)
    except OSError:
        return False


def parse(text: str) -> Optional[tuple]:
    """GoClick's answer as a point in 0..1 of the image, or None."""
    found = re.findall(r"<loc_(\d+)>", text or "")
    if len(found) < 2:
        return None
    u, v = int(found[0]), int(found[1])
    if not (0 <= u <= 999 and 0 <= v <= 999):
        return None
    # Florence-2 quantises each axis into 1000 bins; the centre of a bin is
    # the honest reading of it.
    return ((u + 0.5) / 1000.0, (v + 0.5) / 1000.0)


# ─── The client, in Diablo's process ─────────────────────────────────────────

class _Worker:
    """One worker process, started on demand and stopped when idle."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._last = 0.0
        self._timer: Optional[threading.Timer] = None
        self.load_ms: Optional[float] = None

    def _start(self) -> None:
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   PYTHONUNBUFFERED="1")
        home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._proc = subprocess.Popen(
            [PYTHON, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                           "from skills import goclick; goclick.serve()", home],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=env)
        ready = self._read(TIMEOUT)
        if not ready or not ready.get("ready"):
            self.stop()
            raise RuntimeError((ready or {}).get("error", "the grounder did not start"))
        self.load_ms = ready.get("load_ms")

    def _read(self, timeout: float) -> Optional[Dict[str, Any]]:
        result: Dict[str, Any] = {}

        def read():
            try:
                result["line"] = self._proc.stdout.readline()
            except Exception:
                result["line"] = ""

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout)
        line = result.get("line")
        if not line:
            return None
        try:
            return json.loads(line)
        except ValueError:
            return None

    def ask(self, image: str, target: str) -> Dict[str, Any]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            self._proc.stdin.write(json.dumps({"image": image, "target": target}) + "\n")
            self._proc.stdin.flush()
            reply = self._read(TIMEOUT)
            self._last = time.time()
            self._arm()
            if reply is None:
                self.stop()
                raise RuntimeError("the grounder did not answer in time")
            return reply

    def _arm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(KEEP_ALIVE, self._idle)
        self._timer.daemon = True
        self._timer.start()

    def _idle(self) -> None:
        with self._lock:
            if time.time() - self._last >= KEEP_ALIVE - 1:
                self.stop()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


_WORKER = _Worker()


def available() -> bool:
    return installed()


def propose(frame: Any, target: str):
    """
    Where GoClick thinks `target` is in this frame, as a GroundingProposal in
    FRAME_NORMALISED space, or None. Never a click, never a label.
    """
    from skills import grounding_gate as _gate
    if not installed() or not getattr(frame, "ok", False) or not getattr(frame, "path", None):
        return None
    try:
        reply = _WORKER.ask(frame.path, target)
    except Exception:
        return None
    point = parse(reply.get("raw", ""))
    if point is None:
        return None
    return _gate.GroundingProposal(
        frame_id=frame.frame_id, space=_gate.Space.FRAME_NORMALISED,
        x=point[0], y=point[1], target=target, source_model=MODEL_NAME,
        reasoning=f"{reply.get('ms', 0):.0f} ms")


def stop() -> None:
    _WORKER.stop()


# ─── The worker, in its own process ──────────────────────────────────────────

def _blocked(seq) -> set:
    """Tokens that would repeat a 3-gram already in the sequence."""
    if len(seq) < NGRAM - 1:
        return set()
    prefix = tuple(seq[-(NGRAM - 1):])
    return {seq[i + NGRAM - 1] for i in range(len(seq) - NGRAM + 1)
            if tuple(seq[i:i + NGRAM - 1]) == prefix}


def serve() -> None:
    """Read {"image", "target"} lines on stdin, answer with GoClick's raw text."""
    started = time.time()
    try:
        import mlx.core as mx
        from mlx.utils import tree_map
        from PIL import Image
        from mlx_vlm import load
        model, processor = load(MODEL_DIR, trust_remote_code=False)
        # float16 halves the resident size (3.3 GB to 1.7 GB) and gave the
        # same coordinates as float32 on every measured case.
        model.update(tree_map(lambda p: p.astype(mx.float16)
                              if isinstance(p, mx.array) and p.dtype == mx.float32 else p,
                              model.parameters()))
        mx.eval(model.parameters())
    except Exception as e:
        print(json.dumps({"ready": False, "error": f"{type(e).__name__}: {e}"[:300]}),
              flush=True)
        return
    print(json.dumps({"ready": True, "load_ms": round((time.time() - started) * 1000)}),
          flush=True)
    lm = model.language_model

    for line in sys.stdin:
        began = time.time()
        try:
            request = json.loads(line)
            with Image.open(request["image"]) as opened:
                image = opened.convert("RGB")
            prompt = PROMPT.format(target=str(request["target"])[:200])
            encoded_in = processor(text=[prompt], images=[image], return_tensors="np")
            features = model.get_input_embeddings(
                mx.array(encoded_in["input_ids"]),
                mx.array(encoded_in["pixel_values"]).astype(mx.float16))
            mask = features.attention_mask
            if mask is not None:
                mask = mask.astype(mx.float16)
            cache, encoded, seq = lm.make_cache(), None, [DECODER_START]
            for step in range(MAX_STEPS):
                out = lm(inputs_embeds=features.inputs_embeds if encoded is None else None,
                         attention_mask=mask, decoder_input_ids=mx.array([[seq[-1]]]),
                         encoder_outputs=encoded, cache=cache)
                encoded = out.encoder_outputs
                if step == 0:
                    token = FORCED_BOS
                else:
                    logits = out.logits[0, -1].astype(mx.float32)
                    ban = _blocked(seq)
                    if ban:
                        logits[mx.array(sorted(ban))] = -float("inf")
                    token = int(mx.argmax(logits).item())
                seq.append(token)
                if step > 0 and token == EOS:
                    break
            raw = processor.tokenizer.decode(seq[1:], skip_special_tokens=False)
            reply = {"raw": raw, "ms": round((time.time() - began) * 1000, 1)}
        except Exception as e:
            reply = {"raw": "", "error": f"{type(e).__name__}"[:80],
                     "ms": round((time.time() - began) * 1000, 1)}
        print(json.dumps(reply), flush=True)
