"""A probable mistake in a booking interrupts, with the lookups that would settle the fix running on their own.

On 2026-11-07 the rental-mileage scenario (a weekend in Gijón with a car that includes 200 km) reached the owner as a
digest question asking whether the car was for Gijón. The owner: it is important enough to interrupt, it may be a
booking mistake, and the plan should look up the upgrade or the alternatives before asking.
"""
from __future__ import annotations

from conftest import NOW, config, extract_answer, plan_answer

from radar.pipeline import act, extract as extract_step, reason
from radar.prompts import PLAN_PROMPT

MISTAKE = {"description": "¿Es correcto el límite de 200 km del Europcar 9F2K7A para ir a Gijón?", "severity": "high", "ask_user": True}
LOOKUP = {"title": "Consultar tarifa con km ilimitados para la reserva 9F2K7A", "rationale": "", "action_class": "read", "operation": "web.read", "when": "",
          "urgency": "high", "options": [{"id": "do", "label": "Consultar"}]}


def _plan(**overrides):
    base = plan_answer(task={**plan_answer()["task"], "kind": "trip", "title": "Fin de semana en Gijón 21-22 nov", "due_at": "2026-11-22T19:00:00Z", "next_review_at": ""},
                       notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "x"}, escalation={"decision": "handle", "why": "x"},
                       message="El Europcar solo incluye 200 km y a Gijón son más de 900.")
    base.update(overrides)
    return reason.validate_plan(base)


def _apply(store, plan):
    return act.apply(store, config(), signals=[], extract=extract_step.validate_extract(extract_answer(category="trip")), plan=plan, now_dt=NOW, preferences=[])


def test_a_probable_booking_mistake_interrupts_and_the_lookup_runs_on_its_own(store):
    assert "mileage allowance" in PLAN_PROMPT and "severity high is urgency now" in PLAN_PROMPT
    result = _apply(store, _plan(checks=[MISTAKE], proposals=[LOOKUP]))
    assert result.disposition == "notify_now"
    proposals = {p["operation_id"]: p for p in store.list_proposals(task_id=result.task_id)}
    check, lookup = proposals["check"], proposals["web.read"]
    assert check["batch_key"] == "now" and check["params"]["importance"] == "high" and check["title"] == MISTAKE["description"]
    assert lookup["batch_key"] == "auto"  # the assistant's own work, not a button; its result re-plans with numbers
    audit = [a for a in store.list_audit(subject_id=result.task_id) if a["event_type"] == "pipeline"][-1]["metadata_redacted"]
    assert audit["disposition"] == "notify_now" and audit["importance"] == "high"


def test_a_lesser_check_keeps_the_plan_s_own_disposition_but_is_never_silent(store):
    result = _apply(store, _plan(checks=[{**MISTAKE, "severity": "warning"}], proposals=[]))
    assert result.disposition == "digest"
    check = store.list_proposals(task_id=result.task_id)[0]
    assert check["batch_key"] == "digest" and check["params"]["importance"] == "normal"
    # A silent plan with a question the owner must answer (the hotel night short of the flight, 2026-11-07): the
    # question rides the next reminder instead of vanishing.
    quiet = _apply(store, _plan(task={**_plan()["task"], "title": "Viaje a Lisboa 23-26 nov"}, checks=[{**MISTAKE, "severity": "warning"}], proposals=[],
                                notify={"disposition": "silent", "urgency": "whenever", "importance": "normal", "reason": "x"}))
    assert [p["batch_key"] for p in store.list_proposals(task_id=quiet.task_id)] == ["digest"]


CALENDAR = {"title": "Bloquear calendario 21-22 nov en Gijón", "rationale": "", "action_class": "external", "operation": "calendar.create", "when": "",
            "urgency": "normal", "options": [{"id": "do", "label": "Bloquear calendario"}, {"id": "no", "label": "No bloquear"}]}


def test_the_interruption_for_a_mistake_carries_the_check_alone(store):
    """On 2026-11-07 the interrupt about the 200 km rental also carried «block the calendar»: two decisions in one
    urgent message. The other decisions ride the reminder."""
    result = _apply(store, _plan(checks=[MISTAKE], proposals=[LOOKUP, CALENDAR]))
    batches = {p["operation_id"]: p["batch_key"] for p in store.list_proposals(task_id=result.task_id)}
    assert batches == {"check": "now", "web.read": "auto", "calendar.create": "digest"}


def test_a_delivery_during_a_trip_is_a_check_in_both_orders():
    """Found from the dates alone, so the owner hears of it whatever the model makes of its candidates."""
    from radar.pipeline import conflicts

    trip = {"id": "lisbon", "kind": "trip", "title": "Viaje a Lisboa 20-24 nov", "due_at": "2026-11-24T12:00:00Z",
            "entities": {"dates": [{"label": "check-in", "when": "2026-11-20T14:00:00Z"}, {"label": "check-out", "when": "2026-11-24"}]}}
    parcel = extract_answer(category="purchase", title="Monitor Dell enviado – entrega 22 nov")
    parcel["entities"], parcel["expectations"] = {"dates": [{"label": "entrega", "when": "2026-11-22T10:00:00+01:00"}]}, []
    assert conflicts.away_checks(parcel, [trip], language="es") == [
        {"description": "Monitor Dell enviado – entrega 22 nov cae el 22/11, pero del 20/11 al 24/11 estás fuera (Viaje a Lisboa 20-24 nov). ¿Es correcto?",
         "severity": "high", "ask_user": True}]
    hotel = extract_answer(category="trip", title="Viaje a Lisboa 20-24 nov")
    hotel["entities"], hotel["expectations"] = trip["entities"], []
    purchase = {"id": "p", "kind": "purchase", "title": "Monitor Dell – entrega 22 nov", "entities": {"dates": [{"label": "entrega", "when": "2026-11-22"}]}, "expectations": []}
    assert conflicts.away_checks(hotel, [purchase], language="en")[0]["description"] == (
        "Monitor Dell – entrega 22 nov falls on 22/11, but you are away from 20/11 to 24/11 (Viaje a Lisboa 20-24 nov). Is that right?")
    # A parcel outside the trip raises nothing; a plan with a high check of its own is left alone; otherwise the check is added.
    outside = {**parcel, "entities": {"dates": [{"label": "entrega", "when": "2026-11-26"}]}}
    assert conflicts.away_checks(outside, [trip], language="es") == []
    # A deadline among the trip's dates (the free cancellation on the 17th) is not a day away, and a commitment's
    # deadline binds no place: neither raises a check (2026-11-07: a claim due on the 17th was flagged as «while away»).
    with_deadline = {**hotel, "entities": {"dates": [{"label": "cancelación gratuita hasta", "when": "2026-11-17T23:59:00Z"}, *trip["entities"]["dates"]]}}
    early = {**purchase, "entities": {"dates": [{"label": "entrega", "when": "2026-11-18"}]}}
    assert conflicts.travel_days(with_deadline) == ("2026-11-20", "2026-11-24") and conflicts.away_checks(with_deadline, [early], language="es") == []
    claim = {"id": "c", "kind": "commitment", "title": "Reclamación A11B22 – responder antes 17-nov", "entities": {"dates": [{"label": "plazo", "when": "2026-11-22"}]}}
    assert conflicts.away_checks(hotel, [claim], language="es") == []
    # The net is strict: a date that does not say what it is (the mail's date, the shipping day), a trip whose dates
    # are unlabelled or that spans a month, raise nothing (2026-11-07: «shipped today» against a cancelled month-long
    # «trip» was an interruption for nothing).
    shipped = {**parcel, "entities": {"dates": [{"label": "fecha del correo", "when": "2026-11-22"}, {"label": "envío", "when": "2026-11-22"}]}}
    assert conflicts.away_checks(shipped, [trip], language="es") == []
    vague = {"id": "v", "kind": "trip", "title": "Viaje Berlín – cancela asistencia", "entities": {"dates": [{"label": "fecha", "when": "2026-11-04"}, {"label": "evento", "when": "2026-12-05"}]}}
    month = {"id": "m", "kind": "trip", "title": "Mes fuera", "entities": {"dates": [{"label": "ida", "when": "2026-11-01"}, {"label": "vuelta", "when": "2026-12-05"}]}}
    assert conflicts.away_checks(parcel, [vague, month], language="es") == [] and conflicts.travel_days(month) is None
    own = {"checks": [{"description": "already", "severity": "high", "ask_user": True}]}
    assert conflicts.add_away_checks(own, parcel, [trip], language="es") is own
    assert len(conflicts.add_away_checks({"checks": []}, parcel, [trip], language="es")["checks"]) == 1


def test_tasks_sharing_days_with_the_bundle_are_candidates_with_their_span(store):
    """A hotel in Lisbon from the 20th to the 24th and a parcel due on the 22nd: each bundle alone saw nothing
    (2026-11-07). The trip is a candidate of the parcel's plan, marked with its days, and the other way round."""
    from radar.pipeline import correlate

    now = "2026-11-07T11:00:00Z"
    store.create_task(task_id="lisbon", kind="trip", title="Hotel en Lisboa 20-24 nov", summary="", now=now, due_at="2026-11-24T12:00:00Z",
                      entities={"dates": [{"label": "check-in", "when": "2026-11-20T14:00:00+00:00"}, {"label": "check-out", "when": "2026-11-24"}], "places": ["Lisboa"]},
                      expectations=[{"description": "cancelación gratuita", "kind": "date", "by": "2026-11-17T23:59:00Z"}], checklist=[])
    store.create_task(task_id="december", kind="trip", title="Navidad en Gijón", summary="", now=now, due_at="2026-12-27T12:00:00Z",
                      entities={"dates": [{"label": "ida", "when": "2026-12-23"}]}, expectations=[], checklist=[])
    store.create_task(task_id="work", kind="work_item", title="Revisar PR 2041", summary="", now=now, due_at="2026-11-22T12:00:00Z", entities={}, expectations=[], checklist=[])
    parcel = extract_answer(category="purchase", title="Monitor Dell enviado, llega el 22 nov")
    parcel["entities"] = {"dates": [{"label": "entrega", "when": "2026-11-22T10:00:00+01:00"}], "items": ["Monitor Dell"]}
    candidates = {c["id"]: c for c in correlate.candidate_tasks(store, parcel)}
    assert candidates["lisbon"]["overlap"] == "2026-11-17..2026-11-24"  # the words may bring others in; only the days earn the mark
    assert all("overlap" not in c for cid, c in candidates.items() if cid != "lisbon") and "work" not in candidates
    assert correlate.day_span({"entities": {}, "expectations": []}) is None
    # Trips first, then the tightest spans; a plan spread over months is not a «when» (2026-11-07: three instalment
    # plans and a claim crowded the Lisbon trip and the parcel out of the candidates).
    store.create_task(task_id="monitor", kind="purchase", title="Monitor Dell – entrega 22 nov", summary="", now=now, due_at="2026-11-22T13:00:00Z",
                      entities={"dates": [{"label": "entrega", "when": "2026-11-22"}]}, expectations=[], checklist=[])
    store.create_task(task_id="instalments", kind="purchase", title="Compra en 3 plazos", summary="", now=now, due_at="2027-01-05T00:00:00Z",
                      entities={"dates": [{"label": "primer plazo", "when": "2026-11-04"}]}, expectations=[], checklist=[])
    hotel = extract_answer(category="trip", title="Hotel en Lisboa 20-24 nov")
    hotel["entities"], hotel["expectations"] = {"dates": [{"label": "check-in", "when": "2026-11-20"}, {"label": "check-out", "when": "2026-11-24"}]}, []
    ordered = [task["id"] for task, _ in correlate.overlapping_tasks(store, hotel)]
    assert ordered == ["lisbon", "monitor"]
    # Word matches never crowd the dated tasks out of the candidates (the parcel due during the trip stayed unseen on
    # 2026-11-07 behind six tasks that merely shared words with the hotel mail).
    for index in range(6):
        store.create_task(task_id=f"words-{index}", kind="trip", title=f"Hotel en Lisboa, otra visita {index}", summary="Lisboa hotel", now=now,
                          due_at="2027-03-01T00:00:00Z", entities={}, expectations=[], checklist=[])
    ids = [c["id"] for c in correlate.candidate_tasks(store, hotel)]
    assert "monitor" in ids and "lisbon" in ids and len(ids) <= 6
    from radar.pipeline import conflicts

    assert conflicts.dated_days(store.get_task("monitor")) == ["2026-11-22"]  # «entrega» labels the day
    assert conflicts.dated_days({"due_at": "2026-11-22T13:00:00Z"}) == []  # a due date alone says nothing about being home
    assert conflicts.dated_days({"expectations": [{"description": "Entrega del monitor con firma", "kind": "event", "by": "2026-11-22T14:00:00+01:00"}]}) == ["2026-11-22"]
