"""A settled task asks nothing more, a decision with no step still reaches the owner, and pull requests are re-checked before the summary."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from conftest import NOW, FakeContext, FakeLlm, active_config, extract_answer, plan_answer, verify_destination

from radar.delivery.dispatch import dispatch
from radar.delivery.dispatch import build_reminder
from radar.delivery.notice import build_notice
from radar.pipeline import act, extract as extract_step, reason
from radar.pipeline.process import process_bundle
from radar.pipeline.refresh import SOURCE_ID, pr_reference, refresh_before_digest
from radar.runtime import Runtime

NOW_S = "2026-10-19T10:00:00Z"


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _apply(store, cfg, plan, *, extract=None, existing_task=None):
    return act.apply(store, cfg, signals=[], extract=extract_step.validate_extract(extract or extract_answer()),
                     plan=reason.validate_plan(plan), now_dt=NOW, preferences=[], existing_task=existing_task)


def _proposal(store, pid, task_id, title, operation, *, batch_key="digest", deliver_after=""):
    store.create_task_proposal(proposal_id=pid, task_id=task_id, title=title, reason_text="", action_class="read", operation_id=operation,
                               params={"deliver_after": deliver_after}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z",
                               fingerprint=pid, now=NOW_S, batch_key=batch_key)
    return store.get_proposal(pid)


def _task(store, task_id, title, *, kind="work_item", entities=None, status=None):
    store.create_task(task_id=task_id, kind=kind, title=title, summary="", entities=entities or {}, expectations=[], checklist=[], now=NOW_S)
    if status:
        store.set_task_status(task_id, status, now=NOW_S)
    return store.get_task(task_id)


def test_a_settled_task_withdraws_its_open_asks_and_keeps_only_news(store):
    cfg = active_config()
    verify_destination(store)
    _task(store, "t1", "Revisar el PR #88 de core-api", status="needs_user")
    _proposal(store, "p-ask", "t1", "Preparar el comentario de revisión", None)
    _proposal(store, "p-sent", "t1", "Pedir una revisión más profunda", "investigate", batch_key="now")
    store.mark_proposals_sent(["p-sent"], "out-1", NOW_S)
    store.set_task_status("t1", "done", now=NOW_S, reason="merged")
    assert {p["id"]: p["status"] for p in store.list_proposals(task_id="t1")} == {"p-ask": "expired", "p-sent": "expired"}
    # News written after the closure still goes out...
    _proposal(store, "p-news", "t1", "El PR #88 ya está mergeado", "inform", batch_key="now")
    assert dispatch(store, cfg, NOW)["notices"] == 1 and store.get_proposal("p-news")["status"] == "sent"
    # ...while a step that somehow survives on a closed task is never delivered.
    _proposal(store, "p-stale", "t1", "Comentar en GitHub", "github.comment", batch_key="now")
    assert dispatch(store, cfg, NOW)["notices"] == 0 and store.get_proposal("p-stale")["status"] == "pending"


def test_a_decision_with_no_step_still_reaches_the_owner(store):
    cfg = active_config()
    verify_destination(store)
    quiet = plan_answer(task={**plan_answer()["task"], "kind": "purchase", "title": "Decidir la valoración del pedido", "status": "needs_user", "due_at": "2026-10-22T00:00:00Z"},
                        proposals=[], checks=[], escalation={"decision": "ask", "why": "only the owner can rate"},
                        message="G2G pondrá una valoración positiva por defecto el 22 de octubre si no eliges una. ¿Positiva, neutra o negativa?",
                        notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})
    # Without the date it would wait in the panel: a decision in words goes out when it matters or presses (2026-11-06).
    waiting = _apply(store, cfg, {**quiet, "task": {**quiet["task"], "due_at": ""}}, extract=extract_answer(category="purchase"))
    assert store.list_proposals(task_id=waiting.task_id) == [] and store.get_task(waiting.task_id)["status"] == "needs_user"
    result = _apply(store, cfg, quiet, extract=extract_answer(category="purchase"))
    [item] = store.list_proposals(task_id=result.task_id)
    assert item["operation_id"] == "inform" and item["params"]["ask"] is True and item["batch_key"] == "digest"
    assert item["params"]["deliver_after"] == "2026-10-20T07:00:00Z"  # the next morning summary, 09:00 local
    payload = build_notice(store.get_task(result.task_id), [item], language="es")
    assert "¿Positiva, neutra o negativa?" in payload["text"] and "Responde a este mensaje" in payload["text"]
    assert all("Vale" not in button["label"] for row in payload["buttons"] for button in row)
    assert payload["buttons"][-1][0]["data"] == f"pt:{result.task_id}:mute"
    reminder = build_reminder({"items": [{"task": store.get_task(result.task_id), "decision": item, "news": []}], "news": []}, "Por cierto…", language="es")
    assert reminder["proposal_ids"] == [item["id"]] and reminder["task_id"] == result.task_id
    assert reminder["buttons"] == [[{"label": "⏰ Recuérdamelo mañana", "data": f"pt:{result.task_id}:later"}]]  # answered in words, or later
    # A security alert only the owner can act on interrupts right away, with no step to offer.
    urgent = plan_answer(task={**plan_answer()["task"], "kind": "check", "title": "Dos secretos detectados en hermes-radar", "status": "needs_user"},
                         proposals=[], checks=[], escalation={"decision": "ask", "why": "credentials may be live"},
                         message="GitGuardian detectó dos secretos en el commit de esta mañana. Conviene rotarlos.",
                         notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"})
    alert = _apply(store, cfg, urgent, extract=extract_answer(category="check", risk_flags=["security"]))
    [item] = store.list_proposals(task_id=alert.task_id)
    assert item["batch_key"] == "now" and item["urgency"] == "high" and item["params"]["ask"] is True
    assert dispatch(store, cfg, NOW)["notices"] == 1


def test_a_second_interrupt_about_one_task_waits_for_the_reminder(store):
    cfg = active_config()
    verify_destination(store)
    # The owner was already interrupted about this task once and let the question lapse (the eunet-prod case).
    store.create_task(task_id="eunet", kind="check", title="Vulnerabilidad en eunet-prod-2026", summary="", entities={}, expectations=[], checklist=[], now="2026-10-18T19:34:00Z")
    store.enqueue_notification(outbox_id="o-first", destination_id="configured:primary", followup_id=None, proposal_id=None, notification_key="notice:eunet",
                               payload={"text": "¿Gestionáis el servidor eunet-prod-2026?", "parse_mode": None, "buttons": [], "proposal_ids": [], "kind": "notice"},
                               now="2026-10-18T19:34:00Z", task_id="eunet", kind="notice")
    task = store.get_task("eunet")
    replan = plan_answer(task={**plan_answer()["task"], "id": "eunet", "kind": "check", "title": "Vulnerabilidad en eunet-prod-2026", "status": "needs_user"},
                         proposals=[], checks=[], escalation={"decision": "ask", "why": "only the owner knows"},
                         message="Queda pendiente confirmar si gestionas el servidor eunet-prod-2026 para los tickets de Linode.",
                         notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"})
    result = _apply(store, cfg, replan, extract=extract_answer(category="check"), existing_task=task)
    [item] = store.list_proposals(task_id=result.task_id)
    assert item["operation_id"] == "inform" and item["params"]["ask"] is True  # a decision answered in words
    assert item["batch_key"] == "digest"  # already interrupted once: the re-raised question waits for the reminder, not a 2nd now-notice
    assert dispatch(store, cfg, NOW)["notices"] == 0


def test_one_cold_interrupt_per_task_per_cooldown_whatever_the_plan_restates(store):
    """2026-11-03, 18:18 → 18:30: the re-plan after a job withdrew the two checks the owner had just been asked, so
    the task no longer «awaited» them, and a reply step the planner timed seven minutes ahead went out as a second
    interruption twelve minutes after the first."""
    cfg = active_config()
    verify_destination(store)
    task = _task(store, "gala", "Gala Avilés 13-nov y viaje Berlín", kind="commitment", status="needs_user")
    store.enqueue_notification(outbox_id="o-first", destination_id="configured:primary", followup_id=None, proposal_id=None, notification_key="notice:gala",
                               payload={"text": "¿Es correcta la fecha de ida?", "parse_mode": None, "buttons": [], "proposal_ids": [], "kind": "notice"},
                               now=NOW_S, task_id="gala", kind="notice")  # interrupted at NOW; nothing of that notice stays open
    soon = (NOW + timedelta(minutes=7)).isoformat().replace("+00:00", "Z")
    later = (NOW + timedelta(hours=16)).isoformat().replace("+00:00", "Z")
    replan = plan_answer(task_match={"decision": "advances", "task_id": "gala", "why": "job result"},
                         task={**plan_answer()["task"], "kind": "commitment", "title": "Gala Avilés 13-nov y viaje Berlín", "status": "needs_user"},
                         proposals=[
                             {"title": "Contactar hoy a EUROTECH para aclarar la noche", "rationale": "", "action_class": "external", "operation": "gmail.reply",
                              "when": soon, "urgency": "high", "options": [{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}], "basis": "requested_ack"},
                             {"title": "Comprar los vuelos Ryanair una vez confirmada la noche", "rationale": "", "action_class": "external", "operation": "web.act",
                              "when": later, "urgency": "high", "options": [{"id": "buy", "label": "Comprar"}, {"id": "no", "label": "No"}]},
                             {"title": "¿Tienes autorización para comprar los vuelos?", "rationale": "", "action_class": "read", "operation": "ask", "when": "",
                              "urgency": "high", "options": [{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}], "fact": {"key": "autorizacion_vuelos", "yes": "sí", "no": "no"}},
                         ],
                         notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"})
    _apply(store, cfg, replan, existing_task=task, extract=extract_answer(category="commitment", reply_expected="substantive", sender_kind="partner"))
    rows = {p["title"]: p for p in store.list_proposals(task_id="gala")}
    assert rows["¿Tienes autorización para comprar los vuelos?"]["batch_key"] == "digest"  # told minutes ago: the new decision waits for the reminder
    eurotech = rows["Contactar hoy a EUROTECH para aclarar la noche"]
    assert eurotech["batch_key"] == "digest" and eurotech["params"]["deliver_after"] == soon  # timed minutes after the interruption: no second one
    flights = rows["Comprar los vuelos Ryanair una vez confirmada la noche"]
    assert flights["batch_key"] == "now" and flights["params"]["deliver_after"] == later  # its moment lies past the cooldown: its own notice then
    assert dispatch(store, cfg, NOW + timedelta(minutes=10))["notices"] == 0
    assert act.interrupt_again(store, cfg, "gala", "now", NOW + timedelta(hours=4)) and not act.noticed_recently(store, cfg, "gala", NOW + timedelta(hours=4))


def test_a_merge_settles_the_pending_ask_quietly(store):
    """A merged PR closes the task and withdraws the step nobody had been asked about yet; as news it is routine
    (importance low), so it stays on the task instead of riding the reminder (in the week of 2026-10-31 three such
    lines went out for nothing)."""
    cfg = active_config()
    verify_destination(store)
    task = _task(store, "t2", "Revisar el PR #88 de core-api", entities={"repo": "org/core-api", "url": "https://github.com/org/core-api/pull/88"}, status="needs_user")
    _proposal(store, "p-comment", "t2", "Preparar el comentario de revisión", None, deliver_after="2026-10-20T07:00:00Z")
    settled = plan_answer(task_match={"decision": "completes", "task_id": "t2", "why": "merged"},
                          task={"kind": "work_item", "title": "Revisar el PR #88 de core-api", "summary": "Mergeado.", "status": "done", "due_at": "", "next_review_at": "", "confidence": 0.9},
                          proposals=[], checks=[], escalation={"decision": "inform", "why": "settled"},
                          message="El PR #88 de core-api ya está mergeado; no hace falta tu comentario.",
                          notify={"disposition": "digest", "urgency": "whenever", "importance": "low", "reason": "x"})
    result = _apply(store, cfg, settled, existing_task=task)
    assert result.decision == "completes" and store.get_task("t2")["status"] == "done"
    assert store.get_proposal("p-comment")["status"] == "expired"
    assert [p for p in store.list_proposals(task_id="t2") if p["operation_id"] == "inform"] == []
    assert [a for a in store.list_audit(subject_id="t2") if a["event_type"] == "pipeline"][-1]["metadata_redacted"]["withheld"] == "news"


class FakeRepo:
    def __init__(self, answer):
        self.answer = answer
        self.calls: list[tuple[str, int]] = []

    def pull_request(self, repo, number):
        self.calls.append((repo, number))
        return self.answer


def test_pull_requests_are_rechecked_shortly_before_their_summary(store, tmp_path):
    cfg = active_config()
    verify_destination(store)
    _task(store, "t3", "Revisar el PR #87 de core-api", entities={"repo": "org/core-api", "url": "https://github.com/org/core-api/pull/87"}, status="needs_user")
    _proposal(store, "p-draft", "t3", "Preparar comentario de revisión", "gmail.create_draft", deliver_after="2026-10-20T07:00:00Z")
    _task(store, "t4", "Confirmar la llamada de la oficina", kind="commitment", status="needs_user")
    _proposal(store, "p-call", "t4", "Decidir si asistir", None, deliver_after="2026-10-20T07:00:00Z")
    assert pr_reference(store.get_task("t3")) == ("org/core-api", 87) and pr_reference(store.get_task("t4")) is None
    assert pr_reference({"title": "Revisar el PR #12 de web", "entities": {"repo": "org/web"}}) == ("org/web", 12)
    gh = FakeRepo({"state": "closed", "merged": True, "merged_at": "2026-10-20T06:10:00Z", "title": "feat(search): research",
                   "html_url": "https://github.com/org/core-api/pull/87", "merged_by": {"login": "james"}})
    early = datetime(2026, 10, 20, 6, 30, tzinfo=timezone.utc)  # 08:30 local: the 09:00 summary is not close yet
    assert refresh_before_digest(store, cfg, early, provider=gh) == {"checked": 0, "changed": 0} and gh.calls == []
    close = datetime(2026, 10, 20, 6, 45, tzinfo=timezone.utc)  # 08:45 local
    assert refresh_before_digest(store, cfg, close, provider=gh) == {"checked": 1, "changed": 1} and gh.calls == [("org/core-api", 87)]
    [signal] = store.list_signals(kind="github")
    assert signal["source_id"] == SOURCE_ID and signal["meta"]["task_id"] == "t3" and signal["meta"]["merged"] is True
    assert signal["title"] == "PR #87 merged: feat(search): research" and signal["triage_status"] == "pending"
    # Not asked again for hours, and the same outcome never becomes a second signal.
    assert refresh_before_digest(store, cfg, close + timedelta(minutes=1), provider=gh)["checked"] == 0
    # The pipeline binds the signal to its task with no extraction call; the planner settles it and the ask is withdrawn.
    llm = FakeLlm({"radar.plan": [plan_answer(
        task_match={"decision": "completes", "task_id": "t3", "why": "merged"},
        task={"kind": "work_item", "title": "Revisar el PR #87 de core-api", "summary": "Mergeado.", "status": "done", "due_at": "", "next_review_at": "", "confidence": 0.9},
        proposals=[], checks=[], escalation={"decision": "inform", "why": "settled"}, message="El PR #87 ya está mergeado.",
        notify={"disposition": "digest", "urgency": "whenever", "importance": "low", "reason": "x"})]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: close, hermes_home=str(tmp_path))
    [bundle] = store.claim_signal_bundles(iso(close), iso(close + timedelta(minutes=5)), 5)
    outcome = asyncio.run(process_bundle(rt, bundle, close))
    assert outcome["decision"] == "completes" and store.get_task("t3")["status"] == "done"
    assert store.get_proposal("p-draft")["status"] == "expired" and store.get_proposal("p-call")["status"] == "pending"
    assert [call["purpose"] for call in llm.calls] == ["radar.plan"]
    assert store.list_proposals(task_id="t3", statuses=("pending",)) == []  # routine news (a merge) stays on the task


def _runtime(store, cfg, tmp_path, clock=lambda: NOW):
    return Runtime(FakeContext(cfg), store, cfg, worker_id="w", clock=clock, hermes_home=str(tmp_path))


def test_a_step_for_a_chosen_moment_is_its_own_notice_at_that_moment(store):
    cfg = active_config()
    verify_destination(store)
    opening = "2026-10-22T05:45:00Z"  # check-in opens 24h before a 07:45 local departure
    plan = plan_answer(task={**plan_answer()["task"], "title": "Viaje Madrid-Valencia, 23–25 de octubre"},
                       proposals=[{"title": "Facturar el vuelo IB8832", "rationale": "La facturación abre 24 h antes.", "action_class": "external",
                                   "operation": "web.act", "when": opening, "urgency": "normal", "options": [{"id": "do", "label": "Facturar"}, {"id": "no", "label": "No"}]}],
                       checks=[], notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})
    result = _apply(store, cfg, plan)
    [step] = store.list_proposals(task_id=result.task_id)
    assert step["batch_key"] == "now" and step["params"]["deliver_after"] == opening and step["params"]["when"] == opening
    assert dispatch(store, cfg, NOW) == {"notices": 0, "digests": 0, "proposals": 0}  # nothing before its moment
    assert dispatch(store, cfg, datetime(2026, 10, 20, 7, 0, tzinfo=timezone.utc))["digests"] == 0  # not summary filler either
    counts = dispatch(store, cfg, datetime(2026, 10, 22, 5, 45, tzinfo=timezone.utc))
    assert counts["notices"] == 1
    [row] = store.list_outbox(limit=5)
    assert "Facturar el vuelo IB8832" in row["payload"]["text"] and row["payload"]["buttons"][0][0]["label"] == "Facturar"


def test_summary_slots_no_longer_hold_back_automatic_steps(store, tmp_path):
    from radar.pipeline.process import _start_auto, retry_pending_jobs

    cfg = active_config(trust={"level": "autonomous", "overrides": {}, "auto_external": []})
    verify_destination(store)
    plan = plan_answer(task={**plan_answer()["task"], "kind": "work_item", "title": "Revisar el PR #87"},
                       proposals=[{"title": "Preparar informe de revisión", "rationale": "", "action_class": "local", "operation": "file.write_report",
                                   "when": "", "urgency": "normal", "options": []}],
                       checks=[], notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})
    result = _apply(store, cfg, plan, extract=extract_answer(category="work_item"))
    [draft] = store.list_proposals(task_id=result.task_id)
    assert draft["batch_key"] == "auto" and draft["params"]["deliver_after"] == "2026-10-20T07:00:00Z"  # queued work, never a button
    rt = _runtime(store, cfg, tmp_path)
    assert _start_auto(rt, draft, store.get_task(result.task_id), NOW) is True  # the slot delays the news, not the work
    assert store.get_proposal(draft["id"])["status"] in ("accepted", "waiting", "executing") and store.list_jobs(task_id=result.task_id, limit=5)
    # A step the planner set for later waits for its moment, then runs by itself when trust allows...
    later = plan_answer(task={**plan_answer()["task"], "title": "Informe mensual"},
                        proposals=[{"title": "Preparar el informe", "rationale": "", "action_class": "local", "operation": "",
                                    "when": "2026-10-19T12:00:00Z", "urgency": "normal", "options": []}],
                        checks=[], notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})
    scheduled = _apply(store, cfg, later)
    [report] = store.list_proposals(task_id=scheduled.task_id)
    assert _start_auto(rt, report, store.get_task(scheduled.task_id), NOW) is False
    assert retry_pending_jobs(rt, NOW) == 0
    assert retry_pending_jobs(rt, datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)) == 1
    assert store.get_proposal(report["id"])["status"] in ("accepted", "waiting", "executing")
    # ...while an external one still reaches the owner as a notice at its moment.
    external = plan_answer(task={**plan_answer()["task"], "title": "Tren a Gijón"},
                           proposals=[{"title": "Facturar el tren", "rationale": "", "action_class": "external", "operation": "web.act",
                                       "when": "2026-10-19T12:00:00Z", "urgency": "normal", "options": []}],
                           checks=[], notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"})
    asked = _apply(store, cfg, external)
    at_noon = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    assert retry_pending_jobs(rt, at_noon) == 0
    [checkin] = store.list_proposals(task_id=asked.task_id)
    assert checkin["status"] == "pending" and dispatch(store, cfg, at_noon)["notices"] == 1


def test_a_reworded_or_translated_checklist_item_updates_its_template_row(store):
    from radar.pipeline.checklists import canonical_item, merge_checklist, template_for

    template = [row["item"] for row in template_for("trip")]
    assert canonical_item("Transporte de ida reservado", template) == "outbound transport booked"
    assert canonical_item("Alojamiento reservado para cada noche", template) == "lodging booked for every night of the stay"
    assert canonical_item("Facturación online", template) == "online check-in (airlines open it about 24h before departure)"
    assert canonical_item("Transporte local o traslados organizados", template) == "local transport or transfers arranged"
    assert canonical_item("Documentación de viaje válida para el destino", template) == "travel documents valid for the destination"
    assert canonical_item("Seguro de viaje contratado", template) == "seguro de viaje contratado"  # genuinely new
    merged = merge_checklist(template_for("trip"), [
        {"item": "Transporte de ida reservado", "status": "done", "note": "IB8832"},
        {"item": "Facturación online", "status": "missing", "note": "abre 24 h antes"},
        {"item": "Seguro de viaje contratado", "status": "unknown", "note": ""},
    ])
    assert [row["item"].lower() for row in merged] == template + ["seguro de viaje contratado"]  # a new item keeps its own wording
    assert {row["item"]: row["status"] for row in merged}["outbound transport booked"] == "done"
    assert {row["item"]: row["status"] for row in merged}["online check-in (airlines open it about 24h before departure)"] == "missing"


def test_a_plan_that_cancels_a_question_just_asked_says_so_at_once(store):
    """2026-11-03: «¿Asistes a la reunión de las 14:00?» went out at 11:53, Brightbot cancelled at 11:56, and the owner
    heard of the cancellation at 23:52. The word that takes a fresh question back is the one exception to the cooldown."""
    cfg = active_config()
    verify_destination(store)
    task = _task(store, "meet", "Conexión móvil para chatbot hoy 14:00", kind="commitment", status="needs_user")
    store.create_task_proposal(proposal_id="attend", task_id="meet", title="¿Asistes a la reunión de las 14:00 con Lupa?", reason_text="", action_class="read",
                               operation_id="ask", params={"urgency": "now"}, options=[{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}], urgency="high",
                               expires_at="2026-10-22T10:00:00Z", fingerprint="attend", now=iso(NOW - timedelta(minutes=3)), batch_key="now")
    assert dispatch(store, cfg, NOW - timedelta(minutes=3))["notices"] == 1  # the question is in the owner's chat
    cancelled = plan_answer(task_match={"decision": "completes", "task_id": "meet", "why": "cancelled"},
                            task={**plan_answer()["task"], "kind": "commitment", "title": "Conexión móvil para chatbot hoy 14:00", "status": "done"},
                            proposals=[], checks=[], escalation={"decision": "inform", "why": "nothing to decide"},
                            message="Brightbot ha cancelado la reunión de las 14:00; olvida mi pregunta.",
                            notify={"disposition": "digest", "urgency": "today", "importance": "low", "reason": "x"})
    result = _apply(store, cfg, cancelled, existing_task=task)
    rows = {p["id"]: p for p in store.list_proposals(task_id="meet")}
    assert rows["attend"]["status"] == "expired"  # withdrawn by the closing plan
    [news] = [p for p in rows.values() if p["operation_id"] == "inform"]
    assert news["batch_key"] == "now" and news["params"]["correction"] is True and result.task_status == "done"
    assert dispatch(store, cfg, NOW)["notices"] == 1 and store.get_proposal(news["id"])["status"] == "sent"
    # Without a question in the owner's hands there is nothing to take back, and a cancellation of low importance is
    # not news worth a line: it settles the task quietly.
    other = _task(store, "meet2", "Reunión de seguimiento hoy 16:00", kind="commitment", status="open")
    _proposal(store, "p-pending", "meet2", "¿Asistes?", "ask")
    store.enqueue_notification(outbox_id="o-meet2", destination_id="configured:primary", followup_id=None, proposal_id=None, notification_key="notice:meet2",
                               payload={"text": "x", "proposal_ids": [], "kind": "notice"}, now=iso(NOW - timedelta(minutes=3)), task_id="meet2", kind="notice")
    cancelled2 = {**cancelled, "task_match": {"decision": "completes", "task_id": "meet2", "why": "cancelled"}}
    _apply(store, cfg, cancelled2, existing_task=other)
    assert [p for p in store.list_proposals(task_id="meet2") if p["operation_id"] == "inform"] == [] and store.get_task("meet2")["status"] == "done"


def test_the_owners_own_conversation_never_interrupts_them(store):
    """2026-11-04, Freshly: the checkout chat, judged after the order confirmation had arrived, interrupted the owner
    with «confirm the payment». They were in that chat: what it leaves pending rides the reminder."""
    cfg = active_config()
    verify_destination(store)
    turn = {"id": "c1", "kind": "session_turn", "occurred_at": NOW_S, "title": "Paga", "snippet": "Paga", "meta": {"session_id": "sess-1", "user_text": "Paga"}}
    plan = plan_answer(task={**plan_answer()["task"], "kind": "purchase", "title": "Pedido Freshly pendiente de pago", "status": "needs_user"},
                       proposals=[{"title": "Confirmar el pago de 20,93 € en el móvil", "rationale": "", "action_class": "external", "operation": "web.act",
                                   "when": "", "urgency": "high", "options": [{"id": "do", "label": "Confirmar el pago"}, {"id": "no", "label": "No"}]}],
                       escalation={"decision": "ask", "why": "only the owner"}, notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"})
    result = act.apply(store, cfg, signals=[turn], extract=extract_step.validate_extract(extract_answer(category="purchase", risk_flags=["financial_loss"])),
                       plan=reason.validate_plan(plan), now_dt=NOW, preferences=[])
    assert result.disposition == "digest"
    assert all(p["batch_key"] == "digest" for p in store.list_proposals(task_id=result.task_id))
    assert dispatch(store, cfg, NOW)["notices"] == 0
