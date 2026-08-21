#!/usr/bin/env bash
# Host-only: Metal/MLX cannot see a Linux VM.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
MODEL="${OPENAI_MODEL:-mlx-community/Qwen3.6-35B-A3B-4bit}"
PORT="${MLX_PORT:-11434}"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "create the venv first: python3 -m venv .venv && source .venv/bin/activate && pip install -e . && pip install mlx-lm"
  exit 1
fi
if ! "$PY" -c "import mlx_lm" 2>/dev/null; then
  echo "install mlx-lm in the venv:  .venv/bin/pip install mlx-lm"
  exit 1
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $PORT is already taken (often Ollama)."
  echo "free it, then rerun:"
  echo "  brew services stop ollama"
  echo "  ollama stop --all 2>/dev/null || true"
  exit 1
fi
MAX_TOKENS="${MAX_TOKENS:-4096}"
echo "starting mlx_lm.server  model=$MODEL  http://127.0.0.1:$PORT/v1  max_tokens=$MAX_TOKENS"
echo "first run downloads ~20 GB. leave this terminal open."
exec "$PY" -m mlx_lm.server --model "$MODEL" --host 127.0.0.1 --port "$PORT" --max-tokens "$MAX_TOKENS"
