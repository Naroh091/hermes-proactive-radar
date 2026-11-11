"""A scenario's claims: refused when malformed, and judged on what the run really did."""
from __future__ import annotations

from pathlib import Path

import pytest

from radar import expectations
from radar.simulate import load_scenario

SCENARIOS = sorted((Path(__file__).resolve().parents[2] / "scenarios").glob("*.yaml"))


def _report(*, tasks=(), steps=(), messages=(), signals=(), extract=None, between=()):
    phase = {"signals": list(signals), "bundles": [{"extract": extract}] if extract else [], "would_send": list(messages), "between": {"messages": list(between)}}
    return {"phases": [phase], "final": {"tasks": list(tasks), "steps": [list(steps)]}}


TRIP = {"kind": "trip", "title": "Vuelo a Lisboa", "summary": "ida", "status": "open", "due_at": "2026-11-20"}
READ = {"title": "Buscar hotel", "operation": "web.read", "class": "read", "batch": "auto", "status": "pending"}
PAY = {"title": "Pagar", "operation": "payments.charge", "class": "external", "batch": "digest", "status": "expired"}
NOTICE = {"kind": "notice", "text": "El hotel ha cancelado tu reserva", "buttons": []}
LATER = {"kind": "reminder (queued, not due yet)", "text": "Por cierto, falta el hotel", "buttons": []}


def _failed(expect, **report):
    return [r["claim"] for r in expectations.evaluate({"expect": expect}, _report(**report))["failed"]]


def test_claims_that_hold_and_claims_that_do_not():
    assert not _failed({"task": {"kind": ["trip", "commitment"], "status": "open", "due": True, "title_any": ["lisboa"]}}, tasks=[TRIP])
    assert _failed({"task": {"kind": "purchase"}}, tasks=[TRIP])
    assert not _failed({"task": "none"}) and _failed({"task": "none"}, tasks=[TRIP])
    assert not _failed({"triage": "ignored"}, signals=[{"triage": "ignored (newsletter)"}])
    assert _failed({"triage": "kept"}, signals=[{"triage": "kept"}, {"triage": "ignored (x)"}])
    assert not _failed({"extract": {"relevant": True}}, extract={"relevant": True})


def test_a_step_must_be_open_to_count_and_must_never_have_existed_to_be_absent():
    assert not _failed({"steps": {"include": [{"class": "read", "operation": "web.*"}]}}, steps=[READ])
    assert _failed({"steps": {"include": [{"class": "external"}]}}, steps=[PAY])  # expired: not on the table
    assert not _failed({"steps": {"include": [{"class": "external", "status": "expired"}]}}, steps=[PAY])
    assert _failed({"steps": {"exclude": [{"operation": "payments.*"}]}}, steps=[PAY])
    assert not _failed({"steps": {"exclude": [{"operation": "gmail.*"}]}}, steps=[READ])


def test_messages_are_counted_across_the_run_and_read_for_their_words():
    assert not _failed({"messages": {"count": 0}})
    assert _failed({"messages": {"count": 0}}, messages=[NOTICE])
    assert not _failed({"messages": {"count": {"min": 1, "max": 2}, "kind": "notice", "any": ["CANCELADO"], "none": ["iban"]}}, messages=[NOTICE])
    assert _failed({"messages": {"no_kind": "notice"}}, messages=[NOTICE])
    assert not _failed({"messages": {"kind": "reminder", "no_kind": "notice"}}, messages=[LATER])
    assert not _failed({"messages": {"count": 1}}, between=[NOTICE])  # what arrived meanwhile also arrived


def test_a_phase_is_judged_on_its_own_moment():
    scenario = {"phases": [{"expect": {"task": "none"}}, {"expect": {"task": {"kind": "trip"}}}]}
    report = {"phases": [{"signals": [], "bundles": [], "would_send": []}, {"signals": [], "bundles": [{"task": TRIP, "proposals": [READ]}], "would_send": []}], "final": {}}
    outcome = expectations.evaluate(scenario, report)
    assert outcome["claims"] == 2 and not outcome["failed"]


@pytest.mark.parametrize("spec", [
    {}, {"tasks": "none"}, {"task": {"kinds": "trip"}}, {"triage": "maybe"}, {"steps": {"include": []}}, {"steps": {"include": [{"op": "x"}]}},
    {"steps": {"present": [{"class": "read"}]}}, {"messages": {"kind": "email"}}, {"messages": {"count": "one"}}, {"messages": {"said": ["x"]}},
])
def test_a_malformed_claim_is_refused_before_any_model_is_spent(spec):
    with pytest.raises(ValueError):
        expectations.validate(spec)


def test_a_run_says_which_claim_failed():
    outcome = expectations.evaluate({"expect": {"task": "none"}}, _report(tasks=[TRIP]))
    text = "\n".join(expectations.render(outcome))
    assert "0/1 held" in text and "FAIL" in text and "Vuelo a Lisboa" in text
    assert "none declared" in "\n".join(expectations.render(expectations.evaluate({}, _report())))


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_every_scenario_states_what_should_happen(path):
    pytest.importorskip("yaml")
    scenario = load_scenario(path)  # validates its claims
    assert "expect" in scenario or any("expect" in phase for phase in scenario.get("phases") or []), f"{path.name} claims nothing"
    assert scenario.get("now") or scenario.get("phases"), f"{path.name} depends on today's date"
