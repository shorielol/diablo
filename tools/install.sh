#!/bin/bash
# Installs Diablo — the offline agent core, conversation layer and screen context.
#   1. checks Ollama and the model
#   2. installs Apple's on-device OCR bindings
#   3. runs the offline test suite
#   4. copies the six modules into the skills directory
#   5. patches voice_bridge.py (idempotent, timestamped backup)
# Safe to re-run. Undo with: python3 patch_voice_bridge.py --restore
#
#   install.sh --check     resolve the brain model and check Ollama has it,
#                          then stop. Writes nothing, pulls nothing.
#   install.sh --grounder  fetch GoClick-Large into $DIABLO_HOME/models, check
#                          its sha256, and install its runtime (mlx-vlm) into
#                          Diablo's venv. Nothing else.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="${DIABLO_HOME:-$HOME/.diablo}"
OLD_ROOT="$HOME/.gemini/antigravity/scratch/JARVIS/openclaw"
VENV_PY="$BRIDGE_DIR/venv/bin/python3"
CHECK_ONLY=0
GROUNDER_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1
[ "${1:-}" = "--grounder" ] && GROUNDER_ONLY=1

# GoClick-Large, pinned. Revision, hash and size are read from goclick.py so
# there is one place they are written down. Safetensors and configuration
# only: the repository also holds pickle files and model code, and neither is
# fetched. MLX-VLM's own Florence-2 implementation runs the weights.
GOCLICK_PY="$HERE/../diablo/goclick.py"
GOCLICK_REPO="$(sed -n 's/^REPOSITORY = "\(.*\)"$/\1/p' "$GOCLICK_PY")"
GOCLICK_REV="$(sed -n 's/^REVISION = "\(.*\)"$/\1/p' "$GOCLICK_PY")"
GOCLICK_SHA="$(sed -n 's/^WEIGHTS_SHA256 = "\(.*\)"$/\1/p' "$GOCLICK_PY")"
GOCLICK_DIR="${DIABLO_GOCLICK_PATH:-$BRIDGE_DIR/models/goclick-large}"
GOCLICK_FILES=(config.json generation_config.json preprocessor_config.json
               tokenizer.json tokenizer_config.json vocab.json model.safetensors)

install_grounder() {
    echo "[grounder] GoClick-Large ${GOCLICK_REV:0:12} → $GOCLICK_DIR"
    if [ -z "$GOCLICK_REPO" ] || [ -z "$GOCLICK_REV" ] || [ -z "$GOCLICK_SHA" ]; then
        echo "  ✗ Cannot read the pinned revision from diablo/goclick.py."
        return 1
    fi
    mkdir -p "$GOCLICK_DIR"
    for f in "${GOCLICK_FILES[@]}"; do
        if [ ! -s "$GOCLICK_DIR/$f" ]; then
            curl -fL --retry 3 -C - -o "$GOCLICK_DIR/$f.part" \
                "https://huggingface.co/$GOCLICK_REPO/resolve/$GOCLICK_REV/$f"
            mv "$GOCLICK_DIR/$f.part" "$GOCLICK_DIR/$f"
        fi
    done
    local have
    have="$(shasum -a 256 "$GOCLICK_DIR/model.safetensors" | cut -c1-64)"
    if [ "$have" != "$GOCLICK_SHA" ]; then
        echo "  ✗ model.safetensors does not match the pinned sha256. Removed it; run again."
        rm -f "$GOCLICK_DIR/model.safetensors"
        return 1
    fi
    echo "  ✓ weights verified"
    if "$VENV_PY" -c "import mlx_vlm" 2>/dev/null; then
        echo "  ✓ mlx-vlm already installed"
    else
        "$BRIDGE_DIR/venv/bin/pip" install -q "mlx-vlm==0.7.4" && echo "  ✓ mlx-vlm installed"
    fi
}

if [ "$GROUNDER_ONLY" = 1 ]; then
    install_grounder
    exit $?
fi

# The model Diablo will run, resolved the way it resolves it: a real
# environment variable, then .env, then the default named once in
# local_brain.py. This used to fall back to its own copy, qwen3:14b, so the
# installer checked for a model the app was not configured to run.
DEFAULT_MODEL="$(sed -n 's/^DEFAULT_BRAIN_MODEL = "\(.*\)"$/\1/p' "$HERE/../diablo/local_brain.py")"
if [ -z "$DEFAULT_MODEL" ]; then
    echo "  ✗ Cannot read DEFAULT_BRAIN_MODEL from diablo/local_brain.py."
    exit 1
fi
ENV_MODEL="$(grep -E '^DIABLO_MODEL=' "$BRIDGE_DIR/.env" 2>/dev/null | tail -1 \
             | cut -d= -f2- | tr -d "\"' " || true)"
MODEL="${DIABLO_MODEL:-${ENV_MODEL:-$DEFAULT_MODEL}}"
case "$MODEL" in *:*) ;; *) MODEL="$MODEL:latest" ;; esac
MODULES=(local_brain.py agent_tools.py screen_context.py conversation.py
         wake_word.py voice.py mcp_client.py llm_tool.py agent_core.py delegation_tool.py)

echo "▸ Diablo — offline agent, conversation, screen context, voice"

echo
echo "[0/6] Rebranding, if it has not been done"
if [ "$CHECK_ONLY" = 1 ]; then
    echo "  – skipped (--check)"
elif [ -d "$OLD_ROOT" ] && [ ! -d "$BRIDGE_DIR" ]; then
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
# The whole name. Matching only the part before the colon passed qwen3:14b
# whenever any qwen3 model at all was installed.
if ! ollama list | awk 'NR > 1 {print $1}' | grep -Fqx "$MODEL"; then
    if [ "$CHECK_ONLY" = 1 ]; then
        echo "  ✗ $MODEL is not installed. Run: ollama pull $MODEL"
        exit 1
    fi
    echo "  Pulling $MODEL"
    ollama pull "$MODEL"
fi
echo "  ✓ Ollama up, $MODEL present"
if [ "$CHECK_ONLY" = 1 ]; then
    exit 0
fi

echo
echo "[2/6] On-device OCR (Apple Vision framework)"
if "$VENV_PY" -c "import Vision" 2>/dev/null; then
    echo "  ✓ already installed"
else
    "$BRIDGE_DIR/venv/bin/pip" install -q pyobjc-framework-Vision && echo "  ✓ installed"
fi

echo
echo "[2b/6] Web search (ddgs)"
# The maintained continuation of duckduckgo-search, which stopped at 8.1.1.
# Optional: search_tool falls back to scraping DuckDuckGo's HTML endpoint
# without it, which works but gets an "anomaly" page after about six queries
# in a row. With it, ten in a row came back clean.
if "$VENV_PY" -c "import ddgs" 2>/dev/null; then
    echo "  ✓ already installed"
else
    "$BRIDGE_DIR/venv/bin/pip" install -q ddgs && echo "  ✓ installed" \
        || echo "  – not installed; search falls back to scraping"
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
