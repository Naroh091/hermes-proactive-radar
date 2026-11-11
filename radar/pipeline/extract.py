"""Structured extraction of one bundle of signals."""
from __future__ import annotations

import json
from typing import Any

from ..i18n import language_name
from ..llm import LlmError, LlmGate
from ..redact import redact
from .conversation import CONVERSATION_STATES
from .schemas import CATEGORIES, DISPOSITIONS, EXTRACT_SCHEMA, MESSAGE_KINDS, REPLY_EXPECTED, RISK_FLAGS, SENDER_KINDS, SETTLED_BY
from ..prompts import EXTRACT_PROMPT

MAX_IMPLICATIONS = 8

MAX_BODY = 6000
MAX_THREAD = 3000
MAX_TOTAL = 18000

def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    text = " ".join(text.split())
    return redact(text[:limit])


def signal_view(signal: dict[str, Any], *, budget: int) -> dict[str, Any]:
    meta = signal.get("meta") or {}
    body = signal.get("raw_content") or meta.get("body") or signal.get("snippet") or ""
    view: dict[str, Any] = {
        "id": signal.get("id"),
        "kind": signal.get("kind"),
        "occurred_at": signal.get("occurred_at"),
        "title": _clip(signal.get("title"), 300),
    }
    for key in ("account", "sender", "to", "session_id", "platform", "repo", "event_type", "automation", "attachments",
                "change", "start", "end", "all_day", "status", "location", "organizer", "attendees", "my_response", "link", "calendar_id", "event_id",
                "action", "number", "actor", "author", "url", "ref", "merged", "draft", "labels", "commits", "conclusion", "environment"):
        if meta.get(key):
            view[key] = meta[key]
    if signal.get("kind") == "session_turn":
        view["user_said"] = _clip(meta.get("user_text"), min(MAX_BODY, budget))
        view["assistant_said"] = _clip(meta.get("assistant_text"), min(2000, max(0, budget - len(view["user_said"]))))
    else:
        view["body"] = _clip(body, min(MAX_BODY, budget))
        view["body_truncated"] = bool(isinstance(body, str) and len(body) > min(MAX_BODY, budget))
        if meta.get("thread"):
            view["thread"] = _clip(meta.get("thread"), MAX_THREAD)
    return view


def build_input(
    signals: list[dict[str, Any]], *, owner: dict[str, Any], now: str, facts: list[dict[str, Any]],
    open_tasks: list[dict[str, Any]], conversation: dict[str, Any] | None = None,
) -> str:
    budget = MAX_TOTAL
    views = []
    for signal in [] if conversation else signals:
        view = signal_view(signal, budget=max(1000, budget))
        budget -= len(json.dumps(view, ensure_ascii=False))
        views.append(view)
        if budget <= 0:
            break
    payload = {
        "now": now,
        "owner": {"timezone": owner.get("timezone", "UTC"), "language": owner.get("language", "es")},
        "facts": [{"key": f.get("key"), "value": f.get("value")} for f in facts[:40]],
        "open_tasks": [
            {"id": t.get("id"), "kind": t.get("kind"), "title": t.get("title"), "status": t.get("status")}
            for t in open_tasks[:30]
        ],
        "signals": views,
    }
    if conversation:
        payload["conversation"] = conversation
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def validate_extract(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise LlmError(f"extract is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise LlmError("extract must be an object")
    out: dict[str, Any] = {}
    out["relevant"] = bool(raw.get("relevant", False))
    category = raw.get("category", "noise")
    if category not in CATEGORIES:
        raise LlmError(f"category out of range: {category!r}")
    out["category"] = category
    out["title"] = _clip(raw.get("title"), 200)
    out["summary"] = _clip(raw.get("summary"), 1500)
    disposition = raw.get("disposition", "silent")
    if disposition not in DISPOSITIONS:
        raise LlmError(f"disposition out of range: {disposition!r}")
    out["disposition"] = disposition
    out["reason"] = _clip(raw.get("reason"), 600)
    out["urgency"] = raw.get("urgency") if raw.get("urgency") in ("high", "medium", "low") else "low"
    out["impact"] = raw.get("impact") if raw.get("impact") in ("high", "medium", "low", "none") else "none"
    flags = raw.get("risk_flags") or []
    out["risk_flags"] = [f for f in flags if f in RISK_FLAGS] if isinstance(flags, list) else []
    entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
    out["entities"] = {
        "dates": [d for d in entities.get("dates") or [] if isinstance(d, dict)][:20],
        "places": [str(p)[:100] for p in entities.get("places") or []][:20],
        "amounts": [a for a in entities.get("amounts") or [] if isinstance(a, dict)][:20],
        "people": [str(p)[:100] for p in entities.get("people") or []][:20],
        "organizations": [str(o)[:100] for o in entities.get("organizations") or []][:20],
        "identifiers": [i for i in entities.get("identifiers") or [] if isinstance(i, dict)][:20],
        "items": [str(i)[:150] for i in entities.get("items") or []][:20],
        "repo": _clip(entities.get("repo"), 200),
        "url": _clip(entities.get("url"), 500),
    }
    out["expectations"] = [
        {"description": _clip(e.get("description"), 200), "kind": e.get("kind") if e.get("kind") in ("email", "event", "date", "repo", "other") else "other", "by": _clip(e.get("by"), 40)}
        for e in (raw.get("expectations") or []) if isinstance(e, dict) and e.get("description")
    ][:10]
    facts: list[dict[str, Any]] = []
    for item in raw.get("facts_learned") or []:
        if not isinstance(item, dict) or not item.get("key") or not item.get("value"):
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        facts.append({"key": _clip(item["key"], 80), "value": _clip(item["value"], 300), "confidence": max(0.0, min(1.0, confidence))})
    out["facts_learned"] = facts[:10]
    out["proposed_reply"] = str(raw.get("proposed_reply") or "")[:4000]
    out["keep_full_content"] = bool(raw.get("keep_full_content", False))
    out["needs_thread"] = bool(raw.get("needs_thread", False))
    state = raw.get("conversation_state")
    out["conversation_state"] = state if state in CONVERSATION_STATES else "not_a_conversation"
    out["message_kind"] = raw.get("message_kind") if raw.get("message_kind") in MESSAGE_KINDS else "other"
    out["sender_kind"] = raw.get("sender_kind") if raw.get("sender_kind") in SENDER_KINDS else "unknown"
    out["reply_expected"] = raw.get("reply_expected") if raw.get("reply_expected") in REPLY_EXPECTED else "none"
    implications: list[dict[str, Any]] = []
    for item in raw.get("implications") or []:
        if not isinstance(item, dict) or not str(item.get("concern") or "").strip():
            continue
        settled_by = item.get("settled_by") if item.get("settled_by") in SETTLED_BY else "evidence"
        implications.append({
            "concern": _clip(item.get("concern"), 160), "why": _clip(item.get("why"), 300), "settled_by": settled_by,
            "memory_question": _clip(item.get("memory_question"), 200) if settled_by != "evidence" else "",
        })
    out["implications"] = implications[:MAX_IMPLICATIONS]
    if "security" in out["risk_flags"]:
        # A security flag is never filed as noise: the owner hears of it (attention: protected_exception).
        out["relevant"] = True
        if out["category"] == "noise":
            out["category"] = "check"
    return out


_RULE_CATEGORY = {"receipt": "purchase", "request": "work_item", "invitation": "commitment"}


def keep_by_rule(extract: dict[str, Any], *, rule: dict[str, Any]) -> dict[str, Any]:
    """The deterministic floor for a message the model called noise while the owner holds a standing rule about its
    sender or mailbox (a ``prefer`` preference: «register the receipts from this sender that reach my personal
    mailbox»): the item is kept for the planner, which sees the rule and decides what it means here. On 2026-12-09
    (simulated) the extractor, nudged by a recorded «not this one» about the work mailbox, called the personal
    mailbox's receipt noise as well, and the rule never got its chance."""
    out = dict(extract)
    if out.get("category") in (None, "", "noise"):
        out["category"] = _RULE_CATEGORY.get(str(out.get("message_kind") or ""), "other")
    out["relevant"] = True
    out["kept_by_rule"] = {"id": str(rule.get("id") or ""), "instruction": _clip(rule.get("instruction"), 200)}
    return out


def flag_manipulation(extract: dict[str, Any], *, sender: str, excerpt: str, language: Any = "es") -> dict[str, Any]:
    """The deterministic floor for a message that gives the assistant orders (``rules.is_injection``): whatever the
    model made of it, it is a security check for the owner, interrupts, keeps its full text for review, and its
    title and summary say who sent it and what it asked — never what the model may have paraphrased as a task."""
    from ..i18n import t

    out = dict(extract)
    flags = [f for f in out.get("risk_flags") or [] if f != "security"] + ["security"]
    who = _clip(sender, 120) or "?"
    out.update({
        "relevant": True, "category": "check" if out.get("category") in ("noise", "other", "routine") else out.get("category", "check"),
        "disposition": "notify_now", "urgency": "high", "impact": out.get("impact") if out.get("impact") in ("high", "medium") else "medium",
        "risk_flags": flags, "keep_full_content": True, "proposed_reply": "", "facts_learned": [], "implications": [],
        "manipulation": True, "title": t(language, "manipulation_title", sender=who)[:200],
        "summary": t(language, "manipulation_summary", sender=who, what=_clip(excerpt, 300))[:1500],
        "reason": "manipulation_attempt",
    })
    return out


async def extract(
    gate: LlmGate, signals: list[dict[str, Any]], *, owner: dict[str, Any], now: str,
    facts: list[dict[str, Any]], open_tasks: list[dict[str, Any]], subject_id: str | None = None,
    conversation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prompt = EXTRACT_PROMPT.format(language=language_name(owner.get("language")))
    raw = await gate.structured(
        "extract", instructions=prompt,
        input_text=build_input(signals, owner=owner, now=now, facts=facts, open_tasks=open_tasks, conversation=conversation),
        schema=EXTRACT_SCHEMA, purpose="radar.extract", subject_id=subject_id,
    )
    return validate_extract(raw)
