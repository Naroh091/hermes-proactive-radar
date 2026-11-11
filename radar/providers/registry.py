"""Tool-fingerprint registry: which adapter understands a given MCP server; and which connectors run as a command."""
from __future__ import annotations

from typing import Any, Callable

from .base import MailProvider
from .generic_mcp import GenericMcpMailProvider
from .mail_gmail_mcp import GmailMcpProvider

FINGERPRINTS: dict[str, dict[str, Any]] = {
    "gmail_mcp": {
        "kind": "gmail", "label": "Gmail MCP (gmail-mcp family)",
        "required": {"search_emails", "read_email", "get_thread"},
    },
    "gcal_mcp": {
        "kind": "calendar", "label": "Google Calendar MCP (@cocal/google-calendar-mcp)",
        "required": {"list-events"}, "any": {"search-events", "get-event", "list-calendars"},
    },
    "github_mcp": {
        "kind": "github", "label": "GitHub MCP",
        "required": {"list_pull_requests"}, "any": {"get_pull_request", "list_commits", "search_issues"},
    },
}
# Providers that run a command instead of calling an MCP server (see providers/cli.py): no server, no MCP client.
CLI_MAIL_PROVIDERS = ("gws", "hermes_google", "cli")
CLI_CALENDAR_PROVIDERS = ("gws", "hermes_google")
MCP_MAIL_PROVIDERS = ("gmail_mcp", "generic_mcp")
MCP_CALENDAR_PROVIDERS = ("gcal_mcp",)


def classify_tools(tool_names: list[str] | set[str]) -> str | None:
    names = {str(name) for name in tool_names}
    for provider_id, spec in FINGERPRINTS.items():
        if spec["required"] <= names and (not spec.get("any") or spec["any"] & names):
            return provider_id
    return None


def kind_of(provider_id: str | None) -> str | None:
    spec = FINGERPRINTS.get(provider_id or "")
    return spec["kind"] if spec else None


def provider_of(source: dict[str, Any], default: str = "") -> str:
    """The provider id a source uses: its own, else the MCP default of its kind (or of the builder calling)."""
    by_kind = {"gmail": "gmail_mcp", "calendar": "gcal_mcp", "github": "gh"}.get(str(source.get("kind") or ""), "")
    return str(source.get("provider") or by_kind or default)


def needs_mcp(source: dict[str, Any]) -> bool:
    """True when the source talks to an MCP server (so it needs the host's MCP client); False for a command."""
    return provider_of(source) not in CLI_MAIL_PROVIDERS + CLI_CALENDAR_PROVIDERS


def _gws_client(source: dict[str, Any], hermes_home: str | None):
    from .cli import GwsClient, default_token

    connector = source.get("connector") if isinstance(source.get("connector"), dict) else {}
    return GwsClient(binary=str(connector.get("binary") or "gws"), token=str(connector.get("token") or default_token(hermes_home)))


def _script_client(source: dict[str, Any], hermes_home: str | None):
    from .cli import ScriptClient, default_script

    connector = source.get("connector") if isinstance(source.get("connector"), dict) else {}
    return ScriptClient(str(connector.get("script") or default_script(hermes_home)), python=str(connector.get("python") or ""))


def build_calendar_provider(call_mcp: Callable[..., Any] | None, source: dict[str, Any], *, allow_write: bool = False, timezone: str = "",
                            hermes_home: str | None = None):
    provider = provider_of(source, "gcal_mcp")
    if provider == "gws":
        from .cli import GwsCalendarProvider

        return GwsCalendarProvider(_gws_client(source, hermes_home), allow_write=allow_write, timezone_name=timezone)
    if provider == "hermes_google":
        from .cli import ScriptCalendarProvider

        return ScriptCalendarProvider(_script_client(source, hermes_home), allow_write=allow_write)
    from .calendar_gcal_mcp import GcalMcpProvider

    server = str(source.get("server") or "")
    if not server:
        raise ValueError(f"source {source.get('id')} has no MCP server")
    if not callable(call_mcp):
        raise ValueError(f"source {source.get('id')} needs the MCP client")
    if provider == "gcal_mcp":
        return GcalMcpProvider(call_mcp, server, allow_write=allow_write, account=str(source.get("account") or ""), timezone=timezone)
    raise ValueError(f"unknown calendar provider {provider}")


def build_mail_provider(call_mcp: Callable[..., Any] | None, source: dict[str, Any], *, allow_write: bool = False,
                        hermes_home: str | None = None) -> MailProvider:
    provider = provider_of(source, "gmail_mcp")
    if provider == "gws":
        from .cli import GwsMailProvider

        return GwsMailProvider(_gws_client(source, hermes_home), allow_write=allow_write)
    if provider == "hermes_google":
        from .cli import ScriptMailProvider

        return ScriptMailProvider(_script_client(source, hermes_home), allow_write=allow_write)
    if provider == "cli":
        from .cli import MappedCliMailProvider

        return MappedCliMailProvider(source.get("mapping") or {}, allow_write=allow_write)
    server = str(source.get("server") or "")
    if not server:
        raise ValueError(f"source {source.get('id')} has no MCP server")
    if not callable(call_mcp):
        raise ValueError(f"source {source.get('id')} needs the MCP client")
    if provider == "gmail_mcp":
        return GmailMcpProvider(call_mcp, server, allow_write=allow_write)
    if provider == "generic_mcp":
        return GenericMcpMailProvider(call_mcp, server, source.get("mapping") or {})
    raise ValueError(f"unknown mail provider {provider}")
