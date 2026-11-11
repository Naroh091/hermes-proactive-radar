"""Whether the owner has ever corresponded with a sender, or with their organisation: the first gate on invitations.

The owner (2026-11-06): of eight invitations raised in a week, one got a yes and three got "ignore it" or "stop
notifying me about this". An invitation is worth raising when there is a history of interaction with that address or that
domain (and, once Radar knows the owner's habits, with that kind of event). The check is deterministic and cheap:
the retained signals first (mail seen from or sent to that party), then one search of the owner's sent mail
through a mail provider, and the verdict is kept for a month in the feedback table so a party is looked up once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import canonical_hash
from ..preferences import sender_address

log = logging.getLogger("radar.correspondents")

CACHE_DAYS = 30
SEARCH_SECONDS = 12
MAX_SENT = 3
# Addresses at these domains say nothing about an organisation: the party is the address itself.
GENERIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.es", "outlook.com", "outlook.es", "live.com", "yahoo.com", "yahoo.es",
    "icloud.com", "me.com", "protonmail.com", "proton.me", "aol.com", "msn.com",
})


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def domain_of(address: str) -> str:
    address = str(address or "").strip().lower()
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def party_of(address: str) -> str:
    """What the owner's correspondence is matched on: the organisation's domain («@eunet.example»), or the full address
    when the domain is a public mail service."""
    address = sender_address(address)
    domain = domain_of(address)
    if not domain:
        return ""
    return address if domain in GENERIC_DOMAINS else f"@{domain}"


def sender_of(signals: list[dict[str, Any]]) -> str:
    """The address the bundle comes from: a mail's sender, a calendar event's organizer."""
    for signal in signals:
        meta = signal.get("meta") or {}
        if meta.get("owner_sent"):
            continue
        for key in ("sender", "organizer", "actor"):
            address = sender_address(meta.get(key))
            if "@" in address:
                return address
    return ""


def _cached(store: Any, party: str, now_dt: datetime) -> bool | None:
    since = iso(now_dt - timedelta(days=CACHE_DAYS))
    try:
        rows = store.list_feedback(target_id=party, since=since, limit=5)
    except Exception:
        return None
    for row in rows:
        if row.get("target_type") == "correspondent" and row.get("action") in ("known", "unknown"):
            return row["action"] == "known"
    return None


def _remember(store: Any, party: str, known: bool, detail: dict[str, Any], now_dt: datetime) -> None:
    now = iso(now_dt)
    try:
        store.add_feedback(
            feedback_id=canonical_hash({"correspondent": party, "at": now})[:32], target_type="correspondent", target_id=party,
            action="known" if known else "unknown", reason=json.dumps(detail, ensure_ascii=False), now=now,
        )
    except Exception as exc:
        log.debug("correspondent verdict not kept for %s: %s", party, exc)


def _mail_providers(rt: Any) -> list[Any]:
    sources = getattr(rt, "sources", None) or {}
    out = []
    for adapter in sources.values():
        provider = getattr(adapter, "provider", None)
        if provider is not None and callable(getattr(provider, "search", None)) and hasattr(provider, "read"):
            out.append(provider)
    return out


async def _sent_to(rt: Any, party: str) -> int:
    """Mails the owner sent to ``party`` according to the mail providers (one short search each, until one hits)."""
    query = f"in:sent to:{party.lstrip('@')}"
    for provider in _mail_providers(rt):
        try:
            found = await asyncio.wait_for(asyncio.to_thread(provider.search, query, max_results=MAX_SENT), timeout=SEARCH_SECONDS)
        except Exception as exc:
            log.info("sent-mail search for %s skipped: %s", party, str(exc)[:120])
            continue
        if found:
            return len(found)
    return 0


async def known_correspondent(rt: Any, signals: list[dict[str, Any]], now_dt: datetime) -> bool | None:
    """True when the owner has corresponded with the bundle's sender or their organisation before this bundle,
    False when nothing shows it, None when there is no sender to judge. Never raises."""
    store = rt.store
    address = sender_of(signals)
    party = party_of(address)
    if not party:
        return None
    try:
        habit = store.get_habit("correspondent", party)  # what the warmup pass read in the owner's own mail
    except Exception:
        habit = None
    if habit and (int(habit.get("sent") or 0) or int(habit.get("received") or 0)):
        return True
    cached = _cached(store, party, now_dt)
    if cached is not None:
        return cached
    earliest = min((str(s.get("occurred_at") or "") for s in signals if s.get("occurred_at")), default=iso(now_dt))
    try:
        seen = store.correspondence_count(party, before=earliest, exclude=tuple(str(s.get("id")) for s in signals))
    except Exception as exc:
        log.info("correspondence of %s not counted: %s", party, exc)
        seen = 0
    sent = 0 if seen else await _sent_to(rt, party)
    known = seen > 0 or sent > 0
    _remember(store, party, known, {"address": address, "seen": seen, "sent": sent}, now_dt)
    return known


_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def addresses_in(text: Any) -> list[str]:
    return [m.lower() for m in _ADDRESS.findall(str(text or ""))]
