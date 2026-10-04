# Alfred — an autonomous AI task worker

Give Alfred a task in plain language. It works out the steps, does them in a real browser and a file
workspace, recovers when things break, checks with you when it should not guess, and has its result
checked by an independent reviewer before it tells you "done".

```
python -m alfred "Find the latest invoice from Kestrel Logistics, enter it into Ledger, and tell me the amount and due date."
```

The prototype is deliberately narrow: one worker, a browser, files, and a small simulated company to work
in. Nothing about the task is hard-coded in the agent.

> Demo video: _add link here_

## Setup

Requires Python 3.11+ and an Anthropic API key.

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # optional: falls back to installed Chrome or Edge
```

Set the key (PowerShell: `$env:ANTHROPIC_API_KEY = "sk-ant-..."`, bash: `export ANTHROPIC_API_KEY=...`).

## Run

Terminal 1, the simulated company (intranet with a mail inbox and a bills system):

```bash
python -m sandbox --reset          # http://127.0.0.1:8000, --chaos off to disable injected failures
```

Terminal 2, the worker:

```bash
python -m alfred "Find the latest invoice from Kestrel Logistics, enter it into Ledger, and tell me the amount and due date." --headed
```

Useful flags: `--headed` shows the browser, `--slow 400` slows it down for a demo, `--approve ask|auto|deny`,
`--no-input` for unattended runs, `--max-steps N`, `--no-verify`.

Each run writes `runs/<timestamp>/`: `trace.jsonl` (every decision and observation), `shots/`
(a screenshot after every page change) and `report.html` (outcome, verification table, full timeline).

Other tasks to try, with no code changes between them:

| Task | What it exercises |
|---|---|
| `Bluepine Stationers emailed us about a change to their billing contact. Make sure Ledger reflects it.` | A different workflow (read mail, edit a vendor) |
| `Which of our unpaid bills are overdue today? Save them to overdue.csv and tell me the totals per currency.` | Read-only analysis with a file as the deliverable |
| `Enter the latest Kestrel invoice into Ledger.` | Ambiguity: there are two Kestrel vendors, so it should ask |
| `Mark the August Kestrel Logistics bill as paid.` | Approval gate on a consequential action |

Tests (no API key needed): `python -m pytest -q`

## Architecture

```
            task (plain language)
                    │
   ┌────────────────▼─────────────────┐        workspace/HANDBOOK.md   (what a new hire is told)
   │ worker.py   brief + control tools│◄────── workspace/.alfred/lessons.json  (memory across runs)
   └────────────────┬─────────────────┘
                    │
   ┌────────────────▼─────────────────┐   decide    ┌──────────────┐
   │ loop.py    generic agent loop    │◄───────────►│ llm.py Claude│
   │ budget · stall detection · nudges│             └──────────────┘
   └───┬───────────────┬──────────────┘
       │ act/observe   │ finish(success)
   ┌───▼────────┐  ┌───▼──────────────────────────────┐
   │ tools/     │  │ verifier.py  second agent,       │
   │ browser    │  │ fresh context, read-only browser │──► pass: done
   │ files      │  │ checks the criteria set up front │──► fail: findings go back to the worker
   │ policy.py  │  └──────────────────────────────────┘
   └───┬────────┘
       ▼
   trace.py  →  trace.jsonl, screenshots, report.html
```

- **`loop.py`** — model decides, harness executes, model observes. It knows nothing about browsers or
  invoices. It owns control: step budget, "you are repeating yourself" detection, and the guarantee that
  every tool call gets an observation back, including failures.
- **`tools/browser.py`** — Playwright. The model sees a text rendering of the visible page where each
  interactive element has a ref (`[e12 link "Invoice KL-2026-0926…"]`) and acts by ref.
- **`tools/files.py`** — list/read/write confined to the workspace. PDFs are read as text.
- **`worker.py`** — the four control tools (`plan`, `remember`, `ask_user`, `finish`) and the outcome logic.
- **`verifier.py`** — independent check of a claimed success.
- **`policy.py`** — which actions need a human's approval.
- **`sandbox/`** — the simulated company. The agent never imports it; it only sees it through the browser.

## Design decisions and why

**The model plans; the harness controls.** There is no hand-written workflow or planner graph. A capable
model with good observations chooses next steps better than a fixed pipeline, and a fixed pipeline is what
stops a system generalising. What the harness keeps for itself is everything that must not depend on the
model behaving: budgets, approval, read-only enforcement, and the final verdict.

**Success criteria are fixed before acting, and checked by someone else.** `plan` records concrete end
states up front. On `finish(success)` a second agent with a fresh context checks those criteria against
the live systems. It never sees the worker's transcript, so it cannot inherit the worker's mistakes or its
confidence. Its browser is read-only at the network layer (anything but GET is aborted), so "verify" can
never become "fix quietly". The harness, not the verifier model, computes the overall verdict from the
per-criterion results. A rejected claim goes back to the worker with the findings; after two rejections
the run is reported as `unverified`, never as success.

**Failures are observations.** Tools never raise into the loop. A 503, a validation message, a stale
element ref, a hallucinated tool name or a Python exception all come back as a readable result, because
"what went wrong" is exactly what the model needs to pick the next action. Only idempotent tools
(navigate, snapshot, read) are retried automatically; a click is never auto-retried, since that is how
duplicates get created. The sandbox injects a 503 on the first save and expires the session once, so this
is exercised on every run rather than assumed.

**Text snapshots with refs instead of screenshots or raw HTML.** Cheaper and faster than vision, far
smaller than HTML, and actions are unambiguous. Every action returns the resulting page, so observing is
not a separate step the model can forget. The cost: canvas-heavy or highly visual UIs would need a
vision fallback.

**Approval is enforced, not requested.** The prompt asks the model to be careful, but the gate is code:
a click on a control labelled pay/delete/approve/etc. pauses for a human. This also bounds the damage of
prompt injection. The sandbox inbox contains an email telling "AI assistants" to mark bills as paid; even
if a model fell for it, the click would stop at the gate.

**Environment knowledge lives in a handbook, not in code.** URLs, credentials and house rules are in
`workspace/HANDBOOK.md`, the same thing you would give a new hire. Pointing Alfred at a different
environment means writing a different handbook.

**Memory in three layers.** The append-only conversation is working memory. `remember(fact)` is a
structured ledger of values found during the run, which feeds the report. `remember(lesson)` persists
notes about how systems behave (for example "Ledger wants dates as YYYY-MM-DD") to
`workspace/.alfred/lessons.json` and loads them into the next run.

**Append-only context.** The transcript is never trimmed or rewritten. That keeps prompt caching
effective (each call re-reads the prefix from cache) and keeps earlier reasoning valid. Snapshots are
kept compact so a 40-step run stays well inside the context window.

**Honest outcomes.** A run ends as `success` (verified), `unverified`, `partial`, `failed`, `needs_user`
or `incomplete`. The exit code is 0 only for verified success.

## Assumptions

- Tasks are carried out through ordinary web UIs and files. No desktop apps, no CAPTCHAs, no MFA.
- The user's request plus the handbook is enough context for a competent new hire to do the task.
- Reads (GET) do not change state in the systems being verified.
- One task at a time, one worker, a human reachable at the terminal (or `--no-input`).

## Known limitations

- **Only exercised live in the sandbox.** Real sites bring iframes, shadow DOM, infinite scroll, popups
  and bot detection; the snapshot does not handle iframes or shadow DOM today.
- **Approval is label-based.** A risky action behind an innocuous label ("Confirm"), or a form submitted
  with the Enter key, would not be caught. A real system should gate on the request, not the label.
- **The verifier is another LLM.** It reduces self-grading bias but can be wrong. It also cannot sign in
  (read-only), so if the session has expired it returns `inconclusive` and the worker must sign in again.
- **Lessons are written by the model** and are not reviewed; a poisoned page could try to get a bad
  lesson saved. The prompt forbids it, nothing enforces it.
- **Credentials sit in the handbook** in plain text. Fine for a sandbox, wrong for production.
- **No resume.** A crashed run restarts from scratch (the duplicate check in the sandbox makes that safe
  here, not in general).
- The automated tests drive the full harness with a scripted stand-in for the model. They prove the
  machinery (loop, browser, gates, verification flow), not the quality of the model's decisions.

## What I would build next

1. An eval suite: 20–30 sandbox tasks with ground-truth checks via `/__sandbox/state`, run on every
   change, tracking success rate, steps and cost.
2. Request-level approval policy (method, URL, payload) with a dry-run preview of what will be sent.
3. Durable runs: checkpoint the transcript so a run can resume, and so `needs_user` can wait hours.
4. More tools behind the same interface: an HTTP/API tool, email sending, spreadsheets; a vision
   fallback for pages the text snapshot cannot represent.
5. A secrets broker so the model never sees credentials, and per-task scoped permissions.
6. A small web UI: live trace, approval inbox, run history.

## Models, APIs and components used

- **Model:** Claude Opus 5.5 (`claude-opus-5-5`) through the Anthropic Messages API with tool use,
  adaptive thinking (summarised, so the trace shows why each step was taken) and prompt caching.
  Override with `ALFRED_MODEL` / `ALFRED_EFFORT`.
- **Anthropic Python SDK** for the API call. The agent loop, tools, verifier and policy are written
  from scratch; no agent framework.
- **Playwright** (Chromium) for the browser, **pypdf** for reading PDFs, **rich** for the console.
- **Sandbox:** FastAPI + SQLite + reportlab (to generate the invoice PDFs). All data is fictional.
- Built with Claude Code as a coding assistant.
