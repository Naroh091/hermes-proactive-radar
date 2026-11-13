"""Task correlation, checklist gaps, checks and proposals for one bundle or one task review."""
from __future__ import annotations

import json
from typing import Any

from ..i18n import language_name
from ..llm import LlmError, LlmGate
from .checklists import template_for
from .schemas import ACTION_CLASSES, DISPOSITIONS, DRAFT_BASES, PLAN_SCHEMA, TASK_KINDS, URGENCIES
from ..prompts import PLAN_PROMPT
from ..reversibility import effective_class

def fact_origin(fact: dict[str, Any]) -> str:
    """owner (stated by the owner), verified (proved by a check) or inferred (a model's reading): facts
    recorded before origins existed count as inferred, which is what they were."""
    provenance = fact.get("provenance") if isinstance(fact.get("provenance"), dict) else {}
    origin = str(provenance.get("origin") or "")
    return origin if origin in ("owner", "verified", "inferred") else "inferred"


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return " ".join(text.split())[:limit]


def _clip_words(value: Any, limit: int) -> str:
    """Clip prose at a word boundary with an ellipsis, so an overlong model answer still reads whole."""
    text = " ".join((value if isinstance(value, str) else "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def _signal_view(signal: dict[str, Any]) -> dict[str, Any]:
    """A signal as the planner sees it: with the mailbox that received a mail and its sender, so a question can name
    the mailbox and the owner can treat the same sender differently per account."""
    meta = signal.get("meta") if isinstance(signal.get("meta"), dict) else {}
    view = {"id": signal.get("id"), "kind": signal.get("kind"), "occurred_at": signal.get("occurred_at"),
            "title": _clip(signal.get("title"), 200), "snippet": _clip(signal.get("snippet"), 600)}
    if meta.get("account"):
        view["account"] = _clip(meta.get("account"), 120)
    if meta.get("sender"):
        view["sender"] = _clip(meta.get("sender"), 120)
    if isinstance(meta.get("attachments"), list) and meta["attachments"]:
        view["attachments"] = [_clip(a, 120) for a in meta["attachments"][:10] if str(a or "").strip()]
    return view


def build_input(
    *, now: str, owner: dict[str, Any], trust_level: str, extract: dict[str, Any] | None,
    signals: list[dict[str, Any]], candidates: list[dict[str, Any]], facts: list[dict[str, Any]],
    preferences: list[dict[str, Any]], existing_task: dict[str, Any] | None, recall: dict[str, Any] | None = None,
    implications: list[dict[str, Any]] | None = None, history: list[dict[str, Any]] | None = None,
    later_evidence: list[dict[str, Any]] | None = None, owner_activity: list[dict[str, Any]] | None = None,
    repair: list[str] | None = None,
) -> str:
    kind = (existing_task or {}).get("kind") or (extract or {}).get("category") or "other"
    payload: dict[str, Any] = {
        "now": now,
        "owner": {"timezone": owner.get("timezone", "UTC"), "language": owner.get("language", "es")},
        "trust_level": trust_level,
        "bundle": extract or {},
        "signals": [_signal_view(s) for s in signals[:8]],
        "candidates": candidates[:8],
        "facts": [{"key": f.get("key"), "value": f.get("value"), "origin": fact_origin(f)} for f in facts[:40]],
        "preferences": [
            {"id": p.get("id"), "effect": p.get("effect"), "instruction": _clip(p.get("instruction"), 200)}
            for p in preferences[:20]
        ],
        "checklist_template": [c["item"] for c in template_for(kind if kind in TASK_KINDS else "other")],
    }
    if recall and (recall.get("memory") or recall.get("past_sessions")):
        payload["memory"] = _clip(recall.get("memory"), 1500)
        payload["past_sessions"] = [
            {"session_id": s.get("session_id"), "when": s.get("when"), "title": s.get("title"), "snippet": _clip(s.get("snippet"), 400)}
            for s in (recall.get("past_sessions") or [])[:5]
        ]
    if recall and recall.get("precedents"):
        payload["precedents"] = [
            {"session_id": p.get("session_id"), "when": p.get("when"), "title": _clip(p.get("title"), 120), "owner_said": _clip(p.get("owner_said"), 240),
             "exchange": [{"role": t.get("role"), "text": _clip(t.get("text"), 280)} for t in (p.get("exchange") or [])[:3] if isinstance(t, dict)]}
            for p in recall["precedents"][:3] if isinstance(p, dict)
        ]
    if implications:
        payload["implications"] = [
            {"concern": _clip(i.get("concern"), 160), "why": _clip(i.get("why"), 300), "settled_by": i.get("settled_by") or "evidence",
             "memory": _clip(i.get("memory"), 700)}
            for i in implications[:8] if isinstance(i, dict) and i.get("concern")
        ]
    if history:
        payload["history"] = [
            {"title": _clip(h.get("title"), 120), "kind": h.get("kind"), "status": h.get("status"), "when": h.get("when")}
            for h in history[:6] if isinstance(h, dict)
        ]
    if later_evidence:
        payload["later_evidence"] = [dict(item) for item in later_evidence[:5] if isinstance(item, dict)]
    if owner_activity:
        payload["owner_activity"] = [dict(item) for item in owner_activity[:8] if isinstance(item, dict)]
    if repair:
        payload["repair"] = [_clip(r, 400) for r in repair[:4]]  # what was wrong with the previous plan for this same input
    if existing_task:
        payload["existing_task"] = {
            "id": existing_task.get("id"), "kind": existing_task.get("kind"), "title": existing_task.get("title"),
            "summary": existing_task.get("summary"), "status": existing_task.get("status"),
            "due_at": existing_task.get("due_at"), "entities": existing_task.get("entities") or {},
            "expectations": existing_task.get("expectations") or [], "checklist": existing_task.get("checklist") or [],
            "recent_proposals": existing_task.get("recent_proposals") or [],
            "open_steps": existing_task.get("open_steps") or [],
            "declined_by_owner": existing_task.get("declined_by_owner") or [],
            "unanswered_by_owner": existing_task.get("unanswered_by_owner") or [],
        }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _check(raw: dict[str, Any]) -> dict[str, Any]:
    """A check is an inconsistency between two pieces of evidence, which it must quote. One that the owner is asked
    about and names fewer than two is ``unbacked``: a decision or an opinion dressed as a check, whose fixed answers
    («yes, correct» / «no, a mistake») fit nothing: on 2026-11-10 «shall I raise the worker's memory and retry?» and
    «is it right to publish untested products as first-hand experience?» went out under those two buttons. A plan
    written before the field existed (no ``evidence`` key at all) is trusted."""
    check = {"description": _clip_words(raw.get("description"), 300), "severity": raw.get("severity") if raw.get("severity") in ("info", "warning", "high") else "info",
             "ask_user": bool(raw.get("ask_user"))}
    if "evidence" in raw:
        evidence: list[str] = []
        for piece in raw.get("evidence") or []:
            text = _clip_words(piece, 200)
            if text and text.lower() not in {e.lower() for e in evidence}:
                evidence.append(text)
        check["evidence"] = evidence[:3]
        if check["ask_user"] and len(evidence) < 2:
            check["unbacked"] = True
    return check


def defects(plan: dict[str, Any]) -> list[str]:
    """What is malformed in a plan in a way the planner can fix on a second try, as instructions for it; [] when
    nothing is. Each defect has a deterministic fallback in :func:`settle` for when the second try repeats it."""
    out: list[str] = []
    for check in plan.get("checks") or []:
        if check.get("unbacked"):
            out.append(f'The check "{check.get("description")}" quotes no two pieces of evidence that disagree, so it is not a check. If it is a '
                       'decision only the owner can take, make it an "ask" proposal whose two option labels are the two answers; if you can verify it '
                       "yourself, a read step; otherwise drop it.")
    asked = [c for c in plan.get("checks") or [] if c.get("ask_user") and not c.get("unbacked")]
    message = str(plan.get("message") or "")
    if (not plan.get("proposals") and not asked and (plan.get("escalation") or {}).get("decision") == "ask" and ("?" in message or "？" in message)
            and plan["task_match"]["decision"] != "none"):
        out.append('The message asks the owner to choose but the plan offers no way to answer. Add an "ask" proposal whose title is that question and '
                   "whose two option labels are the two answers, or, when nothing is really theirs to decide yet, state the situation without a question.")
    return out


def settle(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan with what stayed malformed made harmless: an unbacked check is kept as a note and not asked."""
    checks = [{**c, "ask_user": False} if c.get("unbacked") else c for c in plan.get("checks") or []]
    return {**plan, "checks": checks}


ACK_MAX = 160  # the word said back to an answer: one short sentence, it also has to fit a toast


def validate_plan(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise LlmError(f"plan is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise LlmError("plan must be an object")
    match = raw.get("task_match") if isinstance(raw.get("task_match"), dict) else {}
    decision = match.get("decision", "none")
    if decision not in ("new", "advances", "completes", "contradicts", "none"):
        raise LlmError(f"task_match.decision out of range: {decision!r}")
    task = raw.get("task") if isinstance(raw.get("task"), dict) else {}
    kind = task.get("kind") if task.get("kind") in TASK_KINDS else "other"
    status = task.get("status") if task.get("status") in ("open", "waiting", "needs_user", "in_progress", "done", "abandoned") else "open"
    try:
        confidence = max(0.0, min(1.0, float(task.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    proposals = []
    for item in raw.get("proposals") or []:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        action_class = item.get("action_class") if item.get("action_class") in ACTION_CLASSES else "read"
        operation = _clip(item.get("operation"), 60)
        options = []
        for opt in item.get("options") or []:
            if isinstance(opt, dict) and opt.get("id") and opt.get("label"):
                option = {"id": _clip(opt["id"], 32).replace(" ", "_").lower(), "label": _clip_words(opt["label"], 60)}
                ack = _clip_words(opt.get("ack"), ACK_MAX) if operation == "ask" and isinstance(opt.get("ack"), str) else ""
                if ack:
                    option["ack"] = ack  # what Radar says back when this answer is tapped; without it, a plain «Noted»
                options.append(option)
        entry = {
            "title": _clip_words(item.get("title"), 160), "rationale": _clip_words(item.get("rationale"), 500),
            "action_class": "read" if operation == "ask" else effective_class(operation, action_class), "operation": operation,
            "when": _clip(item.get("when"), 40), "awaits": _clip_words(item.get("awaits"), 160),
            "urgency": item.get("urgency") if item.get("urgency") in ("low", "normal", "high") else "normal",
            "options": options[:2] if operation == "ask" else options[:3],
        }
        fact = item.get("fact") if isinstance(item.get("fact"), dict) else {}
        if operation == "ask" and fact.get("key"):
            entry["fact"] = {"key": _clip(fact.get("key"), 80), "yes": _clip_words(fact.get("yes"), 300), "no": _clip_words(fact.get("no"), 300)}
        if operation == "ask":
            entry["then"] = item.get("then") if item.get("then") in ("step", "nothing") else ""  # "" = not said: kept
        if operation.startswith("gmail."):
            entry["basis"] = item.get("basis") if item.get("basis") in DRAFT_BASES else ""  # "" = no basis: dropped
        proposals.append(entry)
    notify = raw.get("notify") if isinstance(raw.get("notify"), dict) else {}
    disposition = notify.get("disposition", "digest")
    if disposition not in DISPOSITIONS:
        raise LlmError(f"notify.disposition out of range: {disposition!r}")
    urgency = notify.get("urgency")
    if urgency not in URGENCIES:
        urgency = {"notify_now": "now", "digest": "today", "silent": "whenever"}[disposition]
    importance = notify.get("importance") if notify.get("importance") in ("high", "normal", "low") else "normal"
    escalation = raw.get("escalation") if isinstance(raw.get("escalation"), dict) else {}
    escalation_decision = escalation.get("decision") if escalation.get("decision") in ("handle", "inform", "ask") else "ask"
    return {
        "task_match": {"decision": decision, "task_id": _clip(match.get("task_id"), 80), "why": _clip_words(match.get("why"), 300)},
        "task": {
            "kind": kind, "title": _clip_words(task.get("title"), 200), "summary": _clip_words(task.get("summary"), 1500),
            "status": status, "due_at": _clip(task.get("due_at"), 40), "next_review_at": _clip(task.get("next_review_at"), 40),
            "confidence": confidence,
        },
        "checklist": [
            {"item": _clip(c.get("item"), 200), "status": c.get("status") if c.get("status") in ("done", "missing", "unknown", "not_applicable") else "unknown", "note": _clip_words(c.get("note"), 300)}
            for c in (raw.get("checklist") or []) if isinstance(c, dict) and c.get("item")
        ][:30],
        "checks": [_check(c) for c in (raw.get("checks") or []) if isinstance(c, dict) and c.get("description")][:5],
        "proposals": proposals[:3],
        "notify": {"disposition": disposition, "urgency": urgency, "importance": importance, "reason": _clip_words(notify.get("reason"), 300)},
        "message": _clip_words(raw.get("message"), 800),
        "escalation": {"decision": escalation_decision, "why": _clip_words(escalation.get("why"), 300)},
    }


async def plan(
    gate: LlmGate, *, now: str, owner: dict[str, Any], trust_level: str, extract: dict[str, Any] | None,
    signals: list[dict[str, Any]], candidates: list[dict[str, Any]], facts: list[dict[str, Any]],
    preferences: list[dict[str, Any]], existing_task: dict[str, Any] | None = None, subject_id: str | None = None,
    recall: dict[str, Any] | None = None, implications: list[dict[str, Any]] | None = None, history: list[dict[str, Any]] | None = None,
    later_evidence: list[dict[str, Any]] | None = None, owner_activity: list[dict[str, Any]] | None = None,
    repair: list[str] | None = None,
) -> dict[str, Any]:
    raw = await gate.structured(
        "plan", instructions=PLAN_PROMPT.format(language=language_name(owner.get("language"))),
        input_text=build_input(
            now=now, owner=owner, trust_level=trust_level, extract=extract, signals=signals,
            candidates=candidates, facts=facts, preferences=preferences, existing_task=existing_task, recall=recall,
            implications=implications, history=history, later_evidence=later_evidence, owner_activity=owner_activity,
            repair=repair,
        ),
        schema=PLAN_SCHEMA, purpose="radar.plan", subject_id=subject_id,
    )
    return validate_plan(raw)


async def plan_checked(gate: LlmGate, **kwargs: Any) -> dict[str, Any]:
    """:func:`plan`, and once more when the plan is malformed (see :func:`defects`), telling the planner what to fix.
    The prompt already forbade every one of these and the model still wrote them; the second try costs one more
    call only for the plans that need it, and what it does not fix is settled by the code."""
    first = await plan(gate, **kwargs)
    issues = defects(first)
    if not issues:
        return first
    try:
        second = await plan(gate, **kwargs, repair=issues)
    except LlmError:
        return settle(first)
    return settle(second)
