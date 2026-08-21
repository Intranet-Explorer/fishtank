#!/usr/bin/env bash
# Start worldd + fetch proxy on the Mac, then two fish containers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "copy .env.example to .env and set WORLD_TOKEN"
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ -z "${WORLD_TOKEN:-}" ]]; then
  echo "WORLD_TOKEN is empty"
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "create a venv first: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
  exit 1
fi

mkdir -p logs webcache private/alpha private/bravo habitat

if [[ ! -f habitat/BOARD.md ]]; then
  echo "seeding habitat"
  .venv/bin/python -m world.seed
fi

wait_http() {
  local url="$1"
  local name="$2"
  local n=0
  while (( n < 40 )); do
    if curl -sf "$url" >/dev/null; then
      echo "$name is up"
      return 0
    fi
    sleep 0.25
    n=$((n + 1))
  done
  echo "$name did not start ($url)"
  return 1
}

if curl -sf "http://${WORLD_HOST:-127.0.0.1}:${WORLD_PORT:-8080}/health" >/dev/null; then
  echo "worldd already running"
else
  echo "starting worldd"
  nohup .venv/bin/python -m world.server > logs/worldd.log 2>&1 &
  echo $! > logs/worldd.pid
  wait_http "http://${WORLD_HOST:-127.0.0.1}:${WORLD_PORT:-8080}/health" worldd
fi

if curl -sf "http://${PROXY_HOST:-127.0.0.1}:${PROXY_PORT:-8787}/health" >/dev/null; then
  echo "fetch-proxy already running"
else
  echo "starting fetch-proxy"
  nohup .venv/bin/python -m proxy.fetch_proxy > logs/proxy.log 2>&1 &
  echo $! > logs/proxy.pid
  wait_http "http://${PROXY_HOST:-127.0.0.1}:${PROXY_PORT:-8787}/health" fetch-proxy
fi

MLX_URL="http://127.0.0.1:11434/v1/models"
WANT="${OPENAI_MODEL:-mlx-community/Qwen3.6-35B-A3B-4bit}"
if ids="$(curl -sf "$MLX_URL")"; then
  if echo "$ids" | grep -q "$WANT"; then
    echo "model server has $WANT"
    export DUMMY="${DUMMY:-0}"
  else
    echo "something is on :11434, but it is not $WANT"
    echo "that is usually Ollama. stop it and start MLX:"
    echo "  brew services stop ollama"
    echo "  ./scripts/run-mlx.sh"
    echo "dummy fish until then."
    export DUMMY=1
  fi
else
  echo "no model server on :11434 (./scripts/run-mlx.sh). dummy fish will swim."
  export DUMMY=1
fi

alive() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

start_host_fish() {
  echo "starting fish on the Mac host (DUMMY=$DUMMY)"
  if ! alive logs/alpha.pid; then
    nohup env AGENT_ID=alpha TEMPERATURE=0.7 DUMMY="$DUMMY" \
      .venv/bin/python -m agent.harness --id alpha > logs/alpha.log 2>&1 &
    echo $! > logs/alpha.pid
    echo "alpha pid $(cat logs/alpha.pid)"
  else
    echo "alpha already running"
  fi
  if ! alive logs/bravo.pid; then
    nohup env AGENT_ID=bravo TEMPERATURE=1.0 DUMMY="$DUMMY" \
      .venv/bin/python -m agent.harness --id bravo > logs/bravo.log 2>&1 &
    echo $! > logs/bravo.pid
    echo "bravo pid $(cat logs/bravo.pid)"
  else
    echo "bravo already running"
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  if [[ -x /Applications/Docker.app/Contents/Resources/bin/docker ]]; then
    export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
  elif [[ -x /Applications/OrbStack.app/Contents/MacOS/xct/docker ]]; then
    export PATH="/Applications/OrbStack.app/Contents/MacOS/xct:$PATH"
  fi
fi

if command -v docker >/dev/null 2>&1; then
  echo "compose up (DUMMY=$DUMMY)"
  docker compose up --build -d
else
  echo "docker not found — install Docker Desktop or OrbStack; swimming on the host for now"
  start_host_fish
fi

echo
echo "glass:  http://127.0.0.1:${WORLD_PORT:-8080}/?token=${WORLD_TOKEN}"
echo "worldd: http://127.0.0.1:${WORLD_PORT:-8080}/health"
echo "proxy:  http://127.0.0.1:${PROXY_PORT:-8787}/health"
echo
echo "stop:    ./scripts/mac-down.sh"
echo "reset:   ./scripts/fresh.sh --stop"
echo "stop mlx: pkill -f mlx_lm.server"
