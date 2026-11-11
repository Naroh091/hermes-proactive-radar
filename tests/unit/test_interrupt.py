"""An immediate notice in the same voice as the reminder: what happened, why now, the one thing to decide."""
from __future__ import annotations

import asyncio
import json

from conftest import NOW, FakeContext, FakeLlm, active_config, verify_destination

from radar.delivery.dispatch import dispatch, select_interrupts
from radar.delivery.notice import build_interrupt, build_notice
from radar.delivery.remind import compose
from radar.prompts import REMIND_PROMPT
from radar.runtime import Runtime
from radar.scheduler import Scheduler
from radar.transports import DeliveryReceipt, TransportRegistry

T = "2026-10-19T10:00:00Z"
DRAFT = "Hola Ana, confirmo la cancelación del pedido 4471 y el reembolso de 120,50 €. Un saludo."


def _seed(store, *, with_draft=True):
    store.create_task(task_id="order", kind="purchase", title="Pedido Brightbot 4471", summary="Cancelado por el proveedor", entities={}, expectations=[],
                      checklist=[], now=T, due_at="2026-10-19T16:00:00Z")
    store.create_task_proposal(proposal_id="news", task_id="order", title="Brightbot ha cancelado el pedido 4471", reason_text="", action_class="read",
                               operation_id="inform", params={"message": "Brightbot cancela el pedido 4471; el reembolso llega en 5 días."},
                               options=[{"id": "ok", "label": "👍 Vale"}], urgency="high", expires_at="2026-10-22T10:00:00Z", fingerprint="news", now=T, batch_key="now")
    store.create_task_proposal(proposal_id="reply", task_id="order", title="Responder a Ana confirmando la cancelación", reason_text="Ana espera confirmación",
                               action_class="external", operation_id="gmail.reply", params={"urgency": "now", **({"draft": DRAFT} if with_draft else {})},
                               options=[{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}], urgency="high",
                               expires_at="2026-10-22T10:00:00Z", fingerprint="reply", now=T, batch_key="now")
    store.create_task_proposal(proposal_id="check", task_id="order", title="¿Era el pedido de las fundas?", reason_text="", action_class="read",
                               operation_id="check", params={}, options=[{"id": "ok", "label": "Sí, es correcto"}, {"id": "wrong", "label": "No, es un error"}],
                               urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="check", now=T, batch_key="now")


def test_the_prose_notice_keeps_the_classic_keyboard_order_and_draft(store):
    _seed(store)
    task = store.get_task("order")
    items = [store.get_proposal(i) for i in ("news", "reply", "check")]
    classic = build_notice(task, items, language="es", message="Brightbot cancela el pedido 4471")
    prose = build_interrupt(task, items, "Brightbot acaba de cancelar el pedido 4471 y Ana espera que se lo confirmes hoy.\n¿Le envío la respuesta?", language="es")
    assert prose["text"].startswith("Brightbot acaba de cancelar el pedido 4471 y Ana espera que se lo confirmes hoy. ¿Le envío la respuesta?")
    assert "<b>Propongo responder:</b>" in prose["text"] and "reembolso de 120,50 €" in prose["text"]
    assert "<b>🛒" not in prose["text"] and "Puedo hacer" not in prose["text"] and "Responde a este mensaje" not in prose["text"]
    assert prose["buttons"] == classic["buttons"] and prose["proposal_ids"] == classic["proposal_ids"] == ["news", "check", "reply"]
    assert prose["kind"] == "notice" and prose["task_id"] == "order" and prose["parse_mode"] == "HTML"
    bare = build_interrupt(task, items[:1], "Brightbot ha cancelado el pedido 4471; el reembolso llega en cinco días.", language="es")
    assert "Propongo responder" not in bare["text"] and bare["buttons"][0][0]["data"] == "pa:news:ok"


def test_select_interrupts_names_the_decision_and_the_news_of_each_task(store):
    cfg = active_config()
    verify_destination(store)
    _seed(store)
    [entry] = select_interrupts(store, cfg, NOW)
    assert entry["task"]["id"] == "order" and entry["decision"]["id"] == "check" and [p["id"] for p in entry["news"]] == ["news"]  # the first numbered one
    assert sorted(p["id"] for p in entry["items"]) == ["check", "news", "reply"]
    entry["text"] = "Brightbot acaba de cancelar el pedido 4471. ¿Le confirmo a Ana?"
    assert dispatch(store, cfg, NOW, interrupts=[entry])["notices"] == 1
    [row] = store.list_outbox()
    assert row["payload"]["text"].startswith("Brightbot acaba de cancelar el pedido 4471. ¿Le confirmo a Ana?") and row["payload"]["urgent"] is True
    assert row["payload"]["deadline"] == "2026-10-19T16:00:00Z" and all(store.get_proposal(i)["status"] == "sent" for i in ("news", "check"))
    # The prose asks one thing: the message carries that decision and the news; the other decision rides the reminder.
    assert row["payload"]["proposal_ids"] == ["news", "check"]
    assert store.get_proposal("reply")["status"] == "pending" and store.get_proposal("reply")["batch_key"] == "digest"


def test_without_a_text_the_notice_keeps_its_classic_layout(store):
    cfg = active_config()
    verify_destination(store)
    _seed(store)
    entries = select_interrupts(store, cfg, NOW)  # composed nowhere: no text
    assert dispatch(store, cfg, NOW, interrupts=entries)["notices"] == 1
    [row] = store.list_outbox()
    assert row["payload"]["text"].startswith("<b>🛒 Pedido Brightbot 4471</b>") and "Puedo hacer" in row["payload"]["text"]


def test_the_scheduler_writes_the_interruption_with_the_model_in_interrupt_mode(store, tmp_path):
    cfg = active_config()
    verify_destination(store)
    _seed(store)
    sent = []
    transports = TransportRegistry()

    async def sender(destination, payload):
        sent.append(payload)
        return DeliveryReceipt("msg-1")

    transports.register("telegram", sender)
    llm = FakeLlm({"radar.remind": [{"text": "Brightbot acaba de cancelar el pedido 4471 y Ana espera confirmación hoy. ¿Le envío la respuesta?"}]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    stats = asyncio.run(Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW, runtime=rt).tick())
    assert stats["dispatched"]["notices"] == 1 and stats["delivered"] == 1
    call = next(c for c in llm.calls if c["purpose"] == "radar.remind")
    payload = json.loads(call["input"][0]["text"])
    assert payload["mode"] == "interrupt" and payload["items"][0]["news"] == ["Brightbot cancela el pedido 4471; el reembolso llega en 5 días."]
    assert payload["items"][0]["pending"]["what"] == "¿Era el pedido de las fundas?" and "done" not in payload["items"][0]
    assert sent[0]["text"].startswith("Brightbot acaba de cancelar el pedido 4471 y Ana espera confirmación hoy. ¿Le envío la respuesta?")
    assert "<b>Propongo responder:</b>" not in sent[0]["text"] and sent[0]["buttons"][0][0]["data"] == "pa:news:ok"  # the reply step waits its turn
    assert [row[0]["data"] for row in sent[0]["buttons"][:2]] == ["pa:news:ok", "pa:check:ok"]


def test_when_the_model_cannot_write_it_the_interruption_goes_out_classic_not_from_the_template(store):
    cfg = active_config()
    _seed(store)
    task = store.get_task("order")
    items = [{"task": task, "decision": store.get_proposal("reply"), "news": [store.get_proposal("news")]}]
    gate = Runtime(FakeContext(cfg, llm=FakeLlm({})), store, cfg, worker_id="w", clock=lambda: NOW).gate  # no canned answer: the call fails
    assert asyncio.run(compose(gate, items=items, language="es", mode="interrupt", template=False)) == ""
    assert asyncio.run(compose(gate, items=items, language="es")).startswith("Por cierto, sigue pendiente")  # a reminder keeps its template
    flat = " ".join(REMIND_PROMPT.split())
    for phrase in ('If mode is "interrupt"', "Open with what happened", "why it cannot wait", 'Never a reminder\'s tone ("still pending"'):
        assert phrase in flat, phrase
