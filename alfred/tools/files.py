"""File tools, confined to the workspace directory."""
from __future__ import annotations

from pathlib import Path

from .base import Tool, ToolError

MAX_CHARS = 20_000


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"'{path}' is outside the workspace. Use paths relative to the workspace root.")
        return p

    def rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root).as_posix()

    # ------------------------------------------------------------------ tools
    def list_files(self, path: str = ".") -> str:
        d = self.resolve(path)
        if not d.is_dir():
            raise ToolError(f"'{path}' is not a directory.")
        entries = sorted(d.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        lines = [f"{self.rel(e)}/" if e.is_dir() else f"{self.rel(e)}  ({e.stat().st_size} bytes)"
                 for e in entries if not e.name.startswith(".")]
        return "\n".join(lines) or "(empty)"

    def read_file(self, path: str) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"No such file: '{path}'. Use list_files to see what exists.")
        if p.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            pages = [page.extract_text() or "" for page in PdfReader(str(p)).pages]
            text = "\n\n".join(f"[page {i}]\n{t.strip()}" for i, t in enumerate(pages, 1))
            if not text.strip():
                raise ToolError("The PDF has no extractable text (it may be a scan).")
        else:
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise ToolError(f"'{path}' is not a text file.") from None
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + f"\n[truncated: file has {len(text) - MAX_CHARS} more characters]"
        return text

    def write_file(self, path: str, content: str) -> str:
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {self.rel(p)}."

    def tools(self, read_only: bool = False) -> list[Tool]:
        tools = [
            Tool("list_files", "List files in a workspace directory. Downloads from the browser land in 'downloads/'.",
                 {"path": {"type": "string", "description": "Directory relative to the workspace root. Default '.'"}},
                 self.list_files, idempotent=True),
            Tool("read_file", "Read a workspace file as text. PDFs are converted to text.",
                 {"path": {"type": "string"}}, self.read_file, required=("path",), idempotent=True),
        ]
        if not read_only:
            tools.append(Tool(
                "write_file", "Create or overwrite a text file in the workspace (reports, CSV exports, notes).",
                {"path": {"type": "string"}, "content": {"type": "string"}}, self.write_file,
                required=("path", "content")))
        return tools
