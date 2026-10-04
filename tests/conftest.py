"""Test fixtures: a live sandbox on a free port, and a scripted stand-in for the model.

The scripted model lets the whole harness (loop, real browser, real sandbox,
verifier, approval gate) run end to end without an API key. It proves the
machinery; it says nothing about how well a real model decides.
"""
from __future__ import annotations

import re
import socket
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
import uvicorn

from sandbox import seed
from sandbox.app import create_app


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    data = tmp_path / "sandbox-data"
    monkeypatch.setattr(seed, "DATA_DIR", data)
    monkeypatch.setattr(seed, "DB_PATH", data / "ledger.db")
    monkeypatch.setattr(seed, "ATTACH_DIR", data / "attachments")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(chaos=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}", app=app)
    server.should_exit = True
    thread.join(timeout=5)


def call(name: str, **args):
    return SimpleNamespace(type="tool_use", id=f"toolu_{uuid.uuid4().hex[:12]}", name=name, input=args)


def say(text: str):
    return SimpleNamespace(type="text", text=text)


def ref(snapshot: str, kind: str, label: str) -> str:
    """Find an element ref in a snapshot the way a model would: by kind and label."""
    m = re.search(rf'\[(e\d+) {kind}\S* "{re.escape(label)}"', snapshot)
    assert m, f"no {kind} labelled {label!r} in:\n{snapshot}"
    return m.group(1)


class ScriptedLLM:
    """Plays back a list of steps. A step is a list of blocks, or a function of the last observation."""

    def __init__(self, worker: list, verifier: list | None = None):
        self.scripts = {"worker": iter(worker), "verifier": iter(verifier or [])}
        self.usage = SimpleNamespace(calls=0)
        self.observations: list[str] = []

    def complete(self, system: str, messages: list, tools: list):
        self.usage.calls += 1
        content = messages[-1]["content"]
        last = content if isinstance(content, str) else "\n".join(
            b.get("content") or b.get("text") or "" for b in content if isinstance(b, dict))
        self.observations.append(last)
        role = "verifier" if system.startswith("You are an independent reviewer") else "worker"
        step = next(self.scripts[role])
        blocks = step(last) if callable(step) else step
        return SimpleNamespace(content=blocks, stop_reason="tool_use" if any(b.type == "tool_use" for b in blocks)
                               else "end_turn", usage=SimpleNamespace())
