"""What else the planner must see before it judges a bundle: the evidence that arrived after it, and the owner's own
activity around it.

Two failures of 2026-11-04 had the same shape. A checkout chat ended at 11:13 with «confirm Google Pay on your phone»;
the merchant's «PEDIDO REALIZADO, TOTAL PAGADO» arrived at 11:14 and opened the task; the chat, held while the session
was alive, was judged at 12:46 and the planner, seeing «payment pending» in the chat and «order placed» in the task,
asked the owner to confirm the payment. And the owner asked their agent at 23:32 to log into eBay; eBay's «password
reset» and «new device» mails arrived at 23:33; the mail bundle was judged on its own and read as an intrusion. The
planner had both halves each time and nobody told it which came first, nor that the other half was the owner's own
doing. So a bundle travels with ``later_evidence`` (what its matched task received after the bundle's last item) and
``owner_activity`` (the owner's chat with their agent in the same minutes), and the prompt says what each means.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..jobs.mark import is_job_prompt

BEFORE = timedelta(minutes=30)  # the owner's chat this long before the bundle's first item is part of its context
AFTER = timedelta(minutes=5)
MAX_LATER = 5
MAX_TURNS = 8
HOST_NOTICE_PREFIXES = ("[IMPORTANT:",)
OWN_KINDS = ("session_turn", "user_reply")  # the owner's own words: not "evidence that arrived later"


def _parse(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def span(signals: list[dict[str, Any]]) -> tuple[datetime, datetime] | None:
    moments = [m for m in (_parse(s.get("occurred_at")) for s in signals) if m is not None]
    return (min(moments), max(moments)) if moments else None


def later_evidence(store: Any, task_ids: list[str], signals: list[dict[str, Any]], *, limit: int = MAX_LATER) -> list[dict[str, Any]]:
    """Signals the matched tasks received after this bundle's last item (mail, events, repository, job results; never
    the owner's own turns), oldest first. What arrived later outranks what the bundle left open."""
    window = span(signals)
    if window is None or not task_ids:
        return []
    _, last = window
    own = {str(s.get("id")) for s in signals}
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task_id in [t for t in task_ids if t][:3]:
        try:
            rows = store.signals_for_task(task_id, limit=20)
        except Exception:
            continue
        for row in rows:
            sid = str(row.get("id") or "")
            at = _parse(row.get("occurred_at"))
            if not sid or sid in own or sid in seen or at is None or at <= last or row.get("kind") in OWN_KINDS:
                continue
            meta = row.get("meta") or {}
            seen.add(sid)
            found.append({
                "task_id": task_id, "at": _iso(at), "kind": row.get("kind"),
                "from": _clip(meta.get("sender") or meta.get("actor") or meta.get("organizer") or "", 80),
                "title": _clip(row.get("title"), 140), "excerpt": _clip(row.get("snippet"), 300),
            })
    found.sort(key=lambda item: item["at"])
    return found[:limit]


def owner_activity(store: Any, signals: list[dict[str, Any]], *, session_id: str = "", limit: int = MAX_TURNS) -> list[dict[str, Any]]:
    """The owner's own chat with their agent from ``BEFORE`` the bundle's first item to ``AFTER`` its last, oldest
    first: what they asked and what the agent answered, in short. The bundle's own turns, the session it is itself a
    conversation of, Radar's jobs and the host's notices are left out."""
    window = span(signals)
    if window is None:
        return []
    first, last = window
    own = {str(s.get("id")) for s in signals}
    try:
        rows = store.signals_in_window(_iso(first - BEFORE), _iso(last + AFTER), kinds=("session_turn",))
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        meta = row.get("meta") or {}
        sid = str(meta.get("session_id") or "")
        said = str(meta.get("user_text") or row.get("snippet") or "")
        if str(row.get("id")) in own or (session_id and sid == session_id) or not said.strip():
            continue
        if is_job_prompt(said) or said.lstrip().startswith(HOST_NOTICE_PREFIXES):
            continue
        out.append({
            "at": str(row.get("occurred_at") or "")[:16], "session": sid[-12:], "owner_said": _clip(said, 200),
            "agent_answered": _clip(meta.get("assistant_text"), 240),
        })
    return out[-limit:]
