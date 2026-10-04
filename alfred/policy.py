"""The enforcer: gates are code, not prompts.

The model is asked to be careful, but that is a request. This is the
guarantee. Every request the worker's browser tries to send passes through
here, and the verdict is made on the actual operation (method + URL, plus the
label of the control that triggered it), never on what the model says it is
about to do. So it also catches a risky action hiding behind an innocuous
button, and it holds no matter what a prompt-injected page talks the model into.

Three verdicts for a state-changing request:
  allow    ordinary work (saving a form, signing in)
  approve  consequential or irreversible: paused until a human says yes
  refuse   on the workspace's never-list: not possible even with approval

Every verdict on a write is recorded in the decision log.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

RISKY = re.compile(r"\b(pay|paid|payment|delete|remove|void|refund|transfer|approve|reject|terminate|wire)\b", re.I)

# (action description, page url) -> approved?
Approver = Callable[[str, str], bool]


def auto_approve(action: str, url: str) -> bool:
    return True


def always_deny(action: str, url: str) -> bool:
    return False


class Enforcer:
    def __init__(self, approver: Approver = always_deny, never: list[str] | None = None,
                 approve: list[str] | None = None, log: Callable[..., object] | None = None):
        self.approver = approver
        self.never = [re.compile(p) for p in never or []]
        self.approve = [re.compile(p) for p in approve or []]
        self.log = log or (lambda **kw: None)

    @classmethod
    def from_workspace(cls, workspace: Path, approver: Approver, log=None) -> "Enforcer":
        """Environment-specific rules live with the workspace (policy.json), like the handbook does."""
        path = workspace / "policy.json"
        rules = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return cls(approver, rules.get("never"), rules.get("approve"), log)

    def check(self, method: str, url: str, label: str = "", page_url: str = "") -> tuple[bool, str]:
        """Return (allowed, reason). Reads are only subject to the never-list."""
        parts = urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        write = method not in ("GET", "HEAD", "OPTIONS")
        if any(p.search(target) for p in self.never):
            self.log(method=method, url=url, label=label, verdict="refused")
            return False, f"Refused by policy: {parts.path} is off limits to the worker. This cannot be approved."
        if not write:
            return True, ""
        risky = RISKY.search(label) or RISKY.search(parts.path) or any(p.search(target) for p in self.approve)
        if not risky:
            self.log(method=method, url=url, label=label, verdict="allowed")
            return True, ""
        action = f"{method} {parts.path}" + (f' (after clicking "{label}")' if label else "")
        if self.approver(action, page_url or url):
            self.log(method=method, url=url, label=label, verdict="approved")
            return True, ""
        self.log(method=method, url=url, label=label, verdict="denied")
        return False, (f"Not done: {action} needs human approval and it was not given. Do not look for another "
                       "way to do the same thing. If the task depends on it, finish with status needs_user and "
                       "say exactly what needs approving.")
