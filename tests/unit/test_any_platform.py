"""Radar writes to the owner on whichever gateway platform they chose. Telegram draws buttons; every other platform
gets the same words as markdown through the host's adapter, with the answers named on one line, and the owner's
written reply to that message is understood the same way."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from conftest import NOW, active_config

from radar.delivery.dispatch import dispatch
from radar.delivery.telegram_ui import handle_text_reply, verify_from_session
from radar.scheduler import Scheduler
from radar.schemas import validate_config
from radar.transports import TransportRegistry, adapter_editor, adapter_sender, adapter_text, choices_line, to_markdown

DISCORD = {"platform": "discord", "gateway_connection_ref": "discord:default", "address": {"chat_id": "c9"}, "owner_platform_id": "u7"}


class Adapter:
    name = "discord"

    def __init__(self, *, ok=True, edits=True):
        self.sent, self.documents, self.edited, self.ok, self.edits = [], [], [], ok, edits

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SimpleNamespace(success=self.ok, message_id=f"m{len(self.sent)}" if self.ok else None, error="channel gone")

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, metadata=None, **kwargs):
        self.documents.append((chat_id, file_path, caption, file_name))
        return SimpleNamespace(success=True)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self.edited.append((chat_id, message_id, content))
        return SimpleNamespace(success=self.edits)


def test_the_notice_html_becomes_markdown_and_the_buttons_become_one_line_of_answers():
    html = '<b>Pedido 4471</b> de <i>Acme</i>: <a href="https://x.example/o?a=1&amp;b=2">ver pedido</a> &lt;hoy&gt;\n<blockquote>Llega el lunes\nPor la tarde</blockquote><code>AB12</code>'
    assert to_markdown(html) == "**Pedido 4471** de _Acme_: [ver pedido](https://x.example/o?a=1&b=2) <hoy>\n> Llega el lunes\n> Por la tarde`AB12`"
    buttons = [[{"label": "Adelante", "data": "pa:p1:do"}, {"label": "Descartar", "data": "pa:p1:no"}], [{"label": "Mañana", "data": "pt:t1:later"}], "junk"]
    assert choices_line(buttons) == "↩ Adelante · Descartar · Mañana" and choices_line([]) == "" and choices_line(None) == ""
    assert adapter_text({"text": "<b>Hola</b>", "parse_mode": "HTML", "buttons": buttons}) == "**Hola**\n\n↩ Adelante · Descartar · Mañana"
    assert adapter_text({"text": "a < b", "parse_mode": None}) == "a < b"  # plain text is sent as it is
    with pytest.raises(ValueError):
        adapter_text({"text": "  "})


def test_a_notice_goes_out_through_the_hosts_adapter_with_its_thread_and_its_report():
    adapter = Adapter()
    send = adapter_sender(adapter)
    destination = {"address": {"chat_id": 123, "thread_id": "t5"}}
    receipt = asyncio.run(send(destination, {"text": "<b>Listo</b>", "parse_mode": "HTML", "document": {"path": "/tmp/r.html", "filename": "r.html", "caption": "Informe"}}))
    assert receipt.platform_message_id == "m1" and adapter.sent == [("123", "**Listo**", {"thread_id": "t5"})]
    assert adapter.documents == [("123", "/tmp/r.html", "Informe", "r.html")]
    with pytest.raises(RuntimeError, match="channel gone"):
        asyncio.run(adapter_sender(Adapter(ok=False))(destination, {"text": "x"}))
    with pytest.raises(ValueError):
        asyncio.run(send({"address": {}}, {"text": "x"}))
    asyncio.run(adapter_editor(adapter)(destination, "m1", {"text": "<i>Hecho</i>", "parse_mode": "HTML"}))
    assert adapter.edited == [("123", "m1", "_Hecho_")]
    with pytest.raises(RuntimeError):
        asyncio.run(adapter_editor(Adapter(edits=False))(destination, "m1", {"text": "x"}))


def test_the_destination_may_be_any_gateway_platform_and_is_verified_from_its_own_chat(store):
    assert validate_config(active_config(destination=DISCORD)) == (True, [])
    ok, errors = validate_config(active_config(destination={**DISCORD, "platform": "Not A Platform"}))
    assert not ok and any("destination.platform" in e for e in errors)
    rt = SimpleNamespace(config=active_config(destination=DISCORD), store=store, clock=lambda: NOW)
    here = {"platform": "discord", "chat_id": "c9", "chat_type": "dm", "thread_id": "", "user_id": "u7"}
    assert verify_from_session(rt, here)
    row = store.destination("configured:primary")
    assert row["platform"] == "discord" and row["capabilities"]["buttons"] is False and row["address"] == {"chat_id": "c9"}
    elsewhere = verify_from_session(SimpleNamespace(config=active_config(destination=DISCORD), store=store, clock=lambda: NOW), {**here, "platform": "telegram"})
    assert elsewhere and store.destination("configured:primary")["platform"] == "discord"  # a chat on another platform verifies nothing


def test_a_written_reply_on_the_chosen_platform_answers_the_notice_and_one_from_elsewhere_does_not(store):
    cfg = active_config(destination=DISCORD)
    now = "2026-10-19T10:00:00Z"
    store.create_task(task_id="t1", kind="work_item", title="Decidir el alcance", summary="", entities={}, expectations=[], checklist=[], now=now)
    store.create_task_proposal(proposal_id="p1", task_id="t1", title="Restringir el acceso a un grupo", reason_text="", action_class="read", operation_id="web.read",
                               params={}, options=[], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="p1", now=now, batch_key="now")
    store.verify_destination(destination_id="configured:primary", platform="discord", gateway_connection_ref="discord:default",
                             address={"chat_id": "c9"}, owner_platform_id="u7", kind="private", capabilities={"text": True, "buttons": False}, now=now)
    adapter, transports = Adapter(), TransportRegistry()
    transports.register("discord", adapter_sender(adapter))
    dispatch(store, cfg, NOW)
    assert asyncio.run(Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW).tick())["delivered"] == 1
    chat_id, text, _ = adapter.sent[0]
    assert chat_id == "c9" and "<b>" not in text and "↩ " in text  # markdown, with the answers on their own line
    rt = SimpleNamespace(config=cfg, store=store, clock=lambda: NOW, transports=transports)

    def event(platform, text, user="u7"):
        return SimpleNamespace(source=SimpleNamespace(platform=platform, user_id=user, chat_id="c9"), reply_to_message_id="m1", text=text)

    assert asyncio.run(handle_text_reply(rt, event("telegram", "no"))) is None and asyncio.run(handle_text_reply(rt, event("discord", "no", user="other"))) is None
    assert store.get_proposal("p1")["status"] == "sent"
    handled = asyncio.run(handle_text_reply(rt, event("discord", "no")))
    assert handled == {"action": "skip", "reason": "radar reply handled: declined"} and store.get_proposal("p1")["status"] == "declined"
    assert len(adapter.sent) == 2  # the acknowledgement goes back on the same platform
