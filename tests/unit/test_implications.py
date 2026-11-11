"""The reasoning leap: a signal implies things, the model says which, the code asks memory, the planner settles them."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from conftest import NOW, FakeContext, FakeLlm, active_config, extract_answer, plan_answer
from test_recall import HINDSIGHT, FakeProvider

from radar.pipeline import act, extract as extract_step, reason
from radar.pipeline.process import process_bundle, review_task
from radar.pipeline.recall import MemoryRecall, Recall
from radar.providers.base import MailMessage
from radar.runtime import Runtime
from radar.sources.session import SOURCE_ID

T0 = "2026-10-19T10:00:00Z"
SID = "20261019_090000_a1b2c3"
HTML_SNIPPET = "[Note: This email is HTML-formatted. Plain text version not available.] <!DOCTYPE HTML><html><head><style>"
IMPLICATIONS = [
    {"concern": "Seguro del coche de alquiler", "why": "OK Mobility solo incluye cobertura básica", "settled_by": "mailbox",
     "memory_question": "¿Qué seguro de coche tiene el dueño?"},
    {"concern": "Alojamiento 12-15 nov en Asturias", "why": "Tres noches fuera de casa", "settled_by": "owner",
     "memory_question": "¿Tiene el dueño familia o casa en Asturias?"},
]


def test_the_validator_bounds_implications():
    many = [{"concern": f"c{i}", "why": "w", "settled_by": "web", "memory_question": "q"} for i in range(9)]
    out = extract_step.validate_extract(extract_answer(implications=many + [{"concern": "", "settled_by": "owner"}]))
    assert len(out["implications"]) == 8 and out["implications"][0] == {"concern": "c0", "why": "w", "settled_by": "web", "memory_question": "q"}
    out = extract_step.validate_extract(extract_answer(implications=[{"concern": "x", "settled_by": "nope", "memory_question": "ignored"}]))
    assert out["implications"] == [{"concern": "x", "why": "", "settled_by": "evidence", "memory_question": ""}]
    assert extract_step.validate_extract(extract_answer())["implications"] == []
    assert "implications:" in extract_step.EXTRACT_PROMPT and "not resolved in the sense that matters" in extract_step.EXTRACT_PROMPT
    # The owner's rule (2026-11-03): look before asking — a lookup that could settle or narrow a point comes first.
    assert "Look before asking" in extract_step.EXTRACT_PROMPT and "look before asking" in reason.PLAN_PROMPT


def test_implications_are_kept_on_the_task_and_merged_on_replan(store):
    cfg = active_config()
    llm = FakeLlm({"radar.extract": [extract_answer(implications=IMPLICATIONS)], "radar.plan": [plan_answer(proposals=[])]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=T0)
    _mail_signal(store, "s1", content_ref="")
    outcome = asyncio.run(process_bundle(rt, store.claim_signal_bundles(T0, "2026-10-19T10:10:00Z", 5)[0], NOW))
    task = store.get_task(outcome["task_id"])
    assert task["entities"]["implications"] == IMPLICATIONS
    # A later bundle on the same task adds what is new and never repeats what is known.
    licence = {"concern": "Carné de conducir en vigor", "why": "Lo piden al recoger", "settled_by": "evidence", "memory_question": ""}
    later = extract_step.validate_extract(extract_answer(implications=[IMPLICATIONS[0], licence]))
    plan = reason.validate_plan(plan_answer(task_match={"decision": "advances", "task_id": task["id"], "why": "same booking"}, proposals=[]))
    act.apply(store, cfg, signals=[], extract=later, plan=plan, now_dt=NOW, preferences=[], existing_task=task)
    assert [i["concern"] for i in store.get_task(task["id"])["entities"]["implications"]] == [i["concern"] for i in IMPLICATIONS] + ["Carné de conducir en vigor"]


def test_the_pipeline_asks_memory_what_each_implication_needs_and_remembers_the_history(store, tmp_path):
    cfg = active_config()
    entities = {**extract_answer()["entities"], "places": ["Asturias"]}
    llm = FakeLlm({"radar.extract": [extract_answer(implications=IMPLICATIONS, entities=entities)],
                   "radar.plan": [plan_answer(proposals=[]), plan_answer(task_match={"decision": "advances", "task_id": "", "why": "review"}, proposals=[])]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    provider = FakeProvider(HINDSIGHT, json.dumps({"result": "Los padres del dueño viven en Gijón."}))
    rt.recall = Recall(rt.ctx, cfg, memory=MemoryRecall(cfg, provider=provider), search_fn=lambda q, l: json.dumps({"success": True, "results": []}))
    # A trip the owner already made to the same place is history the planner sees («has been there before»).
    store.create_task(task_id="old-trip", kind="trip", title="Fin de semana en Asturias", summary="Viaje en mayo", entities={"places": ["Asturias"]},
                      expectations=[], checklist=[], now="2026-05-01T10:00:00Z")
    store.set_task_status("old-trip", "done", now="2026-05-04T10:00:00Z", reason="back home")
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=T0)
    _mail_signal(store, "s1", content_ref="")
    outcome = asyncio.run(process_bundle(rt, store.claim_signal_bundles(T0, "2026-10-19T10:10:00Z", 5)[0], NOW))
    questions = {IMPLICATIONS[0]["memory_question"], IMPLICATIONS[1]["memory_question"]}
    assert {args["query"] for _, args in provider.calls} >= questions and outcome["recall"]["answers"] == 2
    sent = json.dumps(next(c for c in llm.calls if c.get("purpose") == "radar.plan"), default=str, ensure_ascii=False)
    assert "Los padres del dueño viven en Gijón" in sent and "Fin de semana en Asturias" in sent and "Seguro del coche de alquiler" in sent
    audit = next(a for a in store.list_audit(subject_id=outcome["task_id"]) if a.get("event_type") == "pipeline")
    assert (audit.get("metadata") or audit.get("metadata_redacted") or {})["recall"]["answers"] == 2  # the only place recall is visible
    # A review asks memory again from what the task remembers: what it learnt since then counts.
    provider.calls.clear()
    review = asyncio.run(review_task(rt, store.get_task(outcome["task_id"]), NOW))
    assert {args["query"] for _, args in provider.calls} >= questions and review["recall"]["answers"] == 2


def test_a_booked_trip_stays_open_until_it_ends(store):
    cfg = active_config()
    entities = {**extract_answer()["entities"], "dates": [{"label": "recogida", "when": "2026-11-12T10:00:00Z"}, {"label": "devolución", "when": "2026-11-15T13:00:00Z"}]}
    done = {**plan_answer()["task"], "status": "done", "due_at": ""}
    llm = FakeLlm({"radar.extract": [extract_answer(title="Reserva OK Mobility 10000001 completada", entities=entities)], "radar.plan": [plan_answer(task=done, proposals=[])]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=T0)
    _mail_signal(store, "s1", content_ref="")
    outcome = asyncio.run(process_bundle(rt, store.claim_signal_bundles(T0, "2026-10-19T10:10:00Z", 5)[0], NOW))
    task = store.get_task(outcome["task_id"])
    assert task["status"] == "open"  # «reserva completada» is a booked step, not a trip that ended
    audit = next(a for a in store.list_audit(subject_id=task["id"]) if a.get("event_type") == "pipeline")
    assert (audit.get("metadata") or audit.get("metadata_redacted") or {})["kept_open"] == "future_dates"
    # The owner's word (a bundle that completes it) or a cancellation still ends it.
    plan = reason.validate_plan(plan_answer(task_match={"decision": "completes", "task_id": task["id"], "why": "coche devuelto"}, task=done, proposals=[]))
    act.apply(store, cfg, signals=[], extract=None, plan=plan, now_dt=NOW, preferences=[], existing_task=task)
    assert store.get_task(task["id"])["status"] == "done"
    assert "3b. implications" in reason.PLAN_PROMPT and "never the end of the trip" in reason.PLAN_PROMPT
    # A trip whose dates are past closes as the planner says.
    old = store.get_task(task["id"])
    assert not act.lies_ahead({"entities": {"dates": [{"when": "2026-10-01"}]}, "due_at": "2026-10-02T00:00:00Z"}, NOW) and act.lies_ahead(old, NOW)


def test_a_read_step_of_a_trip_is_briefed_to_find_out_not_to_debug():
    from radar.jobs.prompts import build_job_prompt

    step = {"title": "Buscar en el correo el seguro del coche", "reason_text": "OK Mobility solo cubre lo básico", "operation_id": "investigate", "params": {}}
    trip = build_job_prompt(task={"kind": "trip", "title": "Viaje a Asturias"}, proposal=step, option="do", action_class="read", signals=[], facts=[], owner={"language": "es"}, workdir=None)
    assert "HOW TO FIND OUT WHETHER THE OWNER ALREADY HAS SOMETHING" in trip and "HOW TO WORK A BUG" not in trip and "Never contract, book or pay" in trip
    bug = build_job_prompt(task={"kind": "work_item", "title": "Feed roto"}, proposal=step, option="do", action_class="read", signals=[], facts=[], owner={"language": "es"}, workdir=None)
    assert "HOW TO WORK A BUG" in bug and "HOW TO FIND OUT" not in bug
    external = build_job_prompt(task={"kind": "trip", "title": "Viaje"}, proposal=step, option="do", action_class="external", signals=[], facts=[], owner={}, workdir=None)
    assert "HOW TO FIND OUT" not in external


def test_a_chat_that_books_something_for_later_becomes_a_task_with_implications(store, tmp_path):
    cfg = active_config()
    store.ensure_local_observation_source(T0, 30)
    turns = [("alquila un coche automático en Asturias del 12 al 15", "Reservado en OK Mobility, 130 €, recogida en el aeropuerto el 12 a las 10:00."),
             ("perfecto, gracias", "De nada.")]
    for index, (said, replied) in enumerate(turns):
        at = f"2026-10-19T09:0{index}:00Z"
        store.add_signal(signal_id=f"t{index}", source_id=SOURCE_ID, kind="session_turn", occurred_at=at, external_ref=f"t{index}", group_key=f"session:{SID}",
                         title=said[:80], snippet=said, meta={"session_id": SID, "platform": "desktop", "user_text": said, "assistant_text": replied},
                         fingerprint=f"t{index}", now=at)
    llm = FakeLlm({"radar.extract": [extract_answer(title="Viaje a Asturias 12-15 nov con coche de alquiler", conversation_state="deferred", implications=IMPLICATIONS)],
                   "radar.plan": [plan_answer(proposals=[])]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    outcome = asyncio.run(process_bundle(rt, store.claim_signal_bundles(T0, "2026-10-19T10:10:00Z", 5)[0], NOW))
    task = store.get_task(outcome["task_id"])
    assert task["kind"] == "trip" and task["entities"]["session_id"] == SID and task["entities"]["implications"] == IMPLICATIONS


def _mail_signal(store, signal_id: str, *, content_ref: str) -> None:
    store.add_signal(signal_id=signal_id, source_id="src", kind="email", occurred_at=T0, external_ref=signal_id, group_key=f"g-{signal_id}",
                     title="10000001 | Su reserva se ha completado correctamente", snippet=HTML_SNIPPET,
                     meta={"sender": "OK Mobility <no-reply@rent.okmobility.example>", "account": "a@example.org"}, fingerprint=f"f-{signal_id}",
                     now=T0, content_ref=content_ref)


def test_a_signal_without_body_is_read_again_before_extraction(store):
    cfg = active_config()
    llm = FakeLlm({"radar.extract": [extract_answer(), extract_answer()], "radar.plan": [plan_answer(proposals=[]), plan_answer(proposals=[])]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    reads: list[str] = []

    class Provider:
        def read(self, message_id):
            reads.append(message_id)
            if message_id == "m-broken":
                raise RuntimeError("mcp down")
            return MailMessage(id=message_id, subject="10000001 | Su reserva", sender="OK Mobility <no-reply@rent.okmobility.example>",
                               body="Recogida 12/11/2026 10:00 Aeropuerto de Asturias. Devolución 15/11/2026 13:00. Total 130 €.")

    rt.sources = {"src": SimpleNamespace(provider=Provider())}
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=T0)
    _mail_signal(store, "s1", content_ref="gmail:a@example.org:m1")
    bundle = next(b for b in store.claim_signal_bundles(T0, "2026-10-19T10:10:00Z", 5) if b["signals"][0]["id"] == "s1")
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert reads == ["m1"] and outcome["bodies"] == 1
    sent = json.dumps(next(c for c in llm.calls if c.get("purpose") == "radar.extract"), default=str, ensure_ascii=False)
    assert "Aeropuerto de Asturias" in sent and "HTML-formatted" not in sent
    assert store.get_signal("s1")["snippet"].startswith("Recogida 12/11/2026")  # the stored snippet is text from now on
    # A provider that fails leaves the snippet and never blocks the bundle.
    _mail_signal(store, "s2", content_ref="gmail:a@example.org:m-broken")
    bundle = next(b for b in store.claim_signal_bundles(T0, "2026-10-19T10:10:00Z", 5) if b["signals"][0]["id"] == "s2")
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert "bodies" not in outcome and outcome.get("task_id") and reads == ["m1", "m-broken"]
    assert store.get_signal("s2")["snippet"] == HTML_SNIPPET
