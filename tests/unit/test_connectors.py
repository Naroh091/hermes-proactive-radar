"""Mail and calendar through a command-line tool: gws called directly, the Hermes Google script, or any tool by a
mapping the model derives from its help and the conformance test confirms."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import sys
from pathlib import Path

from conftest import NOW, FakeContext, FakeLlm, config

from radar.connectors import collect_help, derive_mapping, subcommands, check_source
from radar.llm import LlmGate
from radar.providers.cli import GwsCalendarProvider, GwsClient, GwsMailProvider, MappedCliMailProvider, ProviderError, conformance, validate_mapping
from radar.providers.discovery import discover_cli, suggest_sources
from radar.providers.registry import build_calendar_provider, build_mail_provider, needs_mcp
from radar.runtime import Runtime
from radar.scheduler import sync_configured_sources
from radar.schemas import validate_config

FAKE_GWS = r'''#!%(python)s
import base64, json, os, sys
argv = sys.argv[1:]
parts, params, body = [], None, None
i = 0
while i < len(argv):
    if argv[i] == "--params":
        params = json.loads(argv[i + 1]); i += 2
    elif argv[i] == "--json":
        body = json.loads(argv[i + 1]); i += 2
    else:
        parts.append(argv[i]); i += 1
with open(os.environ["GOOGLE_WORKSPACE_CLI_FAKE_LOG"], "a") as log:
    log.write(json.dumps({"parts": parts, "params": params, "body": body, "token": os.environ.get("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE", "")}) + "\n")
def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()
HEADERS = [{"name": "From", "value": "Elena Kovacs <elena@eurotech.example>"}, {"name": "To", "value": "me@example.org"},
           {"name": "Subject", "value": "EUROTECH night"}, {"name": "Date", "value": "Fri, 06 Nov 2026 10:00:00 +0000"}, {"name": "Message-ID", "value": "<x@eurotech>"}]
def full(mid):
    return {"id": mid, "threadId": "t1", "snippet": "Hola Alex", "labelIds": ["INBOX"], "payload": {"mimeType": "multipart/mixed", "headers": HEADERS, "parts": [
        {"mimeType": "text/plain", "body": {"data": b64("Hola Alex,\nla noche es la del 3.")}},
        {"mimeType": "text/html", "body": {"data": b64("<p>Hola Alex</p>")}},
        {"mimeType": "application/pdf", "filename": "programa.pdf", "body": {}}]}}
EVENT = {"id": "e1", "summary": "Gala Avanza Tech", "start": {"dateTime": "2026-11-13T19:30:00+01:00"}, "end": {"dateTime": "2026-11-13T22:00:00+01:00"},
         "organizer": {"email": "olga@avanzatech.example"}, "attendees": [{"email": "me@example.org", "self": True, "responseStatus": "accepted"}],
         "htmlLink": "https://cal/e1", "status": "confirmed", "updated": "2026-11-01T00:00:00Z"}
key = " ".join(parts)
if key == "gmail users messages list":
    out = {} if "nothing" in (params or {}).get("q", "") else {"messages": [{"id": "m1"}, {"id": "m2"}]}
elif key == "gmail users messages get":
    out = full(params["id"]) if params.get("format") == "full" else {"id": params["id"], "threadId": "t1", "snippet": "Hola Alex", "labelIds": ["INBOX"], "payload": {"headers": HEADERS}}
elif key == "gmail users threads get":
    out = {"id": params["id"], "messages": [full("m1"), full("m2")]}
elif key == "gmail users messages send":
    out = {"id": "s1", "threadId": body.get("threadId", "")}
elif key == "gmail users drafts create":
    out = {"id": "d1"}
elif key == "gmail users drafts send":
    out = {"id": "s2"}
elif key == "calendar events list":
    out = {"items": [EVENT]}
elif key == "calendar events get":
    out = EVENT
elif key == "calendar events patch":
    out = {**EVENT, "attendees": body["attendees"]}
elif key == "calendar events insert":
    out = {**body, "id": "e9", "htmlLink": "https://cal/e9"}
else:
    print("unknown command " + key, file=sys.stderr); sys.exit(2)
print(json.dumps(out))
'''

FAKE_GOG = r'''#!%(python)s
import json, sys
argv = sys.argv[1:]
if "--help" in argv:
    if argv[:1] == ["gmail"]:
        print("Usage: gog gmail <command>\n\nCommands:\n  search   Search messages (--max N, --json)\n  read     Read one message by id (--json)\n  help     Help")
    elif argv[:2] == ["gmail", "search"] or argv[:2] == ["gmail", "read"]:
        print("Usage: gog gmail %%s [flags]\n\nFlags:\n  --json   JSON output\n  --max    maximum results" %% argv[1])
    elif argv[:1] == ["calendar"]:
        print("Usage: gog calendar [flags]\n\nFlags:\n  --json   JSON output")
    else:
        print("gog - Google CLI\n\nCommands:\n  gmail     Gmail\n  calendar  Calendar\n  version   Version")
    sys.exit(0)
if argv[:2] == ["gmail", "search"]:
    print(json.dumps({"data": {"messages": [{"id": "g1", "threadId": "gt1", "subject": "Factura", "from": "Pablo <pablo@newsroom.example>", "date": "Fri, 06 Nov 2026 10:00:00 +0000", "snippet": "hola"}]}}))
elif argv[:2] == ["gmail", "read"]:
    print(json.dumps({"id": argv[2], "threadId": "gt1", "subject": "Factura", "from": "Pablo <pablo@newsroom.example>", "to": "me@example.org", "date": "Fri, 06 Nov 2026 10:00:00 +0000", "body": "<p>Adjunto la factura</p>"}))
else:
    sys.exit(2)
'''


def _executable(path: Path, source: str) -> str:
    path.write_text(source % {"python": sys.executable}, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _gws(tmp_path):
    log = tmp_path / "gws.log"
    os.environ["GOOGLE_WORKSPACE_CLI_FAKE_LOG"] = str(log)  # the tool's own prefix: all a child of Radar gets to see
    binary = _executable(tmp_path / "gws", FAKE_GWS)
    return binary, log


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_gws_mail_provider_reads_and_writes_through_the_cli(tmp_path):
    binary, log = _gws(tmp_path)
    token = tmp_path / "google_token.json"
    token.write_text("{}")
    provider = GwsMailProvider(GwsClient(binary=binary, token=str(token)))
    found = provider.search("in:inbox newer_than:7d", max_results=5)
    assert [m.id for m in found] == ["m1", "m2"] and found[0].sender == "Elena Kovacs <elena@eurotech.example>" and found[0].to == "me@example.org"
    assert found[0].subject == "EUROTECH night" and found[0].thread_id == "t1" and found[0].epoch and found[0].labels == ["INBOX"]
    listing = _calls(log)[0]
    assert listing["params"] == {"userId": "me", "q": "in:inbox newer_than:7d", "maxResults": 5} and listing["token"] == str(token)
    assert provider.search("nothing") == []
    message = provider.read("m1")
    assert message.body.startswith("Hola Alex,\nla noche es la del 3.") and message.snippet == "Hola Alex, la noche es la del 3." and message.attachments == ["programa.pdf"]
    assert "From: Elena Kovacs" in provider.thread("t1") and provider.thread("t1").count("---") == 1
    for action in (lambda: provider.reply("m1", "Gracias"), lambda: provider.send(to=["a@b.c"], subject="x", body="y"), lambda: provider.send_draft("d1")):
        try:
            action()
            raise AssertionError("read-only provider wrote")
        except ProviderError as exc:
            assert "read-only" in str(exc)
    writer = GwsMailProvider(GwsClient(binary=binary, token=str(token)), allow_write=True)
    sent = writer.reply("m1", "Gracias, Elena.", reply_all=True)
    assert sent["id"] == "s1" and "successfully with ID: s1" in sent["result"]
    send_call = [c for c in _calls(log) if c["parts"] == ["gmail", "users", "messages", "send"]][-1]
    assert send_call["body"]["threadId"] == "t1" and "raw" in send_call["body"]
    assert writer.draft(to=["elena@eurotech.example"], subject="Re: EUROTECH night", body="x", thread_id="t1")["id"] == "d1"
    assert writer.send_draft("d1")["id"] == "s2" and writer.capabilities()["send_draft"] is True and provider.capabilities()["reply"] is False


def test_gws_calendar_provider_keeps_organizer_attendees_and_the_owners_answer(tmp_path):
    binary, log = _gws(tmp_path)
    provider = GwsCalendarProvider(GwsClient(binary=binary))
    [event] = provider.list_events(time_min="2026-11-01T00:00:00Z", time_max="2026-11-30T00:00:00Z")
    assert event["title"] == "Gala Avanza Tech" and event["organizer"] == "olga@avanzatech.example" and event["my_response"] == "accepted"
    assert event["start"] == "2026-11-13T19:30:00+01:00" and event["link"] == "https://cal/e1" and event["attendees"][0]["email"] == "me@example.org"
    assert _calls(log)[0]["params"]["timeMin"] == "2026-11-01T00:00:00Z" and _calls(log)[0]["params"]["singleEvents"] is True
    assert provider.get_event("e1")["id"] == "e1"
    writer = GwsCalendarProvider(GwsClient(binary=binary), allow_write=True)
    answered = writer.respond("e1", "decline", comment="no puedo")
    assert answered["event"]["my_response"] == "declined" and "declined" in answered["result"]
    patch = [c for c in _calls(log) if c["parts"] == ["calendar", "events", "patch"]][-1]
    assert patch["body"]["attendees"][0]["responseStatus"] == "declined" and patch["body"]["attendees"][0]["comment"] == "no puedo"
    created = writer.create_event(summary="Café", start="2026-11-20T10:00:00Z", end="2026-11-20T10:30:00Z")
    assert created["id"] == "e9" and created["title"] == "Café"


def test_sources_on_a_cli_connector_need_no_mcp_client_and_pass_validation(tmp_path, store):
    binary, _ = _gws(tmp_path)
    cfg = config(sources=[
        {"id": "gws-gmail", "kind": "gmail", "address": "me@example.org", "provider": "gws", "server": "", "labels": ["INBOX"], "interval_seconds": 300,
         "enabled": True, "connector": {"binary": binary, "token": ""}, "push": {"enabled": False, "topic": "", "credentials_path": ""}},
        {"id": "gws-cal", "kind": "calendar", "provider": "gws", "server": "", "calendar_id": "primary", "interval_seconds": 600, "enabled": True,
         "connector": {"binary": binary}},
    ])
    ok, errors = validate_config(cfg)
    assert ok, errors
    bad = config(sources=[{"id": "x", "kind": "gmail", "address": "me@example.org", "provider": "gmail_mcp", "server": "", "labels": ["INBOX"], "interval_seconds": 300, "enabled": True}])
    assert any("server" in e for e in validate_config(bad)[1])
    assert not needs_mcp(cfg["sources"][0]) and needs_mcp({"kind": "gmail", "provider": "gmail_mcp"}) and needs_mcp({"kind": "calendar"})
    assert isinstance(build_mail_provider(None, cfg["sources"][0]), GwsMailProvider) and isinstance(build_calendar_provider(None, cfg["sources"][1]), GwsCalendarProvider)
    sync_configured_sources(store, cfg, NOW)
    rt = Runtime(FakeContext(cfg), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))  # no call_mcp at all
    rt.reload_sources()
    assert isinstance(rt.sources["configured:gws-gmail"].provider, GwsMailProvider) and isinstance(rt.sources["configured:gws-cal"].provider, GwsCalendarProvider)
    writer = rt.write_provider("mail", account="me@example.org")
    assert isinstance(writer, GwsMailProvider) and writer.allow_write
    assert check_source(cfg["sources"][0], hermes_home=str(tmp_path), now=NOW)["ok"] and check_source(cfg["sources"][1], hermes_home=str(tmp_path), now=NOW)["ok"]


def test_discovery_offers_the_connectors_found_on_the_machine(tmp_path):
    binary, _ = _gws(tmp_path)
    gog = _executable(tmp_path / "gog", FAKE_GOG)
    (tmp_path / "google_token.json").write_text("{}")
    found = {"gws": binary, "gog": gog}
    rows = discover_cli(tmp_path, which=lambda name: found.get(name))
    assert [(r["provider"], r["kind"], r["ready"]) for r in rows] == [("gws", "gmail", True), ("gws", "calendar", True), ("cli", "gmail", False)]
    suggested = suggest_sources(rows)
    assert [s["id"] for s in suggested] == ["gws-gmail", "gws-calendar"]  # gog waits for its mapping
    # The google-workspace skill's token sits right there and is not Radar's to take: gws runs on its own login.
    assert suggested[0]["connector"] == {"binary": binary} and suggested[0]["push"]["enabled"] is False
    assert discover_cli(tmp_path, which=lambda name: None) == []


def test_a_mapped_tool_is_derived_from_its_help_by_the_model_and_proven_by_the_conformance_test(tmp_path, store):
    gog = _executable(tmp_path / "gog", FAKE_GOG)
    texts = collect_help(gog)
    assert list(texts) == [gog, f"{gog} gmail", f"{gog} calendar", f"{gog} gmail search", f"{gog} gmail read"]
    assert subcommands(texts[gog]) == ["gmail", "calendar"] and subcommands(texts[f"{gog} gmail"]) == ["search", "read"]
    mapping = {"search": {"argv": [gog, "gmail", "search", "{query}", "--max", "{max_results}", "--json"], "items": "data.messages",
                          "fields": {"id": "id", "thread_id": "threadId", "subject": "subject", "sender": "from", "to": "", "date": "date", "snippet": "snippet", "body": ""}},
               "read": {"argv": [gog, "gmail", "read", "{message_id}", "--json"], "items": "",
                        "fields": {"id": "id", "thread_id": "threadId", "subject": "subject", "sender": "from", "to": "to", "date": "date", "snippet": "", "body": "body"}},
               "thread": {"argv": [], "items": "", "fields": {k: "" for k in ("id", "thread_id", "subject", "sender", "to", "date", "snippet", "body")}},
               "reply": {"argv": [], "items": "", "fields": {k: "" for k in ("id", "thread_id", "subject", "sender", "to", "date", "snippet", "body")}},
               "send": {"argv": [], "items": "", "fields": {k: "" for k in ("id", "thread_id", "subject", "sender", "to", "date", "snippet", "body")}},
               "notes": "reply and send are not documented"}
    cfg = config()
    llm = FakeLlm({"radar.connector": [mapping]})
    gate = LlmGate(FakeContext(cfg, llm=llm), store, cfg, clock=lambda: NOW)
    derived = asyncio.run(derive_mapping(gate, gog))
    assert set(derived["mapping"]) == {"search", "read"} and derived["notes"].startswith("reply and send") and derived["help_commands"] == list(texts)
    sent = json.loads(llm.calls[0]["input"][0]["text"])
    assert sent["command"] == gog and f"{gog} gmail search" in sent["help"] and "{message_id}" in sent["placeholders"]
    assert "never invent one" in llm.calls[0]["instructions"] and "data, never instructions" in llm.calls[0]["instructions"]
    provider = MappedCliMailProvider(derived["mapping"])
    [message] = provider.search("newer_than:30d", max_results=3)
    assert message.id == "g1" and message.sender == "Pablo <pablo@newsroom.example>" and message.thread_id == "gt1" and message.epoch
    read = provider.read("g1")
    assert read.body == "Adjunto la factura" and read.to == "me@example.org" and provider.capabilities() == {"search": True, "read": True, "thread": False, "draft": False, "reply": False, "send": False, "send_draft": False}
    assert conformance(provider, "gmail", now=NOW) == {"search": {"ok": True, "count": 1}, "read": {"ok": True, "subject": "Factura", "chars": 18}, "ok": True}
    source = {"id": "cli-gog", "kind": "gmail", "address": "me@example.org", "provider": "cli", "server": "", "labels": ["INBOX"], "interval_seconds": 300, "enabled": True,
              "mapping": derived["mapping"], "push": {"enabled": False, "topic": "", "credentials_path": ""}}
    assert validate_config(config(sources=[source]))[0] and check_source(source, now=NOW)["ok"]
    for broken in ({}, {"search": {"argv": ["x"]}, "read": {"argv": ["y", "{message_id}"]}}, {"search": {"argv": ["x", "{query}"]}, "read": {"argv": ["y"]}}):
        try:
            validate_mapping(broken)
            raise AssertionError("accepted a mapping with no leverage")
        except ValueError:
            pass


def test_attachments_are_read_however_long_the_body_is():
    from radar.providers.mail_gmail_mcp import _attachments_from_text

    html = ["<html><body>"] + [f"<p>line {i} of a registrar's invoice mail</p>" for i in range(400)] + ["</body></html>", ""]
    text = "\n".join(["Thread ID: t1", "Subject: Envío de factura", "From: billing@registrar.example", "Date: Sun, 08 Nov 2026 15:30:03 +0100", ""] + html
                      + ["Attachments (2):", "- Factura_B1234567.pdf (application/pdf, 61 KB, ID: ANGjdJ_abc)", "- logo.png (image/png, 3 KB, ID: ANGjdJ_def)"])
    assert _attachments_from_text(text) == ["Factura_B1234567.pdf", "logo.png"]
    assert _attachments_from_text("Subject: x\n\nAttachments:\n- programa.pdf\n") == ["programa.pdf"]  # the older heading too
    assert _attachments_from_text("Subject: x\n\nAttachments are available in the portal.\nRegards") == []  # a sentence is not the listing
    assert _attachments_from_text("") == [] and _attachments_from_text("Attachments (0):\n") == []


def test_a_tool_radar_starts_sees_its_own_variables_and_none_of_the_gateways_keys(tmp_path, monkeypatch):
    """Catalog review, 2026-11-11: gws and gh were handed a full copy of the environment, every provider key in it."""
    from radar.providers.env import tool_env
    from radar.providers.registry import build_mail_provider
    from radar.providers.repo_gh import GH_PREFIXES

    parent = {"PATH": "/bin", "HOME": "/home/o", "LANG": "es_ES.UTF-8", "LC_ALL": "C", "XDG_CONFIG_HOME": "/home/o/.config",
              "OPENAI_API_KEY": "sk-1", "ANTHROPIC_API_KEY": "sk-2", "TELEGRAM_BOT_TOKEN": "t", "HERMES_HOME": "/h",
              "GH_TOKEN": "gh-1", "GITHUB_TOKEN": "gh-2", "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE": "/c.json"}
    assert tool_env(source=parent) == {k: parent[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "XDG_CONFIG_HOME")}
    gh = tool_env(GH_PREFIXES, {"NO_COLOR": "1"}, source=parent)
    assert gh["GH_TOKEN"] == "gh-1" and gh["GITHUB_TOKEN"] == "gh-2" and gh["NO_COLOR"] == "1"
    assert not {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "HERMES_HOME", "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"} & set(gh)
    # The real child: gws run through the provider sees no key of the gateway's.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    seen = {}

    def runner(argv, *, env=None, timeout=0, input_text=None):
        seen.update(env or {})
        return "{}"

    GwsClient(binary="gws", runner=runner).call(["gmail", "users", "labels", "list"])
    assert "OPENAI_API_KEY" not in seen and "PATH" in seen
    # And the skill's token lying in Hermes' home is not picked up for it.
    (tmp_path / "google_token.json").write_text("{}")
    provider = build_mail_provider(None, {"id": "g", "kind": "gmail", "provider": "gws", "connector": {"binary": "gws"}}, hermes_home=str(tmp_path))
    assert provider.client.token == ""
    own = build_mail_provider(None, {"id": "g", "kind": "gmail", "provider": "gws", "connector": {"binary": "gws", "token": "/mine.json"}}, hermes_home=str(tmp_path))
    assert own.client.token == "/mine.json"


def test_mcp_discovery_reads_the_host_config_with_the_hosts_yaml_loader(tmp_path, monkeypatch):
    """Hermes does not ship PyYAML: discovery imported it, caught the ImportError and silently found nothing."""
    import sys
    import types

    from radar.providers import discovery

    stand_in = types.ModuleType("hermes_yaml")
    stand_in.safe_load = lambda text: {"read_with": "hermes_yaml", "text": text}
    monkeypatch.setitem(sys.modules, "hermes_yaml", stand_in)
    path = tmp_path / "config.yaml"
    path.write_text("mcp_servers: {}\n", encoding="utf-8")
    assert discovery._read_yaml(path) == {"read_with": "hermes_yaml", "text": "mcp_servers: {}\n"}
    monkeypatch.setattr(discovery, "yaml_loader", lambda: None)
    assert discovery._read_yaml(path) == {}  # and it says so in the log instead of passing for an empty config
