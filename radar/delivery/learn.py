"""Turn the owner's replies into stored attention preferences, and manage them.

A preference never executes anything: it only raises or lowers how loudly a kind of signal is surfaced
(``silent``, ``digest``, ``notify``, ``prefer``) within a scope (sender, account, task kind, source...).
The reply is interpreted by the model with the notice's context; a deterministic fallback keeps the
owner's words when the model is unavailable. Preferences are versioned and can be disabled.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..i18n import language_name, t
from ..llm import LlmError
from ..models import canonical_hash
from ..pipeline.schemas import PREFERENCE_SCHEMA, RISK_FLAGS
from ..preferences import DIMENSIONS, EFFECTS, SCOPE_FIELDS, new_preference
from ..prompts import PREFERENCE_PROMPT

log = logging.getLogger("radar.learn")

MAX_INSTRUCTION = 300
_SILENT = re.compile(r"no (me )?(avis|molest|notifi)|silenci|ignor|no quiero|deja de|dejar de|mute|stop (notify|telling)|don'?t (notify|tell|bother)|never (notify|tell)", re.I)
_DIGEST = re.compile(r"resumen|digest|agrup|batch|summar", re.I)
_NOTIFY = re.compile(r"av[ií]same siempre|siempre av|siempre me av|siempre quiero|urgente|interr[uú]mpeme|always (notify|tell|interrupt)|notify me always", re.I)
_SENDER_CUE = re.compile(r"\b(este|esta|ese|esa|remitente|de [ée]l|de ella|sender|this person|this sender|him|her)\b", re.I)
_ACCOUNT_CUE = re.compile(r"\b(cuenta|buz[oó]n|mailbox|account|inbox)\b", re.I)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _address(sender: str) -> str:
    sender = str(sender or "")
    match = re.search(r"<([^>]+)>", sender)
    return (match.group(1) if match else sender).strip().lower()


def notice_context(store: Any, task_id: str | None) -> dict[str, Any]:
    """What the owner was looking at: the task and the latest email behind it."""
    context: dict[str, Any] = {"task_title": "", "kind": "", "sender": "", "account": "", "source_kind": "", "message_type": "", "subject": ""}
    if not task_id:
        return context
    try:
        task = store.get_task(task_id)
    except KeyError:
        return context
    context.update({"task_title": str(task.get("title") or ""), "kind": str(task.get("kind") or "")})
    try:
        signals = store.signals_for_task(task_id, limit=10)
    except Exception:
        signals = []
    signals = sorted(signals, key=lambda s: str(s.get("occurred_at") or ""), reverse=True)
    latest = next((s for s in signals if s.get("kind") == "email"), signals[0] if signals else None)
    if latest:
        meta = latest.get("meta") or {}
        context.update({
            "sender": _address(meta.get("sender", "")), "account": str(meta.get("account") or "").lower(),
            "source_kind": str(meta.get("source_kind") or latest.get("kind") or ""), "message_type": str(latest.get("kind") or ""),
            "subject": str(latest.get("title") or "")[:200],
        })
    return context


def validate_learned(raw: Any, *, fallback_text: str) -> dict[str, Any] | None:
    """The model's answer as fields for ``new_preference``; None when it says the reply is no preference."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("preference answer must be an object")
    if raw.get("applies") is False:
        return None
    effect = str(raw.get("effect") or "")
    if effect not in EFFECTS:
        raise ValueError("invalid effect")
    dimension = str(raw.get("dimension") or "notification")
    if dimension not in DIMENSIONS:
        dimension = "notification"
    scope_raw = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}
    scope = {k: str(v).strip() for k, v in scope_raw.items() if k in SCOPE_FIELDS and isinstance(v, str) and v.strip()}
    exceptions = tuple(sorted({str(x) for x in (raw.get("exceptions") or []) if str(x) in RISK_FLAGS}))
    instruction = " ".join(str(raw.get("instruction") or "").split())[:MAX_INSTRUCTION] or " ".join(fallback_text.split())[:MAX_INSTRUCTION]
    return {"dimension": dimension, "effect": effect, "scope": scope, "exceptions": exceptions, "instruction": instruction}


def fallback_preference(text: str, context: dict[str, Any]) -> dict[str, Any]:
    """Without a model: effect from the owner's wording, scope from what the notice was about."""
    lowered = " ".join(str(text or "").split())
    effect = "notify" if _NOTIFY.search(lowered) else "digest" if _DIGEST.search(lowered) else "silent" if _SILENT.search(lowered) else "prefer"
    scope: dict[str, str] = {}
    if context.get("account") and _ACCOUNT_CUE.search(lowered):
        scope["account"] = str(context["account"])
    elif context.get("sender") and (_SENDER_CUE.search(lowered) or effect in ("silent", "notify")):
        scope["sender"] = str(context["sender"])
    elif context.get("kind"):
        scope["kind"] = str(context["kind"])
    return {"dimension": "notification", "effect": effect, "scope": scope, "exceptions": (), "instruction": lowered[:MAX_INSTRUCTION] or "-"}


def preference_from_fields(args: dict[str, Any]) -> dict[str, Any]:
    """Fields given directly (the ``radar_preference`` tool), validated the same way."""
    raw = {
        "applies": True, "effect": args.get("effect"), "dimension": args.get("dimension") or "notification",
        "scope": args.get("scope") if isinstance(args.get("scope"), dict) else {}, "exceptions": args.get("exceptions") or [],
        "instruction": args.get("instruction") or "",
    }
    data = validate_learned(raw, fallback_text="")
    if data is None or not data["instruction"]:
        raise ValueError("instruction and effect are required")
    return data


def preference_id(data: dict[str, Any]) -> str:
    return "pref-" + canonical_hash({"dimension": data["dimension"], "effect": data["effect"], "scope": data["scope"]})[:10]


def store_preference(store: Any, data: dict[str, Any], *, origin: str, now_dt: datetime, feedback_ref: str | None = None) -> dict[str, Any]:
    """Save as a new version of the preference with the same scope and effect (or version 1)."""
    pid = preference_id(data)
    history = store.preference_history(pid)
    current = int(history[-1]["version"]) if history else 0
    record = new_preference(pid, instruction=data["instruction"], dimension=data["dimension"], scope=data["scope"],
                            effect=data["effect"], exceptions=tuple(data.get("exceptions") or ()), origin=origin, status="active")
    version = store.save_preference(record, expected_version=current, now=iso(now_dt), feedback_ref=feedback_ref)
    return {**record, "version": version}


def forget_preference(store: Any, pid: str, *, now_dt: datetime) -> dict[str, Any] | None:
    history = store.preference_history(pid)
    if not history:
        return None
    latest = history[-1]
    if latest.get("status") == "disabled":
        return latest
    record = new_preference(pid, instruction=str(latest.get("instruction") or "-"), dimension=str(latest.get("dimension") or "notification"),
                            scope=dict(latest.get("scope") or {}), effect=str(latest.get("effect") or "silent"),
                            exceptions=tuple(latest.get("exceptions") or ()), origin=str(latest.get("origin") or "explicit"), status="disabled")
    version = store.save_preference(record, expected_version=int(latest["version"]), now=iso(now_dt))
    return {**record, "version": version}


def describe_preference(pref: dict[str, Any], language: str) -> str:
    scope = pref.get("scope") or {}
    where = ", ".join(f"{t(language, 'scope_' + key)} {value}" for key, value in scope.items() if value) or t(language, "scope_any")
    return f"{t(language, 'pref_effect_' + str(pref.get('effect')))} · {where} · {pref.get('instruction')}"


def list_preferences_text(store: Any, language: str) -> str:
    active = store.list_preferences(active_only=True)
    if not active:
        return t(language, "prefs_empty")
    return "\n".join([t(language, "prefs_header")] + [f"• {p['id']}: {describe_preference(p, language)}" for p in active])


def forget_preference_text(store: Any, pid: str, language: str, *, now_dt: datetime) -> str:
    try:
        record = forget_preference(store, pid.strip(), now_dt=now_dt)
    except ValueError:
        record = None
    return t(language, "pref_forgotten", id=pid.strip()) if record else t(language, "pref_unknown")


ENGAGEMENT_WINDOW_DAYS = 60  # how far back engagements with one sender count
ENGAGEMENTS_TO_LEARN = 2  # distinct tasks of a sender the owner engaged with before its mail is marked as mattering
ENGAGED_REACTIONS = frozenset({"answered", "accepted", "review", "words", "wait", "reopen"})


def engagement_family(store: Any, task: dict[str, Any]) -> dict[str, str] | None:
    """The scope a learnt interest hangs on: the address and source of the mail the task was born from. Only mail
    has a family worth learning (the same address writes again); a chat or a job has none."""
    from ..preferences import sender_address

    try:
        origin = store.get_signal(str(task.get("origin_signal_id") or ""))
    except (KeyError, TypeError):
        return None
    meta = origin.get("meta") or {}
    sender = sender_address(meta.get("sender"))
    if origin.get("kind") != "email" or not sender:
        return None
    return {"sender": sender, "source_kind": str(meta.get("source_kind") or origin.get("kind") or "email")}


def learn_from_engagement(store: Any, task: dict[str, Any], *, reaction: str, now_dt: datetime, language: str = "es",
                          underrated: bool = False) -> dict[str, Any] | None:
    """The owner engaging with a notice teaches as much as muting one. Each engagement (an answer, an approval, a
    request for details, words, «espera») is kept as feedback on the task; once the owner has engaged with
    ``ENGAGEMENTS_TO_LEARN`` tasks of the same sender — or once with one Radar had rated low (``underrated``: the
    model's judgement was wrong, not the owner's interest) — that sender gets an active ``prefer`` rule: never silent,
    always important. Returns the new rule, or None. Never raises past the store."""
    if reaction not in ENGAGED_REACTIONS:
        return None
    family = engagement_family(store, task)
    if family is None:
        return None
    now = iso(now_dt)
    task_id = str(task.get("id") or "")
    try:
        store.add_feedback(
            feedback_id=canonical_hash({"engaged": task_id, "reaction": reaction, "at": now})[:32], target_type="task", target_id=task_id,
            action="engaged", reason=json.dumps({"reaction": reaction, **family, "title": str(task.get("title") or "")[:80]}, ensure_ascii=False), now=now,
        )
    except Exception as exc:
        log.debug("engagement not recorded: %s", exc)
    since = iso(now_dt - timedelta(days=ENGAGEMENT_WINDOW_DAYS))
    engaged: set[str] = set()
    for row in store.list_feedback(action="engaged", since=since, limit=500):
        try:
            detail = json.loads(row.get("reason") or "{}")
        except ValueError:
            continue
        if detail.get("sender") == family["sender"]:
            engaged.add(str(row.get("target_id")))
    engaged.add(task_id)
    if len(engaged) < ENGAGEMENTS_TO_LEARN and not underrated:
        return None
    data = {"dimension": "notification", "effect": "prefer", "scope": family, "exceptions": (),
            "instruction": t(language, "pref_interest", sender=family["sender"], n=len(engaged))}
    history = store.preference_history(preference_id(data))
    if history:
        return None  # already learnt, or the owner disabled it with /radar forget: their word stands
    record = store_preference(store, data, origin="inferred", now_dt=now_dt, feedback_ref=task_id)
    try:
        store.add_audit(
            audit_id=canonical_hash({"pref": record["id"], "v": record["version"]})[:32], event_type="preference", subject_type="preference",
            subject_id=record["id"], correlation_id=task_id, actor_kind="system", decision_code="preference_learned",
            metadata={"effect": "prefer", "scope": family, "how": "engagement", "tasks": len(engaged), "underrated": underrated}, now=now,
        )
    except Exception as exc:
        log.debug("engagement audit skipped: %s", exc)
    return record


def reply_context(store: Any, task_id: str | None, outbox_row: dict[str, Any] | None) -> dict[str, Any]:
    """What the owner was looking at when they wrote: the task and its latest mail (:func:`notice_context`) and, when
    the reply answers a message, that message and the decisions it carried (the question asked and its answers). On
    2026-11-08 «the ones at my personal mailbox yes, the others no», said to «Register this receipt in <system>?»,
    was learnt without the question as a rule about attention, not about registering: the next receipt was asked again."""
    context = notice_context(store, task_id)
    payload = (outbox_row or {}).get("payload") or {}
    if not isinstance(payload, dict) or not payload:
        return context
    from .feedback import decisions_in

    context["message"] = " ".join(re.sub(r"<[^>]+>", "", str(payload.get("text") or "")).split())[:600]
    try:
        decisions = decisions_in(store, payload)
    except Exception:
        decisions = []
    context["decisions"] = [
        {"question": str(d.get("title") or "")[:200], "operation": str(d.get("operation") or ""),
         "answers": [str(a.get("label") or "")[:64] for a in d.get("answers") or [] if isinstance(a, dict)]}
        for d in decisions[:4] if isinstance(d, dict)
    ]
    return context


async def learn_from_reply(rt: Any, *, intent: dict[str, Any], text: str, task_id: str | None, actor: str, now_dt: datetime,
                           result: dict[str, Any], outbox_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a written reply teaches, once :func:`radar.delivery.responses.resolve_reply` has acted on it. A reply that
    is a lasting rule (outcome ``preference``) is learnt and answered as such. One that answers and also lays down a
    rule (``intent.rule``: «the ones from my personal mailbox yes, the others no» said to a question about one
    receipt) keeps its answer's outcome and learns the rule beside it, the acknowledgement saying both. On 2026-11-08
    such a reply was read only as a «no» to the one item and the same question came back a month later. Never raises."""
    if result.get("outcome") == "preference":
        return await learn_preference(rt, text=text, task_id=task_id, actor=actor, now_dt=now_dt, outbox_row=outbox_row)
    if not (isinstance(intent, dict) and intent.get("rule")):
        return result
    try:
        learned = await learn_preference(rt, text=text, task_id=task_id, actor=actor, now_dt=now_dt, outbox_row=outbox_row)
    except Exception as exc:  # the answer stands even when the rule could not be learnt
        log.info("rule beside the answer not learnt: %s", exc)
        return result
    if learned.get("outcome") != "preference":
        return result
    out = dict(result)
    out["text"] = "\n".join(part for part in (str(result.get("text") or ""), str(learned.get("text") or "")) if part)
    out["preference"] = learned.get("preference")
    return out


async def learn_preference(rt: Any, *, text: str, task_id: str | None, actor: str, now_dt: datetime,
                           outbox_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Interpret a preference reply, store it and push it to the live sources. ``outbox_row`` is the message the
    reply answers, when there is one: its question and answers shape the rule. Never raises."""
    store, config, gate = rt.store, rt.config, rt.gate
    language = config.get("owner", {}).get("language", "es")
    context = reply_context(store, task_id, outbox_row)
    data: dict[str, Any] | None = None
    how = "fallback"
    if getattr(gate, "available", False):
        try:
            raw = await gate.structured(
                "preference", instructions=PREFERENCE_PROMPT.format(language=language_name(language)),
                input_text=json.dumps({"reply": " ".join(str(text or "").split())[:600], "context": context}, ensure_ascii=False, sort_keys=True),
                schema=PREFERENCE_SCHEMA, purpose="radar.preference", subject_id=task_id,
            )
            data = validate_learned(raw, fallback_text=text)
            how = "model"
            if data is None:
                return {"outcome": "unclear", "text": t(language, "reply_ack_unclear")}
        except (LlmError, ValueError) as exc:
            log.info("preference not interpreted by the model (%s); keeping the owner's words", exc)
            data = None
    if data is None:
        data = fallback_preference(text, context)
    record = store_preference(store, data, origin="explicit", now_dt=now_dt, feedback_ref=actor)
    try:
        store.add_audit(
            audit_id=canonical_hash({"pref": record["id"], "v": record["version"]})[:32], event_type="preference", subject_type="preference",
            subject_id=record["id"], correlation_id=task_id, actor_kind="owner", decision_code="preference_learned",
            metadata={"effect": record["effect"], "scope": record["scope"], "how": how, "version": record["version"]}, now=iso(now_dt),
        )
    except Exception as exc:
        log.debug("preference audit skipped: %s", exc)
    if record.get("effect") == "silent" and (record.get("scope") or {}).get("sender") and (record.get("scope") or {}).get("subject_stem"):
        from .infer import after_silence

        after_silence(store, config, sender=str(record["scope"]["sender"]), now_dt=now_dt)
    refresh = getattr(rt, "refresh_preferences", None)
    if callable(refresh):
        refresh()
    summary = describe_preference(record, language)
    return {"outcome": "preference", "text": t(language, "pref_saved", summary=summary, id=record["id"]), "preference": record}
