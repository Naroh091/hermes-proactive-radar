"""The rest of the host Radar leans on: plugin context, state database, session search, cron, memory, gateway, panel."""
from __future__ import annotations

import host_bootstrap  # noqa: F401  (skips this module without a host)

import inspect
import json
import sqlite3
from dataclasses import fields
from pathlib import Path

import pytest

from hermes_cli.plugins import PluginContext


@pytest.mark.parametrize("name", [
    "register_tool", "register_hook", "register_command", "register_platform_handler", "register_cli_command", "register_auxiliary_task",
    "call_mcp", "set_config", "spawn_task", "get_config", "profile_name", "on_unload", "llm", "dispatch_tool",
])
def test_the_plugin_context_offers_what_radar_calls(name):
    assert hasattr(PluginContext, name)


def test_the_host_llm_facade_has_the_structured_calls():
    source = inspect.getsource(inspect.getmodule(PluginContext))
    assert "def llm(self)" in source
    from agent import plugin_llm

    names = dir(plugin_llm.PluginLlm) if hasattr(plugin_llm, "PluginLlm") else dir(plugin_llm)
    assert {"complete_structured", "acomplete_structured"} <= set(names)


def test_home_and_config_are_where_radar_reads_them():
    from hermes_cli.config import load_config, load_config_readonly, save_config
    from hermes_constants import get_hermes_home

    assert str(get_hermes_home()) == host_bootstrap.os.environ["HERMES_HOME"]
    assert callable(load_config) and callable(save_config) and callable(load_config_readonly)


def test_the_secret_scope_radar_enters_exists():
    source = (Path(__file__).resolve().parents[2] / "radar" / "hostscope.py").read_text(encoding="utf-8")
    block = source.split("from agent.secret_scope import (", 1)[1].split(")", 1)[0]
    import agent.secret_scope as scope

    for name in (part.split(" as ")[0].strip() for part in block.split(",")):
        if name:
            assert hasattr(scope, name), name


@pytest.fixture()
def state(tmp_path):
    from hermes_state import SessionDB
    from hermes_state_registry import release_or_close

    db = SessionDB(db_path=tmp_path / "state.db")
    yield db, tmp_path / "state.db"
    release_or_close(db)


def test_the_state_database_has_the_shape_lineage_reads(state):
    _, path = state
    conn = sqlite3.connect(path)
    sessions = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    leases = {row[1] for row in conn.execute("PRAGMA table_info(session_turn_leases)")}
    conn.close()
    assert {"id", "last_activity_at", "session_key", "started_at", "parent_session_id"} <= sessions
    assert {"conversation_id", "holder", "expires_at"} <= leases


def test_the_state_database_can_be_read_only_and_archives_a_session(state):
    from hermes_state import SessionDB

    db, path = state
    assert "read_only" in inspect.signature(SessionDB.__init__).parameters
    assert list(inspect.signature(SessionDB.set_session_archived).parameters)[1:3] == ["session_id", "archived"]
    db.create_session("job-1", source="cli")
    assert db.set_session_archived("job-1", True) in (True, 1)


def test_session_search_keeps_the_parameters_and_envelope_recall_uses(state):
    from tools.session_search_tool import session_search

    assert {"query", "limit", "role_filter", "session_id", "around_message_id", "window"} <= set(inspect.signature(session_search).parameters)
    db, _ = state
    answer = json.loads(session_search(query="nothing here", limit=1, db=db))
    assert answer["success"] is True and isinstance(answer.get("results"), list)


def test_cron_creates_and_reads_as_the_provider_expects():
    from cron import jobs

    assert {"prompt", "schedule", "name", "deliver", "origin"} <= set(inspect.signature(jobs.create_job).parameters)
    assert callable(jobs.get_job) and callable(jobs.get_cron_output_dir)


def test_the_memory_loader_takes_the_keyword_recall_passes():
    from plugins.memory import load_memory_provider

    assert "register_skills" in inspect.signature(load_memory_provider).parameters


def test_gateway_events_carry_what_replies_and_webhooks_need():
    from gateway.platforms.event import MessageEvent

    names = {field.name for field in fields(MessageEvent)}
    assert {"reply_to_message_id", "raw_message", "source"} <= names
    source = (Path(host_bootstrap.HOST).expanduser() / "gateway" / "platforms" / "webhook.py").read_text(encoding="utf-8")
    assert 'user_id=f"webhook:{route_name}"' in source


def test_the_gateway_session_env_reader_exists():
    from gateway.session_context import get_session_env

    assert callable(get_session_env)


def test_the_panel_routes_are_behind_the_dashboards_auth():
    from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS
    from hermes_cli import web_server_chat

    assert not any(path.startswith("/api/plugins") for path in PUBLIC_API_PATHS)
    assert callable(web_server_chat._ws_auth_ok)  # dashboard/plugin_api.py accepts the socket when this does not import


def test_discovery_reads_the_cache_the_host_writes():
    from tools import mcp_schema_cache

    assert mcp_schema_cache._CACHE_FILENAME == "mcp_schema_cache.json"
