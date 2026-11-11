"""When «remind me later» lands, and whether it is offered at all.

«Remind me tomorrow» means the start of the next waking day (paced reminders) or the first summary slot of the next
day. That is wrong when what is pending falls before then: on 2026-11-13 the check-in of a flight leaving at 08:30 the
next morning was offered «remind me tomorrow», which would have fired after the plane. With a deadline that does not
leave a night, the snooze is shorter (part of the time left, between one and six hours) and its button says «later»,
not «tomorrow»; with less than an hour of margin left there is nothing to postpone and the button is not offered.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..i18n import t
from .cadence import next_delivery, next_morning, paced

SNOOZE_HOURS = 24  # without pacing or slots: a plain day
SNOOZE_LEAD = timedelta(hours=3)  # the snooze ends at least this long before the deadline
SNOOZE_MIN = timedelta(hours=1)
SNOOZE_MAX = timedelta(hours=6)


def parse_moment(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def deadline_of(task: dict[str, Any], now_dt: datetime | None = None) -> str:
    """The next moment the task is bound to, as ISO, or "": the earliest still ahead among its due date, the ``by`` of
    what it expects and its dated entities. A trip's due date is its return, so a flight tomorrow at 08:30 is found in
    the dates, not in due_at; a far date is harmless (the snooze stays a night)."""
    now = now_dt or datetime.now(timezone.utc)
    moments = [parse_moment(task.get("due_at"))]
    moments += [parse_moment(e.get("by")) for e in task.get("expectations") or [] if isinstance(e, dict)]
    moments += [parse_moment(d.get("when")) for d in (task.get("entities") or {}).get("dates") or [] if isinstance(d, dict)]
    ahead = [m for m in moments if m is not None and m > now]
    return min(ahead).astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if ahead else ""


MORNING = timedelta(hours=8)  # «tomorrow» for an owner with no quiet hours: 08:00, never midnight


def standard_snooze(now_dt: datetime, config: dict[str, Any]) -> datetime:
    """«Tomorrow»: the start of the next waking day (paced) or the first summary slot of the next day. Without quiet
    hours the waking day starts at midnight, which is not what anyone means by tomorrow: 08:00 then."""
    if paced(config):
        until = next_morning(now_dt, config)
        if until.hour == 0 and until.minute == 0:
            until += MORNING
        return until
    return next_delivery(now_dt, "whenever", config) or (now_dt + timedelta(hours=SNOOZE_HOURS))


def snooze_until(now_dt: datetime, config: dict[str, Any], due_at: Any = None) -> datetime | None:
    """When a snoozed thing comes back: tomorrow, unless ``due_at`` (the task's deadline) falls before tomorrow leaves
    a margin; then part of the time left, and None when less than an hour of margin remains."""
    until = standard_snooze(now_dt, config)
    due = parse_moment(due_at)
    if due is None:
        return until
    limit = due - SNOOZE_LEAD
    if until <= limit:
        return until
    left = limit - now_dt
    if left < SNOOZE_MIN:
        return None
    return now_dt + max(SNOOZE_MIN, min(SNOOZE_MAX, left / 2))


def later_button(task_id: str, language: str, *, due_at: Any = None, now_dt: datetime | None = None, config: dict[str, Any] | None = None,
                 prefix: str = "pt") -> dict[str, str] | None:
    """The «later» button of a task: «remind me tomorrow»; «remind me later» when the deadline does not leave a night;
    none when it leaves no time to postpone anything."""
    data = f"{prefix}:{task_id}:later"[:64]
    if not due_at or now_dt is None or config is None:
        return {"label": t(language, "btn_later"), "data": data}
    until = snooze_until(now_dt, config, due_at)
    if until is None:
        return None
    short = until < standard_snooze(now_dt, config)
    return {"label": t(language, "btn_later_soon" if short else "btn_later"), "data": data}
