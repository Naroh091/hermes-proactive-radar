"""A step that depends on an announcement waits for it; a snooze never lands after the deadline; the composer knows
what time it is.

On 2026-11-07 the Iberia scenario (a ticket bought a week before the check-in mail) showed three things: the plan timed
the check-in step by guesswork, so it would have gone out before the airline's mail and, with a later flight, before the
check-in was open; the step's message offered «remind me tomorrow» for a flight leaving at 08:30 the next morning; and
the text written at the step's moment still said the check-in "opens on the 13th".
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from conftest import NOW, config, extract_answer, plan_answer

from radar.delivery import responses
from radar.delivery.remind import compose
from radar.delivery.snooze import deadline_of, later_button, snooze_until, standard_snooze
from radar.pipeline import act, extract as extract_step, reason
from radar.prompts import PLAN_PROMPT, REMIND_PROMPT


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


CHECKIN = {"title": "Hacer check-in del IB3012", "rationale": "Abre 24 h antes", "action_class": "external", "operation": "web.act",
           "when": _iso(NOW + timedelta(days=6, hours=1)), "awaits": "Iberia's check-in mail for IB3012", "urgency": "normal",
           "options": [{"id": "do", "label": "Hacer el check-in"}, {"id": "no", "label": "No hacerlo"}]}


def _plan(**overrides):
    base = plan_answer(task={**plan_answer()["task"], "kind": "trip", "title": "Vuelo IB3012 Madrid-Barcelona", "due_at": _iso(NOW + timedelta(days=7)),
                             "next_review_at": ""},
                       notify={"disposition": "digest", "urgency": "this_week", "importance": "normal", "reason": "a receipt"}, message="")
    base.update(overrides)
    return reason.validate_plan(base)


def _apply(store, plan, *, existing=None, now=NOW):
    return act.apply(store, config(), signals=[], extract=extract_step.validate_extract(extract_answer(category="trip")), plan=plan, now_dt=now,
                     preferences=[], existing_task=existing)


def _audit(store, task_id):
    return [a for a in store.list_audit(subject_id=task_id) if a["event_type"] == "pipeline"][-1]["metadata_redacted"]


def test_a_step_that_awaits_an_announcement_is_not_offered_before_it(store):
    """The check-in step is held: the task expects the airline's mail and is reviewed at the fallback moment."""
    assert "awaits:" in PLAN_PROMPT and "never before the event is possible" in PLAN_PROMPT
    result = _apply(store, _plan(proposals=[CHECKIN]))
    task = store.get_task(result.task_id)
    assert store.list_proposals(task_id=task["id"]) == [] and _audit(store, task["id"])["held"] == 1
    assert task["expectations"][-1] == {"description": "Iberia's check-in mail for IB3012", "kind": "event", "by": CHECKIN["when"], "then": "Hacer check-in del IB3012"}
    assert task["next_review_at"] == CHECKIN["when"]  # the review falls at the fallback moment
    # A review the plan already set earlier is kept; a later one is pulled back to the fallback.
    earlier = _iso(NOW + timedelta(days=2))
    kept = _apply(store, _plan(task={**_plan()["task"], "next_review_at": earlier}, proposals=[CHECKIN]), existing=task)
    assert store.get_task(kept.task_id)["next_review_at"] == earlier
    later = _apply(store, _plan(task={**_plan()["task"], "next_review_at": _iso(NOW + timedelta(days=9))}, proposals=[CHECKIN]), existing=store.get_task(task["id"]))
    assert store.get_task(later.task_id)["next_review_at"] == CHECKIN["when"]


def test_the_awaited_step_is_offered_once_its_moment_has_passed_or_the_plan_drops_awaits(store):
    first = _apply(store, _plan(proposals=[CHECKIN]))
    task = store.get_task(first.task_id)
    # The fallback moment passed without the mail: the review proposes the step and it is offered.
    at = NOW + timedelta(days=6, hours=2)
    overdue = _apply(store, _plan(proposals=[CHECKIN]), existing=task, now=at)
    offered = store.list_proposals(task_id=overdue.task_id)
    assert [p["title"] for p in offered] == ["Hacer check-in del IB3012"] and offered[0]["status"] == "pending"
    # Or the mail arrived: the plan proposes the step without awaits and it is offered right away; the expectation is
    # fulfilled and the review is no longer pinned to the fallback moment.
    second = _apply(store, _plan(proposals=[CHECKIN]))
    arrived = _apply(store, _plan(task={**_plan()["task"], "next_review_at": _iso(NOW + timedelta(days=9))}, proposals=[{**CHECKIN, "awaits": "", "when": ""}]),
                     existing=store.get_task(second.task_id), now=NOW + timedelta(days=5))
    assert [p["status"] for p in store.list_proposals(task_id=arrived.task_id)] == ["pending"]
    task = store.get_task(arrived.task_id)
    assert [e for e in task["expectations"] if e.get("then")][0]["fulfilled"] is True and task["next_review_at"] == _iso(NOW + timedelta(days=9))


def test_a_review_in_between_does_not_lose_the_fallback_moment(store):
    """On 2026-11-11 a review re-planned the task without restating the awaited step and pushed the review past the
    fallback: the step held by the earlier plan keeps the review pinned until it is offered or its moment passes."""
    first = _apply(store, _plan(proposals=[CHECKIN]))
    review = _apply(store, _plan(task={**_plan()["task"], "next_review_at": _iso(NOW + timedelta(days=9))}, proposals=[]),
                    existing=store.get_task(first.task_id), now=NOW + timedelta(days=4))
    assert store.get_task(review.task_id)["next_review_at"] == CHECKIN["when"]


def test_a_decision_that_matters_is_never_left_silent(store):
    """A silent plan with a step the owner must approve and high importance (or a close deadline) goes to the next
    reminder: on 2026-11-13 the check-in step of the next morning's flight was proposed silent, unseen."""
    quiet = {"disposition": "silent", "urgency": "whenever", "importance": "high", "reason": "x"}
    result = _apply(store, _plan(notify=quiet, proposals=[{**CHECKIN, "awaits": "", "when": ""}]))
    assert [p["batch_key"] for p in store.list_proposals(task_id=result.task_id)] == ["digest"]
    plain = _apply(store, _plan(task={**_plan()["task"], "due_at": ""}, notify={**quiet, "importance": "normal"}, proposals=[{**CHECKIN, "awaits": "", "when": ""}]))
    assert [p["batch_key"] for p in store.list_proposals(task_id=plain.task_id)] == ["silent"]


def _paced():
    cfg = config()
    cfg["notifications"]["digest_times"] = []  # paced reminders: «tomorrow» is the next waking morning (08:00 Madrid)
    return cfg


def test_a_snooze_never_lands_after_the_deadline(store):
    cfg = _paced()
    tomorrow = standard_snooze(NOW, cfg)
    assert tomorrow == NOW + timedelta(hours=20)  # 08:00 Madrid the next day
    assert snooze_until(NOW, cfg) == tomorrow and snooze_until(NOW, cfg, _iso(NOW + timedelta(days=10))) == tomorrow
    # A flight at 08:30 tomorrow (22 h away): tomorrow morning is too late; the snooze is six hours.
    assert snooze_until(NOW, cfg, _iso(NOW + timedelta(hours=22))) == NOW + timedelta(hours=6)
    # Five hours away: half of the margin, one hour. Three hours away: no margin, nothing to postpone.
    assert snooze_until(NOW, cfg, _iso(NOW + timedelta(hours=5))) == NOW + timedelta(hours=1)
    assert snooze_until(NOW, cfg, _iso(NOW + timedelta(hours=3))) is None
    assert later_button("t", "es", due_at=_iso(NOW + timedelta(days=10)), now_dt=NOW, config=cfg) == {"label": "⏰ Recuérdamelo mañana", "data": "pt:t:later"}
    assert later_button("t", "es", due_at=_iso(NOW + timedelta(hours=22)), now_dt=NOW, config=cfg)["label"] == "⏰ Recuérdamelo luego"
    assert later_button("t", "en", due_at=_iso(NOW + timedelta(hours=22)), now_dt=NOW, config=cfg)["label"] == "⏰ Remind me later"
    assert later_button("t", "es", due_at=_iso(NOW + timedelta(hours=3)), now_dt=NOW, config=cfg) is None
    assert later_button("t", "es")["label"] == "⏰ Recuérdamelo mañana"  # nothing known: the classic button


def test_tomorrow_is_eight_in_the_morning_when_there_are_no_quiet_hours():
    cfg = _paced()
    cfg["notifications"].update({"quiet_start": "00:00", "quiet_end": "00:00"})  # the waking day is the calendar day
    assert standard_snooze(NOW, cfg) == NOW + timedelta(hours=20)  # 08:00 Madrid the next day, not midnight


def test_the_deadline_that_sizes_a_snooze_is_the_next_moment_the_task_is_bound_to():
    """A trip's due date is its return; the flight tomorrow morning is among the dates, and an expected event's by."""
    far, flight, past = _iso(NOW + timedelta(days=4)), _iso(NOW + timedelta(hours=22)), _iso(NOW - timedelta(days=1))
    task = {"due_at": far, "expectations": [{"description": "check-in opened", "kind": "event", "by": past}],
            "entities": {"dates": [{"label": "flight", "when": flight}, {"label": "bought", "when": past}]}}
    assert deadline_of(task, NOW) == flight
    assert deadline_of({"due_at": far}, NOW) == far and deadline_of({"due_at": past}, NOW) == "" and deadline_of({}, NOW) == ""
    assert deadline_of({"expectations": [{"description": "mail", "kind": "email", "by": _iso(NOW + timedelta(hours=2))}]}, NOW) == _iso(NOW + timedelta(hours=2))
    assert later_button("t", "es", due_at=deadline_of(task, NOW), now_dt=NOW, config=_paced())["label"] == "⏰ Recuérdamelo luego"


def test_later_on_a_task_or_a_step_with_no_margin_keeps_it_on_the_screen(store):
    cfg = _paced()
    now = _iso(NOW)
    store.create_task(task_id="flight", kind="trip", title="Vuelo IB3012", summary="", entities={}, expectations=[], checklist=[], now=now,
                      due_at=_iso(NOW + timedelta(hours=2)))
    created = store.create_task_proposal(proposal_id="checkin", task_id="flight", title="Hacer check-in", reason_text="", action_class="external",
                                         operation_id="web.act", params={}, options=[{"id": "do", "label": "Hacerlo"}, {"id": "later", "label": "Luego"}],
                                         urgency="normal", expires_at=_iso(NOW + timedelta(days=1)), fingerprint="fp", now=now, batch_key="now")
    assert created["created"]
    whole = responses.act_on_task(store, cfg, task_id="flight", action="later", actor="owner", now_dt=NOW)
    assert whole["outcome"] == "kept" and "Vuelo IB3012" in whole["text"] and store.get_task("flight")["status"] == "open"
    one = responses.respond_to_proposal(store, cfg, proposal_id="checkin", option="later", actor="owner", now_dt=NOW)
    assert one["outcome"] == "kept" and store.get_proposal("checkin")["status"] == "pending"
    # With a day of margin the same taps snooze, for a shorter while than a night.
    store.update_task("flight", now=now, bump_version=False, due_at=_iso(NOW + timedelta(hours=22)))
    snoozed = responses.respond_to_proposal(store, cfg, proposal_id="checkin", option="later", actor="owner", now_dt=NOW)
    assert snoozed["outcome"] == "snoozed" and store.get_proposal("checkin")["snoozed_until"] == _iso(NOW + timedelta(hours=6))
    assert "hoy a las" in snoozed["text"] or "18:00" in snoozed["text"]


class _Gate:
    available = True

    def __init__(self):
        self.calls = []

    async def structured(self, slot, **kwargs):
        self.calls.append(kwargs)
        return {"text": "El check-in del IB3012 ya está abierto, ¿lo hago?"}


def test_the_composer_knows_the_time_and_the_moment_a_step_was_timed_for():
    assert "pending.when" in REMIND_PROMPT and "that moment has come" in REMIND_PROMPT
    gate = _Gate()
    task = {"id": "t", "title": "Vuelo IB3012 Madrid-Barcelona", "kind": "trip", "summary": "Check-in abre el 13.", "due_at": "2026-11-14T07:30:00Z"}
    decision = {"id": "p", "title": "Hacer check-in del IB3012", "operation_id": "web.act", "params": {"when": "2026-11-13T07:30:00Z", "deliver_after": "2026-11-13T07:30:00Z"},
                "options": [{"id": "do", "label": "Hacer el check-in"}]}
    text = asyncio.run(compose(gate, items=[{"task": task, "decision": decision, "news": []}], language="es", mode="interrupt", template=False,
                               now="2026-11-13T07:30:00Z"))
    assert text.startswith("El check-in del IB3012")
    payload = json.loads(gate.calls[0]["input_text"])
    assert payload["now"] == "2026-11-13T07:30:00Z" and payload["items"][0]["pending"]["when"] == "2026-11-13T07:30:00Z"
    # Without a moment given, the clock is used: the field is never missing.
    asyncio.run(compose(gate, items=[{"task": task, "decision": None, "news": [decision]}], language="es"))
    assert json.loads(gate.calls[1]["input_text"])["now"].endswith("Z")
