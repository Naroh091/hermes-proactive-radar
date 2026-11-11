"""Command-line connectors: working out how a tool maps onto Radar's mail operations, and proving a source works.

The owner (2026-11-06): let the onboarding offer the connector and "let the model work out how to apply the connection".
So ``derive_mapping`` reads the tool's help (the command and its subcommands, two levels deep), asks the model for the
mapping (slot «connector») and validates its shape; ``check_source`` then runs the conformance test (list recent mail
and read one; list a fortnight of events) that any connector, mapped or built in, must pass before Radar relies on
it. The mapping is saved in the source's config and never derived again at run time.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
from datetime import datetime
from typing import Any, Callable

from .pipeline.schemas import CONNECTOR_FIELDS, CONNECTOR_SCHEMA
from .prompts import CONNECTOR_PROMPT
from .providers.cli import conformance, validate_mapping
from .providers.registry import build_calendar_provider, build_mail_provider

HELP_TIMEOUT = 20
MAX_HELP_CHARS = 4000
MAX_HELP_COMMANDS = 24
MAX_INPUT_CHARS = 60000
_SECTION = re.compile(r"^\s*(?:available\s+)?(?:sub)?commands\s*:?\s*$", re.I)
_ENTRY = re.compile(r"^\s{1,8}([a-z][a-z0-9:_-]{1,30})(?:,\s*[a-z0-9:_-]+)?(?:\s{2,}.*|\s*)$")
_SKIP = {"help", "version", "completion", "completions", "update", "upgrade", "login", "logout", "auth", "config", "init", "setup"}


def _run_help(argv: list[str], *, runner: Callable[..., str] | None = None) -> str:
    if runner is not None:
        return runner(argv)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=HELP_TIMEOUT, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"(no help: {exc})"
    return (result.stdout or "") + ("\n" + result.stderr if result.stderr and not result.stdout else "")


def subcommands(help_text: str) -> list[str]:
    """The subcommand names a help text lists: the indented entries under a «Commands:» heading, else any indented
    word that looks like one. Flags and housekeeping commands are left out."""
    lines = help_text.splitlines()
    found: list[str] = []
    in_section = False
    for line in lines:
        if _SECTION.match(line):
            in_section = True
            continue
        if in_section and line and not line[0].isspace():
            in_section = False
        if in_section:
            match = _ENTRY.match(line)
            if match and match.group(1) not in _SKIP and match.group(1) not in found:
                found.append(match.group(1))
    if not found:
        for line in lines:
            match = _ENTRY.match(line)
            if match and not line.strip().startswith("-") and match.group(1) not in _SKIP and match.group(1) not in found:
                found.append(match.group(1))
    return found[:MAX_HELP_COMMANDS]


def collect_help(command: str, *, depth: int = 2, runner: Callable[..., str] | None = None) -> dict[str, str]:
    """The help of the command and of its subcommands down to ``depth`` levels, keyed by the command line."""
    base = shlex.split(command)
    if not base:
        raise ValueError("a command is required")
    texts: dict[str, str] = {}
    root = _run_help(base + ["--help"], runner=runner)
    texts[" ".join(base)] = root[:MAX_HELP_CHARS]
    queue: list[tuple[list[str], str, int]] = [(base, root, 1)]
    while queue and len(texts) < MAX_HELP_COMMANDS:
        argv, text, level = queue.pop(0)
        if level > depth:
            continue
        for name in subcommands(text):
            sub = argv + [name]
            key = " ".join(sub)
            if key in texts or len(texts) >= MAX_HELP_COMMANDS:
                continue
            sub_text = _run_help(sub + ["--help"], runner=runner)
            texts[key] = sub_text[:MAX_HELP_CHARS]
            queue.append((sub, sub_text, level + 1))
    return texts


async def derive_mapping(gate: Any, command: str, *, kind: str = "gmail", runner: Callable[..., str] | None = None) -> dict[str, Any]:
    """The model's mapping of ``command`` onto Radar's mail operations, validated; raises ValueError when it is
    unusable (no search or read). ``help_commands`` lists what was read."""
    texts = collect_help(command, runner=runner)
    payload = {"command": command, "kind": kind, "placeholders": ["{query}", "{max_results}", "{message_id}", "{thread_id}", "{body}", "{to}", "{subject}"],
               "fields": list(CONNECTOR_FIELDS), "help": texts}
    raw = await gate.structured("connector", instructions=CONNECTOR_PROMPT, input_text=json.dumps(payload, ensure_ascii=False)[:MAX_INPUT_CHARS],
                                schema=CONNECTOR_SCHEMA, purpose="radar.connector")
    if isinstance(raw, str):
        raw = json.loads(raw)
    mapping = validate_mapping(raw)
    return {"mapping": mapping, "notes": str((raw or {}).get("notes") or "") if isinstance(raw, dict) else "", "help_commands": list(texts)}


def check_source(source: dict[str, Any], *, hermes_home: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The conformance test of one configured source, through the provider the registry builds for it (no MCP
    client: only command-line connectors can be tested from the CLI)."""
    kind = str(source.get("kind") or "")
    try:
        if kind == "gmail":
            provider = build_mail_provider(None, source, hermes_home=hermes_home)
        elif kind == "calendar":
            provider = build_calendar_provider(None, source, hermes_home=hermes_home)
        else:
            return {"ok": False, "error": f"no conformance test for {kind or 'this kind'}"}
    except Exception as exc:
        return {"ok": False, "error": f"provider not built: {str(exc)[:200]}"}
    return {"source": source.get("id"), "provider": getattr(provider, "provider_id", source.get("provider")), **conformance(provider, kind, now=now)}
