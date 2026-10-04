"""The agent loop: model decides, harness executes, model observes, repeat.

Used unchanged by both the worker and the verifier. The loop itself knows
nothing about browsers, invoices or any particular task. Its job is control:
step budget, stall detection, and making sure every tool call gets an
observation back.

The message list is append-only. Nothing is ever rewritten or trimmed, which
keeps prompt caching effective and keeps the model's earlier reasoning valid.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from .tools import ToolResult, Toolset
from .trace import Trace

NO_TOOL_NUDGE = ("You replied without calling a tool, so nothing happened. Nobody reads plain replies: act with a "
                 "tool, or end the run with the closing tool and an honest status.")
REPEAT_NUDGE = ("[harness] You have made the same call three times and got the same result. Repeating it will not "
                "help. Re-read the page, try a different approach, or ask for help.")
BUDGET_NUDGE = ("[harness] Only {n} steps remain in this run's budget. Wrap up now: close the run with an honest "
                "status describing what is done and what is not.")


@dataclass
class LoopEnd:
    reason: str                 # final | max_steps | stalled | refusal
    result: ToolResult | None
    steps: int


def run_loop(*, llm, system: str, messages: list, tools: Toolset, trace: Trace, role: str,
             max_steps: int) -> LoopEnd:
    idle_turns = 0
    last_call, streak = None, 0

    for step in range(1, max_steps + 1):
        response = llm.complete(system, messages, tools.schemas())
        blocks = list(response.content or [])
        trace.event(
            "llm", role=role, step=step, stop_reason=response.stop_reason,
            thinking="\n".join(b.thinking for b in blocks if b.type == "thinking" and getattr(b, "thinking", "")),
            text="\n".join(b.text for b in blocks if b.type == "text" and b.text.strip()))

        if response.stop_reason == "refusal":
            return LoopEnd("refusal", None, step)
        if blocks:
            messages.append({"role": "assistant", "content": blocks})

        calls = [b for b in blocks if b.type == "tool_use"]
        if not calls:
            idle_turns += 1
            if idle_turns > 2:
                return LoopEnd("stalled", None, step)
            messages.append({"role": "user", "content": NO_TOOL_NUDGE})
            continue

        results, notes, final = [], [], None
        for call in calls:
            if final is not None:
                # Every tool_use must be answered, even ones made moot by the run ending.
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": "Not run: the run had already ended.", "is_error": True})
                continue
            started = time.time()
            res = tools.call(call.name, call.input)
            trace.event("tool", role=role, step=step, name=call.name, args=call.input, result=res.text,
                        is_error=res.is_error, ms=int((time.time() - started) * 1000), artifact=res.artifact)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": res.text,
                            "is_error": res.is_error})
            if res.final:
                final = res
                continue
            signature = hashlib.sha1(
                (call.name + json.dumps(call.input, sort_keys=True, default=str) + res.text).encode()).hexdigest()
            streak = streak + 1 if signature == last_call else 1
            last_call = signature
            if streak == 3:
                notes.append(REPEAT_NUDGE)
                trace.event("harness", role=role, note="Same call, same result, three times: nudged the model.")

        if final is not None:
            messages.append({"role": "user", "content": results})
            return LoopEnd("final", final, step)
        if streak >= 6:
            messages.append({"role": "user", "content": results})
            return LoopEnd("stalled", None, step)
        if max_steps - step == 3:
            notes.append(BUDGET_NUDGE.format(n=3))
        messages.append({"role": "user", "content": results + [{"type": "text", "text": n} for n in notes]})

    return LoopEnd("max_steps", None, max_steps)
