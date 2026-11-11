"""The host's side of the tool firewall: how tools are named, and that the hook is called the way Radar expects."""
from __future__ import annotations

import host_bootstrap  # noqa: F401  (skips this module without a host)

from pathlib import Path

import yaml

import hermes_cli.plugins as host_plugins
import toolsets
from tools.mcp_tool_schema import mcp_prefixed_tool_name
from tools.tool_search_validation import normalize_tool_call_entries

from radar.jobs import firewall
from radar.jobs.firewall import guard_tool_call, tool_stem
from radar.jobs.mark import bind, forget_all

ROOT = Path(__file__).resolve().parents[2]
READ = {"action_class": "read", "allow": ""}


def test_an_mcp_tools_last_segment_is_the_tool_whatever_the_server_is_called():
    assert tool_stem(mcp_prefixed_tool_name("gmail-work", "send_email")) == "send_email"
    assert tool_stem(mcp_prefixed_tool_name("a.b c", "reply_all")) == "reply_all"


def test_a_name_the_host_clamps_is_still_refused():
    """Over 64 characters the host cuts the name and adds a hash: whatever survives must not read as a harmless tool."""
    name = mcp_prefixed_tool_name("a-very-long-server-name-for-someones-mailbox-account", "reply_all_with_attachment")
    assert len(name) <= 64
    assert guard_tool_call(tool_name=name, args={}, job=READ)["action"] == "block"


def test_every_hook_radar_declares_exists_in_the_host():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert set(manifest["provides_hooks"]) <= set(host_plugins.VALID_HOOKS)


def test_the_host_still_has_the_tools_the_firewall_names():
    core = set(toolsets._HERMES_CORE_TOOLS)
    assert firewall.JOB_TOOLS - {"tool_search", "tool_describe", "tool_call", "todo"} <= core
    assert {"delegate_task", "execute_code", "terminal", "cronjob_manage"} <= core
    assert any(name.startswith(firewall.JOB_TOOL_PREFIXES) for name in core)


def test_the_pre_tool_call_hook_gets_what_the_guard_reads_and_its_veto_is_obeyed(monkeypatch):
    """Through the host's real dispatcher, with the arguments it really passes."""
    import radar

    seen: list[dict] = []

    class Ctx:
        profile_name = "contract"

        def get_config(self, key, default=None):
            return default

    forget_all()
    bind("job-session", dict(READ, job_id="j"))

    def fake_invoke(hook, **kwargs):
        assert hook == "pre_tool_call"
        seen.append(kwargs)
        return [radar.guard(Ctx(), **kwargs)]

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", fake_invoke)
    message, _ = host_plugins._dispatch_pre_tool_call_hooks("mcp__gmail__send_email", {"to": "a@b.c"}, session_id="job-session", task_id="t", tool_call_id="c", turn_id="u", api_request_id="r")
    assert seen[0]["tool_name"] == "mcp__gmail__send_email" and seen[0]["args"] == {"to": "a@b.c"} and seen[0]["session_id"] == "job-session"
    assert message and "radar refused" in message
    # The owner's own session is left alone.
    message, _ = host_plugins._dispatch_pre_tool_call_hooks("mcp__gmail__send_email", {"to": "a@b.c"}, session_id="owners-session", task_id="t", tool_call_id="c", turn_id="u", api_request_id="r")
    assert message is None
    forget_all()


def test_a_block_is_fail_closed_when_the_hook_times_out():
    from hermes_cli.plugins_dispatch import _HOOK_TIMEOUT_FAIL_CLOSED_HOOKS

    assert "pre_tool_call" in _HOOK_TIMEOUT_FAIL_CLOSED_HOOKS


def test_the_tool_call_bridge_names_the_real_tool():
    """Most MCP calls of a job travel through ``tool_call``; the host unwraps it so the hook judges the tool behind it."""
    entries, error = normalize_tool_call_entries({"calls": [{"name": "mcp__gmail__send_email", "arguments": {"to": "a"}}]})
    assert error is None and entries[0]["name"] == "mcp__gmail__send_email"
    source = (Path(host_bootstrap.HOST).expanduser() / "model_tools.py").read_text(encoding="utf-8")
    assert "every downstream hook (pre/post, edit" in source and "sees the real tool name" in source
