from __future__ import annotations

from types import SimpleNamespace

from conftest import NOW, active_config, verify_destination

from radar.delivery.dispatch import dispatch
from radar.delivery.preview import format_next, preview_next


def _seed(store, *, batch_key="now", proposal_id="p0", operation_id="web.read",
          action_class="read", urgency="normal", params=None):
    now = "2026-10-19T10:00:00Z"
    try:
        store.create_task(task_id="t1", kind="trip", title="Viaje a Lisboa", summary="Falta hotel",
                          entities={}, expectations=[], checklist=[], now=now)
    except Exception:
        pass
    store.create_task_proposal(
        proposal_id=proposal_id, task_id="t1", title="Reservar hotel", reason_text="No hay reserva",
        action_class=action_class, operation_id=operation_id,
        params=params if params is not None else {"message": "Tienes vuelo y no hotel."},
        options=[{"id": "do", "label": "Sí"}, {"id": "no", "label": "No"}],
        urgency=urgency, expires_at="2026-10-22T10:00:00Z",
        fingerprint=f"fp-{proposal_id}", now=now, batch_key=batch_key)


def _rt(store, cfg):
    return SimpleNamespace(store=store, config=cfg, clock=lambda: NOW, paused=False)


def test_next_empty_when_nothing_waits(store):
    cfg = active_config()
    verify_destination(store)
    preview = preview_next(store, cfg, NOW)
    assert preview["kind"] == "empty"
    assert format_next(preview, cfg, NOW).startswith("No hay avisos pendientes")


def test_next_shows_ready_notice_and_ignores_quiet_hours(store):
    cfg = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    verify_destination(store)
    _seed(store)  # NOW is 12:00 in Madrid: inside quiet hours, nothing enqueued yet
    preview = preview_next(store, cfg, NOW)
    assert preview["kind"] == "notice" and preview["held"] == "quiet_hours"
    assert "Viaje a Lisboa" in preview["text"] and preview["when"] is not None
    text = format_next(preview, cfg, NOW)
    assert text.startswith("📬 Siguiente aviso") and "silencio" in text and "13:00" in text


def test_next_urgent_notice_crosses_quiet_hours(store):
    cfg = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    verify_destination(store)
    _seed(store, proposal_id="urg", operation_id="check", urgency="high",
          params={"message": "Pago bloqueado.", "urgency": "now"})
    preview = preview_next(store, cfg, NOW)
    assert preview["kind"] == "notice" and preview["urgent"] is True and preview["held"] is None
    assert "cruza las horas de silencio" in format_next(preview, cfg, NOW)


def test_next_queued_outbox_wins_over_pending(store):
    cfg = active_config()
    verify_destination(store)
    _seed(store)
    assert dispatch(store, cfg, NOW)["notices"] == 1
    quiet = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    preview = preview_next(store, quiet, NOW)  # the enqueued notice is held by quiet hours now
    assert preview["kind"] == "queued" and preview["held"] == "quiet_hours"
    assert "Viaje a Lisboa" in preview["text"]
    assert format_next(preview, quiet, NOW).startswith("📬 Siguiente aviso (en cola")


def test_next_reminder_for_digest_decisions(store):
    cfg = active_config()
    verify_destination(store)
    _seed(store, batch_key="digest", proposal_id="dg", operation_id="ask")
    preview = preview_next(store, cfg, NOW)
    assert preview["kind"] == "reminder" and preview["when"] is not None
    text = format_next(preview, cfg, NOW)
    assert text.startswith("⏰ Siguiente recordatorio") and "Viaje a Lisboa" in text


def test_next_command_launches_and_answers(store):
    from radar.delivery.telegram_ui import command_text

    cfg = active_config()
    verify_destination(store)
    _seed(store)
    answer = command_text(_rt(store, cfg), "next")
    assert answer.startswith("📬 Lanzado") and "Viaje a Lisboa" in answer
    row = store.list_outbox(limit=1)[0]
    assert row["kind"] == "notice" and row["payload"]["urgent"] is True
    assert "status | list | next" in command_text(_rt(store, cfg), "bogus")


def test_next_inactive_without_destination(store):
    cfg = active_config()
    preview = preview_next(store, cfg, NOW)
    assert preview["kind"] == "inactive" and preview["reason"] == "no_destination"
    assert "verify" in format_next(preview, cfg, NOW)


def test_next_launches_queued_message_held_by_quiet_hours(store):
    from radar.delivery.preview import deliver_next, format_sent

    cfg = active_config()
    verify_destination(store)
    _seed(store)
    assert dispatch(store, cfg, NOW)["notices"] == 1
    quiet = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    result = deliver_next(store, quiet, NOW)
    assert result["kind"] == "queued" and result["quiet"] is True
    row = store.get_outbox(result["outbox_id"])
    assert row["payload"]["urgent"] is True and row["last_error"] is None
    assert row["next_attempt_at"] <= "2026-10-19T10:00:00Z"
    assert "aunque haya silencio" in format_sent(result, quiet)


def test_next_launches_ready_notice_and_tick_delivers_it_in_quiet_hours(store):
    import asyncio

    from radar.delivery.preview import deliver_next
    from radar.scheduler import Scheduler
    from radar.transports import DeliveryReceipt, TransportRegistry

    cfg = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    verify_destination(store)
    _seed(store)
    result = deliver_next(store, cfg, NOW)
    assert result["kind"] == "notice" and result["title"] == "Viaje a Lisboa"
    sent = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-9")

    transports.register("telegram", sender)
    scheduler = Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW)
    assert asyncio.run(scheduler.tick())["delivered"] == 1
    assert len(sent) == 1 and "Viaje a Lisboa" in sent[0]["text"]


def test_next_launches_digest_reminder(store):
    from radar.delivery.preview import deliver_next

    cfg = active_config()
    verify_destination(store)
    _seed(store, batch_key="digest", proposal_id="dg", operation_id="ask")
    result = deliver_next(store, cfg, NOW)
    assert result["kind"] == "reminder"
    row = store.get_outbox(result["outbox_id"])
    assert row["kind"] == "digest" and row["payload"]["urgent"] is True
    assert store.get_proposal("dg")["status"] == "sent"


def test_next_empty_launches_nothing(store):
    from radar.delivery.preview import deliver_next, format_sent

    cfg = active_config()
    verify_destination(store)
    assert deliver_next(store, cfg, NOW)["kind"] == "empty"
    assert format_sent({"kind": "empty"}, cfg).startswith("No hay avisos pendientes")
    assert store.list_outbox() == []


def test_next_sends_at_once_without_waiting_for_the_tick_even_in_quiet_hours(store):
    """2026-11-06 00:44: «/radar next» answered «Llega en el próximo envío» and the tick, busy with a long queue, was
    minutes away; the owner wanted the message in their chat at once, quiet hours or not."""
    import asyncio

    from radar.delivery.telegram_ui import command_text, launch_now
    from radar.scheduler import Scheduler
    from radar.transports import DeliveryReceipt, TransportRegistry

    quiet = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})  # NOW is 12:00 local
    verify_destination(store)
    _seed(store)
    assert dispatch(store, quiet, NOW)["notices"] == 1  # queued, and it would wait for 13:00
    sent = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-now")

    transports.register("telegram", sender)
    spawned = []

    class Ctx:
        @staticmethod
        def spawn_task(coroutine, name=None):
            spawned.append(name)
            return asyncio.get_running_loop().create_task(coroutine)

    scheduler = Scheduler(store, quiet, worker_id="w", transports=transports, clock=lambda: NOW)
    rt = SimpleNamespace(store=store, config=quiet, clock=lambda: NOW, paused=False, scheduler=scheduler, ctx=Ctx())

    async def run():
        answer = command_text(rt, "next")
        await asyncio.sleep(0)  # let the spawned delivery run
        await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()])
        return answer

    answer = asyncio.run(run())
    assert answer.startswith("📬 Te lo mando ahora mismo, aunque haya silencio: «Viaje a Lisboa»") and spawned == ["radar:next"]
    assert len(sent) == 1 and "Viaje a Lisboa" in sent[0]["text"]
    row = store.outbox_by_platform_message("msg-now")
    assert row["status"] == "sent" and row["payload"]["forced"] is True and row["payload"]["urgent"] is True
    # Without a runtime (the CLI, an older host) nothing is spawned and the wording promises the next delivery.
    _seed(store, proposal_id="p1")
    assert launch_now(SimpleNamespace(store=store), "x") is False
    assert command_text(SimpleNamespace(store=store, config=quiet, clock=lambda: NOW, paused=False), "next").startswith("📬 Lanzado, aunque haya silencio")
    assert store.claim_outbox_row("nope", now="2026-10-19T10:00:00Z", lease_until="2026-10-19T10:02:00Z") == []


def test_a_reminder_launched_by_the_command_is_rewritten_in_prose_before_it_goes_out(store, tmp_path):
    """00:54 the same night: the second «/radar next» queued a reminder straight from the template («Por cierto,
    sigue pendiente…»), since the command runs without the model. The sender now writes it as the tick would."""
    import asyncio

    from conftest import FakeContext, FakeLlm

    from radar.delivery.preview import deliver_next
    from radar.runtime import Runtime
    from radar.scheduler import Scheduler
    from radar.transports import DeliveryReceipt, TransportRegistry

    quiet = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    verify_destination(store)
    _seed(store, batch_key="digest", proposal_id="dg", operation_id="ask")
    result = deliver_next(store, quiet, NOW)
    row = store.get_outbox(result["outbox_id"])
    assert row["payload"]["template"] is True and row["payload"]["forced"] is True and row["payload"]["text"].startswith("Por cierto, sigue pendiente")
    sent = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt(f"msg-{len(sent)}")

    transports.register("telegram", sender)
    llm = FakeLlm({"radar.remind": [{"text": "Sigue sin hotel el viaje a Lisboa: ¿reservo el que vimos?"}]})
    rt = Runtime(FakeContext(quiet, llm=llm), store, quiet, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    scheduler = Scheduler(store, quiet, worker_id="w", transports=transports, clock=lambda: NOW, runtime=rt)
    assert asyncio.run(scheduler.deliver_now(result["outbox_id"], NOW)) == 1  # in quiet hours, at once
    assert sent[0]["text"] == "Sigue sin hotel el viaje a Lisboa: ¿reservo el que vimos?" and sent[0]["buttons"] == row["payload"]["buttons"]
    stored = store.get_outbox(result["outbox_id"])
    assert stored["status"] == "sent" and "template" not in stored["payload"] and stored["payload"]["text"] == sent[0]["text"]
    # Without the model the template goes out as it was; a message nobody holds cannot be rewritten.
    _seed(store, batch_key="digest", proposal_id="dg2", operation_id="ask")
    second = deliver_next(store, quiet, NOW)
    assert asyncio.run(scheduler.deliver_now(second["outbox_id"], NOW)) == 1
    assert sent[1]["text"].startswith("Por cierto, sigue pendiente") and "template" not in store.get_outbox(second["outbox_id"])["payload"]
    import pytest

    with pytest.raises(RuntimeError):
        store.update_outbox_payload(second["outbox_id"], fencing_token=99, payload={}, now="2026-10-19T10:00:00Z")


def test_next_from_a_worker_thread_hands_the_send_to_the_gateway_loop(store):
    """2026-11-06 01:57: the host runs a slash command in a worker thread, where there is no running loop; the first
    immediate-send version fell back to the tick silently. The command now submits the send to the scheduler's loop."""
    import asyncio

    from radar.delivery.telegram_ui import command_text, launch_now
    from radar.scheduler import Scheduler
    from radar.transports import DeliveryReceipt, TransportRegistry

    quiet = active_config(notifications={"quiet_start": "09:00", "quiet_end": "13:00"})
    verify_destination(store)
    _seed(store)
    assert dispatch(store, quiet, NOW)["notices"] == 1
    sent = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-thread")

    transports.register("telegram", sender)
    scheduler = Scheduler(store, quiet, worker_id="w", transports=transports, clock=lambda: NOW)

    async def run():
        rt = SimpleNamespace(store=store, config=quiet, clock=lambda: NOW, paused=False, scheduler=scheduler, loop=asyncio.get_running_loop(), ctx=object())
        answer = await asyncio.to_thread(command_text, rt, "next")  # a worker thread, like the host's
        for _ in range(20):  # the send runs on this loop
            await asyncio.sleep(0.01)
            if sent:
                break
        return answer

    answer = asyncio.run(run())
    assert answer.startswith("📬 Te lo mando ahora mismo") and len(sent) == 1 and "Viaje a Lisboa" in sent[0]["text"]
    assert store.outbox_by_platform_message("msg-thread")["status"] == "sent"
    # No loop anywhere: honest wording, the tick will send it.
    _seed(store, proposal_id="p1")
    bare = SimpleNamespace(store=store, config=quiet, clock=lambda: NOW, paused=False, scheduler=scheduler)
    assert launch_now(bare, "x") is False and command_text(bare, "next").startswith("📬 Lanzado")
