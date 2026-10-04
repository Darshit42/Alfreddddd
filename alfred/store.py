"""Durable state: the task queue, run records and the decision log, in one SQLite file.

This is the deterministic half of the system. Leasing, retries, escalation
and bookkeeping are plain SQL and cost no model calls. The model is invoked
only for the judgment call: "carry out this task".

- Lease before work. A task is claimed atomically and the lease is renewed by
  a heartbeat on every agent step. If the process dies, the lease lapses and
  the task becomes claimable again, so nothing is lost and nothing is run twice
  at once.
- The decision log is append-only. Every model decision, tool observation,
  enforcer verdict and reviewer verdict is a row, written as it happens.
- Escalation is a state, not an exception. A task the worker cannot safely
  finish waits in needs_human with its question until someone answers it.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

LEASE_SECONDS = 300
MAX_ATTEMPTS = 2

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task          TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'queued',   -- queued | leased | done | needs_human | failed
    attempts      INTEGER NOT NULL DEFAULT 0,
    history       TEXT NOT NULL DEFAULT '[]',       -- json: earlier outcomes and the human's replies
    grant_approval INTEGER NOT NULL DEFAULT 0,      -- a human pre-approved the gated action for the next attempt
    lease_owner   TEXT,
    lease_expires TEXT,
    result        TEXT,                              -- final (or blocking) summary
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,                  -- run directory name
    task_id       INTEGER REFERENCES tasks(id),
    task          TEXT NOT NULL,
    status        TEXT,                              -- success | unverified | partial | failed | needs_user | incomplete
    verified      INTEGER,
    steps         INTEGER,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0,
    summary       TEXT,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at      TEXT
);
CREATE TABLE IF NOT EXISTS decisions (               -- append-only
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    at      TEXT NOT NULL DEFAULT (datetime('now')),
    role    TEXT,
    kind    TEXT NOT NULL,                           -- llm | tool | plan | write | verdict | harness | task
    detail  TEXT NOT NULL                            -- json
);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)  # autocommit; transactions are explicit
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.owner = f"worker-{os.getpid()}"

    # ------------------------------------------------------------------ decision log and runs
    def start_run(self, run_id: str, task: str, task_id: int | None = None) -> None:
        self.db.execute("INSERT INTO runs (id, task_id, task) VALUES (?,?,?)", (run_id, task_id, task))

    def log(self, run_id: str, event: dict) -> None:
        detail = {k: v for k, v in event.items() if k not in ("kind", "role")}
        self.db.execute("INSERT INTO decisions (run_id, role, kind, detail) VALUES (?,?,?,?)",
                        (run_id, event.get("role"), event["kind"], json.dumps(detail, ensure_ascii=False, default=str)))

    def end_run(self, run_id: str, outcome: dict) -> None:
        u = outcome.get("usage", {})
        self.db.execute(
            "UPDATE runs SET status=?, verified=?, steps=?, input_tokens=?, output_tokens=?, cost_usd=?, summary=?, "
            "ended_at=datetime('now') WHERE id=?",
            (outcome["status"], outcome.get("verified"), outcome.get("steps"),
             u.get("input_tokens", 0) + u.get("cache_read_tokens", 0) + u.get("cache_write_tokens", 0),
             u.get("output_tokens", 0), outcome.get("cost_usd", 0), outcome.get("summary"), run_id))

    # ------------------------------------------------------------------ queue
    def add(self, task: str) -> int:
        return self.db.execute("INSERT INTO tasks (task) VALUES (?)", (task,)).lastrowid

    def reclaim_expired(self) -> list[int]:
        """Supervisor duty: a lapsed lease means the worker died mid-task. Requeue it, or escalate if it keeps happening."""
        rows = self.db.execute("SELECT id, attempts FROM tasks WHERE state='leased' AND lease_expires < datetime('now')").fetchall()
        for r in rows:
            if r["attempts"] >= MAX_ATTEMPTS:
                self._close(r["id"], "needs_human", f"Abandoned after {r['attempts']} interrupted attempts; needs a person to look.")
            else:
                self.db.execute("UPDATE tasks SET state='queued', lease_owner=NULL, updated_at=datetime('now') "
                                "WHERE id=? AND state='leased'", (r["id"],))
        return [r["id"] for r in rows]

    def lease_next(self) -> sqlite3.Row | None:
        """Atomically claim the oldest queued task. Safe with several workers on one database."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if row is not None:
                self.db.execute(
                    "UPDATE tasks SET state='leased', lease_owner=?, attempts=attempts+1, "
                    "lease_expires=datetime('now', ?), updated_at=datetime('now') WHERE id=?",
                    (self.owner, f"+{LEASE_SECONDS} seconds", row["id"]))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return row

    def heartbeat(self, task_id: int) -> None:
        self.db.execute("UPDATE tasks SET lease_expires=datetime('now', ?) WHERE id=? AND lease_owner=? AND state='leased'",
                        (f"+{LEASE_SECONDS} seconds", task_id, self.owner))

    def _close(self, task_id: int, state: str, result: str) -> None:
        self.db.execute("UPDATE tasks SET state=?, result=?, lease_owner=NULL, lease_expires=NULL, grant_approval=0, "
                        "updated_at=datetime('now') WHERE id=?", (state, result, task_id))

    def complete(self, task_id: int, outcome: dict) -> str:
        """Map a run outcome to a queue state. Only a verified success is 'done'."""
        status = outcome["status"]
        state = {"success": "done", "failed": "failed"}.get(status, "needs_human")
        row = self.db.execute("SELECT history FROM tasks WHERE id=?", (task_id,)).fetchone()
        history = json.loads(row["history"]) + [{"outcome": status, "summary": outcome.get("summary", "")}]
        self.db.execute("UPDATE tasks SET history=? WHERE id=?", (json.dumps(history, ensure_ascii=False), task_id))
        self._close(task_id, state, outcome.get("summary", ""))
        return state

    def answer(self, task_id: int, reply: str, grant_approval: bool = False) -> bool:
        """A human responds to an escalated task; it goes back in the queue with their reply attached."""
        row = self.db.execute("SELECT history, state FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["state"] != "needs_human":
            return False
        history = json.loads(row["history"]) + [{"human": reply}]
        self.db.execute("UPDATE tasks SET state='queued', attempts=0, history=?, grant_approval=?, "
                        "updated_at=datetime('now') WHERE id=?",
                        (json.dumps(history, ensure_ascii=False), int(grant_approval), task_id))
        return True

    # ------------------------------------------------------------------ reporting
    def tasks(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM tasks ORDER BY id").fetchall()

    def metrics(self) -> dict:
        r = self.db.execute("""
            SELECT COUNT(*) AS runs,
                   SUM(status = 'success') AS verified,
                   SUM(status IN ('needs_user', 'unverified', 'partial', 'incomplete')) AS escalated,
                   SUM(status = 'failed') AS failed,
                   COALESCE(SUM(cost_usd), 0) AS cost,
                   COALESCE(AVG(steps), 0) AS avg_steps
            FROM runs WHERE ended_at IS NOT NULL""").fetchone()
        m = dict(r)
        m = {k: (v or 0) for k, v in m.items()}
        # Fully loaded: all spend, including runs that escalated or failed, divided by verified successes.
        m["cost_per_verified"] = m["cost"] / m["verified"] if m["verified"] else None
        return m


def prior_attempts_note(history_json: str, attempts: int) -> str:
    """Context for a retried or answered task, so the worker resumes instead of starting blind."""
    history = json.loads(history_json or "[]")
    lines = []
    for h in history:
        if "human" in h:
            lines.append(f"- Your colleague replied: {h['human']}")
        else:
            lines.append(f"- An earlier attempt ended as '{h['outcome']}': {h['summary']}")
    if attempts > 1 and not lines:
        lines.append("- An earlier attempt at this task was interrupted before it finished.")
    if not lines:
        return ""
    return ("<earlier_attempts>\n" + "\n".join(lines) + "\nSome of the work may already be done. Check the current "
            "state of the systems before acting so that nothing is done twice.\n</earlier_attempts>")
