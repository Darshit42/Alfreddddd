"""Desktop app: python -m alfred.app

A native window (pywebview) over a local-only web server. On first start it
asks how to reach a model: an API key for Claude, OpenAI or Gemini, or the
Claude Code login of someone with a Claude subscription. Keys are kept in the
operating system's credential store, never in a file.

The app is a thin shell: it calls the same run_task() as the CLI. Questions
and approvals that the CLI asks in the terminal appear here as dialogs.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

import keyring
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from rich.console import Console

from .providers import PROVIDERS, list_models, make_llm
from .store import Store
from .team import run_team
from .trace import Trace
from .worker import RunConfig, run_task

CONFIG = Path.home() / ".alfred" / "config.json"
WORKSPACE = Path("workspace")
SANDBOX_PORT = 8000


class Session:
    """Everything the UI can see about the current run."""

    def __init__(self) -> None:
        self.config: dict = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
        self.trace: Trace | None = None
        self.thread: threading.Thread | None = None
        self.task = ""
        self.outcome: dict | None = None
        self.report: str | None = None
        self.pending: dict | None = None     # a question or approval waiting for the human
        self.call: dict | None = None        # phone mode: {"status": ..., "transcript": [...]}
        self._answer = None
        self._answered = threading.Event()

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def save_config(self, **kw) -> None:
        self.config.update(kw)
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(self.config))

    def ask_human(self, pending: dict):
        """Called on the agent thread: show a dialog in the UI and block until it is answered."""
        self._answered.clear()
        self.pending = pending
        self._answered.wait()
        self.pending = None
        return self._answer

    def answer(self, value) -> None:
        self._answer = value
        self._answered.set()

    def run(self, task: str, headed: bool, team: bool = False, phone: str = "") -> None:
        """One task, or with a phone number the loop: do the task, call with the result, take the next by voice."""
        report = ""
        if task:
            report = self.run_one(task, headed, team)
        if not phone:
            return
        from . import voice
        try:
            while True:
                self.call = {"status": f"Calling {phone} ...", "transcript": []}
                result = voice.call_user(phone, report)
                self.call = {"transcript": result.get("transcript", []), "status": (
                    "Call finished: new task received" if result.get("next_task") else
                    "Call finished: no further task" if result.get("answered") else
                    f"Not answered ({result.get('error') or 'no answer'})")}
                if not result.get("next_task"):
                    return
                self.task, self.outcome, self.report, self.trace = result["next_task"], None, None, None
                report = self.run_one(self.task, headed, team)
        except Exception as e:  # noqa: BLE001 - calling is optional; say why it failed and keep the app alive
            self.call = {"status": f"Calling failed: {e}", "transcript": []}
        finally:
            voice.stop_worker()

    def run_one(self, task: str, headed: bool, team: bool = False) -> str:
        """Run a single task to its outcome; returns the report to speak on a call."""
        run_dir = Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S")
        store = Store(WORKSPACE / ".alfred" / "alfred.db")      # SQLite connections are per thread
        store.start_run(run_dir.name, task)
        trace = self.trace = Trace(run_dir, Console(quiet=True), sink=store.log)
        handbook = WORKSPACE / "HANDBOOK.md"

        def asker(question: str, options: list[str]):
            answer = self.ask_human({"kind": "question", "text": question, "options": options})
            trace.event("harness", note=f"User was asked: {question} -> answered: {answer}")
            return answer

        def approver(action: str, url: str) -> bool:
            return bool(self.ask_human({"kind": "approval", "text": action, "url": url}))

        cfg = RunConfig(workspace=WORKSPACE, run_dir=run_dir, headed=headed, slow_mo=250 if headed else 0,
                        handbook=handbook.read_text(encoding="utf-8") if handbook.exists() else "",
                        asker=asker, approver=approver)
        try:
            provider = self.config["provider"]
            llm = make_llm(provider, keyring.get_password("alfred", provider), self.config.get("model"))
            outcome = (run_team if team else run_task)(task, llm, cfg, trace)
        except Exception as e:  # noqa: BLE001 - show the failure in the UI rather than dying silently
            trace.event("harness", note=f"Run aborted: {type(e).__name__}: {e}")
            outcome = {"status": "incomplete", "summary": f"The run aborted: {type(e).__name__}: {e}", "details": []}
        store.end_run(run_dir.name, outcome)
        self.report = str(trace.write_report(task, outcome).resolve())
        trace.close()
        self.outcome = outcome
        from .voice import spoken_report
        return spoken_report(task, outcome)


class Connect(BaseModel):
    provider: str
    key: str = ""


class Start(BaseModel):
    task: str
    headed: bool = False
    team: bool = False
    phone: str = ""      # E.164; when set, Alfred calls with the result and takes the next task by voice


def create_ui(session: Session) -> FastAPI:
    api = FastAPI(title="Alfred")

    @api.get("/")
    def index():
        return HTMLResponse(PAGE.replace("__PROVIDERS__", json.dumps(PROVIDERS)))

    @api.post("/api/connect")
    def connect(body: Connect):
        """Validate the credential against the provider before saving anything."""
        if body.provider not in PROVIDERS:
            return JSONResponse({"error": "Unknown provider."}, 400)
        key = body.key.strip() or (keyring.get_password("alfred", body.provider) or "")
        if body.provider != "claude-code" and not key:
            return JSONResponse({"error": "Enter an API key."}, 400)
        try:
            models = list_models(body.provider, key or None)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Could not connect: {str(e).splitlines()[0][:300]}"}, 400)
        if not models:
            return JSONResponse({"error": "The key works but no usable models were found."}, 400)
        if key:
            keyring.set_password("alfred", body.provider, key)
        session.save_config(provider=body.provider, model=models[0], models=models)
        return {"ok": True}

    @api.post("/api/model")
    def set_model(body: dict):
        session.save_config(model=body.get("model"))
        return {"ok": True}

    @api.post("/api/disconnect")
    def disconnect():
        session.save_config(provider=None)
        return {"ok": True}

    @api.post("/api/run")
    def start(body: Start):
        if session.running:
            return JSONResponse({"error": "A task is already running."}, 409)
        if not session.config.get("provider"):
            return JSONResponse({"error": "Connect a model first."}, 400)
        phone = body.phone.strip().replace(" ", "")
        if phone:
            from . import voice
            if not re.fullmatch(r"\+\d{8,15}", phone):
                return JSONResponse({"error": "Phone number must include the country code, e.g. +919812345678."}, 400)
            if voice.missing_config():
                return JSONResponse({"error": "Calling is not configured: fill in .env (see .env.example)."}, 400)
            session.save_config(phone=phone)
        elif not body.task.strip():
            return JSONResponse({"error": "Type a task first."}, 400)
        session.task, session.outcome, session.report, session.trace = body.task.strip(), None, None, None
        session.call = None
        session.thread = threading.Thread(target=session.run, args=(session.task, body.headed, body.team, phone),
                                          daemon=True)
        session.thread.start()
        return {"ok": True}

    @api.post("/api/reply")
    def reply(body: dict):
        session.answer(body.get("value"))
        return {"ok": True}

    @api.post("/api/sandbox/reset")
    def sandbox_reset():
        import httpx
        httpx.post(f"http://127.0.0.1:{SANDBOX_PORT}/__sandbox/reset", timeout=10)
        return {"ok": True}

    @api.get("/api/shot/{index}")
    def shot(index: int):
        ev = session.trace.events[index] if session.trace and 0 <= index < len(session.trace.events) else None
        if not ev or not ev.get("artifact") or not Path(ev["artifact"]).exists():
            return JSONResponse({"error": "no screenshot"}, 404)
        return FileResponse(ev["artifact"])

    @api.get("/api/state")
    def state(since: int = 0):
        events = session.trace.events if session.trace else []
        out, shot_index = [], None
        for i, ev in enumerate(events):
            if ev.get("artifact") and ev.get("role") != "verifier":
                shot_index = i
            if i >= since:
                e = dict(ev)
                if "result" in e:
                    e["result"] = e["result"][:700]
                out.append(e)
        cfg = session.config
        return {"provider": cfg.get("provider"), "provider_name": PROVIDERS.get(cfg.get("provider") or "", ""),
                "model": cfg.get("model"), "models": cfg.get("models", []), "running": session.running,
                "task": session.task, "run": session.trace.run_dir.name if session.trace else None, "events": out, "total": len(events), "shot": shot_index,
                "pending": session.pending, "call": session.call, "phone": cfg.get("phone", ""),
                "outcome": session.outcome, "report": session.report,
                "sandbox": f"http://127.0.0.1:{SANDBOX_PORT}/"}

    return api


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port: int) -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)


def main() -> None:
    # Start the demo company alongside the app unless something is already on its port.
    with socket.socket() as s:
        sandbox_running = s.connect_ex(("127.0.0.1", SANDBOX_PORT)) == 0
    if not sandbox_running:
        from sandbox.app import create_app
        _serve(create_app(), SANDBOX_PORT)
    port = _free_port()
    _serve(create_ui(Session()), port)
    url = f"http://127.0.0.1:{port}/"
    try:
        import webview
        webview.create_window("Alfred", url, width=1280, height=860, min_size=(980, 640))
        webview.start()
    except Exception as e:  # noqa: BLE001 - no native webview available: fall back to the default browser
        import webbrowser
        print(f"Native window unavailable ({e}); opening {url} in your browser. Ctrl+C to quit.")
        webbrowser.open(url)
        while True:
            time.sleep(1)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Alfred</title><style>
:root{--bg:#f3f4f6;--card:#fff;--ink:#18202b;--mute:#6b7585;--line:#dfe3ea;--accent:#2f5bd8;--ok:#1b7a43;--bad:#b3261e;--warn:#9a6b00}
*{box-sizing:border-box} body{margin:0;font:14px/1.5 "Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;align-items:center;gap:12px;padding:10px 20px;background:#18202b;color:#fff}
header h1{font-size:16px;margin:0;font-weight:600} header .sp{flex:1} header select,header button{font:inherit}
.badge{background:#2b3646;border-radius:12px;padding:2px 10px;font-size:12px}
main{display:grid;grid-template-columns:minmax(340px,420px) 1fr;gap:16px;padding:16px 20px;height:calc(100vh - 48px)}
.col{display:flex;flex-direction:column;gap:14px;min-height:0;overflow:auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);margin:0 0 8px}
textarea,input[type=password],input[type=text]{width:100%;font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px}
textarea{min-height:84px;resize:vertical}
button{font:inherit;padding:7px 14px;border-radius:8px;border:1px solid var(--line);background:#fff;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff} button:disabled{opacity:.5;cursor:default}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0} .chips button{font-size:12px;padding:3px 9px;border-radius:14px;color:var(--mute)}
.row{display:flex;align-items:center;gap:10px;margin-top:10px} .row .sp{flex:1}
ul{margin:4px 0;padding-left:18px} .mute{color:var(--mute)} .small{font-size:12px}
#shot{width:100%;border:1px solid var(--line);border-radius:8px;display:none}
#timeline{flex:1;min-height:200px;overflow:auto}
.ev{border-left:3px solid var(--line);padding:3px 10px;margin:5px 0;font-size:13px}
.ev.verifier{border-color:#9a5bd0;background:#faf6fd}.ev.overseer{border-color:#2f5bd8;background:#f1f5ff}.ev.harness,.ev.write{border-color:#d9a400;background:#fffaea}
.ev .say{color:#3b4656}.ev code{font-size:12px;word-break:break-all}.ev .res{color:var(--mute);font-size:12px}
.ev .res.err{color:var(--bad)} .who{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
.status{display:inline-block;padding:2px 12px;border-radius:12px;font-weight:600;color:#fff;background:var(--mute)}
.status.success{background:var(--ok)}.status.failed,.status.incomplete{background:var(--bad)}
.status.partial,.status.needs_user,.status.unverified{background:var(--warn)}
table{border-collapse:collapse;width:100%;font-size:13px} td{padding:4px 6px;border-top:1px solid var(--line);vertical-align:top}
td.pass{color:var(--ok);font-weight:600}td.fail{color:var(--bad);font-weight:600}td.unknown{color:var(--warn);font-weight:600}
.overlay{position:fixed;inset:0;background:rgba(24,32,43,.55);display:none;align-items:center;justify-content:center;z-index:5}
.overlay .card{width:min(560px,92vw);max-height:90vh;overflow:auto}
.prov{display:block;border:1px solid var(--line);border-radius:8px;padding:9px 12px;margin:6px 0;cursor:pointer}
.prov.sel{border-color:var(--accent);background:#eef3ff} .error{color:var(--bad);margin-top:8px}
</style></head><body>
<header><h1>Alfred</h1><span class="mute small" style="color:#aab4c4">autonomous AI worker</span><span class="sp"></span>
<span class="badge" id="provider"></span><select id="model"></select><button id="change">Change model</button></header>
<main>
<div class="col">
  <div class="card"><h2>Task</h2>
    <textarea id="task" placeholder="Describe what you want done, in plain language"></textarea>
    <div class="chips" id="chips"></div>
    <div class="row"><label class="small"><input type="checkbox" id="headed"> Show browser</label>
    <label class="small" title="An overseer splits the request and dispatches role-scoped sub-agents"><input type="checkbox" id="team"> Overseer + sub-agents</label><span class="sp"></span>
    <button class="primary" id="run">Run</button></div>
    <div class="row"><input type="text" id="phone" placeholder="Your phone, e.g. +919812345678 (optional)" style="flex:1">
    <button id="callme" title="Alfred rings you and takes the task by voice">Call me for a task</button></div>
    <div class="small mute" style="margin-top:4px">With a number filled in, Run also calls you with the result and takes the next task by voice.</div>
    <div class="row small mute"><a id="sandbox" href="#" target="_blank">Open the demo company</a><span class="sp"></span>
    <button id="reset" class="small">Reset demo data</button></div>
    <div class="error" id="runerr"></div></div>
  <div class="card" id="callcard" style="display:none"><h2>Phone</h2><div id="callbox"></div></div>
  <div class="card" id="plancard" style="display:none"><h2>Plan</h2><div id="plan"></div></div>
  <div class="card" id="resultcard" style="display:none"><h2>Result</h2><div id="result"></div></div>
</div>
<div class="col">
  <div class="card"><h2>What the worker sees</h2><img id="shot"><div class="mute small" id="noshot">The live browser view appears here once a task is running.</div></div>
  <div class="card" style="flex:1;display:flex;flex-direction:column;min-height:0"><h2>Activity <span id="busy" class="mute"></span></h2><div id="timeline"></div></div>
</div>
</main>
<div class="overlay" id="setup"><div class="card"><h2>Connect a model</h2>
  <p class="mute">Alfred needs a model to think with. Pick how you want to connect. Keys are stored in your operating system's credential store.</p>
  <div id="provs"></div>
  <div id="keyrow"><input type="password" id="key" placeholder="Paste your API key"></div>
  <p class="mute small" id="cchint" style="display:none">Uses the Claude Code app already installed and signed in on this computer, so it runs on your Claude subscription. No key needed.</p>
  <div class="error" id="seterr"></div>
  <div class="row"><span class="sp"></span><button id="cancel">Cancel</button><button class="primary" id="connect">Connect</button></div></div></div>
<div class="overlay" id="ask"><div class="card"><h2 id="asktitle"></h2><p id="asktext"></p><div id="askopts"></div>
  <div id="askfree"><input type="text" id="askinput" placeholder="Your answer"></div>
  <div class="row"><span class="sp"></span><span id="askbtns"></span></div></div></div>
<script>
const PROVIDERS = __PROVIDERS__;
const EXAMPLES = [
 "Find the latest invoice from Kestrel Logistics, enter it into Ledger, and tell me the amount and due date.",
 "Bluepine Stationers emailed us about a change to their billing contact. Make sure Ledger reflects it.",
 "Which of our unpaid bills are overdue today? Save them to overdue.csv and tell me the totals per currency.",
 "Enter the latest Kestrel invoice into Ledger.",
 "Mark the August Kestrel Logistics bill as paid.",
 "Enter the latest Kestrel Logistics invoice into Ledger, and update Bluepine Stationers' billing contact from their email."];
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const post = (url, body) => fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body || {})}).then(async r => ({ok: r.ok, data: await r.json()}));
let seen = 0, chosen = "claude-code", lastShot = null, askShown = null, runId = null;

EXAMPLES.forEach(t => { const b = document.createElement("button"); b.textContent = t.length > 46 ? t.slice(0, 44) + "…" : t; b.title = t; b.onclick = () => $("task").value = t; $("chips").appendChild(b); });
Object.entries(PROVIDERS).forEach(([id, name]) => { const d = document.createElement("label"); d.className = "prov"; d.dataset.id = id; d.textContent = name; d.onclick = () => pick(id); $("provs").appendChild(d); });
function pick(id) { chosen = id; document.querySelectorAll(".prov").forEach(p => p.classList.toggle("sel", p.dataset.id === id));
  $("keyrow").style.display = id === "claude-code" ? "none" : ""; $("cchint").style.display = id === "claude-code" ? "" : "none"; $("seterr").textContent = ""; }
pick(chosen);
$("connect").onclick = async () => { $("connect").disabled = true; $("connect").textContent = "Checking…"; $("seterr").textContent = "";
  const r = await post("/api/connect", {provider: chosen, key: $("key").value});
  $("connect").disabled = false; $("connect").textContent = "Connect";
  if (!r.ok) { $("seterr").textContent = r.data.error; return; } $("key").value = ""; $("setup").style.display = "none"; };
$("cancel").onclick = () => $("setup").style.display = "none";
$("change").onclick = () => $("setup").style.display = "flex";
$("model").onchange = () => post("/api/model", {model: $("model").value});
$("reset").onclick = async () => { await post("/api/sandbox/reset"); $("reset").textContent = "Reset done"; setTimeout(() => $("reset").textContent = "Reset demo data", 1500); };
const start = async (task, phone) => { $("runerr").textContent = "";
  const r = await post("/api/run", {task, phone, headed: $("headed").checked, team: $("team").checked}); if (!r.ok) $("runerr").textContent = r.data.error; };
$("run").onclick = () => { const task = $("task").value.trim(); if (task) start(task, $("phone").value.trim()); };
$("callme").onclick = () => { const phone = $("phone").value.trim(); if (!phone) { $("runerr").textContent = "Enter your phone number first."; return; } start("", phone); };

function render(ev) {
  const who = ev.role === "verifier" || ev.role === "overseer" ? ev.role : "worker";
  if (ev.kind === "llm") { const t = ev.thinking || ev.text; if (!t) return "";
    return `<div class="ev ${who}"><span class="who">${who} · step ${ev.step}</span><div class="say">${esc(t)}</div></div>`; }
  if (ev.kind === "tool") return `<div class="ev ${who}"><code>${esc(ev.name)} ${esc(JSON.stringify(ev.args)).slice(0, 220)}</code><div class="res ${ev.is_error ? "err" : ""}">${esc(ev.result).slice(0, 260)}</div></div>`;
  if (ev.kind === "harness") return `<div class="ev harness"><span class="who">${ev.role === "overseer" ? "overseer" : "harness"}</span> ${esc(ev.note)}</div>`;
  if (ev.kind === "write") return `<div class="ev write"><span class="who">enforcer</span> ${esc(ev.method)} ${esc(ev.url)} → <b>${esc(ev.verdict)}</b></div>`;
  if (ev.kind === "verdict") return `<div class="ev verifier"><span class="who">independent verification: ${esc(ev.overall)}</span>${checks(ev)}</div>`;
  return "";
}
const checks = v => `<table>${(v.checks || []).map(c => `<tr><td class="${c.result}">${c.result}</td><td>${esc(c.criterion)}<div class="mute small">${esc(c.observed)}</div></td></tr>`).join("")}</table>`;

async function tick() {
  let s; try { s = await (await fetch("/api/state?since=" + seen)).json(); } catch (e) { return; }
  $("provider").textContent = s.provider_name || "No model connected";
  if (!s.provider && $("setup").style.display !== "flex") $("setup").style.display = "flex";
  const sel = $("model"); if (sel.dataset.sig !== s.models.join()) { sel.dataset.sig = s.models.join(); sel.innerHTML = s.models.map(m => `<option>${esc(m)}</option>`).join(""); }
  if (document.activeElement !== sel) sel.value = s.model || "";
  if (!$("phone").dataset.init) { $("phone").dataset.init = "1"; $("phone").value = s.phone || ""; }
  $("callme").disabled = s.running;
  $("callcard").style.display = s.call ? "" : "none";
  if (s.call) $("callbox").innerHTML = `<b>${esc(s.call.status)}</b>` + (s.call.transcript || []).map(t => `<div class="small"><span class="who">${esc(t.role)}</span> ${esc(t.text)}</div>`).join("");
  if (s.task && document.activeElement !== $("task") && s.running) $("task").value = s.task;
  $("sandbox").href = s.sandbox; $("run").disabled = s.running; $("busy").textContent = s.running ? "· working…" : "";
  if (s.run !== runId) { runId = s.run; seen = 0; lastShot = null; $("timeline").innerHTML = "";
    $("plancard").style.display = "none"; $("resultcard").style.display = "none"; return; }
  const tl = $("timeline"); const stick = tl.scrollTop + tl.clientHeight >= tl.scrollHeight - 30;
  for (const ev of s.events) { if (ev.kind === "plan") { $("plancard").style.display = "";
      $("plan").innerHTML = `<b>${esc(ev.goal)}</b><div class="mute small" style="margin-top:6px">Done when</div><ul>${ev.success_criteria.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` + (ev.assumptions.length ? `<div class="mute small">Assuming</div><ul>${ev.assumptions.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : ""); }
    tl.insertAdjacentHTML("beforeend", render(ev)); }
  seen = s.total; if (stick) tl.scrollTop = tl.scrollHeight;
  if (s.shot !== null && s.shot !== lastShot) { lastShot = s.shot; $("shot").src = "/api/shot/" + s.shot + "?t=" + Date.now(); $("shot").style.display = "block"; $("noshot").style.display = "none"; }
  if (s.outcome && !s.running) { const o = s.outcome; $("resultcard").style.display = "";
    const v = o.verified === true ? "Passed independent verification" : o.verified === false ? "Did NOT pass independent verification" : "Not verified";
    $("result").innerHTML = `<span class="status ${o.status}">${esc(o.status)}</span><p>${esc(o.summary)}</p><ul>${(o.details || []).map(d => `<li>${esc(d)}</li>`).join("")}</ul>` +
      (o.verdict ? checks(o.verdict) : "") + `<p class="mute small">${v}${o.steps ? " · " + o.steps + " steps" : ""}<br>Evidence report: ${esc(s.report || "")}</p>`; }
  const p = s.pending, key = p ? p.kind + p.text : null;
  if (key !== askShown) { askShown = key; $("ask").style.display = p ? "flex" : "none";
    if (p) { const approval = p.kind === "approval"; $("asktitle").textContent = approval ? "Approval required" : "Alfred needs your input";
      $("asktext").innerHTML = esc(p.text) + (approval ? `<div class="mute small">on ${esc(p.url)}</div>` : "");
      $("askfree").style.display = approval ? "none" : ""; $("askinput").value = "";
      $("askopts").innerHTML = (p.options || []).map(o => `<button style="display:block;margin:6px 0;text-align:left;width:100%" data-v="${esc(o)}">${esc(o)}</button>`).join("");
      $("askopts").querySelectorAll("button").forEach(b => b.onclick = () => post("/api/reply", {value: b.dataset.v}));
      $("askbtns").innerHTML = approval ? `<button id="deny">Deny</button> <button class="primary" id="allow">Allow</button>` : `<button class="primary" id="send">Send</button>`;
      if (approval) { $("deny").onclick = () => post("/api/reply", {value: false}); $("allow").onclick = () => post("/api/reply", {value: true}); }
      else $("send").onclick = () => { if ($("askinput").value.trim()) post("/api/reply", {value: $("askinput").value.trim()}); }; } }
}
setInterval(tick, 800); tick();
</script></body></html>"""


if __name__ == "__main__":
    main()
