"""Outbound GET proxy: size/time limits, cache, HTML stripped to text."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from world.config import Settings, load_dotenv

load_dotenv()

MAX_BYTES = int(os.environ.get("FETCH_MAX_BYTES", str(10_000_000)))
TIMEOUT = float(os.environ.get("FETCH_TIMEOUT_SEC", "60"))
MAX_REDIRECTS = 5
SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe"}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.hrefs: list[tuple[str, str]] = []
        self._open_a: str | None = None
        self._a_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._open_a = href
            self._a_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
            return
        if tag == "a" and self._open_a is not None:
            label = " ".join(self._a_text).strip()
            self.hrefs.append((label, self._open_a))
            self._open_a = None
            self._a_text = []

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        self.parts.append(text + " ")
        if self._open_a is not None:
            self._a_text.append(text)


def html_to_text(html: str) -> str:
    parser = TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    text = re.sub(r"\n{3,}", "\n\n", "".join(parser.parts))
    return text.strip()[:MAX_BYTES]


def looks_html(content_type: str, body: bytes) -> bool:
    if "html" in content_type.lower():
        return True
    start = body[:200].lstrip().lower()
    return start.startswith(b"<!doctype html") or start.startswith(b"<html")


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def load_cache(root: Path, url: str) -> dict | None:
    path = root / f"{cache_key(url)}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(data.get("ts") or 0) > 3600:
        return None
    return data


def save_cache(root: Path, url: str, payload: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{cache_key(url)}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def fetch_limited(url: str) -> dict:
    if urlparse(url).scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="only http(s)")
    current = url
    with httpx.Client(follow_redirects=False, timeout=TIMEOUT, headers={"User-Agent": "Antfarm/0.1"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            r = client.get(current)
            if r.status_code in {301, 302, 303, 307, 308}:
                loc = r.headers.get("location")
                if not loc:
                    raise HTTPException(status_code=502, detail="redirect without location")
                current = urljoin(current, loc)
                continue
            body = r.content[: MAX_BYTES + 1]
            truncated = len(body) > MAX_BYTES
            body = body[:MAX_BYTES]
            ctype = r.headers.get("content-type", "")
            if looks_html(ctype, body):
                text = html_to_text(body.decode("utf-8", errors="replace"))
            else:
                text = body.decode("utf-8", errors="replace")
            return {
                "url": str(r.url),
                "requested": url,
                "status": r.status_code,
                "content_type": ctype,
                "truncated": truncated,
                "text": text,
                "ts": time.time(),
            }
    raise HTTPException(status_code=508, detail="too many redirects")


def parse_ddg(html: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    # DuckDuckGo html endpoint: result links in .result__a and snippets .result__snippet
    for block in re.split(r'<div class="result', html)[1:]:
        href_m = re.search(r'class="result__a"[^>]*href="([^"]+)"', block)
        title_m = re.search(r'class="result__a"[^>]*>(.*?)</a>', block, re.S)
        snip_m = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)', block, re.S)
        if not href_m:
            continue
        href = re.sub(r"&amp;", "&", href_m.group(1))
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                from urllib.parse import unquote

                href = unquote(m.group(1))
        title = re.sub(r"<[^>]+>", "", title_m.group(1) if title_m else "").strip()
        snippet = re.sub(r"<[^>]+>", "", snip_m.group(1) if snip_m else "").strip()
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= 8:
            break
    if results:
        return results
    parser = TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return results
    for label, href in parser.hrefs:
        if not href.startswith("http"):
            continue
        if "duckduckgo.com" in href:
            continue
        results.append({"title": label, "url": href, "snippet": ""})
        if len(results) >= 8:
            break
    return results


def log_fetch(kind: str, payload: dict) -> None:
    settings = Settings()
    log_dir = settings.repo_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": time.time(), "kind": kind, **payload}, default=str)
    with (log_dir / "proxy.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    token = os.environ.get("WORLD_TOKEN") or settings.world_token
    world_url = os.environ.get("WORLD_URL", f"http://{settings.host}:{settings.port}")
    if not token:
        return
    try:
        httpx.post(
            f"{world_url.rstrip('/')}/api/tool",
            headers={"Authorization": f"Bearer {token}", "X-Agent-Id": "proxy"},
            json={"agent": "proxy", "kind": "tool_result", "name": kind, "payload": payload},
            timeout=2.0,
        )
    except httpx.HTTPError:
        pass


app = FastAPI(title="Antfarm fetch-proxy")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/fetch")
def fetch(url: str = Query(..., min_length=8)) -> JSONResponse:
    settings = Settings()
    cached = load_cache(settings.webcache_dir, url)
    if cached:
        log_fetch("fetch_url", {"url": url, "cache": True, "status": cached.get("status")})
        return JSONResponse(cached)
    try:
        payload = fetch_limited(url)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="upstream timeout") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    save_cache(settings.webcache_dir, url, payload)
    log_fetch("fetch_url", {"url": url, "cache": False, "status": payload.get("status")})
    return JSONResponse(payload)


@app.get("/search")
def search(q: str = Query(..., min_length=1)) -> JSONResponse:
    try:
        results = search_ddg(q)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="search timeout") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log_fetch("web_search", {"query": q, "n": len(results)})
    return JSONResponse({"query": q, "results": results})


def search_ddg(q: str) -> list[dict[str, str]]:
    from urllib.parse import quote_plus

    url = "https://html.duckduckgo.com/html/?q=" + quote_plus(q)
    with httpx.Client(follow_redirects=True, timeout=TIMEOUT, headers={"User-Agent": "Antfarm/0.1"}) as client:
        r = client.get(url)
        html = r.content[:MAX_BYTES].decode("utf-8", errors="replace")
    return parse_ddg(html)


def main() -> None:
    load_dotenv()
    host = os.environ.get("PROXY_HOST", "127.0.0.1")
    port = int(os.environ.get("PROXY_PORT", "8787"))
    uvicorn.run("proxy.fetch_proxy:app", host=host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
