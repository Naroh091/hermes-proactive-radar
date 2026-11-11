"""One open step per family and task: a re-plan updates the step it restates, withdraws the rest, never duplicates running work."""
from __future__ import annotations

from datetime import timedelta

from conftest import NOW, config, extract_answer, plan_answer

from radar.pipeline import act, correlate, extract as extract_step, reason
from radar.pipeline.process import task_context
from radar.pipeline.reconcile import dedupe_open, family, same_family

NOW_S = "2026-10-19T10:00:00Z"


def _step(title, *, operation="", action_class="read", options=None, fact=None, when=""):
    item = {"title": title, "rationale": "", "action_class": action_class, "operation": operation, "when": when, "urgency": "normal",
            "options": options or ([{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}] if operation == "ask" else [])}
    if fact:
        item["fact"] = fact
    if operation.startswith("gmail."):
        item["basis"] = "requested_ack"  # the correspondent of these tasks waits for an answer (see _apply)
    return item


def _apply(store, cfg, plan, *, existing_task=None, at=NOW):
    return act.apply(store, cfg, signals=[], extract=extract_step.validate_extract(extract_answer(category="work_item", reply_expected="substantive", sender_kind="partner")),
                     plan=reason.validate_plan(plan), now_dt=at, preferences=[], existing_task=existing_task)


def test_families_recognise_the_same_step_under_another_wording():
    pairs = [
        ("Enviar a Tomás el borrador ya preparado", "Enviar a Tomás el borrador preparado", "external", "gmail.reply"),
        ("Publicar comentario de revisión en el PR #198", "Enviar comentario de revisión del PR #198", "external", "gmail.reply"),
        ("Comprobar publicación y despliegue v1.0.6", "Comprobar publicación v1.0.6 y despliegue", "read", "investigate"),
    ]
    for one, other, action_class, operation in pairs:
        assert same_family(family({"title": one, "action_class": action_class, "operation": operation}),
                           family({"title": other, "action_class": action_class, "operation_id": operation})), (one, other)
    assert not same_family(family({"title": "Revisar el estado actual del PR #122", "operation": "web.read"}),
                           family({"title": "Verificar despliegue de master tras PR #121", "operation": "investigate"}))
    # Two questions about the same fact are the same question; about different facts, two questions.
    same_fact = {"key": "gestion_mediahub_eu", "yes": "nosotros", "no": "externa"}
    assert same_family(family({"title": "¿La web de Mediahub la llevamos nosotros?", "operation": "ask", "fact": same_fact}),
                       family({"title": "¿Gestionamos el servidor de mediahub.example?", "operation": "ask", "params": {"fact": same_fact}}))
    assert not same_family(family({"title": "¿Rotaste ya la clave de Postgres?", "operation": "ask", "fact": {"key": "postgres_rotated"}}),
                           family({"title": "¿Verificaste el COPY de pgbackup?", "operation": "ask", "fact": {"key": "pgbackup_copy_legit"}}))
    # A step and a question never collapse into each other.
    assert not same_family(family({"title": "Enviar el borrador a Tomás", "action_class": "external"}),
                           family({"title": "¿Enviamos el borrador a Tomás?", "operation": "ask"}))


def test_a_replan_updates_the_restated_step_in_place_withdraws_the_rest_and_leaves_running_work_alone(store):
    cfg = config()
    send = _step("Enviar a Tomás el borrador ya preparado", operation="gmail.reply", action_class="external",
                 options=[{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}])
    verify = _step("Verificar cláusulas del adjunto original", operation="investigate")
    first = _apply(store, cfg, plan_answer(task={**plan_answer()["task"], "kind": "work_item", "title": "Acuerdo IES"}, proposals=[send, verify],
                                           notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}))
    rows = {p["title"]: p for p in store.list_proposals(task_id=first.task_id)}
    sent_id = rows["Enviar a Tomás el borrador ya preparado"]["id"]
    store.mark_proposals_sent([sent_id], "outbox-1", NOW_S)  # the owner has it on screen as «1»
    running = rows["Verificar cláusulas del adjunto original"]
    store.respond_proposal(running["id"], decision="accepted", response={"auto": True}, now=NOW_S)
    store.set_proposal_status(running["id"], "waiting", NOW_S)
    # Something else the plan will not restate.
    store.create_task_proposal(proposal_id="stale", task_id=first.task_id, title="Preparar aviso para Mateo", reason_text="", action_class="local",
                               operation_id="gmail.create_draft", params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z",
                               fingerprint="stale", now=NOW_S, batch_key="digest")
    # A scheduled step keeps its moment whatever a re-plan says.
    store.create_task_proposal(proposal_id="later", task_id=first.task_id, title="Facturar el vuelo", reason_text="", action_class="local",
                               operation_id="web.act", params={"when": "2026-11-02T06:00:00Z"}, options=[], urgency="normal", expires_at="2026-11-03T10:00:00Z",
                               fingerprint="later", now=NOW_S, batch_key="now")
    task = task_context(store, store.get_task(first.task_id))
    assert {s["id"] for s in task["open_steps"]} == {sent_id, running["id"], "stale", "later"}
    replan = plan_answer(task_match={"decision": "advances", "task_id": first.task_id, "why": "reply"},
                         task={**plan_answer()["task"], "kind": "work_item", "title": "Acuerdo IES"},
                         proposals=[_step("Enviar a Tomás el borrador preparado", operation="gmail.reply", action_class="external",
                                          options=[{"id": "send", "label": "Enviar ahora"}, {"id": "no", "label": "No"}]),
                                    _step("Verificar las cláusulas del adjunto", operation="investigate")],
                         notify={"disposition": "digest", "urgency": "today", "importance": "normal", "reason": "x"})
    again = _apply(store, cfg, replan, existing_task=task, at=NOW + timedelta(hours=1))
    after = {p["id"]: p for p in store.list_proposals(task_id=first.task_id)}
    updated = after[sent_id]
    assert updated["status"] == "sent" and updated["title"] == "Enviar a Tomás el borrador preparado" and updated["options"][0]["label"] == "Enviar ahora"
    assert updated["batch_key"] == "now"  # already on the owner's screen: its number still works
    assert after[running["id"]]["status"] == "waiting"  # running work is neither duplicated nor touched
    assert after["stale"]["status"] == "expired" and after["stale"]["params"]["superseded_by"] == "replan"
    assert after["later"]["status"] == "pending"  # scheduled for its moment, not withdrawn implicitly
    assert len(after) == 4 and again.proposals == []  # nothing new was inserted
    audit = [row for row in store.list_audit(limit=10) if row["decision_code"] == "advances"][-1]["metadata_redacted"]
    assert audit["updated"] == 1 and audit["superseded"] == 1


def test_one_fact_under_varying_keys_is_one_question():
    # 2026-11-03: three plans named one fact postura_preseed_sandora, postura_zetta_preseed and postura_preseed_zetta,
    # and the owner got the same question three times.
    title = "¿Mantengo la postura sobre el pre-seed de Zetta?"
    first = family({"title": title, "operation": "ask", "fact": {"key": "postura_preseed_sandora"}})
    assert same_family(first, family({"title": title, "operation": "ask", "fact": {"key": "postura_zetta_preseed"}}))
    assert same_family(first, family({"title": "¿Sigo con la misma postura sobre el pre-seed?", "operation": "ask", "fact": {"key": "postura_preseed_zetta"}}))
    assert same_family(family({"title": title, "operation": "ask", "fact": {"key": "respuesta_inversor"}}),
                       family({"title": title, "operation": "ask", "fact": {"key": "decision_ronda"}}))  # the very same words
    # Two questions about different facts that merely share half their words stay two.
    assert not same_family(family({"title": "¿Reservo el hotel en Oviedo?", "operation": "ask", "fact": {"key": "hotel_oviedo"}}),
                           family({"title": "¿Reservo el coche en Oviedo?", "operation": "ask", "fact": {"key": "coche_oviedo"}}))


def test_a_decision_the_owner_saw_comes_back_as_it_was_when_a_later_plan_asks_for_it_again(store):
    """2026-11-03: a re-plan dropped the sent reply step of the SAS task, the next plan wanted it back, and the owner
    was asked a second time under a new id."""
    cfg = config()
    send = _step("Enviar acuse de recibo al SAS", operation="gmail.reply", action_class="external",
                 options=[{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}])
    first = _apply(store, cfg, plan_answer(task={**plan_answer()["task"], "kind": "work_item", "title": "Expediente SAS"}, proposals=[send],
                                           notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"}))
    [row] = store.list_proposals(task_id=first.task_id)
    store.mark_proposals_sent([row["id"]], "outbox-1", NOW_S)
    advances = {"decision": "advances", "task_id": first.task_id, "why": "reply"}
    dropped = plan_answer(task_match=advances, task={**plan_answer()["task"], "kind": "work_item", "title": "Expediente SAS"},
                          proposals=[_step("Revisar el expediente completo", operation="investigate")],
                          notify={"disposition": "digest", "urgency": "today", "importance": "normal", "reason": "x"})
    _apply(store, cfg, dropped, existing_task=task_context(store, store.get_task(first.task_id)), at=NOW + timedelta(hours=1))
    withdrawn = store.get_proposal(row["id"])
    assert withdrawn["status"] == "expired" and withdrawn["params"]["superseded_from"] == "sent"
    wanted_again = plan_answer(task_match=advances, task={**plan_answer()["task"], "kind": "work_item", "title": "Expediente SAS"},
                               proposals=[_step("Enviar el acuse de recibo al SAS", operation="gmail.reply", action_class="external",
                                                options=[{"id": "send", "label": "Enviar ya"}, {"id": "no", "label": "No"}])],
                               notify={"disposition": "notify_now", "urgency": "now", "importance": "high", "reason": "x"})
    again = _apply(store, cfg, wanted_again, existing_task=task_context(store, store.get_task(first.task_id)), at=NOW + timedelta(hours=2))
    restored = store.get_proposal(row["id"])
    assert restored["status"] == "sent" and restored["title"] == "Enviar el acuse de recibo al SAS" and restored["options"][0]["label"] == "Enviar ya"
    assert "superseded_by" not in restored["params"] and restored["expires_at"] > "2026-10-20T00:00:00Z"
    assert again.proposals == [] and len(store.list_proposals(task_id=first.task_id)) == 2  # the same row, nothing new to send
    audit = [r for r in store.list_audit(limit=10) if r["decision_code"] == "advances"][0]["metadata_redacted"]  # newest first
    assert audit.get("restored") == 1
    # Withdrawn long ago, or never seen by the owner, a decision is asked afresh.
    store.supersede_proposals([row["id"]], now="2026-10-19T12:30:00Z", by="replan")
    much_later = _apply(store, cfg, wanted_again, existing_task=task_context(store, store.get_task(first.task_id)), at=NOW + timedelta(days=3))
    assert len(much_later.proposals) == 1 and store.get_proposal(row["id"])["status"] == "expired"


def test_the_planner_sees_the_open_steps_on_every_path(store):
    cfg = config()
    first = _apply(store, cfg, plan_answer(task={**plan_answer()["task"], "kind": "work_item", "title": "Caída de la web"},
                                           proposals=[_step("¿La web de Mediahub la llevamos nosotros?", operation="ask",
                                                            fact={"key": "gestion_mediahub", "yes": "nosotros", "no": "externa"})],
                                           notify={"disposition": "digest", "urgency": "today", "importance": "normal", "reason": "x"}))
    [ask] = store.list_proposals(task_id=first.task_id)
    assert ask["params"]["fact"] == {"key": "gestion_mediahub", "yes": "nosotros", "no": "externa"}  # the answer can record the fact
    # The thread path: candidates carry their open steps.
    candidates = correlate.candidate_tasks(store, {"category": "work_item", "title": "Caída", "summary": "", "entities": {}})
    assert candidates[0]["id"] == first.task_id and candidates[0]["open_steps"][0]["id"] == ask["id"]
    # The bound path: the planner input names them and the newest proposals come first.
    for index in range(12):
        store.create_task_proposal(proposal_id=f"old{index}", task_id=first.task_id, title=f"Paso {index}", reason_text="", action_class="read", operation_id="investigate",
                                   params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint=f"old{index}",
                                   now=f"2026-10-19T0{index % 9}:00:00Z", batch_key="auto")
    context = task_context(store, store.get_task(first.task_id))
    assert context["recent_proposals"][0]["title"] == ask["title"] and len(context["recent_proposals"]) == 8
    text = reason.build_input(now=NOW_S, owner={}, trust_level="balanced", extract=None, signals=[], candidates=candidates, facts=[], preferences=[], existing_task=context)
    assert '"open_steps"' in text and ask["id"] in text and "unanswered_by_owner" in text
    assert "open_steps" in reason.PLAN_PROMPT and "updated in place" in reason.PLAN_PROMPT


def test_dedupe_keeps_the_newest_of_each_family(store):
    store.create_task(task_id="t1", kind="work_item", title="Workspace", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    titles = [("a", "Enviar respuesta sobre ops@mediahub.example", "2026-10-22T13:05:00Z"), ("b", "Enviar borrador a Irene", "2026-10-22T13:10:00Z"),
              ("c", "Enviar respuesta a Irene sobre Workspace", "2026-10-22T13:29:00Z"), ("d", "Enviar aviso a Irene y Mateo sobre Workspace", "2026-10-22T14:41:00Z")]
    for pid, title, at in titles:
        store.create_task_proposal(proposal_id=pid, task_id="t1", title=title, reason_text="", action_class="external", operation_id="gmail.reply",
                                   params={}, options=[], urgency="normal", expires_at="2026-10-25T10:00:00Z", fingerprint=pid, now=at, batch_key="digest")
    store.create_task_proposal(proposal_id="q", task_id="t1", title="¿Eliminamos tu tarjeta de facturación?", reason_text="", action_class="read", operation_id="ask",
                               params={}, options=[{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}], urgency="normal", expires_at="2026-10-25T10:00:00Z",
                               fingerprint="q", now="2026-10-22T14:44:00Z", batch_key="digest")
    withdrawn = dedupe_open(store, "t1", "2026-10-23T12:00:00Z")
    statuses = {p["id"]: p["status"] for p in store.list_proposals(task_id="t1")}
    assert statuses["q"] == "pending" and statuses["d"] == "pending"
    assert withdrawn == 3 and statuses["c"] == "expired" and store.get_proposal("c")["params"]["superseded_by"] == "dedupe"
    assert {statuses[pid] for pid in ("a", "b")} == {"expired"}  # four replies to the same people are one decision, the newest


CHECK_TEXT = "¿Es correcto que los bloqueos Casa del 26 y Oficina del 27 impiden asistir a las 10:30 y 10:00 propuestas?"
ASK_TEXT = "¿Impiden Casa 26 y Oficina 27 asistir a las 10:30 y 10:00?"
BLOCKS_FACT = {"key": "bloqueos_impiden_26_27", "yes": "impiden", "no": "no_impiden"}


def _andres_plan(**overrides):
    fields = {"task": {**plan_answer()["task"], "kind": "work_item", "title": "Fin de cesión de Andrés"},
              "checks": [{"description": CHECK_TEXT, "severity": "warning", "ask_user": True}],
              "proposals": [_step(ASK_TEXT, operation="ask", fact=BLOCKS_FACT)],
              "notify": {"disposition": "digest", "urgency": "today", "importance": "normal", "reason": "x"}}
    return plan_answer(**{**fields, **overrides})


def test_a_check_and_an_ask_about_the_same_doubt_are_one_question(store):
    assert same_family(family({"title": CHECK_TEXT, "operation": "check"}), family({"title": ASK_TEXT, "operation": "ask", "fact": BLOCKS_FACT}))
    first = _apply(store, config(), _andres_plan())
    [question] = store.list_proposals(task_id=first.task_id)
    assert question["operation_id"] == "ask" and question["params"]["fact"] == BLOCKS_FACT  # the ask stays: its answer records the fact


def test_a_replan_never_lets_a_check_rewrite_the_open_ask_nor_keeps_a_check_beside_a_new_ask(store):
    cfg = config()
    first = _apply(store, cfg, _andres_plan(proposals=[]))
    [check] = store.list_proposals(task_id=first.task_id)
    assert check["operation_id"] == "check"
    replan = _andres_plan(task_match={"decision": "advances", "task_id": first.task_id, "why": "x"})
    _apply(store, cfg, replan, existing_task=task_context(store, store.get_task(first.task_id)), at=NOW + timedelta(hours=1))
    rows = {p["id"]: p for p in store.list_proposals(task_id=first.task_id)}
    assert rows[check["id"]]["status"] == "expired"  # the check gives way to the ask
    [ask] = [p for p in rows.values() if p["status"] == "pending"]
    assert ask["operation_id"] == "ask" and ask["params"]["fact"] == BLOCKS_FACT
    # The next re-plan restates both again: the ask stands untouched and no check comes back.
    again = _andres_plan(task_match={"decision": "advances", "task_id": first.task_id, "why": "x"}, proposals=[])
    _apply(store, cfg, again, existing_task=task_context(store, store.get_task(first.task_id)), at=NOW + timedelta(hours=2))
    open_rows = [p for p in store.list_proposals(task_id=first.task_id) if p["status"] == "pending"]
    assert [p["id"] for p in open_rows] == [ask["id"]] and open_rows[0]["params"]["fact"] == BLOCKS_FACT


def test_dedupe_keeps_the_ask_over_a_newer_check(store):
    store.create_task(task_id="t2", kind="work_item", title="Cesión", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    store.create_task_proposal(proposal_id="ask", task_id="t2", title=ASK_TEXT, reason_text="", action_class="read", operation_id="ask",
                               params={"fact": BLOCKS_FACT}, options=[], urgency="normal", expires_at="2026-10-25T10:00:00Z", fingerprint="ask",
                               now="2026-10-22T10:00:00Z", batch_key="digest")
    store.create_task_proposal(proposal_id="check", task_id="t2", title=CHECK_TEXT, reason_text="", action_class="read", operation_id="check",
                               params={}, options=[], urgency="normal", expires_at="2026-10-25T10:00:00Z", fingerprint="check",
                               now="2026-10-23T10:00:00Z", batch_key="digest")
    assert dedupe_open(store, "t2", "2026-10-23T12:00:00Z") == 1
    assert store.get_proposal("ask")["status"] == "pending" and store.get_proposal("check")["status"] == "expired"
