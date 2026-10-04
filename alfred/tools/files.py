"""File tools. Reading works anywhere on this computer (the worker runs locally,
for its owner). Writing is confined to the workspace so a run cannot overwrite
the user's own files."""
from __future__ import annotations

from pathlib import Path

from .base import Tool, ToolError

MAX_CHARS = 20_000


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str, write: bool = False) -> Path:
        p = (self.root / Path(path).expanduser()).resolve()   # an absolute path replaces the root
        if write and p != self.root and self.root not in p.parents:
            raise ToolError(f"'{path}' is outside the workspace. Files can be read anywhere but only written "
                            "inside the workspace; use a path relative to the workspace root.")
        return p

    def rel(self, p: Path) -> str:
        p = p.resolve()
        return p.relative_to(self.root).as_posix() if self.root in p.parents else p.as_posix()

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
        p = self.resolve(path, write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} characters to {self.rel(p)}."

    def tools(self, read_only: bool = False) -> list[Tool]:
        tools = [
            Tool("list_files", "List a directory. Relative paths are inside your workspace (browser downloads "
                 "land in 'downloads/'); an absolute path or ~ reaches anywhere on this computer.",
                 {"path": {"type": "string", "description": "Directory. Default '.', the workspace root."}},
                 self.list_files, idempotent=True),
            Tool("read_file", "Read a file as text (PDFs are converted to text). Relative paths are inside the "
                 "workspace; absolute paths read from anywhere on this computer.",
                 {"path": {"type": "string"}}, self.read_file, required=("path",), idempotent=True),
        ]
        if not read_only:
            tools.append(Tool(
                "write_file", "Create or overwrite a text file in the workspace (reports, CSV exports, notes).",
                {"path": {"type": "string"}, "content": {"type": "string"}}, self.write_file,
                required=("path", "content")))
        return tools
