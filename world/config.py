from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

KNOWN_AGENTS: list[dict[str, str]] = [
    {"id": "alpha", "origin": "local", "costume": "curious, writes on the board, follows links"},
    {"id": "bravo", "origin": "local", "costume": "tidy, rearranges files, leaves notes about what changed"},
]


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or (REPO_ROOT / ".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


class Settings:
    def __init__(self) -> None:
        load_dotenv()
        self.repo_root = REPO_ROOT
        self.habitat_root = Path(os.environ.get("HABITAT_ROOT", REPO_ROOT / "habitat")).resolve()
        self.private_root = Path(os.environ.get("PRIVATE_ROOT", REPO_ROOT / "private")).resolve()
        self.db_path = Path(os.environ.get("DB_PATH", REPO_ROOT / "observatory.sqlite")).resolve()
        self.viewer_dir = Path(os.environ.get("VIEWER_DIR", REPO_ROOT / "viewer")).resolve()
        self.webcache_dir = Path(os.environ.get("WEBCACHE_DIR", REPO_ROOT / "webcache")).resolve()
        self.world_token = os.environ.get("WORLD_TOKEN", "")
        self.host = os.environ.get("WORLD_HOST", "127.0.0.1")
        self.port = _int("WORLD_PORT", 8080)
        self.scan_interval_sec = float(os.environ.get("SCAN_INTERVAL_SEC", "3"))
        self.max_read_bytes = _int("MAX_READ_BYTES", 32_000_000)
        self.max_write_bytes = _int("MAX_WRITE_BYTES", 32_000_000)
        self.max_grep_hits = _int("MAX_GREP_HITS", 80)

    def require_token(self) -> str:
        if not self.world_token:
            raise RuntimeError("WORLD_TOKEN is empty. Copy .env.example to .env.")
        return self.world_token
