"""Command line entry point: python -m alfred "your task" """
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

from .policy import always_deny, auto_approve
from .trace import Trace
from .worker import RunConfig, run_task

STATUS_STYLE = {"success": "green", "partial": "yellow", "needs_user": "yellow", "unverified": "yellow",
                "failed": "red", "incomplete": "red"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="alfred", description="Autonomous AI task worker")
    ap.add_argument("task", help="what you want done, in plain language")
    ap.add_argument("--workspace", default="workspace", help="directory for files, downloads and memory")
    ap.add_argument("--handbook", default=None, help="context file for the worker (default: <workspace>/HANDBOOK.md)")
    ap.add_argument("--headed", action="store_true", help="show the browser window while it works")
    ap.add_argument("--slow", type=int, default=0, metavar="MS", help="slow each browser action down (for demos)")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--approve", choices=["ask", "auto", "deny"], default="ask",
                    help="what to do when an action needs human approval (default: ask)")
    ap.add_argument("--no-input", action="store_true", help="never prompt: questions go unanswered, approvals are denied")
    ap.add_argument("--no-verify", action="store_true", help="skip independent verification")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    console = Console()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        console.print("[red]ANTHROPIC_API_KEY is not set.[/] Set it and run again.")
        return 2

    workspace = Path(args.workspace)
    handbook_path = Path(args.handbook) if args.handbook else workspace / "HANDBOOK.md"
    handbook = handbook_path.read_text(encoding="utf-8") if handbook_path.exists() else ""
    run_dir = Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S")
    interactive = sys.stdin.isatty() and not args.no_input

    def asker(question: str, options: list[str]) -> str | None:
        if not interactive:
            return None
        console.print(Panel(escape(question) + ("\n" + "\n".join(f"  {i}. {escape(o)}" for i, o in enumerate(options, 1))
                                                if options else ""), title="Alfred needs your input", border_style="yellow"))
        answer = Prompt.ask("Your answer (number or text)")
        if options and answer.strip().isdigit() and 1 <= int(answer) <= len(options):
            answer = options[int(answer) - 1]
        trace.event("harness", note=f"User was asked: {question} -> answered: {answer}")
        return answer

    def ask_approval(action: str, url: str) -> bool:
        if not interactive:
            trace.event("harness", note=f"Approval needed for '{action}' but nobody is available: denied.")
            return False
        console.print(Panel(f"{escape(action)}\non {escape(url)}", title="Approval required", border_style="yellow"))
        ok = Confirm.ask("Allow this action?", default=False)
        trace.event("harness", note=f"Approval for '{action}' on {url}: {'granted' if ok else 'denied'}.")
        return ok

    approver = {"ask": ask_approval, "auto": auto_approve, "deny": always_deny}[args.approve]
    trace = Trace(run_dir, console)
    cfg = RunConfig(workspace=workspace, run_dir=run_dir, handbook=handbook, headed=args.headed, slow_mo=args.slow,
                    max_steps=args.max_steps, verify=not args.no_verify, approver=approver, asker=asker)

    from .llm import ClaudeLLM
    try:
        outcome = run_task(args.task, ClaudeLLM(), cfg, trace)
    except KeyboardInterrupt:
        outcome = {"status": "incomplete", "summary": "Interrupted by the user.", "details": []}
    except Exception as e:  # noqa: BLE001 - report infrastructure failures as an outcome, with the trace intact
        trace.event("harness", note=f"Run aborted: {type(e).__name__}: {e}")
        outcome = {"status": "incomplete", "summary": f"The run aborted: {type(e).__name__}: {e}", "details": []}
    report = trace.write_report(args.task, outcome)
    trace.close()

    style = STATUS_STYLE.get(outcome["status"], "white")
    lines = [escape(outcome["summary"])] + [f"  - {escape(d)}" for d in outcome.get("details", [])]
    verified = {True: "passed independent verification", False: "did NOT pass independent verification",
                None: "not verified"}[outcome.get("verified")]
    lines += ["", f"[dim]Verification: {verified}[/]", f"[dim]Evidence: {report}[/]"]
    console.print(Panel("\n".join(lines), title=f"Result: {outcome['status'].upper()}", border_style=style))
    return 0 if outcome["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
