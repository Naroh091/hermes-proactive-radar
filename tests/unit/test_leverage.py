"""A question is asked only when the answer changes what Radar does; news and decisions in words only when they matter.

In the week of 2026-10-31, 49 of 126 notices were confirmations with no leverage («¿ya recibiste el paquete?», «¿qué
tarjeta llevarás?», «¿habrá alguien para recibir el bulto?»), receipts and closed issues told for nothing, and
«¿me confirmas con un 👍 Vale?» questions with no button. The owner: if the answer only closes a task with no
follow-up, it should expire on its own without telling me; news and receipts only when they really matter.
"""
from __future__ import annotations

from datetime import timedelta

from conftest import NOW, config, extract_answer, plan_answer

from radar.pipeline import act, extract as extract_step, reason
from radar.prompts import PLAN_PROMPT

T = "2026-10-19T10:00:00Z"
PARCEL = {"title": "¿Ya recibiste el paquete de arena?", "rationale": "", "action_class": "read", "operation": "ask", "when": "", "urgency": "normal",
          "options": [{"id": "si", "label": "Sí, ya llegó"}, {"id": "no", "label": "No, sin llegar"}],
          "fact": {"key": "parcel_arena", "yes": "received", "no": "missing"}, "then": "nothing"}
CENTRE = {**PARCEL, "title": "¿Brightbot tiene centro en Galicia?", "fact": {"key": "centre_galicia", "yes": "yes", "no": "no"}, "then": "step"}
STEP = {"title": "Buscar hotel en Avilés", "rationale": "", "action_class": "read", "operation": "web.read", "when": "", "urgency": "normal",
        "options": [{"id": "do", "label": "Sí, busca"}]}


def _plan(**overrides):
    base = plan_answer(task={**plan_answer()["task"], "kind": "purchase", "title": "Pedido Amazon arena FUKUMARU", "due_at": "", "next_review_at": ""},
                       notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"}, escalation={"decision": "ask", "why": "x"},
                       message="La arena debía llegar el 29 y no hay aviso de entrega.")
    base.update(overrides)
    return reason.validate_plan(base)


def _apply(store, plan, *, existing=None):
    return act.apply(store, config(), signals=[], extract=extract_step.validate_extract(extract_answer(category="purchase")), plan=plan, now_dt=NOW,
                     preferences=[], existing_task=existing)


def _audit(store, task_id):
    return [a for a in store.list_audit(subject_id=task_id) if a["event_type"] == "pipeline"][-1]["metadata_redacted"]


def test_a_question_whose_answer_changes_nothing_is_not_asked(store):
    result = _apply(store, _plan(proposals=[PARCEL]))
    task = store.get_task(result.task_id)
    assert task["status"] == "open" and store.list_proposals(task_id=task["id"]) == []  # the task waits for its evidence or expires
    assert _audit(store, task["id"])["asks_dropped"] == 1
    # The same question presses when the task is due within three days, or when the plan says it matters.
    soon = (NOW + timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    pressing = _apply(store, _plan(task={**_plan()["task"], "due_at": soon}, proposals=[PARCEL]))
    assert [p["operation_id"] for p in store.list_proposals(task_id=pressing.task_id)] == ["ask"]
    important = _apply(store, _plan(proposals=[PARCEL], notify={"disposition": "digest", "urgency": "today", "importance": "high", "reason": "x"}))
    assert [p["operation_id"] for p in store.list_proposals(task_id=important.task_id)] == ["ask"]
    # A past due date presses nothing (the parcel was due last week).
    late = (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    assert store.list_proposals(task_id=_apply(store, _plan(task={**_plan()["task"], "due_at": late}, proposals=[PARCEL])).task_id) == []
    # An answer that leads to a step is asked; and a plan that did not say (older plans) is trusted.
    leads = _apply(store, _plan(proposals=[CENTRE]))
    assert [p["title"] for p in store.list_proposals(task_id=leads.task_id)] == ["¿Brightbot tiene centro en Galicia?"]
    unsaid = _apply(store, _plan(proposals=[{k: v for k, v in PARCEL.items() if k != "then"}]))
    assert len(store.list_proposals(task_id=unsaid.task_id)) == 1
    assert reason.validate_plan(plan_answer(proposals=[PARCEL]))["proposals"][0]["then"] == "nothing"
    assert reason.validate_plan(plan_answer(proposals=[STEP]))["proposals"][0].get("then") is None
    for phrase in ('then: "step" when', '"nothing" when either answer only closes or updates the task', "such a question is never sent"):
        assert phrase in " ".join(PLAN_PROMPT.split()), phrase


def test_news_is_told_only_when_it_matters_and_stays_on_the_task_otherwise(store):
    def news(importance, **overrides):
        fields = {"proposals": [], "escalation": {"decision": "inform", "why": "x"},
                  "notify": {"disposition": "digest", "urgency": "whenever", "importance": importance, "reason": "x"},
                  "message": "Linode cobró los 36 USD de la factura 30000003 con el recibo 20000004."}
        fields.update(overrides)
        return _plan(**fields)

    routine = _apply(store, news("low"))
    assert store.list_proposals(task_id=routine.task_id) == [] and _audit(store, routine.task_id)["withheld"] == "news"
    ordinary = _apply(store, news("normal"))
    assert store.list_proposals(task_id=ordinary.task_id) == []
    matters = _apply(store, news("high", message="Falló el cargo de 8,82 USD de tu suscripción de Brave."))
    [inform] = store.list_proposals(task_id=matters.task_id)
    assert inform["operation_id"] == "inform" and not inform["params"].get("ask")
    # A security flag breaks through whatever the importance.
    flagged = act.apply(store, config(), signals=[], extract=extract_step.validate_extract(extract_answer(category="check", risk_flags=["security"])),
                        plan=news("low", message="Alguien entró desde un Oppo A79."), now_dt=NOW, preferences=[])
    assert [p["params"]["protected"] for p in store.list_proposals(task_id=flagged.task_id)] == [True]
    for phrase in ("Only news of high importance is told", "a receipt or invoice matching what was expected"):
        assert phrase in " ".join(PLAN_PROMPT.split()), phrase


def test_a_decision_in_words_is_sent_when_it_matters_or_presses_and_waits_in_the_panel_otherwise(store):
    def decision(importance, due_at=""):
        return _plan(proposals=[], task={**_plan()["task"], "status": "needs_user", "due_at": due_at},
                     notify={"disposition": "digest", "urgency": "this_week", "importance": importance, "reason": "x"},
                     message="La invitación de Hermes Android caduca el 2 de diciembre. ¿La doy por vista?")

    waits = _apply(store, decision("normal"))
    assert store.get_task(waits.task_id)["status"] == "needs_user" and store.list_proposals(task_id=waits.task_id) == []
    assert _audit(store, waits.task_id)["withheld"] == "question"
    soon = (NOW + timedelta(hours=20)).isoformat().replace("+00:00", "Z")
    presses = _apply(store, decision("normal", due_at=soon))
    [inform] = store.list_proposals(task_id=presses.task_id)
    assert inform["operation_id"] == "inform" and inform["params"]["ask"] is True
    matters = _apply(store, decision("high"))
    assert [p["params"]["ask"] for p in store.list_proposals(task_id=matters.task_id)] == [True]
    assert "reaches them only when importance is high or the task is due within three days" in " ".join(PLAN_PROMPT.split())


def test_an_approved_step_still_reports_back_whatever_the_importance(store):
    store.create_task(task_id="t", kind="work_item", title="Apuntar DNS", summary="", entities={}, expectations=[], checklist=[], now=T)
    plan = _plan(proposals=[], escalation={"decision": "inform", "why": "x"}, task_match={"decision": "advances", "task_id": "t", "why": "job"},
                 task={**_plan()["task"], "kind": "work_item", "status": "done"},
                 notify={"disposition": "digest", "urgency": "whenever", "importance": "low", "reason": "x"}, message="CNAME creado y verificado.")
    result = act.apply(store, config(), signals=[], extract=extract_step.validate_extract(extract_answer(category="work_item")), plan=plan, now_dt=NOW,
                       preferences=[], existing_task=store.get_task("t"), owner_followup=True)
    [inform] = store.list_proposals(task_id="t")
    assert result.disposition != "silent" and inform["params"]["owner_followup"] is True
