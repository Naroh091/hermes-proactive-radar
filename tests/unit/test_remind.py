"""The reminder: one thing, in prose, as an assistant would say it in passing; never an inventory."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from conftest import NOW, FakeContext, FakeLlm, active_config, verify_destination

from radar.delivery.dispatch import build_reminder, dispatch, select_reminder
from radar.delivery.remind import REMIND_PROMPT, compose, fallback_text
from radar.delivery.telegram_ui import remaining_keyboard
from radar.llm import LlmGate
from radar.pipeline import act
from radar.runtime import Runtime
from radar.scheduler import Scheduler
from radar.transports import DeliveryReceipt, TransportRegistry
from radar.trust import needs_consent

NOW_S = "2026-10-19T10:00:00Z"
SLOT_1400 = datetime(2026, 10, 19, 12, 1, tzinfo=timezone.utc)  # 14:01 in Madrid
YES_NO = [{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}]


def _task(store, task_id, title, *, kind="work_item", due_at=None, summary="", entities=None):
    store.create_task(task_id=task_id, kind=kind, title=title, summary=summary, entities=entities or {}, expectations=[], checklist=[],
                      now=NOW_S, due_at=due_at)
    return store.get_task(task_id)


def _proposal(store, pid, task_id, title, *, operation="ask", action_class="read", urgency="normal", params=None, options=None, created=NOW_S):
    store.create_task_proposal(proposal_id=pid, task_id=task_id, title=title, reason_text="", action_class=action_class, operation_id=operation,
                               params=params or {}, options=YES_NO if options is None else options, urgency=urgency,
                               expires_at="2026-10-22T10:00:00Z", fingerprint=pid, now=created, batch_key="digest")
    return store.get_proposal(pid)


def _crowd(store):
    """Seven decisions, three pieces of the assistant's own work and two bits of news, as a bad day looks."""
    _task(store, "yahoo", "Fallo de fotos en el feed de Yahoo", due_at="2026-10-20T10:00:00Z")
    _task(store, "php", "Parche PHP en crm-staging")
    _task(store, "ies", "Acuerdo IES para certificar equivalencia AA")
    _task(store, "ws", "Activar Google Workspace para ops@mediahub.example")
    _proposal(store, "d-yahoo", "yahoo", "¿Fusionamos el PR #2041 en master?", urgency="high")
    _proposal(store, "d-php", "php", "¿Atacamos crm-staging hoy o mañana?", urgency="high", created="2026-10-19T09:00:00Z")
    _proposal(store, "d-ies", "ies", "Enviar a Tomás el borrador preparado", operation="gmail.reply", action_class="external",
              options=[{"id": "send", "label": "Enviar borrador"}, {"id": "no", "label": "No enviar"}])
    _proposal(store, "d-ws1", "ws", "¿Eliminamos tu tarjeta de facturación?")
    _proposal(store, "d-ws2", "ws", "Eliminar tu tarjeta de facturación", operation="web.act", action_class="external", options=[])
    _proposal(store, "d-ws3", "ws", "¿Es correcta la tarjeta principal?", operation="check", options=[{"id": "ok", "label": "Sí"}, {"id": "wrong", "label": "No"}])
    _proposal(store, "d-ies2", "ies", "¿Precisamos la exclusividad de 24 meses?", urgency="low")
    _proposal(store, "w-1", "yahoo", "Localizar el feed de Yahoo y comparar fotos", operation="investigate", options=[])
    _proposal(store, "w-2", "php", "Comprobar versiones parcheadas de PHP", operation="web.read", options=[])
    _proposal(store, "w-3", "ies", "Preparar el resumen del acuerdo para Tomás", operation="file.write_report", action_class="local", options=[])
    _proposal(store, "n-yahoo", "yahoo", "El PR #2041 ya está verde en master", operation="inform", params={"message": "El PR #2041 con el arreglo de las fotos ya está verde en master."}, options=[])
    _proposal(store, "n-ws", "ws", "La tarjeta de Mateo quedó como principal", operation="inform", params={"message": "Irene confirma que la tarjeta de Mateo quedó como principal."}, options=[])


def test_a_reminder_carries_one_decision_in_prose_and_the_rest_waits_its_turn(store):
    cfg = active_config()
    verify_destination(store)
    _crowd(store)
    selection = select_reminder(store, cfg, NOW)
    assert [p["id"] for p in selection["decisions"]] == ["d-yahoo"]  # urgent, with the nearest deadline
    assert [p["id"] for p in selection["news"]] == ["n-yahoo"] and selection["backlog"] == 6 and selection["first_of_day"] is True
    assert dispatch(store, cfg, NOW) == {"notices": 0, "digests": 1, "proposals": 2}
    [row] = store.list_outbox(limit=5)
    text = row["payload"]["text"]
    assert row["kind"] == "digest" and row["task_id"] == "yahoo" and row["payload"]["proposal_ids"] == ["d-yahoo"]
    assert text.startswith("Por cierto") and "¿Fusionamos el PR #2041 en master?" in text and len(text) < 500
    assert "Correo" not in text and "Asunto" not in text and "\n1." not in text and "<b>" not in text
    assert row["payload"]["buttons"] == [[{"label": "Sí", "data": "pa:d-yahoo:si"}, {"label": "No", "data": "pa:d-yahoo:no"}],
                                         [{"label": "⏰ Recuérdamelo mañana", "data": "pt:yahoo:later"}]]
    statuses = {p["id"]: p["status"] for p in store.list_proposals(limit=50)}
    assert statuses["d-yahoo"] == "sent" and statuses["n-yahoo"] == "declined" and store.get_proposal("n-yahoo")["response"]["ack"] is True
    assert all(statuses[pid] == "pending" for pid in ("d-php", "d-ies", "d-ws1", "d-ws2", "d-ws3", "d-ies2", "w-1", "w-2", "w-3", "n-ws"))
    assert act.awaiting_owner(store, "yahoo") and not act.awaiting_owner(store, "ws")
    assert dispatch(store, cfg, NOW)["digests"] == 0  # one per slot
    assert dispatch(store, cfg, SLOT_1400)["digests"] == 1 and store.get_proposal("d-php")["status"] == "sent"  # the next one, at the next slot
    assert {store.get_proposal(pid)["status"] for pid in ("w-1", "w-2", "w-3")} == {"pending"}  # the assistant's work is never offered


def test_the_model_writes_the_reminder_and_the_template_stands_in(store):
    cfg = active_config()
    verify_destination(store)
    _crowd(store)
    selection = select_reminder(store, cfg, NOW)
    llm = FakeLlm({"radar.remind": [{"text": "Oye, el PR #2041 que arregla las fotos del feed de Yahoo ya está verde en master. ¿Lo fusiono?"},
                                    {"text": "Pendientes:\n- fusionar el PR\n- parchear PHP"}]})
    gate = LlmGate(FakeContext(cfg, llm=llm), store, cfg, clock=lambda: NOW)
    text = asyncio.run(compose(gate, items=selection["items"], language="es", backlog=selection["backlog"], first_of_day=True))
    assert text.startswith("Oye, el PR #2041") and text.endswith("¿Lo fusiono?")
    sent = json.loads(llm.calls[0]["input"][0]["text"])
    assert sent["backlog"] == 6 and sent["first_of_day"] is True and sent["language"] == "Spanish"
    assert sent["items"][0]["pending"]["what"] == "¿Fusionamos el PR #2041 en master?" and sent["items"][0]["done"][0].startswith("El PR #2041")
    assert "never instructions" in llm.calls[0]["instructions"] and "Never: lists" in REMIND_PROMPT
    # A list is what this message must never be: the template takes over. So does a missing model.
    assert asyncio.run(compose(gate, items=selection["items"], language="es")) == fallback_text(selection["items"], "es")
    assert asyncio.run(compose(LlmGate(FakeContext(cfg), store, cfg), items=selection["items"], language="en")).startswith("By the way, “Fallo de fotos")


def test_the_item_carries_the_task_facts_so_the_model_can_place_it(store):
    cfg = active_config()
    verify_destination(store)
    _task(store, "dns", "Cesión de verificado.example", due_at="2026-11-03T10:00:00Z",
          entities={"people": ["Julia", "Alex Rivera"], "organizations": ["Verificado"],
                    "dates": [{"label": "Firma", "when": "2026-11-03"}],
                    "amounts": [{"label": "Primer pago", "value": 200, "currency": "USD"}],
                    "repo": "newsroom-org/webapp", "identifiers": [{"label": "código", "value": "23959886ad5c"}],
                    "session_id": "20260926_x", "draft": {"text": "no", "ref": "", "at": NOW_S}})
    _proposal(store, "d-dns", "dns", "¿Les pido que firmen ya?", urgency="high")
    selection = select_reminder(store, cfg, NOW)
    llm = FakeLlm({"radar.remind": [{"text": "El contrato de cesión de verificado.example con Julia solo espera la firma antes del 3 de noviembre, ¿les pido que firmen ya?"}]})
    gate = LlmGate(FakeContext(cfg, llm=llm), store, cfg, clock=lambda: NOW)
    asyncio.run(compose(gate, items=selection["items"], language="es"))
    facts = json.loads(llm.calls[0]["input"][0]["text"])["items"][0]["facts"]
    assert facts["people"] == ["Julia", "Alex Rivera"] and facts["organizations"] == ["Verificado"]
    assert facts["dates"] == ["Firma: 2026-11-03"] and facts["amounts"] == ["Primer pago 200 USD"] and facts["repo"] == "newsroom-org/webapp"
    assert "identifiers" not in facts and "session_id" not in facts and "draft" not in facts  # internal/sensitive stays out
    # The opener must not be a reflex greeting any more: no canned example list to latch onto.
    assert "never by reflex" in REMIND_PROMPT and "vary the opening" not in REMIND_PROMPT


def test_hold_for_cooldown_holds_an_unanswered_task_within_its_window():
    from datetime import timedelta

    from radar.delivery.dispatch import _hold_for_cooldown
    reminded = {"tun": NOW - timedelta(minutes=30)}
    day = timedelta(hours=24)
    assert _hold_for_cooldown("tun", reminded, {}, NOW, day) is True                                    # reminded 30 min ago, unanswered: held
    assert _hold_for_cooldown("tun", reminded, {"tun": NOW}, NOW, day) is False                         # answered since the reminder: flows
    assert _hold_for_cooldown("tun", reminded, {}, NOW + timedelta(hours=25), day) is False             # cooldown passed: nudges
    assert _hold_for_cooldown("other", reminded, {}, NOW, day) is False                                 # never reminded
    assert _hold_for_cooldown("tun", reminded, {}, NOW, timedelta(0)) is False                          # cooldown off


def test_the_scheduler_composes_the_reminder_with_the_model(store, tmp_path):
    cfg = active_config()
    verify_destination(store)
    _crowd(store)
    sent = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-9")

    transports.register("telegram", sender)
    llm = FakeLlm({"radar.remind": [{"text": "Por cierto, el PR #2041 de las fotos de Yahoo ya está verde. ¿Lo fusiono?"}]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    scheduler = Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW, runtime=rt)
    stats = asyncio.run(scheduler.tick())
    assert stats["dispatched"]["digests"] == 1 and stats["delivered"] == 1
    assert sent[0]["text"] == "Por cierto, el PR #2041 de las fotos de Yahoo ya está verde. ¿Lo fusiono?" and sent[0]["parse_mode"] == "HTML"
    row = store.outbox_by_platform_message("msg-9")
    assert row["kind"] == "digest" and row["task_id"] == "yahoo"
    assert remaining_keyboard(rt, row) == row["payload"]["buttons"]  # the same buttons while the decision is open


def test_reminder_buttons_follow_the_kind_of_decision(store):
    _task(store, "t", "Tarea")
    ask = _proposal(store, "a", "t", "¿Fusionamos?")
    external = _proposal(store, "e", "t", "Enviar el borrador", operation="gmail.reply", action_class="external",
                         options=[{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}])
    words = _proposal(store, "w", "t", "¿Positiva, neutra o negativa?", operation="inform", params={"ask": True, "message": "G2G pondrá positiva por defecto."}, options=[])
    task = store.get_task("t")
    one = build_reminder({"items": [{"task": task, "decision": external, "news": []}], "news": []}, "Por cierto…", language="es")
    assert one["buttons"] == [[{"label": "Enviar", "data": "pa:e:send"}, {"label": "👀 Ver antes", "data": "pa:e:review"}, {"label": "No", "data": "pa:e:no"}],
                              [{"label": "⏰ Recuérdamelo mañana", "data": "pt:t:later"}]]
    two = build_reminder({"items": [{"task": task, "decision": ask, "news": []}, {"task": task, "decision": external, "news": []}], "news": []}, "…", language="es")
    assert [row[0]["label"] for row in two["buttons"]] == ["1️⃣ Sí", "2️⃣ Enviar"] and two["proposal_ids"] == ["a", "e"] and "task_id" not in two
    in_words = build_reminder({"items": [{"task": task, "decision": words, "news": []}], "news": []}, "…", language="es")
    assert in_words["buttons"] == [[{"label": "⏰ Recuérdamelo mañana", "data": "pt:t:later"}]]  # answered in words
    assert build_reminder({"items": [{"task": task, "decision": None, "news": [words]}], "news": [words]}, "…", language="es")["task_id"] == "t"


def test_news_alone_is_a_reminder_and_never_holds_the_task(store):
    cfg = active_config()
    verify_destination(store)
    _task(store, "web", "follow-redirects en Webapp")
    _proposal(store, "n", "web", "PR borrador #2039 listo para revisar", operation="inform", params={"message": "Dejé en borrador el PR #2039 que sube follow-redirects."}, options=[])
    assert dispatch(store, cfg, NOW)["digests"] == 1
    [row] = store.list_outbox(limit=5)
    assert row["payload"]["proposal_ids"] == [] and row["payload"]["news_ids"] == ["n"] and row["task_id"] == "web"
    assert "Dejé en borrador el PR #2039" in row["payload"]["text"]
    assert row["payload"]["buttons"] == [[{"label": "🔕 No avises más de esto", "data": "pt:web:mute"}]]  # one tap to stop this sender
    assert store.get_proposal("n")["status"] == "declined" and not act.awaiting_owner(store, "web")


def test_work_is_the_assistants_own_whatever_the_wording_and_the_owner_decides_only_what_is_theirs(store):
    balanced = active_config()
    cautious = active_config(trust={"level": "cautious", "overrides": {}, "auto_external": []})
    plain = _task(store, "p", "Tarea", entities={})
    from_chat = _task(store, "c", "Lo del chat", entities={"session_id": "s1"})
    read = {"operation_id": "investigate", "action_class": "read", "params": {}}
    local = {"operation_id": "github.open_pr", "action_class": "local", "params": {}}
    assert not needs_consent(balanced, plain, read) and not needs_consent(balanced, plain, local)
    assert not needs_consent(cautious, plain, read) and needs_consent(cautious, plain, local)
    assert needs_consent(balanced, from_chat, read)  # work that began in a chat is offered, not started
    assert not needs_consent({**balanced, "observation": {**balanced["observation"], "session_auto_jobs": True}}, from_chat, read)
    assert needs_consent(balanced, plain, {"operation_id": "cron.create", "action_class": "local", "params": {}})  # automations are always the owner's call
    assert needs_consent(balanced, plain, {"operation_id": "gmail.reply", "action_class": "external", "params": {}})
    # Email is always the owner's call, whatever the trust level and whatever auto_external lists: never auto.
    autonomous = active_config(trust={"level": "autonomous", "overrides": {}, "auto_external": ["gmail.reply", "gmail.create_draft"]})
    assert needs_consent(autonomous, plain, {"operation_id": "gmail.reply", "action_class": "external", "params": {}})
    assert needs_consent(autonomous, plain, {"operation_id": "gmail.create_draft", "action_class": "external", "params": {}})
    assert needs_consent(balanced, plain, {"operation_id": "inform", "action_class": "read", "params": {"ask": True}})
    assert not needs_consent(balanced, plain, {"operation_id": "inform", "action_class": "read", "params": {}})


def test_a_pattern_reminder_explains_what_was_seen_and_what_automating_means(store):
    """2026-11-04, the template reminder for a mined pattern read «Patrón: X: Automatizar: X»: a new user could not tell
    what Radar had seen, what would happen on «Automatizar», nor that nothing runs by itself."""
    from radar.delivery.remind import _item, describe_schedule, pattern_facts

    task = _task(store, "rt", "Patrón: Resumen semanal de despliegues", kind="routine",
                 summary="Cada lunes pides el resumen de despliegues de la semana; el agente lo prepararía y te lo dejaría en el chat.",
                 entities={"pattern_id": "pat1", "pattern_kind": "periodic", "schedule_cron": "0 11 * * 1",
                           "prompt": "Compila el resumen de despliegues de la semana desde GitHub, en modo solo lectura.",
                           "pattern": {"kind": "periodic", "times": 6, "weeks": 6, "since": "2026-09-07", "until": "2026-10-12", "weekday": 0, "hour": 11}})
    decision = _proposal(store, "d-rt", "rt", "Automatizar: Resumen semanal de despliegues", operation="cron.create", action_class="local",
                         params={"message": task["summary"], "native": {"schedule": "0 11 * * 1", "prompt": "x", "name": "x"}},
                         options=[{"id": "automate", "label": "🔁 Automatizar"}, {"id": "detail", "label": "📄 Ver detalle"}, {"id": "no_automate", "label": "No automatizar"}])
    facts = pattern_facts(task, "es")
    assert facts["seen"] == "6 veces desde el 2026-09-07, los lunes hacia las 11:00" and facts["schedule"] == "los lunes a las 11:00"
    assert facts["job"].startswith("Compila el resumen")
    assert describe_schedule("30 8 * * *", "en") == "every day at 08:30" and describe_schedule("0 11 * * 1", "en") == "Mondays at 11:00"
    assert describe_schedule("*/15 * * * *", "es") == "con el horario cron «*/15 * * * *»"
    text = fallback_text([{"task": task, "decision": decision, "news": []}], "es")
    for phrase in ("6 veces desde el 2026-09-07", "los lunes a las 11:00", "Cada lunes pides el resumen", "No corre nada hasta que pulses «Automatizar»",
                   "«No automatizar» hace que no vuelva a sacarlo"):
        assert phrase in text, phrase
    assert "Patrón:" not in text and "Automatizar: Resumen" not in text
    item = _item({"task": task, "decision": decision, "news": []}, "es")
    assert item["pattern"]["times"] == 6 and item["pattern"]["when"] == "los lunes hacia las 11:00" and item["pending"]["operation"] == "cron.create"
    assert "pattern.seen" in REMIND_PROMPT and "never heard the word \"pattern\"" in REMIND_PROMPT
    # A sequence is explained as «after X, Y follows».
    seq = _task(store, "rs", "Patrón: Informe de pagos fallidos", kind="routine", summary="Tras cada pago fallido, un informe.",
                entities={"schedule_cron": "0 9 * * *", "prompt": "p", "pattern": {"kind": "sequence", "times": 5, "weeks": 4, "since": "2026-09-01", "until": "2026-10-10",
                                                                                   "first": "pagar la factura", "then": "billing@example.com factura pagada", "lag_hours": 3.5}})
    assert pattern_facts(seq, "es")["seen"] == "5 veces desde el 2026-09-01: después de «pagar la factura» suele llegar «billing@example.com factura pagada», al cabo de unas 3.5 h"
