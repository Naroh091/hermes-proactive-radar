from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_config
from .scheduler import Scheduler, sync_configured_sources


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def setup_cli(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="radar_action")
    actions.add_parser("status", help="Show scoped configuration and storage status")
    actions.add_parser("validate", help="Validate configuration without opening the database")
    actions.add_parser("run-once", help="Synchronize configured sources and run one scheduler cycle")
    actions.add_parser("discover", help="Classify the profile's MCP servers by the tools they expose")
    setup = actions.add_parser("setup", help="Interactive onboarding: trust level, model fallback, owner, sources, Gmail push")
    setup.add_argument("--yes", action="store_true", help="Accept defaults without prompting")
    fallback = actions.add_parser("fallback", help="Let radar retry with Hermes' fallback_providers when the main model fails: status (no argument) | on | off")
    fallback.add_argument("state", nargs="?", choices=["on", "off"], default=None)
    sync = actions.add_parser("sync", help="Sync one configured source now")
    sync.add_argument("source_id")
    replay = actions.add_parser("replay", help="Re-queue one signal; the gateway judges it again on its next tick")
    replay.add_argument("signal_id")
    tasks = actions.add_parser("tasks", help="List tracked tasks")
    tasks.add_argument("--all", action="store_true")
    task = actions.add_parser("task", help="Act on one task as the owner would from a notice: done, later, wait (--until), mute, unmute, reopen")
    task.add_argument("task_id")
    task.add_argument("task_action", choices=["done", "later", "wait", "mute", "unmute", "reopen"])
    task.add_argument("--until", default=None, help="For wait: ISO 8601 moment to wake the task up")
    explain = actions.add_parser("explain", help="Explain one task; --html writes a standalone report")
    explain.add_argument("task_id")
    explain.add_argument("--html", action="store_true")
    gmail = actions.add_parser("gmail", help="Gmail push helpers")
    gmail_sub = gmail.add_subparsers(dest="gmail_action")
    receiver = gmail_sub.add_parser("receiver", help="Run the push receiver standalone (foreground)")
    github = actions.add_parser("github", help="GitHub source helpers")
    github_sub = github.add_subparsers(dest="github_action")
    github_route = github_sub.add_parser("route", help="Install a radar-owned host webhook route for GitHub deliveries (no agent turn)")
    github_route.add_argument("name", nargs="?", default="github-radar")
    receiver.add_argument("--port", type=int)
    imp = actions.add_parser("import", help="Import data from another plugin")
    imp.add_argument("what", choices=["mail-intelligence"])
    imp.add_argument("--path", help="state.sqlite to import from")
    actions.add_parser("signals", help="Show recent signals and their triage")
    conn = actions.add_parser("connector", help="Command-line connectors (gws, the Hermes google-workspace script, any tool by mapping): test <source_id> | derive <command>")
    conn_sub = conn.add_subparsers(dest="connector_action")
    conn_test = conn_sub.add_parser("test", help="Run the conformance test of one configured source (search and read mail, or list events)")
    conn_test.add_argument("source_id")
    conn_derive = conn_sub.add_parser("derive", help="Work out with the model how a mail command maps onto Radar's operations, and test it")
    conn_derive.add_argument("command", help="The command to map, e.g. gog")
    conn_derive.add_argument("--source-id", default="", help="With --save: the id of the gmail source to write")
    conn_derive.add_argument("--address", default="", help="With --save: the mailbox address")
    conn_derive.add_argument("--save", action="store_true", help="Write the source into the plugin settings when the test passes")
    sim = actions.add_parser("simulate", help="Run a scenario (mails, events, chat turns from a YAML/JSON file) through the real pipeline against a scratch copy of the store, with the real model, and print what Radar would judge and say; nothing is sent")
    sim.add_argument("scenario", nargs="+", help="Scenario file(s); several run side by side, one host start-up for all")
    sim.add_argument("--fresh", action="store_true", help="Start from an empty store instead of a copy of the live one (no facts, habits or preferences)")
    sim.add_argument("--now", default="", help="Pretend it is this moment (ISO 8601); default: now")
    sim.add_argument("--json", action="store_true", help="Print the full report as JSON instead of the readable summary")
    warm = actions.add_parser("warmup", help="Learn the owner's habits from the last months of mail and calendar (runs inside the gateway): request it, or --status")
    warm.add_argument("--status", action="store_true", help="Show when the last pass ran and what it found")
    nxt = actions.add_parser("next", help="Launch the next notice now, ignoring quiet hours")
    nxt.add_argument("--dry-run", action="store_true", help="Only preview the next notice without sending it")


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str))


def command(ctx, args: argparse.Namespace) -> int:
    action = getattr(args, "radar_action", None)
    if not action:
        print("Usage: hermes radar {status|validate|run-once|discover|setup|fallback|sync|replay|tasks|task|explain|gmail|import|signals|next}")
        return 2
    try:
        if action == "discover":
            return _discover()
        if action == "setup":
            return _setup(ctx, yes=bool(getattr(args, "yes", False)))
        if action == "fallback":
            return _fallback(getattr(args, "state", None))
        cfg = load_config(ctx)
        if action == "validate":
            _print({"valid": True, "enabled": cfg["enabled"], "mode": cfg["mode"], "trust": cfg["trust"]["level"], "sources": [s["id"] for s in cfg["sources"]]})
            return 0
        if action == "connector":
            return _connector(ctx, cfg, args)
        if action == "simulate":
            from .simulate import run_files

            reports = asyncio.run(run_files(ctx, cfg, list(args.scenario), fresh=bool(args.fresh), now_text=args.now, hermes_home=str(_hermes_home())))
            if args.json:
                _print(reports[0] if len(reports) == 1 else reports)
            else:
                print("\n\n".join(report["summary"] for report in reports))
            failed = [report["name"] for report in reports if (report.get("expectations") or {}).get("failed")]
            if failed:
                print(f"\nExpectations not met in: {', '.join(failed)}", file=sys.stderr)
            return 1 if failed else 0
        if not cfg["enabled"]:
            if action == "status":
                _print({"enabled": False, "configured": False, "mode": cfg["mode"], "store": None})
                return 0
            _print({"error": "plugin_disabled", "hint": "set plugins.entries.radar.settings.enabled: true"})
            return 1
        from . import _runtime, _store

        store = _store(ctx)
        if action == "status":
            _print({"store": store.status(), "pending_signals": store.pending_signal_count(),
                    "sources": [{"id": s["id"], "kind": s["kind"], "health": s.get("health"), "cursor": s.get("cursor")} for s in store.list_sources(enabled_only=False)]})
            return 0
        if action == "run-once":
            synced = sync_configured_sources(store, cfg)
            runtime = _runtime(ctx)
            runtime.reload_sources()
            result = asyncio.run(runtime.scheduler.tick())
            _print({"synced": synced, "cycle": result})
            return 0
        if action == "sync":
            sync_configured_sources(store, cfg)
            runtime = _runtime(ctx)
            runtime.reload_sources()
            adapter = runtime.sources.get(args.source_id) or runtime.sources.get(f"configured:{args.source_id}")
            if adapter is None:
                _print({"error": "unknown_source", "available": sorted(runtime.sources)})
                return 1
            adapter.push_requested = True
            result = asyncio.run(runtime.sync_source(adapter, datetime.now(timezone.utc)))
            _print({"added": result.added, "seen": result.seen, "ignored": result.ignored, "error": result.error, "cursor": result.cursor})
            return 0 if not result.error else 1
        if action == "replay":
            # Re-queued for the gateway, not processed here: the gateway has the mail providers (to read a lost
            # body again), the memory provider and the audit; and processing in this process with a lease that
            # expired at once raced the gateway into judging the same signal twice (2026-11-03).
            now = datetime.now(timezone.utc)
            signal = store.get_signal(args.signal_id)
            with store.transaction() as db:
                db.execute(
                    "UPDATE signals SET triage_status='pending', next_attempt_at=?, lease_until=NULL, attempts=0 WHERE profile_key=? AND owner_id=? AND id=?",
                    (now.isoformat().replace("+00:00", "Z"), *store._scope_values, signal["id"]),
                )
            _print({"queued": signal["id"], "group": signal.get("group_key"), "title": signal.get("title"), "note": "the gateway processes it on its next tick"})
            return 0
        if action == "tasks":
            statuses = None if getattr(args, "all", False) else ("open", "waiting", "needs_user", "in_progress", "snoozed")
            rows = []
            for task in store.list_tasks(statuses=statuses, limit=100):
                proposals = store.list_proposals(statuses=("pending", "sent", "snoozed", "waiting", "executing"), task_id=task["id"], limit=10)
                rows.append({"id": task["id"], "kind": task["kind"], "status": task["status"], "title": task["title"],
                             "due_at": task.get("due_at"), "next_review_at": task.get("next_review_at"),
                             "missing": [c.get("item") for c in task.get("checklist") or [] if isinstance(c, dict) and c.get("status") == "missing"],
                             "proposals": [{"id": p["id"], "title": p["title"], "status": p["status"], "class": p["action_class"]} for p in proposals]})
            _print(rows)
            return 0
        if action == "task":
            from .delivery.responses import act_on_task

            full_id = next((t["id"] for t in store.list_tasks(limit=500) if t["id"] == args.task_id or t["id"].startswith(args.task_id)), None)
            if full_id is None:
                _print({"error": "unknown_task"})
                return 1
            _print(act_on_task(store, cfg, task_id=full_id, action=args.task_action, actor="cli", now_dt=datetime.now(timezone.utc),
                               until=getattr(args, "until", None)))
            return 0
        if action == "explain":
            from . import explain_tool

            print(explain_tool(ctx, {"task_id": args.task_id, "html": bool(getattr(args, "html", False))}))
            return 0
        if action == "signals":
            _print([{k: v for k, v in s.items() if k not in {"raw_content", "meta", "extract"}} for s in store.list_signals(limit=30)])
            return 0
        if action == "warmup":
            from .i18n import t
            from .warmup import describe, last_run, request

            language = cfg.get("owner", {}).get("language", "es")
            if getattr(args, "status", False):
                print(describe(last_run(store), language))
                return 0
            request(store, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), by="cli")
            print(t(language, "warmup_requested"))
            return 0
        if action == "next":
            from .delivery.preview import deliver_next, preview_next

            now_dt = datetime.now(timezone.utc)
            if getattr(args, "dry_run", False):
                _print(preview_next(store, cfg, now_dt))
            else:
                _print(deliver_next(store, cfg, now_dt))
            return 0
        if action == "github":
            if getattr(args, "github_action", None) == "route":
                _install_github_route(_hermes_home(), args.name)
                return 0
            print("Usage: hermes radar github route [name]")
            return 1
        if action == "gmail":
            return _gmail(ctx, cfg, args)
        if action == "import":
            return _import(ctx, cfg, args)
        print(f"Unknown radar action: {action}")
        return 2
    except Exception as exc:
        _print({"error": str(exc)})
        return 1


def _connector(ctx, cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """``hermes radar connector test <source>`` and ``derive <command>``: command-line connectors are the ones the
    CLI can exercise itself (no MCP client here)."""
    import shlex

    from .connectors import derive_mapping, check_source

    home = str(_hermes_home())
    sub = getattr(args, "connector_action", None)
    if sub == "test":
        source = next((s for s in cfg["sources"] if s.get("id") == args.source_id), None)
        if source is None:
            _print({"error": "unknown_source", "available": [s["id"] for s in cfg["sources"]]})
            return 1
        result = check_source(source, hermes_home=home)
        _print(result)
        return 0 if result.get("ok") else 1
    if sub == "derive":
        from . import _runtime

        gate = _runtime(ctx).gate
        if not getattr(gate, "available", False):
            _print({"error": "no_model", "hint": "the model works out the mapping from the tool's help; none is available here"})
            return 1
        try:
            derived = asyncio.run(derive_mapping(gate, args.command))
        except Exception as exc:
            _print({"error": "mapping_not_derived", "detail": str(exc)[:300]})
            return 1
        source = {"id": args.source_id or f"cli-{shlex.split(args.command)[0]}", "kind": "gmail", "address": args.address, "provider": "cli", "server": "",
                  "labels": ["INBOX"], "interval_seconds": 300, "enabled": True, "mapping": derived["mapping"],
                  "push": {"enabled": False, "topic": ""}}
        checks = check_source(source, hermes_home=home)
        _print({"source": source, "notes": derived["notes"], "help_read": derived["help_commands"], "test": checks})
        if args.save and checks.get("ok") and "@" in args.address:
            sources = [s for s in cfg["sources"] if s.get("id") != source["id"]] + [source]
            setter = getattr(ctx, "set_config", None)
            if callable(setter):
                setter("sources", sources)
                print("Saved into plugins.entries.radar.settings.sources; restart the gateway to watch it.")
            else:
                print("No host config access here: add the source above to plugins.entries.radar.settings.sources by hand.")
        elif args.save:
            print("Not saved: the test failed or --address is missing.")
        return 0 if checks.get("ok") else 1
    print("Usage: hermes radar connector test <source_id> | derive <command> [--source-id ID --address ADDR --save]")
    return 2


# -- discovery and setup --------------------------------------------------------------------------
def _discover() -> int:
    from .providers.discovery import discover

    rows = discover(_hermes_home())
    for row in rows:
        mark = row["provider"] or ("unknown" if row["cached"] else "not cached")
        print(f"{row['server']:<28} {row['kind'] or '-':<10} {mark:<12} {row['package']}")
    if not rows:
        print("No mcp_servers configured in this profile.")
    return 0


def _ask(prompt: str, default: str = "", *, yes: bool = False) -> str:
    if yes:
        return default
    try:
        answer = input(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    except EOFError:
        return default
    return answer or default


def _setup(ctx, *, yes: bool) -> int:
    from .providers.discovery import discover, suggest_sources
    from .trust import describe

    home = _hermes_home()
    cfg = load_config(ctx)
    from .trust import guide_text, level_summary

    print("Radar setup\n")
    language = _ask("Language (es | en)", cfg["owner"].get("language") or "es", yes=yes)
    print("\n" + guide_text(language) + "\n")
    level = _ask("Trust level (cautious | balanced | autonomous)", cfg["trust"]["level"], yes=yes)
    if level not in ("cautious", "balanced", "autonomous"):
        level = "balanced"
    print(f"  {level_summary(level, language)}")
    print(f"  ({', '.join(describe(level)['auto_classes'])} run alone; external asks)")
    want_fallback = _ask_fallback(yes=yes)
    learn = _ask("Learn your habits from the last 12 months of mail and calendar on the first start? (y/n)",
                 "y" if (cfg.get("warmup") or {}).get("enabled", True) else "n", yes=yes)
    owner_id = _ask("Owner id (any stable label)", cfg["owner"].get("id") or "owner", yes=yes)
    timezone_name = _ask("Timezone", cfg["owner"].get("timezone") or "UTC", yes=yes)
    quiet_start = _ask("Quiet hours start (HH:MM)", cfg["notifications"]["quiet_start"], yes=yes)
    quiet_end = _ask("Quiet hours end (HH:MM)", cfg["notifications"]["quiet_end"], yes=yes)
    brief_default = (cfg.get("brief") or {})
    brief_on = _ask("Morning brief with agenda, conflicts and replies owed at a fixed hour? (y/n)",
                    "y" if brief_default.get("enabled") else "n", yes=yes)
    brief_time = _ask("  brief time (HH:MM)", str(brief_default.get("time") or "07:00"), yes=yes) if brief_on.lower() == "y" else str(brief_default.get("time") or "07:00")
    platform = (_ask("Where should Radar write to you? A platform your gateway is connected to (telegram, discord, slack, whatsapp…)",
                     str(cfg["destination"].get("platform") or "telegram"), yes=yes) or "telegram").strip().lower()
    chat_id = _ask(f"{platform} chat id for notices (empty to skip)", str((cfg["destination"].get("address") or {}).get("chat_id") or ""), yes=yes)
    owner_platform_id = _ask(f"{platform} user id of the owner", str(cfg["destination"].get("owner_platform_id") or ""), yes=yes) if chat_id else ""

    rows = discover(home)
    existing = {s["id"]: s for s in cfg["sources"]}
    chosen: list[dict[str, Any]] = [s for s in cfg["sources"] if s.get("kind") == "http_status"]
    mcp_servers: list[str] = []
    print("\nMCP servers and command-line connectors:")
    for row in rows:
        name = row.get("server") or row.get("label") or row.get("provider") or "?"
        state = row["provider"] or ("unknown" if row["cached"] else "not cached")
        if row.get("cli"):
            state += " (ready)" if row.get("ready") else " (not ready: derive its mapping or authorise it first)"
        print(f"  {name:<44} {row['kind'] or '-':<10} {state}")
    for suggestion in suggest_sources(rows):
        current = existing.get(suggestion["id"], {})
        merged = {**suggestion, **{k: v for k, v in current.items() if k in suggestion}}
        via = suggestion.get("server") or f"{suggestion['provider']} (command-line)"
        if suggestion["kind"] == "gmail":
            use = _ask(f"Watch Gmail through {via}? (y/n)", "y" if current or yes else "n", yes=yes)
            if use.lower() != "y":
                continue
            merged["address"] = _ask(f"  email address of {via}", current.get("address", ""), yes=yes)
            if "@" not in merged["address"]:
                print("  skipped: an address is required")
                continue
            push = dict(current.get("push") or merged.get("push") or {})
            if not suggestion.get("server"):
                push["enabled"] = False  # a command-line connector polls
                merged["push"] = push
                chosen.append(merged)
                continue
            want_push = _ask("  enable Gmail push (Pub/Sub)? (y/n)", "y" if push.get("enabled") else "n", yes=yes)
            if want_push.lower() == "y":
                push["enabled"] = True
                push["topic"] = _ask("  Pub/Sub topic (projects/<id>/topics/<name>)", push.get("topic", ""), yes=yes)
                push.pop("credentials_path", None)  # radar only receives the push; the watch belongs to the mail integration
                if not push["topic"]:
                    push["enabled"] = False
            else:
                push["enabled"] = False
            merged["push"] = push
        elif suggestion["kind"] == "calendar":
            use = _ask(f"Watch calendar through {via}? (y/n)", "y" if current else "n", yes=yes)
            if use.lower() != "y":
                continue
        chosen.append(merged)
        if suggestion.get("server"):
            mcp_servers.append(suggestion["server"])

    settings = {
        "enabled": True, "mode": "active" if chat_id else cfg["mode"],
        "owner": {"id": owner_id, "timezone": timezone_name, "language": language},
        "trust": {**cfg["trust"], "level": level},
        "trust_level": level,
        "notifications": {**cfg["notifications"], "enabled": bool(chat_id), "quiet_start": quiet_start, "quiet_end": quiet_end},
        "brief": {"enabled": brief_on.lower() == "y", "time": brief_time},
        "observation": {**cfg["observation"], "local_sessions": True},
        "warmup": {**(cfg.get("warmup") or {}), "enabled": learn.strip().lower() == "y"},
        "sources": chosen,
    }
    if chat_id:
        settings["destination"] = {**cfg["destination"], "platform": platform, "gateway_connection_ref": f"{platform}:default",
                                   "address": {"chat_id": str(chat_id)}, "owner_platform_id": str(owner_platform_id)}
    any_push = any(s.get("kind") == "gmail" and (s.get("push") or {}).get("enabled") for s in chosen)
    if any_push:
        public_url = _ask("Public HTTPS URL that reaches the receiver (tunnel)", cfg["ingress"]["gmail_receiver"].get("public_url", ""), yes=yes)
        settings["ingress"] = {"gmail_receiver": {**cfg["ingress"]["gmail_receiver"], "enabled": True, "public_url": public_url}}
    from .schemas import validate_config
    from .config import _merge
    from .schemas import CONFIG_DEFAULTS

    merged_cfg = _merge(CONFIG_DEFAULTS, {**cfg, **settings})
    ok, errors = validate_config(merged_cfg)
    if not ok:
        _print({"error": "invalid settings", "details": errors})
        return 1
    setter = getattr(ctx, "set_config", None)
    if callable(setter):
        for key, value in settings.items():
            setter(key, value)
        print("\nSettings written to plugins.entries.radar.settings.")
    else:
        print("\nSettings (copy into plugins.entries.radar.settings):")
        _print(settings)
    _grant_host(mcp_servers, fallback=want_fallback)
    if any_push:
        _install_push_route(home, cfg["ingress"]["gmail_receiver"].get("route", "gmail-push"))
    print("\nNext: enable the plugin (plugins.enabled: [radar]), restart the gateway, then send /radar verify from the owner's chat.")
    return 0


def _host_config() -> tuple[Any, Any] | None:
    """The host's config loader and saver, or None outside Hermes."""
    try:
        from hermes_cli.config import load_config as host_load, save_config as host_save
    except ImportError:
        return None
    return host_load, host_save


FALLBACK_YAML = "plugins.entries.radar.llm: {allow_provider_override: true, allow_model_override: true}"


def _fallback_offer(config: dict[str, Any]) -> tuple[list[dict[str, str]], bool, str]:
    """``(host fallback entries, granted now, their names)`` for the prompt and the status output."""
    from .llm import fallback_entries, override_granted

    entries = fallback_entries(config.get("fallback_providers"))
    return entries, override_granted(config), ", ".join(f"{e['model']} via {e['provider']}" for e in entries)


def _ask_fallback(*, yes: bool) -> bool | None:
    """Onboarding question: retry with Hermes' fallback_providers when the main model fails? None = nothing to decide."""
    host = _host_config()
    if host is None:
        return None
    try:
        config = host[0]() or {}
    except Exception:
        return None
    entries, granted, names = _fallback_offer(config)
    print("\nModel fallback: radar's own calls (triage, planning, reminders) use the main model; Hermes only switches\n"
          "  provider for a few error kinds, and a spend limit is not one of them.")
    if not entries:
        print("  Hermes has no fallback_providers configured, so radar waits whenever the main model fails.\n"
              "  Set one (hermes model) and run `hermes radar fallback on` later.")
        return None
    state = "granted now" if granted else "not granted yet"
    answer = _ask(f"When the main model fails for any reason, let radar retry with {names}? ({state}) (y/n)", "y", yes=yes)
    return answer.lower() == "y"


def _grant_host(servers: list[str], *, fallback: bool | None = None) -> None:
    """One host config write for what the onboarding grants: the MCP allowlist and the model-fallback override."""
    from .llm import grant_override, override_granted

    if not servers and fallback is None:
        return
    host = _host_config()
    if host is None:
        if servers:
            print(f"Add to config.yaml: plugins.entries.radar.mcp_allowlist: {servers}")
        if fallback is not None:
            print(f"Add to config.yaml: {FALLBACK_YAML}" if fallback else "Leave plugins.entries.radar.llm unset")
        return
    host_load, host_save = host
    try:
        config = host_load() or {}
        entry = config.setdefault("plugins", {}).setdefault("entries", {}).setdefault("radar", {})
        changed: list[str] = []
        if servers:
            current = list(entry.get("mcp_allowlist") or [])
            for server in servers:
                if server not in current:
                    current.append(server)
            entry["mcp_allowlist"] = current
            changed.append(f"mcp_allowlist granted: {current}")
        if fallback is not None and fallback != override_granted(config):
            grant_override(config, fallback)
            changed.append("model fallback " + ("granted (plugins.entries.radar.llm)" if fallback else "revoked"))
        if changed:
            host_save(config, merge_existing=True)
            print("\n".join(changed))
    except Exception as exc:
        print(f"Could not write the host config ({exc}). Add plugins.entries.radar.mcp_allowlist: {servers}"
              + (f" and {FALLBACK_YAML}" if fallback else "") + " manually.")


def _fallback(state: str | None) -> int:
    """`hermes radar fallback [on|off]`: show or change whether radar may retry with the host's fallback_providers."""
    host = _host_config()
    if host is None:
        _print({"error": "not inside Hermes", "hint": f"add to config.yaml: {FALLBACK_YAML}"})
        return 1
    host_load, host_save = host
    config = host_load() or {}
    entries, granted, names = _fallback_offer(config)
    if state is None:
        _print({"granted": granted, "fallback_providers": entries,
                "note": "radar retries its own calls with these when the main model fails for any reason" if granted and entries
                else ("no fallback_providers in the host config: set one with `hermes model`" if not entries
                      else "not granted: run `hermes radar fallback on`")})
        return 0
    if state == "on" and not entries:
        _print({"error": "no fallback_providers in the host config", "hint": "set one with `hermes model` (or fallback_providers in config.yaml), then run this again"})
        return 1
    from .llm import grant_override

    grant_override(config, state == "on")
    host_save(config, merge_existing=True)
    _print({"granted": state == "on", "fallback_providers": entries,
            "note": (f"radar now retries with {names} when the main model fails; no restart needed, the grant is read per call"
                     if state == "on" else "radar waits when the main model fails; the grant is read per call, no restart needed")})
    return 0


def _install_github_route(home: Path, route: str) -> None:
    """How to create the host webhook route radar consumes. The route and its signing secret belong to the host:
    the owner creates it with the host's own command, and radar never opens the host's subscriptions file."""
    print(f"Create the webhook route with the host's own command (deliver: log, no prompt, so no agent turn):\n"
          f"  hermes webhook subscribe {route} --deliver log --description 'Radar: GitHub events'\n"
          f"On GitHub: Settings → Webhooks → payload URL <public webhook base>/webhooks/{route}, content type "
          f"application/json, secret = the one that command prints (never paste it in chat). Then add to the radar "
          f"source: route: {route}, consume: true.")


def _install_push_route(home: Path, route: str) -> None:
    """How to wire Gmail push. Both secrets live in the host's secret store (radar reads them through the host and
    never opens ``.env``); the route is created with the host's own command."""
    print(f"Gmail push needs a host webhook route and two secrets, all kept by the host:\n"
          f"  1. hermes webhook subscribe {route} --deliver log --description 'Radar: Gmail push'\n"
          f"  2. Save the secret that command prints as RADAR_GMAIL_PUSH_HERMES_SECRET, and any long random string as "
          f"RADAR_GMAIL_PUSH_TOKEN, in Radar's settings (Plugins → Radar) or in the secret store your host uses.\n"
          f"Pub/Sub push endpoint: <public_url>?token=<RADAR_GMAIL_PUSH_TOKEN> (OIDC required).")


# -- gmail helpers ----------------------------------------------------------------------------------
def _gmail(ctx, cfg: dict[str, Any], args: argparse.Namespace) -> int:
    sub = getattr(args, "gmail_action", None)
    if sub == "receiver":
        from . import _runtime

        runtime = _runtime(ctx)
        receiver_cfg = cfg["ingress"]["gmail_receiver"]
        if getattr(args, "port", None):
            receiver_cfg["port"] = args.port
        receiver_cfg["enabled"] = True

        async def run() -> None:
            started = await runtime.start_receiver()
            if not started:
                print("receiver not started: RADAR_GMAIL_PUSH_TOKEN and RADAR_GMAIL_PUSH_HERMES_SECRET are not set in the host's secret store")
                return
            print(f"receiver listening on {receiver_cfg['host']}:{receiver_cfg['port']} (Ctrl+C to stop)")
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                await runtime.receiver.stop()

        asyncio.run(run())
        return 0
    print("Usage: hermes radar gmail receiver [--port N]")
    return 2


# -- import -----------------------------------------------------------------------------------------
def _import(ctx, cfg: dict[str, Any], args: argparse.Namespace) -> int:
    from . import _store
    from .preferences import validate_preference

    path = Path(getattr(args, "path", None) or (_hermes_home() / "mail-intelligence" / "state.sqlite"))
    if not path.exists():
        _print({"error": f"not found: {path}"})
        return 1
    store = _store(ctx)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    imported = 0
    skipped = 0
    try:
        rows = db.execute(
            """SELECT p.payload FROM preferences p WHERE p.version=(
                 SELECT MAX(q.version) FROM preferences q WHERE q.profile=p.profile AND q.owner=p.owner AND q.id=p.id)"""
        ).fetchall()
    except sqlite3.Error as exc:
        _print({"error": f"cannot read preferences: {exc}"})
        return 1
    existing = {p["id"] for p in store.list_preferences()}
    for (payload,) in rows:
        try:
            data = json.loads(payload)
        except ValueError:
            skipped += 1
            continue
        if data.get("status") != "active" or data["id"] in existing:
            skipped += 1
            continue
        record = {
            "id": data["id"], "dimension": data.get("dimension", "notification"), "scope": data.get("scope") or {},
            "effect": data.get("effect", "silent"), "exceptions": data.get("exceptions") or [],
            "instruction": data.get("instruction") or data["id"], "origin": "imported", "status": "active",
        }
        try:
            validate_preference(record)
            store.save_preference(record, expected_version=0, now=now, feedback_ref=f"import:{path.name}")
            imported += 1
        except ValueError:
            skipped += 1
    db.close()
    _print({"imported": imported, "skipped": skipped, "source": str(path)})
    return 0
