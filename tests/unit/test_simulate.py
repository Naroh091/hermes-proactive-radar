"""The scenario simulator: real pipeline, scratch store, nothing sent."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from conftest import NOW, FakeContext, FakeLlm, active_config, extract_answer, plan_answer, verify_destination

from radar.runtime import Runtime
from radar.simulate import feed, load_scenario, render, run_scenario, scratch_store, sim_config
from radar.store import Scope
from radar.transports import TransportRegistry

ASK = {"title": "¿Actualizas la tarjeta de Brave?", "rationale": "", "action_class": "read", "operation": "ask", "when": "", "urgency": "high",
       "options": [{"id": "si", "label": "Ya la actualicé"}, {"id": "no", "label": "Lo hago luego"}], "fact": {"key": "brave_card", "yes": "updated", "no": "pending"},
       "then": "step"}


def _scenario():
    return {"signals": [
        {"kind": "email", "account": "me@example.org", "from": "Brave Software <no-reply@stripe.example>", "subject": "$8.82 payment was unsuccessful",
         "body": "We were unable to charge your Visa ending in 0042. Please update your billing information."},
        {"kind": "email", "account": "me@example.org", "from": "Newsletter <news@example-list.example>", "subject": "Weekly digest", "body": "unsubscribe here",
         "headers": {}},
        {"kind": "calendar", "title": "Gala Avanza Tech", "organizer": "olga@avanzatech.example", "start": "2026-11-13T19:30:00+01:00", "my_response": "needsAction"},
        {"kind": "chat", "session_id": "sim-1", "turns": [{"user": "Resérvame un coche en Avilés", "assistant": "Hecho: reserva 10000001."}]},
    ], "facts": [{"key": "shoe size", "value": "42"}]}


def test_a_scenario_runs_through_the_real_pipeline_on_a_scratch_store_and_reports_what_would_be_said(store, tmp_path):
    cfg = sim_config(active_config(sources=[{"id": "gmail", "kind": "gmail", "address": "me@example.org", "provider": "gmail_mcp", "server": "g", "labels": ["INBOX"], "interval_seconds": 300, "enabled": True}]))
    assert cfg["jobs"]["enabled"] is False and cfg["warmup"]["enabled"] is False and cfg["notifications"]["enabled"] is True
    verify_destination(store)
    extract = extract_answer(category="purchase", title="Pago fallido de Brave", message_kind="notice", sender_kind="vendor", risk_flags=["financial_loss"])
    plan = plan_answer(task={**plan_answer()["task"], "kind": "purchase", "title": "Pago fallido de 8,82 USD a Brave", "due_at": ""}, proposals=[ASK],
                       notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "money"}, message="Falló el cobro de Brave.")
    llm = FakeLlm({"radar.extract": [extract] * 4, "radar.plan": [plan] * 4, "radar.remind": [{"text": "Falló el cobro de 8,82 $ de Brave en la Visa 0042. ¿Ya la actualizaste?"}] * 4})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="sim", clock=lambda: NOW, transports=TransportRegistry(), hermes_home=str(tmp_path))
    rt.sources = {}
    report = asyncio.run(run_scenario(rt, _scenario(), NOW))
    triage = [(s["kind"], s["triage"]) for s in report["signals"]]
    assert triage[0] == ("email", "kept") and triage[1][1].startswith("ignored") and triage[2] == ("calendar", "kept") and triage[3] == ("chat", "kept")
    assert store.list_facts()[0]["key"] == "shoe size"
    brave = next(b for b in report["bundles"] if b["group"].startswith("thread:"))  # the canned plan answers every bundle; judge the mail's
    assert brave["outcome"]["decision"] == "new" and brave["extract"]["message_kind"] == "notice" and brave["extract"]["sender_kind"] == "vendor"
    assert [p["operation"] for p in brave["proposals"]] == ["ask"] and brave["proposals"][0]["then"] == "step" and brave["gates"]["disposition"] == "notify_now"
    assert report["would_send"] and report["would_send"][0]["kind"] == "notice" and "Visa 0042" in report["would_send"][0]["text"]
    assert "Ya la actualicé" in report["would_send"][0]["buttons"]
    assert store.list_jobs() == [] and store.list_outbox(limit=5)[0]["status"] == "pending"  # nothing ran, nothing was sent
    text = render(report)
    assert "What you would receive:" in text and "[notice]" in text and "gates:" in text
    assert "Scenario at 2026-10-19T10:00:00Z" in text


def test_a_scenario_in_phases_delivers_a_timed_step_at_its_moment_between_two_phases(store, tmp_path):
    """A ticket bought one day, the check-in mail a week later: the step the first plan timed for the morning of the
    check-in goes out at that moment, before the second mail is judged."""
    cfg = sim_config(active_config(sources=[{"id": "gmail", "kind": "gmail", "address": "me@example.org", "provider": "gmail_mcp", "server": "g", "labels": ["INBOX"], "interval_seconds": 300, "enabled": True}]))
    verify_destination(store)
    extract = extract_answer(category="trip", title="Vuelo IB3012 14 nov", message_kind="receipt", sender_kind="vendor")
    timed = {**ASK, "title": "¿Hago el check-in del IB3012?", "when": "2026-10-20T07:30:00Z", "fact": {}}
    plan = plan_answer(task={**plan_answer()["task"], "kind": "trip", "title": "Vuelo IB3012 Madrid-Barcelona", "due_at": "2026-10-21T07:30:00Z", "next_review_at": ""},
                       proposals=[timed], notify={"disposition": "silent", "urgency": "later", "importance": "high", "reason": "a receipt"}, message="")
    llm = FakeLlm({"radar.extract": [extract] * 4, "radar.plan": [plan] * 4, "radar.remind": [{"text": "El check-in del IB3012 ya está abierto. ¿Lo hago?"}] * 6})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="sim", clock=lambda: NOW, transports=TransportRegistry(), hermes_home=str(tmp_path))
    rt.sources = {}
    scenario = {"phases": [
        {"at": "2026-10-19T10:00:00Z", "signals": [{"kind": "email", "account": "me@example.org", "from": "Iberia <noreply@iberia.example>", "subject": "Confirmación de compra ABC123", "body": "Vuelo IB3012."}]},
        {"at": "2026-10-20T10:00:00Z", "signals": [{"kind": "email", "account": "me@example.org", "from": "Iberia <noreply@iberia.example>", "subject": "Ya puedes hacer el check-in", "body": "Abierto."}]},
    ]}
    report = asyncio.run(run_scenario(rt, scenario, NOW))
    first, second = report["phases"]
    assert first["at"] == "2026-10-19T10:00:00Z" and first["would_send"] == [] and first["bundles"][0]["proposals"][0]["when"] == "2026-10-20T07:30:00Z"
    meanwhile = second["between"]["messages"]
    assert [(m["at"], m["kind"]) for m in meanwhile] == [("2026-10-20T07:30:00Z", "notice")] and "check-in del IB3012" in meanwhile[0]["text"]
    assert second["signals"][0]["triage"] == "kept" and second["bundles"]
    text = render(report)
    assert "Scenario in 2 phases" in text and "=== Phase 2 at 2026-10-20T10:00:00Z" in text and "at 2026-10-20T07:30:00Z:" in text


def test_a_review_that_schedules_the_next_one_inside_the_window_runs_it_too(store, tmp_path):
    """The fallback of an awaited step is set by a review: the simulator re-scans after every event, so the second
    review runs at its hour instead of being lost (2026-11-13: the fallback review at 08:30 never ran)."""
    cfg = sim_config(active_config(sources=[{"id": "gmail", "kind": "gmail", "address": "me@example.org", "provider": "gmail_mcp", "server": "g", "labels": ["INBOX"], "interval_seconds": 300, "enabled": True}]))
    verify_destination(store)
    extract = extract_answer(category="trip", title="Vuelo IB3012 14 nov", message_kind="receipt", sender_kind="vendor")

    def plan(review_at):
        return plan_answer(task={**plan_answer()["task"], "kind": "trip", "title": "Vuelo IB3012", "due_at": "2026-10-21T07:30:00Z", "next_review_at": review_at},
                           proposals=[], notify={"disposition": "silent", "urgency": "whenever", "importance": "normal", "reason": "a receipt"}, message="")

    plans = [plan("2026-10-19T11:00:00Z"), plan("2026-10-19T12:00:00Z"), plan(""), plan("")]
    llm = FakeLlm({"radar.extract": [extract] * 4, "radar.plan": plans, "radar.remind": [{"text": "x"}] * 4})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="sim", clock=lambda: NOW, transports=TransportRegistry(), hermes_home=str(tmp_path))
    rt.sources = {}
    scenario = {"phases": [
        {"at": "2026-10-19T10:00:00Z", "signals": [{"kind": "email", "account": "me@example.org", "from": "Iberia <noreply@iberia.example>", "subject": "Compra ABC123", "body": "IB3012."}]},
        {"at": "2026-10-20T10:00:00Z", "signals": [{"kind": "email", "account": "me@example.org", "from": "Iberia <noreply@iberia.example>", "subject": "Check-in abierto", "body": "Abierto."}]},
    ]}
    report = asyncio.run(run_scenario(rt, scenario, NOW))
    reviews = report["phases"][1]["between"]["reviews"]
    assert [r["at"] for r in reviews] == ["2026-10-19T11:00:00Z", "2026-10-19T12:00:00Z"]


def test_the_scratch_store_copies_the_live_one_or_starts_fresh(store, tmp_path):
    home = tmp_path / "home"
    (home / "radar").mkdir(parents=True)
    store.create_task(task_id="live", kind="other", title="Una tarea viva", summary="", entities={}, expectations=[], checklist=[], now="2026-10-19T09:00:00Z")
    import shutil

    store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # the live file may hold its latest writes in the WAL
    shutil.copy(store.path, home / "radar" / "radar.sqlite3")
    copied = scratch_store(home, Scope("profile-a", "owner-a"), fresh=False)
    assert [t["title"] for t in copied.list_tasks(statuses=None)] == ["Una tarea viva"]
    copied.close()
    empty = scratch_store(home, Scope("profile-a", "owner-a"), fresh=True)
    assert empty.list_tasks(statuses=None) == [] and empty.path == home / "radar" / "sim" / "radar.sqlite3"
    empty.close()
    named = scratch_store(home, Scope("profile-a", "owner-a"), fresh=True, name="iberia checkin/2")  # one file per scenario, side by side
    assert named.path == home / "radar" / "sim" / "iberia_checkin_2.sqlite3"
    named.close()
    (tmp_path / "s.json").write_text('{"signals": [{"kind": "email", "from": "a@b.c", "subject": "x"}]}', encoding="utf-8")
    assert load_scenario(tmp_path / "s.json")["signals"][0]["subject"] == "x"
