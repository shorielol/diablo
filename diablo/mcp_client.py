"""
D.I.A.B.L.O. MCP Client
=======================
Lets Diablo use any Model Context Protocol server, so new capabilities arrive as
config rather than as Python.

Written against the wire protocol directly, not the official SDK, because every
published version of `mcp` requires Python >= 3.10 and this venv is 3.9.6 —
carrying whisper, torch, onnxruntime and pyobjc, none of which is worth
rebuilding for a client this small. The protocol is not the hard part:
newline-delimited JSON-RPC 2.0 over the server's stdin and stdout, an
`initialize` handshake, an `initialized` notification, then `tools/list` and
`tools/call`. That is the whole surface this needs.

Config lives in `~/.diablo/mcp.json` and uses the same shape Claude Desktop and
Claude Code use, so a server config found anywhere can be pasted straight in:

    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/Desktop"]
        },
        "home-assistant": {
          "command": "uvx",
          "args": ["mcp-home-assistant"],
          "env": {"HA_TOKEN": "..."},
          "network": true
        }
      }
    }

Two fields are ours rather than the standard's: `enabled` (default true) and
`network` (default false). A server marked `network` is skipped entirely when
DIABLO_OFFLINE=1, which is what keeps "offline" meaning something once arbitrary
third-party servers are in the picture.

Design notes:

* **Servers are started lazily and in the background.** A cold `npx` server can
  take ten seconds to boot. Blocking the wake word on that is unacceptable, so
  connection happens off the critical path and tools appear when they appear.
* **One bad server cannot take the others down.** Every call is wrapped, every
  failure is isolated to its own server, and a server that dies is marked dead
  rather than retried into the ground.
* **Tool names are namespaced** `mcp__<server>__<tool>`, matching the convention
  the rest of the ecosystem uses, so a GitHub server's `create_issue` cannot
  collide with a Linear server's.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"  # widely supported; servers negotiate upward
CLIENT_INFO = {"name": "diablo", "version": "1.0.0"}

HOME = os.getenv("DIABLO_HOME", os.path.expanduser("~/.diablo"))
CONFIG_PATH = os.getenv("DIABLO_MCP_CONFIG", os.path.join(HOME, "mcp.json"))

START_TIMEOUT = float(os.getenv("DIABLO_MCP_START_TIMEOUT", "45"))
CALL_TIMEOUT = float(os.getenv("DIABLO_MCP_CALL_TIMEOUT", "90"))
MAX_RESULT_CHARS = 6000


def offline() -> bool:
    return os.getenv("DIABLO_OFFLINE", "0") not in ("0", "", "false", "False")


class MCPError(Exception):
    pass


# ─── One server ──────────────────────────────────────────────────────────────

class MCPServer:
    """
    A single MCP server, held open as a subprocess.

    Requests and responses share one pipe, so a reader thread demultiplexes by
    id into per-request events. That is simpler and far less fragile than
    reading inline and hoping a notification does not arrive mid-call.
    """

    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self.tools: List[Dict[str, Any]] = []
        self.state = "stopped"          # stopped | starting | ready | failed
        self.error: Optional[str] = None

        self._next_id = 1
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _read_loop(self) -> None:
        stream = self.process.stdout if self.process else None
        if stream is None:
            return
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue  # servers are allowed to be noisy; ignore non-JSON
            request_id = message.get("id")
            if request_id is None:
                continue  # a notification from the server; nothing waits on it
            with self._lock:
                slot = self._pending.get(request_id)
            if slot is not None:
                slot["message"] = message
                slot["event"].set()
        # The pipe closed: the server exited. Release anything still waiting so
        # a dead server surfaces as an error rather than a hang.
        with self._lock:
            for slot in self._pending.values():
                slot["message"] = {"error": {"message": "server exited"}}
                slot["event"].set()
        if self.state == "ready":
            self.state = "failed"
            self.error = "server exited"

    def _send(self, payload: Dict[str, Any]) -> None:
        if not self.process or self.process.poll() is not None:
            raise MCPError(f"{self.name} is not running")
        # The spec forbids embedded newlines; json.dumps escapes them anyway.
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()

    def _request(self, method: str, params: Optional[dict] = None,
                 timeout: float = CALL_TIMEOUT) -> Any:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            slot = {"event": threading.Event(), "message": None}
            self._pending[request_id] = slot

        try:
            self._send({"jsonrpc": "2.0", "id": request_id,
                        "method": method, "params": params or {}})
            if not slot["event"].wait(timeout):
                raise MCPError(f"{self.name}.{method} timed out after {timeout:.0f}s")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

        message = slot["message"] or {}
        if "error" in message:
            detail = message["error"]
            raise MCPError(f"{self.name}.{method}: "
                           f"{detail.get('message', detail) if isinstance(detail, dict) else detail}")
        return message.get("result", {})

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if self.state in ("ready", "starting"):
            return self.state == "ready"

        command = self.config.get("command")
        if not command:
            self.state, self.error = "failed", "no command in config"
            return False
        if not shutil.which(command):
            self.state = "failed"
            self.error = f"'{command}' is not on PATH"
            return False

        self.state = "starting"
        environment = dict(os.environ)
        environment.update(self.config.get("env") or {})
        # Servers that ask questions on stdin will hang forever otherwise.
        environment.setdefault("NO_COLOR", "1")

        try:
            self.process = subprocess.Popen(
                [command] + list(self.config.get("args") or []),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL if not os.getenv("DIABLO_MCP_DEBUG")
                else None,
                text=True, bufsize=1, env=environment,
                cwd=self.config.get("cwd") or None,
            )
        except Exception as e:
            self.state, self.error = "failed", f"{type(e).__name__}: {e}"
            return False

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        try:
            self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            }, timeout=START_TIMEOUT)
            self._notify("notifications/initialized")
            self.tools = self._list_tools()
        except Exception as e:
            self.state, self.error = "failed", str(e)
            self.stop()
            return False

        self.state = "ready"
        self.error = None
        return True

    def _list_tools(self) -> List[Dict[str, Any]]:
        collected, cursor = [], None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params, timeout=START_TIMEOUT)
            collected.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor or len(collected) > 500:
                break
        return collected

    def call(self, tool: str, arguments: Dict[str, Any]) -> str:
        if self.state != "ready" and not self.start():
            return f"Error: {self.name} is unavailable ({self.error})."
        try:
            result = self._request("tools/call",
                                   {"name": tool, "arguments": arguments})
        except MCPError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error calling {self.name}.{tool}: {type(e).__name__}: {e}"
        return _flatten_content(result)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.close()
            except Exception:
                pass
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.process = None
        if self.state != "failed":
            self.state = "stopped"


def _flatten_content(result: Dict[str, Any]) -> str:
    """
    Turn an MCP tool result into the plain text the model reads.

    Content is a list of typed blocks. Text passes through; images and audio are
    described rather than dropped, so the model knows something came back that
    it cannot see rather than assuming the call returned nothing.
    """
    if not isinstance(result, dict):
        return str(result)

    parts = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "image":
            parts.append(f"[an image was returned, {block.get('mimeType', 'unknown type')}]")
        elif kind == "audio":
            parts.append("[audio was returned]")
        elif kind == "resource":
            resource = block.get("resource") or {}
            parts.append(resource.get("text") or f"[resource {resource.get('uri', '')}]")
        else:
            parts.append(json.dumps(block)[:500])

    text = "\n".join(p for p in parts if p).strip()
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"])[:MAX_RESULT_CHARS]
    if not text:
        text = "The tool returned no content."
    if result.get("isError"):
        text = "Error from the tool: " + text
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n... [truncated, {len(text)} chars]"
    return text


# ─── All of them ─────────────────────────────────────────────────────────────

class MCPRegistry:
    """Every configured server, and the flat tool namespace they add up to."""

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        self.servers: Dict[str, MCPServer] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def load(self) -> Dict[str, MCPServer]:
        with self._lock:
            if self._loaded:
                return self.servers
            self._loaded = True

            try:
                with open(self.config_path) as f:
                    config = json.load(f)
            except FileNotFoundError:
                return self.servers
            except ValueError as e:
                print(f"  [MCP] {self.config_path} is not valid JSON: {e}")
                return self.servers

            for name, entry in (config.get("mcpServers") or {}).items():
                if not isinstance(entry, dict) or entry.get("enabled") is False:
                    continue
                if entry.get("network") and offline():
                    print(f"  [MCP] skipping '{name}' — it needs the network "
                          f"and Diablo is in offline mode")
                    continue
                self.servers[name] = MCPServer(name, entry)
            return self.servers

    def start_all(self, background: bool = True) -> None:
        """
        Bring the servers up.

        In the background by default: a cold `npx` server can take ten seconds,
        and the wake word must not wait on it. Tools show up when they show up.
        """
        self.load()
        if not self.servers:
            return

        def boot(server: MCPServer) -> None:
            started = time.time()
            if server.start():
                print(f"  [MCP] {server.name}: {len(server.tools)} tools "
                      f"({time.time() - started:.1f}s)")
            else:
                print(f"  [MCP] {server.name} failed: {server.error}")

        for server in self.servers.values():
            if background:
                threading.Thread(target=boot, args=(server,), daemon=True).start()
            else:
                boot(server)

    def ready_tools(self) -> List[Dict[str, Any]]:
        """Namespaced tool descriptors for every server that came up."""
        out = []
        for name, server in self.servers.items():
            if server.state != "ready":
                continue
            for tool in server.tools:
                out.append({
                    "server": name,
                    "tool": tool.get("name", ""),
                    "qualified": f"mcp__{name}__{tool.get('name', '')}",
                    "description": (tool.get("description")
                                    or tool.get("title")
                                    or f"{tool.get('name', '')} via {name}"),
                    "schema": tool.get("inputSchema") or {"type": "object", "properties": {}},
                })
        return out

    def call(self, qualified: str, arguments: Dict[str, Any]) -> str:
        if not qualified.startswith("mcp__"):
            return f"Error: '{qualified}' is not an MCP tool name."
        remainder = qualified[len("mcp__"):]
        server_name, _, tool_name = remainder.partition("__")
        server = self.servers.get(server_name)
        if server is None:
            known = ", ".join(self.servers) or "none configured"
            return f"Error: no MCP server called '{server_name}'. Configured: {known}."
        return server.call(tool_name, arguments)

    def status(self) -> str:
        self.load()
        if not self.servers:
            return f"No MCP servers configured. Add some to {self.config_path}."
        lines = []
        for name, server in self.servers.items():
            if server.state == "ready":
                lines.append(f"  {name}: ready, {len(server.tools)} tools")
            elif server.state == "failed":
                lines.append(f"  {name}: failed — {server.error}")
            else:
                lines.append(f"  {name}: {server.state}")
        return "\n".join(lines)

    def stop_all(self) -> None:
        for server in self.servers.values():
            server.stop()


registry = MCPRegistry()


# ─── Turning MCP schemas into Ollama tool schemas ────────────────────────────

def _sanitise_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Reduce a JSON Schema to what a 14B model can actually follow.

    Real MCP servers ship schemas with $ref, anyOf, nested objects and enums
    forty values long. qwen3:14b handles a flat object with typed properties and
    starts inventing structure when given anything richer, so composite types
    are flattened to strings with the shape explained in the description.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    properties = {}
    for key, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict):
            properties[key] = {"type": "string"}
            continue
        kind = spec.get("type")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), "string")
        if kind not in ("string", "integer", "number", "boolean", "array", "object"):
            kind = "string"

        entry: Dict[str, Any] = {"type": kind}
        description = spec.get("description") or spec.get("title") or ""
        if spec.get("enum"):
            allowed = [str(v) for v in spec["enum"]][:12]
            description = (description + f" One of: {', '.join(allowed)}.").strip()
        if kind == "array":
            entry["items"] = {"type": "string"}
        if kind == "object":
            entry = {"type": "string"}
            description = (description + " Pass this as a JSON object string.").strip()
        if description:
            entry["description"] = description[:300]
        properties[key] = entry

    required = [r for r in (schema.get("required") or []) if r in properties]
    return {"type": "object", "properties": properties, "required": required}


def schemas() -> List[Dict[str, Any]]:
    """Every ready MCP tool, in the shape Ollama's /api/chat expects."""
    out = []
    for tool in registry.ready_tools():
        out.append({
            "type": "function",
            "function": {
                "name": tool["qualified"],
                "description": tool["description"][:600],
                "parameters": _sanitise_schema(tool["schema"]),
            },
        })
    return out


def names() -> List[str]:
    return [t["qualified"] for t in registry.ready_tools()]


def call(qualified: str, arguments: Dict[str, Any]) -> str:
    return registry.call(qualified, arguments)


EXAMPLE_CONFIG = {
    "mcpServers": {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem",
                     os.path.expanduser("~/Desktop")],
            "enabled": False,
        },
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
            "network": True,
            "enabled": False,
        },
    }
}


def write_example_config(path: str = CONFIG_PATH) -> str:
    if os.path.exists(path):
        return f"{path} already exists; left alone."
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(EXAMPLE_CONFIG, f, indent=2)
    return f"Wrote an example config to {path}. Everything in it is disabled."


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "init":
        print(write_example_config())
        raise SystemExit(0)

    print(f"Config: {CONFIG_PATH}")
    print(f"Offline mode: {offline()}\n")
    registry.load()
    if not registry.servers:
        print("No servers configured. Run: python3 mcp_client.py init")
        raise SystemExit(0)

    registry.start_all(background=False)
    print("\n" + registry.status())
    tools = registry.ready_tools()
    print(f"\n{len(tools)} MCP tools available:")
    for tool in tools[:40]:
        print(f"  {tool['qualified']:<45} {tool['description'][:60]}")
    registry.stop_all()
