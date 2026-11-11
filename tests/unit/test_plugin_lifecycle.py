from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

import radar


class FakeContext:
    profile_name = "isolated-test"

    def __init__(self):
        self.spawn_count = 0

    def get_config(self, key, default=None):
        values = {
            "enabled": True,
            "mode": "shadow",
            "owner": {"id": "owner-test"},
            "scheduler": {"tick_seconds": 60},
        }
        return values.get(key, default)

    def spawn_task(self, coroutine, name=None):
        self.spawn_count += 1
        return asyncio.create_task(coroutine, name=name)


class DummyStore:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def disable_missing_configured_sources(self, active_source_ids, now):
        return 0


def test_status_when_disabled_does_not_open_store(monkeypatch):
    class DisabledContext:
        profile_name = "isolated-test"

        def get_config(self, key, default=None):
            return default

    context = DisabledContext()
    monkeypatch.setattr(radar, "_store", lambda ctx: (_ for _ in ()).throw(AssertionError("store must stay closed")))

    payload = json.loads(radar.status(context))

    assert payload["enabled"] is False
    assert payload["configured"] is False
    assert payload["store"] is None


def test_scheduler_starts_once_and_cleanup_cancels_it(monkeypatch):
    async def scenario():
        context = FakeContext()
        store = DummyStore()
        monkeypatch.setattr(radar, "_store", lambda ctx: store)

        radar.on_session_start(context)
        radar.on_session_start(context)

        assert context.spawn_count == 1
        task = radar._RUNTIME[id(context)]["task"]
        assert not task.done()

        radar.cleanup(context)
        await asyncio.sleep(0)

        assert task.cancelled()
        assert store.closed
        assert id(context) not in radar._RUNTIME

    asyncio.run(scenario())


def test_ensure_scheduler_starts_when_idle_and_is_idempotent(monkeypatch):
    async def scenario():
        context = FakeContext()
        store = DummyStore()
        monkeypatch.setattr(radar, "_store", lambda ctx: store)
        assert radar._scheduler_running(context) is False
        radar.ensure_scheduler(context)  # idle → starts
        radar.ensure_scheduler(context)  # already running → no second task
        assert context.spawn_count == 1 and radar._scheduler_running(context) is True
        radar.cleanup(context)
        await asyncio.sleep(0)
        assert radar._scheduler_running(context) is False

    asyncio.run(scenario())


def test_capture_turn_wakes_the_scheduler_after_a_restart(monkeypatch):
    # With Telegram disabled, on_session_start may never fire; an LLM-call hook must still bring the scheduler up.
    async def scenario():
        context = FakeContext()
        store = DummyStore()
        monkeypatch.setattr(radar, "_store", lambda ctx: store)
        radar.capture_turn(context, user_message="hola", assistant_response="x", session_id="s", platform="cli")
        assert context.spawn_count == 1 and radar._scheduler_running(context) is True
        radar.cleanup(context)
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_scheduler_delegates_to_host_without_local_event_loop(monkeypatch):
    class HostTask:
        cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    class SyncContext(FakeContext):
        def spawn_task(self, coroutine, name=None):
            self.spawn_count += 1
            coroutine.close()
            return HostTask()

    context = SyncContext()
    store = DummyStore()
    monkeypatch.setattr(radar, "_store", lambda ctx: store)

    radar.on_session_start(context)

    assert context.spawn_count == 1
    assert id(context) in radar._RUNTIME
    radar.cleanup(context)
    assert store.closed


def test_spawn_failure_is_fail_closed_and_releases_store(monkeypatch):
    async def scenario():
        context = FakeContext()
        store = DummyStore()
        monkeypatch.setattr(radar, "_store", lambda ctx: store)

        def fail_spawn(coroutine, name=None):
            raise OSError("host task registry unavailable")

        context.spawn_task = fail_spawn
        radar.on_session_start(context)

        assert store.closed
        assert id(context) not in radar._RUNTIME

    asyncio.run(scenario())


def test_store_requires_reliable_profile_identity(tmp_path, monkeypatch):
    class MissingProfileContext:
        def get_config(self, key, default=None):
            if key == "owner":
                return {"id": "owner-test"}
            return default

    context = MissingProfileContext()
    monkeypatch.setattr(radar, "get_hermes_home", lambda: tmp_path)

    with pytest.raises(ValueError, match="profile identity"):
        radar._store(context)


def test_telegram_factory_starts_scheduler_after_transport_registration(monkeypatch):
    calls = []

    class CallbackQueryHandler:
        def __init__(self, callback, pattern=None):
            self.callback = callback
            self.pattern = pattern

    class Native:
        bot = object()

        def __init__(self):
            self.handlers = []

        def add_handler(self, handler, group=None):
            self.handlers.append((handler, group))

    telegram_module = types.ModuleType("telegram")
    telegram_ext_module = types.ModuleType("telegram.ext")
    telegram_ext_module.CallbackQueryHandler = CallbackQueryHandler
    monkeypatch.setitem(sys.modules, "telegram", telegram_module)
    monkeypatch.setitem(sys.modules, "telegram.ext", telegram_ext_module)
    monkeypatch.setattr(radar, "telegram_sender", lambda bot: lambda *args, **kwargs: None)
    monkeypatch.setattr(radar, "on_session_start", lambda ctx, **kwargs: calls.append(ctx))
    context = object()
    native = Native()

    radar.telegram_factory(context)(native, object())

    assert calls == [context]
    assert native.handlers[0][0].pattern == r"^p[at]:" and native.handlers[0][1] == -1


def test_job_child_processes_never_start_the_scheduler_or_record_turns(monkeypatch):
    monkeypatch.setenv("HERMES_RADAR_JOB", "1")
    context = FakeContext()
    monkeypatch.setattr(radar, "_store", lambda ctx: (_ for _ in ()).throw(AssertionError("store must stay closed")))

    radar.on_session_start(context)
    radar.capture_turn(context, user_message="hola", assistant_response="x", session_id="s", platform="cli")

    assert context.spawn_count == 0
    assert id(context) not in radar._RUNTIME


def test_radar_slots_register_with_background_timeouts():
    """The host cuts auxiliary calls at 30 s unless the task says otherwise; radar's run in the background."""
    from types import SimpleNamespace

    from radar.llm import SLOT_TIMEOUTS, SLOTS, register_aux_tasks

    seen: dict[str, dict] = {}
    ctx = SimpleNamespace(register_auxiliary_task=lambda key, **kwargs: seen.__setitem__(key, kwargs))
    assert sorted(register_aux_tasks(ctx)) == sorted("radar_" + slot for slot in SLOTS)
    assert seen["radar_plan"]["defaults"]["timeout"] == SLOT_TIMEOUTS["plan"] >= 180
    assert seen["radar_extract"]["defaults"]["timeout"] >= 120 and all(k["defaults"]["timeout"] >= 45 for k in seen.values())
    assert register_aux_tasks(SimpleNamespace()) == []  # a host without auxiliary tasks is fine
