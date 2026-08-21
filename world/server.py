from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from world.config import KNOWN_AGENTS, Settings
from world.db import Observatory
from world.paths import PathJailError, is_private, logical_from_host, logical_workspace, place_from_path, resolve_host_path

SKIP_NAMES = {".git", ".DS_Store", "__pycache__"}


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subs.discard(q)


class AppState:
    def __init__(self) -> None:
        self.settings = Settings()
        self.db = Observatory(self.settings.db_path)
        self.bus = EventBus()
        self.scan_task: asyncio.Task[None] | None = None


STATE = AppState()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha_size(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return sha256_bytes(data), len(data)


async def emit(agent: str, kind: str, name: str | None = None, payload: Any = None) -> dict[str, Any]:
    event = STATE.db.insert_event(agent, kind, name, payload)
    await STATE.bus.publish(event)
    return event


async def record_file_change(logical: str, host: Path, agent: str) -> None:
    place = place_from_path(logical)
    if not host.is_file():
        if STATE.db.delete_snapshot(logical):
            await emit(
                agent,
                "file_change",
                logical,
                {"path": logical, "deleted": True, "place": place},
            )
        return
    sha, size = file_sha_size(host)
    changed = STATE.db.upsert_snapshot(logical, sha, size)
    if changed:
        await emit(
            agent,
            "file_change",
            logical,
            {"path": logical, "sha": sha, "size": size, "place": place},
        )


async def forget_snapshots_under(prefix: str, agent: str) -> None:
    root = prefix.rstrip("/")
    for logical in list(STATE.db.all_snapshots()):
        if logical == root or logical.startswith(root + "/"):
            if STATE.db.delete_snapshot(logical):
                await emit(
                    agent,
                    "file_change",
                    logical,
                    {"path": logical, "deleted": True, "place": place_from_path(logical)},
                )


async def record_tree(host: Path, logical: str, agent: str) -> None:
    if host.is_file():
        await record_file_change(logical, host, agent)
        return
    if not host.is_dir():
        return
    root = host.resolve()
    for dirpath, dirnames, filenames in os.walk(host):
        dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
        for name in filenames:
            if name in SKIP_NAMES:
                continue
            fp = Path(dirpath) / name
            try:
                rel = fp.resolve().relative_to(root)
            except ValueError:
                continue
            child = logical.rstrip("/") + "/" + rel.as_posix()
            await record_file_change(child, fp, agent)


def _token_ok(provided: str | None) -> bool:
    expected = STATE.settings.world_token
    if not expected:
        return False
    return provided == expected


def bearer_from_header(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def agent_or_viewer(x_agent_id: str | None) -> str | None:
    if not x_agent_id:
        return None
    if not re.match(r"^[a-zA-Z0-9_-]{1,40}$", x_agent_id):
        raise HTTPException(status_code=400, detail="bad X-Agent-Id")
    return x_agent_id


def missing_lookup(logical: str, **extra: Any) -> dict[str, Any]:
    """Quiet miss: HTTP 200 so the glass does not paint a probe as 'failed'."""
    body: dict[str, Any] = {
        "ok": False,
        "exists": False,
        "path": logical,
        "error": "not found",
    }
    body.update(extra)
    return body


def resolve_or_400(
    path: str,
    agent_id: str | None,
    *,
    must_exist: bool = False,
    write: bool = False,
) -> tuple[Path, str]:
    try:
        host, logical = resolve_host_path(
            path,
            agent_id=agent_id,
            habitat=STATE.settings.habitat_root,
            private_root=STATE.settings.private_root,
            must_exist=must_exist,
        )
    except PathJailError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if is_private(logical) and not agent_id:
        raise HTTPException(status_code=403, detail="private path needs X-Agent-Id")
    return host, logical


class WriteBody(BaseModel):
    path: str
    content: str
    mode: Literal["write", "append"] = "write"


class GrepBody(BaseModel):
    pattern: str
    path: str = "/workspace"


class MailBody(BaseModel):
    to: str
    sender: str = Field(alias="from")
    body: str

    model_config = {"populate_by_name": True}


class SayBody(BaseModel):
    agent: str
    text: str


class ToolBody(BaseModel):
    agent: str
    kind: Literal["tool_call", "tool_result", "error", "compact"]
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PresenceBody(BaseModel):
    agent: str
    status: str
    model: str | None = None
    last_action: str | None = None
    location: str | None = None
    place: str | None = None


class MoveBody(BaseModel):
    src: str
    dest: str


class MkdirBody(BaseModel):
    path: str


class JournalBody(BaseModel):
    text: str
    agent: str | None = None


def scan_habitat_once(agent: str = "world", *, emit_events: bool = True) -> list[dict[str, Any]]:
    habitat = STATE.settings.habitat_root
    if not habitat.is_dir():
        return []
    seen: set[str] = set()
    events: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(habitat):
        dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
        for name in filenames:
            if name in SKIP_NAMES:
                continue
            host = Path(dirpath) / name
            if host.is_symlink() or not host.is_file():
                continue
            logical = logical_workspace(host, habitat)
            if not logical:
                continue
            seen.add(logical)
            try:
                sha, size = file_sha_size(host)
            except OSError:
                continue
            if STATE.db.upsert_snapshot(logical, sha, size):
                if emit_events:
                    events.append(
                        STATE.db.insert_event(
                            agent,
                            "file_change",
                            logical,
                            {"path": logical, "sha": sha, "size": size, "via": "scan"},
                        )
                    )
    known = STATE.db.all_snapshots()
    for logical in list(known):
        if logical.startswith("/workspace/") or logical == "/workspace":
            if logical not in seen and logical != "/workspace":
                if STATE.db.delete_snapshot(logical):
                    if emit_events:
                        events.append(
                            STATE.db.insert_event(
                                agent,
                                "file_change",
                                logical,
                                {"path": logical, "deleted": True, "via": "scan"},
                            )
                        )
    return events


async def scan_loop() -> None:
    while True:
        await asyncio.sleep(STATE.settings.scan_interval_sec)
        try:
            events = await asyncio.to_thread(scan_habitat_once)
            for event in events:
                await STATE.bus.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


@asynccontextmanager
async def lifespan(_app: FastAPI):
    STATE.settings.habitat_root.mkdir(parents=True, exist_ok=True)
    STATE.settings.private_root.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(scan_habitat_once, "world", emit_events=False)
    STATE.scan_task = asyncio.create_task(scan_loop())
    yield
    if STATE.scan_task:
        STATE.scan_task.cancel()
        try:
            await STATE.scan_task
        except asyncio.CancelledError:
            pass
    STATE.db.close()


app = FastAPI(title="Antfarm worldd", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if (
        path == "/health"
        or path == "/"
        or path.startswith("/assets")
        or path in {"/app.js", "/style.css", "/index.html", "/favicon.ico"}
    ):
        return await call_next(request)
    if path == "/ws":
        return await call_next(request)
    if path.startswith("/api/"):
        token = bearer_from_header(request.headers.get("authorization"))
        token = token or request.headers.get("x-world-token")
        token = token or request.query_params.get("token")
        if not _token_ok(token):
            return json_status(401, "bad token")
    return await call_next(request)


def json_status(code: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": detail}, status_code=code)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "habitat": str(STATE.settings.habitat_root), "db": str(STATE.settings.db_path)}


@app.get("/api/agents")
def agents() -> dict[str, Any]:
    present = {p["agent"]: p for p in STATE.db.list_presence()}
    roster: list[dict[str, Any]] = []
    seen: set[str] = set()
    for known in KNOWN_AGENTS:
        aid = known["id"]
        seen.add(aid)
        row = present.get(aid, {})
        roster.append(
            {
                "id": aid,
                "origin": row.get("location") or known["origin"],
                "costume": known["costume"],
                "status": row.get("status") or "offline",
                "model": row.get("model") or "",
                "last_action": row.get("last_action") or "",
                "place": row.get("place") or "",
                "ts": row.get("ts") or 0,
            }
        )
    for aid, row in present.items():
        if aid in seen or aid in {"world", "proxy"}:
            continue
        roster.append(
            {
                "id": aid,
                "origin": row.get("location") or "cloud",
                "costume": "",
                "status": row.get("status") or "offline",
                "model": row.get("model") or "",
                "last_action": row.get("last_action") or "",
                "place": row.get("place") or "",
                "ts": row.get("ts") or 0,
            }
        )
    return {"agents": roster}


@app.get("/api/events")
def events(
    after: int = 0,
    limit: int = 300,
    agent: str | None = None,
) -> dict[str, Any]:
    return {"events": STATE.db.events_after(after, limit=limit, agent=agent)}


@app.get("/api/fs/list")
def fs_list(
    path: str = "/workspace",
    deep: bool = False,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    agent = agent_or_viewer(x_agent_id)
    host, logical = resolve_or_400(path, agent)
    if not host.exists():
        return missing_lookup(logical, entries=[])
    if host.is_file():
        st = host.stat()
        return {
            "path": logical,
            "entries": [
                {"name": host.name, "path": logical, "type": "file", "size": st.st_size, "mtime": st.st_mtime}
            ],
        }
    entries: list[dict[str, Any]] = []
    if deep:
        root = host
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_NAMES)
            for name in sorted(filenames):
                if name in SKIP_NAMES:
                    continue
                fp = Path(dirpath) / name
                rel = fp.relative_to(root).as_posix()
                item_logical = logical.rstrip("/") + "/" + rel if logical != "/" else "/" + rel
                try:
                    st = fp.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "name": name,
                        "path": item_logical,
                        "type": "file",
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
            for name in dirnames:
                dp = Path(dirpath) / name
                rel = dp.relative_to(root).as_posix()
                item_logical = logical.rstrip("/") + "/" + rel
                try:
                    st = dp.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "name": name,
                        "path": item_logical,
                        "type": "dir",
                        "size": 0,
                        "mtime": st.st_mtime,
                    }
                )
    else:
        try:
            kids = sorted(host.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        for kid in kids:
            if kid.name in SKIP_NAMES:
                continue
            kid_logical = logical.rstrip("/") + "/" + kid.name
            try:
                st = kid.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": kid.name,
                    "path": kid_logical,
                    "type": "dir" if kid.is_dir() else "file",
                    "size": 0 if kid.is_dir() else st.st_size,
                    "mtime": st.st_mtime,
                }
            )
    return {"ok": True, "exists": True, "path": logical, "entries": entries}


@app.get("/api/fs/read")
def fs_read(
    path: str,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    agent = agent_or_viewer(x_agent_id)
    host, logical = resolve_or_400(path, agent)
    if not host.exists():
        return missing_lookup(logical)
    if host.is_dir():
        raise HTTPException(status_code=400, detail="path is a directory")
    size = host.stat().st_size
    if size > STATE.settings.max_read_bytes:
        raise HTTPException(status_code=413, detail="file too large")
    try:
        text = host.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="not utf-8 text") from None
    return {"ok": True, "exists": True, "path": logical, "content": text, "size": size}


@app.put("/api/fs/write")
async def fs_write(
    body: WriteBody,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    agent = agent_or_viewer(x_agent_id)
    if not agent:
        raise HTTPException(status_code=400, detail="X-Agent-Id required to write")
    data = body.content.encode("utf-8")
    if len(data) > STATE.settings.max_write_bytes:
        raise HTTPException(status_code=413, detail="write too large")
    host, logical = resolve_or_400(body.path, agent, write=True)
    if host.exists() and host.is_dir():
        raise HTTPException(status_code=400, detail="path is a directory")
    host.parent.mkdir(parents=True, exist_ok=True)
    if body.mode == "append" and host.exists():
        with host.open("a", encoding="utf-8") as fh:
            fh.write(body.content)
    else:
        host.write_text(body.content, encoding="utf-8")
    await record_file_change(logical, host, agent)
    return {"ok": True, "path": logical, "size": host.stat().st_size, "mode": body.mode}


@app.post("/api/fs/mkdir")
async def fs_mkdir(
    body: MkdirBody,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    agent = agent_or_viewer(x_agent_id)
    if not agent:
        raise HTTPException(status_code=400, detail="X-Agent-Id required")
    host, logical = resolve_or_400(body.path, agent, write=True)
    if host.exists() and not host.is_dir():
        raise HTTPException(status_code=400, detail="exists and is not a directory")
    host.mkdir(parents=True, exist_ok=True)
    await emit(
        agent,
        "file_change",
        logical,
        {"path": logical, "mkdir": True, "place": place_from_path(logical)},
    )
    return {"ok": True, "path": logical, "place": place_from_path(logical)}


@app.post("/api/fs/move")
async def fs_move(
    body: MoveBody,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    agent = agent_or_viewer(x_agent_id)
    if not agent:
        raise HTTPException(status_code=400, detail="X-Agent-Id required")
    src_host, src_logical = resolve_or_400(body.src, agent, write=True)
    if not src_host.exists():
        return missing_lookup(src_logical)
    dest_host, dest_logical = resolve_or_400(body.dest, agent, write=True)
    src_root = src_logical.rstrip("/")
    if dest_logical.rstrip("/") == src_root or dest_logical.rstrip("/").startswith(src_root + "/"):
        raise HTTPException(status_code=400, detail="cannot move a path into itself")
    if dest_host.exists() and dest_host.is_dir():
        dest_host = dest_host / src_host.name
        dest_logical = dest_logical.rstrip("/") + "/" + src_host.name
    elif dest_host.exists():
        raise HTTPException(status_code=409, detail="destination exists")
    dest_host.parent.mkdir(parents=True, exist_ok=True)
    was_dir = src_host.is_dir()
    shutil.move(str(src_host), str(dest_host))
    await forget_snapshots_under(src_logical, agent)
    await record_tree(dest_host, dest_logical, agent)
    return {
        "ok": True,
        "src": src_logical,
        "dest": dest_logical,
        "dir": was_dir,
        "place": place_from_path(dest_logical),
    }


@app.post("/api/fs/grep")
def fs_grep(
    body: GrepBody,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    agent = agent_or_viewer(x_agent_id)
    host, logical = resolve_or_400(body.path, agent)
    try:
        cre = re.compile(body.pattern)
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"bad pattern: {exc}") from exc
    hits: list[dict[str, Any]] = []
    files: list[Path] = []
    if host.is_file():
        files = [host]
    elif host.is_dir():
        for dirpath, dirnames, filenames in os.walk(host):
            dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
            for name in filenames:
                if name in SKIP_NAMES:
                    continue
                files.append(Path(dirpath) / name)
    else:
        return missing_lookup(logical, pattern=body.pattern, hits=[])
    for fp in files:
        if not fp.is_file() or fp.stat().st_size > STATE.settings.max_read_bytes:
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        logi = logical_from_host(
            fp,
            STATE.settings.habitat_root,
            private_root=STATE.settings.private_root,
            agent_id=agent,
        )
        for i, line in enumerate(text.splitlines(), start=1):
            if cre.search(line):
                hits.append({"path": logi, "line": i, "text": line[:400]})
                if len(hits) >= STATE.settings.max_grep_hits:
                    return {"pattern": body.pattern, "path": logical, "hits": hits, "truncated": True}
    return {"pattern": body.pattern, "path": logical, "hits": hits, "truncated": False}


@app.post("/api/mail")
async def post_mail(
    body: MailBody,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    agent = agent_or_viewer(x_agent_id) or body.sender
    if not re.match(r"^[a-zA-Z0-9_-]{1,40}$", body.to):
        raise HTTPException(status_code=400, detail="bad recipient")
    if not re.match(r"^[a-zA-Z0-9_-]{1,40}$", body.sender):
        raise HTTPException(status_code=400, detail="bad sender")
    ts = int(time.time())
    rel = f"mail/{body.to}/{body.sender}-{ts}.md"
    logical = "/workspace/" + rel
    host = STATE.settings.habitat_root / rel
    host.parent.mkdir(parents=True, exist_ok=True)
    content = f"# letter\n\nfrom: {body.sender}\nto: {body.to}\nts: {ts}\n\n{body.body}\n"
    host.write_text(content, encoding="utf-8")
    await record_file_change(logical, host, agent)
    await emit(
        agent,
        "say",
        "mail",
        {"to": body.to, "from": body.sender, "path": logical},
    )
    return {"ok": True, "path": logical}


@app.get("/api/mail")
def get_mail(agent: str, limit: int = 20) -> dict[str, Any]:
    if not re.match(r"^[a-zA-Z0-9_-]{1,40}$", agent):
        raise HTTPException(status_code=400, detail="bad agent")
    inbox = STATE.settings.habitat_root / "mail" / agent
    letters: list[dict[str, Any]] = []
    if inbox.is_dir():
        files = sorted(inbox.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        for fp in files[:limit]:
            letters.append(
                {
                    "path": f"/workspace/mail/{agent}/{fp.name}",
                    "name": fp.name,
                    "mtime": fp.stat().st_mtime,
                    "excerpt": fp.read_text(encoding="utf-8", errors="replace")[:400],
                }
            )
    return {"agent": agent, "letters": letters}


@app.post("/api/say")
async def post_say(body: SayBody) -> dict[str, Any]:
    event = await emit(body.agent, "say", "say", {"text": body.text})
    return {"ok": True, "event": event}


@app.post("/api/tool")
async def post_tool(body: ToolBody) -> dict[str, Any]:
    event = await emit(body.agent, body.kind, body.name, body.payload)
    return {"ok": True, "event": event}


@app.post("/api/presence")
async def post_presence(body: PresenceBody) -> dict[str, Any]:
    row = STATE.db.upsert_presence(
        body.agent,
        body.status,
        model=body.model,
        last_action=body.last_action,
        location=body.location,
        place=body.place,
    )
    event = await emit(
        body.agent,
        "presence",
        body.status,
        {
            "status": body.status,
            "model": row["model"],
            "last_action": row["last_action"],
            "location": row["location"],
            "place": row.get("place") or "",
        },
    )
    return {"ok": True, "presence": row, "event": event}


@app.post("/api/journal")
async def post_journal(
    body: JournalBody,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
) -> dict[str, Any]:
    agent = agent_or_viewer(x_agent_id) or body.agent
    if not agent:
        raise HTTPException(status_code=400, detail="agent required")
    row = STATE.db.insert_journal(agent, body.text)
    host, logical = resolve_or_400("/private/JOURNAL.md", agent, write=True)
    prev = host.read_text(encoding="utf-8") if host.exists() else ""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"]))
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text(prev + f"\n## {stamp}\n\n{body.text}\n", encoding="utf-8")
    await record_file_change(logical, host, agent)
    return {"ok": True, "journal": row}


@app.get("/api/diff")
def api_diff() -> dict[str, Any]:
    habitat = STATE.settings.habitat_root
    snaps = STATE.db.all_snapshots()
    changed: list[dict[str, Any]] = []
    current: set[str] = set()
    if habitat.is_dir():
        for dirpath, dirnames, filenames in os.walk(habitat):
            dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
            for name in filenames:
                if name in SKIP_NAMES:
                    continue
                host = Path(dirpath) / name
                logical = logical_workspace(host, habitat)
                if not logical:
                    continue
                current.add(logical)
                try:
                    sha, size = file_sha_size(host)
                except OSError:
                    continue
                old = snaps.get(logical)
                if old is None or old[0] != sha:
                    changed.append({"path": logical, "sha": sha, "size": size})
    for logical, (sha, size) in snaps.items():
        if logical not in current and logical.startswith("/workspace"):
            changed.append({"path": logical, "sha": sha, "size": size, "deleted": True})
    return {"files": changed}


@app.websocket("/ws")
async def ws_feed(ws: WebSocket, token: str | None = Query(default=None)):
    header_token = bearer_from_header(ws.headers.get("authorization"))
    got = token or header_token or ws.headers.get("x-world-token")
    if not _token_ok(got):
        await ws.close(code=1008)
        return
    await ws.accept()
    q = STATE.bus.subscribe()
    try:
        hello = {"kind": "hello", "agents": agents()["agents"]}
        await ws.send_json(hello)
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=20)
                await ws.send_json(event)
            except TimeoutError:
                await ws.send_json({"kind": "ping", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        STATE.bus.unsubscribe(q)


@app.get("/")
def glass() -> FileResponse:
    index = STATE.settings.viewer_dir / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=500, detail="viewer missing")
    return FileResponse(index, headers={"Cache-Control": "no-store"})


@app.get("/app.js")
def viewer_js() -> FileResponse:
    return FileResponse(
        STATE.settings.viewer_dir / "app.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/style.css")
def viewer_css() -> FileResponse:
    return FileResponse(
        STATE.settings.viewer_dir / "style.css",
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


def main() -> None:
    settings = Settings()
    if not settings.world_token:
        raise SystemExit("WORLD_TOKEN missing. Copy .env.example to .env")
    uvicorn.run(
        "world.server:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
