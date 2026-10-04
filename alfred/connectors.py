"""Connectors: how a worker reaches a system.

A connector is a named bundle of tools plus the credential it needs. Roles
(see team.py) are granted connectors by name, so "what can this AI employee
touch" is a list in code, not a sentence in a prompt.

Two connectors are built. The rest are the roadmap: each is declared here with
the contract it has to meet, and nothing pretends to work before it does.

Contract for a new connector:
  1. tools()       return Tool objects (alfred/tools/base.py). Failures become
                   ToolError so the model can read and react to them.
  2. credentials   come from the OS keyring via a provider seam, never from
                   the prompt, and are never returned to the model.
  3. writes        every state-changing call goes through the Enforcer
                   (allow / hold for approval / never) and is written to the
                   decision log, exactly as browser requests are today.
  4. verification  expose read-only tools so the independent verifier can
                   check the result without being able to change anything.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    summary: str
    status: str        # built | planned
    auth: str


CONNECTORS = {c.name: c for c in [
    ConnectorSpec("browser", "Any web application, through a real browser (text snapshot + element refs)",
                  "built", "site logins from the handbook"),
    ConnectorSpec("files", "Read files anywhere on this machine, write inside the workspace; PDFs as text",
                  "built", "none"),
    ConnectorSpec("gmail", "Search, read and draft mail; sending is an approval-gated write",
                  "planned", "Google OAuth"),
    ConnectorSpec("google-sheets", "Read ranges, append and update rows", "planned", "Google OAuth"),
    ConnectorSpec("google-docs", "Read documents, create and edit drafts", "planned", "Google OAuth"),
    ConnectorSpec("google-calendar", "Read availability, propose and create events", "planned", "Google OAuth"),
    ConnectorSpec("slack", "Read channels, post messages and escalations", "planned", "Slack app token"),
    ConnectorSpec("http-api", "Call a documented REST API directly instead of driving its UI",
                  "planned", "API key per service"),
]}


def built() -> list[str]:
    return [c.name for c in CONNECTORS.values() if c.status == "built"]
