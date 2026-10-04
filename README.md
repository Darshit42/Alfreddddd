# Alfred — an autonomous AI employee prototype

Give Alfred a task in plain language. It works out the steps, does them in a real browser and a file
workspace, recovers when things break, checks with you when it should not guess, and has its result
checked by an independent reviewer before it tells you "done".

```
python -m alfred "Find the latest invoice from Kestrel Logistics, enter it into Ledger, and tell me the amount and due date."
```

The prototype is deliberately narrow: one worker, a browser, files, and a small simulated company to work
in. Nothing about the task is hard-coded in the agent.

> Demo video: _add link here_

## How this maps to an AI-employee platform

| Platform layer | In Alfred | Where |
|---|---|---|
| Reasoning engine: plans multi-step actions | Model-driven loop; success criteria fixed before acting | `loop.py`, `worker.py` |
| Tool execution: acts in your systems | Browser and file tools; any model provider | `tools/`, `providers.py` |
| Company memory: learns your context | Handbook + lessons kept across runs | `workspace/HANDBOOK.md`, `lessons.json` |
| Permission layer: enforces access rules | Enforcer on every request: allow / approve / never | `policy.py`, `workspace/policy.json` |
| Human escalation: hands off when unsure | Questions and approvals; `needs_human` queue state | `worker.py`, `store.py` |
| Observability: logs every action | Append-only decision log, screenshots, HTML report | `store.py`, `trace.py` |
| Outcome you can trust | Independent read-only verification of every "done" | `verifier.py` |

| Roles with scoped access | Overseer dispatches sub-agents; a role is a list of connectors | `team.py`, `connectors.py` |

Not built: a workflow engine (the model plans each run from scratch), knowledge retrieval over documents,
and connectors beyond browser and files (see Roadmap).

## Setup

Requires Python 3.11+ and one way to reach a model:

| Provider | How | Status |
|---|---|---|
| Claude subscription | Claude Code installed and signed in (`claude`); no key | Run live end to end |
| Claude API | `ANTHROPIC_API_KEY` | Implemented; not run live (no key during development) |
| OpenAI | `OPENAI_API_KEY` | Implemented; not run live |
| Google Gemini | `GEMINI_API_KEY` | Implemented; not run live |

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # optional: falls back to installed Chrome or Edge
```

For the CLI, set a key in the environment (PowerShell: `$env:ANTHROPIC_API_KEY = "sk-ant-..."`), or just have
Claude Code signed in. `--provider` and `--model` override the automatic choice.

## Run: desktop app

```bash
python -m alfred.app
```

A native window opens. On first start it asks how to connect a model: paste a Claude, OpenAI or Gemini key
(validated against the provider before it is saved, and stored in the OS credential store, not in a file),
or choose the Claude subscription option. The demo company starts with the app. Type a task or pick an
example, press Run, and watch the worker's browser, its reasoning, every enforcer verdict and the
independent verification live. Questions and approvals appear as dialogs.

## Run: terminal

```bash
pip install -e .      # once: installs the `alfred` and `alfred-app` commands
alfred
```

`alfred` with no arguments opens an interactive session. On first start it asks which model to connect
(same choices as the desktop app, same keyring). Then type tasks in plain language; `/demo` starts the
demo company, `/headed` shows the browser, `/model` switches provider, `/add`, `/work`, `/status` and
`/answer` drive the queue, `/help` lists everything.

### Phone mode

```
alfred
/call +9198XXXXXXXX
```

Alfred rings that number, reports the result of the last task (or asks what you want if there is none),
takes the next task by voice, hangs up, does the work, and calls back with the result. It keeps going until
you say there is nothing else or do not pick up. Questions and approvals during the work still appear in
the terminal.

It needs `.env` filled in from `.env.example`: a LiveKit project, an outbound SIP trunk and a Google API key
(Gemini Live does the speech). `alfred/voice.py` has the two halves: a LiveKit agent that makes the call,
and `call_user()` which dispatches it and waits for what was said. What the voice model says changes
nothing; a spoken request only becomes work through its `submit_task` tool, and that task then runs through
the same worker, enforcer and verification as a typed one.

**Status:** credentials, the SIP trunk and the voice worker's registration were checked; a full call
(dial, speak, capture a task) has not been run end to end yet.

## Run: one-off commands

Terminal 1, the simulated company (intranet with a mail inbox and a bills system):

```bash
python -m sandbox --reset          # http://127.0.0.1:8000, --chaos off to disable injected failures
```

Terminal 2, the worker:

```bash
python -m alfred "Find the latest invoice from Kestrel Logistics, enter it into Ledger, and tell me the amount and due date." --headed
```

Useful flags: `--headed` shows the browser, `--slow 400` slows it down for a demo, `--approve ask|auto|deny`,
`--no-input`, `--max-steps N`, `--max-cost USD`, `--no-verify`.

### Unattended: the queue

The same worker can drain a queue with nobody watching. Anything it cannot safely finish is parked for a
human instead of guessed at:

```bash
python -m alfred add "Enter the latest Kestrel invoice into Ledger."
python -m alfred add "Mark the August Kestrel Logistics bill as paid."
python -m alfred work            # works every queued task, never prompts
python -m alfred status          # queue, escalations with their question, cost per verified success
python -m alfred answer 1 "Kestrel Logistics"          # reply to an escalation; the task is requeued
python -m alfred answer 2 "Yes, go ahead" --approve    # reply and pre-approve the gated action
python -m alfred work
```

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
   └───┬────────┘  └──────────────────────────────────┘
       │ every request the browser sends
   ┌───▼────────────────────────────┐
   │ policy.py  the enforcer        │──► allow / hold for human approval / refuse
   └───┬────────────────────────────┘
       ▼
   store.py   SQLite: task queue with leases · run records · append-only decision log
   trace.py   trace.jsonl, screenshots, report.html
```

There are two layers, kept strictly apart. The **deterministic layer** (queue, leases, enforcer, budgets,
verdict arithmetic, decision log) is plain code and SQL and costs no model calls. The **non-deterministic
layer** (the model) is invoked for one thing only: the judgment of how to carry out the task.

- **`loop.py`** — model decides, harness executes, model observes. It knows nothing about browsers or
  invoices. It owns control: step budget, "you are repeating yourself" detection, and the guarantee that
  every tool call gets an observation back, including failures.
- **`tools/browser.py`** — Playwright. The model sees a text rendering of the visible page where each
  interactive element has a ref (`[e12 link "Invoice KL-2026-0926…"]`) and acts by ref.
- **`tools/files.py`** — list and read anywhere on the machine (the worker runs locally, for its owner);
  writes are confined to the workspace so a run cannot overwrite the user's own files. PDFs are read as text.
- **`worker.py`** — the four control tools (`plan`, `remember`, `ask_user`, `finish`) and the outcome logic.
- **`verifier.py`** — independent check of a claimed success.
- **`policy.py`** — the enforcer: rules on the requests the browser actually sends.
- **`store.py`** — the queue (atomic leases, heartbeat, reclaim), run records, decision log, metrics.
- **`sandbox/`** — the simulated company. The agent never imports it; it only sees it through the browser.

## Overseer and sub-agents

```
                         request
                            │
                 ┌──────────▼──────────┐
                 │  overseer (team.py) │  never touches a system: splits, dispatches,
                 │  delegate · ask ·   │  reads results, re-dispatches or escalates, reports
                 │  finish             │
                 └───┬─────────────┬───┘
        brief + facts│             │brief + facts
              ┌──────▼─────┐ ┌─────▼──────┐
              │ operator   │ │ analyst    │   each sub-agent: fresh context, own plan,
              │ browser +  │ │ files only │   own independent verification
              │ files      │ │            │
              └────────────┘ └────────────┘
```

`--team` (CLI), `/team` (terminal session) or the "Overseer + sub-agents" box (desktop app) switches from a
single worker to this shape. It is the structure of Apiary, my earlier coding-agent fleet (dispatcher,
isolated agents, a control plane that has the last word), applied to operations work:

- **A role is a list of connectors, enforced in code.** The analyst is not told "do not use the browser";
  it is never handed browser tools.
- **Sub-agents are isolated.** Each gets only the brief and facts the overseer passes, not the original
  request or another agent's transcript, so one agent's confusion does not leak into the next.
- **The overseer proposes, the harness decides.** It cannot report `success` while any sub-task it
  dispatched ended otherwise with no later sub-agent succeeding; the call is rejected and it must
  re-delegate or report honestly.
- **Single worker stays the default.** For one job, an overseer only adds latency and cost.

## Connectors and roadmap

A connector is a named bundle of tools plus the credential it needs (`connectors.py` states the contract:
tools that fail readably, credentials from the keyring and never shown to the model, every write through
the enforcer and into the decision log, read-only tools for the verifier).

| Connector | Status |
|---|---|
| `browser` — any web application through a real browser | Built |
| `files` — read anywhere locally, write in the workspace, PDFs as text | Built |
| `gmail` — search, read, draft; sending is an approval-gated write | Planned |
| `google-sheets`, `google-docs`, `google-calendar` | Planned |
| `slack` — read channels, post messages and escalations | Planned |
| `http-api` — call a documented REST API instead of driving its UI | Planned |

The planned ones are declared, not stubbed: nothing in the repo pretends to send an email. Until a native
connector exists, the browser connector already reaches these products through their web UIs, slowly.
Adding one means writing its tools, an OAuth credential provider, and enforcer rules for its writes; the
loop, verifier, overseer, queue and decision log do not change. New roles then become one-line grants,
for example a `finance-clerk` with `browser + google-sheets` and a `comms` role with `gmail + slack`.

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

**Gates are code, not prompts.** The prompt asks the model to be careful; the enforcer does not ask. Every
request the worker's browser sends is routed through `policy.py`, and the verdict is made on the actual
operation (method and URL, plus the label of the control that triggered it), not on what the model says it
is about to do. Ordinary writes are allowed, consequential ones (pay, delete, approve, ...) are held for a
human, and anything on the workspace's never-list (`workspace/policy.json`) is refused even with approval.
This bounds the damage of prompt injection: the sandbox inbox contains an email telling "AI assistants" to
mark bills as paid, and even if a model fell for it, the request would stop at the gate. Every verdict on
a write is recorded, so the report lists exactly what the worker changed.

**Precision over recall.** When unsure, escalate. In queue mode a question or a needed approval does not
block a terminal: the task moves to `needs_human` with its question, the worker moves on, and
`alfred answer` requeues it with the reply attached. Only a verified success counts as `done`. A wrong
entry in a ledger costs more than a task that waits for a person.

**Lease before work; crash-safe by construction.** A queued task is claimed atomically and its lease is
renewed by a heartbeat on every agent step. If the process dies, the lease lapses, the supervisor pass
requeues the task, and the retry is told an earlier attempt may have partly finished so it checks state
before acting. After two interrupted attempts the task is escalated rather than retried blind.

**A supervisor the model cannot argue with.** Step budget, stall detection and a per-run cost cap
(`--max-cost`, checked before each model call) end a run that is going nowhere. `alfred status` reports
cost per verified success, fully loaded: spend on escalated and failed runs is included, because that is
the number that decides whether the worker is worth running.

**The decision log is written from run one.** Every model decision, observation, enforcer verdict and
reviewer verdict is appended to SQLite as it happens (and mirrored to `trace.jsonl`). Nothing consumes it
yet beyond the report and metrics; it is the raw material for evals and for learning which tasks are safe
to automate.

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
- A human is reachable eventually: at the terminal for `run`, or via `status` / `answer` for the queue.

## Known limitations

- **Verified live with one provider.** Two tasks (invoice entry; an approval-gated payment) were run end
  to end on Claude via Claude Code. The Anthropic-key, OpenAI and Gemini adapters are written against
  their SDKs but have not been run, and the cost cap only knows Claude prices.
- **Sub-agents run one at a time**, each in its own browser session (so each signs in again). The
  overseer adds a few model calls of overhead; it is worth it for compound requests, not single jobs.
- **The desktop app is a Python app in a native window**, not a packaged installer. It runs one task at
  a time; the queue is CLI-only.
- **Only exercised live in the sandbox.** Real sites bring iframes, shadow DOM, infinite scroll, popups
  and bot detection; the snapshot does not handle iframes or shadow DOM today.
- **The enforcer's default rules are keyword-based** (on the request path and the control label). A
  consequential endpoint with a bland path and a bland button needs a rule in `policy.json`; request
  bodies are not inspected.
- **The verifier is another LLM.** It reduces self-grading bias but can be wrong. It also cannot sign in
  (read-only), so if the session has expired it returns `inconclusive` and the worker must sign in again.
- **Lessons are written by the model** and are not reviewed; a poisoned page could try to get a bad
  lesson saved. The prompt forbids it, nothing enforces it.
- **Credentials sit in the handbook** in plain text. Fine for a sandbox, wrong for production.
- **Retries restart, they do not resume.** A reclaimed task starts a fresh run that is told to check
  state first. That is safe against systems with duplicate checks (like the sandbox), not in general.
- **One worker per browser session.** Leases make several workers on one queue safe, but nothing
  coordinates two workers touching the same record.
- The automated tests drive the full harness with a scripted stand-in for the model. They prove the
  machinery (loop, browser, gates, verification flow), not the quality of the model's decisions.

## What I would build next

1. An eval suite: 20–30 sandbox tasks with ground-truth checks via `/__sandbox/state`, run on every
   change, tracking success rate, steps and cost.
2. Enforcer rules on request payloads, with a preview of exactly what will be sent in the approval prompt.
3. Checkpoint the transcript so a reclaimed task resumes mid-run instead of restarting.
4. More tools behind the same interface: an HTTP/API tool, email sending, spreadsheets; a vision
   fallback for pages the text snapshot cannot represent.
5. A secrets broker so the model never sees credentials, and per-task scoped permissions.
6. Native connectors (Gmail, Sheets, Docs, Calendar, Slack, HTTP APIs) per the contract above, and
   sub-agents running in parallel under leases once two of them can no longer collide on one record.
7. A dashboard over the SQLite store: live timeline, approval inbox, decisions, metrics.
8. A triage step before dispatch: hard gates plus one cheap model call to route a task to the worker or
   straight to a human, using the decision log as its training signal.

## Models, APIs and components used

- **Model:** provider-neutral. The loop speaks one message format and `providers.py` adapts it to Claude
  (Anthropic Messages API with tool use, adaptive thinking and prompt caching), OpenAI (Chat Completions
  function calling), Gemini (function calling), or Claude Code. The live runs during development used
  Claude Opus 5.5 through Claude Code.
- **Claude Code as a model backend:** each step is one headless `claude -p` call with Claude Code's own
  tools switched off and the reply constrained to a JSON schema of tool calls, so Alfred's harness
  (enforcer, verifier, budgets) stays in charge. It uses the login of whoever runs it, on their own
  machine. That is fine for personal use and this demo; a product offered to other people should use
  API keys, since Anthropic does not allow third-party products to run on users' subscription logins.
- **pywebview** for the desktop window, **keyring** for credential storage.
- **Phone mode:** LiveKit Agents, an outbound SIP trunk, and Gemini Live for speech in and out.
- **Anthropic Python SDK** for the API call. The agent loop, tools, verifier and policy are written
  from scratch; no agent framework.
- **Playwright** (Chromium) for the browser, **pypdf** for reading PDFs, **rich** for the console.
- **Sandbox:** FastAPI + SQLite + reportlab (to generate the invoice PDFs). All data is fictional.
- Built with Claude Code as a coding assistant.

The control-plane ideas (two strictly separated layers, gates as code, lease-before-work, a supervisor,
an append-only decision log, precision over recall) are carried over from Apiary, an autonomous
coding-agent fleet I built earlier, and applied here to a browser-and-files worker.
