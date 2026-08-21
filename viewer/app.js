const params = new URLSearchParams(location.search);
const TOKEN = params.get("token") || localStorage.getItem("WORLD_TOKEN") || "";
if (params.get("token")) localStorage.setItem("WORLD_TOKEN", params.get("token"));

const headers = () => ({
  Authorization: `Bearer ${TOKEN}`,
  Accept: "application/json",
});

const $ = (id) => document.getElementById(id);
const feedEl = $("feed");
const rosterEl = $("roster");
const boardEl = $("board");
const filesEl = $("files");
const mailEl = $("mail");
const linkEl = $("link-state");
const clockEl = $("clock");
const nowEl = $("now");

let afterId = 0;
let wsOk = false;
const hotFiles = new Map();
const hotWho = new Map();
const seenEventIds = new Set();
const lastAct = new Map();
let agentsCache = [];
let stickBottom = true;

const VERBS = {
  list_dir: "listing",
  read_file: "reading",
  write_file: "writing",
  append_file: "appending",
  grep: "searching",
  fetch_url: "fetching",
  web_search: "searching the web",
  journal: "journaling",
  run: "running a command",
  mail: "mailing",
  move_file: "moving",
  mkdir: "making a folder",
  recent_changes: "checking what changed",
};

function colorClass(id) {
  if (id === "alpha") return "who-alpha";
  if (id === "bravo") return "who-bravo";
  if (id === "world" || id === "proxy") return "";
  return "who-cloud";
}

function fmtTime(ts) {
  const d = new Date((ts || 0) * 1000);
  if (!ts) return "--:--";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function setLink(mode) {
  linkEl.textContent = mode;
  linkEl.className = "pill " + (mode === "live" ? "live" : mode === "poll" ? "poll" : "dim");
}

function prettyPath(p) {
  const s = String(p || "");
  return s.replace(/^\/workspace\//, "").replace(/^\/private\//, "private/");
}

function targetOf(name, payload) {
  const p = payload || {};
  if (p.src && p.dest) return `${prettyPath(p.src)} → ${prettyPath(p.dest)}`;
  if (p.path) return prettyPath(p.path);
  if (p.url) return p.url;
  if (p.query) return p.query;
  if (p.command) return p.command;
  if (p.pattern) return `${p.pattern} in ${prettyPath(p.path || "/workspace")}`;
  if (p.to) return p.to;
  if (typeof p.result === "string" && p.result.startsWith("tool error")) return p.result.slice(0, 120);
  return "";
}

function describe(ev) {
  const p = ev.payload || {};
  const name = ev.name || "";
  if (ev.kind === "say") {
    if (p.to) return { verb: "sent mail to", target: p.to, text: p.text || p.path || "" };
    return { verb: "said", target: "", text: p.text || "" };
  }
  if (ev.kind === "tool_call") {
    return { verb: VERBS[name] || name, target: targetOf(name, p), text: "" };
  }
  if (ev.kind === "file_change") {
    if (p.mkdir) return { verb: "made a folder", target: prettyPath(p.path || name), text: "" };
    return {
      verb: p.deleted ? "deleted" : "changed",
      target: prettyPath(p.path || name),
      text: "",
    };
  }
  if (ev.kind === "error") {
    return { verb: "hit an error", target: "", text: p.message || "" };
  }
  if (ev.kind === "tool_result" && typeof p.result === "string" && p.result.includes("tool error")) {
    return { verb: "failed", target: name, text: p.result.slice(0, 200) };
  }
  return null;
}

function shouldShow(ev) {
  if (!ev || ev.kind === "hello" || ev.kind === "ping" || ev.kind === "presence") return false;
  if (ev.kind === "tool_result") {
    const r = (ev.payload && ev.payload.result) || "";
    return typeof r === "string" && r.includes("tool error");
  }
  if (ev.kind === "file_change" && ev.payload && ev.payload.via === "scan") return false;
  if (ev.agent === "world" || ev.agent === "proxy") return ev.kind === "error";
  return ev.kind === "say" || ev.kind === "tool_call" || ev.kind === "file_change" || ev.kind === "error";
}

function rememberAct(ev) {
  if (!ev.agent || ev.agent === "world" || ev.agent === "proxy") return;
  const d = describe(ev);
  if (!d) return;
  if (ev.kind === "say") {
    lastAct.set(ev.agent, { line: d.text.split("\n")[0].slice(0, 140), ts: ev.ts, kind: "say" });
  } else {
    const line = [d.verb, d.target].filter(Boolean).join(" ");
    lastAct.set(ev.agent, { line, ts: ev.ts, kind: ev.kind });
  }
  renderNow();
}

async function api(path) {
  const r = await fetch(path, { headers: headers() });
  if (!r.ok) throw new Error(`${path} ${r.status}`);
  return r.json();
}

function ingest(event) {
  if (!event || event.kind === "hello" || event.kind === "ping") {
    if (event && event.kind === "hello" && event.agents) {
      agentsCache = event.agents;
      renderRoster(event.agents);
      renderNow();
    }
    return;
  }
  if (event.id && seenEventIds.has(event.id)) return;
  if (event.id) {
    seenEventIds.add(event.id);
    afterId = Math.max(afterId, event.id);
  }
  rememberAct(event);
  if (shouldShow(event)) appendFeed(event);
  if (event.kind === "file_change") {
    const p = (event.payload && event.payload.path) || event.name;
    if (p) {
      hotFiles.set(p, Date.now());
      hotWho.set(p, event.agent);
      refreshFiles();
    }
  }
  if (event.kind === "presence") refreshRoster();
  if (event.kind === "file_change" && String(event.name || "").includes("BOARD.md")) refreshBoard();
  if (event.kind === "say" && event.name === "mail") refreshMail();
  if (event.kind === "file_change" && String(event.payload?.path || "").includes("/mail/")) refreshMail();
}

function appendFeed(ev) {
  const d = describe(ev);
  if (!d) return;
  const who = ev.agent || "?";
  const row = document.createElement("div");
  row.className = `row ${ev.kind} ${colorClass(who)}`;
  const verb = d.verb ? `<span class="verb">${esc(d.verb)}</span>` : "";
  const target = d.target ? `<span class="target">${esc(d.target)}</span>` : "";
  const text = d.text ? `<div class="body">${esc(d.text)}</div>` : "";
  row.innerHTML = `<div class="head"><span class="t">${fmtTime(ev.ts)}</span><span class="who ${colorClass(who)}">${esc(who)}</span> ${verb} ${target}</div>${text}`;
  feedEl.appendChild(row);
  while (feedEl.children.length > 250) feedEl.removeChild(feedEl.firstChild);
  if (stickBottom) feedEl.parentElement.scrollTop = feedEl.parentElement.scrollHeight;
}

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function awakeLabel(st) {
  if (st === "waking") return "awake";
  if (st === "sleeping") return "asleep";
  return st || "offline";
}

function actLine(id, fallback) {
  const a = lastAct.get(id);
  if (a && a.line) return a.line;
  if (fallback && fallback !== "sleep" && fallback !== "wake") {
    return VERBS[fallback] ? VERBS[fallback] : fallback;
  }
  return "idle";
}

function renderNow() {
  const ids = agentsCache.length ? agentsCache.map((a) => a) : [{ id: "alpha" }, { id: "bravo" }];
  nowEl.innerHTML = ids
    .slice(0, 4)
    .map((a) => {
      const st = a.status || "offline";
      return `<div class="slot">
        <div class="who ${colorClass(a.id)}">${esc(a.id)} <span class="state">${esc(awakeLabel(st))}${a.place ? " · " + esc(a.place) : ""}</span></div>
        <div class="doing">${esc(actLine(a.id, a.last_action))}</div>
      </div>`;
    })
    .join("");
}

function renderRoster(agents) {
  agentsCache = agents || [];
  rosterEl.innerHTML = "";
  for (const a of agentsCache) {
    const el = document.createElement("div");
    el.className = "fish";
    const st = a.status || "offline";
    const model = (a.model || "").replace(/^mlx-community\//, "").replace(/:.*$/, "");
    el.innerHTML = `
      <div class="name ${colorClass(a.id)}"><span class="dot ${esc(st)}"></span>${esc(a.id)}</div>
      <div class="doing">${esc(actLine(a.id, a.last_action))}</div>
      <div class="meta">${esc(awakeLabel(st))} · at ${esc(a.place || "room")} · ${esc(a.origin || "local")}<br>${esc(model)}</div>
    `;
    rosterEl.appendChild(el);
  }
  renderNow();
}

async function refreshRoster() {
  try {
    const data = await api("/api/agents");
    renderRoster(data.agents);
  } catch {
    /* keep last */
  }
}

async function refreshBoard() {
  try {
    const data = await api("/api/fs/read?path=/workspace/BOARD.md");
    boardEl.textContent = data.content || "(empty board)";
  } catch {
    boardEl.textContent = "(board unreadable)";
  }
}

async function refreshFiles() {
  try {
    const data = await api("/api/fs/list?path=/workspace&deep=true");
    const files = (data.entries || []).filter((e) => e.type === "file");
    files.sort((a, b) => a.path.localeCompare(b.path));
    const now = Date.now();
    for (const [p, t] of [...hotFiles.entries()]) {
      if (now - t > 16000) {
        hotFiles.delete(p);
        hotWho.delete(p);
      }
    }
    const lines = [];
    let lastDir = "";
    for (const f of files) {
      const dir = f.path.split("/").slice(0, -1).join("/") || "/workspace";
      if (dir !== lastDir) {
        lines.push(`<div class="dir">${esc(prettyPath(dir) || "/workspace")}</div>`);
        lastDir = dir;
      }
      const hot = hotFiles.has(f.path);
      const who = hotWho.get(f.path);
      const mark = hot ? ` <span class="hot">${esc(who || "changed")}</span>` : "";
      lines.push(`<div class="file">${esc(f.name)}${mark}</div>`);
    }
    filesEl.innerHTML = lines.join("") || "<div class='dir'>empty</div>";
  } catch {
    filesEl.textContent = "(files unreadable)";
  }
}

async function refreshMail() {
  try {
    const [a, b] = await Promise.all([api("/api/mail?agent=alpha"), api("/api/mail?agent=bravo")]);
    const letters = [...(a.letters || []), ...(b.letters || [])]
      .sort((x, y) => (y.mtime || 0) - (x.mtime || 0))
      .slice(0, 8);
    mailEl.innerHTML =
      letters
        .map((l) => `<div class="letter"><b>${esc(l.name)}</b>\n${esc(l.excerpt || "")}</div>`)
        .join("") || "<div class='letter'>no letters yet</div>";
  } catch {
    mailEl.textContent = "(mail unreadable)";
  }
}

async function poll() {
  try {
    const data = await api(`/api/events?after=${afterId}`);
    for (const ev of data.events || []) ingest(ev);
    if (!wsOk) setLink("poll");
  } catch {
    if (!wsOk) setLink("offline");
  }
}

function connectWs() {
  if (!TOKEN) {
    setLink("no token");
    return;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => {
    wsOk = true;
    setLink("live");
  };
  ws.onmessage = (m) => {
    try {
      ingest(JSON.parse(m.data));
    } catch {
      /* ignore */
    }
  };
  ws.onclose = () => {
    wsOk = false;
    setLink("poll");
    setTimeout(connectWs, 2000);
  };
  ws.onerror = () => ws.close();
}

feedEl.parentElement.addEventListener("scroll", () => {
  const pane = feedEl.parentElement;
  stickBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80;
});

async function boot() {
  if (!TOKEN) {
    linkEl.textContent = "add ?token=";
    linkEl.className = "pill dim";
  }
  await refreshRoster();
  await refreshBoard();
  await refreshFiles();
  await refreshMail();
  await poll();
  connectWs();
  setInterval(poll, 1000);
  setInterval(refreshBoard, 4000);
  setInterval(refreshFiles, 4000);
  setInterval(refreshMail, 5000);
  setInterval(refreshRoster, 3000);
  setInterval(() => {
    clockEl.textContent = new Date().toLocaleTimeString();
  }, 1000);
}

boot();
