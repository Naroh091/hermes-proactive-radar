"""Decay instead of a treadmill.

A decision the owner does not answer used to live 72 hours and then be proposed again by the next re-plan, so the
same question came back three times a day. Now a decision unanswered for ``notifications.unanswered_hours`` (48)
expires as unanswered and its task is parked for a week (``snoozed``, review timers cancelled): nothing is asked
again until then, unless a new signal reopens the task, and the planner then sees what went unanswered. News that
was never delivered within a day, or stayed sent, simply stops being news.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..delivery.cadence import parse_moment, stale
from ..models import TASK_TERMINAL
from ..trust import decision_like

log = logging.getLogger(__name__)

UNANSWERED_HOURS = 48
NEWS_HOURS = 24
PARK_DAYS = 7
IDLE_HOURS = 24  # a task left with nothing open, running or scheduled for this long is in limbo


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _busy(store: Any, task_id: str) -> bool:
    """A task with another open or running step, or a job in flight, is not parked: it is still moving."""
    if store.list_proposals(statuses=("pending", "sent", "snoozed", "waiting", "accepted", "executing"), task_id=task_id, limit=1):
        return True
    return bool(store.list_jobs(statuses=("queued", "running"), task_id=task_id, limit=1))


def stale_news(store: Any, config: dict[str, Any], now_dt: datetime) -> list[str]:
    """Queued news whose deadline (the task's ``due_at``, or the end of a ``today`` day) passed while it waited: a
    meeting cancellation is worth nothing after the meeting. Expired as stale, never delivered (2026-11-03, 23:52)."""
    ids: list[str] = []
    for proposal in store.list_proposals(statuses=("pending",), limit=200):
        if proposal.get("operation_id") != "inform" or (proposal.get("params") or {}).get("ask"):
            continue
        try:
            task = store.get_task(proposal["task_id"])
        except KeyError:
            continue
        if stale(task, proposal, config, now_dt):
            ids.append(proposal["id"])
    return ids


def idle(store: Any, now_dt: datetime) -> dict[str, int]:
    """Tasks in limbo: open, with nothing proposed or in flight, no job running and no review date, for a day. The
    planner left them that way (a plan with no steps and no ``next_review_at``), so nothing would ever look at them
    again (nine such tasks on 2026-11-06, the oldest from 2026-10-20). The first time, the task gets a review now;
    if the review leaves it idle again, it sleeps a week like an unanswered one, and wakes to another review."""
    reviewed = parked = 0
    now = iso(now_dt)
    for task in store.idle_tasks(limit=50):
        updated = parse_moment(task.get("updated_at"))
        if updated is None or now_dt - updated < timedelta(hours=IDLE_HOURS):
            continue
        task_id = task["id"]
        seen = any(a.get("decision_code") == "idle_review" for a in store.list_audit(limit=30, subject_id=task_id))
        try:
            if seen:
                if not store.park_task(task_id, until=iso(now_dt + timedelta(days=PARK_DAYS)), now=now, reason="idle"):
                    continue
                parked += 1
                code, metadata = "parked_idle", {"until_days": PARK_DAYS}
            else:
                store.update_task(task_id, now=now, bump_version=False, next_review_at=now)
                reviewed += 1
                code, metadata = "idle_review", {"idle_hours": round((now_dt - updated).total_seconds() / 3600, 1)}
        except (KeyError, ValueError, RuntimeError) as exc:
            log.debug("idle task %s not handled: %s", task_id, exc)
            continue
        store.add_audit(
            audit_id=str(uuid4()), event_type="pipeline", subject_type="task", subject_id=task_id, correlation_id=None,
            actor_kind="scheduler", decision_code=code, metadata=metadata, now=now,
        )
    return {"reviewed": reviewed, "parked": parked}


def run(store: Any, config: dict[str, Any], now_dt: datetime) -> dict[str, int]:
    notifications = config.get("notifications") or {}
    hours = float(notifications.get("unanswered_hours") or UNANSWERED_HOURS)
    now = iso(now_dt)
    result = store.decay_proposals(now, unanswered_before=iso(now_dt - timedelta(hours=hours)), news_before=iso(now_dt - timedelta(hours=NEWS_HOURS)))
    outdated = store.expire_proposals_by_id(stale_news(store, config, now_dt), now=now, reason="stale_news")
    parked = 0
    siblings = 0
    for task_id in result["task_ids"]:
        try:
            task = store.get_task(task_id)
        except KeyError:
            continue
        # The decisions queued behind the one the owner ignored share its fate: a variant of an unanswered question,
        # released a day later as «sigue pendiente», is what the owner got on 2026-11-04 about eBay. They expire as
        # unanswered too (the planner sees them as such), and the task, no longer busy, can sleep.
        behind = [p["id"] for p in store.list_proposals(statuses=("pending", "snoozed"), task_id=task_id, limit=20)
                  if p.get("batch_key") != "auto" and decision_like(p)]
        siblings += store.expire_proposals_by_id(behind, now=now, reason="unanswered")
        if task.get("status") in TASK_TERMINAL or task.get("muted") or _busy(store, task_id):
            continue
        try:
            if store.park_task(task_id, until=iso(now_dt + timedelta(days=PARK_DAYS)), now=now):
                parked += 1
                store.add_audit(
                    audit_id=str(uuid4()), event_type="pipeline", subject_type="task", subject_id=task_id, correlation_id=None,
                    actor_kind="scheduler", decision_code="parked_unanswered", metadata={"until_days": PARK_DAYS, "hours": hours}, now=now,
                )
        except (KeyError, ValueError, RuntimeError) as exc:
            log.debug("task %s not parked: %s", task_id, exc)
    limbo = idle(store, now_dt)
    return {"unanswered": len(result["unanswered"]) + siblings, "stale_news": len(result["stale_news"]) + outdated, "parked": parked,
            "idle_reviewed": limbo["reviewed"], "idle_parked": limbo["parked"]}
