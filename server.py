from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone
import json
import os
import socket
import time
import uuid


HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PORT = int(os.environ.get("PANEL_PORT", "8080"))
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "dev-agent-token")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-admin-token")
CLIENT_TIMEOUT_SECONDS = 35

clients = {}
tasks = {}
events = []
metrics = {}
running = {}
cancel_requests = {}


COMMANDS = {
    "hostname": "Show computer name",
    "whoami": "Show current user",
    "cwd": "Show client working directory",
    "date": "Show client local date and time",
    "list_current_dir": "List files in client working directory",
    "stats": "Show system stats (CPU/RAM/disk/temp if available)",
    "mem": "Show memory usage",
    "cpu": "Show CPU usage (best-effort)",
    "cpu_temp": "Show CPU temperature (best-effort)",
    "disk": "Show disk usage (argument: path like C:\\ or /)",
    "top_mem": "Top processes by memory (argument: N, default 10)",
    "ipconfig": "Show Windows network configuration",
    "echo": "Print the provided text",
    "custom_shell": "Run any shell command from the command input",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_ipv4_addrs():
    addrs = set()
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                addrs.add(ip)
    except Exception:
        pass

    # Best-effort way to discover the default interface address without sending traffic.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                addrs.add(ip)
        finally:
            s.close()
    except Exception:
        pass

    return sorted(addrs)


def add_event(kind, client_id, message, output=None, task_id=""):
    if kind == "live" and task_id:
        for event in reversed(events):
            if event.get("kind") == "live" and event.get("task_id") == task_id:
                combined = (event.get("output", "") + (output or ""))[-16000:]
                event["output"] = combined
                event["at"] = now_iso()
                return
    events.append(
        {
            "at": now_iso(),
            "kind": kind,
            "client_id": client_id,
            "message": message,
            "output": output or "",
            "task_id": task_id,
        }
    )
    del events[:-80]


def public_state():
    now = time.time()
    return {
        "clients": [
            {
                **client,
                "online": now - client["last_seen_ts"] <= CLIENT_TIMEOUT_SECONDS,
                "pending": len(tasks.get(client_id, [])),
                "metrics": (metrics.get(client_id) or [])[-1] if metrics.get(client_id) else None,
                "running_task_id": running.get(client_id),
            }
            for client_id, client in sorted(clients.items())
        ],
        "commands": COMMANDS,
        "events": list(reversed(events[-40:])),
    }


PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Safe Client Panel</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --ink: #182033;
      --muted: #687086;
      --line: #dde3ee;
      --accent: #166bff;
      --accent-2: #1f9d68;
      --danger: #c93535;
      --shadow: 0 18px 55px rgba(28, 39, 68, .12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 15% 0%, rgba(22, 107, 255, .14), transparent 28%),
        linear-gradient(145deg, #f7fbff 0%, var(--bg) 52%, #eef4f3 100%);
      min-height: 100vh;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 26px clamp(18px, 4vw, 48px) 12px;
    }
    h1 { margin: 0; font-size: clamp(24px, 3vw, 38px); letter-spacing: 0; }
    .subtitle { color: var(--muted); margin-top: 6px; max-width: 760px; line-height: 1.45; }
    .status-pill {
      white-space: nowrap;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 10px 14px;
      background: rgba(255,255,255,.72);
      font-weight: 700;
      box-shadow: 0 10px 30px rgba(30, 45, 80, .08);
    }
    main {
      display: grid;
      grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
      gap: 18px;
      padding: 14px clamp(18px, 4vw, 48px) 38px;
    }
    section {
      background: rgba(255,255,255,.88);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
    }
    .section-head {
      padding: 18px 18px 12px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    h2 { margin: 0; font-size: 16px; }
    .clients, .events { padding: 12px; display: grid; gap: 10px; }
    .client {
      width: 100%;
      text-align: left;
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 8px;
      padding: 13px;
      cursor: pointer;
      transition: border-color .15s, transform .15s, box-shadow .15s;
    }
    .client:hover, .client.selected {
      border-color: rgba(22,107,255,.45);
      box-shadow: 0 10px 24px rgba(22,107,255,.10);
      transform: translateY(-1px);
    }
    .client-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-weight: 800; }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: #aab3c4; flex: 0 0 auto; }
    .dot.online { background: var(--accent-2); box-shadow: 0 0 0 4px rgba(31,157,104,.13); }
    .meta { margin-top: 8px; color: var(--muted); font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(160px, 1fr) minmax(160px, 1fr) auto auto auto;
      gap: 10px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
    }
    .toolbar .ghost {
      background: rgba(255,255,255,.85);
      font-weight: 800;
      cursor: pointer;
    }
    .toolbar .ghost:hover {
      border-color: rgba(22,107,255,.35);
      box-shadow: 0 10px 22px rgba(22,107,255,.08);
      transform: translateY(-1px);
    }
    .toolbar .danger {
      background: rgba(201,53,53,.08);
      border-color: rgba(201,53,53,.25);
      color: #7c1212;
      font-weight: 900;
      cursor: pointer;
    }
    .toolbar .danger:disabled {
      opacity: .5;
      cursor: not-allowed;
    }
    .toolbar .danger:hover:enabled {
      border-color: rgba(201,53,53,.42);
      box-shadow: 0 10px 22px rgba(201,53,53,.10);
      transform: translateY(-1px);
    }
    .charts {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      padding: 14px 18px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(255,255,255,.92), rgba(250,252,255,.9));
    }
    .chart {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      padding: 10px 10px 8px;
      min-width: 0;
    }
    .chart-title {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 10px;
      font-weight: 800;
      font-size: 13px;
      color: var(--ink);
      margin-bottom: 8px;
    }
    .chart-sub {
      font-weight: 700;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    canvas {
      width: 100%;
      height: 96px;
      display: block;
    }
    select, input, button {
      height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      min-width: 0;
    }
    select, input { padding: 0 12px; }
    button.primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
      padding: 0 18px;
      font-weight: 800;
      cursor: pointer;
    }
    button.primary:disabled { opacity: .5; cursor: not-allowed; }
    .event {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    .event-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 11px 13px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      color: var(--muted);
    }
    .event strong { color: var(--ink); }
    pre {
      margin: 0;
      padding: 13px;
      overflow: auto;
      white-space: pre-wrap;
      line-height: 1.45;
      background: #101827;
      color: #ecf3ff;
      min-height: 42px;
    }
    .empty { padding: 28px 18px; color: var(--muted); text-align: center; }
    @media (max-width: 820px) {
      header { align-items: flex-start; flex-direction: column; }
      main { grid-template-columns: 1fr; }
      .toolbar { grid-template-columns: 1fr; }
      button.primary { width: 100%; }
    }
    @media (max-width: 920px) {
      .charts { grid-template-columns: 1fr; }
      canvas { height: 110px; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Safe Client Panel</h1>
      <div class="subtitle">Consent-based client control with token authentication, allowlist commands, and live output streaming for custom shell commands.</div>
    </div>
    <div class="status-pill" id="summary">Loading...</div>
  </header>
  <main>
    <section>
      <div class="section-head">
        <h2>Clients</h2>
        <button id="saveToken">Set token</button>
      </div>
      <div class="clients" id="clients"></div>
    </section>
    <section>
      <div class="section-head">
        <h2>Command Center</h2>
        <span class="meta" id="selectedName">No client selected</span>
      </div>
      <div class="meta" style="padding: 0 18px 10px;">
        Path: <span id="cwdNow">-</span>
      </div>
      <div class="toolbar">
        <input id="command" list="commandList" placeholder="Type command, e.g. ipconfig or dir /b">
        <datalist id="commandList"></datalist>
        <input id="argument" placeholder="Optional argument (for named commands)">
        <button class="danger" id="stop" title="Stop running command" disabled>Stop</button>
        <button class="ghost" id="clear" title="Clear command results">Clear</button>
        <button class="primary" id="send" disabled>Send</button>
      </div>
      <div class="charts">
        <div class="chart">
          <div class="chart-title"><span>CPU</span><span class="chart-sub" id="cpuNow">-</span></div>
          <canvas id="cpuChart" width="600" height="160"></canvas>
        </div>
        <div class="chart">
          <div class="chart-title"><span>RAM</span><span class="chart-sub" id="ramNow">-</span></div>
          <canvas id="ramChart" width="600" height="160"></canvas>
        </div>
        <div class="chart">
          <div class="chart-title"><span>Temp</span><span class="chart-sub" id="tempNow">-</span></div>
          <canvas id="tempChart" width="600" height="160"></canvas>
        </div>
      </div>
      <div class="events" id="events"></div>
    </section>
  </main>
  <script>
    let selectedClient = null;
    let knownCommands = {};
    let adminToken = localStorage.getItem("adminToken") || "";
    let runningByClient = {};

    const clientsEl = document.getElementById("clients");
    const eventsEl = document.getElementById("events");
    const commandEl = document.getElementById("command");
    const commandListEl = document.getElementById("commandList");
    const argumentEl = document.getElementById("argument");
    const sendEl = document.getElementById("send");
    const stopEl = document.getElementById("stop");
    const clearEl = document.getElementById("clear");
    const selectedNameEl = document.getElementById("selectedName");
    const summaryEl = document.getElementById("summary");
    const cpuNowEl = document.getElementById("cpuNow");
    const ramNowEl = document.getElementById("ramNow");
    const tempNowEl = document.getElementById("tempNow");
    const cwdNowEl = document.getElementById("cwdNow");
    const cpuCanvas = document.getElementById("cpuChart");
    const ramCanvas = document.getElementById("ramChart");
    const tempCanvas = document.getElementById("tempChart");

    document.getElementById("saveToken").addEventListener("click", () => {
      const value = prompt("Admin token", adminToken);
      if (value !== null) {
        adminToken = value.trim();
        localStorage.setItem("adminToken", adminToken);
      }
    });

    sendEl.addEventListener("click", async () => {
      if (!selectedClient) return;
      const parsed = parseCommand(commandEl.value, argumentEl.value);
      if (!parsed.action || (parsed.action === "custom_shell" && !parsed.argument)) {
        alert("Type a command first");
        return;
      }
      const res = await fetch("/api/queue", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Admin-Token": adminToken },
        body: JSON.stringify({
          client_id: selectedClient,
          action: parsed.action,
          argument: parsed.argument
        })
      });
      if (!res.ok) alert(await res.text());
      commandEl.value = "";
      argumentEl.value = "";
      await loadState();
    });

    clearEl.addEventListener("click", async () => {
      if (!adminToken) {
        alert("Set admin token first");
        return;
      }
      const res = await fetch("/api/clear_events", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Admin-Token": adminToken },
        body: JSON.stringify({ client_id: selectedClient || "" })
      });
      if (!res.ok) {
        alert(await res.text());
        return;
      }
      await loadState();
    });

    stopEl.addEventListener("click", async () => {
      if (!adminToken) {
        alert("Set admin token first");
        return;
      }
      if (!selectedClient) return;
      const taskId = runningByClient[selectedClient];
      if (!taskId) return;
      const res = await fetch("/api/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Admin-Token": adminToken },
        body: JSON.stringify({ client_id: selectedClient, task_id: taskId })
      });
      if (!res.ok) {
        alert(await res.text());
        return;
      }
      await loadState();
    });

    commandEl.addEventListener("keydown", event => {
      if (event.key === "Enter" && !sendEl.disabled) sendEl.click();
    });

    argumentEl.addEventListener("keydown", event => {
      if (event.key === "Enter" && !sendEl.disabled) sendEl.click();
    });

    function parseCommand(commandText, argumentText) {
      const trimmed = commandText.trim();
      const extraArgument = argumentText.trim();
      if (!trimmed && !extraArgument) return { action: "", argument: "" };
      const firstSpace = trimmed.search(/\s/);
      const action = firstSpace === -1 ? trimmed : trimmed.slice(0, firstSpace);
      const inlineArgument = firstSpace === -1 ? "" : trimmed.slice(firstSpace).trim();
      if (knownCommands[action] && action !== "custom_shell") {
        return { action, argument: extraArgument || inlineArgument };
      }
      const shellCommand = [trimmed, extraArgument].filter(Boolean).join(" ").trim();
      return { action: "custom_shell", argument: shellCommand };
    }

    function escapeHtml(text) {
      return String(text).replace(/[&<>"']/g, char => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }[char]));
    }

    function renderInlineMetrics(sample) {
      if (!sample) return "Metrics: -";
      const parts = [];
      if (typeof sample.cpu === "number") parts.push(`CPU ${sample.cpu.toFixed(0)}%`);
      if (typeof sample.ram_used === "number" && typeof sample.ram_total === "number" && sample.ram_total > 0) {
        const pct = (sample.ram_used / sample.ram_total) * 100;
        parts.push(`RAM ${pct.toFixed(0)}%`);
      }
      if (typeof sample.temp === "number") parts.push(`${sample.temp.toFixed(1)}C`);
      if (typeof sample.cwd === "string" && sample.cwd) parts.push(sample.cwd);
      return parts.length ? `Metrics: ${parts.join(" В· ")}` : "Metrics: -";
    }

    function renderClients(clients) {
      summaryEl.textContent = `${clients.filter(c => c.online).length} online / ${clients.length} total`;
      runningByClient = Object.fromEntries(clients.map(c => [c.id, c.running_task_id || ""]));
      if (!clients.length) {
        clientsEl.innerHTML = `<div class="empty">Start client.py on another terminal to connect a client.</div>`;
        selectedClient = null;
        sendEl.disabled = true;
        stopEl.disabled = true;
        selectedNameEl.textContent = "No client selected";
        cpuNowEl.textContent = "-";
        ramNowEl.textContent = "-";
        tempNowEl.textContent = "-";
        cwdNowEl.textContent = "-";
        return;
      }
      if (!selectedClient || !clients.some(c => c.id === selectedClient)) selectedClient = clients[0].id;
      clientsEl.innerHTML = clients.map(c => `
        <button class="client ${c.id === selectedClient ? "selected" : ""}" data-id="${escapeHtml(c.id)}">
          <div class="client-title">
            <span>${escapeHtml(c.name)}</span>
            <span class="dot ${c.online ? "online" : ""}"></span>
          </div>
          <div class="meta">${escapeHtml(c.id)}<br>Last seen: ${escapeHtml(c.last_seen)} · Pending: ${c.pending}<br>${renderInlineMetrics(c.metrics)}</div>
        </button>
      `).join("");
      document.querySelectorAll(".client").forEach(btn => {
        btn.addEventListener("click", () => {
          selectedClient = btn.dataset.id;
          renderClients(clients);
          loadMetrics();
        });
      });
      const selected = clients.find(c => c.id === selectedClient);
      selectedNameEl.textContent = selected ? selected.name : "No client selected";
      sendEl.disabled = !selectedClient;
      stopEl.disabled = !(selectedClient && runningByClient[selectedClient]);
    }

    function drawSeries(canvas, samples, valueFn, opts) {
      const ctx = canvas.getContext("2d");
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);

      const values = samples.map(valueFn).filter(v => typeof v === "number" && isFinite(v));
      if (!values.length) {
        ctx.fillStyle = "rgba(104,112,134,.95)";
        ctx.font = "bold 20px Inter, Segoe UI, Arial, sans-serif";
        ctx.fillText("No data", 14, 36);
        ctx.font = "14px Inter, Segoe UI, Arial, sans-serif";
        ctx.fillText("Client may not support this metric.", 14, 60);
        return;
      }

      const min = (opts && typeof opts.min === "number") ? opts.min : Math.min(...values);
      const max = (opts && typeof opts.max === "number") ? opts.max : Math.max(...values);
      const pad = 12;
      const x0 = pad, y0 = pad, x1 = w - pad, y1 = h - pad;
      const range = Math.max(1e-6, (max - min));

      ctx.strokeStyle = "rgba(221,227,238,.95)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i <= 4; i++) {
        const y = y0 + ((y1 - y0) * i / 4);
        ctx.moveTo(x0, y);
        ctx.lineTo(x1, y);
      }
      ctx.stroke();

      ctx.strokeStyle = (opts && opts.color) || "rgba(22,107,255,.95)";
      ctx.lineWidth = 2.2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      const n = samples.length;
      let started = false;
      for (let i = 0; i < n; i++) {
        const v = valueFn(samples[i]);
        if (typeof v !== "number" || !isFinite(v)) continue;
        const x = x0 + (x1 - x0) * (i / Math.max(1, n - 1));
        const y = y1 - (y1 - y0) * ((v - min) / range);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
    }

    async function loadMetrics() {
      if (!selectedClient || !adminToken) return;
      const res = await fetch(`/api/metrics?client_id=${encodeURIComponent(selectedClient)}`, { headers: { "X-Admin-Token": adminToken } });
      if (!res.ok) return;
      const data = await res.json();
      const samples = (data.samples || []).slice(-120);
      const last = samples.length ? samples[samples.length - 1] : null;

      cpuNowEl.textContent = last && typeof last.cpu === "number" ? `${last.cpu.toFixed(0)}%` : "-";
      if (last && typeof last.ram_used === "number" && typeof last.ram_total === "number" && last.ram_total > 0) {
        const pct = (last.ram_used / last.ram_total) * 100;
        ramNowEl.textContent = `${pct.toFixed(0)}%`;
      } else {
        ramNowEl.textContent = "-";
      }
      tempNowEl.textContent = last && typeof last.temp === "number" ? `${last.temp.toFixed(1)}C` : "-";
      cwdNowEl.textContent = last && typeof last.cwd === "string" && last.cwd ? last.cwd : "-";

      drawSeries(cpuCanvas, samples, s => s.cpu, { min: 0, max: 100, color: "rgba(22,107,255,.95)" });
      drawSeries(ramCanvas, samples, s => (typeof s.ram_used === "number" && typeof s.ram_total === "number" && s.ram_total > 0) ? (s.ram_used / s.ram_total) * 100 : null, { min: 0, max: 100, color: "rgba(31,157,104,.95)" });
      drawSeries(tempCanvas, samples, s => s.temp, { color: "rgba(201,53,53,.95)" });
    }

    function renderCommands(commands) {
      knownCommands = commands || {};
      commandListEl.innerHTML = Object.entries(commands)
        .filter(([key]) => key !== "custom_shell")
        .map(([key, label]) => `<option value="${escapeHtml(key)}" label="${escapeHtml(label)}"></option>`)
        .join("");
    }

    function renderEvents(events) {
      if (!events.length) {
        eventsEl.innerHTML = `<div class="empty">Command results will appear here.</div>`;
        return;
      }
      eventsEl.innerHTML = events.map(e => `
        <article class="event">
          <div class="event-top">
            <span><strong>${escapeHtml(e.kind)}</strong> · ${escapeHtml(e.message)}</span>
            <span>${escapeHtml(e.at)}</span>
          </div>
          <pre>${escapeHtml(e.output || "No output")}</pre>
        </article>
      `).join("");
    }

    async function loadState() {
      const res = await fetch("/api/state", { headers: { "X-Admin-Token": adminToken } });
      if (!res.ok) {
        summaryEl.textContent = "Set admin token";
        stopEl.disabled = true;
        return;
      }
      const state = await res.json();
      renderCommands(state.commands);
      renderClients(state.clients);
      renderEvents(state.events);
      loadMetrics();
    }

    loadState();
    setInterval(loadState, 900);
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "SafePanel/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status, text, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def admin_ok(self):
        return self.headers.get("X-Admin-Token") == ADMIN_TOKEN

    def agent_ok(self):
        return self.headers.get("X-Agent-Token") == AGENT_TOKEN

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_text(200, PAGE, "text/html; charset=utf-8")
            return

        if parsed.path == "/api/state":
            if not self.admin_ok():
                self.send_text(401, "Invalid admin token")
                return
            self.send_json(200, public_state())
            return

        if parsed.path == "/api/metrics":
            if not self.admin_ok():
                self.send_text(401, "Invalid admin token")
                return
            query = parse_qs(parsed.query)
            client_id = query.get("client_id", [""])[0][:80]
            if not client_id:
                self.send_text(400, "Missing client_id")
                return
            self.send_json(200, {"client_id": client_id, "samples": metrics.get(client_id, [])})
            return

        if parsed.path == "/api/cancelled":
            if not self.agent_ok():
                self.send_text(401, "Invalid agent token")
                return
            query = parse_qs(parsed.query)
            client_id = query.get("client_id", [""])[0][:80]
            task_id = query.get("task_id", [""])[0][:80]
            if not client_id or not task_id:
                self.send_text(400, "Missing client_id or task_id")
                return
            key = (client_id, task_id)
            ts = cancel_requests.get(key)
            if ts is None:
                self.send_json(200, {"cancel": False})
                return
            # Expire old requests to avoid unbounded growth.
            if time.time() - ts > 300:
                cancel_requests.pop(key, None)
                self.send_json(200, {"cancel": False})
                return
            self.send_json(200, {"cancel": True})
            return

        if parsed.path == "/api/poll":
            if not self.agent_ok():
                self.send_text(401, "Invalid agent token")
                return
            query = parse_qs(parsed.query)
            client_id = query.get("client_id", [""])[0][:80]
            name = query.get("name", ["unknown"])[0][:80]
            if not client_id:
                self.send_text(400, "Missing client_id")
                return
            clients[client_id] = {
                "id": client_id,
                "name": name,
                "last_seen": now_iso(),
                "last_seen_ts": time.time(),
            }
            queue = tasks.setdefault(client_id, [])
            task = queue.pop(0) if queue else None
            self.send_json(200, {"task": task})
            return

        self.send_text(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/queue":
            if not self.admin_ok():
                self.send_text(401, "Invalid admin token")
                return
            data = self.read_json()
            client_id = str(data.get("client_id", ""))[:80]
            action = str(data.get("action", ""))
            argument_limit = 1500 if action == "custom_shell" else 500
            argument = str(data.get("argument", ""))[:argument_limit]
            if client_id not in clients:
                self.send_text(404, "Unknown client")
                return
            if action not in COMMANDS:
                self.send_text(400, "Command is not allowed")
                return
            if action == "custom_shell" and not argument.strip():
                self.send_text(400, "Custom command cannot be empty")
                return
            task = {
                "id": uuid.uuid4().hex,
                "action": action,
                "argument": argument,
                "created_at": now_iso(),
            }
            tasks.setdefault(client_id, []).append(task)
            add_event("queued", client_id, action, task_id=task["id"])
            if action == "custom_shell" and client_id not in running:
                # Mark as running-ish so the UI can offer Stop immediately.
                running[client_id] = task["id"]
            self.send_json(200, {"ok": True, "task": task})
            return

        if parsed.path == "/api/cancel":
            if not self.admin_ok():
                self.send_text(401, "Invalid admin token")
                return
            data = self.read_json()
            client_id = str(data.get("client_id", ""))[:80].strip()
            task_id = str(data.get("task_id", ""))[:80].strip()
            if not client_id or not task_id:
                self.send_text(400, "Missing client_id or task_id")
                return
            if client_id not in clients:
                self.send_text(404, "Unknown client")
                return
            # If the task is still queued, remove it so it never starts.
            queue = tasks.get(client_id) or []
            tasks[client_id] = [t for t in queue if str(t.get("id", "")) != task_id]
            cancel_requests[(client_id, task_id)] = time.time()
            add_event("cancel", client_id, "cancel", task_id=task_id)
            self.send_json(200, {"ok": True})
            return

        if parsed.path == "/api/clear_events":
            if not self.admin_ok():
                self.send_text(401, "Invalid admin token")
                return
            data = self.read_json()
            client_id = str(data.get("client_id", ""))[:80].strip()
            if client_id:
                remaining = [e for e in events if e.get("client_id") != client_id]
                events[:] = remaining
            else:
                events[:] = []
            self.send_json(200, {"ok": True})
            return

        if parsed.path == "/api/metrics":
            if not self.agent_ok():
                self.send_text(401, "Invalid agent token")
                return
            data = self.read_json()
            client_id = str(data.get("client_id", ""))[:80]
            sample = data.get("sample") or {}
            if not client_id:
                self.send_text(400, "Missing client_id")
                return
            if client_id not in clients:
                self.send_text(404, "Unknown client")
                return
            try:
                at = str(sample.get("at", ""))[:40] or now_iso()
                cpu = sample.get("cpu")
                ram_used = sample.get("ram_used")
                ram_total = sample.get("ram_total")
                cwd = sample.get("cwd")
                temp = sample.get("temp")
                disk_used = sample.get("disk_used")
                disk_total = sample.get("disk_total")
                normalized = {
                    "at": at,
                    "cpu": float(cpu) if cpu is not None else None,
                    "ram_used": int(ram_used) if ram_used is not None else None,
                    "ram_total": int(ram_total) if ram_total is not None else None,
                    "cwd": (str(cwd)[:260] if cwd is not None else None),
                    "temp": float(temp) if temp is not None else None,
                    "disk_used": int(disk_used) if disk_used is not None else None,
                    "disk_total": int(disk_total) if disk_total is not None else None,
                }
            except Exception:
                self.send_text(400, "Invalid sample")
                return
            series = metrics.setdefault(client_id, [])
            series.append(normalized)
            del series[:-180]
            self.send_json(200, {"ok": True})
            return

        if parsed.path == "/api/progress":
            if not self.agent_ok():
                self.send_text(401, "Invalid agent token")
                return
            data = self.read_json()
            client_id = str(data.get("client_id", ""))[:80]
            task_id = str(data.get("task_id", ""))[:80]
            action = str(data.get("action", "unknown"))[:80]
            chunk = str(data.get("chunk", ""))[:4000]
            if chunk:
                add_event("live", client_id, action, chunk, task_id=task_id)
                if action == "custom_shell" and task_id:
                    running[client_id] = task_id
            self.send_json(200, {"ok": True})
            return

        if parsed.path == "/api/result":
            if not self.agent_ok():
                self.send_text(401, "Invalid agent token")
                return
            data = self.read_json()
            client_id = str(data.get("client_id", ""))[:80]
            task_id = str(data.get("task_id", ""))[:80]
            action = str(data.get("action", "unknown"))
            output = str(data.get("output", ""))
            ok = bool(data.get("ok", False))
            add_event("result" if ok else "error", client_id, action, output, task_id=task_id)
            if running.get(client_id) == task_id:
                running.pop(client_id, None)
            cancel_requests.pop((client_id, task_id), None)
            self.send_json(200, {"ok": True})
            return

        self.send_text(404, "Not found")


if __name__ == "__main__":
    if HOST in ("0.0.0.0", "::"):
        print(f"Panel listening on {HOST}:{PORT}")
        print(f"Open (same machine): http://127.0.0.1:{PORT}")
        for ip in local_ipv4_addrs():
            print(f"Open (LAN): http://{ip}:{PORT}")
    else:
        print(f"Panel: http://{HOST}:{PORT}")
    print(f"Admin token: {ADMIN_TOKEN}")
    print(f"Agent token: {AGENT_TOKEN}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
