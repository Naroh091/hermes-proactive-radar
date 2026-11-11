"""Parsers for the text that real MCP mail servers return (ported from mail-intelligence).

The host wraps every MCP call as ``{"ok": bool, "result": ..., "truncated"?: True}`` and caps the raw
handler JSON at about 64 KB. These helpers unwrap that envelope, recover a payload cut mid-string and
parse the rendered ``search_emails`` / ``read_email`` output of the gmail-mcp family.
"""
from __future__ import annotations

import json
import re
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any


def payload_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (str, bytes)):
        try:
            value = json.loads(payload)
        except ValueError:
            return {}
        if isinstance(value, dict):
            return value
    return {}


def unwrap(payload: Any) -> Any:
    """Unwrap the host MCP envelope. MCP errors raise instead of looking empty."""
    data = payload_dict(payload)
    if isinstance(data.get("ok"), bool) and not data["ok"]:
        raise ValueError(f"mcp error: {str(data.get('error'))[:200]}")
    inner = data.get("result", data)
    if "structuredContent" in data and not isinstance(inner, (dict, list)):
        inner = data["structuredContent"]
    return inner


_SEARCH_BLOCK = re.compile(
    r"^ID:\s*(?P<id>.+?)\s*\nSubject:\s*(?P<subject>.*?)\s*\nFrom:\s*(?P<from>.*?)\s*\nDate:\s*(?P<date>.*?)\s*(?:\n|$)",
    re.MULTILINE,
)
_READ_HEAD = re.compile(
    r"^Thread ID:\s*(?P<thread>.*?)\s*\nMessage-ID:.*?\nSubject:\s*(?P<subject>.*?)\s*\nFrom:\s*(?P<from>.*?)\s*\nTo:.*?\nDate:\s*(?P<date>.*?)\s*\n\n(?P<body>.*)$",
    re.MULTILINE | re.DOTALL,
)
_TRUNCATED_HANDLER = re.compile(r'^\s*\{\s*"result"\s*:\s*"')
_TRUNCATION_MARK = "… [truncated]"
_JSON_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}


def header_field(text: str, name: str) -> str:
    match = re.search(rf"(?im)^{re.escape(name)}:\s*(.*)$", text or "")
    return match.group(1).strip() if match else ""


def _unescape_json_string(text: str) -> str:
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            nxt = text[index + 1]
            if nxt in _JSON_ESCAPES:
                out.append(_JSON_ESCAPES[nxt])
                index += 2
                continue
            if nxt == "u" and index + 6 <= length:
                try:
                    out.append(chr(int(text[index + 2:index + 6], 16)))
                    index += 6
                    continue
                except ValueError:
                    pass
        out.append(char)
        index += 1
    return "".join(out)


def recover_truncated_json_string(text: str) -> str:
    """Recover usable text from a host-truncated handler payload (``{"result": "Thread ID: ...``)."""
    match = _TRUNCATED_HANDLER.match(text)
    if not match:
        return text
    body = text[match.end():]
    if body.endswith(_TRUNCATION_MARK):
        body = body[: -len(_TRUNCATION_MARK)]
    if body.endswith('"}'):
        body = body[:-2]
    elif body.endswith('"'):
        body = body[:-1]
    return _unescape_json_string(body)


def content_texts(payload: Any) -> str:
    """Text carried by MCP content blocks (search/read/thread), or the recovered truncated string."""
    inner = unwrap(payload)
    if isinstance(inner, str):
        return recover_truncated_json_string(inner)
    if isinstance(inner, dict):
        texts = []
        for item in inner.get("content") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        if texts:
            return "\n".join(texts)
        if isinstance(inner.get("structuredContent"), str):
            return inner["structuredContent"]
        if isinstance(inner.get("text"), str):
            return inner["text"]
    if isinstance(inner, list):
        texts = [item.get("text") for item in inner if isinstance(item, dict) and isinstance(item.get("text"), str)]
        return "\n".join(texts)
    return ""


def parse_date(value: str) -> float | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    try:
        return parsed.timestamp()
    except (OverflowError, ValueError):
        return None


def parse_search_blocks(text: str) -> list[dict[str, Any]]:
    found = []
    for match in _SEARCH_BLOCK.finditer(text or ""):
        if match.group("id").strip():
            date = match.group("date").strip()
            found.append(
                {
                    "id": match.group("id").strip(),
                    "subject": match.group("subject").strip(),
                    "from": match.group("from").strip(),
                    "date": date,
                    "epoch": parse_date(date),
                }
            )
    return found


def parse_read_text(text: str) -> dict[str, Any]:
    match = _READ_HEAD.match(text or "")
    if match:
        return {
            "thread_id": match.group("thread").strip(),
            "subject": match.group("subject").strip(),
            "sender": match.group("from").strip(),
            "date": match.group("date").strip(),
            "to": header_field(text, "To"),
            "body": match.group("body").strip(),
        }
    text = text or ""
    if not text.strip():
        return {}
    return {
        "thread_id": header_field(text, "Thread ID"),
        "subject": header_field(text, "Subject"),
        "sender": header_field(text, "From"),
        "date": header_field(text, "Date"),
        "to": header_field(text, "To"),
        "body": text.strip(),
    }


def parse_thread_text(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text or "")
    except ValueError:
        return []
    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list):
            return [m for m in messages if isinstance(m, dict)]
    return []


def message_id(item: dict[str, Any]) -> str:
    for key in ("id", "gmail_id", "message_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# -- HTML-only mail ------------------------------------------------------------------------------
# A booking confirmation often has no text part at all; the gmail-mcp family then returns the raw HTML
# behind a note. On 2026-10-28 a car rental reached the model as 500 characters of <!DOCTYPE html><style>…
_HTML_NOTE = re.compile(r"\[Note:\s*This email is HTML-formatted[^\]]*\]\s*", re.I)
_HTML_MARK = re.compile(r"<\s*(?:!doctype\s+html|html|body|table|div|p|br|span)\b", re.I)
_SKIP_TAGS = frozenset({"script", "style", "head", "title", "noscript", "template"})
_BLOCK_TAGS = frozenset({"p", "div", "br", "hr", "tr", "li", "ul", "ol", "table", "section", "article", "header", "footer",
                         "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"})
_CELL_TAGS = frozenset({"td", "th"})


def looks_like_html(text: str) -> bool:
    """True for a mail body that is an HTML document (the gmail-mcp note, or a document tag near the start)."""
    head = str(text or "")[:400]
    return bool(_HTML_NOTE.search(head) or _HTML_MARK.search(head))


class _TextOfHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skipping += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in _CELL_TAGS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in _CELL_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(text: str) -> str:
    """The readable text of an HTML mail: no markup, scripts or styles; one line per block, table cells
    separated by spaces, entities decoded, blank runs collapsed; the gmail-mcp note dropped."""
    parser = _TextOfHtml()
    try:
        parser.feed(_HTML_NOTE.sub("", str(text or ""), count=1))
        parser.close()
    except Exception:  # a malformed document still yields what was parsed before the fault
        pass
    lines = [" ".join(line.split()) for line in "".join(parser.parts).replace("\xa0", " ").splitlines()]
    return "\n".join(line for line in lines if line)


def automation_signals(sender: str, headers: dict[str, str] | None = None) -> list[str]:
    """Technical automation signals, never a reason by themselves to suppress."""
    signals: list[str] = []
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if headers.get("list-unsubscribe"):
        signals.append("list-unsubscribe")
    precedence = headers.get("precedence", "").lower()
    if precedence in ("bulk", "list", "junk"):
        signals.append("precedence:" + precedence)
    auto = headers.get("auto-submitted", "").lower()
    if auto and auto != "no":
        signals.append("auto-submitted:" + auto)
    if re.search(r"noreply|no-reply|donotreply|do-not-reply|mailer-daemon|notifications?@", sender or "", re.I):
        signals.append("noreply-sender")
    return signals
