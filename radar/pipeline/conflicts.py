"""Conflicts between what a bundle brings and what the owner already has on those days, found without the model.

On 2026-11-07 a hotel in Lisbon from the 20th to the 24th and a parcel due at home on the 22nd were judged one after
the other, in both orders, and the planner never raised the clash because the trip was not among its candidates.
With the dated tasks always among the candidates the planner raises it itself; this module is the net under it: a
delivery dated during a trip, or a trip booked over a delivery, becomes a check of severity high when the plan has
none. The net is strict on purpose: on 2026-11-07 a looser version flagged a claim due on a cancellation deadline and
a parcel «shipped today» against a trip the owner had already cancelled, each of them an interruption for nothing.
Only dates whose labels say what they are (check-in, flight, delivery) count, and only trips of up to two weeks.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..i18n import t
from .correlate import _DAY

AWAY_KINDS = ("trip",)  # what places the owner elsewhere
DATED_KINDS = ("purchase",)  # what binds a day at home: a delivery (a commitment's deadline binds no place)
MAX_TRIP_DAYS = 14
# Labels of the dates that are days of a trip, and of the dates that bind the owner at home. Spanish and English, the
# languages the extractor writes in for this owner; a label that says nothing recognisable is not a day away.
_TRAVEL = re.compile(r"check-?in|check-?out|entrada|salida|\bida\b|vuelta|vuelo|flight|depart|arriv|return|llegada|regreso|estancia|\bstay\b|noche|night|tren|train", re.I)
_DELIVERY = re.compile(r"entrega|deliver|reparto|recogida|pick-?up|\bcita\b|appointment|instalaci|install", re.I)
# A dated entity that is a deadline, not a day of the trip: the free-cancellation limit, a payment date.
_DEADLINE = re.compile(r"cancel|anul|l[ií]mite|plazo|deadline|refund|reembols|pago|payment|vencimiento|due date", re.I)


def _days(dates: Any, pattern: re.Pattern[str]) -> list[str]:
    out: list[str] = []
    for item in dates or []:
        if not isinstance(item, dict) or not pattern.search(str(item.get("label") or "")) or _DEADLINE.search(str(item.get("label") or "")):
            continue
        match = _DAY.match(str(item.get("when") or ""))
        if match:
            out.append(match.group(0))
    return out


def travel_days(item: dict[str, Any]) -> tuple[str, str] | None:
    """The days a trip spans: its dated entities labelled as check-in, check-out, flights or stay, never a deadline;
    None when no date says so, or when the span is a single day or longer than two weeks."""
    days = _days((item.get("entities") or {}).get("dates"), _TRAVEL)
    if not days:
        return None
    first, last = min(days), max(days)
    length = (date.fromisoformat(last) - date.fromisoformat(first)).days
    return (first, last) if 1 <= length <= MAX_TRIP_DAYS else None


def dated_days(item: dict[str, Any]) -> list[str]:
    """The days a delivery or an appointment binds: its dated entities labelled as such, and what it expects by when
    if the expectation speaks of a delivery."""
    days = _days((item.get("entities") or {}).get("dates"), _DELIVERY)
    for expectation in item.get("expectations") or []:
        if isinstance(expectation, dict) and _DELIVERY.search(str(expectation.get("description") or "")):
            match = _DAY.match(str(expectation.get("by") or ""))
            if match:
                days.append(match.group(0))
    return sorted(set(days))


def _pretty(day: str) -> str:
    return f"{day[8:10]}/{day[5:7]}"


def away_checks(extract: dict[str, Any], candidates: list[dict[str, Any]], *, language: str) -> list[dict[str, Any]]:
    """Checks of severity high for the days the bundle and a candidate cannot both hold: a delivery during a candidate
    trip, or a trip bundle over a candidate's delivery day. One check at most."""
    category = str(extract.get("category") or "")
    title = str(extract.get("title") or "")
    if category in DATED_KINDS:
        for day in dated_days(extract):
            for candidate in candidates:
                span = travel_days(candidate) if candidate.get("kind") in AWAY_KINDS else None
                if span and span[0] <= day <= span[1]:
                    return [{"description": t(language, "check_away", item=title, day=_pretty(day), start=_pretty(span[0]), end=_pretty(span[1]),
                                              trip=str(candidate.get("title") or "")), "severity": "high", "ask_user": True}]
    elif category in AWAY_KINDS:
        span = travel_days({"entities": extract.get("entities") or {}})
        if span:
            for candidate in candidates:
                if candidate.get("kind") not in DATED_KINDS:
                    continue
                for day in dated_days(candidate):
                    if span[0] <= day <= span[1]:
                        return [{"description": t(language, "check_away", item=str(candidate.get("title") or ""), day=_pretty(day), start=_pretty(span[0]),
                                                  end=_pretty(span[1]), trip=title), "severity": "high", "ask_user": True}]
    return []


def add_away_checks(plan: dict[str, Any], extract: dict[str, Any] | None, candidates: list[dict[str, Any]], *, language: str) -> dict[str, Any]:
    """The plan with the clash the dates reveal, unless it already raises one of its own (a check of severity high)."""
    if not extract or any(isinstance(c, dict) and c.get("severity") == "high" for c in plan.get("checks") or []):
        return plan
    found = away_checks(extract, candidates, language=language)
    if not found:
        return plan
    return {**plan, "checks": list(plan.get("checks") or []) + found}
