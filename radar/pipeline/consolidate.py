"""Consolidation — the "dreaming" pass.

Once a day, during the owner's quiet hours, Radar reviews its open tasks and merges clusters that are really one
subject (the same trip, the same invoice, the same incident, the same commitment) into a single task, so the board
stops accumulating near-duplicates created over days from different signals. Like memory consolidation in sleep: it
runs on its own and leaves one clean record.

Two gates keep it safe. A deterministic pass proposes clusters (shared repo, identifier or PR, or high title-stem
overlap within one kind); the model then confirms a cluster is genuinely one subject before anything is merged, and
may exclude members that don't belong. The survivor is chosen to keep the live decisions, a task with work in flight
is never folded away, and folded tasks become ``abandoned`` with a ``merged_into`` pointer — reversible, nothing is
deleted. It runs silently: the only trace is the audit trail.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ..llm import BudgetExhausted, LlmError
from .reconcile import stems
from ..prompts import CONSOLIDATE_PROMPT

log = logging.getLogger(__name__)

OPEN_STATUSES = ("open", "waiting", "needs_user", "in_progress", "snoozed")
OPEN_PROPOSALS = ("pending", "sent", "snoozed", "waiting", "accepted", "executing")
INFLIGHT = ("waiting", "accepted", "executing")
DECISION_OPS = ("ask", "check")
STATUS_RANK = {"needs_user": 4, "in_progress": 3, "open": 2, "waiting": 1, "snoozed": 0}

CONSOLIDATE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "same_subject": {"type": "boolean"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "reason": {"type": "string"},
        "exclude_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["same_subject", "title", "summary", "reason"],
}

def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _lang(config: dict[str, Any]) -> str:
    return "English" if str((config.get("owner") or {}).get("language") or "es") == "en" else "español"


def _hours_since(then_iso: str, now_dt: datetime) -> float:
    try:
        parsed = datetime.fromisoformat(str(then_iso).replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now_dt - parsed).total_seconds() / 3600.0


def _view(task: dict[str, Any]) -> dict[str, Any]:
    e = task.get("entities") or {}
    return {
        "id": task.get("id"), "kind": task.get("kind"), "status": task.get("status"),
        "title": task.get("title"), "summary": (task.get("summary") or "")[:300],
        "repo": e.get("repo") or "", "pr": e.get("pr") or "",
        "identifiers": [i.get("value") for i in (e.get("identifiers") or []) if isinstance(i, dict) and i.get("value")][:4],
    }


def _similar(a: frozenset[str], b: frozenset[str], min_overlap: float) -> bool:
    """Two titles share a subject when they have at least two significant word-stems in common and those cover at
    least ``min_overlap`` of the shorter title. The overlap coefficient (not Jaccard) is used on purpose: "Reconocimiento
    médico bianual – decidir" and "Renuncia a reconocimiento médico bianual bloqueada" share the subject phrase but
    diverge in their tails, which would sink a Jaccard score. This is only a pre-filter; the model confirms the subject."""
    if not a or not b:
        return False
    shared = len(a & b)
    return shared >= 2 and shared / min(len(a), len(b)) >= min_overlap


def _similar_across(a: frozenset[str], b: frozenset[str], min_overlap: float) -> bool:
    """Across kinds the bar is higher: three shared stems, not two. The same event filed once as a commitment
    ("European Young Engineer award finalist – confirm Berlin 3 Dec") and once as a trip ("Trip to Berlin
    3-4 Dec as European Young Engineer finalist") never clustered until 2026-11-06 because kinds were compared apart; the
    owner merged them by hand."""
    if not a or not b:
        return False
    shared = len(a & b)
    return shared >= 3 and shared / min(len(a), len(b)) >= min_overlap


def cluster_tasks(tasks: list[dict[str, Any]], min_overlap: float) -> list[list[dict[str, Any]]]:
    """Union tasks that share a repo+kind, a PR, an identifier value, enough title word-stems within one kind, or a
    strong title overlap across kinds. Returns the clusters of two or more. Deterministic; the model confirms each
    before anything merges."""
    parent = {t["id"]: t["id"] for t in tasks}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    buckets: dict[tuple, list[str]] = {}
    for t in tasks:
        e = t.get("entities") or {}
        keys: list[tuple] = []
        if e.get("repo"):
            keys.append(("repo", str(e["repo"]).lower(), str(t.get("kind"))))
        if e.get("pr"):
            keys.append(("pr", str(e["pr"]).lower()))
        for idf in (e.get("identifiers") or []):
            if isinstance(idf, dict) and idf.get("value"):
                keys.append(("id", str(idf["value"]).lower()))
        for key in keys:
            buckets.setdefault(key, []).append(t["id"])
    for ids in buckets.values():
        for other in ids[1:]:
            union(ids[0], other)

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        by_kind.setdefault(str(t.get("kind")), []).append(t)
    for group in by_kind.values():
        cached = [(t["id"], stems(t.get("title"))) for t in group]
        for i in range(len(cached)):
            for j in range(i + 1, len(cached)):
                if _similar(cached[i][1], cached[j][1], min_overlap):
                    union(cached[i][0], cached[j][0])
    everything = [(t["id"], str(t.get("kind")), stems(t.get("title"))) for t in tasks]
    for i in range(len(everything)):
        for j in range(i + 1, len(everything)):
            if everything[i][1] != everything[j][1] and _similar_across(everything[i][2], everything[j][2], min_overlap):
                union(everything[i][0], everything[j][0])

    clusters: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        clusters.setdefault(find(t["id"]), []).append(t)
    return [c for c in clusters.values() if len(c) >= 2]


def _weight(store: Any, task: dict[str, Any]) -> dict[str, Any]:
    try:
        props = store.list_proposals(task_id=task["id"], statuses=OPEN_PROPOSALS, limit=50)
    except Exception:
        props = []
    inflight = any(p.get("status") in INFLIGHT for p in props)
    decisions = sum(1 for p in props if (p.get("operation_id") in DECISION_OPS or p.get("action_class") == "external"))
    return {"inflight": inflight, "decisions": decisions, "rank": STATUS_RANK.get(str(task.get("status")), 0),
            "updated": str(task.get("updated_at") or "")}


def choose_survivor(store: Any, members: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The survivor keeps the live decisions: prefer work in flight, then open decisions, then status, then recency.
    A non-survivor that has work in flight is never folded away — it stays separate."""
    weighed = [(m, _weight(store, m)) for m in members]
    weighed.sort(key=lambda mw: (mw[1]["inflight"], mw[1]["decisions"], mw[1]["rank"], mw[1]["updated"]), reverse=True)
    survivor = weighed[0][0]
    dups = [m for (m, w) in weighed[1:] if not w["inflight"]]
    return survivor, dups


async def run(store: Any, config: dict[str, Any], gate: Any, now_dt: datetime) -> dict[str, Any]:
    """One consolidation pass. Returns a small summary; writes only audit trail. Never raises on a single cluster."""
    cfg = (config or {}).get("consolidation") or {}
    if not cfg.get("enabled", True) or gate is None or not getattr(gate, "available", False):
        return {}
    every = int(cfg.get("every_hours", 24) or 24)
    last = store.last_audit_at(event_type="consolidation")
    if last and _hours_since(last, now_dt) < every:
        return {}
    now = iso(now_dt)
    tasks = store.list_tasks(statuses=OPEN_STATUSES, limit=int(cfg.get("max_tasks", 150)), include_muted=False)
    clusters = cluster_tasks(tasks, float(cfg.get("min_overlap", 0.6)))[: int(cfg.get("max_clusters", 8))]
    clusters_merged = 0
    merged_total = 0

    def _trace(code: str, cluster: list[dict[str, Any]], **metadata: Any) -> None:
        # Every cluster leaves its verdict in the audit trail: three nights of «noop, clusters_seen: 2» (2026-11-03
        # to 05) said nothing about which tasks were seen or why they stayed apart.
        store.add_audit(
            audit_id=str(uuid4()), event_type="consolidation", subject_type="task", subject_id=cluster[0]["id"],
            correlation_id=None, actor_kind="scheduler", decision_code=code,
            metadata={"members": [{"id": t["id"], "kind": t.get("kind"), "title": str(t.get("title") or "")[:80]} for t in cluster], **metadata}, now=now,
        )

    for cluster in clusters:
        try:
            verdict = await gate.structured(
                "consolidate", instructions=CONSOLIDATE_PROMPT.format(language=_lang(config)),
                input_text=_dump([_view(t) for t in cluster]), schema=CONSOLIDATE_SCHEMA, purpose="radar.consolidate",
                subject_id=cluster[0]["id"],
            )
        except BudgetExhausted:
            break
        except LlmError as exc:
            log.info("consolidate skipped a cluster: %s", exc)
            _trace("cluster_skipped", cluster, error=str(exc)[:200])
            continue
        if not verdict.get("same_subject"):
            _trace("cluster_rejected", cluster, reason=str(verdict.get("reason") or "")[:300])
            continue
        exclude = {str(x) for x in (verdict.get("exclude_ids") or [])}
        members = [t for t in cluster if t["id"] not in exclude]
        if len(members) < 2:
            continue
        survivor, dups = choose_survivor(store, members)
        if not dups:
            _trace("cluster_kept_apart", cluster, reason="every other member has work in flight")
            continue
        title = (verdict.get("title") or survivor.get("title") or "")[:200]
        summary = verdict.get("summary") or survivor.get("summary") or ""
        try:
            store.update_task(survivor["id"], now=now, title=title, summary=summary)
            for dup in dups:
                store.merge_task_into(dup["id"], survivor["id"], now=now)
        except Exception as exc:
            log.warning("consolidate merge failed: %s", exc)
            continue
        store.add_audit(
            audit_id=str(uuid4()), event_type="consolidation", subject_type="task", subject_id=survivor["id"],
            correlation_id=None, actor_kind="scheduler", decision_code="consolidated",
            metadata={"survivor": survivor["id"], "merged": [d["id"] for d in dups], "count": len(dups) + 1,
                      "reason": str(verdict.get("reason") or "")[:300]}, now=now,
        )
        clusters_merged += 1
        merged_total += len(dups)
    if clusters_merged == 0:
        # Mark the run so the daily cadence holds even when nothing was mergeable (any 'consolidation' audit counts).
        store.add_audit(
            audit_id=str(uuid4()), event_type="consolidation", subject_type="task", subject_id=None,
            correlation_id=None, actor_kind="scheduler", decision_code="noop",
            metadata={"clusters_seen": len(clusters), "clusters": [[t["id"] for t in cluster] for cluster in clusters]}, now=now,
        )
    return {"clusters": clusters_merged, "merged": merged_total}


def _dump(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
