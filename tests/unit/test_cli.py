from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest

from radar import cli
from radar.cli import command


class DisabledContext:
    profile_name = "default"

    @staticmethod
    def get_config(key, default=None):
        return False if key == "enabled" else default


def test_disabled_cli_status_does_not_require_owner_or_open_store(capsys):
    result = command(DisabledContext(), argparse.Namespace(radar_action="status"))
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload == {"enabled": False, "configured": False, "mode": "shadow", "store": None}


FALLBACKS = [{"provider": "opencode-go", "model": "muse-spark"}]


class FakeHostConfig:
    """Stands in for hermes_cli.config: one in-memory config.yaml."""

    def __init__(self, data):
        self.data = data
        self.saved = []

    def load_config(self):
        return deepcopy(self.data)

    def save_config(self, config, *, merge_existing=False):
        self.saved.append((deepcopy(config), merge_existing))
        self.data = deepcopy(config)


@pytest.fixture
def host(monkeypatch):
    fake = FakeHostConfig({"fallback_providers": FALLBACKS, "plugins": {"entries": {"hermes-proactive-radar": {"allow_tool_override": False}}}})
    monkeypatch.setitem(sys.modules, "hermes_cli", SimpleNamespace(config=fake))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake)
    return fake


def _fallback(capsys):
    code = command(DisabledContext(), argparse.Namespace(radar_action="fallback"))
    return code, json.loads(capsys.readouterr().out)


def test_fallback_shows_the_grant_and_has_no_way_to_change_it(host, capsys):
    """Catalog review, 2026-11-11: the grant is two capabilities Hermes asks for; the command only reports."""
    code, status = _fallback(capsys)
    assert code == 0 and status["granted"] is False and status["fallback_providers"] == FALLBACKS and "hermes plugins enable hermes-proactive-radar" in status["note"]
    with pytest.raises(SystemExit):  # `on` / `off` are gone from the command line
        parser = argparse.ArgumentParser()
        cli.setup_cli(parser)
        parser.parse_args(["fallback", "on"])
    assert host.saved == [] and "llm" not in host.data["plugins"]["entries"]["hermes-proactive-radar"]
    host.data["plugins"]["entries"]["hermes-proactive-radar"]["llm"] = {"allow_provider_override": True, "allow_model_override": True}  # Hermes recorded it
    assert _fallback(capsys)[1]["granted"] is True
    host.data.pop("fallback_providers")
    assert "hermes model" in _fallback(capsys)[1]["note"]


def test_onboarding_only_tells_where_the_fallback_stands(host, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_ask", lambda *a, **k: pytest.fail("the fallback is not setup's question"))
    cli._fallback_note()
    assert "Not granted" in capsys.readouterr().out and host.saved == []
    assert not hasattr(cli, "_ask_fallback") and not hasattr(cli, "_grant_host")


def test_the_mcp_allowlist_is_written_only_on_a_yes_the_owner_typed(host, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr(cli, "_ask", lambda prompt, default="", *, yes=False: asked.append((prompt, default)) or default)
    cli._grant_mcp(["gmail"], yes=False)  # the default answer
    assert asked[0][1] == "n" and "gmail" in asked[0][0] and host.saved == []
    assert "plugins.entries.hermes-proactive-radar.mcp_allowlist: ['gmail']" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_ask", lambda *a, **k: pytest.fail("--yes must not ask, and must not grant"))
    cli._grant_mcp(["gmail"], yes=True)
    assert host.saved == [] and "Add to config.yaml" in capsys.readouterr().out
    cli._grant_mcp([], yes=False)  # nothing to grant, nothing asked
    monkeypatch.setattr(cli, "_ask", lambda *a, **k: "y")
    cli._grant_mcp(["gmail", "calendar"], yes=False)
    entry = host.data["plugins"]["entries"]["hermes-proactive-radar"]
    assert len(host.saved) == 1 and host.saved[0][1] is True and entry == {"allow_tool_override": False, "mcp_allowlist": ["gmail", "calendar"]}  # that key only
    cli._grant_mcp(["gmail"], yes=False)  # already there: no second write
    assert len(host.saved) == 1
