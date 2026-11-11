"""Learn the owner's habits from what they already have: a read-only pass over their mail and calendar.

The owner (2026-11-06): "we lack a warmup step after onboarding that lets us learn the user's patterns, probably by
reading their mail". Until then Radar started every judgement from nothing: it could not tell a
correspondent of years from a stranger, a gala the owner attends every year from a webinar they never open, or a
monthly invoice from an unexpected charge. The pass reads the last ``warmup.months`` of received mail (senders), sent
mail (recipients, read one by one because the listing carries no To), and past calendar events with the owner's
response, and writes what it found as habits (see ``radar/migrations/014_habits.sql``):

- ``correspondent`` «@domain» or address → mails sent to and received from that party, and when;
- ``recurring`` «party|subject stem» → how often that mail repeats (a monthly invoice, a weekly report);
- ``event_type`` «award», «webinar»… → how the owner answered invitations of that kind;
- ``organizer`` «@domain» → how the owner answered that organizer's invitations.

Everything is deterministic (no model call) and costs provider reads only. The pass runs once on the first tick after
onboarding, again every ``warmup.refresh_days`` in quiet hours, and on demand (``/radar warmup``, ``hermes radar warmup``).
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .pipeline.act import owner_addresses
from .pipeline.correspondents import addresses_in, party_of, sender_of
from .preferences import sender_address, subject_stem

log = logging.getLogger("radar.warmup")

DEFAULT_MONTHS = 12
DEFAULT_REFRESH_DAYS = 7
CALL_SECONDS = 40  # one provider call
PER_SLICE = 100  # the Gmail listing caps at 100 per query: one query per month and folder
SENT_READS = 120  # sent mails read in full to learn their recipients
RECURRING_MIN = 3  # repetitions of one subject from one party before it counts as recurring
RECURRING_CADENCE_DAYS = (5, 45)  # weekly to monthly-ish
META_KIND, META_KEY, REQUEST_KEY = "meta", "warmup", "warmup_requested"

EVENT_TYPES = ("webinar", "conference", "award", "workshop", "meeting", "social", "launch", "course", "other")
_TYPE_WORDS: dict[str, tuple[str, ...]] = {
    "webinar": ("webinar", "webinario", "seminario web", "online session", "sesión online", "sesion online", "livestream", "masterclass"),
    "conference": ("conference", "conferencia", "congress", "congreso", "summit", "cumbre", "symposium", "simposio", "jornada", "jornadas",
                   "forum", "foro", "townhall", "town hall", "council", "panel", "mesa redonda", "roundtable"),
    "award": ("award", "awards", "premio", "premios", "gala", "ceremony", "ceremonia", "prize", "entrega de"),
    "workshop": ("workshop", "taller", "hackathon", "bootcamp", "clinic", "lab"),
    "meeting": ("meeting", "reunión", "reunion", "call", "llamada", "sync", "catch-up", "catchup", "kick-off", "kickoff", "kick off", "review",
                "revisión", "revision", "entrevista", "interview", "1:1", "demo", "show & tell", "show and tell", "conexión", "conexion", "onboarding"),
    "social": ("party", "fiesta", "aniversario", "anniversary", "dinner", "cena", "lunch", "comida", "drinks", "cóctel", "coctel", "cocktail",
               "afterwork", "celebración", "celebracion", "celebration", "brunch", "desayuno", "breakfast", "networking"),
    "launch": ("launch", "lanzamiento", "presentación", "presentacion", "presentation", "release", "estreno", "inauguración", "inauguracion", "opening"),
    "course": ("course", "curso", "training", "formación", "formacion", "class", "clase", "lesson", "lección", "leccion", "tutorial"),
}
_TYPE_RE = {kind: re.compile(r"(?<![\w])(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w])") for kind, words in _TYPE_WORDS.items()}


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def event_type_of(title: Any) -> str:
    """The kind of event a title names, in the owner's languages or English: webinar, conference, award, workshop,
    meeting, social, launch, course, or other. Deterministic, so past events and new invitations get the same label."""
    lowered = " ".join(str(title or "").lower().split())
    for kind in EVENT_TYPES[:-1]:
        if _TYPE_RE[kind].search(lowered):
            return kind
    return "other"


def _slices(since: datetime, until: datetime) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    start = since
    while start < until:
        end = min(start + timedelta(days=30), until)
        out.append((start, end))
        start = end
    return out


async def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=CALL_SECONDS)


def _party(address: Any, owners: set[str]) -> str:
    bare = sender_address(address)
    if not bare or bare in owners:
        return ""
    return party_of(bare)


def _name_of(sender: Any) -> str:
    text = str(sender or "")
    return text.split("<", 1)[0].strip(" \"'") if "<" in text else ""


async def _mail(provider: Any, since: datetime, now_dt: datetime, owners: set[str], correspondents: dict[str, dict[str, Any]],
                recurring: dict[tuple[str, str], list[float]], subjects: dict[tuple[str, str], str], counts: dict[str, int], reads_left: list[int]) -> None:
    for start, end in _slices(since, now_dt):
        window = f"after:{start:%Y/%m/%d} before:{end:%Y/%m/%d}"
        try:
            received = await _call(provider.search, f"in:inbox {window}", max_results=PER_SLICE)
        except Exception as exc:
            counts["errors"] += 1
            log.info("warmup: inbox %s not listed: %s", window, str(exc)[:120])
            received = []
        for message in received:
            counts["mail_listed"] += 1
            party = _party(getattr(message, "sender", ""), owners)
            if not party:
                continue
            entry = correspondents[party]
            entry["received"] += 1
            stamp = _stamp(message)
            entry["last_received"] = max(entry["last_received"], stamp)
            name = _name_of(getattr(message, "sender", ""))
            if name and name not in entry["names"] and len(entry["names"]) < 5:
                entry["names"].append(name)
            stem = subject_stem(getattr(message, "subject", ""))
            epoch = getattr(message, "epoch", None)
            if stem and epoch:
                recurring[(party, stem)].append(float(epoch))
                subjects.setdefault((party, stem), str(getattr(message, "subject", ""))[:120])
        if reads_left[0] <= 0:
            continue
        try:
            sent = await _call(provider.search, f"in:sent {window}", max_results=PER_SLICE)
        except Exception as exc:
            counts["errors"] += 1
            log.info("warmup: sent %s not listed: %s", window, str(exc)[:120])
            sent = []
        for message in sorted(sent, key=lambda m: float(getattr(m, "epoch", 0) or 0), reverse=True):
            if reads_left[0] <= 0:
                break
            reads_left[0] -= 1
            try:
                full = await _call(provider.read, message.id)
            except Exception as exc:
                counts["errors"] += 1
                log.info("warmup: sent mail %s not read: %s", getattr(message, "id", "?"), str(exc)[:120])
                continue
            counts["mail_read"] += 1
            stamp = _stamp(full) or _stamp(message)
            for address in addresses_in(getattr(full, "to", "")):
                party = _party(address, owners)
                if not party:
                    continue
                entry = correspondents[party]
                entry["sent"] += 1
                entry["last_sent"] = max(entry["last_sent"], stamp)


def _stamp(message: Any) -> str:
    epoch = getattr(message, "epoch", None)
    if epoch:
        try:
            return iso(datetime.fromtimestamp(float(epoch), tz=timezone.utc))[:10]
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


async def _calendar(provider: Any, since: datetime, now_dt: datetime, owners: set[str], types: dict[str, dict[str, int]],
                    organizers: dict[str, dict[str, Any]], counts: dict[str, int]) -> None:
    for start, end in _slices(since, now_dt):
        try:
            events = await _call(provider.list_events, time_min=iso(start), time_max=iso(end))
        except Exception as exc:
            counts["errors"] += 1
            log.info("warmup: calendar %s..%s not listed: %s", start.date(), end.date(), str(exc)[:120])
            continue
        for event in events:
            begins = _parse(event.get("start"))
            if begins is not None and begins > now_dt:
                continue
            counts["events"] += 1
            organizer = sender_address(event.get("organizer"))
            response = str(event.get("my_response") or "").lower()
            bucket = "organized" if organizer and organizer in owners else {"accepted": "accepted", "declined": "declined", "tentative": "accepted"}.get(response, "ignored")
            kind = event_type_of(event.get("title"))
            types.setdefault(kind, {"accepted": 0, "declined": 0, "ignored": 0, "organized": 0})[bucket] += 1
            party = _party(organizer, owners)
            if party:
                entry = organizers.setdefault(party, {"accepted": 0, "declined": 0, "ignored": 0, "organized": 0, "types": {}})
                entry[bucket] += 1
                entry["types"][kind] = int(entry["types"].get(kind, 0)) + 1


def _cadence(epochs: list[float]) -> float | None:
    moments = sorted(set(epochs))
    if len(moments) < RECURRING_MIN:
        return None
    gaps = [(b - a) / 86400 for a, b in zip(moments, moments[1:]) if b > a]
    if not gaps:
        return None
    cadence = statistics.median(gaps)
    low, high = RECURRING_CADENCE_DAYS
    return round(cadence, 1) if low <= cadence <= high else None


async def run(rt: Any, now_dt: datetime, *, months: int | None = None) -> dict[str, Any]:
    """The pass itself. Returns a summary and leaves it as the ``meta/warmup`` habit."""
    store, config = rt.store, rt.config
    settings = config.get("warmup") or {}
    months = int(months or settings.get("months") or DEFAULT_MONTHS)
    since = now_dt - timedelta(days=30 * months)
    owners = owner_addresses(config)
    counts = {"mail_listed": 0, "mail_read": 0, "events": 0, "errors": 0, "sources": 0}
    correspondents: dict[str, dict[str, Any]] = defaultdict(lambda: {"sent": 0, "received": 0, "last_sent": "", "last_received": "", "names": []})
    recurring: dict[tuple[str, str], list[float]] = defaultdict(list)
    subjects: dict[tuple[str, str], str] = {}
    types: dict[str, dict[str, int]] = {}
    organizers: dict[str, dict[str, Any]] = {}
    reads_left = [int(settings.get("sent_reads") or SENT_READS)]
    for adapter in (getattr(rt, "sources", None) or {}).values():
        provider = getattr(adapter, "provider", None)
        if provider is None:
            continue
        if callable(getattr(provider, "search", None)) and callable(getattr(provider, "read", None)):
            counts["sources"] += 1
            await _mail(provider, since, now_dt, owners, correspondents, recurring, subjects, counts, reads_left)
        elif callable(getattr(provider, "list_events", None)):
            counts["sources"] += 1
            await _calendar(provider, since, now_dt, owners, types, organizers, counts)
    now = iso(now_dt)
    written = {"correspondents": 0, "recurring": 0, "event_types": 0, "organizers": 0}
    for party, entry in correspondents.items():
        store.upsert_habit(kind="correspondent", key=party, value=entry, origin="warmup", now=now)
        written["correspondents"] += 1
    for (party, stem), epochs in recurring.items():
        cadence = _cadence(epochs)
        if cadence is None:
            continue
        store.upsert_habit(kind="recurring", key=f"{party}|{stem}", origin="warmup", now=now,
                           value={"party": party, "subject": subjects.get((party, stem), ""), "count": len(set(epochs)), "cadence_days": cadence,
                                  "last_at": iso(datetime.fromtimestamp(max(epochs), tz=timezone.utc))[:10]})
        written["recurring"] += 1
    for kind, tally in types.items():
        store.upsert_habit(kind="event_type", key=kind, value=tally, origin="warmup", now=now)
        written["event_types"] += 1
    for party, entry in organizers.items():
        store.upsert_habit(kind="organizer", key=party, value=entry, origin="warmup", now=now)
        written["organizers"] += 1
    summary = {"ran_at": now, "months": months, **counts, **written}
    store.upsert_habit(kind=META_KIND, key=META_KEY, value=summary, origin="warmup", now=now)
    try:
        store.delete_habits("meta_request")
    except Exception:
        pass
    try:
        store.add_audit(
            audit_id=str(uuid4()), event_type="warmup", subject_type="owner", subject_id=None, correlation_id=None,
            actor_kind="scheduler", decision_code="completed", metadata=summary, now=now,
        )
    except Exception as exc:
        log.debug("warmup audit skipped: %s", exc)
    log.info("warmup: %s", summary)
    return summary


def request(store: Any, now: str, *, by: str = "owner") -> None:
    """Ask for the pass to run on the next tick (the CLI, a command with no loop at hand)."""
    store.upsert_habit(kind="meta_request", key=REQUEST_KEY, value={"at": now, "by": by}, origin="explicit", now=now)


def last_run(store: Any) -> dict[str, Any] | None:
    return store.get_habit(META_KIND, META_KEY)


def due(store: Any, config: dict[str, Any], now_dt: datetime, *, quiet: bool) -> str:
    """Why the pass should run now: "requested", "first" (never ran, right after onboarding) or "refresh" (older than
    ``warmup.refresh_days``, in quiet hours); "" when it should not."""
    settings = config.get("warmup") or {}
    if not settings.get("enabled", True):
        return ""
    if store.get_habit("meta_request", REQUEST_KEY):
        return "requested"
    last = last_run(store)
    if last is None:
        return "first"
    ran = _parse(last.get("ran_at"))
    days = int(settings.get("refresh_days") or DEFAULT_REFRESH_DAYS)
    if quiet and (ran is None or now_dt - ran >= timedelta(days=days)):
        return "refresh"
    return ""


def _attends(habit: dict[str, Any] | None) -> bool:
    if not habit:
        return False
    went = int(habit.get("accepted") or 0) + int(habit.get("organized") or 0)
    return went >= 1 and went >= int(habit.get("declined") or 0)


def invitation_history(store: Any, signals: list[dict[str, Any]], extract: dict[str, Any]) -> dict[str, Any]:
    """What the owner did with invitations like this one before: the event's kind (from its title), how they answered
    that kind and that organizer, and the verdict ``attends`` (they went to such events, or to this organizer's, at
    least as often as they declined). The planner sees it; act.apply lets an invitation through on it."""
    title = str(extract.get("title") or (signals[0].get("title") if signals else "") or "")
    kind = event_type_of(title)
    organizer = party_of(sender_of(signals))
    type_habit = store.get_habit("event_type", kind)
    organizer_habit = store.get_habit("organizer", organizer) if organizer else None
    return {
        "event_type": kind, "organizer": organizer,
        "type_history": {k: v for k, v in (type_habit or {}).items() if k in ("accepted", "declined", "ignored", "organized")},
        "organizer_history": {k: v for k, v in (organizer_habit or {}).items() if k in ("accepted", "declined", "ignored", "organized")},
        "attends": _attends(type_habit) or _attends(organizer_habit),
    }


def describe(summary: dict[str, Any] | None, language: str) -> str:
    from .i18n import t

    if not summary:
        return t(language, "warmup_never")
    return t(language, "warmup_status", at=str(summary.get("ran_at") or "")[:16].replace("T", " "), months=summary.get("months", "?"),
             correspondents=summary.get("correspondents", 0), recurring=summary.get("recurring", 0), events=summary.get("events", 0),
             types=summary.get("event_types", 0))
