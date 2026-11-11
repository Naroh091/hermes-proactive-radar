"""What the review of 9-11 November 2026 fixed: of 29 notices in two days, 11 were noise.

A task born in a chat restated as a question what the owner had just worked through there; an invoice they had closed
came back from another task; a choice between two things was to be answered with «reply 👍 OK»; decisions went out
under «yes, correct / no, a mistake»; two interruptions landed before 08:00 for a publication they had scheduled
themselves; «see it first» sent its detail twice; a sender Radar itself found suspicious was asked about.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from conftest import NOW, active_config, config, extract_answer, plan_answer, verify_destination

from radar.delivery import telegram_ui
from radar.delivery.dispatch import settled_questions
from radar.delivery.remind import _item, compose, one_question, without_reply_word
from radar.pipeline import act, extract as extract_step, reason
from radar.scheduler import Scheduler
from radar.transports import TransportRegistry

T = "2026-10-19T10:00:00Z"
SID = "20261019_080000_aaaaaa"
ASK = {"title": "¿Limito el puerto 8000 a Tailscale?", "rationale": "", "action_class": "read", "operation": "ask", "when": "", "urgency": "normal",
       "options": [{"id": "limit", "label": "Limitar a Tailscale"}, {"id": "leave", "label": "Dejarlo abierto"}],
       "fact": {"key": "puerto_8000_tailscale", "yes": "limitar", "no": "abierto"}, "then": "step"}


def _task(store, task_id="t", *, chat=True, due_at=None, title="Exporters de babor: puerto 8000", entities=None, kind="work_item"):
    store.create_task(task_id=task_id, kind=kind, title=title, summary="", entities={**({"session_id": SID} if chat else {}), **(entities or {})},
                      expectations=[], checklist=[], now=T, due_at=due_at)
    return store.get_task(task_id)


def _plan(task_id="t", *, proposals=(ASK,), checks=(), notify=None, due_at="", message="El 8000 responde por Tailscale.", escalation="ask"):
    return reason.validate_plan(plan_answer(
        task_match={"decision": "advances", "task_id": task_id, "why": "same task"},
        task={"kind": "work_item", "title": "", "summary": "", "status": "needs_user", "due_at": due_at, "next_review_at": "", "confidence": 0.8},
        checklist=[], checks=list(checks), proposals=[dict(p) for p in proposals],
        notify=notify or {"disposition": "digest", "urgency": "today", "importance": "high", "reason": "x"},
        message=message, escalation={"decision": escalation, "why": "x"}))


def _apply(store, cfg, plan, task_id="t", **kwargs):
    return act.apply(store, cfg, signals=[], extract=kwargs.pop("extract", None), plan=plan, now_dt=kwargs.pop("at", NOW), preferences=[],
                     existing_task=store.get_task(task_id), **kwargs)


def _batches(store, task_id="t"):
    return {p["title"]: p["batch_key"] for p in store.list_proposals(task_id=task_id)}


# -- a chat-born task waits in the panel until something new happens, unless it has a deadline


def test_a_review_of_a_chat_task_with_nothing_new_leaves_its_question_in_the_panel(store):
    cfg = config()
    _task(store)
    result = _apply(store, cfg, _plan(), trigger="review")
    assert result.disposition == "silent" and _batches(store) == {ASK["title"]: "silent"}  # important, and still not sent
    [audit] = [a for a in store.list_audit(subject_id="t") if a["event_type"] == "pipeline"]
    assert audit["metadata_redacted"]["panel"] == "chat_nothing_new"


def test_the_result_of_a_step_is_something_new_and_reaches_the_owner_in_the_reminder(store):
    cfg = config()
    _task(store)
    result = _apply(store, cfg, _plan(), trigger="job_result")
    assert result.disposition == "digest" and _batches(store) == {ASK["title"]: "digest"}
    # A later review with nothing new does not take it back from the reminder.
    _apply(store, cfg, _plan(), trigger="review", at=NOW + timedelta(minutes=20))
    assert _batches(store) == {ASK["title"]: "digest"}


def test_a_chat_task_with_a_deadline_is_still_reminded_at_its_review(store):
    cfg = config()
    _task(store, due_at="2026-10-30T10:00:00Z")
    result = _apply(store, cfg, _plan(due_at="2026-10-30T10:00:00Z"), trigger="review")
    assert result.disposition == "digest" and _batches(store) == {ASK["title"]: "digest"}


def test_a_task_born_from_mail_is_reviewed_as_before(store):
    cfg = config()
    _task(store, chat=False)
    assert _apply(store, cfg, _plan(), trigger="review").disposition == "digest" and _batches(store) == {ASK["title"]: "digest"}


# -- what Radar raised itself in a chat task does not interrupt


def test_radars_own_step_in_a_chat_task_never_interrupts_unless_the_task_is_due_within_a_day(store):
    cfg = config()
    now = {"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}
    check = {"description": "¿Es correcto publicar las reseñas como experiencia propia?", "severity": "high", "ask_user": True}
    _task(store)
    result = _apply(store, cfg, _plan(proposals=(), checks=(check,), notify=now), trigger="job_result")
    assert result.disposition == "digest" and set(_batches(store).values()) == {"digest"}  # a high check there is no booking mistake
    assert all((p["params"] or {}).get("own_doing") for p in store.list_proposals(task_id="t"))
    _task(store, "far", due_at="2026-10-22T06:00:00Z")
    assert _apply(store, cfg, _plan("far", notify=now, due_at="2026-10-22T06:00:00Z"), "far", trigger="job_result").disposition == "digest"
    _task(store, "near", due_at="2026-10-19T16:00:00Z")
    assert _apply(store, cfg, _plan("near", notify=now, due_at="2026-10-19T16:00:00Z"), "near", trigger="job_result").disposition == "notify_now"
    assert _batches(store, "near") == {ASK["title"]: "now"}
    # A mail about the same chat-born task is not Radar's own doing: it interrupts as any other.
    _task(store, "mail")
    assert _apply(store, cfg, _plan("mail", notify=now), "mail").disposition == "notify_now"


def test_what_radar_raised_itself_never_crosses_quiet_hours(store):
    cfg = active_config(notifications={"quiet_start": "00:00", "quiet_end": "08:00"})
    scheduler = Scheduler(store, cfg, worker_id="w", transports=TransportRegistry(), clock=lambda: NOW)
    night = NOW.replace(hour=3)  # 05:00 in Madrid, with a publication at 06:50
    payload = {"urgent": True, "deadline": "2026-10-19T04:50:00Z"}
    assert scheduler._crosses_quiet(payload, night) is True
    assert scheduler._crosses_quiet({**payload, "own_doing": True}, night) is False
    assert scheduler._crosses_quiet({**payload, "own_doing": True, "protected": True}, night) is True  # a security flag still does


# -- who wrote does not change with what a lookup finds


def test_a_cold_sender_stays_cold_after_the_lookup_radar_ran_on_their_mail(store):
    cfg = config()
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=T)
    store.add_signal(signal_id="m1", source_id="src", kind="email", occurred_at=T, external_ref="m1", group_key="thread:1", title="Data found in breach records",
                     snippet="", meta={"sender": "dana@coldsender.example", "account": "a@example.org"}, fingerprint="m1", now=T)
    lookup = {"title": "Verificar el dominio del remitente", "rationale": "", "action_class": "read", "operation": "web.read", "when": "", "urgency": "normal",
              "options": [{"id": "do", "label": "Verificar"}]}
    new = reason.validate_plan(plan_answer(task={"kind": "check", "title": "Aviso no solicitado de datos filtrados", "summary": "", "status": "open", "due_at": "",
                                                 "next_review_at": "", "confidence": 0.7}, checklist=[], proposals=[lookup],
                                           notify={"disposition": "silent", "urgency": "whenever", "importance": "low", "reason": "x"}, escalation={"decision": "handle", "why": "x"}))
    extract = extract_step.validate_extract(extract_answer(category="check", sender_kind="cold", facts_learned=[]))
    first = act.apply(store, cfg, signals=[store.get_signal("m1")], extract=extract, plan=new, now_dt=NOW, preferences=[])
    assert store.get_task(first.task_id)["entities"]["sender_kind"] == "cold"
    ask = {**ASK, "title": "¿Usas ese correo en cuentas con contraseña?", "fact": {"key": "correo_con_password", "yes": "si", "no": "no"}}
    replan = _plan(first.task_id, proposals=(ask,))  # the job result re-plans on a synthetic extract that knows no sender
    result = act.apply(store, cfg, signals=[], extract={}, plan=replan, now_dt=NOW + timedelta(minutes=10), preferences=[],
                       existing_task=store.get_task(first.task_id), trigger="job_result")
    assert [p["title"] for p in store.list_proposals(task_id=first.task_id) if p["operation_id"] == "ask"] == [] and result.proposals == []


# -- what the owner closed does not come back through another task


def test_identifier_tokens_are_references_never_dates_or_plain_words():
    found = act.identifier_tokens("Factura rectificativa FR-2026-901 de tarjeta gráfica, pedido RF-2026-11-901, plazo 2026-12-09 a las 06:50, 648,50 €, pedido 200005 y PR #2038")
    assert found == {"fr-2026-901", "rf-2026-11-901", "200005"}


def test_a_question_naming_what_the_owner_closed_in_another_task_is_not_asked(store):
    cfg = config()
    _task(store, "invoice", chat=False, kind="purchase", title="Factura rectificativa FR-2026-901 de tarjeta gráfica, pedido RF-2026-11-901")
    store.set_task_status("invoice", "done", now=T, reason="owner_says_done")
    _task(store, "gpu", chat=False, kind="purchase", title="Compra GPU MSI 4060 Ti para babor")
    check = {"description": "¿Es correcta la factura FR-2026-901 a nombre de Fundación Ejemplo?", "severity": "warning", "ask_user": True}
    serial = {**ASK, "title": "¿Pido al vendedor que confirme el número de serie?", "fact": {"key": "gpu_serie", "yes": "si", "no": "no"}}
    result = _apply(store, cfg, _plan("gpu", proposals=(serial,), checks=(check,)), "gpu")
    assert [p["title"] for p in store.list_proposals(task_id="gpu")] == [serial["title"]] and len(result.proposals) == 1
    [audit] = [a for a in store.list_audit(subject_id="gpu") if a["event_type"] == "pipeline"]
    assert audit["metadata_redacted"]["closed_subject"] == 1
    # Their own words about it may raise it again.
    _task(store, "gpu2", chat=False, kind="purchase", title="Garantía de la GPU")
    _apply(store, cfg, _plan("gpu2", proposals=(), checks=(check,)), "gpu2", owner_bundle=True)
    assert [p["operation_id"] for p in store.list_proposals(task_id="gpu2")] == ["check"]


def test_a_question_the_owner_already_answered_is_on_record_and_never_asked_again(store):
    cfg = config()
    _task(store, chat=False)
    store.record_owner_fact(key="puerto_8000_tailscale", value="limitar", provenance={"at": T}, now=T)
    assert _apply(store, cfg, _plan()).proposals == [] and store.list_proposals(task_id="t") == []


def test_the_same_question_is_not_asked_from_two_tasks(store):
    cfg = config()
    _task(store, "a", chat=False, title="Reseñas de casaseca")
    _task(store, "b", chat=False, title="Guías de casaseca")
    first = _apply(store, cfg, _plan("a"), "a")
    store.mark_proposals_sent(first.proposals, None, T)
    assert _apply(store, cfg, _plan("b"), "b", at=NOW + timedelta(hours=1)).proposals == []
    assert len(_apply(store, cfg, _plan("b"), "b", at=NOW + timedelta(hours=30)).proposals) == 1  # a day later it is no longer in front of them


# -- one question, and only the answers that exist


def test_a_decision_in_words_is_composed_without_answers_and_loses_the_reply_ok_sentence():
    decision = {"id": "p", "title": "Baja de Seguros Norte", "operation_id": "inform", "action_class": "read",
                "params": {"ask": True, "message": "La póliza vence el 1 de diciembre."}, "options": [{"id": "ok", "label": "👍 Vale"}]}
    item = _item({"task": {"id": "t", "title": "Baja de Seguros Norte Hogar", "kind": "commitment"}, "decision": decision, "news": []})
    assert item["pending"]["kind"] == "open_question" and item["pending"]["answers"] == []
    text = "La póliza vence el 1 de diciembre. ¿Pido la baja por teléfono o por escrito? Responde «vale» para confirmarlo. Hay otras 9 cosas esperando."
    assert without_reply_word(text, "es") == "La póliza vence el 1 de diciembre. ¿Pido la baja por teléfono o por escrito? Hay otras 9 cosas esperando."
    assert without_reply_word("Los clones ya están. ¿Sustituyen a los originales? Responde 👍 si es así.", "es") == "Los clones ya están. ¿Sustituyen a los originales?"
    assert without_reply_word("Vale la pena revisarlo hoy. ¿Lo miras?", "es") == "Vale la pena revisarlo hoy. ¿Lo miras?"  # before the question: left alone


def test_a_message_with_one_decision_asks_one_question(tmp_path):
    two = "mgarcia ya está creado. Queda atc, que da 401: ¿quieres que lo reactive? Además, la contraseña de jlopez sigue en correos antiguos, así que ¿la rotamos ya?"
    assert one_question(two) == "mgarcia ya está creado. Además, la contraseña de jlopez sigue en correos antiguos, así que ¿la rotamos ya?"
    assert one_question("Todo listo. ¿Lo envío?") == "Todo listo. ¿Lo envío?"
    decision = {"id": "p", "title": "Rotar la contraseña de jlopez", "operation_id": "ask", "action_class": "read", "params": {},
                "options": [{"id": "rotate", "label": "Rotar la contraseña"}, {"id": "no", "label": "No"}]}
    gate = SimpleNamespace(available=True)

    async def structured(*args, **kwargs):
        return {"text": two}

    gate.structured = structured
    text = asyncio.run(compose(gate, items=[{"task": {"id": "t", "title": "Caddyfile de mediahub", "kind": "check"}, "decision": decision, "news": []}], language="es"))
    assert text.count("?") == 1 and text.endswith("¿la rotamos ya?")


def test_what_the_owner_answered_travels_to_the_composer_as_settled(store):
    _task(store, chat=False)
    store.create_task_proposal(proposal_id="c", task_id="t", title="¿Es correcta la factura a nombre de la Fundación?", reason_text="", action_class="read",
                               operation_id="check", params={}, options=[{"id": "ok", "label": "👍 Sí, es correcto"}, {"id": "wrong", "label": "⚠️ No, es un error"}],
                               urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="c", now=T, batch_key="digest")
    store.mark_proposals_sent(["c"], None, T)
    store.respond_proposal("c", decision="declined", response={"actor": "telegram:7", "option": "ok"}, now=T)
    settled = settled_questions(store, "t")
    assert settled == [{"question": "¿Es correcta la factura a nombre de la Fundación?", "answer": "👍 Sí, es correcto"}]
    item = _item({"task": store.get_task("t"), "decision": None, "news": [], "settled": settled})
    assert item["settled"] == settled


# -- a check quotes the two things that disagree


def test_a_decision_dressed_as_a_check_is_sent_back_to_the_planner_once_and_then_not_asked():
    decision = {"description": "¿Subo la memoria del worker y reintento?", "severity": "high", "ask_user": True, "evidence": []}
    real = {"description": "¿Es correcta la talla 48 del pedido?", "severity": "warning", "ask_user": True, "evidence": ["pedido: talla 48", "dato del dueño: usa la 42"]}
    plan = reason.validate_plan(plan_answer(checks=[decision, real]))
    assert [bool(c.get("unbacked")) for c in plan["checks"]] == [True, False]
    [issue] = reason.defects(plan)
    assert "Subo la memoria" in issue and '"ask"' in issue
    assert [c["ask_user"] for c in reason.settle(plan)["checks"]] == [False, True]
    assert reason.defects(reason.validate_plan(plan_answer(checks=[{"description": "¿Talla 48?", "severity": "high", "ask_user": True}]))) == []  # older plans are trusted

    fixed = plan_answer(checks=[real], proposals=[{**ASK, "title": "¿Subo la memoria del worker y reintento?"}])
    answers = [plan_answer(checks=[decision, real]), fixed]
    calls: list[dict] = []

    async def fake_plan(gate, **kwargs):
        calls.append(kwargs)
        return reason.validate_plan(answers.pop(0))

    original, reason.plan = reason.plan, fake_plan
    try:
        result = asyncio.run(reason.plan_checked(None, now=T))
    finally:
        reason.plan = original
    assert len(calls) == 2 and "Subo la memoria" in calls[1]["repair"][0]
    assert [c["description"] for c in result["checks"] if c["ask_user"]] == [real["description"]] and result["proposals"][0]["operation"] == "ask"


def test_a_question_in_the_message_with_no_way_to_answer_is_a_defect():
    words = reason.validate_plan(plan_answer(proposals=[], message="La póliza vence el 1 de diciembre. ¿Pido la baja por teléfono o por escrito?",
                                             escalation={"decision": "ask", "why": "x"}))
    assert len(reason.defects(words)) == 1
    told = reason.validate_plan(plan_answer(proposals=[], message="La póliza vence el 1 de diciembre.", escalation={"decision": "ask", "why": "x"}))
    assert reason.defects(told) == [] and reason.defects(reason.validate_plan(plan_answer())) == []


# -- «see it first», tapped twice


def test_a_second_tap_on_see_it_first_does_not_send_the_detail_again(store):
    verify_destination(store)
    _task(store, chat=False)
    store.record_sent(outbox_id="d1", destination_id="configured:primary", notification_key="detail:p:1", payload={"kind": "detail", "proposal_ids": ["p"], "text": "x"},
                      platform_message_id="m1", now=T, task_id="t", kind="detail")
    assert telegram_ui._detail_just_sent(store, "p", NOW + timedelta(seconds=9)) is True
    assert telegram_ui._detail_just_sent(store, "p", NOW + timedelta(minutes=5)) is False
    assert telegram_ui._detail_just_sent(store, "other", NOW + timedelta(seconds=9)) is False
