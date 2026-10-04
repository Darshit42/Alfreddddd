"""Command line.

  python -m alfred "task"                 do one task now, with you at the terminal
  python -m alfred add "task"             put a task on the queue
  python -m alfred work                   work through the queue unattended
  python -m alfred status                 queue, escalations and metrics
  python -m alfred answer ID "reply"      respond to an escalated task and requeue it
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .policy import always_deny, auto_approve
from .providers import ENV_KEYS, PROVIDERS, detect_provider, make_llm
from .store import Store, prior_attempts_note
from .trace import Trace
from .worker import RunConfig, run_task

STATUS_STYLE = {"success": "green", "partial": "yellow", "needs_user": "yellow", "unverified": "yellow",
                "failed": "red", "incomplete": "red"}
STATE_STYLE = {"done": "green", "queued": "cyan", "leased": "blue", "needs_human": "yellow", "failed": "red"}
COMMANDS = ("run", "add", "work", "status", "answer")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", default="workspace", help="directory for files, downloads, memory and the queue")
    run_opts = argparse.ArgumentParser(add_help=False)
    run_opts.add_argument("--handbook", default=None, help="context file (default: <workspace>/HANDBOOK.md)")
    run_opts.add_argument("--headed", action="store_true", help="show the browser window while it works")
    run_opts.add_argument("--slow", type=int, default=0, metavar="MS", help="slow each browser action down (demos)")
    run_opts.add_argument("--max-steps", type=int, default=40)
    run_opts.add_argument("--max-cost", type=float, default=10.0, metavar="USD", help="stop a run past this spend")
    run_opts.add_argument("--no-verify", action="store_true", help="skip independent verification")
    run_opts.add_argument("--provider", choices=list(PROVIDERS), default=None,
                          help="model provider (default: whichever credential is found; see README)")
    run_opts.add_argument("--model", default=None, help="model name for the chosen provider")

    ap = argparse.ArgumentParser(prog="alfred", description="Autonomous AI task worker")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", parents=[common, run_opts], help="do one task now")
    run.add_argument("task", help="what you want done, in plain language")
    run.add_argument("--approve", choices=["ask", "auto", "deny"], default="ask",
                     help="what to do when an action needs human approval (default: ask)")
    run.add_argument("--no-input", action="store_true", help="never prompt: questions go unanswered, approvals denied")
    add = sub.add_parser("add", parents=[common], help="queue a task")
    add.add_argument("task")
    work = sub.add_parser("work", parents=[common, run_opts], help="work through the queue unattended")
    work.add_argument("--once", action="store_true", help="do one task and exit")
    sub.add_parser("status", parents=[common], help="show the queue and metrics")
    ans = sub.add_parser("answer", parents=[common], help="reply to an escalated task and requeue it")
    ans.add_argument("id", type=int)
    ans.add_argument("reply")
    ans.add_argument("--approve", action="store_true",
                     help="also pre-approve the gated action for the next attempt (the never-list still applies)")
    return ap


def execute(task: str, args, store: Store, console: Console, *, task_id: int | None = None,
            interactive: bool, approve: str, earlier: str = "") -> dict:
    """One run, start to finish: trace, decision log, report, result panel."""
    workspace = Path(args.workspace)
    handbook_path = Path(args.handbook) if args.handbook else workspace / "HANDBOOK.md"
    handbook = handbook_path.read_text(encoding="utf-8") if handbook_path.exists() else ""
    run_dir = Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S")
    store.start_run(run_dir.name, task, task_id)
    trace = Trace(run_dir, console, sink=store.log)

    def asker(question: str, options: list[str]) -> str | None:
        if not interactive:
            return None
        body = escape(question) + ("\n" + "\n".join(f"  {i}. {escape(o)}" for i, o in enumerate(options, 1))
                                   if options else "")
        console.print(Panel(body, title="Alfred needs your input", border_style="yellow"))
        answer = Prompt.ask("Your answer (number or text)")
        if options and answer.strip().isdigit() and 1 <= int(answer) <= len(options):
            answer = options[int(answer) - 1]
        trace.event("harness", note=f"User was asked: {question} -> answered: {answer}")
        return answer

    def ask_approval(action: str, url: str) -> bool:
        if not interactive:
            return False
        console.print(Panel(f"{escape(action)}\non {escape(url)}", title="Approval required", border_style="yellow"))
        return Confirm.ask("Allow this action?", default=False)

    cfg = RunConfig(
        workspace=workspace, run_dir=run_dir, handbook=handbook, headed=args.headed, slow_mo=args.slow,
        max_steps=args.max_steps, max_cost_usd=args.max_cost, verify=not args.no_verify, asker=asker,
        approver={"ask": ask_approval, "auto": auto_approve, "deny": always_deny}[approve],
        earlier_attempts=earlier, heartbeat=(lambda: store.heartbeat(task_id)) if task_id else (lambda: None))

    try:
        outcome = run_task(task, make_llm(args.provider, model=args.model), cfg, trace)
    except KeyboardInterrupt:
        outcome = {"status": "incomplete", "summary": "Interrupted by the user.", "details": []}
    except Exception as e:  # noqa: BLE001 - an infrastructure failure is still an outcome, with the trace intact
        trace.event("harness", note=f"Run aborted: {type(e).__name__}: {e}")
        outcome = {"status": "incomplete", "summary": f"The run aborted: {type(e).__name__}: {e}", "details": []}
    store.end_run(run_dir.name, outcome)
    report = trace.write_report(task, outcome)
    trace.close()

    lines = [escape(outcome["summary"])] + [f"  - {escape(d)}" for d in outcome.get("details", [])]
    verified = {True: "passed independent verification", False: "did NOT pass independent verification",
                None: "not verified"}[outcome.get("verified")]
    lines += ["", f"[dim]Verification: {verified} · cost ${outcome.get('cost_usd', 0):.2f}[/]",
              f"[dim]Evidence: {report}[/]"]
    console.print(Panel("\n".join(lines), title=f"Result: {outcome['status'].upper()}",
                        border_style=STATUS_STYLE.get(outcome["status"], "white")))
    return outcome


def cmd_work(args, store: Store, console: Console) -> int:
    """Drain the queue with nobody watching. Anything the worker cannot safely finish is escalated, not guessed."""
    done = 0
    while True:
        for tid in store.reclaim_expired():
            console.print(f"[yellow]supervisor:[/] task {tid} had a lapsed lease (worker died); reclaimed.")
        row = store.lease_next()
        if row is None:
            console.print(f"Queue empty. {done} task(s) worked.")
            return 0
        console.rule(f"task {row['id']} (attempt {row['attempts'] + 1})")
        outcome = execute(row["task"], args, store, console, task_id=row["id"], interactive=False,
                          approve="auto" if row["grant_approval"] else "deny",
                          earlier=prior_attempts_note(row["history"], row["attempts"] + 1))
        state = store.complete(row["id"], outcome)
        console.print(f"task {row['id']} -> [{STATE_STYLE[state]}]{state}[/]")
        done += 1
        if args.once:
            return 0


def cmd_status(store: Store, console: Console) -> int:
    table = Table(title="Tasks")
    for col in ("ID", "State", "Tries", "Task", "Result / question"):
        table.add_column(col)
    for t in store.tasks():
        table.add_row(str(t["id"]), f"[{STATE_STYLE[t['state']]}]{t['state']}[/]", str(t["attempts"]),
                      escape(t["task"][:60]), escape((t["result"] or "")[:90]))
    console.print(table)
    m = store.metrics()
    per = f"${m['cost_per_verified']:.2f}" if m["cost_per_verified"] is not None else "n/a"
    console.print(f"Runs: {m['runs']} · verified success: {m['verified']} · escalated to a human: {m['escalated']} · "
                  f"failed: {m['failed']} · avg steps: {m['avg_steps']:.0f}\n"
                  f"Spend: ${m['cost']:.2f} · cost per verified success (fully loaded): {per}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and argv[0] not in ("-h", "--help"):
        argv.insert(0, "run")   # python -m alfred "task" is shorthand for: run "task"
    args = build_parser().parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    console = Console()
    store = Store(Path(args.workspace) / ".alfred" / "alfred.db")

    if args.command == "add":
        console.print(f"Queued as task {store.add(args.task)}.")
        return 0
    if args.command == "status":
        return cmd_status(store, console)
    if args.command == "answer":
        if not store.answer(args.id, args.reply, args.approve):
            console.print(f"[red]Task {args.id} is not waiting for a human.[/]")
            return 1
        console.print(f"Task {args.id} requeued with your reply.")
        return 0

    args.provider = args.provider or detect_provider()
    if not args.provider:
        console.print(f"[red]No model credentials found.[/] Set one of {', '.join(ENV_KEYS.values())}, "
                      "or install Claude Code and sign in to use a Claude subscription.")
        return 2
    console.print(f"[dim]Model provider: {PROVIDERS[args.provider]}[/]")
    if args.command == "work":
        return cmd_work(args, store, console)
    interactive = sys.stdin.isatty() and not args.no_input
    outcome = execute(args.task, args, store, console, interactive=interactive, approve=args.approve)
    return 0 if outcome["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
