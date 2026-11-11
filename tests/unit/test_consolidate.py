"""The "dreaming" pass: a daily review that merges open tasks which are really one subject."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from conftest import FakeContext, FakeLlm, active_config

from radar.llm import LlmGate
from radar.pipeline import consolidate

NOW = datetime(2026, 11, 2, 3, 0, 0, tzinfo=timezone.utc)
OPEN = ("open", "waiting", "needs_user", "in_progress", "snoozed")


def _gate(store, cfg, answers):
    return LlmGate(FakeContext(cfg, llm=FakeLlm(answers)), store, cfg)


def _reconocimiento(store, now="2026-11-02T01:00:00Z"):
    store.create_task(task_id="r1", kind="commitment", title="Reconocimiento médico bianual – decidir y enviar documento", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task(task_id="r2", kind="commitment", title="Reconocimiento médico bianual – formulario sin localizar", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task(task_id="r3", kind="commitment", title="Renuncia a reconocimiento médico bianual bloqueada sin formulario", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task(task_id="other", kind="trip", title="Viaje a Berlín en diciembre", summary="", entities={}, expectations=[], checklist=[], now=now)


def test_cluster_groups_one_subject_and_leaves_the_rest(store):
    _reconocimiento(store)
    tasks = store.list_tasks(statuses=OPEN, limit=50)
    clusters = consolidate.cluster_tasks(tasks, 0.5)
    assert len(clusters) == 1 and {t["id"] for t in clusters[0]} == {"r1", "r2", "r3"}


def test_run_merges_a_confirmed_cluster_silently(store):
    cfg = active_config()
    _reconocimiento(store)
    verdict = {"same_subject": True, "title": "Reconocimiento médico bianual", "summary": "Renuncia firmada y enviada a RRHH.", "reason": "mismo asunto"}
    gate = _gate(store, cfg, {"radar.consolidate": [verdict]})
    out = asyncio.run(consolidate.run(store, cfg, gate, NOW))
    assert out == {"clusters": 1, "merged": 2}
    open_recon = [t for t in store.list_tasks(statuses=OPEN, limit=50) if t["id"] in ("r1", "r2", "r3")]
    assert len(open_recon) == 1  # one survivor stays open
    survivor = open_recon[0]["id"]
    assert store.get_task(survivor)["title"] == "Reconocimiento médico bianual"
    folded = [t for t in ("r1", "r2", "r3") if t != survivor]
    for tid in folded:
        task = store.get_task(tid)
        assert task["status"] == "abandoned" and task["entities"]["merged_into"] == survivor
    assert store.get_task("other")["status"] == "open"  # the unrelated task is untouched
    assert any(a["decision_code"] == "consolidated" for a in store.list_audit(limit=10))


def test_run_never_merges_when_the_model_is_unsure(store):
    cfg = active_config()
    _reconocimiento(store)
    gate = _gate(store, cfg, {"radar.consolidate": [{"same_subject": False, "title": "", "summary": "", "reason": "podrían ser distintas"}]})
    out = asyncio.run(consolidate.run(store, cfg, gate, NOW))
    assert out == {"clusters": 0, "merged": 0}
    assert len([t for t in store.list_tasks(statuses=OPEN, limit=50) if t["id"].startswith("r")]) == 3
    assert any(a["decision_code"] == "noop" for a in store.list_audit(limit=10))  # the run is marked so the cadence holds


def test_run_excludes_a_member_the_model_rejects(store):
    cfg = active_config()
    _reconocimiento(store)
    verdict = {"same_subject": True, "title": "Reconocimiento médico bianual", "summary": "x", "reason": "r1 y r2 sí; r3 es otra cosa", "exclude_ids": ["r3"]}
    gate = _gate(store, cfg, {"radar.consolidate": [verdict]})
    out = asyncio.run(consolidate.run(store, cfg, gate, NOW))
    assert out == {"clusters": 1, "merged": 1}
    assert store.get_task("r3")["status"] == "open"  # excluded, kept separate


def test_cadence_holds_for_a_day(store):
    cfg = active_config()
    _reconocimiento(store)
    gate = _gate(store, cfg, {"radar.consolidate": [{"same_subject": False, "title": "", "summary": "", "reason": ""}]})
    asyncio.run(consolidate.run(store, cfg, gate, NOW))
    # A second pass minutes later does nothing (no LLM call): the daily audit paces it.
    gate2 = _gate(store, cfg, {"radar.consolidate": []})  # would raise if called
    out = asyncio.run(consolidate.run(store, cfg, gate2, NOW + timedelta(hours=2)))
    assert out == {}
    out = asyncio.run(consolidate.run(store, cfg, _gate(store, cfg, {"radar.consolidate": [{"same_subject": False, "title": "", "summary": "", "reason": ""}]}), NOW + timedelta(hours=25)))
    assert out == {"clusters": 0, "merged": 0}  # a day later it runs again


def test_choose_survivor_keeps_decisions_and_never_folds_inflight_work():
    class FakeStore:
        def __init__(self, props):
            self.props = props
        def list_proposals(self, *, task_id, statuses=None, limit=50):
            return self.props.get(task_id, [])
    members = [
        {"id": "a", "status": "open", "updated_at": "2026-11-02T00:00:00Z"},
        {"id": "b", "status": "needs_user", "updated_at": "2026-11-02T00:00:00Z"},
        {"id": "c", "status": "in_progress", "updated_at": "2026-11-02T00:00:00Z"},
    ]
    store = FakeStore({
        "b": [{"status": "pending", "operation_id": "ask"}],  # a live decision
        "c": [{"status": "executing", "operation_id": "investigate"}],  # work in flight
    })
    survivor, dups = consolidate.choose_survivor(store, members)
    assert survivor["id"] == "c"  # work in flight wins the survivor slot
    assert [d["id"] for d in dups] == ["b", "a"]  # b (decision) and a fold in; none of them is in flight
    # If 'c' is not the survivor's cluster, an in-flight non-survivor is never folded:
    survivor2, dups2 = consolidate.choose_survivor(FakeStore({"a": [{"status": "executing", "operation_id": "x"}], "b": [{"status": "pending", "operation_id": "ask"}]}), members)
    assert survivor2["id"] == "a" and "a" not in [d["id"] for d in dups2]


def test_the_same_event_filed_as_a_commitment_and_as_a_trip_clusters_and_every_verdict_leaves_a_trace(store):
    """Avilés and Berlín, 2026-11-03: six twin tasks over two subjects; the nightly pass saw two clusters and merged
    nothing, and its audit («noop, clusters_seen: 2») could not say which or why."""
    cfg = active_config()
    now = "2026-11-02T01:00:00Z"
    store.create_task(task_id="trip", kind="trip", title="Viaje a Berlín 3-4 dic por finalista European Young Engineer", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task(task_id="award", kind="commitment", title="Finalista premio European Young Engineer – confirmar Berlín 3 dic", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task(task_id="gala", kind="commitment", title="Premio Avanza Tech 2026 - gala 13 nov en Avilés", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task(task_id="hotel", kind="trip", title="Hotel en Avilés la noche de la gala", summary="", entities={}, expectations=[], checklist=[], now=now)
    tasks = store.list_tasks(statuses=OPEN, limit=50)
    clusters = consolidate.cluster_tasks(tasks, 0.6)
    assert [sorted(t["id"] for t in c) for c in clusters] == [["award", "trip"]]  # five shared stems across kinds; the gala shares two with the hotel: apart
    rejected = {"same_subject": False, "title": "", "summary": "", "reason": "la gala y el viaje son cosas distintas"}
    out = asyncio.run(consolidate.run(store, cfg, _gate(store, cfg, {"radar.consolidate": [rejected]}), NOW))
    assert out == {"clusters": 0, "merged": 0}
    codes = {a["decision_code"]: a for a in store.list_audit(limit=10) if a["event_type"] == "consolidation"}
    assert set(codes) == {"cluster_rejected", "noop"}
    assert {m["id"] for m in codes["cluster_rejected"]["metadata_redacted"]["members"]} == {"award", "trip"} and "distintas" in codes["cluster_rejected"]["metadata_redacted"]["reason"]
    assert codes["noop"]["metadata_redacted"]["clusters"] == [[t["id"] for t in clusters[0]]]
    from radar.prompts import CONSOLIDATE_PROMPT
    assert "Tasks of different kinds can still be one subject" in CONSOLIDATE_PROMPT
