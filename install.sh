#!/usr/bin/env bash
# ==============================================================================
# amh Installer & Updater: Installs or updates amh across AI agents with runtimes
# Usage:
#   ./install.sh [--auto | --gpu-only | --cpu-only] [--update] [--prefix /path] [--no-agent]
# ==============================================================================

set -e

# Default settings
MODE="auto"
IS_UPDATE=false
TARGET_DIR=""
CONFIGURE_AGENTS=true
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AMH_SRC="${SCRIPT_DIR}/amh.py"

# Color helpers
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

show_help() {
    cat << EOF
amh (AI-MEMORY-HOOK) Installer & Updater

Usage:
  ./install.sh [OPTIONS]

Runtime Modes:
  --auto        (Default) Auto-detects GPU (NVIDIA/AMD/Apple) vs CPU runtime.
  --gpu-only    Forces GPU acceleration without runtime checking.
  --cpu-only    Pure CPU mode. Does not check GPU runtimes, uses lightweight CPU runtime.

Update Options:
  --update      Explicitly run in update mode (refreshes binary, runtimes & agent configs).
                Note: If amh is already installed, running ./install.sh automatically updates it.

Installation Options:
  --prefix DIR  Install directory for the binary symlink (default: /usr/local/bin or ~/.local/bin).
  --no-agent    Skip automatic agent hook configuration (Claude Code, Gemini, Antigravity).
  -h, --help    Show this help message.

Examples:
  ./install.sh --auto
  ./install.sh --update
  ./install.sh --gpu-only
  ./install.sh --cpu-only
EOF
}

# ---------------------------------------------------------------- Parse Flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        --auto)
            MODE="auto"
            shift
            ;;
        --gpu-only)
            MODE="gpu-only"
            shift
            ;;
        --cpu-only)
            MODE="cpu-only"
            shift
            ;;
        --update)
            IS_UPDATE=true
            shift
            ;;
        --prefix)
            TARGET_DIR="$2"
            shift 2
            ;;
        --no-agent)
            CONFIGURE_AGENTS=false
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

echo -e "${BOLD}====================================================${NC}"
echo -e "${BOLD}         amh (AI-MEMORY-HOOK) Installer             ${NC}"
echo -e "${BOLD}====================================================${NC}"

# Check for amh.py
if [[ ! -f "$AMH_SRC" ]]; then
    log_error "Cannot find amh.py in $SCRIPT_DIR"
    exit 1
fi
chmod +x "$AMH_SRC"

# ---------------------------------------------------------------- Detect Existing Installation
EXISTING_BIN=""
if command -v amh &> /dev/null; then
    EXISTING_BIN="$(command -v amh)"
fi

if [[ -n "$EXISTING_BIN" || "$IS_UPDATE" == true ]]; then
    IS_UPDATE=true
    if [[ -n "$EXISTING_BIN" ]]; then
        CURRENT_TARGET="$(readlink -f "$EXISTING_BIN" 2>/dev/null || echo "$EXISTING_BIN")"
        log_info "Existing amh installation detected at: ${BOLD}${EXISTING_BIN}${NC}"
        log_info "Target source: ${CURRENT_TARGET}"
        log_info "Updating installation to current source: ${AMH_SRC}..."
    else
        log_info "Update mode requested. Refreshing amh installation..."
    fi
else
    log_info "Fresh installation detected."
fi

# ---------------------------------------------------------------- Hardware Selection
SELECTED_RUNTIME="cpu"

if [[ "$MODE" == "gpu-only" ]]; then
    log_info "Mode: --gpu-only (Forcing GPU acceleration without runtime checks)"
    SELECTED_RUNTIME="gpu"
elif [[ "$MODE" == "cpu-only" ]]; then
    log_info "Mode: --cpu-only (Pure CPU mode selected; skipping GPU checks)"
    SELECTED_RUNTIME="cpu"
else
    log_info "Mode: --auto (Detecting hardware runtime environment...)"
    GPU_FOUND=false

    # Check NVIDIA CUDA
    if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1 || echo "NVIDIA GPU")
        log_ok "Detected NVIDIA GPU: ${GPU_NAME}"
        GPU_FOUND=true
    # Check AMD ROCm
    elif command -v rocm-smi &> /dev/null && rocm-smi &> /dev/null; then
        log_ok "Detected AMD ROCm GPU"
        GPU_FOUND=true
    # Check Apple Silicon Metal
    elif [[ "$(uname)" == "Darwin" ]] && sysctl -n machdep.cpu.brand_string 2>/dev/null | grep -q "Apple"; then
        log_ok "Detected Apple Silicon (Metal/MPS)"
        GPU_FOUND=true
    fi

    if [[ "$GPU_FOUND" == true ]]; then
        SELECTED_RUNTIME="gpu"
    else
        log_info "No GPU detected or GPU runtime inactive. Falling back to CPU."
        SELECTED_RUNTIME="cpu"
    fi
fi

# ---------------------------------------------------------------- Runtime Dependencies
log_info "Configuring ${SELECTED_RUNTIME^^} runtime environment..."

if [[ "$SELECTED_RUNTIME" == "gpu" ]]; then
    log_info "Updating GPU acceleration packages (numpy, optional onnxruntime-gpu)..."
    python3 -m pip install --quiet --upgrade pip 2>/dev/null || true
    python3 -m pip install --quiet numpy 2>/dev/null || log_warn "Could not pip install numpy; pure-python fallback will be used."
    
    if python3 -m pip install --quiet onnxruntime-gpu 2>/dev/null; then
        log_ok "GPU acceleration (onnxruntime-gpu) updated successfully."
    else
        log_warn "onnxruntime-gpu not available for this environment; standard GPU acceleration configured."
    fi
else
    log_info "Verifying CPU runtime packages..."
    python3 -m pip install --quiet numpy 2>/dev/null || log_info "Using standard library zero-dependency engine (no pip packages needed)."
fi

# ---------------------------------------------------------------- Binary Symlink Installation / Update
if [[ -z "$TARGET_DIR" ]]; then
    if [[ -n "$EXISTING_BIN" ]]; then
        # Preserve directory of existing binary
        TARGET_DIR="$(dirname "$EXISTING_BIN")"
    elif [[ -w "/usr/local/bin" ]]; then
        TARGET_DIR="/usr/local/bin"
    elif command -v sudo &> /dev/null && sudo -n true 2>/dev/null; then
        TARGET_DIR="/usr/local/bin"
    else
        TARGET_DIR="${HOME}/.local/bin"
        mkdir -p "$TARGET_DIR"
    fi
fi

BIN_DEST="${TARGET_DIR}/amh"
log_info "Updating executable symlink at ${BIN_DEST} -> ${AMH_SRC}..."

if [[ -e "$BIN_DEST" || -L "$BIN_DEST" ]]; then
    if [[ ! -w "$TARGET_DIR" ]]; then
        sudo rm -f "$BIN_DEST"
        sudo ln -sf "$AMH_SRC" "$BIN_DEST"
    else
        rm -f "$BIN_DEST"
        ln -sf "$AMH_SRC" "$BIN_DEST"
    fi
    log_ok "Binary link updated: ${BIN_DEST}"
else
    if [[ "$TARGET_DIR" == "/usr/local/bin" && ! -w "/usr/local/bin" ]]; then
        sudo ln -sf "$AMH_SRC" "$BIN_DEST"
    else
        mkdir -p "$TARGET_DIR"
        ln -sf "$AMH_SRC" "$BIN_DEST"
    fi
    log_ok "Binary link created: ${BIN_DEST}"
fi

# Verify in PATH
if ! command -v amh &> /dev/null; then
    if [[ ":$PATH:" != *":$TARGET_DIR:"* ]]; then
        log_warn "$TARGET_DIR is not in your PATH."
        echo "  Add it to your shell rc (e.g. ~/.bashrc or ~/.zshrc):"
        echo "    export PATH=\"${TARGET_DIR}:\$PATH\""
    fi
fi

# ---------------------------------------------------------------- Agent Configuration Update
if [[ "$CONFIGURE_AGENTS" == true ]]; then
    log_info "Updating amh hook across detected AI agents..."

    # 1. Claude Code (~/.claude/settings.json)
    CLAUDE_SETTINGS="${HOME}/.claude/settings.json"
    if [[ -d "${HOME}/.claude" ]] || command -v claude &> /dev/null; then
        mkdir -p "${HOME}/.claude"
        python3 - << 'PYEOF'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
data = {}
if os.path.exists(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

hooks = data.setdefault("hooks", {})
ups = hooks.setdefault("UserPromptSubmit", [])

# Deduplicate / refresh amh hook
filtered_ups = []
for item in ups:
    if isinstance(item, dict):
        subhooks = [h for h in item.get("hooks", []) if h.get("command") != "amh hook"]
        if subhooks:
            item["hooks"] = subhooks
            filtered_ups.append(item)
    else:
        filtered_ups.append(item)

# Append clean amh hook entry
filtered_ups.append({"hooks": [{"type": "command", "command": "amh hook"}]})
hooks["UserPromptSubmit"] = filtered_ups

with open(p, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
print("  [OK] Claude Code hook refreshed in ~/.claude/settings.json")
PYEOF
    fi

    # 2. Antigravity / Gemini CLI / Codex global instructions (Always refresh)
    AGENT_INSTRUCTION_DIR="${HOME}/.agents"
    mkdir -p "$AGENT_INSTRUCTION_DIR/rules" 2>/dev/null || true
    cat << 'EOF' > "$AGENT_INSTRUCTION_DIR/rules/amh_memory.md" 2>/dev/null || true
# AI-MEMORY-HOOK (amh) Agent Protocol
- At the start of every task run: `amh hook "<the user request>"`
- Follow the returned `<system>`, `<rules>`, and `<file_topics>`.
- Use `amh read path:l0-l1` to read code slices without loading whole files.
- Persist durable decisions: `amh note "short title" --folder decisions --text "..."`
- Persist durable rules: `amh rule <name> --append --text "- always ..."`
EOF
    log_ok "Global agent memory rule updated in ~/.agents/rules/amh_memory.md"
fi

# ---------------------------------------------------------------- Refresh Project Index if Present
if [[ -d ".amh" ]]; then
    log_info "Active project .amh directory detected. Refreshing local project index..."
    "${AMH_SRC}" index 2>/dev/null || true
    log_ok "Project index refreshed."
fi

ACTION_WORD="installed"
if [[ "$IS_UPDATE" == true ]]; then
    ACTION_WORD="updated"
fi

echo -e "\n${BOLD}${GREEN}✔ amh successfully ${ACTION_WORD}!${NC}"
echo -e "  Runtime:   ${BOLD}${SELECTED_RUNTIME^^}${NC} (Mode: ${MODE})"
echo -e "  Binary:    ${BOLD}${BIN_DEST}${NC}"
echo -e "\nVerification:"
echo -e "  amh status\n"
