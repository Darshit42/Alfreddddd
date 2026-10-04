from __future__ import annotations

import httpx
import pytest
from rich.console import Console

from alfred.loop import run_loop
from alfred.policy import Enforcer, always_deny, auto_approve
from alfred.store import Store, prior_attempts_note
from alfred.tools import Tool, ToolError, ToolResult, Toolset
from alfred.tools.browser import BrowserSession
from alfred.tools.files import Workspace
from alfred.trace import Trace
from alfred.worker import RunConfig, run_task

from .conftest import ScriptedLLM, call, ref, say

LOGIN = ("alfred", "sandbox-only-password")
BILL = {"Vendor": "Kestrel Logistics", "Invoice number": "KL-2026-0926", "Invoice date": "2026-09-26",
        "Due date": "2026-10-26", "Total amount": "184375.50", "Currency": "INR"}


def quiet_trace(tmp_path) -> Trace:
    return Trace(tmp_path / "run", Console(quiet=True))


def sign_in(obs: str):
    return [call("browser_fill", fields=[{"ref": ref(obs, "input", "Username"), "value": LOGIN[0]},
                                         {"ref": ref(obs, "input", "Password"), "value": LOGIN[1]}])]


def fill_bill(obs: str, **overrides):
    values = BILL | overrides
    kinds = {"Vendor": "select", "Currency": "select"}
    return [call("browser_fill", fields=[{"ref": ref(obs, kinds.get(k, "input"), k), "value": v}
                                         for k, v in values.items()])]


# --------------------------------------------------------------------------- end to end
def test_invoice_task_end_to_end(sandbox, tmp_path):
    """Download an invoice, enter it in Ledger through a 503 and a session expiry, and get it verified."""
    url = sandbox.url
    seen: dict = {}

    def keep(name):
        def step(obs):
            seen[name] = obs
            return None
        return step

    worker = [
        [call("plan", goal="Enter the latest Kestrel Logistics invoice in Ledger",
              success_criteria=["Ledger has exactly one bill KL-2026-0926 for Kestrel Logistics, 184375.50 INR, due 2026-10-26"])],
        [call("browser_navigate", url=f"{url}/mail?q=kestrel+logistics")],
        lambda obs: [call("browser_click", ref=ref(obs, "link", "Invoice KL-2026-0926 for September linehaul"))],
        lambda obs: [call("browser_click", ref=ref(obs, "link", "KL-2026-0926.pdf"))],
        lambda obs: keep("download")(obs) or [call("read_file", path="downloads/KL-2026-0926.pdf")],
        lambda obs: keep("pdf")(obs) or [call("browser_navigate", url=f"{url}/ledger/bills/new")],
        sign_in,
        lambda obs: [call("browser_snapshot")],
        lambda obs: [call("browser_click", ref=ref(obs, "button", "Sign in"))],
        # First attempt uses the invoice's own formats: Ledger must reject it with a readable message.
        lambda obs: fill_bill(obs, **{"Total amount": "1,84,375.50", "Due date": "26 October 2026"}),
        lambda obs: [call("browser_snapshot")],
        lambda obs: [call("browser_click", ref=ref(obs, "button", "Save bill"))],          # chaos: 503
        lambda obs: keep("first_save")(obs) or [call("browser_navigate", url=f"{url}/ledger/bills/new")],
        lambda obs: fill_bill(obs, **{"Total amount": "1,84,375.50", "Due date": "26 October 2026"}),
        lambda obs: [call("browser_snapshot")],
        lambda obs: [call("browser_click", ref=ref(obs, "button", "Save bill"))],          # validation errors
        lambda obs: keep("validation")(obs) or fill_bill(obs),
        lambda obs: [call("browser_snapshot")],
        lambda obs: [call("browser_click", ref=ref(obs, "button", "Save bill"))],          # saved
        lambda obs: keep("saved")(obs) or [call("finish", status="success", summary="Entered KL-2026-0926.")],
        # The reviewer hit an expired session, so the first finish is rejected. Sign in again, finish again.
        lambda obs: keep("rejection")(obs) or [call("browser_navigate", url=f"{url}/ledger/bills")],
        sign_in,
        lambda obs: [call("browser_snapshot")],
        lambda obs: [call("browser_click", ref=ref(obs, "button", "Sign in"))],
        [call("finish", status="success", summary="Entered KL-2026-0926.", details=["Bill for 184375.50 INR"])],
    ]

    def judge(obs: str):
        if "Sign in to Ledger" in obs:
            return [call("submit_verdict", overall="inconclusive", feedback="Ledger showed a sign-in page.",
                         checks=[{"criterion": "bill exists", "result": "unknown", "observed": "sign-in page"}])]
        rows = [line for line in obs.splitlines() if "KL-2026-0926" in line]
        ok = len(rows) == 1 and "184375.50" in rows[0] and "2026-10-26" in rows[0]
        return [call("submit_verdict", overall="pass" if ok else "fail",
                     checks=[{"criterion": "bill exists", "result": "pass" if ok else "fail", "observed": str(rows)}])]

    verifier = [[call("browser_navigate", url=f"{url}/ledger/bills?vendor=Kestrel")], judge] * 2
    llm = ScriptedLLM(worker, verifier)
    trace = quiet_trace(tmp_path)
    cfg = RunConfig(workspace=tmp_path / "ws", run_dir=trace.run_dir)
    outcome = run_task("Enter the latest Kestrel Logistics invoice", llm, cfg, trace)

    assert "Downloaded file to workspace: downloads/KL-2026-0926.pdf" in seen["download"]
    assert "KL-2026-0926" in seen["pdf"] and "1,84,375.50" in seen["pdf"] and "26 October 2026" in seen["pdf"]
    assert "HTTP status: 503" in seen["first_save"]
    assert "Due date must be a valid date" in seen["validation"] and "Total amount must be" in seen["validation"]
    assert "Bill saved." in seen["saved"]
    assert "Not accepted" in seen["rejection"] and "sign-in page" in seen["rejection"]
    assert outcome["status"] == "success" and outcome["verified"] is True

    state = httpx.get(f"{url}/__sandbox/state").json()
    entered = [b for b in state["bills"] if b["invoice_number"] == "KL-2026-0926"]
    assert len(entered) == 1, "the failed and rejected attempts must not have created duplicates"
    assert entered[0]["amount"] == 184375.50 and entered[0]["due_date"] == "2026-10-26"
    assert entered[0]["vendor"] == "Kestrel Logistics" and entered[0]["created_by"] == "alfred"
    assert trace.write_report("t", outcome).exists()


def test_unverifiable_success_is_not_reported_as_success(sandbox, tmp_path):
    worker = [[call("plan", goal="g", success_criteria=["bill X exists"])]] + \
             [[call("finish", status="success", summary="Done!")]] * 3
    fail = [[call("submit_verdict", overall="fail", feedback="No such bill.",
                  checks=[{"criterion": "bill X exists", "result": "fail", "observed": "not in the list"}])]] * 3
    trace = quiet_trace(tmp_path)
    outcome = run_task("t", ScriptedLLM(worker, fail), RunConfig(workspace=tmp_path / "ws", run_dir=trace.run_dir), trace)
    assert outcome["status"] == "unverified" and outcome["verified"] is False
    assert outcome["verdict"]["overall"] == "fail"


# --------------------------------------------------------------------------- browser guards
@pytest.fixture()
def session(tmp_path):
    s = BrowserSession(tmp_path / "dl", tmp_path / "shots")
    yield s
    s.close()


def login(browser, url):
    obs = browser.navigate(f"{url}/ledger/login").text
    browser.fill([{"ref": ref(obs, "input", "Username"), "value": LOGIN[0]},
                  {"ref": ref(obs, "input", "Password"), "value": LOGIN[1]}])
    return browser.click(ref(obs, "button", "Sign in")).text


def test_enforcer_gates_the_actual_request(sandbox, session):
    asked, log = [], []
    enforcer = Enforcer(lambda action, url: asked.append(action) or False, never=["^/__sandbox/"],
                        log=lambda **kw: log.append((kw["method"], kw["verdict"])))
    browser = session.open("w", enforcer=enforcer)
    login(browser, sandbox.url)                                   # an ordinary write: allowed, and logged
    obs = browser.navigate(f"{sandbox.url}/ledger/bills/2").text
    with pytest.raises(ToolError, match="needs human approval"):
        browser.click(ref(obs, "button", "Mark as paid"))
    assert asked == ['POST /ledger/bills/2/pay (after clicking "Mark as paid")']
    with pytest.raises(ToolError, match="off limits"):            # never-list: not even readable
        browser.navigate(f"{sandbox.url}/__sandbox/state")
    assert log == [("POST", "allowed"), ("POST", "denied"), ("GET", "refused")]
    bills = httpx.get(f"{sandbox.url}/__sandbox/state").json()["bills"]
    assert next(b for b in bills if b["id"] == 2)["status"] == "unpaid"


def test_slow_human_approval_still_yields_the_resulting_page(sandbox, session):
    import time

    def slow_yes(action, url):
        time.sleep(4)          # a person reading the approval dialog
        return True

    browser = session.open("w", enforcer=Enforcer(slow_yes))
    login(browser, sandbox.url)
    obs = browser.navigate(f"{sandbox.url}/ledger/bills/2").text
    after = browser.click(ref(obs, "button", "Mark as paid")).text
    assert "Status | paid" in after


def test_enforcer_judges_the_operation_not_the_label():
    e = Enforcer(always_deny)
    assert e.check("POST", "http://x/ledger/bills/new", "Save bill")[0]
    assert e.check("POST", "http://x/ledger/login", "Sign in")[0]
    assert not e.check("POST", "http://x/ledger/bills/2/delete", "Confirm")[0]    # innocuous label, risky request
    assert not e.check("POST", "http://x/form", "Delete bill")[0]                 # risky label, innocuous URL
    assert e.check("GET", "http://x/ledger/bills/2/delete")[0]                    # reads are not writes
    assert Enforcer(auto_approve).check("POST", "http://x/ledger/bills/2/pay")[0]
    assert not Enforcer(auto_approve, never=["^/admin"]).check("POST", "http://x/admin/x")[0]   # approval cannot override


def test_read_only_browser_cannot_change_anything(sandbox, session):
    login(session.open("w", enforcer=Enforcer(always_deny)), sandbox.url)
    reviewer = session.open("v", read_only=True)       # shares the signed-in cookie jar
    obs = reviewer.navigate(f"{sandbox.url}/ledger/bills/2").text
    assert "Bill #2" in obs
    with pytest.raises(ToolError, match="read-only"):
        reviewer.click(ref(obs, "button", "Delete bill"))
    assert len(httpx.get(f"{sandbox.url}/__sandbox/state").json()["bills"]) == 6


def test_stale_ref_is_an_explainable_error(sandbox, session):
    browser = session.open("w")
    browser.navigate(f"{sandbox.url}/mail")
    with pytest.raises(ToolError, match="not on the current page"):
        browser.click("e999")


# --------------------------------------------------------------------------- loop and tools
def test_loop_turns_failures_into_observations_and_stops_when_stuck(tmp_path):
    def boom():
        raise RuntimeError("kaboom")

    tools = Toolset([Tool("boom", "", {}, boom), Tool("done", "", {}, lambda: ToolResult("ok", final=True))])
    llm = ScriptedLLM([[say("thinking out loud")], [call("nope")], [call("boom", extra=1)]] + [[call("boom")]] * 6)
    messages = [{"role": "user", "content": "go"}]
    end = run_loop(llm=llm, system="", messages=messages, tools=tools, trace=quiet_trace(tmp_path),
                   role="worker", max_steps=20)
    assert end.reason == "stalled"
    joined = "\n".join(llm.observations)
    assert "without calling a tool" in joined            # plain text reply gets a nudge
    assert "Unknown tool 'nope'" in joined               # hallucinated tool
    assert "unknown parameters: extra" in joined         # bad arguments
    assert "RuntimeError: kaboom" in joined              # a crash is an observation, not a crash
    assert "same call three times" in "\n".join(str(m["content"]) for m in messages)


def test_workspace_confines_paths(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.write_file("out/report.csv", "a,b\n")
    assert "out/report.csv" in ws.list_files("out")
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.read_file("../secret.txt")


# --------------------------------------------------------------------------- supervisor, queue, decision log
def test_cost_cap_stops_the_run(tmp_path):
    llm = ScriptedLLM([[call("plan", goal="g", success_criteria=["c"])]] * 5)
    llm.cost = lambda: 0.50 * llm.usage.calls
    trace = quiet_trace(tmp_path)
    cfg = RunConfig(workspace=tmp_path / "ws", run_dir=trace.run_dir, max_cost_usd=1.0)
    outcome = run_task("t", llm, cfg, trace)
    assert outcome["status"] == "incomplete" and "cost cap" in outcome["summary"]
    assert llm.usage.calls == 3     # stopped before the fourth call, not after the money was spent


def test_queue_leases_are_exclusive_and_crash_safe(tmp_path):
    a, b = Store(tmp_path / "q.db"), Store(tmp_path / "q.db")
    b.owner = "worker-b"
    tid = a.add("do the thing")
    assert a.lease_next()["id"] == tid
    assert b.lease_next() is None                         # one task, one owner

    # The worker dies: its lease lapses and the supervisor requeues the task for another attempt.
    a.db.execute("UPDATE tasks SET lease_expires = datetime('now', '-1 second')")
    assert b.reclaim_expired() == [tid]
    row = b.lease_next()
    assert row["id"] == tid and "interrupted" in prior_attempts_note(row["history"], row["attempts"] + 1)

    # It dies again: no third blind retry, a person gets it instead.
    b.db.execute("UPDATE tasks SET lease_expires = datetime('now', '-1 second')")
    b.reclaim_expired()
    assert b.tasks()[0]["state"] == "needs_human" and b.lease_next() is None


def test_escalated_task_waits_for_a_human_then_resumes(tmp_path):
    store = Store(tmp_path / "q.db")
    tid = store.add("enter the latest Kestrel invoice")
    store.lease_next()
    assert store.complete(tid, {"status": "needs_user", "summary": "Which Kestrel: Logistics or Labs?"}) == "needs_human"
    assert store.lease_next() is None                     # parked, not retried
    assert store.answer(tid, "Logistics") and not store.answer(999, "x")
    row = store.lease_next()
    note = prior_attempts_note(row["history"], row["attempts"] + 1)
    assert "Which Kestrel" in note and "Your colleague replied: Logistics" in note
    assert store.complete(tid, {"status": "success", "summary": "done"}) == "done"


def test_decision_log_and_metrics(tmp_path):
    store = Store(tmp_path / "q.db")
    trace = Trace(tmp_path / "run-1", Console(quiet=True), sink=store.log)
    store.start_run("run-1", "t")
    trace.event("write", role="worker", method="POST", url="http://x/pay", label="Pay", verdict="denied")
    store.end_run("run-1", {"status": "success", "verified": True, "steps": 4, "cost_usd": 0.4})
    store.start_run("run-2", "t")
    store.end_run("run-2", {"status": "needs_user", "steps": 2, "cost_usd": 0.2})
    kinds = [r["kind"] for r in store.db.execute("SELECT kind FROM decisions WHERE run_id = 'run-1'")]
    assert kinds == ["write"]
    m = store.metrics()
    assert (m["runs"], m["verified"], m["escalated"]) == (2, 1, 1)
    assert round(m["cost_per_verified"], 2) == 0.60       # fully loaded: escalated spend counts too
