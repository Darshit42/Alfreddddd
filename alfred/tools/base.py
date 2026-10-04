"""Tool plumbing shared by the worker and the verifier.

A tool is a plain Python function plus a JSON schema. Tools never raise into
the agent loop: every failure becomes an error result the model can read and
react to, because "what went wrong" is exactly the observation it needs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(Exception):
    """An expected, explainable failure. The message is shown to the model."""


@dataclass
class ToolResult:
    text: str
    is_error: bool = False
    final: bool = False          # ends the loop (finish / submit_verdict)
    data: dict = field(default_factory=dict)
    artifact: str | None = None  # e.g. screenshot path, for the trace


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    fn: Callable[..., "str | ToolResult"]
    required: tuple[str, ...] = ()
    idempotent: bool = False  # safe to retry automatically on a transient failure

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {"type": "object", "properties": self.properties, "required": list(self.required)},
        }


class Toolset:
    def __init__(self, tools: list[Tool]):
        self.tools = {t.name: t for t in tools}

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def call(self, name: str, args: Any) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool '{name}'. Available: {', '.join(self.tools)}.", is_error=True)
        if not isinstance(args, dict):
            return ToolResult("Tool input must be a JSON object.", is_error=True)
        missing = [k for k in tool.required if k not in args]
        unknown = [k for k in args if k not in tool.properties]
        if missing or unknown:
            problems = ([f"missing required: {', '.join(missing)}"] if missing else []) + \
                       ([f"unknown parameters: {', '.join(unknown)}"] if unknown else [])
            return ToolResult(f"Invalid input for {name} ({'; '.join(problems)}).", is_error=True)

        attempts = 2 if tool.idempotent else 1
        for attempt in range(1, attempts + 1):
            try:
                out = tool.fn(**args)
                return out if isinstance(out, ToolResult) else ToolResult(str(out))
            except ToolError as e:
                return ToolResult(str(e), is_error=True)
            except Exception as e:  # noqa: BLE001 - anything else is still just an observation for the model
                transient = "Timeout" in type(e).__name__ or "net::" in str(e)
                if transient and attempt < attempts:
                    time.sleep(1.0)
                    continue
                first_line = str(e).strip().splitlines()[0] if str(e).strip() else ""
                note = " (retried once automatically)" if attempt > 1 else ""
                return ToolResult(f"{type(e).__name__}: {first_line}{note}", is_error=True)
        raise AssertionError("unreachable")
