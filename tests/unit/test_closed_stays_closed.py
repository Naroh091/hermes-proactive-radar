"""What the owner closed stays closed to everyone but them.

2026-11-02 18:45 the owner said «No voy a valorar nada, ciérrala» about a compromised Postgres host; on 2026-11-04 at
02:30 a lagging job result reopened the task and woke them with «sigue comprometido… necesito que decidas». The same
week Rekognition was asked about twice after «da igual», and a thread the owner had closed came back as a new task.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from conftest import NOW, FakeContext, FakeLlm, active_config, config, extract_answer, plan_answer

from radar.pipeline import act, extract as extract_step, reason
from radar.pipeline.process import process_bundle
from radar.runtime import Runtime

T = "2026-10-19T10:00:00Z"
STEP = [{"title": "Detener los procesos y vaciar el crontab", "rationale": "", "action_class": "read", "operation": "investigate", "when": "",
         "urgency": "high", "options": [{"id": "do", "label": "Sí"}]}]


def _mail(store, signal_id, group_key, title, *, sender="ops@linode.example"):
    if not any(s["id"] == "src" for s in store.list_sources()):
        store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=T)
    store.add_signal(signal_id=signal_id, source_id="src", kind="email", occurred_at=T, external_ref=signal_id, group_key=group_key, title=title,
                     snippet="", meta={"sender": sender, "account": "a@example.org"}, fingerprint=signal_id, now=T)
    return store.get_signal(signal_id)


def _closed_by_owner(store, task_id, title, *, kind="work_item", when=T, signal=None):
    store.create_task(task_id=task_id, kind=kind, title=title, summary="", entities={}, expectations=[], checklist=[], now=when,
                      origin_signal_id=signal["id"] if signal else None)
    if signal:
        store.link_task_signal(task_id, signal["id"], "origin", when)
    store.set_task_status(task_id, "done", now=when, reason="owner_says_done")
    return store.get_task(task_id)


def _replan(status="open", proposals=STEP):
    plan = plan_answer(task_match={"decision": "advances", "task_id": "pg", "why": "the job found the process still running"},
                       task={**plan_answer()["task"], "kind": "work_item", "status": status}, proposals=proposals,
                       notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "still compromised"},
                       message="El host 203.0.113.44 sigue comprometido: el proceso corre desde /tmp.")
    return reason.validate_plan(plan)


def test_a_job_result_or_a_replan_never_reopens_what_the_owner_closed_but_their_own_words_do(store):
    cfg = config()
    _closed_by_owner(store, "pg", "Reinfección Postgres Brightbot 203.0.113.44")
    extract = extract_step.validate_extract(extract_answer(category="work_item"))
    result = act.apply(store, cfg, signals=[], extract=extract, plan=_replan(), now_dt=NOW, preferences=[], existing_task=store.get_task("pg"))
    assert store.get_task("pg")["status"] == "done" and result.task_status == "done"
    assert result.disposition == "silent" and result.attention_reason == "owner_closed" and result.proposals == []
    assert store.list_proposals(task_id="pg") == []
    [audit] = [a for a in store.list_audit(subject_id="pg") if a["event_type"] == "pipeline"]
    assert audit["metadata_redacted"]["kept_closed"] == "owner_closed"
    # Not even as news of a finished task.
    news = act.apply(store, cfg, signals=[], extract=extract, plan=_replan(status="done", proposals=[]), now_dt=NOW, preferences=[], existing_task=store.get_task("pg"))
    assert news.disposition == "silent" and store.list_proposals(task_id="pg") == []
    # The owner's own words (an answer, a chat turn, their mail) are the one thing that reopens it.
    theirs = act.apply(store, cfg, signals=[], extract=extract, plan=_replan(), now_dt=NOW, preferences=[], existing_task=store.get_task("pg"), owner_bundle=True)
    assert store.get_task("pg")["status"] == "open" and theirs.disposition != "silent" and len(theirs.proposals) == 1
    # A task the planner closed on evidence (not the owner) still reopens when something new arrives.
    store.create_task(task_id="ev", kind="work_item", title="Otra cosa", summary="", entities={}, expectations=[], checklist=[], now=T)
    store.set_task_status("ev", "done", now=T, reason="the receipt matched the invoice")
    plan = reason.validate_plan(plan_answer(task_match={"decision": "contradicts", "task_id": "ev", "why": "refund"}, task={**plan_answer()["task"], "kind": "work_item", "status": "open"}, proposals=STEP))
    act.apply(store, cfg, signals=[], extract=extract, plan=plan, now_dt=NOW, preferences=[], existing_task=store.get_task("ev"))
    assert store.get_task("ev")["status"] == "open"
    assert act.owner_closed({"status": "done", "closed_reason": "owner_says_done"}) and act.owner_closed({"status": "abandoned", "muted": 1, "closed_reason": None})
    assert not act.owner_closed({"status": "open", "closed_reason": "owner_says_done"}) and not act.owner_closed({"status": "done", "closed_reason": "job_started"})


def test_a_message_in_a_thread_or_on_a_subject_the_owner_closed_lands_on_it_quietly(store):
    cfg = config()
    ten_days_ago = (NOW - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    first = _mail(store, "m1", "thread:eunet:1", "Nova Verifica application", sender="backend@eunet.example")
    _closed_by_owner(store, "eunet", "Revisar solicitud de Nova Verifica en EUNET", when=ten_days_ago, signal=first)
    again = _mail(store, "m2", "thread:eunet:1", "Re: Nova Verifica application", sender="backend@eunet.example")
    plan = reason.validate_plan(plan_answer(task={**plan_answer()["task"], "kind": "commitment", "title": "Completar la ficha de Nova Verifica"}))
    result = act.apply(store, cfg, signals=[again], extract=extract_step.validate_extract(extract_answer(category="commitment")), plan=plan, now_dt=NOW, preferences=[])
    assert result.decision == "suppressed" and result.task_id == "eunet" and result.disposition == "silent"
    assert {s["id"]: s["role"] for s in store.signals_for_task("eunet")} == {"m1": "origin", "m2": "context"}  # visible on the task, in the panel
    assert len(store.list_tasks(statuses=None)) == 1
    # The same subject under another kind and another thread, within the window: still the owner's closed subject.
    _closed_by_owner(store, "berlin", "Gala Avilés 13-nov y viaje Berlín 3-4 dic: noche EUROTECH y vuelos", kind="commitment", when=ten_days_ago)
    assert act.recently_resolved(store, "trip", "Viaje Berlín 3-4 dic: noche EUROTECH y vuelos", NOW) == "berlin"
    assert act.recently_resolved(store, "trip", "Viaje Berlín 3-4 dic: noche EUROTECH y vuelos", NOW + timedelta(days=act.OWNER_CLOSED_DAYS)) == ""
    assert act.recently_resolved(store, "trip", "Reserva de hotel en Lisboa", NOW) == ""
    # Closed by the planner, not the owner: only the short, same-kind cooldown applies.
    store.create_task(task_id="plan", kind="purchase", title="Factura Linode 30000002 de 84 USD", summary="", entities={}, expectations=[], checklist=[], now=ten_days_ago)
    store.set_task_status("plan", "done", now=ten_days_ago, reason="receipt matched")
    assert act.recently_resolved(store, "purchase", "Factura Linode 30000002 de 84 USD del 1 nov", NOW) == ""
    # The same subject closed minutes ago, but another item: a receipt that reached another mailbox, or another number.
    store.create_task(task_id="r-work", kind="purchase", title="Acme receipt 10 USD of 8 December", summary="",
                      entities={"identifiers": [{"label": "Receipt no.", "value": "1502-7720"}]}, expectations=[], checklist=[], now=T)
    store.link_task_signal("r-work", _mail(store, "m-work", "thread:a@example.org:w1", "Your Acme receipt", sender="receipts@acme.example")["id"], "origin", T)
    store.set_task_status("r-work", "done", now=T, reason="left unregistered by preference")
    same = "Acme receipt 10 USD of 8 December"
    assert act.recently_resolved(store, "purchase", same, NOW) == "r-work"
    assert act.recently_resolved(store, "purchase", same, NOW, account="a@example.org", identifiers=["1502-7720"]) == "r-work"
    assert act.recently_resolved(store, "purchase", same, NOW, account="personal@example.org") == ""  # another mailbox
    assert act.recently_resolved(store, "purchase", same, NOW, identifiers=["1502-7718"]) == ""  # another receipt
    assert act.another_item(store, store.get_task("r-work"), account="A@example.org", identifiers=["1502-7720"]) is False
    assert act.another_item(store, store.get_task("plan"), account="x@example.org", identifiers=["1"]) is False  # nothing known: no judgement
    # Through act.apply: the second receipt of the day, in the other mailbox, is a task of its own.
    store.add_signal(signal_id="m-personal", source_id="src", kind="email", occurred_at=T, external_ref="m-personal", group_key="thread:personal@example.org:p1",
                     title="Your Acme receipt", snippet="", meta={"sender": "receipts@acme.example", "account": "personal@example.org"}, fingerprint="m-personal", now=T)
    extract = extract_step.validate_extract(extract_answer(category="purchase", title=same, entities={"identifiers": [{"label": "Receipt no.", "value": "1502-7718"}]}))
    plan = reason.validate_plan(plan_answer(task={**plan_answer()["task"], "kind": "purchase", "title": same}, proposals=[]))
    result = act.apply(store, cfg, signals=[store.get_signal("m-personal")], extract=extract, plan=plan, now_dt=NOW, preferences=[])
    assert result.decision == "new" and result.task_id not in ("r-work", None)


def test_a_step_radar_chose_whose_task_closed_meanwhile_is_filed_without_a_plan_or_a_word(store):
    from radar.jobs.runner import JobOutcome
    from radar.jobs.service import enqueue_job_for_proposal, record_job_outcome

    cfg = active_config()
    store.create_task(task_id="pg", kind="work_item", title="Reinfección Postgres", summary="", entities={}, expectations=[], checklist=[], now=T)
    store.create_task_proposal(proposal_id="p1", task_id="pg", title="Comprobar el host", reason_text="", action_class="read", operation_id="investigate",
                               params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fp1", now=T, batch_key="auto")
    store.respond_proposal("p1", decision="accepted", response={"auto": True}, now=T)
    assert enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("p1"), option="do", now_dt=NOW)
    claimed = store.claim_queued_jobs(T, "2026-10-19T10:10:00Z", 2)[0]
    record_job_outcome(store, cfg, job=claimed, outcome=JobOutcome("succeeded", summary="El proceso sigue corriendo desde /tmp.", result={"findings": ["crontab @reboot"]}), now_dt=NOW)
    store.set_task_status("pg", "done", now=T, reason="owner_says_done")  # «ciérrala», while the job was running
    llm = FakeLlm({})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    [bundle] = store.claim_signal_bundles(T, "2026-10-19T10:10:00Z", 5)
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["result"] == "task_closed" and llm.calls == []  # no plan, no model call
    [signal] = [s for s in store.list_signals() if s["kind"] == "job_result"]
    assert signal["triage_status"] == "ignored" and signal["triage_reason"] == "task_closed"
    assert store.get_task("pg")["status"] == "done" and store.list_outbox(limit=5) == []
    assert any(s["id"] == signal["id"] and s["role"] == "context" for s in store.signals_for_task("pg"))
    assert [a["decision_code"] for a in store.list_audit(subject_id="pg") if a["event_type"] == "pipeline"] == ["task_closed"]
