"""A drafted mail needs a basis the store can verify; an invitation needs a correspondent the owner knows.

The owner (2026-11-06): no drafts with platitudes («I'll look into it and confirm»); a draft is worth it when it
comes out of work the assistant did, or follows a decision the owner took, including an acknowledgement an
administration or a colleague asked for — never a cold mail asking to confirm something. And invitations are
raised only when there is a history of interaction with that sender or their organisation.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from conftest import NOW, FakeContext, FakeLlm, active_config, config, extract_answer, plan_answer

from radar.pipeline import act, extract as extract_step, reason
from radar.pipeline.correspondents import known_correspondent, party_of, sender_of
from radar.pipeline.process import process_bundle
from radar.prompts import EXTRACT_PROMPT, PLAN_PROMPT
from radar.runtime import Runtime

T = "2026-10-19T10:00:00Z"
REPLY = {"title": "Responder a Pablo", "rationale": "", "action_class": "external", "operation": "gmail.reply", "when": "", "urgency": "normal",
         "options": [{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}]}
ASK = {"title": "¿Confirmamos la cita?", "rationale": "", "action_class": "read", "operation": "ask", "when": "", "urgency": "normal",
       "options": [{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}], "fact": {"key": "cita", "yes": "sí", "no": "no"}, "then": "step"}
LOOK = {"title": "Buscar la factura en el buzón", "rationale": "", "action_class": "read", "operation": "investigate", "when": "", "urgency": "normal", "options": []}


def _mail(store, signal_id, sender, *, group="g", title="Hola", when=T):
    if not any(s["id"] == "src" for s in store.list_sources()):
        store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=T)
    store.add_signal(signal_id=signal_id, source_id="src", kind="email", occurred_at=when, external_ref=signal_id, group_key=group, title=title, snippet="",
                     meta={"sender": sender, "account": "me@example.org", "source_kind": "gmail"}, fingerprint=signal_id, now=when)
    return store.get_signal(signal_id)


def _apply(store, plan, extract, *, signals=(), existing=None, **kwargs):
    return act.apply(store, config(), signals=list(signals), extract=extract, plan=reason.validate_plan(plan), now_dt=NOW, preferences=[],
                     existing_task=existing, **kwargs)


def _plan(proposals, **overrides):
    fields = {"task": {**plan_answer()["task"], "kind": "work_item", "title": "Facturas AWS de Brightbot"}, "proposals": proposals,
              "notify": {"disposition": "digest", "urgency": "today", "importance": "normal", "reason": "x"}}
    fields.update(overrides)
    return plan_answer(**fields)


def _drafts_dropped(store, task_id):
    return [a for a in store.list_audit(subject_id=task_id) if a["event_type"] == "pipeline"][-1]["metadata_redacted"].get("drafts_dropped", 0)


def test_a_draft_goes_out_only_on_a_basis_the_store_can_verify(store):
    mail = _mail(store, "m1", "Pablo Medina <pablo@newsroom.example>")
    courtesy = extract_step.validate_extract(extract_answer(category="work_item"))  # reply_expected "none", sender unknown
    none = _apply(store, _plan([{**REPLY}]), courtesy, signals=[mail])
    assert store.list_proposals(task_id=none.task_id) == [] and _drafts_dropped(store, none.task_id) == 1
    assert reason.validate_plan(_plan([REPLY]))["proposals"][0]["basis"] == ""  # a mail step with no basis is dropped by the code
    # requested_ack: the newest mail asks for an answer and comes from an administration, a colleague or a partner.
    asked = extract_step.validate_extract(extract_answer(category="work_item", reply_expected="ack", sender_kind="colleague"))
    ack = _apply(store, _plan([{**REPLY, "basis": "requested_ack"}]), asked, signals=[mail])
    assert [p["operation_id"] for p in store.list_proposals(task_id=ack.task_id)] == ["gmail.reply"]
    vendor = extract_step.validate_extract(extract_answer(category="work_item", reply_expected="ack", sender_kind="vendor"))
    assert store.list_proposals(task_id=_apply(store, _plan([{**REPLY, "basis": "requested_ack"}]), vendor, signals=[mail]).task_id) == []
    # findings: a step of this task ran and produced something.
    claimed = _apply(store, _plan([{**REPLY, "basis": "findings"}]), courtesy, signals=[mail])
    assert store.list_proposals(task_id=claimed.task_id) == []  # nothing ran: the claim does not hold
    result = extract_step.validate_extract(extract_answer(category="work_item"))
    result["job"] = {"status": "succeeded", "findings": ["invoice EUREES26-000001 found"], "refs": []}
    found = _apply(store, _plan([{**REPLY, "basis": "findings"}]), result, signals=[mail])
    assert [p["operation_id"] for p in store.list_proposals(task_id=found.task_id)] == ["gmail.reply"]
    # owner_decision: the owner answered or wrote about this task.
    store.create_task(task_id="trip", kind="trip", title="Viaje a Berlín", summary="", entities={}, expectations=[], checklist=[], now=T)
    claim = _plan([{**REPLY, "title": "Avisar a Elena de que no asistiré", "basis": "owner_decision"}], task_match={"decision": "advances", "task_id": "trip", "why": "x"})
    silent = _apply(store, claim, courtesy, existing=store.get_task("trip"))
    assert store.list_proposals(task_id="trip") == []
    theirs = _apply(store, claim, courtesy, existing=store.get_task("trip"), owner_bundle=True)
    assert [p["title"] for p in store.list_proposals(task_id="trip")] == ["Avisar a Elena de que no asistiré"]
    assert silent.task_id == theirs.task_id == "trip"
    for phrase in ("Every mail step carries basis", "A mail step with no basis is dropped by the code", 'Never "I will look into it and get back to you"'):
        assert phrase in " ".join(PLAN_PROMPT.split()) or phrase in " ".join(EXTRACT_PROMPT.split()), phrase


def test_a_cold_sender_gets_no_reply_and_raises_no_question(store):
    mail = _mail(store, "m1", "Growth Team <sales@coldpitch.example>")
    cold = extract_step.validate_extract(extract_answer(category="work_item", reply_expected="substantive", sender_kind="cold"))
    result = _apply(store, _plan([{**REPLY, "basis": "requested_ack"}, ASK, LOOK]), cold, signals=[mail])
    assert [p["operation_id"] for p in store.list_proposals(task_id=result.task_id)] == ["investigate"]
    assert _drafts_dropped(store, result.task_id) == 2
    # Nor a decision in words.
    words = _apply(store, _plan([], task={**_plan([])["task"], "status": "needs_user"}, escalation={"decision": "ask", "why": "x"},
                                notify={"disposition": "digest", "urgency": "today", "importance": "high", "reason": "x"},
                                message="¿Confirmas la llamada que proponen?"), cold, signals=[mail])
    assert store.list_proposals(task_id=words.task_id) == []
    assert [a for a in store.list_audit(subject_id=words.task_id) if a["event_type"] == "pipeline"][-1]["metadata_redacted"]["withheld"] == "cold_sender"


def test_an_invitation_from_an_unknown_correspondent_is_recorded_and_raised_nowhere(store):
    mail = _mail(store, "m1", "JEDI <events@jedi-council.example>")
    invite = extract_step.validate_extract(extract_answer(category="commitment", message_kind="invitation", sender_kind="unknown"))
    invite["known_correspondent"] = False
    plan = _plan([ASK, LOOK], task={**plan_answer()["task"], "kind": "commitment", "title": "Moonshot Council 16 nov", "status": "needs_user"},
                 notify={"disposition": "notify_now", "urgency": "today", "importance": "high", "reason": "x"}, message="Te invitan al Moonshot Council.")
    result = _apply(store, plan, invite, signals=[mail])
    task = store.get_task(result.task_id)
    assert task["title"] == "Moonshot Council 16 nov" and store.list_proposals(task_id=task["id"]) == []  # in the panel, nowhere else
    assert result.disposition == "silent"
    assert [a for a in store.list_audit(subject_id=task["id"]) if a["event_type"] == "pipeline"][-1]["metadata_redacted"]["withheld"] == "invitation_unknown"
    # Known correspondent: the usual rules apply.
    invite["known_correspondent"] = True
    known = _apply(store, plan, invite, signals=[_mail(store, "m2", "Irene <irene@uni.example>", group="g2")])
    assert sorted(p["operation_id"] for p in store.list_proposals(task_id=known.task_id)) == ["ask", "investigate"]
    assert "bundle.known_correspondent false" in " ".join(PLAN_PROMPT.split())


class FakeProvider:
    def __init__(self, hits):
        self.hits, self.queries = hits, []

    def search(self, query, *, max_results=20):
        self.queries.append(query)
        return list(self.hits)[:max_results]

    def read(self, message_id):
        return None


def test_known_correspondent_looks_at_the_retained_mail_then_the_sent_folder_and_remembers(store):
    assert party_of("Elena Kovacs <elena@eurotech.example>") == "@eurotech.example" and party_of("pat.sample@gmail.com") == "pat.sample@gmail.com" and party_of("") == ""
    assert sender_of([{"meta": {"owner_sent": True, "sender": "me@example.org"}}, {"meta": {"organizer": "Irene <irene@uni.example>"}}]) == "irene@uni.example"
    now = NOW
    first = _mail(store, "c1", "EUROTECH <events@eurotech.example>", title="Invitation", when="2026-10-19T09:00:00Z")
    rt = SimpleNamespace(store=store, sources={"src": SimpleNamespace(provider=FakeProvider([]))})
    assert asyncio.run(known_correspondent(rt, [first], now)) is False  # nothing retained, nothing sent
    assert rt.sources["src"].provider.queries == ["in:sent to:eurotech.example"]
    # The verdict is kept a month: the same organisation is not looked up again.
    second = _mail(store, "c2", "Elena <elena@eurotech.example>", title="Reminder", when="2026-10-19T09:30:00Z")
    assert asyncio.run(known_correspondent(rt, [second], now)) is False and len(rt.sources["src"].provider.queries) == 1
    [verdict] = store.list_feedback(action="unknown")
    assert verdict["target_type"] == "correspondent" and verdict["target_id"] == "@eurotech.example"
    # Earlier mail from the organisation (or sent to it) makes it known without any search.
    _mail(store, "u0", "Irene <irene@uni.example>", group="old", title="Kick-off", when="2026-09-01T09:00:00Z")
    invite = _mail(store, "u1", "UNAV events <eventos@uni.example>", group="new", title="Invitation", when="2026-10-19T09:40:00Z")
    assert asyncio.run(known_correspondent(rt, [invite], now)) is True and len(rt.sources["src"].provider.queries) == 1
    # A sent mail found by the provider counts too; a provider that fails is skipped.
    hit = _mail(store, "h1", "Olga <olga@avanzatech.example>", group="h", title="Gala", when="2026-10-19T09:50:00Z")
    rt.sources = {"src": SimpleNamespace(provider=FakeProvider([SimpleNamespace(id="x")]))}
    assert asyncio.run(known_correspondent(rt, [hit], now)) is True
    assert asyncio.run(known_correspondent(SimpleNamespace(store=store, sources={}), [{"meta": {}}], now)) is None


def test_the_pipeline_asks_who_the_inviter_is_before_planning_and_keeps_the_task_quiet(store):
    cfg = active_config()
    _mail(store, "m1", "JEDI <events@jedi-council.example>", title="Invitation: Moonshot Council")
    extract = extract_answer(category="commitment", message_kind="invitation", sender_kind="unknown", title="Moonshot Council 16 nov")
    plan = plan_answer(task={**plan_answer()["task"], "kind": "commitment", "title": "Moonshot Council 16 nov", "status": "needs_user"}, proposals=[ASK],
                       notify={"disposition": "notify_now", "urgency": "today", "importance": "high", "reason": "x"}, message="Te invitan.")
    llm = FakeLlm({"radar.extract": [extract], "radar.plan": [plan]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    [bundle] = store.claim_signal_bundles(T, "2026-10-19T10:10:00Z", 5)
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["disposition"] == "silent" and outcome["proposals"] == 0
    sent = __import__("json").loads(llm.calls[1]["input"][0]["text"])
    assert sent["bundle"]["known_correspondent"] is False and sent["bundle"]["message_kind"] == "invitation"
    assert store.list_feedback(action="unknown")[0]["target_id"] == "@jedi-council.example"
    assert store.get_task(outcome["task_id"])["title"] == "Moonshot Council 16 nov"
