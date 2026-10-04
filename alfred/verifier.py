"""Independent verification.

"The agent said it worked" is not evidence. When the worker claims success, a
second agent with a fresh context and read-only tools checks the systems of
record against the success criteria that were fixed before any action was
taken. It never sees the worker's transcript, so it cannot inherit the
worker's mistakes or its confidence.
"""
from __future__ import annotations

from .loop import run_loop
from .prompts import VERIFIER_SYSTEM, verifier_brief
from .tools import Tool, ToolError, ToolResult, Toolset
from .tools.browser import BrowserSession
from .tools.files import Workspace
from .trace import Trace

RESULTS = ("pass", "fail", "unknown")
OVERALL = ("pass", "fail", "inconclusive")


def _submit_verdict(checks: list, overall: str, feedback: str = "") -> ToolResult:
    if overall not in OVERALL:
        raise ToolError(f"overall must be one of {OVERALL}.")
    if not isinstance(checks, list) or not checks:
        raise ToolError("checks must list every criterion you evaluated.")
    clean = []
    for c in checks:
        if not isinstance(c, dict) or c.get("result") not in RESULTS or not c.get("criterion"):
            raise ToolError(f"Each check needs a criterion, a result in {RESULTS}, and what you observed.")
        clean.append({"criterion": str(c["criterion"]), "result": c["result"], "observed": str(c.get("observed", ""))})
    # The harness, not the model, has the last word on the overall result.
    if any(c["result"] == "fail" for c in clean):
        overall = "fail"
    elif any(c["result"] == "unknown" for c in clean):
        overall = "inconclusive"
    else:
        overall = "pass"
    return ToolResult("Verdict recorded.", final=True, data={"checks": clean, "overall": overall, "feedback": feedback})


def verify(*, llm, task: str, today: str, handbook: str, plan: dict, claim: dict, session: BrowserSession,
           workspace: Workspace, trace: Trace, max_steps: int = 20) -> dict:
    browser = session.open("verifier", read_only=True)
    try:
        tools = Toolset(browser.tools() + workspace.tools(read_only=True) + [Tool(
            "submit_verdict", "Record your verdict and end the review.",
            {"checks": {"type": "array", "items": {"type": "object", "properties": {
                "criterion": {"type": "string"},
                "result": {"type": "string", "enum": list(RESULTS)},
                "observed": {"type": "string", "description": "What you saw, with the actual values and where."}},
                "required": ["criterion", "result", "observed"]}},
             "overall": {"type": "string", "enum": list(OVERALL)},
             "feedback": {"type": "string", "description": "For the worker: what to fix, or what blocked you."}},
            _submit_verdict, required=("checks", "overall"))])
        messages = [{"role": "user", "content": verifier_brief(task, today, handbook, plan, claim)}]
        end = run_loop(llm=llm, system=VERIFIER_SYSTEM, messages=messages, tools=tools, trace=trace,
                       role="verifier", max_steps=max_steps)
    finally:
        browser.page.close()
    if end.reason == "final":
        verdict = end.result.data
    else:
        verdict = {"checks": [], "overall": "inconclusive",
                   "feedback": f"The reviewer did not reach a verdict ({end.reason})."}
    trace.event("verdict", role="verifier", **verdict)
    return verdict
