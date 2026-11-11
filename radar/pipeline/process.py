"""Process one claimed bundle of signals end to end."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..jobs.service import DEFER_REASONS, expire_proposal, job_block_reason, start_or_wait
from ..llm import BudgetExhausted, LlmError
from ..models import TASK_TERMINAL
from ..trust import needs_consent
from .recall import MAX_QUERY, build_query, precedent_names, recall_summary
from .conversation import is_conversation, transcript, view as conversation_view
from .sessions import quiet_minutes, session_active, session_of, task_session_active
from . import act, conflicts, correlate, correspondents, evidence, extract as extract_step, lineage, reason, rules
from .triage import triage_bundle

log = logging.getLogger(__name__)

RETRY_MINUTES = 15
BUDGET_RETRY_HOURS = 6
BODY_READ_SECONDS = 20
MAX_BODY_RELOADS = 8


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def reload_bodies(rt: Any, signals: list[dict[str, Any]], now_dt: datetime) -> int:
    """Read again the mail bodies a bundle lost: a body expires after its hold, and a late retry or a
    ``hermes radar replay`` would otherwise judge the mail on its 500-character snippet. A mail signal with
    no ``raw_content`` but a ``content_ref`` is fetched through its source's provider (converted to text
    by the provider) and kept for the usual hold; a provider that is missing or fails leaves the snippet."""
    from ..sources.gmail import RAW_HOLD_HOURS

    sources = getattr(rt, "sources", None) or {}
    snippet_chars = int((rt.config.get("retention") or {}).get("snippet_chars", 500))
    reloaded = 0
    for signal in signals[:MAX_BODY_RELOADS]:
        if signal.get("kind") != "email" or signal.get("raw_content"):
            continue
        ref = str(signal.get("content_ref") or "")
        provider = getattr(sources.get(str(signal.get("source_id") or "")), "provider", None)
        message_id = ref.rsplit(":", 1)[-1] if ref.startswith("gmail:") and ref.count(":") >= 2 else ""
        if provider is None or not message_id:
            continue
        try:
            message = await asyncio.wait_for(asyncio.to_thread(provider.read, message_id), timeout=BODY_READ_SECONDS)
        except Exception as exc:
            log.info("body of %s not read again: %s", signal.get("id"), str(exc)[:160])
            continue
        body = str(getattr(message, "body", "") or "")
        if not body.strip():
            continue
        signal["raw_content"] = body[:20000]
        signal["raw_expires_at"] = iso(now_dt + timedelta(hours=RAW_HOLD_HOURS))
        signal["snippet"] = " ".join(body.split())[:snippet_chars]
        try:
            rt.store.update_signal(signal["id"], now=iso(now_dt), raw_content=signal["raw_content"],
                                   raw_expires_at=signal["raw_expires_at"], snippet=signal["snippet"])
        except Exception as exc:  # the fresh body still feeds this extraction
            log.info("body of %s not stored: %s", signal.get("id"), str(exc)[:160])
        reloaded += 1
    return reloaded


def _synthetic_extract_from_job(signal: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    meta = signal.get("meta") or {}
    failed = meta.get("status") not in {"succeeded"}
    return {
        "relevant": True, "category": task.get("kind", "other"), "title": task.get("title", ""),
        "summary": signal.get("snippet", ""), "disposition": "digest" if failed else "notify_now",
        "reason": "job_result", "urgency": "low" if failed else "medium", "impact": "low", "risk_flags": [],
        "entities": {}, "expectations": [], "facts_learned": [], "proposed_reply": meta.get("proposed_reply", ""),
        "keep_full_content": False, "needs_thread": False,
        "job": {"status": meta.get("status"), "findings": meta.get("findings", []), "refs": meta.get("refs", []),
                "next_steps": meta.get("next_steps", []), "effects": meta.get("effects", []),
                "branch": meta.get("branch", ""), "repo": meta.get("repo", ""), "pr": meta.get("pr", ""), "base": meta.get("base", "")},
    }


def _synthetic_extract_from_refresh(signal: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """A pull request looked up before the summary: the signal already says what changed."""
    meta = signal.get("meta") or {}
    return {
        "relevant": True, "category": task.get("kind", "other"), "title": task.get("title", ""),
        "summary": signal.get("snippet", ""), "disposition": "digest", "reason": "artifact_refresh",
        "urgency": "low", "impact": "low", "risk_flags": [],
        "entities": {"repo": str(meta.get("repo") or ""), "url": str(meta.get("url") or "")}, "expectations": [], "facts_learned": [],
        "proposed_reply": "", "keep_full_content": False, "needs_thread": False,
    }


def _synthetic_extract_from_reply(signal: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """The owner answered a question about the task: the fact is already recorded, the plan starts again from it."""
    meta = signal.get("meta") or {}
    return {
        "relevant": True, "category": task.get("kind", "other"), "title": task.get("title", ""),
        "summary": signal.get("snippet", ""), "disposition": "digest", "reason": "owner_answer",
        "urgency": "medium", "impact": "medium", "risk_flags": [],
        "entities": {}, "expectations": [], "facts_learned": [], "proposed_reply": "",
        "keep_full_content": False, "needs_thread": False,
        "owner_answer": {"question": meta.get("question", ""), "answer": meta.get("answer", ""), "fact": meta.get("fact") or {}},
    }


def _synthetic_extract_from_owner_mail(signal: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """The owner wrote in the thread themselves (a reply to the reporter, a message to a colleague): the task
    moves on, nothing is told back to them, and the planner judges what their words settle."""
    meta = signal.get("meta") or {}
    return {
        "relevant": True, "category": task.get("kind", "other"), "title": task.get("title", ""),
        "summary": signal.get("snippet", ""), "disposition": "silent", "reason": "owner_reply",
        "urgency": "low", "impact": "low", "risk_flags": [],
        "entities": {}, "expectations": [], "facts_learned": [], "proposed_reply": "",
        "keep_full_content": False, "needs_thread": False,
        "owner_reply": {"text": signal.get("snippet", ""), "at": signal.get("occurred_at", ""), "to": str(meta.get("to") or ""), "subject": signal.get("title", "")},
    }


def _synthetic_extract(signal: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    meta = signal.get("meta") or {}
    if signal.get("kind") == "user_reply":
        return _synthetic_extract_from_reply(signal, task)
    if meta.get("refresh"):
        return _synthetic_extract_from_refresh(signal, task)
    if signal.get("kind") == "email" and meta.get("owner_sent"):
        return _synthetic_extract_from_owner_mail(signal, task)
    return _synthetic_extract_from_job(signal, task)


def standing_rule(preferences: list[dict[str, Any]], signals: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first active ``prefer`` preference, of any dimension, whose scope matches the bundle's first signal (its
    sender, mailbox, source): the owner's standing rule about such items, or None."""
    from ..preferences import DIMENSIONS, applicable

    observation = act._observation(signals, "")
    for dimension in sorted(DIMENSIONS):
        for preference in applicable(preferences, observation, dimension):
            if preference.get("effect") == "prefer":
                return preference
    return None


def implications_for(extract: dict[str, Any] | None, task: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What the bundle implies, else what the task already knows it implies (a review has no new bundle)."""
    found = (extract or {}).get("implications") or ((task or {}).get("entities") or {}).get("implications") or []
    return [dict(item) for item in found if isinstance(item, dict) and item.get("concern")][:8]


async def answer_implications(rt: Any, implications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each implication with what the owner's memory answers to its question (``memory``), looked up all at
    once; without a memory provider every answer is empty and the planner judges on facts and history."""
    recall = getattr(rt, "recall", None)
    questions = [" ".join(str(item.get("memory_question") or "").split())[:MAX_QUERY] for item in implications]
    answers: dict[str, str] = {}
    if recall is not None and any(questions):
        try:
            for item in await recall.answer([q for q in questions if q]):
                answers[item["question"]] = item.get("memory") or ""
        except Exception as exc:  # recall is optional context, never a reason to stall the plan
            log.info("implication recall skipped: %s", exc)
    return [{**item, "memory": answers.get(question, "")} for item, question in zip(implications, questions)]


def _manipulation_in(signals: list[dict[str, Any]]) -> tuple[str, str] | None:
    """``(sender, excerpt)`` of the first signal from outside (mail, repository, calendar; never the owner's own
    chat or their own mail) that addresses the assistant and gives it orders; None otherwise."""
    for signal in signals:
        meta = signal.get("meta") or {}
        if signal.get("kind") in ("session_turn", "user_reply", "job_result", "cron_result") or meta.get("owner_sent"):
            continue
        text = "\n".join(str(part or "") for part in (signal.get("title"), signal.get("snippet"), signal.get("raw_content"), meta.get("body")))
        excerpt = rules.injection_excerpt(text)
        if excerpt:
            return str(meta.get("sender") or meta.get("actor") or meta.get("organizer") or ""), excerpt
    return None


def _bound_task_id(signals: list[dict[str, Any]], store: Any = None) -> str:
    """The task a bundle already belongs to: job and cron results, the owner's answer to one of its questions,
    a refresh of the task's own artifact, or mail the owner themselves sent in a thread an open task follows
    (judged on its own, their «ya lo miramos» looked like noise and the task never learned they had answered)."""
    if not signals:
        return ""
    if all(s.get("kind") in ("job_result", "cron_result", "user_reply") for s in signals) or all((s.get("meta") or {}).get("refresh") for s in signals):
        return str((signals[0].get("meta") or {}).get("task_id") or "")
    if store is not None and all(s.get("kind") == "email" and (s.get("meta") or {}).get("owner_sent") for s in signals):
        group_key = str(signals[-1].get("group_key") or "")
        if group_key.startswith("thread:") and hasattr(store, "tasks_for_group"):
            try:
                tracked = store.tasks_for_group(group_key, statuses=correlate.OPEN_STATUSES, limit=1)
            except Exception:
                tracked = []
            if tracked:
                return str(tracked[0]["id"])
    return ""


def _settle_draft(store: Any, task_id: str, signal: dict[str, Any], now: str) -> None:
    """After a successful external mail step, the drafted reply is no longer pending."""
    meta = signal.get("meta") or {}
    if meta.get("status") != "succeeded" or not meta.get("proposal_id"):
        return
    try:
        proposal = store.get_proposal(meta["proposal_id"])
    except KeyError:
        return
    operation = str(proposal.get("operation_id") or "").lower()
    if proposal.get("action_class") == "external" and (operation.startswith("gmail.") or "send" in operation or "reply" in operation):
        act.forget_draft(store, task_id, now)
    if operation == "github.open_pr":
        refs = [str(r) for r in meta.get("refs") or [] if str(r).startswith("http")]
        act.remember_pr(store, task_id, refs[0] if refs else str(meta.get("pr") or ""), now)
    if operation == "cron.create":
        from ..patterns import mark_automated

        ref = next((str(r)[5:] for r in meta.get("refs") or [] if str(r).startswith("cron:")), "")
        mark_automated(store, task_id, ref, now)


def _owner_bundle(signals: list[dict[str, Any]]) -> bool:
    """The bundle is the owner's own words: an answer to a notice, a turn of their chat with the agent, or mail
    they sent. The only thing that reopens a task they closed (see ``act.owner_closed``)."""
    return any(s.get("kind") in ("user_reply", "session_turn") or bool((s.get("meta") or {}).get("owner_sent")) for s in signals)


def fell_short(signal: dict[str, Any]) -> bool:
    """True when a job_result says the step did not do what it was for: it failed, something cut it short (the
    anomaly: out of turns, a login wall), or an external step reports no effects. On 2026-11-08 the owner approved
    a hotel cancellation twice; both jobs ran out of turns one click short and the second time the plan's new step
    was held by the task cap, so nobody told them: they saw the hotel's verification code arrive and then silence."""
    meta = signal.get("meta") if isinstance(signal.get("meta"), dict) else {}
    if str(meta.get("status") or "") != "succeeded" or meta.get("anomaly"):
        return True
    return str(meta.get("action_class") or "") == "external" and not meta.get("effects")


def _owner_approved_job(store: Any, signal: dict[str, Any]) -> bool:
    """True when a job_result signal comes from a step the owner approved (not one the plugin chose). The owner's
    own answer to a question (``user_reply``) also names its proposal, but it is no job result: telling them
    «done, could not verify it» about their own answer was the 2026-11-03 echo."""
    if signal.get("kind") != "job_result":
        return False
    proposal_id = (signal.get("meta") or {}).get("proposal_id")
    if not proposal_id:
        return False
    try:
        response = store.get_proposal(proposal_id).get("response") or {}
    except KeyError:
        return False
    return not response.get("auto")


async def process_bundle(rt: Any, bundle: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    store, config, gate = rt.store, rt.config, rt.gate
    now = iso(now_dt)
    signals = bundle["signals"]
    owner = config.get("owner", {})
    preferences = store.list_preferences(active_only=True)
    summary: dict[str, Any] = {"group": bundle["group_key"], "signals": len(signals)}
    existing_task = None
    kept = signals
    session_id = session_of(signals) if is_conversation(signals) else ""
    thread_sessions = lineage.thread_sessions(session_id) if session_id else []
    for linked in (store.session_thread(session_id) if session_id else []):  # what the host told radar at each compaction
        if linked not in thread_sessions:
            thread_sessions.append(linked)
    if session_id and session_active(store, session_id, now_dt, config):
        # The conversation is still doing the work: learn from it once it goes quiet.
        store.defer_signals(signals, next_attempt_at=iso(now_dt + timedelta(minutes=quiet_minutes(config))), error="session_active", now=now,
                            counts_attempt=False)  # waiting for the owner to finish talking is not a failed attempt
        summary["deferred"] = "session_active"
        return summary
    bound = _bound_task_id(signals, store)
    if bound:
        try:
            existing_task = store.get_task(bound)
        except KeyError:
            existing_task = None
    if (existing_task is not None and existing_task.get("status") in TASK_TERMINAL
            and all(s.get("kind") in ("job_result", "cron_result") for s in signals) and not _owner_approved_job(store, signals[-1])):
        # The result of a step Radar chose for a task that has since closed: kept on the task as evidence, never
        # planned on, never told. (A step the owner approved is a promise, and its result still reaches them.)
        for signal in signals:
            try:
                store.link_task_signal(existing_task["id"], signal["id"], "context", now)
            except Exception:
                pass
        store.finish_signals(signals, status="ignored", reason="task_closed", now=now)
        store.add_audit(
            audit_id=str(uuid4()), event_type="pipeline", subject_type="task", subject_id=existing_task["id"],
            correlation_id=signals[0]["id"], actor_kind="scheduler", decision_code="task_closed",
            metadata={"signals": len(signals), "status": existing_task.get("status"), "closed_reason": str(existing_task.get("closed_reason") or "")[:120]}, now=now,
        )
        summary["result"] = "task_closed"
        return summary
    session_tasks = store.tasks_for_sessions(thread_sessions, statuses=correlate.OPEN_STATUSES) if session_id else []
    if existing_task is None:
        kept, ignored = triage_bundle(signals, preferences, config, bound=bool(session_tasks))
        for signal, decision in ignored:
            store.finish_signals([signal], status="ignored", reason=decision.reason, now=now)
        summary["ignored"] = len(ignored)
        if not kept:
            return summary
    if not gate.available:
        store.defer_signals(kept, next_attempt_at=iso(now_dt + timedelta(minutes=RETRY_MINUTES)), error="llm unavailable", now=now, counts_attempt=False)
        summary["deferred"] = "llm_unavailable"
        return summary
    max_attempts = int(config.get("limits", {}).get("signal_attempts", 5))
    if any(int(s.get("attempts") or 0) > max_attempts for s in kept):
        store.finish_signals(kept, status="error", reason="too_many_attempts", now=now)
        summary["error"] = "too_many_attempts"
        return summary
    facts = store.list_facts(limit=40)
    recall_job = asyncio.ensure_future(recall_context(rt, None, kept, existing_task))  # runs while the model extracts
    try:
        if existing_task is not None:
            last = kept[-1]
            extracted = _synthetic_extract(last, existing_task)
            candidates = []
        else:
            conversation = None
            if session_id:
                # The whole session is the unit of judgement: earlier turns come along as context.
                conversation = conversation_view(session_id, kept, transcript(store, session_id, {s["id"] for s in kept}))
            else:
                reloaded = await reload_bodies(rt, kept, now_dt)
                if reloaded:
                    summary["bodies"] = reloaded
            extracted = await extract_step.extract(
                gate, kept, owner=owner, now=now, facts=facts, open_tasks=store.open_task_summaries(), subject_id=kept[0]["id"],
                conversation=conversation,
            )
            manipulation = _manipulation_in(kept) if not session_id else None
            if manipulation:
                # A message giving the assistant orders: a security check for the owner whatever the model made of it,
                # and marked as such for the planner and act.apply (no step ever works on it, see act.MANIPULATION_STEPS).
                if (not extracted.get("relevant") or extracted.get("category") == "noise"
                        or "security" not in (extracted.get("risk_flags") or [])):
                    extracted = extract_step.flag_manipulation(extracted, sender=manipulation[0], excerpt=manipulation[1], language=owner.get("language", "es"))
                extracted["manipulation"] = True
                extracted["keep_full_content"] = True
                summary["manipulation"] = True
            if not session_id and extracted.get("relevant") and extracted.get("message_kind") == "invitation":
                # An invitation is raised only when the owner has corresponded with its sender or their organisation
                # (the retained mail, then one search of the sent folder); the planner and act.apply read the verdict.
                extracted["known_correspondent"] = await correspondents.known_correspondent(rt, kept, now_dt)
                from ..warmup import invitation_history

                extracted["invitation_history"] = invitation_history(store, kept, extracted)  # did they go to such events before?
            if not session_id and (not extracted.get("relevant") or extracted.get("category") == "noise"):
                # A standing rule of the owner about this sender or mailbox (a prefer preference) outranks the model's
                # «noise»: the planner sees both and decides (see ``extract.keep_by_rule``).
                rule = standing_rule(preferences, kept)
                if rule is not None:
                    extracted = extract_step.keep_by_rule(extracted, rule=rule)
                    summary["kept_by_rule"] = str(rule.get("id") or "")
            resolved = session_id and extracted.get("conversation_state") == "resolved"
            if (not extracted.get("relevant") or extracted.get("category") == "noise") and not (resolved and session_tasks):
                reason_code = "conversation_resolved" if resolved else "model_noise"
                store.finish_signals(kept, status="ignored", reason=reason_code, now=now, extracts={s["id"]: extracted for s in kept})
                _apply_retention(store, kept, keep=False, config=config, now_dt=now_dt)
                summary["result"] = "noise"
                return summary
            candidates = correlate.candidate_tasks(store, extracted, session_id=session_id, session_ids=thread_sessions, group_key=str(kept[0].get("group_key") or ""))
        if existing_task is not None:
            existing_task = task_context(store, existing_task)
        # What the planner must see besides the bundle: what its task received after the bundle's last item, and what
        # the owner was doing with their agent in the same minutes (see ``radar.pipeline.evidence``).
        matched_ids = [existing_task["id"]] if existing_task is not None else [str(c.get("id") or "") for c in candidates[:3]]
        later = evidence.later_evidence(store, matched_ids, kept)
        activity = evidence.owner_activity(store, kept, session_id=session_id)
        recalled = await recall_job
        implications = implications_for(extracted, existing_task)
        if existing_task is None:
            # A fresh judgement asks memory what each implication needs; a job result or the owner's answer
            # re-plans on the checklist it already has.
            implications = await answer_implications(rt, implications)
            if recalled is not None:
                recalled["answers"] = [{"question": i.get("memory_question"), "memory": i["memory"]} for i in implications if i.get("memory")]
        places = (extracted.get("entities") or {}).get("places") or ((existing_task or {}).get("entities") or {}).get("places")
        history = correlate.history_at_places(store, places, exclude_id=str((existing_task or {}).get("id") or ""))
        summary["recall"] = recall_summary(recalled)
        planned = await reason.plan_checked(
            gate, now=now, owner=owner, trust_level=str(config.get("trust", {}).get("level", "balanced")),
            extract=extracted, signals=kept, candidates=candidates, facts=facts, preferences=preferences,
            existing_task=existing_task, subject_id=kept[0]["id"], recall=recalled, implications=implications, history=history,
            later_evidence=later, owner_activity=activity,
        )
        # The clash the dates alone reveal (a delivery while a trip has the owner away), whatever the model made of it.
        planned = conflicts.add_away_checks(planned, extracted, candidates, language=str(owner.get("language") or "es"))
        summary["candidates"] = [{"title": c.get("title"), **({"overlap": c["overlap"]} if c.get("overlap") else {})} for c in candidates[:6]]
    except BudgetExhausted:
        recall_job.cancel()
        store.defer_signals(kept, next_attempt_at=iso(now_dt + timedelta(hours=BUDGET_RETRY_HOURS)), error="budget_exhausted", now=now, counts_attempt=False)
        summary["deferred"] = "budget"
        return summary
    except LlmError as exc:
        recall_job.cancel()
        # A provider down (a spend limit, a rate limit, no connection) is not the bundle's failure: it waits without
        # being charged an attempt, however long the outage; a bad answer still counts towards too_many_attempts.
        store.defer_signals(kept, next_attempt_at=iso(now_dt + timedelta(minutes=RETRY_MINUTES)), error=str(exc)[:200], now=now,
                            counts_attempt=not getattr(exc, "transient", False))
        summary["deferred"] = str(exc)[:100]
        return summary
    if session_id and extracted.get("conversation_state") == "resolved" and planned["task_match"]["decision"] in ("new", "none"):
        # Nothing of a finished conversation becomes a task; it only closes what the same session opened.
        store.finish_signals(kept, status="ignored", reason="conversation_resolved", now=now, extracts={s["id"]: extracted for s in kept})
        _apply_retention(store, kept, keep=False, config=config, now_dt=now_dt)
        summary["result"] = "resolved"
        return summary
    owner_followup = existing_task is not None and _owner_approved_job(store, kept[-1])
    result = act.apply(
        store, config, signals=[] if existing_task is not None else kept, extract=extracted, plan=planned,
        now_dt=now_dt, preferences=preferences, existing_task=existing_task, owner_followup=owner_followup, recall=summary.get("recall"),
        owner_bundle=_owner_bundle(kept),
        trigger="job_result" if existing_task is not None and all(s.get("kind") in ("job_result", "cron_result") for s in kept) else "",
    )
    if existing_task is not None:
        _settle_draft(store, existing_task["id"], kept[-1], now)
        if extracted.get("owner_reply"):
            act.forget_draft(store, existing_task["id"], now)  # the owner wrote the reply themselves: the drafted one is moot
    if owner_followup and (not result.proposals or fell_short(kept[-1])):
        # Nothing left to decide, or the approved step did not do its work: the owner hears how it ended now, not in
        # the next summary and never only through a new step that the caps may hold.
        from ..delivery.dispatch import notify_job_result

        summary["result_notice"] = notify_job_result(store, config, task_id=existing_task["id"], signal=kept[-1], now_dt=now_dt,
                                                     message=str((planned or {}).get("message") or ""))
    if existing_task is not None:
        for signal in kept:
            try:
                store.link_task_signal(existing_task["id"], signal["id"], "advances", now)
            except Exception:
                pass
    _apply_retention(store, kept, keep=bool(extracted.get("keep_full_content")), config=config, now_dt=now_dt)
    store.finish_signals(kept, status="extracted", reason=result.decision, now=now, extracts={s["id"]: extracted for s in kept})
    auto_jobs = _auto_run(rt, result, now_dt)
    summary.update({
        "task_id": result.task_id, "decision": result.decision, "disposition": result.disposition,
        "proposals": len(result.proposals), "auto_jobs": auto_jobs, "facts": result.facts,
    })
    return summary


def _apply_retention(store: Any, signals: list[dict[str, Any]], *, keep: bool, config: dict[str, Any], now_dt: datetime) -> None:
    retention = config.get("retention", {})
    now = iso(now_dt)
    for signal in signals:
        if not signal.get("raw_content"):
            continue
        kinds = retention.get("full_content_kinds") or []
        if keep or signal.get("kind") in kinds:
            store.update_signal(signal["id"], now=now, raw_expires_at=iso(now_dt + timedelta(days=int(retention.get("full_content_days", 14)))))
        else:
            store.update_signal(signal["id"], now=now, raw_content=None, raw_expires_at=None)


async def recall_context(rt: Any, extract: dict[str, Any] | None, signals: list[dict[str, Any]], existing_task: dict[str, Any] | None) -> dict[str, Any] | None:
    """Memory, past-session and precedent context for the planner (what the owner had done before with the same
    sender or organisation); None when recall is unavailable. Never raises."""
    recall = getattr(rt, "recall", None)
    if recall is None:
        return None
    try:
        recalled = await recall.gather(build_query(extract, signals, existing_task), names=precedent_names(signals, existing_task))
    except Exception as exc:
        log.info("recall skipped: %s", exc)
        return None
    try:
        status = rt.recall_status() if hasattr(rt, "recall_status") else {}
        log.info("recall for %s: memory=%d chars via %s/%s, sessions=%d, precedents=%d%s",
                 (existing_task or {}).get("id") or (signals[0].get("id") if signals else "?"), len(recalled.get("memory") or ""),
                 status.get("provider") or "no provider", status.get("tool") or "-", len(recalled.get("past_sessions") or []),
                 len(recalled.get("precedents") or []), f" (last error: {status['last_error']})" if status.get("last_error") else "")
    except Exception as exc:  # the summary line is cosmetic
        log.debug("recall summary skipped: %s", exc)
    return recalled


def task_context(store: Any, task: dict[str, Any]) -> dict[str, Any]:
    """The task plus what was already proposed (newest first), what is still open or running, what the owner
    refused and what they let expire unanswered, for the planner."""
    task = dict(task)
    history = store.list_proposals(task_id=task["id"], limit=50, newest=True)
    task["recent_proposals"] = [
        {"title": p.get("title"), "status": p.get("status"), "operation": p.get("operation_id") or ""} for p in history[:8]
    ]
    task["open_steps"] = correlate.open_steps(store, task["id"])
    task["declined_by_owner"] = [
        p.get("title") for p in history
        if p.get("status") == "declined" and not (p.get("response") or {}).get("auto") and not (p.get("response") or {}).get("task_action")
        and not (p.get("response") or {}).get("ack")
    ][:10]
    task["unanswered_by_owner"] = [
        p.get("title") for p in history
        if p.get("status") == "expired" and (p.get("params") or {}).get("expired_reason") == "unanswered"
    ][:5]
    return task


def _auto_run(rt: Any, result: Any, now_dt: datetime) -> int:
    """Start the auto-eligible proposals of a fresh plan: one runs, the others wait for it."""
    if not result.task_id or not result.proposals:
        return 0
    store = rt.store
    try:
        task = store.get_task(result.task_id)
    except KeyError:
        return 0
    if task.get("status") in TASK_TERMINAL or task.get("muted"):
        return 0
    started = 0
    for proposal_id in result.proposals:
        try:
            proposal = store.get_proposal(proposal_id)
        except KeyError:
            continue
        if _start_auto(rt, proposal, task, now_dt):
            started += 1
    return started


def _auto_eligible(rt: Any, proposal: dict[str, Any], task: dict[str, Any]) -> bool:
    if proposal.get("operation_id") in ("check", "inform", "ask"):
        return False
    if task_session_active(rt.store, task, rt.clock(), rt.config):
        return False  # the chat session is on it; no parallel work
    return not needs_consent(rt.config, task, proposal)  # the assistant's own work, unless the trust level or a chat origin reserves it


def _start_auto(rt: Any, proposal: dict[str, Any], task: dict[str, Any], now_dt: datetime) -> bool:
    """Accept one pending proposal the trust level allows and start it, or park it as waiting.

    Caps that the owner could lift (per-task cap, jobs disabled) leave the proposal pending so it is
    asked instead of silently dropped.
    """
    store, config = rt.store, rt.config
    now = iso(now_dt)
    if proposal.get("status") != "pending" or not _auto_eligible(rt, proposal, task):
        return False
    params = proposal.get("params") or {}
    scheduled = act.parse_iso(params.get("when"))
    if scheduled is not None and scheduled > now_dt:
        return False  # the planner chose a later moment on purpose; a summary slot is not a reason to wait
    reason = job_block_reason(store, config, proposal=proposal, now_dt=now_dt, auto=True)
    if reason and reason not in DEFER_REASONS:
        return False
    try:
        store.respond_proposal(
            proposal["id"], decision="accepted", response={"auto": True, "trust": (config.get("trust") or {}).get("level", "balanced")}, now=now,
        )
        accepted = store.get_proposal(proposal["id"])
    except (ValueError, RuntimeError, KeyError):
        return False
    job, _ = start_or_wait(store, config, proposal=accepted, option="do", now_dt=now_dt, auto=True)
    return job is not None


def _has_job(store: Any, proposal: dict[str, Any]) -> bool:
    return any(job.get("proposal_id") == proposal.get("id") for job in store.list_jobs(task_id=proposal.get("task_id"), limit=50))


def _hand_back(store: Any, proposal: dict[str, Any], now: str) -> None:
    """Trust was lowered while an automatic step waited: let the owner decide instead."""
    try:
        store.set_proposal_status(proposal["id"], "pending", now)
        store.update_proposal_params(proposal["id"], now=now, wait_reason=None, wait_since=None)
    except (ValueError, RuntimeError, KeyError):
        pass


def retry_pending_jobs(rt: Any, now_dt: datetime) -> int:
    """Each tick: start waiting steps whose block cleared, park accepted steps that never got a job, run the
    assistant's own queued work (``batch_key`` auto) once nothing blocks it, and run the scheduled steps whose
    moment has come when trust allows (the rest reach the owner as a notice)."""
    store, config = rt.store, rt.config
    now = iso(now_dt)
    started = 0
    for proposal in store.list_proposals(statuses=("pending",), limit=200):
        if proposal.get("operation_id") in ("check", "inform", "ask"):
            continue
        due = act.parse_iso((proposal.get("params") or {}).get("when"))
        if due is not None and due > now_dt:
            continue
        if due is None and proposal.get("batch_key") != "auto":
            continue  # a decision waits for the owner, not for the clock
        try:
            task = store.get_task(str(proposal.get("task_id") or ""))
        except KeyError:
            continue
        if task.get("status") in TASK_TERMINAL or task.get("muted"):
            continue
        if _start_auto(rt, proposal, task, now_dt):
            started += 1
    for proposal in store.list_proposals(statuses=("waiting", "accepted"), limit=200):
        response = proposal.get("response") or {}
        auto = bool(response.get("auto"))
        task_id = proposal.get("task_id")
        if proposal.get("operation_id") in ("ask", "check", "inform"):
            # A question the owner answered or a piece of news they acknowledged has nothing to run: it is settled
            # here (rows answered before 2026-11-03 were left accepted, and each got a job that «verified» the answer).
            try:
                store.set_proposal_status(proposal["id"], "succeeded" if proposal.get("status") == "accepted" else "expired", now)
            except (ValueError, RuntimeError, KeyError):
                pass
            continue
        try:
            task = store.get_task(task_id) if task_id else None
        except KeyError:
            task = None
        if task is None or task.get("status") in TASK_TERMINAL or task.get("muted") or str(proposal.get("expires_at") or "") <= now:
            expire_proposal(store, proposal, "stale", now)
            continue
        if proposal.get("status") == "accepted" and _has_job(store, proposal):
            continue  # queued already; only the status update was lost
        if auto and not _auto_eligible(rt, proposal, task):
            _hand_back(store, proposal, now)
            continue
        job, _ = start_or_wait(store, config, proposal=proposal, option=str(response.get("option") or "do"), now_dt=now_dt, auto=auto)
        if job is not None:
            started += 1
    return started


async def review_task(rt: Any, task: dict[str, Any], now_dt: datetime) -> dict[str, Any]:
    """Re-plan an existing task when its review timer fires (no new signal)."""
    store, config, gate = rt.store, rt.config, rt.gate
    now = iso(now_dt)
    summary: dict[str, Any] = {"task_id": task["id"]}
    if task.get("status") in TASK_TERMINAL or task.get("muted"):
        summary["skipped"] = "closed_or_muted"
        return summary
    if not gate.available:
        summary["skipped"] = "llm_unavailable"
        return summary
    if task_session_active(store, task, now_dt, config):
        minutes = quiet_minutes(config)
        try:
            store.update_task(task["id"], now=now, bump_version=False, next_review_at=iso(now_dt + timedelta(minutes=minutes)))
        except (KeyError, ValueError, RuntimeError):
            pass
        summary["skipped"] = "session_active"
        return summary
    preferences = store.list_preferences(active_only=True)
    facts = store.list_facts(limit=40)
    signals = store.signals_for_task(task["id"], limit=5)
    task = task_context(store, task)
    recalled = await recall_context(rt, None, signals, task)
    implications = await answer_implications(rt, implications_for(None, task))  # what memory learnt since counts
    if recalled is not None:
        recalled["answers"] = [{"question": i.get("memory_question"), "memory": i["memory"]} for i in implications if i.get("memory")]
    history = correlate.history_at_places(store, (task.get("entities") or {}).get("places"), exclude_id=task["id"])
    summary["recall"] = recall_summary(recalled)
    try:
        planned = await reason.plan_checked(
            gate, now=now, owner=config.get("owner", {}), trust_level=str(config.get("trust", {}).get("level", "balanced")),
            extract=None, signals=signals, candidates=[], facts=facts, preferences=preferences,
            existing_task=task, subject_id=task["id"], recall=recalled, implications=implications, history=history,
        )
    except LlmError as exc:
        summary["deferred"] = str(exc)[:100]
        return summary
    result = act.apply(store, config, signals=[], extract=None, plan=planned, now_dt=now_dt, preferences=preferences, existing_task=task,
                       recall=summary.get("recall"), trigger="review")
    summary.update({"decision": result.decision, "disposition": result.disposition, "proposals": len(result.proposals),
                    "auto_jobs": _auto_run(rt, result, now_dt), "status": result.task_status})
    return summary
