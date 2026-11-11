from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryReceipt:
    platform_message_id: str


Sender = Callable[[dict[str, Any], dict[str, Any]], Awaitable[DeliveryReceipt]]


class TransportRegistry:
    """Runtime-only platform senders; no SDK object is persisted."""

    def __init__(self) -> None:
        self._senders: dict[str, Sender] = {}
        self._editors: dict[str, Callable[..., Awaitable[None]]] = {}

    def register(self, platform: str, sender: Sender, editor: Callable[..., Awaitable[None]] | None = None) -> None:
        key = platform.strip().lower()
        if not key or not callable(sender):
            raise ValueError("platform and callable sender are required")
        self._senders[key] = sender
        if editor is not None:
            self._editors[key] = editor

    def unregister(self, platform: str) -> None:
        self._senders.pop(platform.strip().lower(), None)
        self._editors.pop(platform.strip().lower(), None)

    def available(self, platform: str) -> bool:
        return platform.strip().lower() in self._senders

    async def send(
        self, platform: str, destination: dict[str, Any], payload: dict[str, Any]
    ) -> DeliveryReceipt:
        sender = self._senders.get(platform.strip().lower())
        if sender is None:
            raise RuntimeError(f"transport unavailable: {platform}")
        return await sender(destination, payload)

    async def edit(self, platform: str, destination: dict[str, Any], message_id: str, payload: dict[str, Any]) -> bool:
        editor = self._editors.get(platform.strip().lower())
        if editor is None:
            return False
        await editor(destination, message_id, payload)
        return True


_TAG = re.compile(r"<[^>]+>")


def _plain(text: str) -> str:
    import html as _html

    return _html.unescape(_TAG.sub("", text or ""))


def _keyboard(buttons: Any) -> Any:
    if not buttons:
        return None
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        return None
    rows = []
    for row in buttons:
        if not isinstance(row, list):
            continue
        cells = [
            InlineKeyboardButton(str(cell["label"])[:40], callback_data=str(cell["data"])[:64])
            for cell in row if isinstance(cell, dict) and cell.get("label") and cell.get("data")
        ]
        if cells:
            rows.append(cells)
    return InlineKeyboardMarkup(rows) if rows else None


def _chat(destination: dict[str, Any], payload: dict[str, Any]) -> tuple[Any, Any]:
    address = destination.get("address") or {}
    chat_id = address.get("chat_id")
    if not isinstance(chat_id, (str, int)) or isinstance(chat_id, bool):
        raise ValueError("verified Telegram destination lacks chat_id")
    thread_id = payload.get("thread_id") or address.get("thread_id")
    try:
        thread_id = int(thread_id) if thread_id not in (None, "") else None
    except (TypeError, ValueError):
        thread_id = None
    return chat_id, thread_id


async def telegram_send(bot: Any, destination: dict[str, Any], payload: dict[str, Any]) -> DeliveryReceipt:
    chat_id, thread_id = _chat(destination, payload)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("notification text is required")
    kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    markup = _keyboard(payload.get("buttons"))
    if markup is not None:
        kwargs["reply_markup"] = markup
    parse_mode = payload.get("parse_mode")
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    try:
        message = await bot.send_message(**kwargs)
    except Exception as exc:
        if not parse_mode:
            raise
        log.warning("telegram rich send failed (%s); retrying as plain text", exc)
        kwargs.pop("parse_mode", None)
        kwargs["text"] = _plain(text)
        message = await bot.send_message(**kwargs)
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        raise RuntimeError("Telegram did not return a message id")
    document = payload.get("document")
    if isinstance(document, dict) and document.get("path"):
        try:
            with open(document["path"], "rb") as handle:
                doc_kwargs: dict[str, Any] = {
                    "chat_id": chat_id, "document": handle, "filename": document.get("filename") or "report.html",
                    "reply_to_message_id": message_id,
                }
                if thread_id is not None:
                    doc_kwargs["message_thread_id"] = thread_id
                if document.get("caption"):
                    doc_kwargs["caption"] = str(document["caption"])[:1000]
                await bot.send_document(**doc_kwargs)
        except Exception as exc:
            log.warning("telegram document send failed: %s", exc)
    return DeliveryReceipt(str(message_id))


async def telegram_edit(bot: Any, destination: dict[str, Any], message_id: str, payload: dict[str, Any]) -> None:
    chat_id, _ = _chat(destination, payload)
    kwargs: dict[str, Any] = {"chat_id": chat_id, "message_id": int(message_id), "text": payload.get("text") or "",
                              "disable_web_page_preview": True}
    markup = _keyboard(payload.get("buttons"))
    kwargs["reply_markup"] = markup
    if payload.get("parse_mode"):
        kwargs["parse_mode"] = payload["parse_mode"]
    try:
        await bot.edit_message_text(**kwargs)
    except Exception as exc:
        if not payload.get("parse_mode"):
            raise
        log.warning("telegram rich edit failed (%s); retrying as plain text", exc)
        kwargs.pop("parse_mode", None)
        kwargs["text"] = _plain(kwargs["text"])
        await bot.edit_message_text(**kwargs)


def telegram_sender(bot: Any) -> Sender:
    async def send(destination: dict[str, Any], payload: dict[str, Any]) -> DeliveryReceipt:
        return await telegram_send(bot, destination, payload)

    return send


def telegram_editor(bot: Any) -> Callable[..., Awaitable[None]]:
    async def edit(destination: dict[str, Any], message_id: str, payload: dict[str, Any]) -> None:
        await telegram_edit(bot, destination, message_id, payload)

    return edit


# -- any gateway platform ---------------------------------------------------------------------------
# Notices are composed once, in the small HTML subset Telegram accepts. Every other platform gets the same words as
# markdown through the host's own adapter (``BasePlatformAdapter.send``), which already knows how each platform
# formats, splits and threads a message.
_LINK = re.compile(r'<a\s+href="([^"]+)"\s*>(.*?)</a>', re.S | re.I)
_MARKS = (("b", "**"), ("strong", "**"), ("i", "_"), ("em", "_"), ("code", "`"), ("s", "~~"))


def to_markdown(text: str) -> str:
    """The notice's HTML as markdown: bold, italics, code, links and quotes survive; any other tag is dropped."""
    import html as _html

    out = _LINK.sub(lambda m: f"[{_TAG.sub('', m.group(2))}]({m.group(1)})", text or "")
    out = re.sub(r"<pre>(.*?)</pre>", lambda m: "```\n" + m.group(1).strip("\n") + "\n```", out, flags=re.S | re.I)
    out = re.sub(r"<blockquote[^>]*>(.*?)</blockquote>", lambda m: "\n".join("> " + line for line in m.group(1).strip().splitlines()), out, flags=re.S | re.I)
    for tag, mark in _MARKS:
        out = re.sub(rf"</?{tag}>", mark, out, flags=re.I)
    return _html.unescape(_TAG.sub("", out))


def choices_line(buttons: Any) -> str:
    """The answers a notice offers, for a platform that draws no buttons: their labels on one line. The owner answers
    in words (replying to the message), and the reply is understood against the same decisions the buttons stand for."""
    labels = []
    for row in buttons or []:
        for cell in row if isinstance(row, list) else []:
            label = str((cell or {}).get("label") or "").strip() if isinstance(cell, dict) else ""
            if label and label not in labels:
                labels.append(label)
    return ("↩ " + " · ".join(labels)) if labels else ""


def adapter_text(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("notification text is required")
    body = to_markdown(text) if payload.get("parse_mode") else text
    choices = choices_line(payload.get("buttons"))
    return f"{body}\n\n{choices}" if choices else body


def _address(destination: dict[str, Any], payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    address = destination.get("address") or {}
    chat_id = address.get("chat_id")
    if not isinstance(chat_id, (str, int)) or isinstance(chat_id, bool) or str(chat_id) == "":
        raise ValueError("verified destination lacks chat_id")
    thread_id = payload.get("thread_id") or address.get("thread_id")
    return str(chat_id), ({"thread_id": str(thread_id)} if thread_id not in (None, "") else None)


def adapter_sender(adapter: Any) -> Sender:
    """A sender over the host's adapter of any platform: text (and the report, where the platform takes files)."""
    async def send(destination: dict[str, Any], payload: dict[str, Any]) -> DeliveryReceipt:
        chat_id, metadata = _address(destination, payload)
        result = await adapter.send(chat_id, adapter_text(payload), metadata=metadata)
        if not getattr(result, "success", False):
            raise RuntimeError(f"{getattr(adapter, 'name', 'platform')} did not deliver: {str(getattr(result, 'error', '') or 'unknown error')[:200]}")
        message_id = getattr(result, "message_id", None)
        document = payload.get("document")
        if isinstance(document, dict) and document.get("path"):
            try:
                await adapter.send_document(chat_id, str(document["path"]), caption=str(document.get("caption") or "")[:1000] or None,
                                            file_name=document.get("filename") or "report.html", metadata=metadata)
            except Exception as exc:
                log.warning("document send failed: %s", exc)
        # A platform that returns no id still delivered; the notice just cannot be matched to a quoted reply later.
        return DeliveryReceipt(str(message_id) if message_id not in (None, "") else "")
    return send


def adapter_editor(adapter: Any) -> Callable[..., Awaitable[None]]:
    async def edit(destination: dict[str, Any], message_id: str, payload: dict[str, Any]) -> None:
        chat_id, _ = _address(destination, payload)
        result = await adapter.edit_message(chat_id, str(message_id), adapter_text(payload))
        if not getattr(result, "success", False):
            raise RuntimeError("the platform does not edit messages")
    return edit
