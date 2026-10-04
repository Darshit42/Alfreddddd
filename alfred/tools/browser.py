"""Browser tools built on Playwright.

The model never sees raw HTML or pixels. Each observation is a compact text
rendering of the visible page in which every interactive element carries a
short ref (e1, e2, ...). Actions take a ref. This keeps observations small,
makes actions unambiguous, and works on any ordinary web page without
site-specific selectors.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from ..policy import Approver, needs_approval
from .base import Tool, ToolError, ToolResult

MAX_SNAPSHOT_CHARS = 12_000

# Walks the visible DOM in reading order. Interactive elements are tagged with
# data-alfred-ref and rendered inline as [ref kind "label" ...].
SNAPSHOT_JS = r"""
() => {
  const INTERACTIVE = 'a[href],button,input,select,textarea,[role=button],[role=link],summary';
  const BLOCK = new Set(['P','DIV','SECTION','ARTICLE','HEADER','FOOTER','MAIN','NAV','UL','OL','LI','TABLE',
    'TBODY','THEAD','TR','H1','H2','H3','H4','H5','H6','FORM','FIELDSET','PRE','ASIDE','DL','DT','DD',
    'BLOCKQUOTE','HR','BR','LABEL']);
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','HEAD']);
  document.querySelectorAll('[data-alfred-ref]').forEach(e => e.removeAttribute('data-alfred-ref'));
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const q = t => JSON.stringify(clean(t).slice(0, 120));
  const visible = el => {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const labelOf = el => clean(el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText)
    || el.getAttribute('placeholder') || el.getAttribute('name') || '');
  let n = 0;
  const describe = (el, ref) => {
    const tag = el.tagName;
    const dis = el.disabled ? ' disabled' : '';
    if (tag === 'A') return `[${ref} link ${q(el.innerText || el.getAttribute('aria-label'))}]`;
    if (tag === 'SELECT') {
      const opts = [...el.options].map(o => clean(o.text)).join(' | ');
      const sel = el.selectedOptions[0] ? clean(el.selectedOptions[0].text) : '';
      return `[${ref} select ${q(labelOf(el))} selected=${q(sel)} options: ${opts}${dis}]`;
    }
    if (tag === 'TEXTAREA') return `[${ref} textarea ${q(labelOf(el))} value=${q(el.value)}${dis}]`;
    if (tag === 'INPUT') {
      const type = (el.type || 'text').toLowerCase();
      if (['submit', 'button', 'reset'].includes(type)) return `[${ref} button ${q(el.value)}${dis}]`;
      if (['checkbox', 'radio'].includes(type))
        return `[${ref} ${type} ${q(labelOf(el))} ${el.checked ? 'checked' : 'unchecked'}${dis}]`;
      const value = type === 'password' ? (el.value ? '(hidden)' : '') : el.value;
      const hint = el.placeholder ? ` placeholder=${q(el.placeholder)}` : '';
      return `[${ref} input:${type} ${q(labelOf(el))} value=${q(value)}${hint}${el.required ? ' required' : ''}${dis}]`;
    }
    return `[${ref} button ${q(el.innerText || el.getAttribute('aria-label'))}${dis}]`;
  };
  const lines = [];
  let cur = '';
  const flush = () => { const t = clean(cur); if (t) lines.push(t); cur = ''; };
  const walk = node => {
    if (node.nodeType === 3) { cur += node.textContent; return; }
    if (node.nodeType !== 1) return;
    const el = node, tag = el.tagName;
    if (SKIP.has(tag) || !visible(el)) return;
    if (el.matches(INTERACTIVE)) {
      if (tag === 'INPUT' && el.type === 'hidden') return;
      const ref = 'e' + (++n);
      el.setAttribute('data-alfred-ref', ref);
      cur += ' ' + describe(el, ref) + ' ';
      return;
    }
    if (tag === 'TR') {
      flush();
      for (const cell of el.children) { for (const c of cell.childNodes) walk(c); cur += ' | '; }
      cur = cur.replace(/\s*\|\s*$/, '');
      flush();
      return;
    }
    const block = BLOCK.has(tag);
    if (block) flush();
    if (/^H[1-6]$/.test(tag)) cur += '#'.repeat(+tag[1]) + ' ';
    if (tag === 'LI') cur += '- ';
    for (const c of el.childNodes) walk(c);
    if (block) flush();
  };
  walk(document.body);
  return { title: document.title, text: lines.join('\n') };
}
"""


class BrowserSession:
    """Owns the Chromium process. Hands out pages that share one cookie jar."""

    def __init__(self, downloads_dir: Path, shots_dir: Path, headed: bool = False, slow_mo: int = 0):
        self.downloads_dir = downloads_dir
        self.shots_dir = shots_dir
        downloads_dir.mkdir(parents=True, exist_ok=True)
        shots_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._chromium = self._launch(headless=not headed, slow_mo=slow_mo)
        self.context = self._chromium.new_context(accept_downloads=True, viewport={"width": 1200, "height": 850})
        self.context.set_default_timeout(10_000)
        self._shot_count = 0

    def _launch(self, **options):
        """Use Playwright's bundled Chromium, or an installed Chrome / Edge if it was never downloaded."""
        channels = [os.environ["ALFRED_BROWSER_CHANNEL"]] if os.environ.get("ALFRED_BROWSER_CHANNEL") \
            else [None, "chrome", "msedge"]
        error: Exception | None = None
        for channel in channels:
            try:
                return self._pw.chromium.launch(channel=channel, **options)
            except PlaywrightError as e:
                error = e
        self._pw.stop()
        raise RuntimeError("No usable browser. Run 'python -m playwright install chromium', or install Chrome "
                           f"or Edge. ({str(error).splitlines()[0]})")

    def open(self, name: str, read_only: bool = False, approver: Approver | None = None) -> "Browser":
        return Browser(self, name, read_only, approver)

    def next_shot_path(self, name: str) -> Path:
        self._shot_count += 1
        return self.shots_dir / f"{self._shot_count:03d}-{name}.png"

    def close(self) -> None:
        try:
            self.context.close()
            self._chromium.close()
        finally:
            self._pw.stop()


class Browser:
    def __init__(self, session: BrowserSession, name: str, read_only: bool, approver: Approver | None):
        self.session = session
        self.name = name
        self.read_only = read_only
        self.approver = approver
        self.page = session.context.new_page()
        self._status: int | None = None
        self._downloads: list = []
        self._blocked: list[str] = []
        self.page.on("response", self._on_response)
        self.page.on("download", self._on_download)
        if read_only:
            # Enforced at the network layer: a read-only browser cannot send anything but GET.
            self.page.route("**/*", self._guard)

    # ------------------------------------------------------------------ internals
    def _on_response(self, response) -> None:
        try:
            req = response.request
            if req.is_navigation_request() and req.frame == self.page.main_frame and not 300 <= response.status < 400:
                self._status = response.status
        except PlaywrightError:
            pass

    def _on_download(self, download) -> None:
        self._downloads.append(download)

    def _guard(self, route) -> None:
        if route.request.method in ("GET", "HEAD"):
            route.continue_()
        else:
            self._blocked.append(f"{route.request.method} {route.request.url}")
            route.abort()

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10_000)
            self.page.wait_for_load_state("networkidle", timeout=3_000)
        except PlaywrightError:
            pass  # a slow page is still a page; the snapshot shows whatever is there

    def _collect_downloads(self) -> str:
        self.page.wait_for_timeout(300)
        notes = []
        while self._downloads:
            d = self._downloads.pop(0)
            target = self.session.downloads_dir / Path(d.suggested_filename).name
            d.save_as(str(target))
            notes.append(f"Downloaded file to workspace: downloads/{target.name}")
        return "\n".join(notes)

    def _locate(self, ref: str):
        if not re.fullmatch(r"e\d+", ref or ""):
            raise ToolError(f"'{ref}' is not a valid ref. Refs look like e12 and come from the latest snapshot.")
        loc = self.page.locator(f'[data-alfred-ref="{ref}"]')
        if loc.count() != 1:
            raise ToolError(f"Ref {ref} is not on the current page (the page changed). "
                            "Call browser_snapshot and use refs from it.")
        return loc

    def _shot(self) -> str | None:
        try:
            path = self.session.next_shot_path(self.name)
            self.page.screenshot(path=str(path))
            return str(path)
        except PlaywrightError:
            return None

    def _observe(self, lead: str = "") -> ToolResult:
        try:
            snap = self.page.evaluate(SNAPSHOT_JS)
        except PlaywrightError:  # navigation raced the evaluate; settle and try once more
            self._settle()
            snap = self.page.evaluate(SNAPSHOT_JS)
        text = snap["text"]
        if len(text) > MAX_SNAPSHOT_CHARS:
            text = (text[:MAX_SNAPSHOT_CHARS] + f"\n[snapshot truncated: {len(text) - MAX_SNAPSHOT_CHARS} more "
                    "characters not shown. Use the page's own search or filters to narrow it down.]")
        head = [f"Page: {snap['title']}", f"URL: {self.page.url}"]
        if self._status and self._status >= 400:
            head.append(f"HTTP status: {self._status} (the server reported an error for this page)")
        body = "\n".join(head) + "\n---\n" + (text or "(the page has no visible content)")
        return ToolResult((lead + "\n" if lead else "") + body, artifact=self._shot())

    # ------------------------------------------------------------------ tools
    def navigate(self, url: str) -> ToolResult:
        if not re.match(r"https?://", url):
            raise ToolError("url must start with http:// or https://")
        self._status = None
        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as e:
            if "Download is starting" not in str(e):
                raise
            return ToolResult(self._collect_downloads() or "The URL started a download but no file arrived.")
        self._settle()
        return self._observe()

    def snapshot(self) -> ToolResult:
        return self._observe()

    def click(self, ref: str) -> ToolResult:
        loc = self._locate(ref)
        label = loc.evaluate("el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 80)")
        what = f'{ref} "{label}"'
        if self.approver and needs_approval(label):
            if not self.approver(f'Click "{label}"', self.page.url):
                raise ToolError(f'Not done: clicking "{label}" needs human approval and it was not given. '
                                "Do not look for another way to do the same thing. If the task depends on it, "
                                "finish with status needs_user and explain.")
        self._blocked.clear()
        self._status = None
        loc.click()
        self._settle()
        if self._blocked:
            raise ToolError(f"Blocked: this browser is read-only and the click tried to send {self._blocked[0]}.")
        downloads = self._collect_downloads()
        return self._observe(f"Clicked {what}." + (f"\n{downloads}" if downloads else ""))

    def fill(self, fields: list) -> str:
        if not isinstance(fields, list) or not fields:
            raise ToolError("fields must be a non-empty list of {ref, value}.")
        lines, failed = [], False
        for f in fields:
            ref, value = (f or {}).get("ref"), str((f or {}).get("value", ""))
            try:
                loc = self._locate(ref)
                kind = loc.evaluate("el => el.tagName === 'SELECT' ? 'select' : (el.type || 'text').toLowerCase()")
                if kind == "select":
                    try:
                        loc.select_option(label=value, timeout=2_000)
                    except PlaywrightError:
                        try:
                            loc.select_option(value=value, timeout=2_000)
                        except PlaywrightError:
                            raise ToolError(f'no option "{value}" (options are listed in the snapshot)') from None
                    now = loc.evaluate("el => el.selectedOptions[0] ? el.selectedOptions[0].text.trim() : ''")
                elif kind in ("checkbox", "radio"):
                    loc.set_checked(value.strip().lower() in ("true", "yes", "on", "1", "checked"))
                    now = "checked" if loc.is_checked() else "unchecked"
                else:
                    loc.fill(value)
                    now = "(hidden)" if kind == "password" else loc.input_value()
                lines.append(f"{ref}: now {now!r}")
            except ToolError as e:
                failed = True
                lines.append(f"{ref}: FAILED, {e}")
            except PlaywrightError as e:
                failed = True
                lines.append(f"{ref}: FAILED, {str(e).splitlines()[0]}")
        report = "\n".join(lines)
        if failed:
            raise ToolError("Some fields were not filled:\n" + report)
        return "Filled (nothing is submitted until you click the form's button):\n" + report

    def back(self) -> ToolResult:
        self._status = None
        self.page.go_back(wait_until="domcontentloaded")
        self._settle()
        return self._observe("Went back.")

    def tools(self) -> list[Tool]:
        ref = {"type": "string", "description": "Element ref from the latest snapshot, e.g. e7"}
        return [
            Tool("browser_navigate", "Open a URL. Returns a snapshot of the page: its visible text with every "
                 "interactive element shown as [ref kind \"label\" ...]. A URL that serves a file downloads it "
                 "into the workspace instead.",
                 {"url": {"type": "string"}}, self.navigate, required=("url",), idempotent=True),
            Tool("browser_snapshot", "Re-read the current page. Use after filling fields if you need fresh refs "
                 "or want to confirm what the page now shows.", {}, self.snapshot, idempotent=True),
            Tool("browser_click", "Click a link or button by ref. Returns a snapshot of the resulting page, so "
                 "read it to see whether the click did what you expected (errors and confirmations appear there).",
                 {"ref": ref}, self.click, required=("ref",)),
            Tool("browser_fill", "Set the value of one or more form fields (text inputs, textareas, selects by "
                 "option label, checkboxes with 'true'/'false'). Does not submit. Returns each field's value as "
                 "read back from the page.",
                 {"fields": {"type": "array", "items": {
                     "type": "object", "properties": {"ref": ref, "value": {"type": "string"}},
                     "required": ["ref", "value"]}}},
                 self.fill, required=("fields",)),
            Tool("browser_back", "Go back one page in history.", {}, self.back),
        ]
