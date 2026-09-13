#!/usr/bin/env python3
"""
Tests the MCP client against a real MCP server.

The server is defined in this same file and launched as a subprocess, so the
test is a genuine end-to-end exercise of the wire protocol — spawn, initialize
handshake, initialized notification, tools/list with pagination, tools/call,
error results, shutdown — with no network and no npx download.

    python3 test_mcp.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time

# ─── The server half ─────────────────────────────────────────────────────────
# Run as `python3 test_mcp.py --serve`, it is a minimal but honest MCP server.

TOOLS = [
    {
        "name": "add",
        "description": "Add two numbers together.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "shout",
        "description": "Return the given text in capitals.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "what to shout"}},
            "required": ["text"],
        },
    },
    {
        "name": "explode",
        "description": "Always fails, for testing error handling.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        # Deliberately awkward: a nested object, an enum, a union type and a
        # $ref-ish shape, to prove the schema flattener copes.
        "name": "awkward",
        "description": "A tool with a schema a small model cannot follow raw.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["fast", "slow", "careful"]},
                "options": {"type": "object", "properties": {"deep": {"type": "boolean"}}},
                "count": {"type": ["integer", "null"], "description": "how many"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "weird": {"$ref": "#/definitions/Thing"},
            },
            "required": ["mode", "missing_on_purpose"],
        },
    },
]


def serve():
    def send(payload):
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "test-server", "version": "1.0.0"},
            }})
        elif method == "notifications/initialized":
            # Emit an unsolicited notification too, to prove the reader thread
            # does not confuse it with a response.
            send({"jsonrpc": "2.0", "method": "notifications/message",
                  "params": {"level": "info", "data": "ready"}})
        elif method == "tools/list":
            cursor = (message.get("params") or {}).get("cursor")
            if cursor is None:
                send({"jsonrpc": "2.0", "id": request_id,
                      "result": {"tools": TOOLS[:2], "nextCursor": "page2"}})
            else:
                send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS[2:]}})
        elif method == "tools/call":
            params = message.get("params") or {}
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "add":
                text = str(args.get("a", 0) + args.get("b", 0))
                send({"jsonrpc": "2.0", "id": request_id,
                      "result": {"content": [{"type": "text", "text": text}]}})
            elif name == "shout":
                send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "text", "text": str(args.get("text", "")).upper()}]}})
            elif name == "explode":
                send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "it exploded"}]}})
            elif name == "awkward":
                send({"jsonrpc": "2.0", "id": request_id, "result": {
                    "content": [{"type": "image", "mimeType": "image/png", "data": "x"}]}})
            else:
                send({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32602, "message": f"no tool {name}"}})
        elif request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id,
                  "error": {"code": -32601, "message": "method not found"}})


if "--serve" in sys.argv:
    serve()
    raise SystemExit(0)


# ─── The test half ───────────────────────────────────────────────────────────

failures = []


def check(label, got, want):
    if got == want:
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label}\n      got:  {got!r}\n      want: {want!r}")
        failures.append(label)


def check_true(label, got):
    check(label, bool(got), True)


HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX = tempfile.mkdtemp(prefix="diablo-mcp-")
CONFIG = os.path.join(SANDBOX, "mcp.json")

with open(CONFIG, "w") as f:
    json.dump({"mcpServers": {
        "test": {"command": sys.executable,
                 "args": [os.path.join(HERE, "test_mcp.py"), "--serve"]},
        "disabled": {"command": "true", "enabled": False},
        "cloudy": {"command": "true", "network": True},
        "missing": {"command": "definitely-not-a-real-binary-xyz"},
    }}, f)

os.environ["DIABLO_MCP_CONFIG"] = CONFIG
os.environ["DIABLO_HOME"] = SANDBOX
# DIABLO_HOME is pointed at the sandbox above, so the package has to be found
# from the real install rather than from that variable.
sys.path.insert(0, os.path.expanduser("~/.diablo"))
from skills import mcp_client  # noqa: E402

print("\nConfig loading")
servers = mcp_client.registry.load()
check("loads the enabled servers", sorted(servers), ["cloudy", "missing", "test"])
check_true("skips a server marked enabled: false", "disabled" not in servers)

print("\nOffline mode excludes network servers")
os.environ["DIABLO_OFFLINE"] = "1"
offline_registry = mcp_client.MCPRegistry(CONFIG)
check("network server is dropped when offline",
      "cloudy" in offline_registry.load(), False)
check_true("local server still loads", "test" in offline_registry.load())
os.environ["DIABLO_OFFLINE"] = "0"

print("\nHandshake and tool discovery")
started = time.time()
mcp_client.registry.start_all(background=False)
elapsed = time.time() - started
server = servers["test"]
check("server reaches ready", server.state, "ready")
check_true(f"handshake completes quickly ({elapsed:.1f}s)", elapsed < 20)
check("paginated tools/list is followed to the end", len(server.tools), 4)
check("tool names are namespaced",
      sorted(t["qualified"] for t in mcp_client.registry.ready_tools()),
      ["mcp__test__add", "mcp__test__awkward", "mcp__test__explode", "mcp__test__shout"])

print("\nA broken server is isolated, not fatal")
check("missing binary is marked failed", servers["missing"].state, "failed")
check_true("with a readable reason", "PATH" in (servers["missing"].error or ""))
check_true("and the good server is untouched", servers["test"].state == "ready")

print("\nCalling tools")
check("numbers come back", mcp_client.call("mcp__test__add", {"a": 2, "b": 40}), "42")
check("text comes back", mcp_client.call("mcp__test__shout", {"text": "hello"}), "HELLO")
check_true("a tool error is reported, not raised",
           mcp_client.call("mcp__test__explode", {}).startswith("Error from the tool"))
check_true("non-text content is described rather than dropped",
           "image" in mcp_client.call("mcp__test__awkward", {"mode": "fast"}))
check_true("unknown tool is an error string",
           mcp_client.call("mcp__test__nope", {}).startswith("Error"))
check_true("unknown server is an error string",
           mcp_client.call("mcp__ghost__thing", {}).startswith("Error"))
check_true("a non-MCP name is rejected",
           mcp_client.call("read_file", {}).startswith("Error"))

print("\nSchemas are flattened for a 14B model")
schemas = {s["function"]["name"]: s["function"] for s in mcp_client.schemas()}
check("every tool has a schema", len(schemas), 4)
awkward = schemas["mcp__test__awkward"]["parameters"]
check("union type collapses to one type", awkward["properties"]["count"]["type"], "integer")
check("nested object becomes a string", awkward["properties"]["options"]["type"], "string")
check_true("with instructions for how to pass it",
           "JSON object" in awkward["properties"]["options"]["description"])
check("array items are typed", awkward["properties"]["tags"]["items"]["type"], "string")
check_true("enum values are put in the description",
           "fast, slow, careful" in awkward["properties"]["mode"]["description"])
check("$ref is downgraded rather than passed through",
      awkward["properties"]["weird"]["type"], "string")
check("required names that do not exist are dropped",
      awkward["required"], ["mode"])
check_true("descriptions are present",
           len(schemas["mcp__test__add"]["description"]) > 5)

print("\nStatus and shutdown")
status = mcp_client.registry.status()
check_true("status names the ready server", "test: ready, 4 tools" in status)
check_true("status names the failure", "missing: failed" in status)
mcp_client.registry.stop_all()
time.sleep(0.3)
check("server process is gone", server.process, None)

print()
if failures:
    print(f"✗ {len(failures)} failed: {', '.join(failures)}")
    sys.exit(1)
print("✓ all MCP checks passed")
