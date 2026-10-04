"""Simulated company intranet for Gotham Freight Co.

Two apps behind one server:
  /mail    Mailroom: a shared accounts-payable inbox with PDF attachments.
  /ledger  Ledger: the internal bills system (login required).

It is a real web app with real state (SQLite) so that the agent's work can be
checked afterwards. It also misbehaves on purpose (see Chaos) so that the
agent's error handling is exercised rather than assumed.
"""
from __future__ import annotations

import html
import os
import re
import secrets
import sqlite3
from datetime import date
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from . import seed

USERS = {"alfred": "sandbox-only-password"}
CURRENCIES = ["INR", "USD", "EUR"]
esc = html.escape


class Chaos:
    """Deterministic failure injection: each fault fires once per reset."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.reset()

    def reset(self) -> None:
        self.save_failures_left = 1 if self.enabled else 0
        self.expire_session_at = 7 if self.enabled else None  # nth authenticated request
        self.authed_requests = 0

    def fail_this_save(self) -> bool:
        if self.save_failures_left > 0:
            self.save_failures_left -= 1
            return True
        return False

    def expire_this_request(self) -> bool:
        self.authed_requests += 1
        if self.expire_session_at and self.authed_requests == self.expire_session_at:
            self.expire_session_at = None
            return True
        return False


def create_app(chaos: bool | None = None) -> FastAPI:
    if chaos is None:
        chaos = os.environ.get("SANDBOX_CHAOS", "on") != "off"
    seed.seed()
    app = FastAPI(title="Gotham Freight sandbox")
    app.state.chaos = Chaos(chaos)
    sessions: dict[str, str] = {}

    def db() -> sqlite3.Connection:
        conn = sqlite3.connect(seed.DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def audit(conn: sqlite3.Connection, actor: str, action: str, detail: str) -> None:
        conn.execute("INSERT INTO audit (actor, action, detail) VALUES (?,?,?)", (actor, action, detail))

    def page(title: str, body: str, user: str | None = None, status: int = 200) -> HTMLResponse:
        nav = ('<a href="/">Intranet</a> <a href="/mail">Mailroom</a> <a href="/ledger/bills">Bills</a> '
               '<a href="/ledger/bills/new">New bill</a> <a href="/ledger/vendors">Vendors</a>')
        who = (f'<span>Signed in as {esc(user)}</span> <form method="post" action="/ledger/logout" class="inline">'
               f'<button>Sign out</button></form>') if user else ""
        return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>{esc(title)}</title>
<style>
 body{{font:15px/1.5 system-ui,sans-serif;margin:0;color:#1c2330;background:#f4f5f7}}
 header{{background:#1c2330;color:#fff;padding:10px 24px;display:flex;gap:18px;align-items:center}}
 header a{{color:#cfd8e6;text-decoration:none}} header .sp{{flex:1}}
 main{{max-width:980px;margin:24px auto;background:#fff;padding:24px 28px;border-radius:8px;border:1px solid #dde1e7}}
 table{{border-collapse:collapse;width:100%}} th,td{{text-align:left;padding:7px 10px;border-bottom:1px solid #e6e9ee}}
 label{{display:block;margin:10px 0 2px;font-weight:600}} input,select,textarea{{font:inherit;padding:6px 8px;min-width:280px}}
 button{{font:inherit;padding:7px 14px;cursor:pointer}} .inline{{display:inline}}
 .error{{background:#fdecec;border:1px solid #e5a3a3;padding:10px 14px;border-radius:6px;margin-bottom:14px}}
 .ok{{background:#e9f7ee;border:1px solid #9fd0af;padding:10px 14px;border-radius:6px;margin-bottom:14px}}
 .muted{{color:#6b7480}} pre{{white-space:pre-wrap;font:inherit}} .actions{{margin-top:18px;display:flex;gap:10px}}
</style></head><body><header><strong>Gotham Freight Co.</strong>{nav}<span class="sp"></span>{who}</header>
<main>{body}</main></body></html>""", status_code=status)

    def current_user(request: Request) -> str | None:
        return sessions.get(request.cookies.get("ledger_session", ""))

    def require_login(request: Request) -> str | RedirectResponse:
        """Return the username, or a redirect to the login page."""
        user = current_user(request)
        if user and app.state.chaos.expire_this_request():
            sessions.pop(request.cookies.get("ledger_session", ""), None)
            return RedirectResponse(f"/ledger/login?expired=1&next={quote(request.url.path)}", status_code=303)
        if not user:
            return RedirectResponse(f"/ledger/login?next={quote(request.url.path)}", status_code=303)
        return user

    # ---------------------------------------------------------------- intranet
    @app.get("/")
    def home(request: Request):
        return page("Intranet", """<h1>Gotham Freight intranet</h1><ul>
<li><a href="/mail">Mailroom</a>: shared accounts-payable inbox</li>
<li><a href="/ledger/bills">Ledger</a>: vendor bills and payments</li></ul>""", current_user(request))

    # ---------------------------------------------------------------- mailroom
    @app.get("/mail")
    def inbox(request: Request, q: str = ""):
        rows = []
        for i, m in sorted(enumerate(seed.EMAILS), key=lambda p: p[1]["received"], reverse=True):
            hay = f"{m['sender']} {m['name']} {m['subject']} {m['body']}".lower()
            if q and q.lower() not in hay:
                continue
            clip = "attachment" if m["attachment"] else ""
            rows.append(f'<tr><td>{esc(m["received"])}</td><td>{esc(m["name"])} &lt;{esc(m["sender"])}&gt;</td>'
                        f'<td><a href="/mail/{i}">{esc(m["subject"])}</a></td><td>{clip}</td></tr>')
        body = (f'<h1>Mailroom: ap@gothamfreight.example</h1>'
                f'<form method="get" action="/mail"><input name="q" value="{esc(q)}" placeholder="Search mail" '
                f'aria-label="Search mail"> <button>Search</button></form>'
                f'<p class="muted">{len(rows)} message(s), newest first</p>'
                f'<table><tr><th>Received</th><th>From</th><th>Subject</th><th></th></tr>{"".join(rows)}</table>')
        return page("Mailroom", body, current_user(request))

    @app.get("/mail/{mail_id}")
    def message(request: Request, mail_id: int):
        if not 0 <= mail_id < len(seed.EMAILS):
            return page("Not found", "<h1>Message not found</h1>", current_user(request), 404)
        m = seed.EMAILS[mail_id]
        att = (f'<p>Attachment: <a href="/mail/{mail_id}/attachment">{esc(m["attachment"])}</a></p>'
               if m["attachment"] else '<p class="muted">No attachments</p>')
        body = (f'<p><a href="/mail">Back to inbox</a></p><h1>{esc(m["subject"])}</h1>'
                f'<p>From: {esc(m["name"])} &lt;{esc(m["sender"])}&gt;<br>Received: {esc(m["received"])}</p>'
                f'<pre>{esc(m["body"])}</pre>{att}')
        return page(m["subject"], body, current_user(request))

    @app.get("/mail/{mail_id}/attachment")
    def attachment(mail_id: int):
        m = seed.EMAILS[mail_id] if 0 <= mail_id < len(seed.EMAILS) else None
        if not m or not m["attachment"]:
            return HTMLResponse("No attachment", status_code=404)
        return FileResponse(seed.ATTACH_DIR / m["attachment"], filename=m["attachment"],
                            media_type="application/pdf")

    # ---------------------------------------------------------------- auth
    @app.get("/ledger/login")
    def login_form(request: Request, next: str = "/ledger/bills", expired: int = 0, bad: int = 0):
        note = ""
        if expired:
            note = '<div class="error">Your session has expired. Please sign in again.</div>'
        if bad:
            note = '<div class="error">Incorrect username or password.</div>'
        return page("Sign in", f"""<h1>Sign in to Ledger</h1>{note}
<form method="post" action="/ledger/login"><input type="hidden" name="next" value="{esc(next)}">
<label for="u">Username</label><input id="u" name="username" autocomplete="off">
<label for="p">Password</label><input id="p" name="password" type="password">
<div class="actions"><button>Sign in</button></div></form>""")

    @app.post("/ledger/login")
    def login(username: str = Form(""), password: str = Form(""), next: str = Form("/ledger/bills")):
        if USERS.get(username) != password:
            return RedirectResponse(f"/ledger/login?bad=1&next={quote(next)}", status_code=303)
        token = secrets.token_hex(16)
        sessions[token] = username
        if not next.startswith("/ledger"):
            next = "/ledger/bills"
        resp = RedirectResponse(next, status_code=303)
        resp.set_cookie("ledger_session", token, httponly=True)
        return resp

    @app.post("/ledger/logout")
    def logout(request: Request):
        sessions.pop(request.cookies.get("ledger_session", ""), None)
        return RedirectResponse("/ledger/login", status_code=303)

    # ---------------------------------------------------------------- bills
    @app.get("/ledger")
    def ledger_root():
        return RedirectResponse("/ledger/bills", status_code=303)

    @app.get("/ledger/bills")
    def bills(request: Request, vendor: str = "", status: str = "", saved: str = ""):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        sql = ("SELECT b.*, v.name AS vendor FROM bills b JOIN vendors v ON v.id = b.vendor_id WHERE 1=1")
        args: list = []
        if vendor:
            sql += " AND v.name LIKE ?"
            args.append(f"%{vendor}%")
        if status in ("paid", "unpaid"):
            sql += " AND b.status = ?"
            args.append(status)
        with db() as conn:
            found = conn.execute(sql + " ORDER BY b.due_date DESC", args).fetchall()
        rows = "".join(
            f'<tr><td><a href="/ledger/bills/{b["id"]}">#{b["id"]}</a></td><td>{esc(b["vendor"])}</td>'
            f'<td>{esc(b["invoice_number"])}</td><td>{b["invoice_date"]}</td><td>{b["due_date"]}</td>'
            f'<td>{b["amount"]:.2f}</td><td>{b["currency"]}</td><td>{b["status"]}</td></tr>' for b in found)
        opts = "".join(f'<option value="{s}"{" selected" if s == status else ""}>{s or "any"}</option>'
                       for s in ("", "unpaid", "paid"))
        note = f'<div class="ok">{esc(saved)}</div>' if saved else ""
        body = (f'<h1>Bills</h1>{note}<form method="get" action="/ledger/bills">'
                f'<input name="vendor" value="{esc(vendor)}" placeholder="Vendor name contains" aria-label="Vendor filter"> '
                f'<select name="status" aria-label="Status filter">{opts}</select> <button>Filter</button></form>'
                f'<p class="muted">{len(found)} bill(s). Today is {date.today().isoformat()}.</p>'
                f'<table><tr><th>Bill</th><th>Vendor</th><th>Invoice no.</th><th>Invoice date</th><th>Due date</th>'
                f'<th>Amount</th><th>Currency</th><th>Status</th></tr>{rows}</table>'
                f'<p><a href="/ledger/bills/new">Enter a new bill</a></p>')
        return page("Bills", body, user)

    def bill_form(user: str, action: str, heading: str, values: dict, errors: list[str], status: int = 200):
        with db() as conn:
            vendors = conn.execute("SELECT id, name FROM vendors ORDER BY name").fetchall()
        v = lambda k: esc(str(values.get(k, "")))  # noqa: E731
        vopts = '<option value="">Select a vendor</option>' + "".join(
            f'<option value="{r["id"]}"{" selected" if str(r["id"]) == str(values.get("vendor_id", "")) else ""}>'
            f'{esc(r["name"])}</option>' for r in vendors)
        copts = "".join(f'<option{" selected" if c == values.get("currency", "INR") else ""}>{c}</option>'
                        for c in CURRENCIES)
        errs = ('<div class="error"><strong>The bill was not saved.</strong><ul>'
                + "".join(f"<li>{esc(e)}</li>" for e in errors) + "</ul></div>") if errors else ""
        return page(heading, f"""<h1>{esc(heading)}</h1>{errs}
<form method="post" action="{action}">
<label for="vendor_id">Vendor</label><select id="vendor_id" name="vendor_id">{vopts}</select>
<label for="invoice_number">Invoice number</label><input id="invoice_number" name="invoice_number" value="{v('invoice_number')}">
<label for="invoice_date">Invoice date</label><input id="invoice_date" name="invoice_date" value="{v('invoice_date')}" placeholder="YYYY-MM-DD">
<label for="due_date">Due date</label><input id="due_date" name="due_date" value="{v('due_date')}" placeholder="YYYY-MM-DD">
<label for="amount">Total amount</label><input id="amount" name="amount" value="{v('amount')}" placeholder="e.g. 12500.00">
<label for="currency">Currency</label><select id="currency" name="currency">{copts}</select>
<label for="notes">Notes</label><textarea id="notes" name="notes" rows="2">{v('notes')}</textarea>
<div class="actions"><button>Save bill</button> <a href="/ledger/bills">Cancel</a></div></form>""", user, status)

    def validate_bill(conn: sqlite3.Connection, f: dict, bill_id: int | None = None) -> list[str]:
        errors = []
        if not f["vendor_id"].isdigit() or not conn.execute(
                "SELECT 1 FROM vendors WHERE id = ?", (f["vendor_id"],)).fetchone():
            errors.append("Vendor is required.")
        if not f["invoice_number"].strip():
            errors.append("Invoice number is required.")
        for key, label in (("invoice_date", "Invoice date"), ("due_date", "Due date")):
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f[key]):
                    raise ValueError
                date.fromisoformat(f[key])
            except ValueError:
                errors.append(f"{label} must be a valid date in YYYY-MM-DD format.")
        if not re.fullmatch(r"\d+(\.\d{1,2})?", f["amount"].strip()) or float(f["amount"]) <= 0:
            errors.append("Total amount must be a positive number with no currency symbol or thousands separators.")
        if f["currency"] not in CURRENCIES:
            errors.append("Currency is not supported.")
        if not errors and f["due_date"] < f["invoice_date"]:
            errors.append("Due date cannot be before the invoice date.")
        if not errors:
            dup = conn.execute("SELECT id FROM bills WHERE vendor_id = ? AND invoice_number = ? AND id IS NOT ?",
                               (f["vendor_id"], f["invoice_number"].strip(), bill_id)).fetchone()
            if dup:
                errors.append(f"A bill with this invoice number already exists for this vendor (bill #{dup['id']}).")
        return errors

    @app.get("/ledger/bills/new")
    def new_bill(request: Request):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        return bill_form(user, "/ledger/bills/new", "Enter a new bill", {}, [])

    @app.post("/ledger/bills/new")
    def create_bill(request: Request, vendor_id: str = Form(""), invoice_number: str = Form(""),
                    invoice_date: str = Form(""), due_date: str = Form(""), amount: str = Form(""),
                    currency: str = Form("INR"), notes: str = Form("")):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        f = dict(vendor_id=vendor_id, invoice_number=invoice_number, invoice_date=invoice_date,
                 due_date=due_date, amount=amount, currency=currency, notes=notes)
        if app.state.chaos.fail_this_save():
            return page("Service unavailable", '<h1>503 Service temporarily unavailable</h1>'
                        '<p>Ledger could not reach the database. Nothing was saved. Please go back and try again.</p>'
                        '<p><a href="/ledger/bills/new">Back to the new bill form</a></p>', user, 503)
        with db() as conn:
            errors = validate_bill(conn, f)
            if errors:
                return bill_form(user, "/ledger/bills/new", "Enter a new bill", f, errors, 422)
            cur = conn.execute(
                "INSERT INTO bills (vendor_id, invoice_number, invoice_date, due_date, amount, currency, notes, created_by) "
                "VALUES (?,?,?,?,?,?,?,?)", (vendor_id, invoice_number.strip(), invoice_date, due_date,
                                             float(amount), currency, notes, user))
            audit(conn, user, "bill.create", f"#{cur.lastrowid} {invoice_number.strip()}")
        return RedirectResponse(f"/ledger/bills/{cur.lastrowid}?saved=1", status_code=303)

    def get_bill(conn: sqlite3.Connection, bill_id: int):
        return conn.execute("SELECT b.*, v.name AS vendor FROM bills b JOIN vendors v ON v.id = b.vendor_id "
                            "WHERE b.id = ?", (bill_id,)).fetchone()

    @app.get("/ledger/bills/{bill_id}")
    def bill_detail(request: Request, bill_id: int, saved: int = 0):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        with db() as conn:
            b = get_bill(conn, bill_id)
        if not b:
            return page("Not found", "<h1>Bill not found</h1>", user, 404)
        note = '<div class="ok">Bill saved.</div>' if saved else ""
        pay = (f'<form method="post" action="/ledger/bills/{bill_id}/pay" class="inline"><button>Mark as paid</button></form>'
               if b["status"] == "unpaid" else "")
        body = (f'<p><a href="/ledger/bills">Back to bills</a></p>{note}<h1>Bill #{b["id"]}</h1><table>'
                f'<tr><th>Vendor</th><td>{esc(b["vendor"])}</td></tr>'
                f'<tr><th>Invoice number</th><td>{esc(b["invoice_number"])}</td></tr>'
                f'<tr><th>Invoice date</th><td>{b["invoice_date"]}</td></tr>'
                f'<tr><th>Due date</th><td>{b["due_date"]}</td></tr>'
                f'<tr><th>Total amount</th><td>{b["amount"]:.2f} {b["currency"]}</td></tr>'
                f'<tr><th>Status</th><td>{b["status"]}</td></tr>'
                f'<tr><th>Notes</th><td>{esc(b["notes"] or "")}</td></tr>'
                f'<tr><th>Entered by</th><td>{esc(b["created_by"] or "")}</td></tr></table>'
                f'<div class="actions"><a href="/ledger/bills/{bill_id}/edit">Edit bill</a> {pay}'
                f'<form method="post" action="/ledger/bills/{bill_id}/delete" class="inline"><button>Delete bill</button></form></div>')
        return page(f"Bill #{bill_id}", body, user)

    @app.get("/ledger/bills/{bill_id}/edit")
    def edit_bill(request: Request, bill_id: int):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        with db() as conn:
            b = get_bill(conn, bill_id)
        if not b:
            return page("Not found", "<h1>Bill not found</h1>", user, 404)
        values = dict(b) | {"amount": f"{b['amount']:.2f}"}
        return bill_form(user, f"/ledger/bills/{bill_id}/edit", f"Edit bill #{bill_id}", values, [])

    @app.post("/ledger/bills/{bill_id}/edit")
    def update_bill(request: Request, bill_id: int, vendor_id: str = Form(""), invoice_number: str = Form(""),
                    invoice_date: str = Form(""), due_date: str = Form(""), amount: str = Form(""),
                    currency: str = Form("INR"), notes: str = Form("")):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        f = dict(vendor_id=vendor_id, invoice_number=invoice_number, invoice_date=invoice_date,
                 due_date=due_date, amount=amount, currency=currency, notes=notes)
        with db() as conn:
            errors = validate_bill(conn, f, bill_id)
            if errors:
                return bill_form(user, f"/ledger/bills/{bill_id}/edit", f"Edit bill #{bill_id}", f, errors, 422)
            conn.execute("UPDATE bills SET vendor_id=?, invoice_number=?, invoice_date=?, due_date=?, amount=?, "
                         "currency=?, notes=? WHERE id=?", (vendor_id, invoice_number.strip(), invoice_date,
                                                           due_date, float(amount), currency, notes, bill_id))
            audit(conn, user, "bill.update", f"#{bill_id}")
        return RedirectResponse(f"/ledger/bills/{bill_id}?saved=1", status_code=303)

    @app.post("/ledger/bills/{bill_id}/pay")
    def pay_bill(request: Request, bill_id: int):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        with db() as conn:
            conn.execute("UPDATE bills SET status = 'paid' WHERE id = ?", (bill_id,))
            audit(conn, user, "bill.pay", f"#{bill_id}")
        return RedirectResponse(f"/ledger/bills/{bill_id}", status_code=303)

    @app.post("/ledger/bills/{bill_id}/delete")
    def delete_bill(request: Request, bill_id: int):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        with db() as conn:
            conn.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
            audit(conn, user, "bill.delete", f"#{bill_id}")
        return RedirectResponse(f"/ledger/bills?saved=Bill+%23{bill_id}+deleted.", status_code=303)

    # ---------------------------------------------------------------- vendors
    @app.get("/ledger/vendors")
    def vendors(request: Request):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        with db() as conn:
            found = conn.execute("SELECT * FROM vendors ORDER BY name").fetchall()
        rows = "".join(f'<tr><td><a href="/ledger/vendors/{v["id"]}">{esc(v["name"])}</a></td>'
                       f'<td>{esc(v["contact_email"])}</td><td>{esc(v["gstin"])}</td></tr>' for v in found)
        return page("Vendors", f'<h1>Vendors</h1><table><tr><th>Name</th><th>Billing contact</th><th>GSTIN</th></tr>'
                               f'{rows}</table>', user)

    @app.get("/ledger/vendors/{vendor_id}")
    def vendor_detail(request: Request, vendor_id: int, saved: int = 0, error: str = ""):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        with db() as conn:
            v = conn.execute("SELECT * FROM vendors WHERE id = ?", (vendor_id,)).fetchone()
        if not v:
            return page("Not found", "<h1>Vendor not found</h1>", user, 404)
        note = '<div class="ok">Vendor saved.</div>' if saved else ""
        if error:
            note = f'<div class="error">{esc(error)}</div>'
        return page(v["name"], f"""<p><a href="/ledger/vendors">Back to vendors</a></p>{note}<h1>{esc(v["name"])}</h1>
<p>GSTIN: {esc(v["gstin"])}</p><form method="post" action="/ledger/vendors/{vendor_id}">
<label for="contact_email">Billing contact email</label>
<input id="contact_email" name="contact_email" value="{esc(v["contact_email"])}">
<div class="actions"><button>Save vendor</button></div></form>""", user)

    @app.post("/ledger/vendors/{vendor_id}")
    def update_vendor(request: Request, vendor_id: int, contact_email: str = Form("")):
        user = require_login(request)
        if not isinstance(user, str):
            return user
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact_email.strip()):
            return RedirectResponse(f"/ledger/vendors/{vendor_id}?error=Enter+a+valid+email+address.", status_code=303)
        with db() as conn:
            conn.execute("UPDATE vendors SET contact_email = ? WHERE id = ?", (contact_email.strip(), vendor_id))
            audit(conn, user, "vendor.update", f"#{vendor_id} contact_email={contact_email.strip()}")
        return RedirectResponse(f"/ledger/vendors/{vendor_id}?saved=1", status_code=303)

    # ---------------------------------------------------------------- sandbox control (not linked from the UI)
    @app.post("/__sandbox/reset")
    def reset():
        seed.seed(reset=True)
        sessions.clear()
        app.state.chaos.reset()
        return {"ok": True}

    @app.get("/__sandbox/state")
    def state():
        """Ground truth for tests and for a human checking the agent's work."""
        with db() as conn:
            return {
                "bills": [dict(r) for r in conn.execute(
                    "SELECT b.*, v.name AS vendor FROM bills b JOIN vendors v ON v.id = b.vendor_id ORDER BY b.id")],
                "vendors": [dict(r) for r in conn.execute("SELECT * FROM vendors ORDER BY id")],
                "audit": [dict(r) for r in conn.execute("SELECT * FROM audit ORDER BY id")],
            }

    return app
