from __future__ import annotations

import json
import os
import re
import socket
from typing import Any

import httpx

_ORIG_GETADDRINFO = socket.getaddrinfo
_IPV4_DNS = False


def _force_ipv4_dns() -> None:
    """Docker Desktop injects an unroutable IPv6 host.docker.internal.

    HTTPTransport(local_address='0.0.0.0') looks IPv4-only, but create_connection
    still walks AAAA first: bind(('0.0.0.0',0)) on an AF_INET6 socket raises
    gaierror [-9] Address family for hostname not supported.
    """
    global _IPV4_DNS
    if _IPV4_DNS:
        return

    def getaddrinfo(
        host: Any,
        port: Any,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[Any, ...]]:
        if family in (0, socket.AF_UNSPEC):
            family = socket.AF_INET
        return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)

    socket.getaddrinfo = getaddrinfo  # type: ignore[method-assign]
    _IPV4_DNS = True


_force_ipv4_dns()

TOOL_NAMES = frozenset(
    {
        "list_dir",
        "read_file",
        "write_file",
        "append_file",
        "grep",
        "journal",
        "fetch_url",
        "web_search",
        "move_file",
        "mkdir",
        "recent_changes",
        "run",
    }
)

JSON_SCHEMA_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a directory. /workspace is the habitat; other absolute paths work too.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a utf-8 text file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Overwrite a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "Append to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search files for a regex.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "journal",
            "description": "Write a private journal note. Not visible on the glass as mail.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "GET a URL through the fetch proxy. May save a copy if you ask keep=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "keep": {
                        "type": "boolean",
                        "description": "If true, always save a copy under /workspace/outbox/",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web. Returns titles, urls, snippets.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move or rename a file or folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "dest": {"type": "string"},
                },
                "required": ["src", "dest"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mkdir",
            "description": "Create a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_changes",
            "description": "List habitat files that changed since the last snapshot. Use instead of re-listing everything.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run",
            "description": "Run a shell command in the container, 120s timeout.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


def tools_for_model(*, enable_run: bool) -> list[dict[str, Any]]:
    if enable_run:
        return JSON_SCHEMA_TOOLS
    return [t for t in JSON_SCHEMA_TOOLS if t["function"]["name"] != "run"]


def ipv4_client(**kwargs: Any) -> httpx.Client:
    """IPv4-only HTTP. Docker Desktop's host.docker.internal AAAA is unroutable."""
    _force_ipv4_dns()
    return httpx.Client(**kwargs)


class WorldClient:
    def __init__(self, base: str, token: str, agent_id: str, *, timeout: float = 60.0) -> None:
        self.base = base.rstrip("/")
        self.agent_id = agent_id
        self._client = ipv4_client(
            base_url=self.base,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Agent-Id": agent_id,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _req(self, method: str, path: str, **kwargs: Any) -> Any:
        r = self._client.request(method, path, **kwargs)
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    def say(self, text: str) -> Any:
        return self._req("POST", "/api/say", json={"agent": self.agent_id, "text": text})

    def presence(
        self,
        status: str,
        model: str = "",
        last_action: str = "",
        location: str = "local",
        place: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {
            "agent": self.agent_id,
            "status": status,
            "model": model,
            "last_action": last_action,
            "location": location,
        }
        if place:
            body["place"] = place
        return self._req("POST", "/api/presence", json=body)

    def tool_event(self, kind: str, name: str, payload: dict[str, Any]) -> Any:
        clipped = _clip(payload, 1500)
        return self._req(
            "POST",
            "/api/tool",
            json={"agent": self.agent_id, "kind": kind, "name": name, "payload": clipped},
        )

    def log_error(self, message: str) -> None:
        try:
            self._req(
                "POST",
                "/api/tool",
                json={
                    "agent": self.agent_id,
                    "kind": "error",
                    "name": "error",
                    "payload": {"message": message},
                },
            )
        except httpx.HTTPError:
            pass

    def events(self, after: int = 0, limit: int = 40) -> list[dict[str, Any]]:
        data = self._req("GET", "/api/events", params={"after": after, "limit": limit, "agent": self.agent_id})
        return data.get("events", [])

    def list_dir(self, path: str) -> Any:
        return self._req("GET", "/api/fs/list", params={"path": path})

    def read_file(self, path: str) -> Any:
        return self._req("GET", "/api/fs/read", params={"path": path})

    def write_file(self, path: str, content: str, mode: str = "write") -> Any:
        return self._req("PUT", "/api/fs/write", json={"path": path, "content": content, "mode": mode})

    def grep(self, pattern: str, path: str = "/workspace") -> Any:
        return self._req("POST", "/api/fs/grep", json={"pattern": pattern, "path": path})

    def mail(self, to: str, body: str) -> Any:
        return self._req("POST", "/api/mail", json={"to": to, "from": self.agent_id, "body": body})

    def journal(self, text: str) -> Any:
        return self._req("POST", "/api/journal", json={"text": text, "agent": self.agent_id})

    def agents(self) -> list[str]:
        data = self._req("GET", "/api/agents")
        return [a["id"] for a in data.get("agents", [])]

    def mkdir(self, path: str) -> Any:
        return self._req("POST", "/api/fs/mkdir", json={"path": path})

    def move_file(self, src: str, dest: str) -> Any:
        return self._req("POST", "/api/fs/move", json={"src": src, "dest": dest})

    def recent_changes(self) -> Any:
        return self._req("GET", "/api/diff")


def _clip(payload: dict[str, Any], n: int) -> dict[str, Any]:
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except TypeError:
        raw = str(payload)
    if len(raw) <= n:
        return payload
    return {"truncated": True, "preview": raw[:n]}


def outbox_name(url: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", url.replace("https://", "").replace("http://", ""))
    slug = slug.strip("-")[:80] or "fetch"
    return f"/workspace/outbox/{slug}.md"


class Toolbelt:
    def __init__(
        self,
        world: WorldClient,
        *,
        fetch_proxy: str,
        enable_run: bool,
        workspace_cwd: str = "/workspace",
    ) -> None:
        self.world = world
        self.fetch_proxy = fetch_proxy.rstrip("/")
        self.enable_run = enable_run
        self.workspace_cwd = workspace_cwd
        self._http = ipv4_client(timeout=70.0)

    def begin_wake(self) -> None:
        return

    def close(self) -> None:
        self._http.close()

    def _lookup(self, fn, path: str, extra: dict[str, Any] | None = None) -> Any:
        try:
            return fn()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                body: dict[str, Any] = {
                    "ok": False,
                    "exists": False,
                    "path": path,
                    "error": "not found",
                }
                if extra:
                    body.update(extra)
                return body
            raise

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name not in TOOL_NAMES:
            return f"tool error: unknown tool {name}. you do not have it."
        if name == "run" and not self.enable_run:
            return "tool error: run is disabled on this host."
        try:
            result = self._dispatch(name, args)
        except httpx.HTTPStatusError as exc:
            return f"tool error: {name} HTTP {exc.response.status_code} {exc.response.text[:300]}"
        except Exception as exc:  # noqa: BLE001 — surface to the model, do not crash the fish
            return f"tool error: {name}: {exc}"
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)[:8000]

    def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        path = str(args.get("path") or "/workspace")
        if name == "list_dir":
            if not args.get("path"):
                return "tool error: path is required."
            return self._lookup(lambda: self.world.list_dir(path), path, extra={"entries": []})
        if name == "read_file":
            return self._lookup(lambda: self.world.read_file(path), path)
        if name == "write_file":
            return self._write(path, str(args.get("content") or ""), "write")
        if name == "append_file":
            return self._write(path, str(args.get("content") or ""), "append")
        if name == "grep":
            gpath = str(args.get("path") or "/workspace")
            return self._lookup(
                lambda: self.world.grep(str(args.get("pattern") or ""), gpath),
                gpath,
                extra={"hits": [], "pattern": str(args.get("pattern") or "")},
            )
        if name == "journal":
            return self.world.journal(str(args.get("text") or ""))
        if name == "fetch_url":
            return self._fetch(str(args.get("url") or ""), keep=bool(args.get("keep")))
        if name == "web_search":
            return self._search(str(args.get("query") or ""))
        if name == "move_file":
            src, dest = str(args.get("src") or ""), str(args.get("dest") or "")
            if not src or not dest:
                return "tool error: move_file needs src and dest."
            return self._lookup(lambda: self.world.move_file(src, dest), src)
        if name == "mkdir":
            return self.world.mkdir(path)
        if name == "recent_changes":
            return self.world.recent_changes()
        if name == "run":
            return self._run(str(args.get("command") or ""))
        return f"tool error: unknown tool {name}"

    def _write(self, path: str, content: str, mode: str) -> Any:
        return self.world.write_file(path, content, mode)

    def _fetch(self, url: str, *, keep: bool) -> dict[str, Any]:
        if not url.startswith("http://") and not url.startswith("https://"):
            return {"error": "only http(s) urls"}
        r = self._http.get(f"{self.fetch_proxy}/fetch", params={"url": url})
        r.raise_for_status()
        data = r.json()
        text = str(data.get("text") or "")
        worth = keep or (len(text) > 80 and "error" not in data)
        saved = None
        if worth and text:
            dest = outbox_name(url)
            body = f"# fetch\n\nsource: {url}\n\n{text[:10_000_000]}\n"
            self.world.write_file(dest, body, "write")
            saved = dest
        return {"url": url, "status": data.get("status"), "saved": saved, "text": text[:4000]}

    def _search(self, query: str) -> Any:
        r = self._http.get(f"{self.fetch_proxy}/search", params={"q": query})
        r.raise_for_status()
        return r.json()

    def _run(self, command: str) -> dict[str, Any]:
        import subprocess

        if not command.strip():
            return {"error": "empty command"}
        home = os.environ.get("HOME") or "/private"
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": home,
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        proxy = os.environ.get("FETCH_PROXY", self.fetch_proxy)
        if proxy:
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
        run_timeout = float(os.environ.get("RUN_TIMEOUT_SEC", "120"))
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.workspace_cwd if os.path.isdir(self.workspace_cwd) else os.getcwd(),
                capture_output=True,
                text=True,
                timeout=run_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return {"error": f"timeout ({int(run_timeout)}s)"}
        out = (proc.stdout or "")[-4000:]
        err = (proc.stderr or "")[-2000:]
        return {"code": proc.returncode, "stdout": out, "stderr": err}


def place_from_path(path: str) -> str:
    """Room in the tank. Duplicated from world.paths — the fish image does not ship world/."""
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
    return "room"


def place_from_tool(name: str, args: dict[str, Any] | None) -> str:
    args = args or {}
    if name in {"fetch_url", "web_search"}:
        return "outbox"
    if name == "journal":
        return "private"
    if name == "move_file":
        return place_from_path(str(args.get("dest") or args.get("src") or ""))
    path = str(args.get("path") or "")
    return place_from_path(path) if path else ""
