"""Radar reaches the host only through its public surfaces: tools dispatched through the plugin context, and
credentials read through the host's secret scope. Nothing here opens a host file."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from radar.pipeline.recall import host_session_tool
from radar.providers.cron_host import HostCronProvider, deliver_target
from radar.secrets import read_secret


def test_a_cron_job_is_created_and_read_back_through_the_hosts_own_tool():
    calls = []

    def dispatch(name, args):
        calls.append((name, args))
        if args["action"] == "create":
            return json.dumps({"success": True, "job_id": "j1", "name": args["name"]})
        return json.dumps({"success": True, "count": 2, "jobs": [{"job_id": "j0"}, {"job_id": "j1", "name": "Weekly"}]})

    cron = HostCronProvider(allow_write=True, dispatch=dispatch)
    origin = {"platform": "telegram", "chat_id": "42", "thread_id": "7"}
    assert cron.create(prompt="Report", schedule="0 9 * * 1", name="Weekly", origin=origin) == {"id": "j1", "name": "Weekly", "schedule": "0 9 * * 1"}
    assert calls[0] == ("cronjob_manage", {"action": "create", "prompt": "Report", "schedule": "0 9 * * 1", "name": "Weekly", "deliver": "telegram:42:7"})
    assert cron.get("j1")["name"] == "Weekly" and cron.get("missing") is None
    assert deliver_target(None) == "" and deliver_target({"platform": "telegram", "chat_id": ""}) == "" and deliver_target({"chat_id": "42"}) == "telegram:42"


def test_the_cron_provider_refuses_without_write_or_without_the_host_and_reports_the_hosts_refusal():
    with pytest.raises(PermissionError):
        HostCronProvider(dispatch=lambda *a: "{}").create(prompt="p", schedule="s", name="n")
    with pytest.raises(RuntimeError):
        HostCronProvider(allow_write=True).create(prompt="p", schedule="s", name="n")
    refused = HostCronProvider(allow_write=True, dispatch=lambda *a: json.dumps({"success": False, "error": "bad schedule"}))
    with pytest.raises(RuntimeError, match="bad schedule"):
        refused.create(prompt="p", schedule="nope", name="n")
    assert HostCronProvider(hermes_home="/srv/h").output_dir() == Path("/srv/h/cron/output")


def test_past_sessions_are_searched_through_the_plugin_context():
    class Ctx:
        def dispatch_tool(self, name, args):
            return json.dumps({"tool": name, **args})

    assert json.loads(host_session_tool(Ctx())(query="hotel", limit=2)) == {"tool": "session_search", "query": "hotel", "limit": 2}
    assert host_session_tool(object()) is None


def test_a_secret_comes_from_the_process_environment_only_outside_the_host(monkeypatch):
    monkeypatch.setenv("RADAR_TEST_SECRET", "s3")
    assert read_secret("RADAR_TEST_SECRET") == "s3" and read_secret("RADAR_TEST_MISSING", "d") == "d"


def test_the_plugin_code_never_names_the_hosts_secret_files():
    root = Path(__file__).resolve().parents[2]
    for path in list((root / "radar").rglob("*.py")) + list((root / "dashboard").glob("*.py")):
        if path.name == "firewall.py":  # the firewall names them to block a job that reaches for them
            continue
        text = path.read_text(encoding="utf-8")
        assert '".env"' not in text and "webhook_subscriptions.json" not in text and '"auth.json"' not in text, path


def test_a_compaction_links_the_old_session_to_the_new_one_in_radars_own_store(tmp_path):
    from radar.store import Scope, Store

    store = Store(tmp_path / "r.sqlite3", Scope("default", "owner"))
    assert store.add_session_link("s1", "s2", now="2026-11-01T10:00:00Z") and store.add_session_link("s2", "s3", now="2026-11-02T10:00:00Z")
    assert not store.add_session_link("", "s9", now="x") and not store.add_session_link("s3", "s3", now="x")  # compacted in place
    assert store.session_thread("s1") == ["s1", "s2", "s3"] and store.session_thread("s3") == ["s3", "s2", "s1"]
    assert store.session_thread("other") == ["other"] and store.session_thread("") == []
