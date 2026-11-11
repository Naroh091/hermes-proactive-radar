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
    fake = FakeHostConfig({"fallback_providers": FALLBACKS, "plugins": {"entries": {"radar": {"allow_tool_override": False}}}})
    monkeypatch.setitem(sys.modules, "hermes_cli", SimpleNamespace(config=fake))
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake)
    return fake


def _fallback(state, capsys):
    code = command(DisabledContext(), argparse.Namespace(radar_action="fallback", state=state))
    return code, json.loads(capsys.readouterr().out)


def test_fallback_status_on_and_off_write_the_host_grant(host, capsys):
    code, status = _fallback(None, capsys)
    assert code == 0 and status["granted"] is False and status["fallback_providers"] == FALLBACKS and "fallback on" in status["note"]
    code, on = _fallback("on", capsys)
    assert code == 0 and on["granted"] is True and "no restart needed" in on["note"]
    assert host.data["plugins"]["entries"]["radar"]["llm"] == {"allow_provider_override": True, "allow_model_override": True}
    assert host.data["plugins"]["entries"]["radar"]["allow_tool_override"] is False and host.saved[-1][1] is True  # merged, nothing else touched
    assert _fallback(None, capsys)[1]["granted"] is True
    code, off = _fallback("off", capsys)
    assert code == 0 and off["granted"] is False
    assert host.data["plugins"]["entries"]["radar"]["llm"] == {"allow_provider_override": False, "allow_model_override": False}


def test_fallback_on_refuses_when_the_host_has_no_fallback_providers(host, capsys):
    host.data.pop("fallback_providers")
    code, payload = _fallback("on", capsys)
    assert code == 1 and "no fallback_providers" in payload["error"] and host.saved == []
    assert "hermes model" in _fallback(None, capsys)[1]["note"]


def test_onboarding_asks_for_the_fallback_and_writes_it_with_the_mcp_allowlist_in_one_save(host, monkeypatch, capsys):
    answers = []
    monkeypatch.setattr(cli, "_ask", lambda prompt, default="", *, yes=False: answers.append((prompt, default)) or "y")
    assert cli._ask_fallback(yes=False) is True
    assert "muse-spark via opencode-go" in answers[0][0] and answers[0][1] == "y"
    cli._grant_host(["gmail"], fallback=True)
    assert len(host.saved) == 1
    radar = host.data["plugins"]["entries"]["radar"]
    assert radar["mcp_allowlist"] == ["gmail"] and radar["llm"] == {"allow_provider_override": True, "allow_model_override": True}
    out = capsys.readouterr().out
    assert "model fallback granted" in out and "mcp_allowlist granted" in out
    # Already granted and answered yes again: nothing to write.
    cli._grant_host([], fallback=True)
    assert len(host.saved) == 1
    # Answered no: revoked.
    cli._grant_host([], fallback=False)
    assert len(host.saved) == 2 and host.data["plugins"]["entries"]["radar"]["llm"]["allow_provider_override"] is False


def test_onboarding_skips_the_question_without_fallback_providers(host, monkeypatch, capsys):
    host.data.pop("fallback_providers")
    monkeypatch.setattr(cli, "_ask", lambda *a, **k: pytest.fail("must not ask"))
    assert cli._ask_fallback(yes=False) is None
    assert "no fallback_providers" in capsys.readouterr().out
    cli._grant_host([], fallback=None)
    assert host.saved == []
