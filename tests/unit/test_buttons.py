"""Buttons that say what they do, one notice per open question, and chats judged whole even when deferred piecemeal."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from conftest import NOW, active_config, config, extract_answer, plan_answer, verify_destination

from radar.delivery.dispatch import dispatch
from radar.delivery.notice import build_notice
from radar.delivery.responses import act_on_task, resolve_reply
from radar.delivery.telegram_ui import handle_text_reply
from radar.pipeline import act, extract as extract_step, reason
from radar.scheduler import Scheduler
from radar.sources.session import record_turn
from radar.transports import DeliveryReceipt, TransportRegistry


def _apply(store, cfg, plan, *, extract=None, preferences=()):
    return act.apply(store, cfg, signals=[], extract=extract_step.validate_extract(extract or extract_answer()),
                     plan=reason.validate_plan(plan), now_dt=NOW, preferences=list(preferences))


def test_a_session_deferred_turn_by_turn_is_still_claimed_whole(store):
    cfg = config()
    first = {"session_id": "s1", "turn_id": "a", "platform": "telegram", "user_message": "podemos desplegar deepseek en mcp-tools?", "assistant_response": "Sí, faltan dos decisiones.", "model": "m"}
    assert record_turn(store, cfg, first, NOW)
    assert record_turn(store, cfg, {**first, "turn_id": "b", "user_message": "no limites las llamadas diarias, por favor"}, NOW + timedelta(minutes=10))
    due = {s["id"]: s["next_attempt_at"] for s in store.list_signals()}
    assert sorted(due.values()) == ["2026-10-19T10:02:00Z", "2026-10-19T10:12:00Z"]
    bundles = store.claim_signal_bundles("2026-10-19T10:05:00Z", "2026-10-19T10:15:00Z", 5)
    assert [len(b["signals"]) for b in bundles] == [2]  # the later turn travels with the due one
    assert store.claim_signal_bundles("2026-10-19T10:05:00Z", "2026-10-19T10:15:00Z", 5) == []  # all leased


def test_a_protected_risk_flag_keeps_news_out_of_silence_not_out_of_the_summary(store):
    cfg = config()
    risky = extract_answer(risk_flags=["financial_loss"], disposition="silent")
    ask = {"title": "¿Reclamo el cargo?", "rationale": "", "action_class": "read", "operation": "ask", "when": "", "urgency": "normal",
           "options": [{"id": "si", "label": "Sí, reclama"}, {"id": "no", "label": "No"}]}
    weekly = _apply(store, cfg, plan_answer(proposals=[ask] + plan_answer()["proposals"],
                                            notify={"disposition": "silent", "urgency": "this_week", "importance": "normal", "reason": "x"}), extract=risky)
    assert weekly.attention_reason == "protected_exception" and weekly.disposition == "digest"
    by_operation = {p["operation_id"]: p["batch_key"] for p in store.list_proposals(task_id=weekly.task_id)}
    assert by_operation["ask"] == "digest" and by_operation["web.read"] == "auto"  # the decision waits for the summary; the work runs by itself
    urgent = _apply(store, cfg, plan_answer(task={**plan_answer()["task"], "title": "Cargo no reconocido"},
                                            notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}), extract=risky)
    assert urgent.disposition == "notify_now"


def test_an_unanswered_notice_holds_what_comes_next_for_the_summary(store):
    cfg = active_config()
    verify_destination(store)
    send = {"title": "Enviar la respuesta a la agencia", "rationale": "", "action_class": "external", "operation": "gmail.reply", "when": "", "urgency": "normal",
            "options": [{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}], "basis": "requested_ack"}
    first = _apply(store, cfg, plan_answer(proposals=[send], notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}),
                   extract=extract_answer(reply_expected="substantive", sender_kind="partner"))  # the agency waits for an answer
    assert dispatch(store, cfg, NOW)["notices"] == 1
    assert act.awaiting_owner(store, first.task_id)
    louder = [{"id": "pref-loud", "status": "active", "dimension": "notification", "effect": "notify", "scope": {}, "origin": "explicit", "exceptions": []}]
    plan = plan_answer(task_match={"decision": "advances", "task_id": first.task_id, "why": "same trip"},
                       proposals=[send, {"title": "Reservar el hotel más barato", "rationale": "", "action_class": "external", "operation": "booking.reserve", "when": "", "urgency": "normal",
                                         "options": [{"id": "book", "label": "Reservar"}, {"id": "no", "label": "No"}]}],
                       notify={"disposition": "notify_now", "urgency": "today", "importance": "normal", "reason": "x"})
    again = _apply(store, cfg, plan, preferences=louder)
    assert again.attention_reason == "preference_notify" and again.task_id == first.task_id
    newest = next(p for p in store.list_proposals(task_id=first.task_id) if p["title"].startswith("Reservar"))
    assert newest["batch_key"] == "digest" and newest["params"]["deliver_after"] == "2026-10-19T12:00:00Z"  # 14:00 local
    assert dispatch(store, cfg, NOW)["notices"] == 0


def test_notice_separates_questions_from_steps_and_numbers_them_in_order(store):
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="purchase", title="Zapatillas talla 48", summary="Pedido confirmado.", entities={}, expectations=[], checklist=[], now=now)

    def proposal(pid, title, operation, options):
        store.create_task_proposal(proposal_id=pid, task_id="t1", title=title, reason_text="", action_class="read", operation_id=operation,
                                   params={}, options=options, urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint=pid, now=now)
        return store.get_proposal(pid)

    step = proposal("p-step", "Pedir el cambio de talla", "web.read", [])
    check = proposal("p-check", "¿Es correcta la talla 48 del pedido?", "check", [{"id": "ok", "label": "👍 Sí, es correcto"}, {"id": "wrong", "label": "⚠️ No, es un error"}])
    news = proposal("p-news", "Ha llegado la confirmación", "inform", [{"id": "ok", "label": "👍 Vale"}])
    payload = build_notice(store.get_task("t1"), [step, check, news], language="es")
    text = payload["text"]
    assert text.index("<b>Confírmame:</b>") < text.index("1. ¿Es correcta") < text.index("<b>Puedo hacer:</b>") < text.index("2. Pedir el cambio")
    assert payload["proposal_ids"] == ["p-news", "p-check", "p-step"]  # the numbers match a reply like «la 2»
    assert "Responde a este mensaje si prefieres" in text
    rows = payload["buttons"]
    assert rows[0][0]["label"] == "👍 Vale" and rows[1][0]["label"] == "1️⃣ 👍 Sí, es correcto" and rows[2][0]["label"] == "2️⃣ ✅ Adelante" and rows[2][1]["label"] == "❌ Descartar"
    assert [b["data"] for b in rows[-2]] == ["pt:t1:done", "pt:t1:later"] and rows[-1][0]["data"] == "pt:t1:mute"
    only_news = build_notice(store.get_task("t1"), [news], language="es")
    assert "Responde a este mensaje" not in only_news["text"] and "Puedo hacer" not in only_news["text"]


def test_task_buttons_say_when_they_come_back(store):
    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Decidir el alcance", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Paso", reason_text="", action_class="read", operation_id="web.read", params={},
                               options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="p1", now=now)
    later = act_on_task(store, cfg, task_id="t1", action="later", actor="telegram:7", now_dt=NOW)
    assert later["text"] == "Te lo recuerdo mañana a las 09:00: Decidir el alcance"
    assert store.get_task("t1")["snoozed_until"] == "2026-10-20T07:00:00Z" and store.get_proposal("p1")["snoozed_until"] == "2026-10-20T07:00:00Z"
    assert store.wake_snoozed_proposals("2026-10-20T07:00:00Z") == 1
    paced = active_config(notifications={"digest_times": [], "quiet_start": "22:00", "quiet_end": "08:00"})
    assert act_on_task(store, paced, task_id="t1", action="later", actor="telegram:7", now_dt=NOW)["text"] == "Te lo recuerdo mañana a las 08:00: Decidir el alcance"
    done = act_on_task(store, cfg, task_id="t1", action="done", actor="telegram:7", now_dt=NOW)
    assert done["text"] == "Marcado como resuelto: Decidir el alcance" and store.get_task("t1")["status"] == "done"


def test_a_reply_that_is_not_a_plain_order_reaches_the_agent(store):
    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Decidir el alcance", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Restringir el acceso a un grupo", reason_text="", action_class="read", operation_id="web.read",
                               params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="p1", now=now, batch_key="now")
    verify_destination(store)
    transports = TransportRegistry()
    sent = []

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-1")

    transports.register("telegram", sender)
    dispatch(store, cfg, NOW)
    assert asyncio.run(Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW).tick())["delivered"] == 1
    rt = SimpleNamespace(config=cfg, store=store, clock=lambda: NOW, transports=transports)

    def event(text):
        return SimpleNamespace(source=SimpleNamespace(platform="telegram", user_id="7", chat_id="42"), reply_to_message_id="msg-1", text=text)

    passed = asyncio.run(handle_text_reply(rt, event("mejor restríngelo al equipo de datos")))  # the agent still answers the owner …
    assert passed["action"] == "rewrite" and passed["text"].endswith("mejor restríngelo al equipo de datos")
    assert store.get_proposal("p1")["status"] == "sent"
    # … and the task hears the owner's words: a user_reply signal bound to it, judged like a button answer
    # (2026-11-03: «espera a que respondan» fell to the agent alone and the task kept asking).
    from radar.pipeline.process import _bound_task_id, _synthetic_extract

    [words] = [s for s in store.list_signals() if s["kind"] == "user_reply"]
    assert words["meta"]["task_id"] == "t1" and words["meta"]["answer"] == "mejor restríngelo al equipo de datos" and words["meta"]["free_text"] is True
    assert words["meta"]["question"].startswith("🛠️ Decidir el alcance Puedo hacer: 1. Restringir") and words["meta"]["proposal_id"] == "p1"  # the notice, as plain text
    assert _bound_task_id([words]) == "t1" and _synthetic_extract(words, store.get_task("t1"))["owner_answer"]["answer"] == "mejor restríngelo al equipo de datos"
    asked = asyncio.run(handle_text_reply(rt, event("¿has comprobado los permisos?")))  # a question for the agent, not an answer
    assert asked["action"] == "rewrite" and asked["text"].endswith("¿has comprobado los permisos?")
    assert len([s for s in store.list_signals() if s["kind"] == "user_reply"]) == 1
    assert [r["decision_code"] for r in store.list_audit(limit=5) if r["event_type"] == "owner_response"][:1] == ["words"]
    handled = asyncio.run(handle_text_reply(rt, event("no")))
    assert handled == {"action": "skip", "reason": "radar reply handled: declined"} and store.get_proposal("p1")["status"] == "declined"


def test_the_owner_can_park_mute_and_reopen_a_whole_task(store):
    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="trip", title="Viaje a Berlín", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Enviar consulta a Elena", reason_text="", action_class="external", operation_id="gmail.reply",
                               params={}, options=[], urgency="normal", expires_at="2026-11-30T10:00:00Z", fingerprint="p1", now=now, batch_key="now")
    store.mark_proposals_sent(["p1"], "o1", now)
    assert act_on_task(store, cfg, task_id="t1", action="wait", actor="agent", now_dt=NOW)["outcome"] == "invalid"  # until is required
    parked = act_on_task(store, cfg, task_id="t1", action="wait", actor="agent", now_dt=NOW, until="2026-11-25T08:00:00Z", note="espera a que respondan")
    assert parked["outcome"] == "waiting" and parked["text"] == "Aparcada hasta el 25/11 a las 09:00: Viaje a Berlín"
    task = store.get_task("t1")
    assert task["status"] == "snoozed" and task["snoozed_until"] == "2026-11-25T08:00:00Z" and task["next_review_at"] is None
    assert store.get_proposal("p1")["status"] == "snoozed" and store.get_proposal("p1")["snoozed_until"] == "2026-11-25T08:00:00Z"
    assert store.tasks_due_review("2026-11-24T08:00:00Z") == [] and [t["id"] for t in store.tasks_due_review("2026-11-25T08:00:00Z")] == ["t1"]
    assert act_on_task(store, cfg, task_id="t1", action="mute", actor="agent", now_dt=NOW)["outcome"] == "muted" and store.get_task("t1")["muted"]
    assert act_on_task(store, cfg, task_id="t1", action="unmute", actor="agent", now_dt=NOW)["text"] == "Vuelvo a avisar de «Viaje a Berlín»."
    assert not store.get_task("t1")["muted"]
    act_on_task(store, cfg, task_id="t1", action="done", actor="agent", now_dt=NOW)
    reopened = act_on_task(store, cfg, task_id="t1", action="reopen", actor="agent", now_dt=NOW)
    assert reopened["outcome"] == "reopened" and store.get_task("t1")["status"] == "open" and store.get_task("t1")["snoozed_until"] is None
    assert [r["metadata_redacted"].get("note") for r in store.list_audit(limit=10) if r["decision_code"] == "waiting"] == ["espera a que respondan"]


def test_a_written_yes_or_no_answers_a_question_as_such(store):
    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="c1", kind="purchase", title="Zapatillas", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="chk", task_id="c1", title="¿Es correcta la talla 48?", reason_text="", action_class="read", operation_id="check",
                               params={}, options=[{"id": "ok", "label": "👍 Sí, es correcto"}, {"id": "wrong", "label": "⚠️ No, es un error"}],
                               urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="chk", now=now)
    row = {"task_id": "c1", "payload": {"proposal_ids": ["chk"], "task_id": "c1"}}
    from radar.delivery.feedback import classify

    assert classify("no")["intent"] == "reject" and classify("no, descártalo")["intent"] == "reject"
    wrong = resolve_reply(store, cfg, outbox_row=row, intent=classify("no"), actor="telegram:7", now_dt=NOW, note="no")
    assert wrong["outcome"] == "accepted" and wrong["text"].startswith("Anotado como error") and store.get_task("c1")["status"] == "needs_user"


def test_the_step_an_answer_leads_to_is_put_to_the_owner_at_once(store):
    """2026-11-11: the owner tapped «cancel it» on a question, the plan answered with «cancel the subscription?» and it
    waited for the summary under a notice that had just said «I'll carry on from there and report back»."""
    from radar.delivery.responses import respond_to_proposal

    cfg = active_config()
    verify_destination(store)
    ask = {"title": "¿Mantengo el plan o lo cancelo?", "rationale": "", "action_class": "read", "operation": "ask", "when": "", "urgency": "normal",
           "options": [{"id": "mantener", "label": "Mantener"}, {"id": "cancelar", "label": "Cancelar"}]}
    first = _apply(store, cfg, plan_answer(proposals=[ask], notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}))
    assert dispatch(store, cfg, NOW)["notices"] == 1
    question = next(p for p in store.list_proposals(task_id=first.task_id) if p["operation_id"] == "ask")
    cancel = {"title": "Cancelar la suscripción", "rationale": "", "action_class": "external", "operation": "web.act", "when": "", "urgency": "normal",
              "options": [{"id": "cancelar", "label": "Cancelar suscripción"}, {"id": "no", "label": "No cancelar"}]}
    later = plan_answer(task_match={"decision": "advances", "task_id": first.task_id, "why": "same receipt"}, proposals=[cancel],
                        notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})

    def plan_from(signals):
        return act.apply(store, cfg, signals=signals, extract=extract_step.validate_extract(extract_answer()),
                         plan=reason.validate_plan(later), now_dt=NOW + timedelta(minutes=1), preferences=[])

    assert respond_to_proposal(store, cfg, proposal_id=question["id"], option="cancelar", actor="telegram:1", now_dt=NOW)["outcome"] == "answered"
    [answer] = [s for s in store.list_signals() if s["kind"] == "user_reply"]
    told = plan_from([answer])
    step = next(p for p in store.list_proposals(task_id=first.task_id) if p["operation_id"] == "web.act")
    assert told.disposition == "notify_now" and step["batch_key"] == "now"
    assert dispatch(store, cfg, NOW + timedelta(minutes=1))["notices"] == 1


def test_the_same_step_without_an_answer_still_waits_for_the_summary(store):
    cfg = active_config()
    verify_destination(store)
    cancel = {"title": "Cancelar la suscripción", "rationale": "", "action_class": "external", "operation": "web.act", "when": "", "urgency": "normal",
              "options": [{"id": "cancelar", "label": "Cancelar suscripción"}, {"id": "no", "label": "No cancelar"}]}
    held = _apply(store, cfg, plan_answer(proposals=[cancel], notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"}))
    step = next(p for p in store.list_proposals(task_id=held.task_id) if p["operation_id"] == "web.act")
    assert held.disposition == "digest" and step["batch_key"] == "digest"


def test_an_answer_is_acknowledged_in_the_planners_own_words_for_that_answer(store):
    """The fixed «Noted… I'll carry on and report back» followed every answer, in two languages only and also when the
    answer led nowhere (2026-11-11). The planner now writes, with the question, what it says back to each answer."""
    from radar.delivery.responses import respond_to_proposal

    cfg = active_config()
    verify_destination(store)
    ask = {"title": "Tu peux être là pour la nouvelle livraison ?", "rationale": "", "action_class": "read", "operation": "ask", "when": "", "urgency": "normal",
           "options": [{"id": "present", "label": "J'y serai", "ack": "Parfait, je te préviens dès que le livreur confirme l'heure."},
                       {"id": "relais", "label": "Point relais", "ack": "<b>D'accord</b>"}],
           "fact": {"key": "presence_livraison", "yes": "présent", "no": "point relais"}, "then": "step"}
    step = {"title": "Réserver", "rationale": "", "action_class": "external", "operation": "web.act", "when": "", "urgency": "normal",
            "options": [{"id": "do", "label": "Réserver", "ack": "ignored on a step"}]}
    plan = reason.validate_plan(plan_answer(proposals=[ask, step], notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}))
    asked, other = plan["proposals"][0], plan["proposals"][1]
    assert asked["options"][0]["ack"].startswith("Parfait") and "ack" not in other["options"][0]
    long = reason.validate_plan(plan_answer(proposals=[{**ask, "options": [{"id": "a", "label": "A", "ack": "mot " * 200}, {"id": "b", "label": "B", "ack": 7}]}]))
    assert len(long["proposals"][0]["options"][0]["ack"]) <= reason.ACK_MAX and "ack" not in long["proposals"][0]["options"][1]

    first = act.apply(store, cfg, signals=[], extract=extract_step.validate_extract(extract_answer()), plan=plan, now_dt=NOW, preferences=[])
    question = next(p for p in store.list_proposals(task_id=first.task_id) if p["operation_id"] == "ask")
    said = respond_to_proposal(store, cfg, proposal_id=question["id"], option="present", actor="telegram:1", now_dt=NOW)
    assert said["outcome"] == "answered" and said["text"] == "Parfait, je te préviens dès que le livreur confirme l'heure."


def test_an_answer_without_usable_words_gets_a_plain_noted_that_promises_nothing(store):
    from radar.delivery.responses import _ack

    assert _ack({"id": "a", "label": "A"}, "es", "presente") == "Anotado: presente."
    assert _ack({"id": "a", "label": "A", "ack": "<b>Vale</b>"}, "es", "presente") == "Anotado: presente."  # markup never travels
    assert _ack({"id": "a", "label": "A", "ack": "x" * 400}, "en", "present") == "Noted: present."
    assert _ack({"id": "a", "label": "A", "ack": "  De acuerdo,\n lo cancelo antes del día 11. "}, "es", "cancelar") == "De acuerdo, lo cancelo antes del día 11."


def test_a_reminder_asked_for_a_named_day_is_set_for_that_day_not_for_tomorrow(store):
    """2026-11-11: «recuérdamelo el 10 de diciembre» was read as a plain «later» and answered «te lo recuerdo mañana a
    las 08:00». Such a reply now goes to the chat agent, whose tool snoozes the task until the moment they named."""
    from radar.delivery.responses import named_moment

    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="r1", kind="purchase", title="Recibo Starter", summary="", entities={}, expectations=[], checklist=[], now=now)
    said = act_on_task(store, cfg, task_id="r1", action="later", actor="agent", now_dt=NOW, until="2026-12-10")
    task = store.get_task("r1")
    assert said["outcome"] == "snoozed" and task["status"] == "snoozed" and task["snoozed_until"] == "2026-12-10T07:00:00Z"  # 08:00 in Madrid
    assert "10/12" in said["text"] and "mañana" not in said["text"]
    # An hour is kept; what is past, unreadable or a year away falls back to the usual «tomorrow».
    assert named_moment("2026-10-23T17:30", NOW, cfg).isoformat() == "2026-10-23T17:30:00+02:00"
    assert named_moment("2026-10-01", NOW, cfg) is None and named_moment("el viernes", NOW, cfg) is None and named_moment("2028-01-01", NOW, cfg) is None
    store.create_task(task_id="r2", kind="purchase", title="Otro recibo", summary="", entities={}, expectations=[], checklist=[], now=now)
    plain = act_on_task(store, cfg, task_id="r2", action="later", actor="telegram:7", now_dt=NOW, until="2026-10-01")
    assert plain["outcome"] == "snoozed" and store.get_task("r2")["snoozed_until"].startswith("2026-10-2")


def test_a_reply_that_is_no_plain_answer_reaches_the_agent_with_the_notice_in_front_of_it(store):
    from radar.delivery.brief import CLOSE, OPEN, agent_brief, strip_brief
    from radar.delivery.responses import local_now
    from radar.delivery.telegram_ui import to_agent

    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="b1", kind="purchase", title="Recibo Starter", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="pb1", task_id="b1", title="Cancelar la suscripción", reason_text="", action_class="external", operation_id="web.act",
                               params={}, options=[{"id": "cancelar", "label": "Cancelar"}, {"id": "no", "label": "No cancelar"}],
                               urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="pb1", now=now)
    row = {"task_id": "b1", "kind": "notice", "payload": {"proposal_ids": ["pb1"], "task_id": "b1", "text": "<b>¿Procedo</b> con la cancelación?"}}
    words = "recuérdamelo el 10 de diciembre"
    brief = agent_brief(store, cfg, row, words, local_now(cfg, NOW))
    assert brief.startswith(OPEN) and brief.endswith(words) and CLOSE in brief
    assert "Task b1" in brief and "¿Procedo con la cancelación?" in brief and "<b>" not in brief
    assert "proposal pb1" in brief and "cancelar (Cancelar)" in brief and "2026-10-19T12:00 Monday (Europe/Madrid)" in brief
    assert strip_brief(brief) == words and strip_brief(words) == words and strip_brief(OPEN + " never closed") == OPEN + " never closed"
    rt = SimpleNamespace(store=store, config=cfg, clock=lambda: NOW)
    assert to_agent(rt, row, words) == {"action": "rewrite", "text": brief}
    # What is recorded as the owner's turn is their words, never the brief.
    assert record_turn(store, cfg, {"session_id": "s9", "turn_id": "t", "platform": "telegram", "user_message": brief, "assistant_response": "Hecho.", "model": "m"}, NOW)
    [turn] = [s for s in store.list_signals() if s["kind"] == "session_turn"]
    assert OPEN not in (turn["title"] + turn["snippet"]) and words in turn["snippet"]
