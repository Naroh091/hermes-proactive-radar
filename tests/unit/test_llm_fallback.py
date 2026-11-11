"""When the main model fails for any reason, radar retries with the host's own fallback providers."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from conftest import NOW, FakeContext, active_config

from radar import llm as llm_mod
from radar.llm import FALLBACK_BENCH_MINUTES, LlmError, LlmGate, fallback_entries, grant_override, override_granted

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
FALLBACKS = [{"provider": "opencode-go", "model": "muse-spark"}]


class BadRequestError(Exception):
    """Shaped like the Anthropic spend-limit answer: a plain HTTP 400 that no host ladder treats as capacity."""


class PluginLlmTrustError(PermissionError):
    pass


class FlakyLlm:
    """The main model answers ``main_error`` (or succeeds when it is None); the fallback provider always answers."""

    def __init__(self, main_error: Exception | None, *, deny_override: bool = False):
        self.main_error = main_error
        self.deny_override = deny_override
        self.calls: list[dict] = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("provider"):
            if self.deny_override:
                raise PluginLlmTrustError("Plugin 'radar' cannot override the provider (set plugins.entries.radar.llm.allow_provider_override to true to allow).")
            return SimpleNamespace(parsed={"ok": True, "by": "fallback"}, text="", model=kwargs["model"], provider=kwargs["provider"],
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=0.0))
        if self.main_error is not None:
            raise self.main_error
        return SimpleNamespace(parsed={"ok": True, "by": "main"}, text="", model="claude-haiku-5-5", provider="anthropic",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cost_usd=0.0))


def _gate(store, fake, *, fallbacks=None, clock=None):
    ticks = {"now": NOW}
    gate = LlmGate(FakeContext(active_config(), llm=fake), store, active_config(), clock=clock or (lambda: ticks["now"]),
                   fallbacks=(lambda: fallbacks) if fallbacks is not None else (lambda: list(FALLBACKS)))
    return gate, ticks


def _ask(gate, purpose="radar.extract"):
    return asyncio.run(gate.structured("extract", instructions="i", input_text="t", schema=SCHEMA, purpose=purpose, subject_id="s1"))


def _llm_audits(store):
    """LLM audit rows, newest first."""
    return [row.get("metadata_redacted") or {} for row in store.list_audit(limit=20) if row.get("event_type") == "llm_call"]


def test_a_failing_main_model_is_rescued_by_the_host_fallback_and_benched(store):
    fake = FlakyLlm(BadRequestError("Error code: 400 - You have reached your specified API usage limits."))
    gate, ticks = _gate(store, fake)
    assert _ask(gate)["by"] == "fallback"
    assert [c.get("provider") for c in fake.calls] == [None, "opencode-go"]  # main first, then the fallback
    assert fake.calls[1]["model"] == "muse-spark" and fake.calls[1]["task"] == "radar_extract"  # the slot is kept
    audit = _llm_audits(store)[0]
    assert audit["fallback"] == {"provider": "opencode-go", "model": "muse-spark"} and audit["provider"] == "opencode-go"
    # Within the bench window the main model is not even tried.
    ticks["now"] = NOW + timedelta(minutes=FALLBACK_BENCH_MINUTES - 1)
    assert _ask(gate)["by"] == "fallback" and len(fake.calls) == 3 and fake.calls[2]["provider"] == "opencode-go"
    # Once the bench expires the main model gets another chance, and when it works the fallback is not used.
    ticks["now"] = NOW + timedelta(minutes=FALLBACK_BENCH_MINUTES + 1)
    fake.main_error = None
    assert _ask(gate)["by"] == "main" and len(fake.calls) == 4 and fake.calls[3].get("provider") is None
    assert "fallback" not in _llm_audits(store)[0]


def test_a_bench_is_renewed_when_the_main_model_still_fails(store):
    fake = FlakyLlm(BadRequestError("usage limits"))
    gate, ticks = _gate(store, fake)
    _ask(gate)
    ticks["now"] = NOW + timedelta(minutes=FALLBACK_BENCH_MINUTES + 1)
    assert _ask(gate)["by"] == "fallback"
    assert [c.get("provider") for c in fake.calls] == [None, "opencode-go", None, "opencode-go"]
    assert gate.benched_until == ticks["now"] + timedelta(minutes=FALLBACK_BENCH_MINUTES)


def test_without_the_override_grant_the_main_error_surfaces_and_the_override_is_asked_again_later(store):
    fake = FlakyLlm(BadRequestError("usage limits"), deny_override=True)
    gate, ticks = _gate(store, fake)
    with pytest.raises(LlmError) as first:
        _ask(gate)
    assert first.value.transient and "BadRequestError" in str(first.value)  # the main error, costing the signal no attempt
    assert gate.override_denied_until == NOW + timedelta(minutes=FALLBACK_BENCH_MINUTES)
    with pytest.raises(LlmError):
        _ask(gate)
    assert [c.get("provider") for c in fake.calls] == [None, "opencode-go", None]  # no second override attempt for now
    assert gate.benched_until is None
    # The owner adds the grant to the host config; it is read per call, so the next ask after the pause goes through.
    ticks["now"] = NOW + timedelta(minutes=FALLBACK_BENCH_MINUTES + 1)
    fake.deny_override = False
    assert _ask(gate)["by"] == "fallback" and fake.calls[-1]["provider"] == "opencode-go"


def test_without_fallbacks_nothing_changes(store):
    fake = FlakyLlm(BadRequestError("usage limits"))
    gate, _ = _gate(store, fake, fallbacks=[])
    with pytest.raises(LlmError) as err:
        _ask(gate)
    assert err.value.transient and len(fake.calls) == 1


def test_fallbacks_are_tried_in_order_and_a_failing_one_is_skipped(store):
    class TwoTier(FlakyLlm):
        def complete_structured(self, **kwargs):
            if kwargs.get("provider") == "dead":
                self.calls.append(kwargs)
                raise ConnectionError("no route")
            return super().complete_structured(**kwargs)

    fake = TwoTier(BadRequestError("usage limits"))
    gate, _ = _gate(store, fake, fallbacks=[{"provider": "dead", "model": "x"}, *FALLBACKS])
    assert _ask(gate)["by"] == "fallback"
    assert [c.get("provider") for c in fake.calls] == [None, "dead", "opencode-go"]


def test_when_every_fallback_fails_the_main_error_is_the_one_raised(store):
    class AllDown(FlakyLlm):
        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            raise BadRequestError("usage limits") if not kwargs.get("provider") else ConnectionError("no route")

    gate, _ = _gate(store, AllDown(None))
    with pytest.raises(LlmError) as err:
        _ask(gate)
    assert "BadRequestError" in str(err.value) and gate.benched_until is None


def test_host_fallbacks_come_from_fallback_providers_and_ignore_malformed_entries(monkeypatch):
    assert fallback_entries([{"provider": "opencode-go", "model": "muse-spark"}, {"provider": "x"}, "junk", {"model": "", "provider": ""}]) == FALLBACKS
    assert fallback_entries(None) == [] and fallback_entries({"provider": "a", "model": "b"}) == []
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli", SimpleNamespace(config=SimpleNamespace(load_config_readonly=lambda: {"fallback_providers": FALLBACKS})))
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli.config", SimpleNamespace(load_config_readonly=lambda: {"fallback_providers": FALLBACKS}))
    assert llm_mod.host_fallbacks() == FALLBACKS


def test_the_grant_is_both_flags_under_the_plugin_entry():
    assert override_granted({}) is False and override_granted(None) is False
    assert override_granted({"plugins": {"entries": {"radar": {"llm": {"allow_provider_override": True}}}}}) is False  # one flag is not enough
    cfg = {"plugins": {"entries": {"radar": {"allow_tool_override": False}}}}
    assert override_granted(grant_override(cfg, True)) is True
    assert cfg["plugins"]["entries"]["radar"] == {"allow_tool_override": False, "llm": {"allow_provider_override": True, "allow_model_override": True}}
    assert override_granted(grant_override(cfg, False)) is False
    assert override_granted(grant_override({}, True)) is True
