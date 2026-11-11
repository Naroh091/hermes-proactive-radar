"""Candidate open tasks for a freshly extracted bundle (FTS + same-kind recency)."""
from __future__ import annotations

import re
from typing import Any


def _entities_text(entities: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("places", "people", "organizations", "items"):
        parts.extend(str(v) for v in entities.get(key) or [])
    for item in entities.get("identifiers") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("value", "")))
    for item in entities.get("dates") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("when", "")))
    if entities.get("repo"):
        parts.append(str(entities["repo"]))
    return " ".join(parts)


def _identifier_text(entities: dict[str, Any]) -> str:
    parts: list[str] = [str(i.get("value", "")) for i in entities.get("identifiers") or [] if isinstance(i, dict)]
    for key in ("repo", "url"):
        if entities.get(key):
            parts.append(str(entities[key]))
    return " ".join(parts)


def compact_task(task: dict[str, Any]) -> dict[str, Any]:
    checklist = task.get("checklist") or []
    missing = [c.get("item") for c in checklist if isinstance(c, dict) and c.get("status") == "missing"]
    return {
        "id": task.get("id"), "kind": task.get("kind"), "title": task.get("title"),
        "summary": (task.get("summary") or "")[:400], "status": task.get("status"), "due_at": task.get("due_at"),
        "entities": task.get("entities") or {}, "missing": missing[:8],
        "expectations": [e.get("description") for e in (task.get("expectations") or []) if isinstance(e, dict)][:6],
        "updated_at": task.get("updated_at"),
    }


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")
OVERLAP_KINDS = ("trip", "commitment", "purchase")


def day_span(item: dict[str, Any]) -> tuple[str, str] | None:
    """The first and last calendar day an item is bound to: its dated entities, what it expects by when, its due date."""
    days: list[str] = []
    for date in (item.get("entities") or {}).get("dates") or []:
        if isinstance(date, dict):
            match = _DAY.match(str(date.get("when") or ""))
            if match:
                days.append(match.group(0))
    for expectation in item.get("expectations") or []:
        if isinstance(expectation, dict):
            match = _DAY.match(str(expectation.get("by") or ""))
            if match:
                days.append(match.group(0))
    match = _DAY.match(str(item.get("due_at") or ""))
    if match:
        days.append(match.group(0))
    return (min(days), max(days)) if days else None


MAX_OVERLAP_DAYS = 45  # a purchase paid in instalments over months, a warranty: not a «when» the owner is bound to


def overlapping_tasks(store: Any, extract: dict[str, Any], *, exclude: set[str] | None = None, limit: int = 4) -> list[tuple[dict[str, Any], str]]:
    """Open trips, commitments and purchases whose days overlap the bundle's: where the owner will be, or what else is
    due then; trips first, then the tightest spans (a delivery day before a two-month instalment plan). On 2026-11-07
    a parcel due on the 22nd and a hotel in Lisbon from the 20th to the 24th never met: each bundle was judged alone
    and nobody noticed the owner would be away."""
    from datetime import date

    span = day_span({"entities": extract.get("entities") or {}, "expectations": extract.get("expectations") or []})
    if not span:
        return []
    rows: list[tuple[bool, int, dict[str, Any], str]] = []
    for task in store.list_tasks(statuses=OPEN_STATUSES, limit=300, kinds=OVERLAP_KINDS):
        if str(task.get("id") or "") in (exclude or set()):
            continue
        other = day_span(task)
        if not other or max(span[0], other[0]) > min(span[1], other[1]):
            continue
        length = (date.fromisoformat(other[1]) - date.fromisoformat(other[0])).days
        if length > MAX_OVERLAP_DAYS:
            continue
        rows.append((task.get("kind") != "trip", length, task, f"{other[0]}..{other[1]}"))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [(task, label) for _, _, task, label in rows[:limit]]


def history_at_places(store: Any, places: Any, *, exclude_id: str = "", limit: int = 6) -> list[dict[str, Any]]:
    """Tasks of any status that mention the same places, newest first: a trip the owner already made to the
    city settles «has been there before» without asking anyone."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for place in [str(p) for p in (places or []) if str(p or "").strip()][:2]:
        try:
            rows = store.search_tasks(place, limit=5, open_only=False)
        except Exception:
            rows = []
        for task in rows:
            task_id = str(task.get("id") or "")
            if not task_id or task_id == exclude_id or task_id in seen:
                continue
            seen.add(task_id)
            out.append({"id": task_id, "title": task.get("title"), "kind": task.get("kind"), "status": task.get("status"),
                        "when": str(task.get("created_at") or "")[:10]})
    out.sort(key=lambda row: row["when"], reverse=True)
    return out[:limit]


OPEN_STATUSES = ("open", "waiting", "needs_user", "in_progress", "snoozed")
OPEN_STEP_STATUSES = ("pending", "sent", "snoozed", "waiting", "accepted", "executing")


def open_steps(store: Any, task_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """The task's steps still open (waiting for the owner) or running, newest first: what a re-plan must restate
    or withdraw, and what it must never duplicate."""
    if not hasattr(store, "list_proposals"):
        return []
    try:
        rows = store.list_proposals(task_id=task_id, limit=50, newest=True)
    except TypeError:  # a store without ``newest``
        rows = list(reversed(store.list_proposals(task_id=task_id, limit=50)))
    out: list[dict[str, Any]] = []
    for proposal in rows:
        if proposal.get("status") not in OPEN_STEP_STATUSES:
            continue
        out.append({
            "id": proposal.get("id"), "title": proposal.get("title"), "operation": proposal.get("operation_id") or "",
            "action_class": proposal.get("action_class") or "read", "status": proposal.get("status"),
        })
        if len(out) >= limit:
            break
    return out


def candidate_tasks(store: Any, extract: dict[str, Any], *, limit: int = 6, session_id: str = "", session_ids: Any = None, group_key: str = "") -> list[dict[str, Any]]:
    """Open tasks the bundle may refer to. A task already fed by the same thread (``group_key``) comes
    first: a second advisory in the same mail thread is the same task, whatever its wording. A conversation
    bundle starts with the tasks born from the same thread — every session a context compaction split it into,
    not just this window (``session_ids``) — and never gets the same-kind fallback: sharing a kind is not sharing
    a subject."""
    entities = extract.get("entities") or {}
    if session_id:
        # Words shared with an unrelated task mean nothing here; only identifiers, repos and links do.
        query = " ".join(_identifier_text(entities).split())
    else:
        query = " ".join([extract.get("title", ""), extract.get("summary", ""), _entities_text(entities)])
    found: dict[str, dict[str, Any]] = {}
    if group_key and not session_id and hasattr(store, "tasks_for_group"):
        for task in store.tasks_for_group(group_key, statuses=OPEN_STATUSES, limit=limit):
            found[task["id"]] = task
    if session_id:
        thread = [str(s) for s in (session_ids or [session_id]) if s] or [session_id]
        for task in store.tasks_for_sessions(thread, statuses=OPEN_STATUSES, limit=limit):
            found[task["id"]] = task
    for task in store.search_tasks(query, limit=limit) if query else []:
        found.setdefault(task["id"], task)
    overlaps: dict[str, str] = {}
    if not session_id:
        for task, span in overlapping_tasks(store, extract):
            found.setdefault(task["id"], task)
            overlaps[task["id"]] = span  # also when the words already found it: the days are what matter here
    category = extract.get("category")
    if category and category != "noise" and not session_id:
        for task in store.list_tasks(statuses=OPEN_STATUSES, limit=4, kinds=(category,)):
            found.setdefault(task["id"], task)
    # The tasks that share days with the bundle always make the cut: on 2026-11-07 six word matches pushed the parcel
    # due during the Lisbon trip out of the list, and the clash went unseen.
    ordered = list(found.values())
    dated = [task for task in ordered if task["id"] in overlaps][:limit]
    rest = [task for task in ordered if task["id"] not in overlaps][:max(0, limit - len(dated))]
    candidates = [compact_task(task) for task in ordered if task in dated or task in rest]
    for candidate in candidates:
        candidate["open_steps"] = open_steps(store, str(candidate.get("id") or ""), limit=6)
        if candidate.get("id") in overlaps:
            candidate["overlap"] = overlaps[candidate["id"]]  # shares days with the bundle: where the owner will be, or what else is due then
    return candidates
