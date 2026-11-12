"""Mail and calendar through a command-line tool instead of an MCP server.

The owner (2026-11-06): "we connect to mail through MCP, but some users may use gog or gws and we have to support
that too". Three profiles, all behind the same :class:`MailProvider` / :class:`CalendarProvider` interface the
sources and the native operations already use:

- ``gws``: Google's Workspace CLI, called directly (``gws gmail users messages list --params {...}``); full Gmail and
  Calendar API JSON, so nothing is lost (organizer, attendees, the owner's response). Credentials: gws's own login,
  or the file the owner sets as ``connector.token`` (``GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE``); never another tool's.
- ``hermes_google``: the Hermes skill's ``google_api.py`` wrapper (gws when installed, the Python client otherwise),
  with its fixed JSON contract; calendar events come without organizer or response.
- ``cli``: any other tool (``gog``…) through a mapping of Radar's operations onto its argv and JSON output, derived
  once with the model from the tool's help (``hermes radar connector derive``) and kept only if it passes the
  conformance test (``hermes radar connector test``).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Callable

from . import mcp_parsing as parse
from .base import CalendarProvider, MailMessage, MailProvider, ProviderError
from .calendar_gcal_mcp import normalise_event
from .env import tool_env

DEFAULT_TIMEOUT = 60
MAX_BODY = 20000
TOKEN_FILE = "google_token.json"
SCRIPT_RELATIVE = ("skills", "productivity", "google-workspace", "scripts", "google_api.py")
MAIL_PROFILES = ("gws", "hermes_google", "cli")
CALENDAR_PROFILES = ("gws", "hermes_google")


def run_cli(argv: list[str], *, env: dict[str, str] | None = None, timeout: float = DEFAULT_TIMEOUT, input_text: str | None = None) -> str:
    """Run a command and return its stdout; any failure is a :class:`ProviderError` with the tool's own words."""
    try:
        # Never the gateway's whole environment: a mail or calendar tool gets its own minimal one (see providers/env.py).
        env = tool_env() if env is None else env
        result = subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                                timeout=timeout, input=input_text, check=False)
    except FileNotFoundError as exc:
        raise ProviderError(f"{argv[0]}: not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"{argv[0]}: timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise ProviderError(f"{Path(argv[0]).name} exited {result.returncode}: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def json_out(text: str) -> Any:
    """The JSON a tool printed, tolerating a log line or two before it."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
        if starts:
            try:
                return json.loads(text[min(starts):])
            except ValueError:
                pass
        raise ProviderError(f"not JSON: {text[:120]}")


def default_script(hermes_home: str | Path | None) -> str:
    path = Path(hermes_home or "~/.hermes").expanduser().joinpath(*SCRIPT_RELATIVE)
    return str(path) if path.exists() else ""


# -- Gmail API JSON --------------------------------------------------------------------------------
def gmail_headers(msg: dict[str, Any]) -> dict[str, str]:
    return {str(h.get("name")).lower(): str(h.get("value") or "") for h in ((msg.get("payload") or {}).get("headers") or []) if isinstance(h, dict) and h.get("name")}


def _parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            found.append(part)
            found.extend(_parts(part))
    return found


def _decode(data: Any) -> str:
    try:
        return base64.urlsafe_b64decode(str(data) + "=" * (-len(str(data)) % 4)).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def gmail_body(msg: dict[str, Any]) -> str:
    """The text of a full-format message: its text/plain part, else its HTML converted to text."""
    payload = msg.get("payload") or {}
    if (payload.get("body") or {}).get("data"):
        text = _decode(payload["body"]["data"])
        return parse.html_to_text(text) if str(payload.get("mimeType") or "").startswith("text/html") or parse.looks_like_html(text) else text
    parts = _parts(payload)
    for part in parts:
        if part.get("mimeType") == "text/plain" and (part.get("body") or {}).get("data"):
            return _decode(part["body"]["data"])
    for part in parts:
        if part.get("mimeType") == "text/html" and (part.get("body") or {}).get("data"):
            return parse.html_to_text(_decode(part["body"]["data"]))
    return ""


def gmail_attachments(msg: dict[str, Any]) -> list[str]:
    return [str(p.get("filename")) for p in _parts(msg.get("payload") or {}) if p.get("filename")]


def mime_raw(*, to: str, subject: str, body: str, cc: str = "", in_reply_to: str = "", references: str = "") -> str:
    message = MIMEText(body)
    message["To"] = to
    message["Subject"] = subject
    if cc:
        message["Cc"] = cc
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = references or in_reply_to
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def _message(msg: dict[str, Any], *, message_id: str = "", body: str | None = None) -> MailMessage:
    headers = gmail_headers(msg)
    text = body if body is not None else ""
    return MailMessage(
        id=str(msg.get("id") or message_id), thread_id=str(msg.get("threadId") or ""), subject=headers.get("subject", ""),
        sender=headers.get("from", ""), to=headers.get("to", ""), date=headers.get("date", ""), epoch=parse.parse_date(headers.get("date", "")),
        snippet=" ".join((text or str(msg.get("snippet") or "")).split())[:500], body=text[:MAX_BODY],
        labels=[str(l) for l in msg.get("labelIds") or []], attachments=gmail_attachments(msg) if body is not None else [],
    )


class GwsClient:
    """One ``gws`` invocation per call, JSON in and out."""

    def __init__(self, *, binary: str = "gws", token: str = "", timeout: float = DEFAULT_TIMEOUT, runner: Callable[..., str] = run_cli):
        self.binary = binary or "gws"
        self.token = token
        self.timeout = timeout
        self.runner = runner

    def call(self, parts: list[str], *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> Any:
        argv = [self.binary, *parts]
        if params is not None:
            argv += ["--params", json.dumps(params)]
        if body is not None:
            argv += ["--json", json.dumps(body)]
        env = tool_env(("GOOGLE_WORKSPACE_CLI_",))
        if self.token:
            env["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = self.token
        return json_out(self.runner(argv, env=env, timeout=self.timeout))


class GwsMailProvider(MailProvider):
    provider_id = "gws"

    def __init__(self, client: GwsClient, *, allow_write: bool = False, user: str = "me"):
        self.client = client
        self.allow_write = allow_write
        self.user = user

    def _get(self, message_id: str, *, fmt: str, headers: list[str] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"userId": self.user, "id": message_id, "format": fmt}
        if headers:
            params["metadataHeaders"] = headers
        data = self.client.call(["gmail", "users", "messages", "get"], params=params)
        if not isinstance(data, dict) or not data.get("id"):
            raise ProviderError(f"unreadable message {message_id}")
        return data

    def search(self, query: str, *, max_results: int = 20) -> list[MailMessage]:
        listing = self.client.call(["gmail", "users", "messages", "list"],
                                   params={"userId": self.user, "q": query, "maxResults": max(1, min(int(max_results), 100))})
        found: list[MailMessage] = []
        for meta in (listing.get("messages") if isinstance(listing, dict) else None) or []:
            if not isinstance(meta, dict) or not meta.get("id"):
                continue
            msg = self._get(str(meta["id"]), fmt="metadata", headers=["From", "To", "Subject", "Date"])
            found.append(_message(msg, message_id=str(meta["id"])))
        return found

    def read(self, message_id: str) -> MailMessage:
        msg = self._get(message_id, fmt="full")
        return _message(msg, message_id=message_id, body=gmail_body(msg))

    def thread(self, thread_id: str, *, max_chars: int = 4000) -> str:
        data = self.client.call(["gmail", "users", "threads", "get"], params={"userId": self.user, "id": thread_id, "format": "full"})
        pieces = []
        for msg in (data.get("messages") if isinstance(data, dict) else None) or []:
            if isinstance(msg, dict):
                headers = gmail_headers(msg)
                pieces.append(f"From: {headers.get('from', '')}\nDate: {headers.get('date', '')}\n\n{gmail_body(msg)}".strip())
        return "\n\n---\n\n".join(pieces)[:max_chars]

    def _writable(self) -> None:
        if not self.allow_write:
            raise ProviderError("gws mail provider is read-only")

    def _sent(self, result: Any, how: str) -> dict[str, Any]:
        sent_id = str((result or {}).get("id") or "") if isinstance(result, dict) else ""
        return {"id": sent_id, "threadId": str((result or {}).get("threadId") or "") if isinstance(result, dict) else "",
                "result": f"{how} successfully with ID: {sent_id}" if sent_id else f"{how}: no id returned"}

    def send(self, *, to: list[str], subject: str, body: str) -> dict[str, Any]:
        self._writable()
        raw = mime_raw(to=", ".join(to), subject=subject, body=body)
        return self._sent(self.client.call(["gmail", "users", "messages", "send"], params={"userId": self.user}, body={"raw": raw}), "Email sent")

    def reply(self, message_id: str, body: str, *, reply_all: bool = False) -> dict[str, Any]:
        self._writable()
        original = self._get(message_id, fmt="metadata", headers=["From", "To", "Cc", "Subject", "Message-ID"])
        headers = gmail_headers(original)
        subject = headers.get("subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        cc = ", ".join(x for x in (headers.get("to", ""), headers.get("cc", "")) if x) if reply_all else ""
        raw = mime_raw(to=headers.get("from", ""), subject=subject, body=body, cc=cc, in_reply_to=headers.get("message-id", ""))
        result = self.client.call(["gmail", "users", "messages", "send"], params={"userId": self.user},
                                  body={"raw": raw, "threadId": str(original.get("threadId") or "")})
        return self._sent(result, "Email sent")

    def draft(self, *, to: list[str], subject: str, body: str, thread_id: str | None = None,
              in_reply_to: str | None = None) -> dict[str, Any]:
        self._writable()
        message: dict[str, Any] = {"raw": mime_raw(to=", ".join(to), subject=subject, body=body, in_reply_to=in_reply_to or "")}
        if thread_id:
            message["threadId"] = thread_id
        result = self.client.call(["gmail", "users", "drafts", "create"], params={"userId": self.user}, body={"message": message})
        draft_id = str((result or {}).get("id") or "") if isinstance(result, dict) else ""
        return {"id": draft_id, "result": f"Draft created with ID: {draft_id}" if draft_id else "Draft: no id returned"}

    def send_draft(self, draft_id: str) -> dict[str, Any]:
        self._writable()
        return self._sent(self.client.call(["gmail", "users", "drafts", "send"], params={"userId": self.user}, body={"id": draft_id}), "Email sent")

    def capabilities(self) -> dict[str, bool]:
        return {"search": True, "read": True, "thread": True, "draft": self.allow_write, "reply": self.allow_write,
                "send": self.allow_write, "send_draft": self.allow_write}


def rfc3339(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class GwsCalendarProvider(CalendarProvider):
    provider_id = "gws"

    def __init__(self, client: GwsClient, *, allow_write: bool = False, timezone_name: str = ""):
        self.client = client
        self.allow_write = allow_write
        self.timezone_name = timezone_name

    def list_events(self, *, time_min: str, time_max: str, calendar_id: str = "primary") -> list[dict[str, Any]]:
        data = self.client.call(["calendar", "events", "list"], params={
            "calendarId": calendar_id, "timeMin": rfc3339(time_min), "timeMax": rfc3339(time_max), "singleEvents": True,
            "orderBy": "startTime", "maxResults": 250,
        })
        items = (data.get("items") if isinstance(data, dict) else data) or []
        events = [normalise_event(item, calendar_id=calendar_id) for item in items if isinstance(item, dict)]
        return [event for event in events if event]

    def get_event(self, event_id: str, *, calendar_id: str = "primary") -> dict[str, Any]:
        data = self.client.call(["calendar", "events", "get"], params={"calendarId": calendar_id, "eventId": event_id})
        event = normalise_event(data, calendar_id=calendar_id) if isinstance(data, dict) else None
        if not event:
            raise ProviderError(f"event {event_id} not found")
        return event

    def create_event(self, *, summary: str, start: str, end: str, calendar_id: str = "primary", description: str = "",
                     location: str = "", attendees: list[str] | None = None) -> dict[str, Any]:
        if not self.allow_write:
            raise ProviderError("gws calendar provider is read-only")
        body: dict[str, Any] = {"summary": summary, "start": {"dateTime": rfc3339(start)}, "end": {"dateTime": rfc3339(end)}}
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        data = self.client.call(["calendar", "events", "insert"], params={"calendarId": calendar_id}, body=body)
        event = normalise_event(data, calendar_id=calendar_id) if isinstance(data, dict) else None
        if not event:
            raise ProviderError("event not created")
        return event

    def respond(self, event_id: str, response: str, *, calendar_id: str = "primary", comment: str = "") -> dict[str, Any]:
        """Accept, decline or tentatively answer an invitation: the owner's own attendee entry is patched."""
        if not self.allow_write:
            raise ProviderError("gws calendar provider is read-only")
        status = {"accept": "accepted", "accepted": "accepted", "decline": "declined", "declined": "declined",
                  "tentative": "tentative", "maybe": "tentative"}.get(str(response).lower())
        if not status:
            raise ProviderError(f"unknown response {response}")
        data = self.client.call(["calendar", "events", "get"], params={"calendarId": calendar_id, "eventId": event_id})
        attendees = [dict(a) for a in ((data or {}).get("attendees") if isinstance(data, dict) else None) or [] if isinstance(a, dict)]
        mine = [a for a in attendees if a.get("self")]
        if not mine:
            raise ProviderError("the owner is not an attendee of this event")
        for attendee in mine:
            attendee["responseStatus"] = status
            if comment:
                attendee["comment"] = comment
        patched = self.client.call(["calendar", "events", "patch"], params={"calendarId": calendar_id, "eventId": event_id}, body={"attendees": attendees})
        event = normalise_event(patched, calendar_id=calendar_id) if isinstance(patched, dict) else None
        return {"event": event or {}, "result": f"Responded {status} to event {event_id}"}


# -- the Hermes google-workspace skill's wrapper ---------------------------------------------------
class ScriptClient:
    def __init__(self, script: str, *, python: str = "", timeout: float = DEFAULT_TIMEOUT, runner: Callable[..., str] = run_cli):
        if not script:
            raise ValueError("hermes_google needs the google_api.py script path")
        self.script = script
        self.python = python or sys.executable
        self.timeout = timeout
        self.runner = runner

    def call(self, *args: str) -> Any:
        # The skill's own wrapper: it needs to find Hermes' home, nothing else of the gateway's environment.
        return json_out(self.runner([self.python, self.script, *args], env=tool_env(("HERMES_",)), timeout=self.timeout))


class ScriptMailProvider(MailProvider):
    provider_id = "hermes_google"

    def __init__(self, client: ScriptClient, *, allow_write: bool = False):
        self.client = client
        self.allow_write = allow_write

    @staticmethod
    def _from(item: dict[str, Any], *, body: str | None = None) -> MailMessage:
        text = body or ""
        return MailMessage(
            id=str(item.get("id") or ""), thread_id=str(item.get("threadId") or ""), subject=str(item.get("subject") or ""),
            sender=str(item.get("from") or ""), to=str(item.get("to") or ""), date=str(item.get("date") or ""),
            epoch=parse.parse_date(str(item.get("date") or "")), snippet=" ".join((text or str(item.get("snippet") or "")).split())[:500],
            body=text[:MAX_BODY], labels=[str(l) for l in item.get("labels") or []],
        )

    def search(self, query: str, *, max_results: int = 20) -> list[MailMessage]:
        data = self.client.call("gmail", "search", query, "--max", str(max(1, min(int(max_results), 100))))
        return [self._from(item) for item in (data if isinstance(data, list) else []) if isinstance(item, dict) and item.get("id")]

    def read(self, message_id: str) -> MailMessage:
        data = self.client.call("gmail", "get", message_id)
        if not isinstance(data, dict) or not data.get("id"):
            raise ProviderError(f"unreadable message {message_id}")
        body = str(data.get("body") or "")
        return self._from(data, body=parse.html_to_text(body) if parse.looks_like_html(body) else body)

    def thread(self, thread_id: str, *, max_chars: int = 4000) -> str:
        raise ProviderError("hermes_google does not read threads")

    def reply(self, message_id: str, body: str, *, reply_all: bool = False) -> dict[str, Any]:
        if not self.allow_write:
            raise ProviderError("hermes_google mail provider is read-only")
        data = self.client.call("gmail", "reply", message_id, "--body", body)
        sent_id = str((data or {}).get("id") or "") if isinstance(data, dict) else ""
        return {"id": sent_id, "result": f"Email sent successfully with ID: {sent_id}" if sent_id else "Email sent: no id returned"}

    def send(self, *, to: list[str], subject: str, body: str) -> dict[str, Any]:
        if not self.allow_write:
            raise ProviderError("hermes_google mail provider is read-only")
        data = self.client.call("gmail", "send", "--to", ", ".join(to), "--subject", subject, "--body", body)
        sent_id = str((data or {}).get("id") or "") if isinstance(data, dict) else ""
        return {"id": sent_id, "result": f"Email sent successfully with ID: {sent_id}" if sent_id else "Email sent: no id returned"}

    def capabilities(self) -> dict[str, bool]:
        return {"search": True, "read": True, "thread": False, "draft": False, "reply": self.allow_write, "send": self.allow_write, "send_draft": False}


class ScriptCalendarProvider(CalendarProvider):
    provider_id = "hermes_google"

    def __init__(self, client: ScriptClient, *, allow_write: bool = False):
        self.client = client
        self.allow_write = allow_write

    def list_events(self, *, time_min: str, time_max: str, calendar_id: str = "primary") -> list[dict[str, Any]]:
        data = self.client.call("calendar", "list", "--start", rfc3339(time_min), "--end", rfc3339(time_max), "--max", "250", "--calendar", calendar_id)
        events = [normalise_event(item, calendar_id=calendar_id) for item in (data if isinstance(data, list) else []) if isinstance(item, dict)]
        return [event for event in events if event]

    def get_event(self, event_id: str, *, calendar_id: str = "primary") -> dict[str, Any]:
        raise ProviderError("hermes_google does not read one event")


# -- any other tool, by mapping ----------------------------------------------------------------------
FIELD_NAMES = ("id", "thread_id", "subject", "sender", "to", "date", "snippet", "body")


def _format(template: Any, params: dict[str, Any]) -> str:
    text = str(template)
    try:
        return text.format(**params)
    except (KeyError, IndexError, ValueError):
        return text


def _pick(data: Any, path: str) -> Any:
    for key in [p for p in str(path or "").split(".") if p]:
        if isinstance(data, dict):
            data = data.get(key)
        else:
            return None
    return data


class MappedCliMailProvider(MailProvider):
    """Radar's mail operations on any command: each operation names the argv (with ``{query}``, ``{max_results}``,
    ``{message_id}``, ``{thread_id}``, ``{body}``, ``{to}``, ``{subject}`` placeholders), where the items sit in the
    JSON the tool prints (``items``, a dotted path or empty for the root) and which keys carry each field."""

    provider_id = "cli"

    def __init__(self, mapping: dict[str, Any], *, allow_write: bool = False, timeout: float = DEFAULT_TIMEOUT, runner: Callable[..., str] = run_cli):
        if not isinstance(mapping, dict) or not _argv(mapping.get("search")) or not _argv(mapping.get("read")):
            raise ValueError("cli mapping requires search and read operations with argv")
        self.mapping = mapping
        self.allow_write = allow_write
        self.timeout = timeout
        self.runner = runner

    def _invoke(self, operation: str, **params: Any) -> tuple[Any, dict[str, Any]]:
        spec = self.mapping.get(operation) if isinstance(self.mapping.get(operation), dict) else {}
        argv = [_format(part, params) for part in _argv(spec)]
        if not argv:
            raise ProviderError(f"operation {operation} is not mapped")
        return json_out(self.runner(argv, timeout=self.timeout)), spec

    @staticmethod
    def _items(data: Any, spec: dict[str, Any]) -> list[dict[str, Any]]:
        inner = _pick(data, str(spec.get("items") or ""))
        if isinstance(inner, dict):
            inner = [inner]
        return [item for item in (inner or []) if isinstance(item, dict)]

    @staticmethod
    def _message(item: dict[str, Any], spec: dict[str, Any], *, with_body: bool) -> MailMessage:
        fields = spec.get("fields") if isinstance(spec.get("fields"), dict) else {}

        def get(name: str) -> str:
            key = str(fields.get(name) or name)
            value = _pick(item, key)
            return "" if value is None else (", ".join(str(v) for v in value) if isinstance(value, list) else str(value))

        body = get("body") if with_body else ""
        if with_body and parse.looks_like_html(body):
            body = parse.html_to_text(body)
        return MailMessage(id=get("id"), thread_id=get("thread_id"), subject=get("subject"), sender=get("sender"), to=get("to"), date=get("date"),
                           epoch=parse.parse_date(get("date")), snippet=" ".join((body or get("snippet")).split())[:500], body=body[:MAX_BODY])

    def search(self, query: str, *, max_results: int = 20) -> list[MailMessage]:
        data, spec = self._invoke("search", query=query, max_results=max(1, int(max_results)))
        return [m for m in (self._message(item, spec, with_body=False) for item in self._items(data, spec)) if m.id]

    def read(self, message_id: str) -> MailMessage:
        data, spec = self._invoke("read", message_id=message_id)
        items = self._items(data, spec)
        if not items:
            raise ProviderError(f"unreadable message {message_id}")
        message = self._message(items[0], spec, with_body=True)
        return message if message.id else MailMessage(id=message_id, subject=message.subject, sender=message.sender, to=message.to, date=message.date,
                                                      epoch=message.epoch, snippet=message.snippet, body=message.body)

    def thread(self, thread_id: str, *, max_chars: int = 4000) -> str:
        if not _argv(self.mapping.get("thread")):
            raise ProviderError("thread is not mapped")
        data, spec = self._invoke("thread", thread_id=thread_id)
        pieces = [f"From: {m.sender}\nDate: {m.date}\n\n{m.body}" for m in (self._message(i, spec, with_body=True) for i in self._items(data, spec))]
        return "\n\n---\n\n".join(pieces)[:max_chars]

    def _write(self, operation: str, **params: Any) -> dict[str, Any]:
        if not self.allow_write:
            raise ProviderError("cli mail provider is read-only")
        if not _argv(self.mapping.get(operation)):
            raise ProviderError(f"{operation} is not mapped")
        data, spec = self._invoke(operation, **params)
        fields = spec.get("fields") if isinstance(spec.get("fields"), dict) else {}
        sent_id = str(_pick(data, str(fields.get("id") or "id")) or "") if isinstance(data, dict) else ""
        return {"id": sent_id, "result": f"Email sent successfully with ID: {sent_id}" if sent_id else "Email sent"}

    def reply(self, message_id: str, body: str, *, reply_all: bool = False) -> dict[str, Any]:
        return self._write("reply", message_id=message_id, body=body)

    def send(self, *, to: list[str], subject: str, body: str) -> dict[str, Any]:
        return self._write("send", to=", ".join(to), subject=subject, body=body)

    def capabilities(self) -> dict[str, bool]:
        return {"search": True, "read": True, "thread": bool(_argv(self.mapping.get("thread"))), "draft": False,
                "reply": self.allow_write and bool(_argv(self.mapping.get("reply"))), "send": self.allow_write and bool(_argv(self.mapping.get("send"))),
                "send_draft": False}


def _argv(spec: Any) -> list[str]:
    if not isinstance(spec, dict):
        return []
    argv = spec.get("argv")
    return [str(a) for a in argv] if isinstance(argv, list) and argv else []


def validate_mapping(raw: Any) -> dict[str, Any]:
    """A mapping as the model or a human wrote it, reduced to what the provider reads; raises ValueError when search
    or read are missing."""
    if not isinstance(raw, dict):
        raise ValueError("mapping must be an object")
    out: dict[str, Any] = {}
    for operation in ("search", "read", "thread", "reply", "send"):
        spec = raw.get(operation)
        if not isinstance(spec, dict) or not _argv(spec):
            continue
        fields = {k: str(v) for k, v in (spec.get("fields") or {}).items() if k in FIELD_NAMES and isinstance(v, str) and v.strip()} if isinstance(spec.get("fields"), dict) else {}
        out[operation] = {"argv": _argv(spec), "items": str(spec.get("items") or ""), "fields": fields}
    if "search" not in out or "read" not in out:
        raise ValueError("mapping requires search and read with argv")
    if "{query}" not in " ".join(out["search"]["argv"]):
        raise ValueError("search argv must carry {query}")
    if "{message_id}" not in " ".join(out["read"]["argv"]):
        raise ValueError("read argv must carry {message_id}")
    return out


# -- conformance -------------------------------------------------------------------------------------
def conformance(provider: Any, kind: str, *, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    """What a connector must do before Radar relies on it: list recent mail and read one, or list a fortnight of
    events. Every check reports ok or the tool's error; ``ok`` sums them up."""
    checks: dict[str, Any] = {}
    now = now or datetime.now(timezone.utc)
    if kind == "gmail":
        found: list[Any] = []
        try:
            found = provider.search(f"newer_than:{days}d", max_results=3)
            checks["search"] = {"ok": True, "count": len(found)}
        except Exception as exc:
            checks["search"] = {"ok": False, "error": str(exc)[:200]}
        if found:
            try:
                message = provider.read(found[0].id)
                checks["read"] = {"ok": bool(message.subject or message.body), "subject": (message.subject or "")[:80], "chars": len(message.body or "")}
            except Exception as exc:
                checks["read"] = {"ok": False, "error": str(exc)[:200]}
    elif kind == "calendar":
        try:
            events = provider.list_events(time_min=rfc3339((now - timedelta(days=7)).isoformat()), time_max=rfc3339((now + timedelta(days=7)).isoformat()))
            checks["list_events"] = {"ok": True, "count": len(events), "titles": [e.get("title", "")[:40] for e in events[:3]]}
        except Exception as exc:
            checks["list_events"] = {"ok": False, "error": str(exc)[:200]}
    else:
        checks["kind"] = {"ok": False, "error": f"no conformance test for {kind}"}
    checks["ok"] = all(c.get("ok") for c in checks.values() if isinstance(c, dict))
    return checks
