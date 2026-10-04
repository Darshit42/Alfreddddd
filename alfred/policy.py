"""Human-approval policy.

The model is asked to check in before risky actions, but that is a request,
not a guarantee. This gate is enforced by the harness: a click on a control
whose label looks consequential or irreversible is paused until a human says
yes, no matter what the model (or a prompt-injected web page) wants.
"""
from __future__ import annotations

import re
from typing import Callable

RISKY_LABEL = re.compile(
    r"\b(pay|paid|payment|delete|remove|void|refund|transfer|approve|reject|terminate|wire)\b", re.I)

# (action description, page url) -> approved?
Approver = Callable[[str, str], bool]


def needs_approval(control_label: str) -> bool:
    return bool(RISKY_LABEL.search(control_label))


def auto_approve(action: str, url: str) -> bool:
    return True


def always_deny(action: str, url: str) -> bool:
    return False
