"""Run trace: one JSONL event log, a live console view, and an HTML report.

Every decision the agent makes (reasoning summary, tool call, observation,
question, approval, verdict) is an event. The trace is the answer to "why did
it do that?" and the report is the evidence handed back to the user.
"""
from __future__ import annotations

import html
import json
import os
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Trace:
    def __init__(self, run_dir: Path, console: Console | None = None):
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self.events: list[dict] = []
        self.console = console or Console()
        self._file = (run_dir / "trace.jsonl").open("a", encoding="utf-8")
        self._t0 = time.time()

    def event(self, kind: str, **data) -> dict:
        ev = {"t": round(time.time() - self._t0, 2), "kind": kind, **data}
        self.events.append(ev)
        self._file.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        self._file.flush()
        self._render(ev)
        return ev

    def close(self) -> None:
        self._file.close()

    # ------------------------------------------------------------------ console
    def _render(self, ev: dict) -> None:
        c, k = self.console, ev["kind"]
        tag = "[magenta]verifier[/] " if ev.get("role") == "verifier" else ""
        if k == "task":
            c.print(Panel(escape(ev["task"]), title="Task", border_style="cyan"))
        elif k == "llm":
            c.print(f"\n{tag}[bold]step {ev['step']}[/]")
            if ev.get("thinking"):
                c.print(f"  [dim italic]{escape(_short(ev['thinking'], 400))}[/]")
            if ev.get("text"):
                c.print(f"  {escape(_short(ev['text'], 500))}")
        elif k == "tool":
            args = _short(json.dumps(ev["args"], ensure_ascii=False), 160)
            c.print(f"  {tag}[cyan]{ev['name']}[/] [dim]{escape(args)}[/]")
            style = "red" if ev["is_error"] else "green"
            mark = "x" if ev["is_error"] else "ok"
            c.print(f"    [{style}]{mark}[/] [dim]{escape(_short(ev['result'], 220))}[/]")
        elif k == "plan":
            lines = [f"[bold]Goal:[/] {escape(ev['goal'])}", "[bold]Done when:[/]"]
            lines += [f"  - {escape(s)}" for s in ev["success_criteria"]]
            if ev.get("assumptions"):
                lines += ["[bold]Assuming:[/]"] + [f"  - {escape(s)}" for s in ev["assumptions"]]
            c.print(Panel("\n".join(lines), title="Plan", border_style="blue"))
        elif k == "harness":
            c.print(f"  [yellow]harness:[/] {escape(ev['note'])}")
        elif k == "verdict":
            colour = {"pass": "green", "fail": "red"}
            style = colour.get(ev["overall"], "yellow")
            lines = [f"[{colour.get(ch['result'], 'yellow')}]{ch['result'].upper():7}[/] "
                     f"{escape(ch['criterion'])}\n        [dim]{escape(_short(ch['observed'], 240))}[/]"
                     for ch in ev["checks"]]
            if ev.get("feedback"):
                lines.append(f"[bold]Feedback:[/] {escape(ev['feedback'])}")
            c.print(Panel("\n".join(lines), title=f"Independent verification: {ev['overall'].upper()}",
                          border_style=style))

    # ------------------------------------------------------------------ report
    def write_report(self, task: str, outcome: dict) -> Path:
        esc = html.escape
        rows = []
        for ev in self.events:
            k = ev["kind"]
            who = ev.get("role", "worker")
            if k == "llm" and (ev.get("thinking") or ev.get("text")):
                body = ""
                if ev.get("thinking"):
                    body += f'<div class="think">{esc(ev["thinking"])}</div>'
                if ev.get("text"):
                    body += f"<div>{esc(ev['text'])}</div>"
                rows.append(f'<div class="ev {who}"><span class="t">{ev["t"]}s · {who} · step {ev["step"]}</span>{body}</div>')
            elif k == "tool":
                shot = ""
                if ev.get("artifact"):
                    rel = os.path.relpath(ev["artifact"], self.run_dir).replace("\\", "/")
                    shot = f'<a href="{esc(rel)}"><img src="{esc(rel)}" loading="lazy"></a>'
                cls = "err" if ev["is_error"] else "okk"
                rows.append(
                    f'<div class="ev {who}"><span class="t">{ev["t"]}s · {who} · {ev["ms"]} ms</span>'
                    f'<code>{esc(ev["name"])} {esc(json.dumps(ev["args"], ensure_ascii=False))}</code>'
                    f'<details><summary class="{cls}">{esc(_short(ev["result"], 160))}</summary>'
                    f'<pre>{esc(ev["result"])}</pre>{shot}</details></div>')
            elif k == "harness":
                rows.append(f'<div class="ev harness"><span class="t">{ev["t"]}s · harness</span>{esc(ev["note"])}</div>')
            elif k == "verdict":
                checks = "".join(f'<tr><td class="{c["result"]}">{c["result"]}</td><td>{esc(c["criterion"])}</td>'
                                 f'<td>{esc(c["observed"])}</td></tr>' for c in ev["checks"])
                rows.append(f'<div class="ev verifier"><span class="t">{ev["t"]}s · verdict: {ev["overall"]}</span>'
                            f'<table>{checks}</table>{esc(ev.get("feedback") or "")}</div>')
        plan = outcome.get("plan") or {}
        crit = "".join(f"<li>{esc(s)}</li>" for s in plan.get("success_criteria", []))
        facts = "".join(f"<li><b>{esc(k)}</b>: {esc(v)}</li>" for k, v in (outcome.get("facts") or {}).items())
        details = "".join(f"<li>{esc(d)}</li>" for d in outcome.get("details", []))
        u = outcome.get("usage", {})
        page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Alfred run report</title><style>
body{{font:14px/1.5 system-ui,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#1c2330}}
h1{{font-size:20px}} .status{{display:inline-block;padding:3px 10px;border-radius:12px;background:#eef;font-weight:600}}
.ev{{border-left:3px solid #cbd3df;padding:6px 12px;margin:8px 0}} .ev.verifier{{border-color:#a05bd0;background:#faf6fd}}
.ev.harness{{border-color:#d9a400;background:#fffaea}} .t{{display:block;color:#7a8494;font-size:12px}}
.think{{color:#667;font-style:italic;white-space:pre-wrap}} pre{{white-space:pre-wrap;background:#f5f6f8;padding:8px;max-height:320px;overflow:auto}}
img{{max-width:100%;border:1px solid #ccd;margin-top:6px}} .err{{color:#b3261e}} .okk{{color:#1b6e3c}}
td{{padding:4px 8px;border-bottom:1px solid #e3e6eb;vertical-align:top}} td.pass{{color:#1b6e3c;font-weight:600}}
td.fail{{color:#b3261e;font-weight:600}} td.unknown{{color:#9a6b00;font-weight:600}} code{{word-break:break-all}}
</style></head><body>
<h1>Alfred run report</h1><p class="t">{esc(datetime.now().strftime("%Y-%m-%d %H:%M"))} · {esc(self.run_dir.name)}</p>
<h2>Task</h2><p>{esc(task)}</p>
<h2>Outcome <span class="status">{esc(outcome["status"])}</span></h2><p>{esc(outcome.get("summary", ""))}</p>
<ul>{details}</ul>
<h2>Success criteria (set before acting)</h2><ul>{crit}</ul>
<h2>Facts recorded</h2><ul>{facts}</ul>
<p class="t">{u.get("calls", 0)} model calls · {u.get("input_tokens", 0)} input / {u.get("output_tokens", 0)} output tokens ·
{u.get("cache_read_tokens", 0)} cached</p>
<h2>Timeline</h2>{''.join(rows)}</body></html>"""
        path = self.run_dir / "report.html"
        path.write_text(page, encoding="utf-8")
        return path
