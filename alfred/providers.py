"""Model providers. The agent loop is provider-neutral: it speaks one message
format (text / tool_use / tool_result blocks) and each adapter translates.

  anthropic    Claude via API key (alfred/llm.py, the reference implementation)
  claude-code  Claude via the locally installed Claude Code CLI and its login,
               for people with a Claude subscription and no API key
  openai       OpenAI via API key
  gemini       Google Gemini via API key
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

from .llm import ClaudeLLM, Usage

PROVIDERS = {"anthropic": "Claude (API key)", "claude-code": "Claude subscription (Claude Code login)",
             "openai": "OpenAI (API key)", "gemini": "Google Gemini (API key)"}
ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}
# Tried in order against the provider's own model list; the first one that exists is the default.
PREFERRED = {"openai": ("gpt-5.2", "gpt-5.1", "gpt-5", "gpt-4.1", "gpt-4o"),
             "gemini": ("gemini-3-pro", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash")}


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, args: dict, call_id: str | None = None):
    return SimpleNamespace(type="tool_use", id=call_id or f"call_{uuid.uuid4().hex[:12]}", name=name, input=args)


def reply(blocks: list, native=None):
    return SimpleNamespace(content=blocks, native=native, usage=None,
                           stop_reason="tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")


def _user_parts(content) -> tuple[list[dict], str]:
    """Split a user turn into its tool results and any plain text."""
    if isinstance(content, str):
        return [], content
    results = [b for b in content if b.get("type") == "tool_result"]
    text = "\n".join(b["text"] for b in content if b.get("type") == "text")
    return results, text


# --------------------------------------------------------------------------- Claude Code (subscription login)
class ClaudeCodeLLM:
    """Uses the user's own Claude Code install as the model.

    Each step is one headless `claude -p` call with Claude Code's built-in tools
    switched off and the reply constrained to a JSON schema of tool calls, so the
    harness (enforcer, verifier, budgets) stays in charge. Claude Code keeps the
    conversation in a session that is resumed on every step.
    """

    def __init__(self, model: str | None = None):
        self.exe = shutil.which("claude")
        if not self.exe:
            raise RuntimeError("Claude Code is not installed (no 'claude' on PATH).")
        self.model = "" if (model or "").startswith("(") else (model or os.environ.get("ALFRED_MODEL") or "")
        self.cwd = Path.home() / ".alfred" / "claude-code"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.sessions: dict[int, tuple[str, int]] = {}   # conversation -> (session id, messages already sent)
        self.names: dict[str, str] = {}                  # tool call id -> tool name
        self.usage = Usage()
        self._cost = 0.0

    def cost(self) -> float:
        return self._cost   # list-price equivalent reported by Claude Code; a subscription is not billed per call

    def complete(self, system: str, messages: list, tools: list):
        session, sent = self.sessions.get(id(messages), (None, 0))
        prompt = "\n\n".join(self._render(m["content"]) for m in messages[sent:] if m["role"] == "user")
        catalogue = "\n".join(f"- {t['name']}: {t['description']}\n  input schema: {json.dumps(t['input_schema'])}"
                              for t in tools)
        schema = {"type": "object", "required": ["say", "calls"], "properties": {
            "say": {"type": "string"},
            "calls": {"type": "array", "items": {"type": "object", "required": ["name", "input"], "properties": {
                "name": {"type": "string", "enum": [t["name"] for t in tools]}, "input": {"type": "object"}}}}}}
        cmd = [self.exe, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
               "--json-schema", json.dumps(schema), "--system-prompt",
               f"{system}\n\n# Tools\nYou act only by calling these tools:\n{catalogue}\n\n"
               "Reply through the structured output. `say`: one or two sentences on what you just observed and "
               "why you are taking the next action. `calls`: the tool calls to make now, each with a tool name "
               "and an input object matching that tool's schema. Tool results arrive in the next message."]
        if session:
            cmd += ["--resume", session]
        if self.model:
            cmd += ["--model", self.model]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              cwd=self.cwd, timeout=600)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Claude Code failed: {(proc.stderr or proc.stdout).strip()[:400]}") from None
        if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
            raise RuntimeError(f"Claude Code error: {str(out.get('result'))[:400]} "
                               "(if this is a login problem, run `claude` once and sign in)")
        self.sessions[id(messages)] = (out["session_id"], len(messages))
        self.usage.add(SimpleNamespace(**{k: v for k, v in out.get("usage", {}).items() if isinstance(v, int)}))
        self._cost += out.get("total_cost_usd") or 0.0
        data = out["structured_output"]
        blocks = [text_block(data["say"])] if data.get("say") else []
        for c in data.get("calls", []):
            block = tool_block(c["name"], c.get("input") or {})
            self.names[block.id] = block.name
            blocks.append(block)
        return reply(blocks)

    def _render(self, content) -> str:
        results, text = _user_parts(content)
        parts = []
        for r in results:
            flag = ' error="true"' if r.get("is_error") else ""
            parts.append(f'<tool_result tool="{self.names.get(r["tool_use_id"], "?")}"{flag}>\n'
                         f'{r["content"]}\n</tool_result>')
        return "\n\n".join(parts + ([text] if text else []))


# --------------------------------------------------------------------------- OpenAI
class OpenAILLM:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"), max_retries=4)
        self.model = model or os.environ.get("ALFRED_MODEL") or PREFERRED["openai"][0]
        self.usage = Usage()

    def cost(self) -> float:
        return 0.0   # no price table for this provider: the step budget is the cap

    def complete(self, system: str, messages: list, tools: list):
        msgs: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant":
                calls = [{"id": b.id, "type": "function",
                          "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                         for b in m["content"] if b.type == "tool_use"]
                text = "\n".join(b.text for b in m["content"] if b.type == "text")
                msgs.append({"role": "assistant", "content": text or None, **({"tool_calls": calls} if calls else {})})
            else:
                results, text = _user_parts(m["content"])
                msgs += [{"role": "tool", "tool_call_id": r["tool_use_id"], "content": r["content"]} for r in results]
                if text:
                    msgs.append({"role": "user", "content": text})
        resp = self.client.chat.completions.create(
            model=self.model, messages=msgs,
            tools=[{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                      "parameters": t["input_schema"]}} for t in tools])
        msg = resp.choices[0].message
        self.usage.add(SimpleNamespace(input_tokens=resp.usage.prompt_tokens, output_tokens=resp.usage.completion_tokens))
        blocks = [text_block(msg.content)] if msg.content else []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": tc.function.arguments}   # surfaces as a tool error the model can fix
            blocks.append(tool_block(tc.function.name, args, tc.id))
        return reply(blocks)


# --------------------------------------------------------------------------- Gemini
class GeminiLLM:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        from google import genai
        self.client = genai.Client(api_key=api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        self.model = model or os.environ.get("ALFRED_MODEL") or PREFERRED["gemini"][1]
        self.usage = Usage()
        self.names: dict[str, str] = {}

    def cost(self) -> float:
        return 0.0

    def complete(self, system: str, messages: list, tools: list):
        from google.genai import types
        contents = []
        for m in messages:
            if m["role"] == "assistant":
                # Replay Gemini's own turn object: it carries the thought signatures tool calling needs.
                native = getattr(m["content"][0], "native", None)
                contents.append(native or types.Content(role="model", parts=[
                    types.Part.from_text(text=b.text) for b in m["content"] if b.type == "text"]))
            else:
                results, text = _user_parts(m["content"])
                parts = [types.Part.from_function_response(
                    name=self.names.get(r["tool_use_id"], "tool"),
                    response={"error" if r.get("is_error") else "result": r["content"]}) for r in results]
                if text:
                    parts.append(types.Part.from_text(text=text))
                contents.append(types.Content(role="user", parts=parts))
        resp = self.client.models.generate_content(
            model=self.model, contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                    name=t["name"], description=t["description"], parameters_json_schema=t["input_schema"])
                    for t in tools])],
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        um = resp.usage_metadata
        self.usage.add(SimpleNamespace(input_tokens=getattr(um, "prompt_token_count", 0),
                                       output_tokens=getattr(um, "candidates_token_count", 0)))
        content = resp.candidates[0].content if resp.candidates else None
        blocks = []
        for part in (content.parts if content and content.parts else []):
            if part.function_call:
                block = tool_block(part.function_call.name, dict(part.function_call.args or {}), part.function_call.id)
                self.names[block.id] = block.name
                blocks.append(block)
            elif part.text and not getattr(part, "thought", False):
                blocks.append(text_block(part.text))
        if blocks:
            blocks[0].native = content
        return reply(blocks)


# --------------------------------------------------------------------------- factory
def detect_provider() -> str | None:
    """Pick a provider from the environment: an explicit choice, else whichever credential exists."""
    if os.environ.get("ALFRED_PROVIDER"):
        return os.environ["ALFRED_PROVIDER"]
    for provider, env in ENV_KEYS.items():
        if os.environ.get(env):
            return provider
    return "claude-code" if shutil.which("claude") else None


def make_llm(provider: str, api_key: str | None = None, model: str | None = None):
    if provider == "anthropic":
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        return ClaudeLLM(**({"model": model} if model else {}))
    if provider == "claude-code":
        return ClaudeCodeLLM(model)
    if provider == "openai":
        return OpenAILLM(api_key, model)
    if provider == "gemini":
        return GeminiLLM(api_key, model)
    raise ValueError(f"Unknown provider '{provider}'. Choose from: {', '.join(PROVIDERS)}.")


def list_models(provider: str, api_key: str | None = None) -> list[str]:
    """Validate a credential by asking the provider what models it can use. Raises on a bad key."""
    if provider == "anthropic":
        import anthropic
        return [m.id for m in anthropic.Anthropic(api_key=api_key).models.list(limit=50)]
    if provider == "openai":
        from openai import OpenAI
        ids = sorted(m.id for m in OpenAI(api_key=api_key).models.list() if m.id.startswith(("gpt-", "o")))
    elif provider == "gemini":
        from google import genai
        ids = sorted(m.name.removeprefix("models/") for m in genai.Client(api_key=api_key).models.list()
                     if "gemini" in m.name)
    elif provider == "claude-code":
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("Claude Code is not installed. Install it and sign in with your Claude account.")
        out = subprocess.run([exe, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config", "Say OK."],
                             capture_output=True, text=True, encoding="utf-8", timeout=120,
                             cwd=Path.home())
        try:
            ok = not json.loads(out.stdout).get("is_error")
        except json.JSONDecodeError:
            ok = False
        if not ok:
            raise RuntimeError("Claude Code is installed but not signed in. Run `claude` in a terminal and log in.")
        return ["(Claude Code default)", "opus", "sonnet", "haiku"]
    else:
        raise ValueError(provider)
    preferred = [p for p in PREFERRED[provider] if p in ids]
    return preferred + [i for i in ids if i not in preferred]
