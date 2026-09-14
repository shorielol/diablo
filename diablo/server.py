"""
D.I.A.B.L.O. — the local API the app talks to.

Runs inside the assistant's own process, so it reads live state rather than a
copy of it: the same CompanionManager the voice loop is using, the same job
store, the same trace. Nothing is duplicated and nothing can drift.

Two deliberate choices:

  Server-sent events, not WebSockets. Everything the UI needs is one-way —
  state changes, wake events, job progress — and SSE is about thirty lines of
  stdlib against a few hundred for RFC 6455 framing. Commands go back as
  ordinary POSTs. A WebSocket would be more fashionable and buy nothing.

  Bound to 127.0.0.1 only. Diablo can read files, run shell commands and see
  the screen. An API for that has no business being reachable from the
  network, and binding to localhost is the difference between a personal
  assistant and an open door.

Every handler returns JSON and none of them raise: an exception here would take
down the thread serving the UI while the voice loop carried on, which looks to
the user like the app froze for no reason.
"""

import json
import mimetypes
import os

from skills import config  # noqa: E402
import queue
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
# ~/.diablo/ui already belongs to the openclaw tree this install was carved
# out of, so the app's files live beside it rather than inside it.
UI_DIR = os.path.join(HOME, "app")
HOST = "127.0.0.1"
PORT = config.env_int("DIABLO_UI_PORT", 17494)

# Every connected browser gets its own queue. Bounded, because a tab that stops
# reading must not grow a queue until the assistant runs out of memory.
_clients: List["queue.Queue"] = []
_clients_lock = threading.Lock()

_manager = None          # set by attach()
_speak = None            # the bridge's speak(), so the UI can make it talk
_handle = None           # the bridge's utterance handler
_panel_toggle = None     # set by the app shell


def attach(manager=None, speak=None, handle=None, panel_toggle=None) -> None:
    """Hand the server the live objects. Called once, by voice_bridge."""
    global _manager, _speak, _handle, _panel_toggle
    if manager is not None:
        _manager = manager
        manager.on("*", _broadcast_event)
    if speak is not None:
        _speak = speak
    if handle is not None:
        _handle = handle
    if panel_toggle is not None:
        _panel_toggle = panel_toggle


def _broadcast_event(payload: Dict[str, Any]) -> None:
    """Fan one manager event out to every connected client."""
    with _clients_lock:
        targets = list(_clients)
    for q in targets:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass          # a stalled tab loses events, not the assistant


def push(event: str, **data: Any) -> None:
    """Send an event that did not come from the manager."""
    _broadcast_event(dict(data, event=event, ts=time.time()))


# ─── State gathering ─────────────────────────────────────────────────────────

def _state() -> Dict[str, Any]:
    if _manager is not None:
        try:
            return _manager.health()
        except Exception as e:
            return {"state": "error", "last_error": str(e), "subsystems": {}}
    return {"state": "cold", "subsystems": {}, "degraded": ["manager not attached"]}


def _jobs() -> List[Dict[str, Any]]:
    try:
        from skills import delegation_tool
        return delegation_tool.store.recent(limit=25)
    except Exception:
        return []


def _trace(limit: int = 12) -> List[List[Dict[str, Any]]]:
    try:
        from skills import trace
        return trace.turns(limit=limit)
    except Exception:
        return []


def _conversation() -> List[Dict[str, str]]:
    try:
        from skills import conversation
        return list(conversation.session.turns)
    except Exception:
        return []


def _settings() -> Dict[str, Any]:
    """The dials the UI is allowed to change, with their ranges."""
    return {
        "humour":    {"value": config.env_int("DIABLO_HUMOUR", 2), "min": 0, "max": 3,
                      "label": "Humour", "help": "Conversation only. Task reports stay flat."},
        "warmth":    {"value": config.env_int("DIABLO_WARMTH", 1), "min": 0, "max": 3,
                      "label": "Warmth", "help": "How much it acts like it cares."},
        "formality": {"value": config.env_int("DIABLO_FORMALITY", 2), "min": 0, "max": 3,
                      "label": "Formality", "help": "How much it stands on ceremony."},
        "address":   {"value": os.getenv("DIABLO_ADDRESS", "Sir"), "type": "text",
                      "label": "Calls you", "help": "Used sparingly, never as an opener."},
        "model":     {"value": os.getenv("DIABLO_MODEL", ""), "type": "text",
                      "label": "Model", "help": "Restart required."},
        "escalate":  {"value": os.getenv("DIABLO_ESCALATE_MODEL", ""), "type": "text",
                      "label": "Escalates to", "help": "Blank disables escalation."},
    }


ENV_KEYS = {
    "humour": "DIABLO_HUMOUR", "warmth": "DIABLO_WARMTH",
    "formality": "DIABLO_FORMALITY", "address": "DIABLO_ADDRESS",
    "model": "DIABLO_MODEL", "escalate": "DIABLO_ESCALATE_MODEL",
}


def _save_settings(changes: Dict[str, Any]) -> Dict[str, Any]:
    """
    Write the dials back to .env and apply what can be applied live.

    The persona dials are read at import into module constants, so changing
    them here updates the environment and the already-loaded persona module.
    Model changes need a restart and the UI says so.
    """
    env_path = os.path.join(HOME, ".env")
    applied, needs_restart = [], []
    try:
        lines = open(env_path).read().splitlines()
    except OSError:
        lines = []

    rejected = []
    for key, value in changes.items():
        env_key = ENV_KEYS.get(key)
        if not env_key:
            continue
        value = str(value)

        # Validate before writing. Without this, {"humour": "loud"} was written
        # to .env and persona fell back to its default forever after, with
        # nothing anywhere saying why.
        if key in ("humour", "warmth", "formality"):
            if not value.isdigit() or not 0 <= int(value) <= 3:
                rejected.append(f"{key} must be 0-3, got {value!r}")
                continue
        elif key == "address":
            if len(value) > 32:
                rejected.append("address is too long")
                continue
        elif key in ("model", "escalate"):
            if any(c in value for c in " \t\n\"'$`"):
                rejected.append(f"{key} has characters a model name cannot have")
                continue
        os.environ[env_key] = value
        for index, line in enumerate(lines):
            if line.startswith(env_key + "="):
                lines[index] = f"{env_key}={value}"
                break
        else:
            lines.append(f"{env_key}={value}")

        if key in ("humour", "warmth", "formality", "address"):
            try:
                from skills import persona
                setattr(persona, key.upper(), int(value) if value.isdigit() else value)
                applied.append(key)
            except Exception:
                needs_restart.append(key)
        else:
            needs_restart.append(key)

    try:
        with open(env_path, "w") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as e:
        return {"ok": False, "error": str(e)}

    return {"ok": not rejected, "applied": applied,
            "needs_restart": needs_restart, "rejected": rejected}


# ─── HTTP ────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Diablo"

    def log_message(self, *args):
        pass          # the UI polls; its access log is pure noise

    # ── helpers ──────────────────────────────────────────────────────────
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        kind = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        # The UI is rebuilt in place during development; never cache it.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    # ── routes ───────────────────────────────────────────────────────────
    def do_GET(self):
        try:
            route = urllib.parse.urlparse(self.path).path
            if route in ("/", "/index.html"):
                return self._file(os.path.join(UI_DIR, "index.html"))
            if route.startswith("/static/"):
                safe = os.path.normpath(route[len("/static/"):]).lstrip("/.")
                return self._file(os.path.join(UI_DIR, safe))
            if route == "/api/state":
                return self._json(_state())
            if route == "/api/jobs":
                return self._json(_jobs())
            if route == "/api/trace":
                return self._json(_trace())
            if route == "/api/conversation":
                return self._json(_conversation())
            if route == "/api/settings":
                return self._json(_settings())
            if route == "/api/everything":
                # One round trip for a cold UI load, rather than five.
                return self._json({
                    "state": _state(), "jobs": _jobs(), "trace": _trace(6),
                    "conversation": _conversation(), "settings": _settings(),
                })
            if route == "/events":
                return self._events()
            return self._json({"error": "no such route"}, 404)
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            try:
                self._json({"error": "internal"}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            route = urllib.parse.urlparse(self.path).path
            data = self._body()

            if route == "/api/say":
                text = (data.get("text") or "").strip()
                if not text:
                    return self._json({"ok": False, "error": "nothing to say"}, 400)
                if _handle is None:
                    return self._json({"ok": False, "error": "not connected"}, 503)
                # Off the request thread: a task can take a minute and the UI
                # must stay responsive while it runs.
                threading.Thread(target=_handle, args=(text,), daemon=True).start()
                return self._json({"ok": True})

            if route == "/api/speak":
                if _speak is None:
                    return self._json({"ok": False, "error": "no voice"}, 503)
                threading.Thread(target=_speak, args=(data.get("text", ""),),
                                 daemon=True).start()
                return self._json({"ok": True})

            if route == "/api/settings":
                return self._json(_save_settings(data or {}))

            if route == "/api/control":
                return self._json(_control(data.get("action", "")))

            return self._json({"error": "no such route"}, 404)
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            try:
                self._json({"error": "internal"}, 500)
            except Exception:
                pass

    # ── server-sent events ───────────────────────────────────────────────
    def _events(self) -> None:
        """
        Hold the connection open and write every manager event to it.

        The heartbeat is not decoration. Without traffic the OS will eventually
        drop an idle socket and the UI goes quiet with no error anywhere, which
        is the hardest kind of bug to notice.
        """
        client: "queue.Queue" = queue.Queue(maxsize=200)
        with _clients_lock:
            _clients.append(client)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = client.get(timeout=15)
                    frame = f"data: {json.dumps(payload)}\n\n".encode()
                except queue.Empty:
                    frame = b": ping\n\n"
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _clients_lock:
                if client in _clients:
                    _clients.remove(client)


def _control(action: str) -> Dict[str, Any]:
    try:
        if action == "interrupt":
            from skills import voice
            voice.interrupt()
            return {"ok": True}
        if action == "reset":
            from skills import conversation
            conversation.session.reset()
            push("conversation_reset")
            return {"ok": True}
        if action == "listen" and _manager is not None:
            from companion_manager import DiabloState
            _manager.transition(DiabloState.LISTENING, "asked by the app")
            return {"ok": True}
        if action == "panel":
            # The shell registers a callback here so the window can be opened
            # without a click, which is the only way to test it unattended.
            if _panel_toggle is not None:
                _panel_toggle()
                return {"ok": True}
            return {"ok": False, "error": "no shell attached"}
        if action == "quit":
            push("quitting")
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return {"ok": True}
        return {"ok": False, "error": f"unknown action {action!r}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ─── Lifecycle ───────────────────────────────────────────────────────────────

_server: Optional[ThreadingHTTPServer] = None


def start(port: int = PORT) -> Optional[str]:
    """Serve in a background thread. Returns the URL, or None if it could not."""
    global _server
    if _server is not None:
        return f"http://{HOST}:{port}"
    try:
        ThreadingHTTPServer.allow_reuse_address = True
        _server = ThreadingHTTPServer((HOST, port), Handler)
    except OSError as e:
        print(f"  [UI] Port {port} unavailable ({e}). The app cannot connect.")
        return None
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    print(f"  [UI] http://{HOST}:{port}")
    return f"http://{HOST}:{port}"


def stop() -> None:
    global _server
    if _server is not None:
        _server.shutdown()
        _server = None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, HOME)
    from companion_manager import create_manager

    manager = create_manager()
    manager.start()
    attach(manager=manager)
    url = start()
    print(f"\n  Serving {UI_DIR}")
    print(f"  {url}\n  Ctrl-C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop()
