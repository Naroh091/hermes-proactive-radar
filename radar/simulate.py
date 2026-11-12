"""Scenarios through the real pipeline: what Radar would judge and say about a situation, without sending anything.

``hermes radar simulate <file>`` takes a scenario (mails, calendar events, chat turns, optional owner facts), copies
the live store to a scratch database (so facts, habits, preferences and open tasks are the owner's real ones; ``--fresh``
starts empty), feeds the signals in exactly as the sources would (same metadata, same triage), runs the real pipeline
with the real model (extract, correspondents, plan, act) and reports: what triage did with each signal, what the
extractor made of the bundle, the task and the steps the plan left, the gates that held things back (``withheld``,
``asks_dropped``, ``drafts_dropped``, ``kept_closed``) and the message the owner would have received, written by the
model in prose. Jobs are disabled and no transport is registered: nothing runs, nothing is sent, the live store is
never touched. A scenario's ``expect`` blocks (:mod:`radar.expectations`) are checked against the run and the command
fails when one does not hold.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .delivery.dispatch import dispatch, select_interrupts, select_reminder
from .delivery.remind import compose
from . import expectations
from .models import canonical_hash
from .pipeline.process import process_bundle
from .pipeline.triage import triage_signal
from .providers.mcp_parsing import automation_signals
from .runtime import Runtime
from .sources.base import fingerprint
from .sources.calendar import event_fingerprint
from .sources.session import record_turn
from .store import Scope, Store
from .transports import TransportRegistry
from .trust import needs_consent

SIM_DIR = "sim"
MAIL_SOURCE = "sim:mail"
CALENDAR_SOURCE = "sim:calendar"
_TAGS = re.compile(r"<[^>]+>")


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_moment(text: Any, default: datetime) -> datetime:
    raw = str(text or "").strip()
    if not raw:
        return default
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return default
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def load_scenario(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith(".json"):
        data = json.loads(text)
    else:
        from .providers.discovery import yaml_loader

        load = yaml_loader()
        if load is None:
            raise ValueError("YAML scenarios need a YAML loader (Hermes' hermes_yaml or PyYAML); write the scenario as JSON")
        data = load(text)
    if not isinstance(data, dict) or not (isinstance(data.get("signals"), list) or isinstance(data.get("phases"), list)):
        raise ValueError("a scenario is an object with a 'signals' list, or a 'phases' list of {at, signals, replies}")
    try:
        expectations.validate_scenario(data)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return data


def phases_of(scenario: dict[str, Any], default_now: datetime) -> list[dict[str, Any]]:
    """A scenario is one moment (``signals`` at ``now``) or several (``phases: [{at, signals, facts, preferences,
    replies}]``), in order: a ticket bought one day and the check-in mail a week later."""
    if isinstance(scenario.get("phases"), list):
        phases = [dict(p) for p in scenario["phases"] if isinstance(p, dict)]
        moment = default_now
        for phase in phases:
            moment = parse_moment(phase.get("at"), moment)
            phase["at"] = moment
        return phases
    return [{**scenario, "at": default_now}]


def scratch_store(hermes_home: str | Path, scope: Scope, *, fresh: bool, name: str = "radar") -> Store:
    """A store under ``<home>/radar/sim/<name>.sqlite3``: a consistent copy of the live database (WAL included) or an
    empty one. One file per scenario, so several can run side by side and each result can be inspected afterwards."""
    sim_dir = Path(hermes_home) / "radar" / SIM_DIR
    sim_dir.mkdir(parents=True, exist_ok=True)
    path = sim_dir / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', name) or 'radar'}.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        if target.exists():
            target.unlink()
    live = Path(hermes_home) / "radar" / "radar.sqlite3"
    if not fresh and live.exists():
        source = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
        destination = sqlite3.connect(str(path))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
    return Store(path, scope)


def sim_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """The owner's configuration with everything that could act or send switched off."""
    out = deepcopy(cfg)
    out["enabled"] = True
    out["mode"] = "active"
    out["jobs"] = {**(out.get("jobs") or {}), "enabled": False}
    out["notifications"] = {**(out.get("notifications") or {}), "enabled": True, "quiet_start": "00:00", "quiet_end": "00:00", "daily_limit": 1000}
    out["notices_per_day"] = 1000  # the copied store carries today's real notices; the scenario's must still get through
    out["warmup"] = {**(out.get("warmup") or {}), "enabled": False}
    out["observation"] = {**(out.get("observation") or {}), "local_sessions": True}
    out["ingress"] = {**(out.get("ingress") or {}), "gmail_receiver": {**((out.get("ingress") or {}).get("gmail_receiver") or {}), "enabled": False}}
    return out


def _owner_account(cfg: dict[str, Any]) -> str:
    for source in cfg.get("sources") or []:
        if isinstance(source, dict) and source.get("kind") == "gmail" and source.get("address"):
            return str(source["address"])
    return "owner@example.org"


def _ensure_sources(store: Store, cfg: dict[str, Any], now: str) -> None:
    retention = int((cfg.get("retention") or {}).get("observations_days", 30))
    for source_id, kind, ref in ((MAIL_SOURCE, "gmail", _owner_account(cfg)), (CALENDAR_SOURCE, "calendar", "primary")):
        try:
            store.get_source(source_id)
        except KeyError:
            store.add_source(source_id=source_id, kind=kind, account_ref=ref, resource_scope={}, retention_days=retention, now=now)


def _mail_signal(item: dict[str, Any], cfg: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    account = str(item.get("account") or _owner_account(cfg))
    sender = str(item.get("from") or item.get("sender") or "someone@example.org")
    subject = str(item.get("subject") or "(no subject)")
    body = str(item.get("body") or "")
    occurred = parse_moment(item.get("date"), now_dt - timedelta(minutes=5))
    message_id = str(item.get("id") or f"sim-{uuid4().hex[:12]}")
    thread_id = str(item.get("thread") or item.get("thread_id") or message_id)
    meta = {
        "account": account, "sender": sender, "to": str(item.get("to") or account), "date": occurred.strftime("%a, %d %b %Y %H:%M:%S +0000"),
        "thread_id": thread_id, "automation": automation_signals(sender), "attachments": [str(a) for a in item.get("attachments") or []],
        "source_kind": "gmail", "thread_reply": subject.lower().startswith(("re:", "fwd:", "rv:")), "server": "sim",
        **({"subject": subject}),
    }
    if sender.lower().split("<")[-1].rstrip(">").strip() == account.lower():
        meta["owner_sent"] = True
    snippet_chars = int((cfg.get("retention") or {}).get("snippet_chars", 500))
    return {
        "id": str(uuid4()), "source_id": MAIL_SOURCE, "kind": "email", "occurred_at": iso(occurred), "external_ref": message_id,
        "group_key": f"thread:{account}:{thread_id}", "title": subject[:300], "snippet": " ".join(body.split())[:snippet_chars], "meta": meta,
        "fingerprint": fingerprint("email", account, message_id), "raw_content": body[:20000] or None,
        "raw_expires_at": iso(now_dt + timedelta(hours=48)) if body else None, "content_ref": "",
    }


def _calendar_signal(item: dict[str, Any], cfg: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    start = parse_moment(item.get("start"), now_dt + timedelta(days=7))
    end = parse_moment(item.get("end"), start + timedelta(hours=1))
    event = {
        "id": str(item.get("id") or f"sim-{uuid4().hex[:10]}"), "title": str(item.get("title") or "(untitled)"), "start": iso(start), "end": iso(end),
        "all_day": bool(item.get("all_day")), "status": str(item.get("status") or "confirmed"), "updated": iso(now_dt), "location": str(item.get("location") or ""),
        "description": str(item.get("description") or ""), "organizer": str(item.get("organizer") or ""),
        "attendees": [{"email": str(a)} for a in item.get("attendees") or []], "my_response": str(item.get("my_response") or ""),
        "link": str(item.get("link") or ""), "calendar_id": "primary", "recurring_event_id": "", "event_type": "",
    }
    change = str(item.get("change") or "created")
    snippet_chars = int((cfg.get("retention") or {}).get("snippet_chars", 500))
    head = f"{event['start']} → {event['end']}" + (f" · {event['location']}" if event["location"] else "") + (f" · organizer {event['organizer']}" if event["organizer"] else "")
    meta = {
        "source_kind": "calendar", "account": "primary", "calendar_id": "primary", "event_id": event["id"], "change": change, "start": event["start"],
        "end": event["end"], "all_day": event["all_day"], "status": event["status"], "location": event["location"], "organizer": event["organizer"],
        "attendees": [a["email"] for a in event["attendees"]], "my_response": event["my_response"], "link": event["link"], "recurring_event_id": "",
        "event_type": "", "updated": event["updated"],
    }
    return {
        "id": str(uuid4()), "source_id": CALENDAR_SOURCE, "kind": "calendar", "occurred_at": iso(now_dt - timedelta(minutes=5)),
        "external_ref": f"{event['id']}@{change}:{event_fingerprint(event)}", "group_key": f"calendar:{CALENDAR_SOURCE}:{event['id']}",
        "title": event["title"][:300], "snippet": (f"[{change}] {head}. {event['description']}").strip()[:snippet_chars], "meta": meta,
        "fingerprint": fingerprint("calendar", CALENDAR_SOURCE, event["id"], change, event_fingerprint(event)),
    }


def _add(store: Store, cfg: dict[str, Any], signal: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    decision = triage_signal(signal, store.list_preferences(active_only=True), cfg)
    store.add_signal(
        signal_id=signal["id"], source_id=signal["source_id"], kind=signal["kind"], occurred_at=signal["occurred_at"], external_ref=signal["external_ref"],
        group_key=signal["group_key"], title=signal["title"], snippet=signal["snippet"], meta=signal["meta"], fingerprint=signal["fingerprint"],
        now=iso(now_dt), raw_content=signal.get("raw_content"), raw_expires_at=signal.get("raw_expires_at"), content_ref=signal.get("content_ref") or None,
        triage_status="pending" if decision.keep else "ignored", triage_reason=None if decision.keep else decision.reason,
        expires_at=iso(now_dt + timedelta(days=30)),
    )
    return {"id": signal["id"], "kind": signal["kind"], "title": signal["title"], "triage": "kept" if decision.keep else f"ignored ({decision.reason})"}


def feed(store: Store, cfg: dict[str, Any], scenario: dict[str, Any], now_dt: datetime) -> list[dict[str, Any]]:
    """The scenario's facts and signals into the scratch store, as the sources would write them. Whatever the copied
    store already had pending (the owner's live backlog of signals) is put to sleep first, so only the scenario is
    judged and no model call is spent on it."""
    now = iso(now_dt)
    with store.transaction() as db:
        db.execute("UPDATE signals SET next_attempt_at=? WHERE triage_status='pending'", (iso(now_dt + timedelta(days=365)),))
    _ensure_sources(store, cfg, now)
    for fact in scenario.get("facts") or []:
        if isinstance(fact, dict) and fact.get("key") and fact.get("value"):
            store.record_owner_fact(key=str(fact["key"]), value=str(fact["value"]), provenance={"at": now, "scenario": True}, now=now)
    for pref in scenario.get("preferences") or []:
        # A rule the owner would have stated or learnt (effect silent|digest|notify|prefer, scope by sender, account, kind…).
        if not isinstance(pref, dict) or pref.get("effect") not in ("silent", "digest", "notify", "prefer"):
            continue
        from .delivery.learn import store_preference

        store_preference(store, {"dimension": "notification", "effect": str(pref["effect"]), "scope": {k: str(v) for k, v in (pref.get("scope") or {}).items()},
                                 "exceptions": (), "instruction": str(pref.get("instruction") or f"scenario: {pref['effect']} {pref.get('scope')}")},
                         origin="explicit", now_dt=now_dt)
    fed: list[dict[str, Any]] = []
    for item in scenario.get("signals") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "email")
        if kind in ("email", "mail"):
            fed.append(_add(store, cfg, _mail_signal(item, cfg, now_dt), now_dt))
        elif kind in ("calendar", "event"):
            fed.append(_add(store, cfg, _calendar_signal(item, cfg, now_dt), now_dt))
        elif kind in ("chat", "session"):
            session_id = str(item.get("session_id") or f"sim-{uuid4().hex[:8]}")
            when = parse_moment(item.get("when"), now_dt - timedelta(hours=1))  # long enough ago for the session to count as quiet
            turns = item.get("turns") or []
            added = 0
            for index, turn in enumerate(turns):
                if not isinstance(turn, dict):
                    continue
                event = {"platform": str(item.get("platform") or "desktop"), "session_id": session_id, "turn_id": f"{session_id}:{index}",
                         "user_message": str(turn.get("user") or ""), "assistant_response": str(turn.get("assistant") or "")}
                if record_turn(store, cfg, event, when + timedelta(seconds=index)):
                    added += 1
            fed.append({"kind": "chat", "title": f"session {session_id}: {added} turn(s)", "triage": "kept" if added else "ignored (empty)"})
    return fed


def _text(payload: dict[str, Any]) -> str:
    return " ".join(_TAGS.sub("", str(payload.get("text") or "")).split())


def _buttons(payload: dict[str, Any]) -> list[str]:
    return [str(b.get("label")) for row in payload.get("buttons") or [] for b in row if isinstance(b, dict)]


async def would_send(rt: Runtime, now_dt: datetime, *, language: str, task_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """What the gateway's next tick would put in the owner's chat about the scenario's tasks: the immediate notices
    and the reminder, each written in prose by the model; and what waits behind them (a decision not due yet).
    The copied store carries the owner's real backlog too; with ``task_ids`` only the scenario's tasks are shown."""
    store, cfg = rt.store, rt.config
    gate = rt.gate
    before = {row["id"] for row in store.list_outbox(limit=200)}
    interrupts = select_interrupts(store, cfg, now_dt)
    for entry in interrupts:
        entry["text"] = await compose(gate, items=[{"task": entry["task"], "decision": entry["decision"], "news": entry["news"]}], language=language,
                                      mode="interrupt", template=False, subject_id=entry["task"]["id"], now=iso(now_dt))
    # The reminder: what production's pacing would pick now, if it is one of the scenario's tasks; otherwise the
    # scenario's own pending decision or news, built and queued exactly as the reminder would be (so a reply of the
    # scenario can land on it), labelled as not due yet.
    picked = select_reminder(store, cfg, now_dt)
    picked_task = str(((picked or {}).get("items") or [{}])[0].get("task", {}).get("id") or "") if picked else ""
    timing = "reminder"
    if not picked or (task_ids is not None and picked_task not in task_ids):
        picked = None
        timing = "reminder (queued, not due yet)"
        pending = [p for p in store.list_proposals(statuses=("pending",), limit=200) if p.get("batch_key") == "digest"]
        for proposal in pending:
            task_id = str(proposal.get("task_id") or "")
            if task_ids is not None and task_id not in task_ids:
                continue
            try:
                task = store.get_task(task_id)
            except KeyError:
                continue
            items = [p for p in pending if str(p.get("task_id") or "") == task_id]
            decisions = [p for p in items if needs_consent(cfg, task, p)]
            news = [p for p in items if p.get("operation_id") == "inform" and not needs_consent(cfg, task, p)]
            if not decisions and not news:
                continue
            picked = {"items": [{"task": task, "decision": decisions[0] if decisions else None, "news": news[:1]}], "decisions": decisions[:1],
                      "news": news[:1], "backlog": 0, "first_of_day": False}
            break
    if picked:
        picked["text"] = await compose(gate, items=picked["items"], language=language, backlog=picked.get("backlog", 0), first_of_day=picked.get("first_of_day", False),
                                       subject_id=(picked["items"][0].get("task") or {}).get("id") if picked["items"] else None, now=iso(now_dt))
    dispatch(store, cfg, now_dt, reminder=picked, interrupts=interrupts)
    out: list[dict[str, Any]] = []
    for row in store.list_outbox(limit=200):
        if row["id"] in before:
            continue
        payload = row.get("payload") or {}
        task_id = str(row.get("task_id") or payload.get("task_id") or "")
        if task_ids is not None and task_id not in task_ids:
            continue
        out.append({"kind": "notice" if row.get("kind") == "notice" else timing, "task_id": task_id, "text": _text(payload), "buttons": _buttons(payload)})
    return out


def _audit_flags(store: Store, task_id: str) -> dict[str, Any]:
    rows = [a for a in store.list_audit(limit=50, subject_id=task_id) if a.get("event_type") == "pipeline"]
    if not rows:
        return {}
    meta = rows[0].get("metadata_redacted") or {}
    return {k: meta.get(k) for k in ("disposition", "attention", "urgency", "importance", "withheld", "asks_dropped", "drafts_dropped", "kept_closed", "suppressed", "kept_open") if meta.get(k)}


async def apply_replies(rt: Runtime, scenario: dict[str, Any], now_dt: datetime, *, task_ids: set[str], language: str) -> list[dict[str, Any]]:
    """The owner's replies of the scenario (``replies: [{text: "..."}]``), each to the latest message about one of the
    scenario's tasks, read and resolved exactly as a Telegram reply would be: what Radar answers, what it learnt
    (preferences), and what the task heard (a ``user_reply`` signal)."""
    from .delivery.feedback import understand
    from .delivery.responses import record_owner_words, resolve_reply

    store, cfg = rt.store, rt.config
    out: list[dict[str, Any]] = []
    for item in scenario.get("replies") or []:
        text = str((item or {}).get("text") or "") if isinstance(item, dict) else str(item or "")
        if not text:
            continue
        rows = [r for r in store.list_outbox(limit=200) if str(r.get("task_id") or (r.get("payload") or {}).get("task_id") or "") in task_ids]
        if not rows:
            out.append({"text": text, "answer": "(no message of the scenario to reply to)"})
            continue
        row = rows[0]
        before = {p["id"] for p in store.list_preferences()}
        intent = await understand(rt.gate, text, outbox_row=row, store=store, language=language)
        result = resolve_reply(store, cfg, outbox_row=row, intent=intent, actor="telegram:sim", now_dt=now_dt, note=text)
        from .delivery.learn import learn_from_reply

        # A lasting rule in the owner's words (the whole reply, or beside its answer) becomes a preference, as in Telegram.
        result = await learn_from_reply(rt, intent=intent, text=text, task_id=str(row.get("task_id") or (row.get("payload") or {}).get("task_id") or ""),
                                        actor="telegram:sim", now_dt=now_dt, result=result, outbox_row=row)
        if result.get("outcome") in ("unclear", "noted"):
            record_owner_words(store, outbox_row=row, text=text, actor="telegram:sim", channel="telegram", now_dt=now_dt, language=language)
        learnt = [p for p in store.list_preferences() if p["id"] not in before]
        answer = str(result.get("text") or "") or "(no answer)"
        if result.get("outcome") in ("unclear", "question"):
            answer = "(handed to the chat agent, which answers in the chat; the task heard your words)"
        out.append({"text": text, "intent": intent.get("intent"), "outcome": result.get("outcome"), "answer": answer,
                    "learnt": [f"{p.get('effect')} {p.get('scope')}" for p in learnt]})
    return out


def _proposal_view(proposal: dict[str, Any]) -> dict[str, Any]:
    params = proposal.get("params") or {}
    view = {"title": proposal.get("title"), "operation": proposal.get("operation_id"), "class": proposal.get("action_class"), "batch": proposal.get("batch_key"),
            "status": proposal.get("status"), "urgency": proposal.get("urgency"), "importance": params.get("importance")}
    for key in ("ask", "basis", "then", "when", "owner_followup", "correction"):
        if params.get(key):
            view[key] = params[key]
    if proposal.get("options"):
        view["options"] = [str(o.get("label")) for o in proposal["options"] if isinstance(o, dict)]
    return view


async def _between(rt: Runtime, since_dt: datetime, now_dt: datetime, task_ids: set[str], *, language: str) -> dict[str, Any]:
    """What the gateway does on its own between two moments of the scenario, in the order it would happen: a step
    timed for a moment in between goes out at that moment, a review that came due runs at its hour (each may change
    what the other sees), then what lapsed expires, what was snoozed wakes, the idle decays, and the reminder that came
    due by the new moment is shown."""
    from .pipeline.decay import run as decay
    from .pipeline.process import review_task

    store, cfg = rt.store, rt.config
    out: dict[str, Any] = {"messages": [], "reviews": []}
    done: set[tuple[str, str]] = set()
    for _ in range(40):
        # Re-scanned after every event: a review may schedule the next one (the fallback of an awaited step) still
        # inside the window, and a delivery may be the result of a review. The earliest pending event goes first.
        events: list[tuple[datetime, str, tuple[str, str], dict[str, Any] | None]] = []
        for proposal in store.list_proposals(statuses=("pending",), limit=500):
            if str(proposal.get("task_id") or "") not in task_ids:
                continue
            after = parse_moment((proposal.get("params") or {}).get("deliver_after"), since_dt)
            key = ("deliver", str(proposal.get("id")))
            if since_dt < after <= now_dt and key not in done:
                events.append((after, "deliver", key, None))
        for task in store.tasks_due_review(iso(now_dt), limit=100):
            if str(task.get("id")) not in task_ids:
                continue
            due = parse_moment(task.get("next_review_at") or task.get("snoozed_until"), now_dt)
            at = min(max(due, since_dt), now_dt)
            key = ("review", f"{task.get('id')}@{iso(at)}")
            if key not in done:
                events.append((at, "review", key, task))
        if not events:
            break
        events.sort(key=lambda event: (event[0], event[1]))
        at, kind, key, task = events[0]
        done.add(key)
        if kind == "deliver":
            out["messages"].extend({**m, "at": iso(at)} for m in await would_send(rt, at, language=language, task_ids=task_ids))
            continue
        try:
            summary = await review_task(rt, task, at)  # type: ignore[arg-type]
        except Exception as exc:
            summary = {"error": str(exc)[:120]}
        out["reviews"].append({"task": (task or {}).get("title"), "at": iso(at),
                               **{k: v for k, v in (summary or {}).items() if k in ("decision", "disposition", "proposals", "deferred", "skipped", "error")}})
    now = iso(now_dt)
    out["expired"], out["woken"] = store.expire_proposals(now), store.wake_snoozed_proposals(now)
    try:
        out["decayed"] = decay(store, cfg, now_dt)
    except Exception as exc:
        out["decay_error"] = str(exc)[:120]
    out["messages"].extend({**m, "at": now} for m in await would_send(rt, now_dt, language=language, task_ids=task_ids))
    return out


async def _process_pending(rt: Runtime, now_dt: datetime) -> list[tuple[dict[str, Any], dict[str, Any], datetime]]:
    store = rt.store
    processed: list[tuple[dict[str, Any], dict[str, Any], datetime]] = []
    for bundle in store.claim_signal_bundles(iso(now_dt), iso(now_dt + timedelta(minutes=10)), 20):
        processed.append((bundle, await process_bundle(rt, bundle, now_dt), now_dt))
    # A bundle the model could not judge (a provider failure, the bench) is retried once, as a later tick would.
    if any(s.get("deferred") for _, s, _ in processed):
        from .pipeline.process import RETRY_MINUTES

        later = now_dt + timedelta(minutes=RETRY_MINUTES + 1)
        for bundle in store.claim_signal_bundles(iso(later), iso(later + timedelta(minutes=10)), 20):
            processed = [item for item in processed if item[0]["group_key"] != bundle["group_key"]]
            processed.append((bundle, await process_bundle(rt, bundle, later), later))
    return processed


async def run_scenario(rt: Runtime, scenario: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    """Feed, process and report, one phase after another. ``rt`` is a runtime on the scratch store with jobs off and
    no transports. Between two phases the gateway's own housekeeping runs (:func:`_between`) and whatever came due
    meanwhile is shown before the new signals."""
    store, cfg = rt.store, rt.config
    language = str((cfg.get("owner") or {}).get("language") or "es")
    phases = phases_of(scenario, now_dt)
    report: dict[str, Any] = {"now": iso(phases[0]["at"]), "phases": [], "signals": [], "bundles": [], "would_send": []}
    task_ids: set[str] = set()
    started, calls_before = time.monotonic(), _model_stats(store)
    for index, phase in enumerate(phases):
        at = phase["at"]
        phase_started = time.monotonic()
        entry: dict[str, Any] = {"at": iso(at)}
        if index:
            entry["between"] = await _between(rt, phases[index - 1]["at"], at, task_ids, language=language)
        entry["signals"] = feed(store, cfg, phase, at)
        entry["bundles"] = []
        for bundle, summary, when in await _process_pending(rt, at):
            entry["bundles"].append(_bundle_view(store, bundle, summary))
            if summary.get("task_id"):
                task_ids.add(str(summary["task_id"]))
        entry["would_send"] = await would_send(rt, at, language=language, task_ids=task_ids)
        if phase.get("replies"):
            entry["replies"] = await apply_replies(rt, phase, at + timedelta(minutes=1), task_ids=task_ids, language=language)
            later = at + timedelta(minutes=2)
            follow: list[dict[str, Any]] = []
            for bundle, summary, when in await _process_pending(rt, later):
                follow.append(_bundle_view(store, bundle, summary))
                if summary.get("task_id"):
                    task_ids.add(str(summary["task_id"]))
            entry["after_replies"] = {"bundles": follow, "would_send": await would_send(rt, later, language=language, task_ids=task_ids)}
        entry["took_s"] = round(time.monotonic() - phase_started, 1)
        report["phases"].append(entry)
        report["signals"].extend(entry["signals"])
        report["bundles"].extend(entry["bundles"])
        report["would_send"] = entry["would_send"]
        if entry.get("replies"):
            report["replies"], report["after_replies"] = entry["replies"], entry.get("after_replies")
    report["final"] = _final_state(store, task_ids)
    report["took_s"] = round(time.monotonic() - started, 1)
    after = _model_stats(store)
    report["model_calls"] = after["calls"] - calls_before["calls"]
    report["model_s"] = round((after["ms"] - calls_before["ms"]) / 1000, 1)
    report["models"] = sorted(set(after["models"]) - set(calls_before["models"])) or after["models"][-1:]
    report["summary"] = render(report)
    return report


def _final_state(store: Store, task_ids: set[str]) -> dict[str, Any]:
    """The scenario's tasks and steps as they stand when the run ends, reviews and expiries included."""
    tasks: list[dict[str, Any]] = []
    steps: list[list[dict[str, Any]]] = []
    for task_id in sorted(task_ids):
        try:
            task = store.get_task(task_id)
        except KeyError:
            continue
        tasks.append({"id": task_id, "kind": task.get("kind"), "title": task.get("title"), "status": task.get("status"), "due_at": task.get("due_at"), "summary": task.get("summary")})
        steps.append([_proposal_view(p) for p in store.list_proposals(task_id=task_id, limit=40)])
    return {"tasks": tasks, "steps": steps}


def _model_stats(store: Store) -> dict[str, Any]:
    """The model calls made on this store so far (the gate audits every call with its duration and model)."""
    try:
        count, ms = store.db.execute("SELECT COUNT(*), COALESCE(SUM(json_extract(metadata_redacted_json, '$.elapsed_ms')), 0) FROM audit WHERE event_type='llm_call'").fetchone()
        rows = store.db.execute("SELECT DISTINCT json_extract(metadata_redacted_json, '$.provider') || ':' || json_extract(metadata_redacted_json, '$.model') FROM audit WHERE event_type='llm_call'").fetchall()
        return {"calls": int(count or 0), "ms": int(ms or 0), "models": [str(r[0]) for r in rows if r and r[0]]}
    except Exception:
        return {"calls": 0, "ms": 0, "models": []}


_model_calls = lambda store: _model_stats(store)["calls"]  # noqa: E731


def _bundle_view(store: Store, bundle: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """One processed bundle as the report shows it: outcome, the last extract, the task, its proposals and gates."""
    ids = [s["id"] for s in bundle["signals"]]
    entry: dict[str, Any] = {"group": bundle["group_key"], "outcome": {k: v for k, v in summary.items() if k not in ("group", "signals")}}
    extracts = []
    for sid in ids:
        try:
            extract = store.get_signal(sid).get("extract")
        except KeyError:
            extract = None
        if isinstance(extract, dict):
            extracts.append({k: extract.get(k) for k in ("relevant", "category", "title", "message_kind", "sender_kind", "reply_expected", "risk_flags",
                                                        "known_correspondent", "invitation_history", "conversation_state", "disposition", "urgency") if k in extract})
    if extracts:
        entry["extract"] = extracts[-1]
    task_id = summary.get("task_id")
    if task_id:
        try:
            task = store.get_task(task_id)
            entry["task"] = {"id": task_id, "kind": task.get("kind"), "title": task.get("title"), "status": task.get("status"), "due_at": task.get("due_at"),
                             "summary": task.get("summary"), "closed_reason": task.get("closed_reason"), "next_review_at": task.get("next_review_at"),
                             "expectations": [e for e in task.get("expectations") or [] if isinstance(e, dict)][:6]}
            entry["proposals"] = [_proposal_view(p) for p in store.list_proposals(task_id=task_id, limit=20)]
            entry["gates"] = _audit_flags(store, task_id)
        except KeyError:
            pass
    return entry


def render(report: dict[str, Any]) -> str:
    phases = report.get("phases") or [report]
    name = f" {report['name']}" if report.get("name") else ""
    footer: list[str] = []
    if report.get("took_s") is not None:
        models = ", ".join(report.get("models") or []) or "no model"
        footer = ["", f"Took {report['took_s']} s: {report.get('model_calls', 0)} model calls, {report.get('model_s', 0)} s in the model ({models})"]
    if len(phases) == 1:
        return "\n".join([f"Scenario{name} at {report['now']}", ""] + _render_phase(phases[0]) + footer)
    lines = [f"Scenario{name} in {len(phases)} phases"]
    for index, phase in enumerate(phases, 1):
        took = f" ({phase['took_s']} s)" if phase.get("took_s") is not None else ""
        lines.extend(["", f"=== Phase {index} at {phase.get('at')}{took}"])
        lines.extend(_render_phase(phase))
    return "\n".join(lines + footer)


def _render_phase(report: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    between = report.get("between")
    if between:
        lines.append("Meanwhile: " + ", ".join(f"{k}={v}" for k, v in between.items() if k not in ("reviews", "messages")))
        for review in between.get("reviews") or []:
            lines.append(f"  review of {review.get('task')} at {review.get('at')}: " + ", ".join(f"{k}={v}" for k, v in review.items() if k not in ("task", "at")))
        lines.append("What you would have received meanwhile:")
        if between.get("messages"):
            for message in between["messages"]:
                lines.append(f"  at {message.get('at')}:")
                lines.extend(_messages([message]))
        else:
            lines.append("  nothing")
    lines.append("Signals:")
    for signal in report["signals"]:
        lines.append(f"  - [{signal['kind']}] {signal['title'][:90]} → {signal['triage']}")
    for entry in report["bundles"]:
        lines.extend(["", f"Bundle {entry['group']}:"])
        outcome = entry.get("outcome") or {}
        lines.append("  outcome: " + ", ".join(f"{k}={v}" for k, v in outcome.items() if k != "candidates"))
        if outcome.get("candidates"):
            lines.append("  candidates: " + "; ".join(f"{c.get('title')}" + (f" (overlap {c['overlap']})" if c.get("overlap") else "") for c in outcome["candidates"]))
        extract = entry.get("extract")
        if extract:
            lines.append("  extract: " + ", ".join(f"{k}={v}" for k, v in extract.items() if k != "invitation_history"))
            if extract.get("invitation_history"):
                lines.append(f"  invitation history: {extract['invitation_history']}")
        task = entry.get("task")
        if task:
            lines.append(f"  task: [{task['kind']}] {task['title']} — {task['status']}" + (f" (due {task['due_at']})" if task.get("due_at") else ""))
            if task.get("summary"):
                lines.append(f"    {task['summary'][:300]}")
            for expectation in task.get("expectations") or []:
                held = f" → then: {expectation['then']}" if expectation.get("then") else ""
                by = f" by {expectation['by']}" if expectation.get("by") else ""
                lines.append(f"    expects [{expectation.get('kind') or '?'}]: {expectation.get('description')}{by}{held}")
            if task.get("next_review_at"):
                lines.append(f"    next review {task['next_review_at']}")
        auto = False
        for proposal in entry.get("proposals") or []:
            extras = " ".join(f"{k}={proposal[k]}" for k in ("ask", "basis", "then", "when", "importance") if proposal.get(k))
            lines.append(f"  step: {proposal['title']} [{proposal['operation'] or proposal['class']} · {proposal['batch']} · {proposal['status']}] {extras}".rstrip())
            auto = auto or proposal.get("batch") == "auto"
        if auto:
            lines.append("  note: a step marked auto is Radar's own work: in production a job runs it and its result re-plans the task; the simulator runs no jobs")
        if entry.get("gates"):
            lines.append("  gates: " + ", ".join(f"{k}={v}" for k, v in entry["gates"].items()))
    lines.extend(["", "What you would receive:"])
    lines.extend(_messages(report["would_send"]))
    if report.get("replies"):
        lines.extend(["", "Your replies:"])
        for reply in report["replies"]:
            lines.append(f"  you: {reply['text']}")
            lines.append(f"    read as {reply.get('intent')} → {reply.get('outcome')}: {reply.get('answer')}")
            if reply.get("learnt"):
                lines.append("    learnt: " + "; ".join(reply["learnt"]))
        after = report.get("after_replies") or {}
        for entry in after.get("bundles") or []:
            lines.append(f"  re-plan {entry['group']}: " + ", ".join(f"{k}={v}" for k, v in (entry.get("outcome") or {}).items()))
        lines.extend(["", "What you would receive after that:"])
        lines.extend(_messages(after.get("would_send") or []))
    return lines


def _messages(messages: list[dict[str, Any]]) -> list[str]:
    if not messages:
        return ["  nothing"]
    lines = []
    for message in messages:
        lines.append(f"  [{message['kind']}] {message['text']}")
        if message.get("buttons"):
            lines.append("    buttons: " + " | ".join(message["buttons"]))
    return lines


async def run_files(ctx: Any, cfg: dict[str, Any], paths: list[str], *, fresh: bool = False, now_text: str = "", hermes_home: str = "") -> list[dict[str, Any]]:
    """Several scenarios in one go, side by side (each on its own scratch copy): one host start-up for all of them,
    and the model calls of one overlap the waits of the others."""
    return list(await asyncio.gather(*(run_file(ctx, cfg, path, fresh=fresh, now_text=now_text, hermes_home=hermes_home) for path in paths)))


async def run_file(ctx: Any, cfg: dict[str, Any], path: str, *, fresh: bool = False, now_text: str = "", hermes_home: str = "") -> dict[str, Any]:
    scenario = load_scenario(path)
    now_dt = parse_moment(now_text or scenario.get("now"), datetime.now(timezone.utc))
    profile = str(getattr(ctx, "profile_name", "") or "default")
    owner = str((cfg.get("owner") or {}).get("id") or "owner")
    store = scratch_store(hermes_home, Scope(profile, owner), fresh=fresh, name=Path(path).stem)
    config = sim_config(cfg)
    if fresh:
        destination = config.get("destination") or {}
        store.verify_destination(destination_id="configured:primary", platform="telegram", gateway_connection_ref="telegram:default",
                                 address=dict(destination.get("address") or {"chat_id": "0"}), owner_platform_id=str(destination.get("owner_platform_id") or "0"),
                                 kind="private", capabilities={"text": True, "buttons": True}, now=iso(now_dt))
    rt = Runtime(ctx, store, config, worker_id="sim", clock=lambda: now_dt, transports=TransportRegistry(), hermes_home=hermes_home or None)
    rt.sources = {}
    try:
        report = await run_scenario(rt, scenario, now_dt)
    finally:
        store.close()
    report["name"] = Path(path).stem
    report["expectations"] = expectations.evaluate(scenario, report)
    report["summary"] = "\n".join([render(report), *expectations.render(report["expectations"])])
    return report
