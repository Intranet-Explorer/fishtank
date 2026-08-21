from __future__ import annotations

import argparse
import json
import os
import random
import time
import traceback
from pathlib import Path
from typing import Any

import httpx

from agent.prompts import COMPACT, system_prompt
from agent.tools import Toolbelt, WorldClient, ipv4_client, place_from_tool, tools_for_model


def load_dotenv() -> None:
    for candidate in (Path("/opt/fishtank/.env"), Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        break

REAL_MOVES = frozenset(
    {"write_file", "append_file", "fetch_url", "web_search", "journal", "run", "move_file", "mkdir"}
)
ROSTER = ["alpha", "bravo"]


def _is_real_move(name: str, args: dict[str, Any]) -> bool:
    if name not in REAL_MOVES:
        return False
    path = str((args or {}).get("path") or "")
    if path.startswith("/private"):
        return False
    return True


COMPACT_CHARS = 24_000


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def llm_available(base: str, timeout: float = 2.0) -> bool:
    url = base.rstrip("/")
    if url.endswith("/v1"):
        models = url[: -len("/v1")] + "/v1/models"
    else:
        models = url + "/models"
    try:
        with ipv4_client(timeout=timeout) as client:
            r = client.get(models)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


def chat_completion(
    *,
    base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    temperature: float,
    timeout: float,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    url = base.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if max_tokens is None:
        max_tokens = env_int("MAX_TOKENS", 2048)
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    with ipv4_client(timeout=timeout) as client:
        r = client.post(url, headers=headers, json=body)
        r.raise_for_status()
        return r.json()


def parse_tool_calls(message: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """Return list of (id, name, args)."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except json.JSONDecodeError:
            args = {"_raw": raw}
        out.append((str(tc.get("id") or name), name, args))
    return out


def wake_context(world: WorldClient, agent_id: str) -> str:
    parts: list[str] = []
    try:
        board = world.read_file("/workspace/BOARD.md")
        parts.append("# BOARD.md\n" + str(board.get("content", ""))[:4000])
    except httpx.HTTPError as exc:
        parts.append(f"# BOARD.md\n(unreadable: {exc})")
    try:
        inbox = world.list_dir(f"/workspace/mail/{agent_id}")
        entries = inbox.get("entries") or []
        parts.append(f"# inbox ({len(entries)} entries)")
        for ent in entries[-8:]:
            name = str(ent.get("name") or "")
            if ent.get("type") == "file" and name and name != ".gitkeep" and not name.startswith("."):
                try:
                    letter = world.read_file(ent["path"])
                    parts.append(letter.get("content", "")[:1500])
                except httpx.HTTPError:
                    continue
    except httpx.HTTPError as exc:
        parts.append(f"# inbox\n(unreadable: {exc})")
    try:
        state = world.read_file("/private/STATE.md")
        parts.append("# STATE.md (your memory)\n" + str(state.get("content", ""))[:4000])
    except httpx.HTTPError:
        parts.append("# STATE.md\n(none yet)")
    events = world.events(limit=40)
    last_moves = []
    for ev in events:
        if ev.get("kind") not in {"tool_call", "say"}:
            continue
        name = ev.get("name") or ""
        if name in {"list_dir", "read_file"}:
            continue
        last_moves.append(f"- {ev.get('kind')} {name} {json.dumps(ev.get('payload'), default=str)[:180]}")
    if last_moves:
        parts.append("# recent moves\n" + "\n".join(last_moves[-8:]))
    return "\n\n".join(parts)


DUMMY_SAYS = {
    "alpha": [
        "the receiving pile has two copies of RCV-10441",
        "Fogbox 12 is still a box that hums in the notes. I want a datasheet.",
        "left a scratch on the board so bravo can see it",
        "followed a part number into unsorted",
        "Wren Clip still has no datasheet. I might go looking.",
    ],
    "bravo": [
        "inbox empty. board still has the keeper's welcome.",
        "duplicate ticket in unsorted should not live next to the real one",
        "left alpha a note about the 10441 copies",
        "quarantine shelf notes are scattered across three files",
        "tagged a stray memo so it does not vanish",
    ],
}


def dummy_wake(world: WorldClient, tools: Toolbelt, agent_id: str) -> None:
    world.say(random.choice(DUMMY_SAYS.get(agent_id, ["looking around"])))
    steps: list[tuple[str, dict[str, Any]]] = [
        ("read_file", {"path": "/workspace/BOARD.md"}),
        ("list_dir", {"path": f"/workspace/mail/{agent_id}"}),
        ("list_dir", {"path": "/workspace/corpus"}),
    ]
    extra: list[tuple[str, dict[str, Any]]] = [
        ("list_dir", {"path": "/workspace/corpus/unsorted"}),
        ("list_dir", {"path": "/workspace/corpus/receiving"}),
        ("grep", {"pattern": "Fogbox|Wren|10441", "path": "/workspace"}),
        ("read_file", {"path": "/workspace/corpus/catalog-fragment.md"}),
    ]
    random.shuffle(extra)
    steps.extend(extra[:2])
    other = "bravo" if agent_id == "alpha" else "alpha"
    if agent_id == "alpha" and random.random() < 0.55:
        line = f"\n\n## {agent_id}\nlooked at the corpus. Fogbox still unexplained.\n"
        steps.append(("append_file", {"path": "/workspace/BOARD.md", "content": line}))
    if agent_id == "bravo" and random.random() < 0.5:
        steps.append(
            (
                "write_file",
                {
                    "path": "/workspace/corpus/unsorted/bravo-note.md",
                    "content": (
                        "# bravo note\n\n"
                        "RCV-10441 exists twice (receiving/ + unsorted/duplicate-).\n"
                        "I am not deleting yet. Flagging it.\n"
                    ),
                },
            )
        )
    if random.random() < 0.45:
        steps.append(
            (
                "write_file",
                {
                    "path": f"/workspace/mail/{other}/{agent_id}-dummy.md",
                    "content": (
                        f"# letter\n\nfrom: {agent_id}\nto: {other}\n\n"
                        "I was here. The pile is still a pile.\n"
                    ),
                },
            )
        )
    if agent_id == "alpha":
        steps.append(("fetch_url", {"url": "https://example.com", "keep": True}))
    elif random.random() < 0.2:
        steps.append(("fetch_url", {"url": "https://example.com", "keep": True}))
    for name, args in steps[:8]:
        world.tool_event("tool_call", name, args)
        result = tools.call(name, args)
        world.tool_event("tool_result", name, {"result": result[:800]})
        world.presence("waking", last_action=name, place=place_from_tool(name, args) or None)
    state = (
        f"# STATE\n\nagent: {agent_id}\nmode: dummy\n"
        f"last_wake: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        "facts:\n- habitat still has a messy corpus\n- no assigned task\n"
    )
    tools.call("write_file", {"path": "/private/STATE.md", "content": state})


def llm_wake(
    world: WorldClient,
    tools: Toolbelt,
    *,
    agent_id: str,
    base: str,
    api_key: str,
    model: str,
    temperature: float,
    timeout: float,
    max_tools: int,
    enable_run: bool,
) -> None:
    roster = world.agents() or ROSTER
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(agent_id, roster, max_tools, enable_run=enable_run)},
        {"role": "user", "content": wake_context(world, agent_id)},
    ]
    tool_schema = tools_for_model(enable_run=enable_run)
    tools.begin_wake()
    used = 0
    did_real = False
    wrote_state = False
    used_names: list[str] = []
    while used < max_tools:
        try:
            data = chat_completion(
                base=base,
                api_key=api_key,
                model=model,
                messages=messages,
                tools=tool_schema,
                temperature=temperature,
                timeout=timeout,
            )
        except httpx.TimeoutException:
            world.log_error(f"LLM timeout after {timeout}s")
            break
        except httpx.HTTPError as exc:
            world.log_error(f"LLM HTTP error: {exc}")
            break
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        messages.append(message)
        calls = parse_tool_calls(message)
        text = (message.get("content") or "").strip()
        if text:
            world.say(text[:2000])
        if not calls:
            break
        for call_id, name, args in calls:
            if used >= max_tools:
                result = "tool error: wake tool budget exhausted"
            else:
                world.tool_event("tool_call", name, args)
                result = tools.call(name, args)
                world.tool_event("tool_result", name, {"result": result[:800]})
                world.presence(
                    "waking",
                    last_action=name,
                    model=model,
                    place=place_from_tool(name, args) or None,
                )
                used += 1
                used_names.append(name)
                if _is_real_move(name, args):
                    did_real = True
                if name in {"write_file", "append_file"} and str(args.get("path") or "").startswith("/private/STATE"):
                    wrote_state = True
            messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
        if _chars(messages) > COMPACT_CHARS:
            _compact(world, tools, agent_id, base, api_key, model, temperature, timeout, messages)
    if not wrote_state:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        facts = (
            f"# STATE\n\nagent: {agent_id}\nlast_wake: {stamp}\n"
            f"tools: {', '.join(used_names) or 'none'}\n"
            f"workspace_write: {'yes' if did_real else 'no'}\n"
        )
        tools.call("write_file", {"path": "/private/STATE.md", "content": facts})


def _chars(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, default=str))


def _compact(
    world: WorldClient,
    tools: Toolbelt,
    agent_id: str,
    base: str,
    api_key: str,
    model: str,
    temperature: float,
    timeout: float,
    messages: list[dict[str, Any]],
) -> None:
    notes = json.dumps(messages[-12:], default=str)[:8000]
    try:
        data = chat_completion(
            base=base,
            api_key=api_key,
            model=model,
            messages=[
                {"role": "system", "content": COMPACT.format(NAME=agent_id)},
                {"role": "user", "content": notes},
            ],
            tools=[],
            temperature=min(temperature, 0.4),
            timeout=timeout,
        )
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except httpx.HTTPError:
        text = ""
    if text.strip():
        tools.call("write_file", {"path": "/private/STATE.md", "content": text.strip() + "\n"})
    world.tool_event("compact", "compact", {"dropped": max(0, len(messages) - 4)})
    keep_sys = messages[0]
    messages[:] = [keep_sys, {"role": "user", "content": wake_context(world, agent_id)}]


def one_wake(args: argparse.Namespace, world: WorldClient, tools: Toolbelt) -> None:
    dummy = args.dummy or env_bool("DUMMY")
    model = os.environ.get("OPENAI_MODEL", "mlx-community/Qwen3.6-35B-A3B-4bit")
    base = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
    if not dummy and not llm_available(base):
        dummy = True
        world.say("model server is down; walking the room without a brain this wake")
    world.presence(
        "waking",
        model=model if not dummy else "dummy",
        last_action="wake",
        location=os.environ.get("AGENT_ORIGIN", "local"),
    )
    if dummy:
        dummy_wake(world, tools, args.id)
    else:
        llm_wake(
            world,
            tools,
            agent_id=args.id,
            base=base,
            api_key=os.environ.get("OPENAI_API_KEY", "local"),
            model=model,
            temperature=float(os.environ.get("TEMPERATURE", args.temperature)),
            timeout=float(os.environ.get("LLM_TIMEOUT_SEC", "180")),
            max_tools=int(os.environ.get("MAX_TOOLS_PER_WAKE", args.max_tools)),
            enable_run=args.enable_run,
        )
    world.presence(
        "sleeping",
        model=model if not dummy else "dummy",
        last_action="sleep",
        location=os.environ.get("AGENT_ORIGIN", "local"),
    )


def loop(args: argparse.Namespace) -> None:
    world = WorldClient(args.world_url, args.token, args.id)
    cwd = "/workspace" if os.path.isdir("/workspace") else os.path.join(os.getcwd(), "habitat")
    tools = Toolbelt(world, fetch_proxy=args.fetch_proxy, enable_run=args.enable_run, workspace_cwd=cwd)
    heartbeat = float(os.environ.get("HEARTBEAT_SEC", args.heartbeat))
    try:
        while True:
            try:
                one_wake(args, world, tools)
            except Exception as exc:  # noqa: BLE001 — never crash the container
                traceback.print_exc()
                try:
                    world.log_error(f"wake crashed: {exc}")
                    world.presence("sleeping", last_action="crash")
                except httpx.HTTPError:
                    pass
            if args.once:
                return
            time.sleep(heartbeat)
    finally:
        tools.close()
        world.close()


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Antfarm fish")
    parser.add_argument("--id", default=os.environ.get("AGENT_ID", "alpha"))
    parser.add_argument("--dummy", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--world-url", default=os.environ.get("WORLD_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--token", default=os.environ.get("WORLD_TOKEN", ""))
    parser.add_argument("--fetch-proxy", default=os.environ.get("FETCH_PROXY", "http://127.0.0.1:8787"))
    parser.add_argument("--heartbeat", type=float, default=float(os.environ.get("HEARTBEAT_SEC", "5")))
    parser.add_argument("--max-tools", type=int, default=int(os.environ.get("MAX_TOOLS_PER_WAKE", "48")))
    parser.add_argument("--temperature", type=float, default=float(os.environ.get("TEMPERATURE", "0.8")))
    parser.add_argument("--enable-run", action="store_true", default=env_bool("ENABLE_RUN"))
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("WORLD_TOKEN missing")
    in_container = os.path.exists("/.dockerenv") or env_bool("IN_CONTAINER")
    if in_container:
        args.enable_run = True
    loop(args)


if __name__ == "__main__":
    main()
