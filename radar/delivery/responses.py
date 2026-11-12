"""Apply the owner's answers (buttons, replies, tools) to proposals and tasks. Pure store logic."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..i18n import t
from ..models import TASK_TERMINAL, canonical_hash
from ..trust import AGENT_CHANNELS, owner_only
from .cadence import next_delivery, paced
from .context import thread_context
from .snooze import SNOOZE_HOURS, deadline_of, snooze_until  # noqa: F401  (snooze_until is this module's historical name for it)

OPEN_PROPOSAL = ("pending", "sent", "snoozed")
WAITING = "waiting"  # approved, not started yet: can still be cancelled, not re-answered
CANCELLABLE = OPEN_PROPOSAL + (WAITING,)
DECLINE_OPTIONS = {"no", "never", "decline", "reject", "cancel"}
ACCEPT_OPTIONS = {"do", "yes", "ok", "accept", "approve", "send", "go"}
LATER_OPTIONS = {"later", "snooze"}
DONE_OPTIONS = {"done", "already_done"}
NEGATIVE_LABELS = ("no", "nope", "don't", "do not", "descart", "cancel", "mejor no")
LATER_LABELS = ("dejarlo", "más tarde", "mas tarde", "later", "leave it", "después", "despues", "pospon")


def option_intent(proposal: dict[str, Any], option: str) -> str:
    """What a pressed option means: accept | decline | later | done | review.

    The planner's contract is that the first option means "do it", whatever verb names the action: on
    2026-11-08 «Cancelar el hotel», the first option of a cancellation step, read as a refusal because its
    label starts with "cancel", and the owner's press filed the step as dismissed while the hotel stayed
    booked. Any other option only executes when it reads as approval; "review it first" and the like show
    the details and keep the proposal open.
    """
    option = (option or "").strip().lower()
    options = [o for o in proposal.get("options") or [] if isinstance(o, dict) and o.get("id")]
    ids = [str(o["id"]).strip().lower() for o in options]
    label = next((str(o.get("label") or "") for o in options if str(o["id"]).strip().lower() == option), "").strip().lower()
    if ids and option == ids[0]:
        return "accept"
    if option in LATER_OPTIONS or label.startswith(LATER_LABELS):
        return "later"
    if option in DONE_OPTIONS:
        return "done"
    if option in DECLINE_OPTIONS or option.startswith(("no_", "not_")) or label.startswith(NEGATIVE_LABELS):
        return "decline"
    if option in ACCEPT_OPTIONS:
        return "accept"
    if option in ids:
        from .feedback import classify

        intent = classify(label).get("intent")
        return {"approve": "accept", "reject": "decline", "later": "later"}.get(str(intent), "review")
    return "review"


def review_detail(store: Any, config: dict[str, Any], proposal: dict[str, Any]) -> str:
    """Everything the owner may want to read before deciding: the full draft, or the step and the situation."""
    language = config.get("owner", {}).get("language", "es")
    params = proposal.get("params") or {}
    options = [o for o in proposal.get("options") or [] if isinstance(o, dict) and o.get("label")]
    label = str(options[0]["label"]) if options else t(language, "btn_do")
    draft = str(params.get("draft") or "")
    context = thread_context(store, proposal["task_id"], config) if proposal.get("task_id") else None
    prefix = ""
    if context and context.get("sender"):
        when = f" ({context['when']})" if context.get("when") else ""
        prefix = t(language, "review_context", sender=context["sender"], when=when, excerpt=_clip(context.get("excerpt") or "", 600)) + "\n\n"
    if draft:
        ref = str(params.get("draft_ref") or "")
        return prefix + t(language, "review_draft", text=draft, ref=f" ({ref})" if ref else "", label=label)
    summary = ""
    if proposal.get("task_id"):
        try:
            summary = str(store.get_task(proposal["task_id"]).get("summary") or "")
        except KeyError:
            summary = ""
    rationale = str(proposal.get("reason_text") or params.get("rationale") or "")
    return prefix + t(language, "review_details", title=proposal.get("title", ""), rationale=rationale, summary=summary, label=label)


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:") + "…"


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


log = logging.getLogger("radar.responses")


def _engaged(store: Any, task_id: Any, reaction: str, now_dt: datetime, *, language: str = "es", underrated: bool = False) -> None:
    """The owner engaged with a task (answered, approved, asked for details, wrote, waits): what that teaches is
    learnt here, whatever the channel (a button, words, the panel). Never fails the response it rides on."""
    if not task_id:
        return
    try:
        from .learn import learn_from_engagement

        learn_from_engagement(store, store.get_task(str(task_id)), reaction=reaction, now_dt=now_dt, language=language, underrated=underrated)
    except Exception as exc:
        log.debug("engagement not learnt for %s: %s", task_id, exc)


def _due_of(store: Any, task_id: Any, now_dt: datetime) -> str:
    try:
        return deadline_of(store.get_task(str(task_id)), now_dt) if task_id else ""
    except KeyError:
        return ""


def snoozed_text(language: str, now_dt: datetime, until: datetime, config: dict[str, Any], title: str) -> str:
    if not paced(config) and next_delivery(now_dt, "whenever", config) is None:
        return t(language, "snoozed", hours=SNOOZE_HOURS, title=title)
    local_now, local_until = now_dt.astimezone(until.tzinfo), until
    clock = local_until.strftime("%H:%M")
    days = (local_until.date() - local_now.date()).days
    if days <= 0:
        when = t(language, "when_today", time=clock)
    elif days == 1:
        when = t(language, "when_tomorrow", time=clock)
    else:
        when = t(language, "when_date", date=local_until.strftime("%d/%m"), time=clock)
    return t(language, "snoozed_at", when=when, title=title)


def who(channel: str) -> str:
    """Who an audit row says acted: the owner on a channel that checks it is them (their chat, the panel), the agent
    when the chat model recorded the decision through its tool. Every row used to say «verified_owner»."""
    return "agent" if channel in AGENT_CHANNELS else "verified_owner"


def respond_to_proposal(
    store: Any, config: dict[str, Any], *, proposal_id: str, option: str, actor: str, now_dt: datetime,
    channel: str = "telegram", note: str = "",
) -> dict[str, Any]:
    """Returns ``{"outcome": accepted|declined|snoozed|stale, "text": ..., "proposal": ..., "job": bool}``."""
    language = config.get("owner", {}).get("language", "es")
    now = iso(now_dt)
    try:
        proposal = store.get_proposal(proposal_id)
    except KeyError:
        return {"outcome": "stale", "text": t(language, "unknown_callback"), "proposal": None}
    title = proposal.get("title", "")
    option = (option or "").strip().lower()
    is_check = proposal.get("operation_id") == "check"
    is_info = proposal.get("operation_id") == "inform"
    if proposal.get("operation_id") == "ask" and proposal.get("status") in CANCELLABLE:
        answered = _answer_ask(store, proposal, option=option, actor=actor, channel=channel, note=note, now_dt=now_dt, language=language)
        if answered is not None:
            return answered
    intent = ("decline" if option == "ok" else "accept" if option == "wrong" else option_intent(proposal, option)) if is_check else option_intent(proposal, option)
    if is_info and proposal.get("status") in CANCELLABLE:
        store.respond_proposal(proposal_id, decision="declined", response={**{"option": option, "actor": actor, "channel": channel, "note": note[:500]}, "ack": True}, now=now)
        store.add_audit(
            audit_id=str(uuid4()), event_type="owner_response", subject_type="proposal", subject_id=proposal_id,
            correlation_id=proposal.get("task_id"), actor_kind=who(channel), decision_code="acknowledged",
            metadata={"option": option, "channel": channel}, now=now,
        )
        return {"outcome": "acknowledged", "text": t(language, "acknowledged"), "proposal": store.get_proposal(proposal_id)}
    if proposal.get("status") == WAITING and intent != "decline":
        return {"outcome": "stale", "text": t(language, "already_waiting", title=title), "proposal": proposal}
    if proposal.get("status") not in CANCELLABLE:
        return {"outcome": "stale", "text": t(language, "unknown_callback"), "proposal": proposal}
    response = {"option": option, "actor": actor, "channel": channel, "note": note[:500]}
    if intent == "accept" and channel in AGENT_CHANNELS and owner_only(proposal):
        # The chat model's word is not the owner's: nothing is recorded, the step stays as it was, waiting for them.
        store.add_audit(
            audit_id=str(uuid4()), event_type="owner_response", subject_type="proposal", subject_id=proposal_id,
            correlation_id=proposal.get("task_id"), actor_kind="agent", decision_code="owner_required",
            metadata={"option": option, "channel": channel}, now=now,
        )
        return {"outcome": "owner_required", "text": t(language, "owner_required", title=title), "proposal": proposal}
    if intent == "review":
        store.add_audit(
            audit_id=str(uuid4()), event_type="owner_response", subject_type="proposal", subject_id=proposal_id,
            correlation_id=proposal.get("task_id"), actor_kind=who(channel), decision_code="review",
            metadata={"option": option, "channel": channel}, now=now,
        )
        _engaged(store, proposal.get("task_id"), "review", now_dt, language=language)
        return {"outcome": "review", "text": t(language, "review_ack"), "detail": review_detail(store, config, proposal), "proposal": proposal}
    if intent == "decline":
        store.respond_proposal(proposal_id, decision="declined", response=response, now=now)
        outcome, text = "declined", t(language, "declined", title=title)
        if is_check and option == "ok":
            text = t(language, "check_ok", title=title)
            if proposal.get("task_id"):
                _close_check_task(store, proposal["task_id"], now)
        if proposal.get("operation_id") == "cron.create":
            from ..patterns import dismiss

            dismiss(store, proposal.get("task_id"), now)
    elif intent == "later":
        until_dt = snooze_until(now_dt, config, _due_of(store, proposal.get("task_id"), now_dt))
        if until_dt is None:  # the deadline leaves no time to postpone: the decision stays on the screen
            return {"outcome": "kept", "text": t(language, "snooze_no_time", title=title), "proposal": proposal}
        store.respond_proposal(proposal_id, decision="snoozed", response=response, now=now, snoozed_until=iso(until_dt))
        outcome, text = "snoozed", snoozed_text(language, now_dt, until_dt, config, title)
    elif intent == "done":
        store.respond_proposal(proposal_id, decision="declined", response={**response, "done": True}, now=now)
        outcome, text = "declined", t(language, "task_done", title=title)
        if proposal.get("task_id"):
            try:
                store.set_task_status(proposal["task_id"], "done", now=now, reason="owner_says_done")
                store.cancel_task_followups(proposal["task_id"], now)
            except (KeyError, ValueError, RuntimeError):
                pass
    else:
        store.respond_proposal(proposal_id, decision="accepted", response=response, now=now)
        outcome, text = "accepted", t(language, "accepted", title=title)
        _engaged(store, proposal.get("task_id"), "accepted", now_dt, language=language)
        if is_check:
            text = t(language, "check_wrong", title=title)
            if proposal.get("task_id"):
                try:
                    store.set_task_status(proposal["task_id"], "needs_user", now=now, reason="check_confirmed_wrong")
                except (KeyError, ValueError, RuntimeError):
                    pass
    store.add_audit(
        audit_id=str(uuid4()), event_type="owner_response", subject_type="proposal", subject_id=proposal_id,
        correlation_id=proposal.get("task_id"), actor_kind=who(channel), decision_code=outcome,
        metadata={"option": option, "channel": channel}, now=now,
    )
    return {"outcome": outcome, "text": text, "proposal": store.get_proposal(proposal_id)}


def _answer_ask(store: Any, proposal: dict[str, Any], *, option: str, actor: str, channel: str, note: str,
                now_dt: datetime, language: str) -> dict[str, Any] | None:
    """The owner answered a question: the answer becomes a fact of theirs and a signal bound to the task, so the
    plan starts again from what they said. None when the option is not one of the question's answers."""
    options = [o for o in proposal.get("options") or [] if isinstance(o, dict) and o.get("id")]
    ids = [str(o["id"]).strip().lower() for o in options]
    if option not in ids:
        return None
    now = iso(now_dt)
    index = ids.index(option)
    label = str(options[index].get("label") or option)
    fact = (proposal.get("params") or {}).get("fact") or {}
    value = str(fact.get("yes" if index == 0 else "no") or label)
    key = str(fact.get("key") or "")
    if key:
        try:
            store.record_owner_fact(key=key, value=value, provenance={"proposal_id": proposal["id"], "at": now, "channel": channel}, now=now)
        except ValueError:
            key = ""
    store.respond_proposal(proposal["id"], decision="accepted", response={"option": option, "actor": actor, "channel": channel, "note": note[:500], "answer": label}, now=now)
    # An answered question is settled, not approved work: left ``accepted`` it looked like a step without a job and
    # the scheduler ran a «verification» of the owner's own answer, then told them its result (2026-11-03).
    store.set_proposal_status(proposal["id"], "succeeded", now)
    task_id = proposal.get("task_id")
    if task_id:
        try:
            source_id = store.ensure_owner_source(now)
            question = str(proposal.get("title") or "")
            store.add_signal(
                signal_id=str(uuid4()), source_id=source_id, kind="user_reply", occurred_at=now,
                external_ref=f"answer:{proposal['id']}", group_key=f"task:{task_id}", title=f"{question} → {label}",
                snippet=f"{question} {label}." + (f" {key} = {value}." if key else "") + (f" {note[:300]}" if note else ""),
                meta={"task_id": task_id, "proposal_id": proposal["id"], "question": question, "answer": label,
                      "fact": {"key": key, "value": value} if key else {}, "source_kind": "owner", "platform": channel},
                fingerprint=f"answer:{proposal['id']}", now=now,
            )
        except Exception:
            pass
    store.add_audit(
        audit_id=str(uuid4()), event_type="owner_response", subject_type="proposal", subject_id=proposal["id"],
        correlation_id=task_id, actor_kind=who(channel), decision_code="answered",
        metadata={"option": option, "channel": channel, "fact": key}, now=now,
    )
    _engaged(store, task_id, "answered", now_dt, language=language)
    return {"outcome": "answered", "text": _ack(options[index], language, value), "proposal": store.get_proposal(proposal["id"])}


ACK_LIMIT = 180  # it travels as a toast (200 characters) and as the last line of the notice


def _ack(option: dict[str, Any], language: str, value: str) -> str:
    """What Radar says back to an answer: the sentence the planner wrote for that option, in the owner's language and
    in its own words, or a plain «Noted» when there is none. The fixed text used to promise «I'll report back» after
    every answer, also the ones that led nowhere (2026-11-11)."""
    written = " ".join(str(option.get("ack") or "").split())
    if written and len(written) <= ACK_LIMIT and "<" not in written and ">" not in written:
        return written
    return t(language, "ask_recorded", value=value)


def _close_check_task(store: Any, task_id: str, now: str) -> None:
    try:
        task = store.get_task(task_id)
        if task.get("kind") == "check" and task.get("status") not in TASK_TERMINAL:
            store.set_task_status(task_id, "done", now=now, reason="check_confirmed_ok")
            store.cancel_task_followups(task_id, now)
    except (KeyError, ValueError, RuntimeError):
        pass


def learn_from_mute(store: Any, task: dict[str, Any], now: str) -> dict[str, Any] | None:
    """"Stop notifying about this" teaches something: the next mail of the same family (same sender, same subject stem) is
    silenced at triage instead of becoming a task again. Only mails qualify; a chat has no family."""
    from ..preferences import new_preference, sender_address, subject_stem

    try:
        origin = store.get_signal(task.get("origin_signal_id") or "")
    except (KeyError, TypeError):
        return None
    meta = origin.get("meta") or {}
    sender = sender_address(meta.get("sender"))
    stem = subject_stem(meta.get("subject") or origin.get("title") or "")
    if origin.get("kind") != "email" or not sender or not stem:
        return None
    pref_id = "mute:" + canonical_hash({"sender": sender, "stem": stem})[:16]
    data = new_preference(
        pref_id, instruction=f"Silenciado por el propietario desde «{task.get('title', '')[:80]}»: mensajes de {sender} sobre «{stem}».",
        scope={"sender": sender, "subject_stem": stem, "kind": "email"}, effect="silent", origin="explicit",
    )
    try:
        current = next((p for p in store.list_preferences() if p.get("id") == pref_id), None)
        store.save_preference(data, expected_version=int(current.get("version") or 0) if current else 0, now=now, feedback_ref=task.get("id"))
    except ValueError:
        return None
    return data


def record_owner_words(store: Any, *, outbox_row: dict[str, Any], text: str, actor: str, channel: str, now_dt: datetime,
                       language: str = "es") -> str | None:
    """The owner answered a notice in words no button stands for ("wait for their answer", "I already asked them
    another way", 2026-11-03). Their words become a ``user_reply`` signal bound to the task of that message, so the next
    plan starts from them; before, the turn fell to the chat agent, which has no Radar context, and the task
    never heard the owner. Returns the signal id, or None when the message belongs to no task."""
    payload = outbox_row.get("payload") or {}
    proposal_ids = [pid for pid in payload.get("proposal_ids") or [] if isinstance(pid, str)]
    task_id = str(outbox_row.get("task_id") or payload.get("task_id") or "")
    if not task_id and proposal_ids:
        task_id = str(_task_of(store, proposal_ids[0]) or "")
    words = " ".join(str(text or "").split())[:1000]
    if not task_id or not words:
        return None
    now = iso(now_dt)
    question = " ".join(re.sub(r"<[^>]+>", "", str(payload.get("text") or "")).split())[:300]
    signal_id = str(uuid4())
    try:
        source_id = store.ensure_owner_source(now)
        created = store.add_signal(
            signal_id=signal_id, source_id=source_id, kind="user_reply", occurred_at=now,
            external_ref=f"words:{outbox_row.get('id')}:{now}", group_key=f"task:{task_id}",
            title=f"{question[:80]} → {words[:120]}", snippet=words[:500],
            meta={"task_id": task_id, "proposal_id": proposal_ids[0] if proposal_ids else "", "question": question, "answer": words,
                  "fact": {}, "free_text": True, "source_kind": "owner", "platform": channel},
            fingerprint=f"words:{outbox_row.get('id')}:{canonical_hash(words)[:16]}", now=now,
        )
    except Exception:
        return None
    if not created:
        return None
    store.add_audit(
        audit_id=str(uuid4()), event_type="owner_response", subject_type="task", subject_id=task_id,
        correlation_id=outbox_row.get("id"), actor_kind="verified_owner", decision_code="words",
        metadata={"channel": channel, "chars": len(words), "signal_id": signal_id}, now=now,
    )
    # Writing back to a message Radar rated as mere news (no decision in it) says the rating was wrong: learnt at once.
    news_only = not proposal_ids and bool(payload.get("news_ids"))
    _engaged(store, task_id, "words", now_dt, language=language, underrated=news_only)
    return signal_id


def act_on_task(store: Any, config: dict[str, Any], *, task_id: str, action: str, actor: str, now_dt: datetime,
                until: str | None = None, note: str = "") -> dict[str, Any]:
    """What the owner can do to a whole task: ``later`` (until tomorrow morning), ``wait`` (parked until ``until``,
    an ISO moment, because someone else has to move), ``mute`` / ``unmute``, ``done`` and ``reopen``."""
    language = config.get("owner", {}).get("language", "es")
    now = iso(now_dt)
    try:
        task = store.get_task(task_id)
    except KeyError:
        return {"outcome": "stale", "text": t(language, "unknown_callback")}
    title = task.get("title", "")
    action = (action or "").strip().lower()
    open_proposals = store.list_proposals(statuses=CANCELLABLE, task_id=task_id, limit=20)
    if action == "wait":
        until_dt = _parse_until(until)
        if until_dt is None or until_dt <= now_dt:
            return {"outcome": "invalid", "text": t(language, "wait_until_required")}
        for proposal in [p for p in open_proposals if p.get("status") != WAITING]:
            store.respond_proposal(proposal["id"], decision="snoozed", response={"actor": actor, "task_action": action, "note": note[:300]}, now=now, snoozed_until=iso(until_dt))
        parked = store.park_task(task_id, until=iso(until_dt), now=now, reason="owner_waits")
        if not parked:
            return {"outcome": "stale", "text": t(language, "unknown_callback")}
        local = until_dt.astimezone(_tz(config))
        outcome, text = "waiting", t(language, "task_waits", date=local.strftime("%d/%m"), time=local.strftime("%H:%M"), title=title)
        _engaged(store, task_id, "wait", now_dt, language=language)
    elif action == "unmute":
        store.update_task(task_id, now=now, bump_version=False, muted=False)
        outcome, text = "unmuted", t(language, "unmuted", title=title)
    elif action == "reopen":
        store.update_task(task_id, now=now, bump_version=False, muted=False, snoozed_until=None)
        if task.get("status") in TASK_TERMINAL or task.get("status") == "snoozed":
            try:
                store.set_task_status(task_id, "open", now=now, reason="owner_reopened")
            except (ValueError, RuntimeError):
                pass
        outcome, text = "reopened", t(language, "task_reopened", title=title)
        _engaged(store, task_id, "reopen", now_dt, language=language)
    elif action in {"later", "snooze"}:
        # The moment the owner named (the chat agent passes it as ``until``) wins over «tomorrow»: on 2026-11-11
        # «remind me on December 10» was answered «I'll remind you tomorrow at 08:00». The button, a bare «later» and
        # a moment that is past, unreadable or absurdly far keep the usual snooze.
        until_dt = named_moment(until, now_dt, config) or snooze_until(now_dt, config, deadline_of(task, now_dt))
        if until_dt is None:
            return {"outcome": "kept", "text": t(language, "snooze_no_time", title=title)}
        until = iso(until_dt)
        for proposal in [p for p in open_proposals if p.get("status") != WAITING]:
            store.respond_proposal(proposal["id"], decision="snoozed", response={"actor": actor, "task_action": action}, now=now, snoozed_until=until)
        if task.get("status") not in TASK_TERMINAL:
            store.update_task(task_id, now=now, bump_version=False, snoozed_until=until)
            try:
                store.set_task_status(task_id, "snoozed", now=now, reason="owner_snoozed")
            except (ValueError, RuntimeError):
                pass
        outcome, text = "snoozed", snoozed_text(language, now_dt, until_dt, config, title)
    elif action in {"mute", "never"}:
        for proposal in open_proposals:
            store.respond_proposal(proposal["id"], decision="declined", response={"actor": actor, "task_action": action}, now=now)
        store.update_task(task_id, now=now, bump_version=False, muted=True)
        store.cancel_task_followups(task_id, now)
        outcome, text = "muted", t(language, "muted", title=title)
        learned = learn_from_mute(store, task, now)
        if learned:
            text = t(language, "muted_with_preference", title=title, sender=learned.get("scope", {}).get("sender", ""))
            from .infer import after_silence

            after_silence(store, config, sender=learned.get("scope", {}).get("sender", ""), now_dt=now_dt)
    elif action == "done":
        for proposal in open_proposals:
            store.respond_proposal(proposal["id"], decision="declined", response={"actor": actor, "task_action": action}, now=now)
        if task.get("status") not in TASK_TERMINAL:
            try:
                store.set_task_status(task_id, "done", now=now, reason="owner_says_done")
            except (ValueError, RuntimeError):
                pass
        store.cancel_task_followups(task_id, now)
        outcome, text = "done", t(language, "task_done", title=title)
    else:
        return {"outcome": "stale", "text": t(language, "unknown_callback")}
    store.add_audit(
        audit_id=str(uuid4()), event_type="owner_response", subject_type="task", subject_id=task_id,
        correlation_id=None, actor_kind="verified_owner", decision_code=outcome,
        metadata={"action": action, "actor": actor, **({"until": until} if until else {}), **({"note": note[:200]} if note else {})}, now=now,
    )
    return {"outcome": outcome, "text": text}


NAMED_SNOOZE_MAX = timedelta(days=366)


def local_now(config: dict[str, Any], now_dt: datetime) -> datetime:
    return now_dt.astimezone(_tz(config))


def named_moment(value: Any, now_dt: datetime, config: dict[str, Any]) -> datetime | None:
    """The moment the owner asked to be reminded at, in their timezone, or None when there is none to honour. A day
    with no hour is that day at 08:00; a moment with no zone is local; what is not ahead of now, or more than a year
    away, is not a snooze."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    tz = _tz(config)
    if len(text) <= 10:
        moment = moment.replace(hour=8, minute=0)
    moment = moment.replace(tzinfo=tz) if moment.tzinfo is None else moment.astimezone(tz)
    if moment <= now_dt or moment - now_dt > NAMED_SNOOZE_MAX:
        return None
    return moment


def _parse_until(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _tz(config: dict[str, Any]) -> Any:
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(str((config.get("owner") or {}).get("timezone") or "UTC"))
    except Exception:
        return timezone.utc


def resolve_reply(
    store: Any, config: dict[str, Any], *, outbox_row: dict[str, Any], intent: dict[str, Any], actor: str,
    now_dt: datetime, note: str = "",
) -> dict[str, Any]:
    """Map a free-text reply to a notice or a reminder onto its proposals.

    A number refers to the position in the message as it was shown, whatever has been answered since; a
    number that is already settled or does not exist says so instead of landing on another step. Without a
    number, a reminder with several open decisions asks which one; a notice about one task keeps its old
    manners (the first open step, or every step for a plain «no»)."""
    language = config.get("owner", {}).get("language", "es")
    payload = outbox_row.get("payload") or {}
    proposal_ids = [pid for pid in payload.get("proposal_ids") or [] if isinstance(pid, str)]
    open_ids = [pid for pid in proposal_ids if _is_open(store, pid)]
    kind = intent.get("intent")
    index = int(intent.get("proposal_index") or 0)
    reminder = outbox_row.get("kind") == "digest"
    target: str | None = None
    decides = kind in ("approve", "reject", "later", "done", "edit_request")
    if index and decides:
        if index > len(proposal_ids):
            return {"outcome": "unknown_number", "text": t(language, "reply_unknown_number", n=index, count=len(proposal_ids))}
        target = proposal_ids[index - 1]
        if not _is_open(store, target):
            return {"outcome": "already_answered", "text": t(language, "reply_already_answered", n=index, title=_title(store, target))}
    elif index:
        # A question or an order about a decision, answered or not («did you cancel the hotel?»): the agent answers it
        # with the task in front of it. On 2026-11-08 that question got «number 1 was already answered» instead.
        target = proposal_ids[index - 1] if 0 < index <= len(proposal_ids) else None
    elif len(open_ids) == 1 or (open_ids and not reminder):
        target = open_ids[0]
    elif open_ids and kind in ("approve", "reject", "later", "done"):
        return {"outcome": "which_one", "text": t(language, "reply_which_one", count=len(proposal_ids))}
    task_id = outbox_row.get("task_id") or payload.get("task_id")
    if target and (index or not task_id):
        task_id = _task_of(store, target) or task_id
    if kind == "approve" and target:
        return respond_to_proposal(store, config, proposal_id=target, option=_chosen(store, target, intent, "yes"), actor=actor, now_dt=now_dt, note=note)
    if kind in ("approve", "reject") and not open_ids and task_id:
        # A yes or a no to a message with nothing left to decide (news, or decisions already settled): their words go to
        # the task, nothing is approved or declined, and the answer never claims a proposal has lapsed. On 2026-11-06
        # the owner's reply to a news-only reminder was met with «Esa propuesta ya no está activa».
        return {"outcome": "noted", "text": t(language, "reply_ack_noted")}
    if kind == "reject":
        targets = [target] if (target and (index or reminder)) else open_ids  # a bare «no» to a notice about one task declines its steps
        results = [respond_to_proposal(store, config, proposal_id=pid, option=_chosen(store, pid, intent, "no"), actor=actor, now_dt=now_dt, note=note) for pid in targets]
        return results[0] if results else {"outcome": "stale", "text": t(language, "unknown_callback")}
    if kind == "later" and task_id:
        return act_on_task(store, config, task_id=task_id, action="later", actor=actor, now_dt=now_dt)
    if kind == "done" and task_id:
        return act_on_task(store, config, task_id=task_id, action="done", actor=actor, now_dt=now_dt)
    if kind == "preference":
        return {"outcome": "preference", "text": t(language, "reply_ack_preference", text=note[:120])}
    if kind == "question":
        return {"outcome": "question", "text": t(language, "reply_ack_question")}
    if kind == "thanks":
        return {"outcome": "thanks", "text": ""}
    return {"outcome": "unclear", "text": t(language, "reply_ack_unclear")}


def _chosen(store: Any, proposal_id: str, intent: dict[str, Any], word: str) -> str:
    """The answer the model matched to the owner's words («we run it ourselves» → «Sí, la llevamos nosotros»), when it
    is one the proposal offers; otherwise the plain yes or no."""
    option = str((intent or {}).get("option") or "").strip().lower()
    if option:
        try:
            ids = [str(o.get("id")).strip().lower() for o in store.get_proposal(proposal_id).get("options") or [] if isinstance(o, dict) and o.get("id")]
        except KeyError:
            ids = []
        if option in ids:
            return option
    return _answer(store, proposal_id, word)


def _answer(store: Any, proposal_id: str, word: str) -> str:
    """A written yes/no answers a question as such: "yes" confirms a check and "no" reports the mistake."""
    try:
        proposal = store.get_proposal(proposal_id)
    except KeyError:
        proposal = {}
    operation = proposal.get("operation_id")
    if operation == "ask":  # a question's own two answers, first means yes
        ids = [str(o.get("id")) for o in proposal.get("options") or [] if isinstance(o, dict) and o.get("id")]
        if len(ids) >= 2:
            return ids[0] if word == "yes" else ids[1]
    if operation == "check":
        return "ok" if word == "yes" else "wrong"
    return "do" if word == "yes" else "no"


def _title(store: Any, proposal_id: str) -> str:
    try:
        return str(store.get_proposal(proposal_id).get("title") or "")
    except KeyError:
        return ""


def _task_of(store: Any, proposal_id: str) -> str | None:
    try:
        return store.get_proposal(proposal_id).get("task_id")
    except KeyError:
        return None


def _is_open(store: Any, proposal_id: str) -> bool:
    try:
        return store.get_proposal(proposal_id).get("status") in OPEN_PROPOSAL
    except KeyError:
        return False
