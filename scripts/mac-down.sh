#!/usr/bin/env bash
# Stop fish, worldd, and the fetch proxy. Does not stop mlx_lm.server.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v docker >/dev/null 2>&1; then
  :
elif [[ -x /Applications/Docker.app/Contents/Resources/bin/docker ]]; then
  export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
elif [[ -x /Applications/OrbStack.app/Contents/MacOS/xct/docker ]]; then
  export PATH="/Applications/OrbStack.app/Contents/MacOS/xct:$PATH"
fi

if command -v docker >/dev/null 2>&1; then
  docker compose down || true
fi

for f in logs/worldd.pid logs/proxy.pid logs/alpha.pid logs/bravo.pid; do
  if [[ -f "$f" ]]; then
    pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      echo "stopped $f ($pid)"
    fi
    rm -f "$f"
  fi
done

echo "tank host processes stopped"
echo "mlx (if you started it) is still up. stop it with: pkill -f mlx_lm.server"
