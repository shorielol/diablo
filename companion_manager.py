"""
D.I.A.B.L.O. Companion Manager
===============================
Central state machine for the voice assistant runtime.

Every subsystem — mic, wake word, voice, brain, screen — reports to this
object and is started, stopped, and health-checked through it. Nothing
else in the stack should be managing lifecycle or tracking what state
Diablo is in; if it is, it belongs here instead.

The design is stolen from the right place: Clicky's CompanionManager
owns everything in that app the same way. The implementation is original
because the stacks are different — Swift/SwiftUI with Combine publishers
vs Python with threads and callbacks — and because Diablo runs offline
on Ollama, not through cloud APIs.

Two deliberate departures from the brief that asked for this:

  Threads, not asyncio. voice_bridge.py contains zero `async def` and
  seven threading constructs; the audio callback is a C callback from
  PortAudio and the wake listener owns its own thread. Bolting an event
  loop onto that would mean rewriting the parts that already work, so
  `start`, `stop` and `handle_utterance` are ordinary blocking calls.

  The wake gate is load-bearing. voice_bridge's set_ui_state does one
  thing that is not cosmetic: it sets wake_word.SPEAKING while Diablo
  talks, because Diablo hears itself through the speakers and otherwise
  says its own name and instantly wakes itself. That behaviour moves
  here intact rather than being reinvented.
"""

import json
import logging
import os
import sys
import threading
import time
import uuid
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

log = logging.getLogger("diablo.manager")

# ─── Configuration ───────────────────────────────────────────────────────────

DIABLO_HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
ENV_PATH = os.path.join(DIABLO_HOME, ".env")


def load_env(path: str = ENV_PATH) -> Dict[str, str]:
    """
    Put ~/.diablo/.env into the environment, without overriding what is there.

    Diablo's settings live in that file and the launcher sources it before
    starting voice_bridge. Nothing else does — so `diablo bench`, the test
    suites and `doctor` were all running against code defaults rather than the
    configuration the assistant actually uses. A benchmark of a different model
    than the one you are talking to is worse than no benchmark.

    Real environment variables win, so `DIABLO_MODEL=x python3 ...` still works
    for a one-off.
    """
    loaded: Dict[str, str] = {}
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                loaded[key] = value
                os.environ.setdefault(key, value)
    except OSError:
        pass
    return loaded


# Read it at import, before any subsystem module is imported, because those
# capture their settings into module constants at import time.
_ENV_FILE = load_env()



# ─── States ──────────────────────────────────────────────────────────────────

class DiabloState(Enum):
    COLD = "cold"              # Not started yet
    INITIALIZING = "init"      # Subsystems booting
    IDLE = "idle"              # Listening for the wake word
    LISTENING = "listening"    # Capturing what was said
    PROCESSING = "processing"  # Thinking, or running the agent
    RESPONDING = "responding"  # Speaking the answer
    ERROR = "error"            # Broken, but recoverable
    SHUTDOWN = "shutdown"      # Tearing down


# Every edge Diablo is allowed to take. Anything not listed is a bug
# somewhere, and the interesting part is that it is a bug worth hearing
# about rather than dying on: a voice assistant that raises on an odd
# transition goes silent, and silence is the one failure mode with no
# symptom. Invalid transitions are logged and refused.
TRANSITIONS: Dict[DiabloState, set] = {
    DiabloState.COLD:         {DiabloState.INITIALIZING, DiabloState.SHUTDOWN},
    DiabloState.INITIALIZING: {DiabloState.IDLE, DiabloState.ERROR, DiabloState.SHUTDOWN},
    DiabloState.IDLE:         {DiabloState.LISTENING, DiabloState.PROCESSING,
                               DiabloState.RESPONDING, DiabloState.ERROR,
                               DiabloState.SHUTDOWN},
    DiabloState.LISTENING:    {DiabloState.PROCESSING, DiabloState.IDLE,
                               DiabloState.ERROR, DiabloState.SHUTDOWN},
    DiabloState.PROCESSING:   {DiabloState.RESPONDING, DiabloState.LISTENING,
                               DiabloState.IDLE, DiabloState.ERROR,
                               DiabloState.SHUTDOWN},
    DiabloState.RESPONDING:   {DiabloState.IDLE, DiabloState.LISTENING,
                               DiabloState.PROCESSING, DiabloState.ERROR,
                               DiabloState.SHUTDOWN},
    # ERROR is recoverable on purpose. Ollama comes back, the mic is
    # re-probed, Voicebox is relaunched — none of those should need a restart.
    DiabloState.ERROR:        {DiabloState.IDLE, DiabloState.INITIALIZING,
                               DiabloState.SHUTDOWN},
    DiabloState.SHUTDOWN:     {DiabloState.COLD},
}


# ─── Subsystems ──────────────────────────────────────────────────────────────

class Subsystem:
    """
    One thing the manager owns.

    Wrappers, not replacements. Each holds a reference to the existing module
    and adds three things it does not have: a name, a start/stop that cannot
    raise, and a health dictionary in a shape common to all of them.
    """

    name = "subsystem"
    required = False       # if True, failing to start puts Diablo in ERROR

    def __init__(self, manager: "CompanionManager"):
        self.manager = manager
        self.ready = False
        self.error: Optional[str] = None

    def start(self) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        pass

    def health(self) -> Dict[str, Any]:
        return {"ready": self.ready, "error": self.error}

    def _fail(self, exc: BaseException) -> bool:
        self.ready = False
        self.error = f"{type(exc).__name__}: {exc}"
        log.warning("[Manager] %s unavailable — %s", self.name, self.error)
        self.manager.emit("error", subsystem=self.name, message=self.error)
        return False


class BrainSubsystem(Subsystem):
    """Ollama. Diablo starts without it and says so; it is not required."""

    name = "brain"

    def start(self) -> bool:
        try:
            from skills import local_brain
            self.module = local_brain
            self.ready = local_brain.available()
            self.error = None if self.ready else "Ollama is not answering"
            self.manager.emit("brain_online" if self.ready else "brain_offline",
                              model=local_brain.CHAT_MODEL)
            return self.ready
        except BaseException as e:
            return self._fail(e)

    def health(self) -> Dict[str, Any]:
        base = super().health()
        try:
            from skills import local_brain
            base.update({
                "model_name": local_brain.CHAT_MODEL,
                "is_online": local_brain.available(),
                "model_loaded": self._resident(local_brain),
                "context": local_brain.NUM_CTX,
                "keep_alive": local_brain.KEEP_ALIVE,
            })
        except BaseException as e:
            base["error"] = str(e)
        return base

    @staticmethod
    def _resident(local_brain) -> bool:
        """Whether the weights are in memory right now, not merely installed."""
        try:
            import urllib.request
            with urllib.request.urlopen(
                    f"{local_brain.OLLAMA_HOST}/api/ps", timeout=2) as response:
                loaded = json.loads(response.read()).get("models", [])
            return any(m.get("name") == local_brain.CHAT_MODEL for m in loaded)
        except Exception:
            return False


class VoiceSubsystem(Subsystem):
    """Voicebox, the disk cache, and macOS `say` underneath both."""

    name = "voice"

    def __init__(self, manager):
        super().__init__(manager)
        self.is_speaking = False
        self.voice_route: Optional[str] = None

    def start(self) -> bool:
        try:
            from skills import voice
            self.module = voice
            self.ready = True            # always usable; `say` is the floor
            self.error = None if voice.available() else "Voicebox unreachable, using macOS voice"
            return True
        except BaseException as e:
            return self._fail(e)

    def say(self, text: str) -> str:
        """Speak, and record which of the three routes actually carried it."""
        from skills import voice
        self.is_speaking = True
        self.manager.transition(DiabloState.RESPONDING)
        try:
            self.voice_route = voice.speak(text)
            return self.voice_route
        except BaseException as e:
            log.warning("[Manager] speech failed — %s", e)
            self.voice_route = "failed"
            return "failed"
        finally:
            self.is_speaking = False

    def health(self) -> Dict[str, Any]:
        base = super().health()
        try:
            from skills import voice
            base.update({
                "is_speaking": self.is_speaking,
                "voice_route": self.voice_route,
                "profile": voice.VOICE_ID,
                "voicebox_up": voice.available(),
                "cached_lines": voice.cache_size(),
            })
        except BaseException as e:
            base["error"] = str(e)
        return base


class MicSubsystem(Subsystem):
    """
    The input device.

    Required. Everything else can degrade, but an assistant that cannot hear
    is not degraded, it is off.
    """

    name = "mic"
    required = True

    def __init__(self, manager):
        super().__init__(manager)
        self.current_device: Optional[Dict[str, Any]] = None
        self.audio_power_level = 0.0

    def start(self) -> bool:
        try:
            from skills import mic
            self.module = mic
            self.current_device = mic.load_choice()
            if self.current_device is None:
                self.ready = False
                self.error = "no microphone has been chosen yet — run `diablo mic`"
                self.manager.emit("mic_lost", reason=self.error)
                return False
            self.ready = True
            self.error = None
            self.manager.emit("mic_ready", device=self.current_device.get("name"))
            return True
        except BaseException as e:
            return self._fail(e)

    def note_level(self, peak: float) -> None:
        """Called from the audio thread. Assignment of a float is atomic."""
        self.audio_power_level = peak

    def health(self) -> Dict[str, Any]:
        base = super().health()
        base.update({
            "current_device": (self.current_device or {}).get("name"),
            "rate": (self.current_device or {}).get("rate"),
            "audio_power_level": round(self.audio_power_level, 4),
        })
        return base


class WakeWordSubsystem(Subsystem):
    """Wake detection, and the gate that stops Diablo waking itself up."""

    name = "wake_word"

    def __init__(self, manager):
        super().__init__(manager)
        self.is_listening = False
        self.last_trigger_time: Optional[float] = None

    def start(self) -> bool:
        try:
            from skills import wake_word
            self.module = wake_word
            self.ready = True
            self.is_listening = True
            self.error = None
            return True
        except BaseException as e:
            return self._fail(e)

    def stop(self) -> None:
        self.is_listening = False

    def note_trigger(self, phrase: str, confidence: float = 1.0) -> None:
        self.last_trigger_time = time.time()
        self.manager.emit("wake_detected", phrase=phrase, confidence=confidence)

    def set_speaking(self, speaking: bool) -> None:
        """
        Deafen the wake word while Diablo talks.

        Not cosmetic. Diablo hears itself through the speakers, and without
        this it says its own name mid-reply and wakes itself up.
        """
        try:
            from skills import wake_word
            if speaking:
                wake_word.SPEAKING.set()
            else:
                wake_word.SPEAKING.clear()
        except BaseException:
            pass

    def health(self) -> Dict[str, Any]:
        base = super().health()
        try:
            from skills import wake_word
            base.update({
                "phrase": wake_word.PHRASE,
                "is_listening": self.is_listening,
                "last_trigger_time": self.last_trigger_time,
                "muted_for_speech": wake_word.SPEAKING.is_set(),
            })
        except BaseException as e:
            base["error"] = str(e)
        return base


class ScreenSubsystem(Subsystem):
    """Window listing and per-window OCR. Optional."""

    name = "screen"

    def __init__(self, manager):
        super().__init__(manager)
        self.last_capture_app: Optional[str] = None

    def start(self) -> bool:
        try:
            from skills import screen_context
            self.module = screen_context
            self.ready = screen_context.ocr_available()
            self.error = None if self.ready else "on-device OCR is not installed"
            return self.ready
        except BaseException as e:
            return self._fail(e)

    def health(self) -> Dict[str, Any]:
        base = super().health()
        try:
            from skills import screen_context
            windows = screen_context.list_windows()
            base.update({
                "has_permission": bool(windows),
                "windows_visible": len(windows),
                "last_capture_app": self.last_capture_app,
                "ocr_available": screen_context.ocr_available(),
            })
        except BaseException as e:
            base["error"] = str(e)
        return base


class ConversationSubsystem(Subsystem):
    """The rolling session. Nothing to start; it is state, not a service."""

    name = "conversation"

    def start(self) -> bool:
        try:
            from skills import conversation
            self.module = conversation
            self.ready = True
            self.error = None
            return True
        except BaseException as e:
            return self._fail(e)

    def stop(self) -> None:
        try:
            from skills import conversation
            conversation.session.reset()
        except BaseException:
            pass

    def health(self) -> Dict[str, Any]:
        base = super().health()
        try:
            from skills import conversation
            session = conversation.session
            age = (time.time() - session.last_spoke_at) if session.last_spoke_at else None
            base.update({
                "turn_count": len(session.turns) // 2,
                "is_followup": session.is_followup(),
                "session_age": round(age, 1) if age is not None else None,
                "last_screen_app": session.last_screen_app,
            })
        except BaseException as e:
            base["error"] = str(e)
        return base


# ─── The manager ─────────────────────────────────────────────────────────────

class CompanionManager:
    """
    One object that knows what Diablo is doing and what is working.

    Safe to read from any thread. State changes go through `transition`, which
    is the only writer and holds the lock for the duration of the check and the
    assignment. Listener callbacks are invoked outside the lock, because a
    callback that calls back into the manager would otherwise deadlock.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = DiabloState.COLD
        self._listeners: Dict[str, tuple] = {}      # id -> (event, callback)
        self._state_history: List[tuple] = []       # (ts, from, to)
        self._started_at: Optional[float] = None
        self._last_error: Optional[str] = None

        self.brain = BrainSubsystem(self)
        self.voice = VoiceSubsystem(self)
        self.mic = MicSubsystem(self)
        self.wake = WakeWordSubsystem(self)
        self.screen = ScreenSubsystem(self)
        self.conversation = ConversationSubsystem(self)

        # Start order is deliberate. The brain first because it is the slowest
        # to warm and the most likely to be missing; voice next so there is a
        # way to say what went wrong; the mic and wake word last, because the
        # moment they are live Diablo is expected to respond.
        self.subsystems: List[Subsystem] = [
            self.brain, self.voice, self.mic, self.wake,
            self.screen, self.conversation,
        ]

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def state(self) -> DiabloState:
        with self._lock:
            return self._state

    def transition(self, new_state: DiabloState, reason: str = "") -> bool:
        """
        Move to a new state, or refuse and say why.

        Returns True if the move happened. A refused transition is a warning
        and nothing else: it never raises, because the caller is usually a
        worker thread mid-utterance and an exception there is a silent death.
        """
        with self._lock:
            old = self._state
            if new_state == old:
                return True
            if new_state not in TRANSITIONS.get(old, set()):
                log.warning("[Manager] refused %s -> %s%s",
                            old.value, new_state.value,
                            f" ({reason})" if reason else "")
                return False
            self._state = new_state
            self._state_history.append((time.time(), old, new_state))
            if len(self._state_history) > 200:
                self._state_history = self._state_history[-200:]

        log.info("[Manager] %s -> %s%s", old.value, new_state.value,
                 f"  {reason}" if reason else "")
        # Outside the lock: the wake gate touches another module, and a
        # listener may well call back in here.
        self.wake.set_speaking(new_state is DiabloState.RESPONDING)
        self.emit("state_changed", old_state=old.value,
                  new_state=new_state.value, reason=reason)
        return True

    # ── observers ────────────────────────────────────────────────────────────

    def on(self, event: str, callback: Callable) -> str:
        """Subscribe. `event` of "*" hears everything."""
        listener_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._listeners[listener_id] = (event, callback)
        return listener_id

    def off(self, listener_id: str) -> None:
        with self._lock:
            self._listeners.pop(listener_id, None)

    def emit(self, event: str, **data: Any) -> None:
        """
        Notify listeners. Never raises, never blocks the caller on a bad one.

        The list is copied under the lock and the callbacks run outside it, so
        a listener is free to subscribe, unsubscribe, or read state from inside
        its own callback.
        """
        with self._lock:
            targets = [cb for want, cb in self._listeners.values()
                       if want == event or want == "*"]
        payload = dict(data, event=event, ts=time.time())
        for callback in targets:
            try:
                callback(payload)
            except BaseException as e:
                log.warning("[Manager] listener for %r raised %s: %s",
                            event, type(e).__name__, e)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Bring every subsystem up, in order, without letting one stop the rest.

        Returns True if Diablo reached IDLE. A missing brain or a silent
        Voicebox does not prevent that — only the microphone does, because
        there is nothing to degrade to when you cannot hear.
        """
        if self.state not in (DiabloState.COLD, DiabloState.ERROR, DiabloState.SHUTDOWN):
            log.warning("[Manager] start() ignored — already %s", self.state.value)
            return self.state is DiabloState.IDLE

        if self.state is DiabloState.SHUTDOWN:
            self.transition(DiabloState.COLD)
        self.transition(DiabloState.INITIALIZING, "start()")
        self._started_at = time.time()

        blocked = []
        for subsystem in self.subsystems:
            began = time.time()
            ok = False
            try:
                ok = subsystem.start()
            except BaseException as e:          # a wrapper that itself misbehaves
                subsystem._fail(e)
            took = (time.time() - began) * 1000
            log.info("[Manager] %-13s %-9s %5.0f ms%s",
                     subsystem.name, "ready" if ok else "degraded", took,
                     f"  ({subsystem.error})" if subsystem.error else "")
            if not ok and subsystem.required:
                blocked.append(subsystem.name)

        if blocked:
            self._last_error = f"cannot start without: {', '.join(blocked)}"
            self.transition(DiabloState.ERROR, self._last_error)
            return False

        self.transition(DiabloState.IDLE, "all subsystems up or degraded")
        self.emit("health_update", **self.health())
        return True

    def stop(self) -> None:
        """Tear down in reverse order. Every stop is allowed to fail quietly."""
        self.transition(DiabloState.SHUTDOWN, "stop()")
        for subsystem in reversed(self.subsystems):
            try:
                subsystem.stop()
                subsystem.ready = False
            except BaseException as e:
                log.warning("[Manager] %s did not stop cleanly — %s", subsystem.name, e)
        self.transition(DiabloState.COLD)

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def recover(self) -> bool:
        """
        Try to get out of ERROR without a full restart.

        The three things that break in practice — Ollama stopped, the mic
        unplugged, Voicebox quit — all come back on their own, so this re-runs
        the health checks rather than rebuilding anything.
        """
        if self.state is not DiabloState.ERROR:
            return True
        log.info("[Manager] attempting recovery")
        for subsystem in self.subsystems:
            try:
                subsystem.start()
            except BaseException:
                pass
        if all(s.ready for s in self.subsystems if s.required):
            self.transition(DiabloState.IDLE, "recovered")
            return True
        return False

    # ── the pipeline ─────────────────────────────────────────────────────────

    def handle_utterance(self, text: str,
                         ask: Optional[Callable[[str], str]] = None,
                         confirm: Optional[Callable[[str], bool]] = None) -> str:
        """
        One spoken sentence, from transcript to spoken answer.

        Delegates the actual routing to delegation_tool, which already owns the
        clarify loop and the inline-versus-queued decision. What this adds is
        the state machine around it, so anything watching knows which phase
        Diablo is in, and a guarantee of returning to IDLE however it ends.
        """
        if not text or not text.strip():
            return ""

        self.emit("transcript_ready", text=text, duration=None)
        self.transition(DiabloState.PROCESSING, f"utterance: {text[:40]!r}")
        began = time.time()

        try:
            from skills import delegation_tool

            spoken: List[str] = []

            def speak(message: str, **kwargs: Any) -> None:
                spoken.append(message)
                route = self.voice.say(message)
                self.emit("response_ready", text=message, route=route)
                self.transition(DiabloState.PROCESSING, "spoke, still working")

            delegation_tool.handle(text, speak, ask=ask, confirm=confirm)
            return spoken[-1] if spoken else ""

        except BaseException as e:
            self._last_error = f"{type(e).__name__}: {e}"
            log.exception("[Manager] utterance failed")
            self.emit("error", subsystem="pipeline", message=self._last_error)
            try:
                self.voice.say("Something went wrong there, Sir.")
            except BaseException:
                pass
            return ""
        finally:
            elapsed = (time.time() - began) * 1000
            log.info("[Manager] utterance handled in %.0f ms", elapsed)
            # Whatever happened, Diablo goes back to listening. Getting stuck
            # in PROCESSING is indistinguishable from being switched off.
            self.transition(DiabloState.IDLE, "utterance complete")

    # ── permissions ──────────────────────────────────────────────────────────

    def check_permissions(self) -> Dict[str, bool]:
        """
        What macOS is currently letting Diablo do.

        Each is checked by trying the thing, because TCC has no honest API for
        "may I?" — and a denial is not an error, it is silence: a denied
        microphone returns endless zeros rather than raising.
        """
        result = {"mic": False, "screen": False, "accessibility": False}

        try:
            from skills import mic
            chosen = mic.load_choice()
            result["mic"] = bool(chosen) and not mic.who_holds_the_input()
            if chosen and not result["mic"]:
                # Something else has the device; that is not a permission problem.
                result["mic"] = True
        except BaseException as e:
            log.debug("[Manager] mic permission check failed: %s", e)

        try:
            import Quartz
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
            # Without Screen Recording, titles come back empty for other apps.
            result["screen"] = any(w.get("kCGWindowName") for w in windows)
        except BaseException as e:
            log.debug("[Manager] screen permission check failed: %s", e)

        try:
            from ApplicationServices import AXIsProcessTrusted
            result["accessibility"] = bool(AXIsProcessTrusted())
        except BaseException as e:
            log.debug("[Manager] accessibility check failed: %s", e)

        return result

    # ── introspection ────────────────────────────────────────────────────────

    @property
    def config(self) -> Dict[str, Any]:
        """Every DIABLO_* knob as it is actually set right now."""
        snapshot = {k: v for k, v in os.environ.items() if k.startswith("DIABLO_")}
        for key, value in _ENV_FILE.items():
            snapshot.setdefault(key, value)
        snapshot["OLLAMA_HOST"] = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        snapshot["_env_file"] = ENV_PATH if _ENV_FILE else "(none found)"
        return dict(sorted(snapshot.items()))

    @property
    def uptime(self) -> Optional[float]:
        return (time.time() - self._started_at) if self._started_at else None

    def health(self) -> Dict[str, Any]:
        """One snapshot of everything, safe to call from any thread."""
        with self._lock:
            history = [(ts, a.value, b.value) for ts, a, b in self._state_history[-8:]]
            state = self._state.value
            last_error = self._last_error

        return {
            "state": state,
            "uptime": round(self.uptime, 1) if self.uptime else None,
            "degraded": [s.name for s in self.subsystems if not s.ready],
            "last_error": last_error,
            "subsystems": {s.name: s.health() for s in self.subsystems},
            "permissions": self.check_permissions(),
            "recent_transitions": history,
        }


# ─── Singleton ───────────────────────────────────────────────────────────────

_instance: Optional[CompanionManager] = None
_instance_lock = threading.Lock()


def create_manager() -> CompanionManager:
    """
    The one manager.

    A singleton because the things it owns are singletons: there is one
    microphone, one wake listener and one conversation. Two managers would
    fight over the audio device.
    """
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = CompanionManager()
        return _instance


# ─── Diagnostics ─────────────────────────────────────────────────────────────

def _report() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    manager = create_manager()

    seen: List[str] = []
    manager.on("*", lambda payload: seen.append(payload["event"]))

    print("\n  D.I.A.B.L.O. Companion Manager\n")
    started = manager.start()

    health = manager.health()
    print(f"\n  state     {health['state']}")
    print(f"  uptime    {health['uptime']}s")
    if health["degraded"]:
        print(f"  degraded  {', '.join(health['degraded'])}")
    if health["last_error"]:
        print(f"  error     {health['last_error']}")

    print("\n  Subsystems")
    for name, detail in health["subsystems"].items():
        mark = "ok " if detail.get("ready") else "-- "
        extra = {k: v for k, v in detail.items() if k not in ("ready", "error")}
        note = f"  {detail['error']}" if detail.get("error") else ""
        print(f"    {mark} {name:13}{note}")
        for key, value in list(extra.items())[:5]:
            print(f"           {key:20} {value}")

    print("\n  Permissions")
    for name, granted in health["permissions"].items():
        print(f"    {'granted' if granted else 'DENIED ':8} {name}")

    print("\n  Invalid transitions are refused, not raised")
    before = manager.state
    ok = manager.transition(DiabloState.RESPONDING)   # IDLE -> RESPONDING is legal
    bad = manager.transition(DiabloState.COLD)        # RESPONDING -> COLD is not
    print(f"    legal move accepted    {ok}")
    print(f"    illegal move refused   {not bad}   (still {manager.state.value})")
    manager.transition(DiabloState.IDLE)

    print(f"\n  Events seen by the wildcard listener: {len(seen)}")
    print(f"    {', '.join(sorted(set(seen)))}")

    manager.stop()
    print(f"\n  after stop: {manager.state.value}\n")
    return 0 if started else 1


if __name__ == "__main__":
    sys.exit(_report())
