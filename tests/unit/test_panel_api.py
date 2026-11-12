from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import NOW, active_config, verify_destination  # noqa: E402


def _client(monkeypatch, store, cfg, tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("radar_panel_api", Path(__file__).resolve().parents[2] / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_settings", lambda: cfg)
    monkeypatch.setattr(module, "_open", lambda: (store, cfg))
    app = fastapi.FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-proactive-radar")
    return TestClient(app), module


def test_board_detail_respond_and_preferences_through_the_panel_api(store, tmp_path, monkeypatch):
    cfg = active_config()
    verify_destination(store)
    now = "2026-10-19T10:00:00Z"
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=now)
    store.create_task(task_id="t1", kind="work_item", title="Bug de Hans", summary="Faltan capturas", entities={"draft": {"text": "Hi Hans, screenshots?", "ref": "Draft ID: r1"}, "repo": "newsroom-org/webapp"},
                      expectations=[], checklist=[{"item": "reproduce", "status": "missing", "note": ""}], now=now)
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at=now, external_ref="m1", group_key="g", title="Re: upload bug", snippet="It fails",
                     meta={"sender": "Hans <hans@example.com>", "account": "me@example.org"}, fingerprint="f1", now=now)
    store.link_task_signal("t1", "s1", "origin", now)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Enviar la respuesta", reason_text="Lista", action_class="external", operation_id="gmail.reply",
                               params={"draft": "Hi Hans, screenshots?", "draft_ref": "Draft ID: r1"}, options=[{"id": "send", "label": "Enviar"}, {"id": "review", "label": "Revisar"}, {"id": "no", "label": "No"}],
                               urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fp1", now=now)
    store.upsert_pattern(pattern_id="pat1", kind="periodic", fingerprint="fx", evidence={"key": ["session", "owner", "resumen"], "schedule_cron": "0 9 * * 1"}, support=4, confidence=0.7, now=now)
    client, module = _client(monkeypatch, store, cfg, tmp_path)
    board = client.get("/api/plugins/hermes-proactive-radar/board").json()
    assert board["stats"]["tasks_open"] == 1 and board["stats"]["proposals_pending"] == 1 and board["language"] == "es"
    task = board["tasks"][0]
    assert task["id"] == "t1" and task["context"]["sender"] == "Hans" and task["draft"].startswith("Hi Hans") and task["entities"]["repo"] == "newsroom-org/webapp"
    assert task["proposals"][0]["options"][0]["id"] == "send" and task["proposals"][0]["draft_ref"] == "Draft ID: r1"
    assert board["patterns"][0]["id"] == "pat1" and "resumen" in board["patterns"][0]["summary"]
    assert task["card_kind"] == "work_item" and task["artifact"]["layout"] == "ticket" and task["artifact"]["repo"] == "newsroom-org/webapp" and task["artifact"]["draft"] is True
    assert board["card_kinds"]["trip"]["layout"] == "boarding-pass" and board["card_kinds"]["work_item"]["hue"] == 268 and board["timezone"] == "Europe/Madrid"
    detail = client.get("/api/plugins/hermes-proactive-radar/tasks/t1").json()
    assert detail["signals"][0]["meta"]["sender"].startswith("Hans") and detail["history"][0]["id"] == "p1" and detail["checklist"][0]["item"] == "reproduce"
    report = client.get("/api/plugins/hermes-proactive-radar/tasks/t1/report").json()
    assert report["data_url"].startswith("data:text/html;base64,") and "Bug de Hans" in report["html"]
    assert client.get("/api/plugins/hermes-proactive-radar/tasks/nope").status_code == 404
    # Review returns the full draft without changing the proposal; decline closes it.
    review = client.post("/api/plugins/hermes-proactive-radar/proposals/p1/respond", json={"option": "review"}).json()
    assert review["outcome"] == "review" and "Hi Hans" in review["detail"] and store.get_proposal("p1")["status"] == "pending"
    declined = client.post("/api/plugins/hermes-proactive-radar/proposals/p1/respond", json={"option": "no"}).json()
    assert declined["outcome"] == "declined" and store.get_proposal("p1")["status"] == "declined"
    audit = store.list_audit(subject_id="p1")[0]
    assert (audit.get("metadata") or audit.get("metadata_redacted") or {}).get("channel") == "desktop"
    # Task actions, preferences and pattern dismissal.
    assert client.post("/api/plugins/hermes-proactive-radar/tasks/t1/action", json={"action": "later"}).json()["outcome"] in ("snoozed", "later", "ok") or store.get_task("t1")["status"] == "snoozed"
    assert client.post("/api/plugins/hermes-proactive-radar/tasks/t1/action", json={"action": "bogus"}).status_code == 400
    # Muting removes the task from the board and lists it apart; unmuting brings it back.
    assert client.post("/api/plugins/hermes-proactive-radar/tasks/t1/action", json={"action": "mute"}).json()["outcome"] == "muted"
    after = client.get("/api/plugins/hermes-proactive-radar/board").json()
    assert after["tasks"] == [] and [m["id"] for m in after["muted_tasks"]] == ["t1"] and after["stats"]["muted"] == 1
    assert client.post("/api/plugins/hermes-proactive-radar/tasks/t1/action", json={"action": "unmute"}).json()["outcome"] == "unmuted"
    assert client.get("/api/plugins/hermes-proactive-radar/board").json()["muted_tasks"] == []
    saved = client.post("/api/plugins/hermes-proactive-radar/preferences", json={"instruction": "No avisar de CI", "effect": "digest", "scope": {"sender": "ci@github.example"}}).json()
    assert saved["saved"]["effect"] == "digest" and store.list_preferences(active_only=True)[0]["scope"] == {"sender": "ci@github.example"}
    assert client.post(f"/api/plugins/hermes-proactive-radar/preferences/{saved['saved']['id']}/forget").json()["disabled"] == saved["saved"]["id"]
    assert client.get("/api/plugins/hermes-proactive-radar/preferences").json()["preferences"] == []
    assert client.post("/api/plugins/hermes-proactive-radar/patterns/pat1/dismiss").json()["dismissed"] == "pat1" and store.get_pattern("pat1")["status"] == "dismissed"
    assert client.get("/api/plugins/hermes-proactive-radar/status").json()["trust"]["level"] == "balanced"
    # The change marker moves when something is written (what a panel compares between two polls).
    before = module._change_marker(store)
    store.create_task(task_id="t2", kind="trip", title="Viaje", summary="", entities={}, expectations=[], checklist=[], now="2026-10-19T11:00:00Z")
    assert module._change_marker(store) != before


def test_inferred_suggestions_surface_in_the_board_and_confirm_through_the_panel(store, tmp_path, monkeypatch):
    from radar.delivery import infer
    from radar.preferences import new_preference

    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    for i, stem in enumerate(("factura marzo", "factura abril", "factura mayo")):
        store.save_preference(new_preference(f"mute:{i}", instruction=stem, scope={"sender": "billing@linode.example", "subject_stem": stem, "kind": "email"},
                                             effect="silent", origin="explicit"), expected_version=0, now=now)
    candidate = infer.after_silence(store, cfg, sender="billing@linode.example", now_dt=NOW)
    assert candidate is not None
    client, _ = _client(monkeypatch, store, cfg, tmp_path)
    board = client.get("/api/plugins/hermes-proactive-radar/board").json()
    assert board["stats"]["suggestions"] == 1
    assert [s["id"] for s in board["suggestions"]] == [candidate["id"]] and board["suggestions"][0]["status"] == "candidate"
    assert [p["id"] for p in board["preferences"]] == [p["id"] for p in store.list_preferences(active_only=True)]  # candidate is not in the active list
    confirmed = client.post(f"/api/plugins/hermes-proactive-radar/preferences/{candidate['id']}/confirm").json()
    assert confirmed["confirmed"] == candidate["id"]
    after = client.get("/api/plugins/hermes-proactive-radar/board").json()
    assert after["stats"]["suggestions"] == 0 and candidate["id"] in [p["id"] for p in after["preferences"]]
    assert client.post("/api/plugins/hermes-proactive-radar/preferences/pref-nope/confirm").status_code == 404


def test_batch_action_marks_several_tasks_at_once(store, tmp_path, monkeypatch):
    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    for tid in ("t1", "t2", "t3"):
        store.create_task(task_id=tid, kind="work_item", title=f"Tarea {tid}", summary="", entities={}, expectations=[], checklist=[], now=now)
    client, _ = _client(monkeypatch, store, cfg, tmp_path)
    assert client.post("/api/plugins/hermes-proactive-radar/tasks/batch", json={"ids": ["t1", "t2"], "action": "bogus"}).status_code == 400
    res = client.post("/api/plugins/hermes-proactive-radar/tasks/batch", json={"ids": ["t1", "t2"], "action": "done"}).json()
    assert res["applied"] == 2 and res["action"] == "done" and res["requested"] == 2
    assert store.get_task("t1")["status"] == "done" and store.get_task("t2")["status"] == "done"
    assert store.get_task("t3")["status"] not in ("done",)  # untouched
    muted = client.post("/api/plugins/hermes-proactive-radar/tasks/batch", json={"ids": ["t3"], "action": "mute"}).json()
    assert muted["applied"] == 1 and bool(store.get_task("t3")["muted"])


def test_say_records_the_owner_reply_as_a_signal(store, tmp_path, monkeypatch):
    cfg = active_config()
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Arreglar deploy", summary="", entities={}, expectations=[], checklist=[], now=now)
    client, _ = _client(monkeypatch, store, cfg, tmp_path)
    assert client.post("/api/plugins/hermes-proactive-radar/tasks/t1/say", json={"text": "   "}).status_code == 400
    assert client.post("/api/plugins/hermes-proactive-radar/tasks/nope/say", json={"text": "hola"}).status_code == 404
    before = store.pending_signal_count()
    res = client.post("/api/plugins/hermes-proactive-radar/tasks/t1/say", json={"text": "Mira en Cloudflare, el DNS cambió ayer"}).json()
    assert res["outcome"] == "said" and "Cloudflare" in res["text"]
    assert store.pending_signal_count() == before + 1  # the reply is queued for the planner
