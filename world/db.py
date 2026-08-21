from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

EVENT_KINDS = frozenset(
    {"say", "tool_call", "tool_result", "error", "compact", "presence", "file_change"}
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS events_id ON events(id);
CREATE INDEX IF NOT EXISTS events_agent ON events(agent, id);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    path TEXT NOT NULL,
    sha TEXT NOT NULL,
    size INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS snapshots_path ON snapshots(path);

CREATE TABLE IF NOT EXISTS presence (
    agent TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    status TEXT NOT NULL,
    model TEXT,
    last_action TEXT,
    location TEXT,
    place TEXT
);
"""


def _now() -> float:
    return time.time()


def _payload_dumps(payload: Any) -> str:
    if payload is None:
        return "{}"
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, default=str)


def event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["payload"]
    try:
        payload: Any = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"raw": raw}
    return {
        "id": row["id"],
        "ts": row["ts"],
        "agent": row["agent"],
        "kind": row["kind"],
        "name": row["name"],
        "payload": payload,
    }


class Observatory:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(presence)").fetchall()}
        if "place" not in cols:
            self._conn.execute("ALTER TABLE presence ADD COLUMN place TEXT")
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def insert_event(
        self,
        agent: str,
        kind: str,
        name: str | None = None,
        payload: Any = None,
        ts: float | None = None,
    ) -> dict[str, Any]:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind}")
        stamp = _now() if ts is None else ts
        blob = _payload_dumps(payload)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events(ts, agent, kind, name, payload) VALUES (?, ?, ?, ?, ?)",
                (stamp, agent, kind, name or "", blob),
            )
            self._conn.commit()
            event_id = int(cur.lastrowid)
        return {
            "id": event_id,
            "ts": stamp,
            "agent": agent,
            "kind": kind,
            "name": name or "",
            "payload": json.loads(blob) if blob.startswith("{") or blob.startswith("[") else blob,
        }

    def events_after(self, after_id: int = 0, limit: int = 300, agent: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        with self._lock:
            if agent:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE id > ? AND agent = ? ORDER BY id ASC LIMIT ?",
                    (after_id, agent, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (after_id, limit),
                ).fetchall()
        return [event_from_row(r) for r in rows]

    def recent_for_agent(self, agent: str, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE agent = ? ORDER BY id DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
        return [event_from_row(r) for r in reversed(rows)]

    def insert_journal(self, agent: str, text: str) -> dict[str, Any]:
        stamp = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO journal(ts, agent, text) VALUES (?, ?, ?)",
                (stamp, agent, text),
            )
            self._conn.commit()
            jid = int(cur.lastrowid)
        return {"id": jid, "ts": stamp, "agent": agent, "text": text}

    def upsert_snapshot(self, path: str, sha: str, size: int, ts: float | None = None) -> bool:
        """Return True if sha changed (or new)."""
        stamp = _now() if ts is None else ts
        with self._lock:
            row = self._conn.execute(
                "SELECT sha, size FROM snapshots WHERE path = ?", (path,)
            ).fetchone()
            if row and row["sha"] == sha and int(row["size"]) == size:
                return False
            self._conn.execute(
                """
                INSERT INTO snapshots(ts, path, sha, size) VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET ts=excluded.ts, sha=excluded.sha, size=excluded.size
                """,
                (stamp, path, sha, size),
            )
            self._conn.commit()
            return True

    def delete_snapshot(self, path: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM snapshots WHERE path = ?", (path,))
            self._conn.commit()
            return cur.rowcount > 0

    def all_snapshots(self) -> dict[str, tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute("SELECT path, sha, size FROM snapshots").fetchall()
        return {r["path"]: (r["sha"], int(r["size"])) for r in rows}

    def upsert_presence(
        self,
        agent: str,
        status: str,
        model: str | None = None,
        last_action: str | None = None,
        location: str | None = None,
        place: str | None = None,
    ) -> dict[str, Any]:
        stamp = _now()
        with self._lock:
            existing = self._conn.execute(
                "SELECT model, last_action, location, place FROM presence WHERE agent = ?",
                (agent,),
            ).fetchone()
            model_v = model if model is not None else (existing["model"] if existing else "")
            action_v = last_action if last_action is not None else (existing["last_action"] if existing else "")
            loc_v = location if location is not None else (existing["location"] if existing else "local")
            place_v = place if place is not None else (existing["place"] if existing else "")
            self._conn.execute(
                """
                INSERT INTO presence(agent, ts, status, model, last_action, location, place)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent) DO UPDATE SET
                    ts=excluded.ts,
                    status=excluded.status,
                    model=excluded.model,
                    last_action=excluded.last_action,
                    location=excluded.location,
                    place=excluded.place
                """,
                (agent, stamp, status, model_v or "", action_v or "", loc_v or "local", place_v or ""),
            )
            self._conn.commit()
        return {
            "agent": agent,
            "ts": stamp,
            "status": status,
            "model": model_v or "",
            "last_action": action_v or "",
            "location": loc_v or "local",
            "place": place_v or "",
        }

    def list_presence(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM presence").fetchall()
        return [
            {
                "agent": r["agent"],
                "ts": r["ts"],
                "status": r["status"],
                "model": r["model"],
                "last_action": r["last_action"],
                "location": r["location"],
                "place": (r["place"] or "") if "place" in r.keys() else "",
            }
            for r in rows
        ]
