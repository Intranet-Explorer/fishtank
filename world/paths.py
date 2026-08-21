from __future__ import annotations

import re
from pathlib import Path

AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")


class PathJailError(ValueError):
    """Bad path (kept for the /private alias, which needs an agent id)."""


def normalize_logical(path: str) -> str:
    raw = (path or "").strip() or "/workspace"
    raw = raw.replace("\\", "/")
    if raw == "~" or raw.startswith("~/"):
        home = str(Path.home())
        raw = home if raw == "~" else home + "/" + raw[2:]
    elif not raw.startswith("/"):
        raw = "/workspace/" + raw.lstrip("./")
    parts: list[str] = []
    for piece in raw.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if parts:
                parts.pop()
            continue
        parts.append(piece)
    return "/" + "/".join(parts) if parts else "/"


def is_workspace(logical: str) -> bool:
    return logical == "/workspace" or logical.startswith("/workspace/")


def is_private(logical: str) -> bool:
    return logical == "/private" or logical.startswith("/private/")


def logical_workspace(host_path: Path, habitat: Path) -> str | None:
    try:
        rel = host_path.resolve().relative_to(habitat.resolve())
    except ValueError:
        return None
    rel_s = rel.as_posix()
    if rel_s == ".":
        return "/workspace"
    return "/workspace/" + rel_s


def logical_from_host(
    host_path: Path,
    habitat: Path,
    *,
    private_root: Path | None = None,
    agent_id: str | None = None,
) -> str:
    mapped = logical_workspace(host_path, habitat)
    if mapped:
        return mapped
    if private_root and agent_id:
        try:
            priv = (private_root / agent_id).resolve()
            rel = host_path.resolve().relative_to(priv)
            rel_s = rel.as_posix()
            return "/private" if rel_s == "." else "/private/" + rel_s
        except ValueError:
            pass
    try:
        return str(host_path.resolve())
    except OSError:
        return str(host_path)


def _as_host(dest: Path) -> Path:
    if dest.exists():
        return dest.resolve()
    parent = dest.parent
    if parent.exists():
        return parent.resolve() / dest.name
    return dest


def resolve_host_path(
    logical: str,
    *,
    agent_id: str | None,
    habitat: Path,
    private_root: Path,
    must_exist: bool = False,
) -> tuple[Path, str]:
    """Map /workspace and /private onto host folders; other absolute paths are real host paths."""
    logical = normalize_logical(logical)

    if is_workspace(logical):
        rel = logical[len("/workspace") :].lstrip("/")
        root = habitat.resolve()
        dest = (root / rel).resolve() if rel else root
        if dest == root or root in dest.parents:
            if must_exist and not dest.exists():
                raise FileNotFoundError(logical)
            return dest, logical
        logical = str(dest)

    if is_private(logical):
        if not agent_id or not AGENT_ID_RE.match(agent_id):
            raise PathJailError("private path requires a valid X-Agent-Id")
        rel = logical[len("/private") :].lstrip("/")
        root = (private_root / agent_id).resolve()
        root.mkdir(parents=True, exist_ok=True)
        dest = (root / rel).resolve() if rel else root
        if dest == root or root in dest.parents:
            if must_exist and not dest.exists():
                raise FileNotFoundError(logical)
            return dest, logical
        logical = str(dest)

    dest = _as_host(Path(logical))
    if must_exist and not dest.exists():
        raise FileNotFoundError(logical)
    return dest, logical


def place_from_path(path: str) -> str:
    """Room in the tank. For the glass now, sprites later."""
    p = (path or "").replace("\\", "/").rstrip("/")
    if p.startswith("http://") or p.startswith("https://"):
        return "outbox"
    if p.endswith("BOARD.md") or p == "/workspace/BOARD.md":
        return "corkboard"
    if "/mail/" in p or p.endswith("/mail"):
        parts = [x for x in p.split("/") if x]
        try:
            i = parts.index("mail")
            nxt = parts[i + 1] if i + 1 < len(parts) else ""
            if nxt and "." not in nxt:
                return f"mail/{nxt}"
        except ValueError:
            pass
        return "mail"
    if "/outbox" in p:
        return "outbox"
    if p.startswith("/workspace/corpus/"):
        bits = p[len("/workspace/") :].split("/")
        return "/".join(bits[:2])
    if p.startswith("/private"):
        return "private"
    if p.startswith("/workspace/") and p != "/workspace":
        return p[len("/workspace/") :].split("/")[0]
    if p.startswith("/Users/"):
        bits = [x for x in p.split("/") if x]
        if len(bits) >= 2:
            return "/".join(bits[:2])
        return p
    return "room"
