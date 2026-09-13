#!/bin/bash
# Installs Diablo — the offline agent core, conversation layer and screen context.
#   1. checks Ollama and the model
#   2. installs Apple's on-device OCR bindings
#   3. runs the offline test suite
#   4. copies the six modules into the skills directory
#   5. patches voice_bridge.py (idempotent, timestamped backup)
# Safe to re-run. Undo with: python3 patch_voice_bridge.py --restore

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="${DIABLO_HOME:-$HOME/.diablo}"
OLD_ROOT="$HOME/.gemini/antigravity/scratch/JARVIS/openclaw"
VENV_PY="$BRIDGE_DIR/venv/bin/python3"
MODEL="${DIABLO_MODEL:-${DIABLO_MODEL:-qwen3:14b}}"
MODULES=(local_brain.py agent_tools.py screen_context.py conversation.py
         wake_word.py voice.py mcp_client.py llm_tool.py agent_core.py delegation_tool.py)

echo "▸ Diablo — offline agent, conversation, screen context, voice"

echo
echo "[0/6] Rebranding, if it has not been done"
if [ -d "$OLD_ROOT" ] && [ ! -d "$BRIDGE_DIR" ]; then
    echo "  Assistant is still in the Jarvis tree. Moving it."
    python3 "$HERE/rebrand.py" --go
elif [ -d "$BRIDGE_DIR" ]; then
    echo "  ✓ already at $BRIDGE_DIR"
else
    echo "  ✗ Cannot find the assistant at either location."
    exit 1
fi

echo
echo "[1/6] Checking the local brain"
if ! curl -s --max-time 3 http://localhost:11434/api/tags >/dev/null; then
    echo "  ✗ Ollama is not responding on port 11434."
    echo "    Start it (open -a Ollama, or 'ollama serve') and run this again."
    exit 1
fi
if ! ollama list | grep "^${MODEL%%:*}" > /dev/null; then
    echo "  ✗ $MODEL is not installed. Run: ollama pull $MODEL"
    exit 1
fi
echo "  ✓ Ollama up, $MODEL present"

echo
echo "[2/6] On-device OCR (Apple Vision framework)"
if "$VENV_PY" -c "import Vision" 2>/dev/null; then
    echo "  ✓ already installed"
else
    "$BRIDGE_DIR/venv/bin/pip" install -q pyobjc-framework-Vision && echo "  ✓ installed"
fi

echo
echo "[3/6] Running the offline test suites"
python3 "$HERE/test_diablo.py" | tail -1
python3 "$HERE/test_mcp.py" | tail -1

echo
echo "[4/6] Copying modules into skills/"
if [ ! -d "$BRIDGE_DIR/skills" ]; then
    echo "  ✗ No skills directory at $BRIDGE_DIR/skills"
    exit 1
fi
for module in "${MODULES[@]}"; do
    cp "$HERE/$module" "$BRIDGE_DIR/skills/$module"
    echo "  ✓ $module"
done

echo
echo "[5/6] Patching voice_bridge.py"
python3 "$HERE/patch_voice_bridge.py"

echo
echo "[6/6] Caching the spoken lines"
if grep -q "^ELEVENLABS_API_KEY=.\+" "$BRIDGE_DIR/.env" 2>/dev/null; then
    set -a; . "$BRIDGE_DIR/.env"; set +a
    "$VENV_PY" "$BRIDGE_DIR/skills/voice.py" prewarm | tail -3
else
    echo "  ⚠ No ELEVENLABS_API_KEY in $BRIDGE_DIR/.env"
    echo "    Diablo will speak with the macOS voice until you add one."
    echo "    Add it, then run:  diablo voice"
fi
echo
echo "  Start:    diablo"
echo "  Watch:    diablo logs"
echo
echo "  Say \"Hey Diablo\" to wake it. The phrase is a config value"
echo "  (DIABLO_WAKE_PHRASE) — no model to retrain if you change your mind."
echo
echo "  Try:"
echo "    \"Hey Diablo, take a look at my screen and tell me what I'm reading\""
echo "    \"Hey Diablo, what's in my chrome tab?\""
echo "    \"Hey Diablo, open the file\"      (it should ask you which)"
echo "    \"Hey Diablo, what are you working on?\""
echo
echo "  MCP: paste server configs into $BRIDGE_DIR/mcp.json"
echo "       (same shape as Claude Desktop). Start with:"
echo "       $VENV_PY $BRIDGE_DIR/skills/mcp_client.py init"
echo
echo "  Full live checks (about three minutes):"
echo "    python3 \"$HERE/live_check.py\""
echo "    $VENV_PY \"$HERE/live_screen_check.py\""
echo "    $VENV_PY \"$HERE/wake_word.py\"          (wake word, synthesised speech)"
