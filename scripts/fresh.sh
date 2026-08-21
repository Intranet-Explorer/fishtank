#!/usr/bin/env bash
# Wipe live tank state and restore the seed habitat. Does not touch .venv or model weights.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STOP=0
if [[ "${1:-}" == "--stop" ]]; then
  STOP=1
fi

ensure_docker() {
  if command -v docker >/dev/null 2>&1; then
    return 0
  fi
  if [[ -x /Applications/Docker.app/Contents/Resources/bin/docker ]]; then
    export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
  elif [[ -x /Applications/OrbStack.app/Contents/MacOS/xct/docker ]]; then
    export PATH="/Applications/OrbStack.app/Contents/MacOS/xct:$PATH"
  fi
  command -v docker >/dev/null 2>&1
}

alive() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

tank_running() {
  if alive logs/worldd.pid || alive logs/proxy.pid || alive logs/alpha.pid || alive logs/bravo.pid; then
    return 0
  fi
  if ensure_docker; then
    if docker compose ps --status running -q 2>/dev/null | grep -q .; then
      return 0
    fi
  fi
  return 1
}

if [[ "$STOP" -eq 1 ]]; then
  if ensure_docker; then
    docker compose down || true
  fi
  for f in logs/worldd.pid logs/proxy.pid logs/alpha.pid logs/bravo.pid; do
    if alive "$f"; then
      kill "$(cat "$f")" 2>/dev/null || true
    fi
    rm -f "$f"
  done
  echo "stopped fish / worldd / fetch-proxy (mlx left running)"
elif tank_running; then
  echo "tank is running. stop it first, or pass --stop:"
  echo "  ./scripts/fresh.sh --stop"
  echo "  # or: ./scripts/mac-down.sh && ./scripts/fresh.sh"
  exit 1
fi

if [[ ! -d seed/habitat ]]; then
  echo "missing seed/habitat"
  exit 1
fi

rm -rf habitat
mkdir -p habitat logs webcache private/alpha private/bravo

for agent in alpha bravo; do
  find "private/$agent" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -exec rm -rf {} +
done

rm -f observatory.sqlite observatory.sqlite-wal observatory.sqlite-shm
find logs -type f ! -name '.gitkeep' -delete
find webcache -type f ! -name '.gitkeep' -delete

if [[ -x .venv/bin/python ]]; then
  .venv/bin/python -m world.seed --force
else
  python3 -m world.seed --force
fi

echo "fresh tank: habitat restored from seed/, private diaries emptied, events/logs/webcache cleared"
echo "start with: ./scripts/mac-up.sh"
