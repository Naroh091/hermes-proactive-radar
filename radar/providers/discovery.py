"""Onboarding discovery: classify the profile's MCP servers by the tools they expose.

Reads ``mcp_servers`` from the profile config and the host's MCP schema cache
(``$HERMES_HOME/cache/mcp_schema_cache.json``), so no server is spawned. A server missing from the
cache is reported as ``unknown`` and can be probed with ``hermes mcp test <name>``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .registry import FINGERPRINTS, classify_tools, kind_of


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # host dependency
    except ImportError:  # pragma: no cover - the host always ships PyYAML
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_mcp_servers(hermes_home: Path) -> dict[str, dict[str, Any]]:
    config = _read_yaml(hermes_home / "config.yaml")
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    return {str(name): entry for name, entry in servers.items() if isinstance(entry, dict)}


def load_schema_cache(hermes_home: Path) -> dict[str, list[str]]:
    path = hermes_home / "cache" / "mcp_schema_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    result: dict[str, list[str]] = {}
    if not isinstance(data, dict):
        return result
    for name, entry in data.items():
        tools = entry.get("tools") if isinstance(entry, dict) else None
        if isinstance(tools, list):
            result[str(name)] = [str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")]
    return result


def package_of(entry: dict[str, Any]) -> str:
    args = entry.get("args") or []
    for item in args:
        text = str(item)
        if text.startswith("@") or ("/" in text and not text.startswith("-")):
            return text
    return str(entry.get("command") or entry.get("url") or "")


def discover(hermes_home: Path) -> list[dict[str, Any]]:
    """One row per configured MCP server (tools seen, matched adapter, kind), then one per command-line connector
    found on this machine (see :func:`discover_cli`)."""
    servers = load_mcp_servers(hermes_home)
    cache = load_schema_cache(hermes_home)
    rows: list[dict[str, Any]] = []
    for name, entry in sorted(servers.items()):
        tools = cache.get(name, [])
        provider = classify_tools(tools) if tools else None
        rows.append(
            {
                "server": name,
                "package": package_of(entry),
                "enabled": entry.get("enabled", True) is not False,
                "tools": tools,
                "cached": bool(tools),
                "provider": provider,
                "kind": kind_of(provider),
                "label": FINGERPRINTS.get(provider or "", {}).get("label"),
            }
        )
    rows.extend(discover_cli(hermes_home))
    return rows


def discover_cli(hermes_home: Path, *, which=None) -> list[dict[str, Any]]:
    """Command-line connectors on this machine: ``gws`` (Google Workspace CLI) with the token the Hermes
    google-workspace skill keeps, that skill's own ``google_api.py`` wrapper when it is authorised, and ``gog``, whose
    mapping has to be derived first (``hermes radar connector derive gog``). Rows look like MCP rows, with ``cli``
    true, no server and a ``connector`` the source keeps."""
    import shutil

    from .cli import SCRIPT_RELATIVE, TOKEN_FILE

    which = which or shutil.which
    rows: list[dict[str, Any]] = []
    token = hermes_home / TOKEN_FILE
    gws = which("gws")
    if gws:
        for kind in ("gmail", "calendar"):
            rows.append({"server": "", "package": gws, "enabled": True, "tools": [], "cached": True, "provider": "gws", "kind": kind, "cli": True,
                         "label": "Google Workspace CLI (gws)", "ready": token.exists(), "connector": {"binary": gws, "token": str(token) if token.exists() else ""}})
    script = hermes_home.joinpath(*SCRIPT_RELATIVE)
    if script.exists() and token.exists():
        for kind in ("gmail", "calendar"):
            rows.append({"server": "", "package": str(script), "enabled": True, "tools": [], "cached": True, "provider": "hermes_google", "kind": kind, "cli": True,
                         "label": "Hermes google-workspace skill (google_api.py)", "ready": True, "connector": {"script": str(script)}})
    gog = which("gog")
    if gog:
        rows.append({"server": "", "package": gog, "enabled": True, "tools": [], "cached": True, "provider": "cli", "kind": "gmail", "cli": True,
                     "label": "gog (mapping to derive: hermes radar connector derive gog)", "ready": False, "connector": {"binary": gog}})
    return rows


def suggest_sources(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Default source entries for every recognised server or ready connector. Addresses are filled in by the user."""
    suggestions: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("provider") or not row.get("enabled"):
            continue
        if row.get("cli"):
            if not row.get("ready"):
                continue  # a mapping-driven tool is offered once its mapping exists
            ident = f"{row['provider']}-{row['kind']}"
            if row["kind"] == "gmail":
                suggestions.append({"id": ident, "kind": "gmail", "address": "", "provider": row["provider"], "server": "", "labels": ["INBOX"],
                                    "interval_seconds": 300, "enabled": True, "connector": dict(row.get("connector") or {}),
                                    "push": {"enabled": False, "topic": ""}})
            elif row["kind"] == "calendar":
                suggestions.append({"id": ident, "kind": "calendar", "provider": row["provider"], "server": "", "calendar_id": "primary",
                                    "interval_seconds": 600, "enabled": True, "connector": dict(row.get("connector") or {})})
            continue
        if row["kind"] == "gmail":
            suggestions.append(
                {
                    "id": row["server"], "kind": "gmail", "address": "", "provider": "gmail_mcp",
                    "server": row["server"], "labels": ["INBOX"], "interval_seconds": 300, "enabled": True,
                    "push": {"enabled": False, "topic": ""},
                }
            )
        elif row["kind"] == "calendar":
            suggestions.append(
                {
                    "id": row["server"], "kind": "calendar", "provider": "gcal_mcp", "server": row["server"],
                    "calendar_id": "primary", "interval_seconds": 600, "enabled": True,
                }
            )
    return suggestions
