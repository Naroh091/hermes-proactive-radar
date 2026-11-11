"""Apply a validated plan: tasks, links, facts, attention, proposals and review timers."""
from __future__ import annotations

import json
import re

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from ..delivery.cadence import next_delivery
from ..i18n import t
from ..trust import decision_like, may_auto_run, needs_consent
from .sessions import session_of, task_session_active
from ..models import TASK_TERMINAL, canonical_hash
from ..preferences import applicable
from .attention import resolve
from .checklists import merge_checklist, template_for
from . import rules
from .reconcile import MIN_OVERLAP, collapse, overlap, reconcile, stems
from .schemas import TASK_KINDS

PROPOSAL_TTL_HOURS = 72
SCHEDULED_TTL_HOURS = 48
MIN_FACT_CONFIDENCE = 0.4
INFERRED_FACT_CAP = 0.6  # a model's reading of a message never outranks what the owner said
INTERRUPT_COOLDOWN_HOURS = 3
MANIPULATION_STEPS = ("check", "inform")  # the only steps a manipulation attempt may produce
RECENT_CLOSED_COOLDOWN_HOURS = 6  # a subject finished this recently is not reopened as a fresh, duplicate task
OWNER_CLOSED_REASONS = frozenset({"owner_says_done", "check_confirmed_ok", "pattern_dismissed"})  # closures by the owner's hand
SAME_QUESTION_OVERLAP = 0.6  # two questions sharing this much of their words are one question
INTERRUPT_HORIZON_HOURS = 24  # in a chat-born task, what Radar raised itself interrupts only when the task is due within this
OWNER_CLOSED_DAYS = 30  # what the owner closed stays closed to everyone but them for this long
SOON_HOURS = 72  # a task due within this is pressing enough to be asked or told about whatever its importance


def due_soon(task: dict[str, Any], now_dt: datetime) -> bool:
    """True when the task is due within SOON_HOURS (not past: a parcel due last week presses nothing)."""
    due = parse_iso(task.get("due_at"))
    return due is not None and now_dt <= due <= now_dt + timedelta(hours=SOON_HOURS)


TRUSTED_SENDERS = ("administration", "colleague", "partner")  # whose request for an acknowledgement is worth a draft


def latest_mail_judgement(store: Any, task_id: str) -> dict[str, Any]:
    """What the extractor made of the newest mail of the task (sender_kind, reply_expected…): a re-plan carries a
    synthetic extract and has to look back at the mail itself."""
    try:
        signals = store.signals_for_task(task_id, limit=30)
    except Exception:
        return {}
    mails = sorted((s for s in signals if s.get("kind") == "email" and isinstance(s.get("extract"), dict)),
                   key=lambda s: str(s.get("occurred_at") or ""), reverse=True)
    return dict(mails[0]["extract"]) if mails else {}


def draft_basis_holds(store: Any, task: dict[str, Any], item: dict[str, Any], extract: dict[str, Any], *, owner_bundle: bool) -> bool:
    """A mail step goes out only on a basis the store can verify. "findings": a step of this task ran and produced
    something (this bundle is its result, a job succeeded, a branch or PR exists). "owner_decision": the owner
    answered or wrote about this task. "requested_ack": the newest mail asks for a reply and comes from an
    administration, a colleague or a partner. A courtesy reply («I'll look into it») has no basis and is dropped
    (2026-10-31 to 11-06: sixteen of eighteen drafts were declined or left to expire; the two approved were a
    file the owner asked to send and an acknowledgement an administration requested)."""
    basis = str(item.get("basis") or "")
    task_id = str(task.get("id") or "")
    entities = task.get("entities") if isinstance(task.get("entities"), dict) else {}
    if item.get("existing_id") or item.get("restore_id"):
        return True  # a step already in the owner's hands, restated by a re-plan: it was offered once on its basis
    if basis == "findings":
        draft = entities.get("draft") if isinstance(entities.get("draft"), dict) else {}
        if extract.get("job") or entities.get("branch") or entities.get("pr") or draft.get("ref"):
            return True  # this bundle is a job's result, or a job left a branch, a PR or a saved draft behind
        try:
            return bool(store.list_jobs(task_id=task_id, statuses=("succeeded",), limit=5))
        except Exception:
            return False
    if basis == "owner_decision":
        if owner_bundle:
            return True
        try:
            return any(s.get("kind") == "user_reply" for s in store.signals_for_task(task_id, limit=30))
        except Exception:
            return False
    if basis == "requested_ack":
        judgement = extract if extract.get("reply_expected") else latest_mail_judgement(store, task_id)
        return str(judgement.get("reply_expected") or "") in ("ack", "substantive") and str(judgement.get("sender_kind") or "") in TRUSTED_SENDERS
    return False


def ask_leads_somewhere(item: dict[str, Any], *, important: bool, pressing: bool) -> bool:
    """A question is sent only when an answer changes what Radar does next. The planner says so in ``then``:
    "nothing" means either answer only closes or updates the task ("did the parcel arrive?", "which card will you
    bring?"), and such a question is dropped; the task waits for its evidence or expires. A pressing deadline or
    high importance still lets it through; a plan that did not say (older plans) is trusted."""
    return item.get("operation") != "ask" or item.get("then") != "nothing" or important or pressing


def owner_closed(task: dict[str, Any]) -> bool:
    """A task the owner themselves finished or silenced: "already resolved", "yes, correct", "do not automate", a
    mute, "close it" through the agent. Only they reopen it; the planner, a lagging job result or a third party's
    mail never do (2026-11-04 02:30: a job result raised a Postgres host the owner had closed with "close it")."""
    return task.get("status") in TASK_TERMINAL and (str(task.get("closed_reason") or "") in OWNER_CLOSED_REASONS or bool(task.get("muted")))


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/.]{3,}[A-Za-z0-9]")
_DATE_LIKE = re.compile(r"^\d{1,4}[-/.:]\d{1,2}([-/.:]\d{1,4})?$")


def identifier_tokens(text: Any) -> set[str]:
    """The reference-shaped words of a text (an invoice number, an order id, a booking code), lowercased: at least
    five characters with a digit in them, six when they are digits alone, never a date or a time."""
    out: set[str] = set()
    for token in _IDENTIFIER.findall(str(text or "")):
        token = token.strip("-_/.").lower()
        if len(token) < 5 or not any(c.isdigit() for c in token) or _DATE_LIKE.match(token):
            continue
        if token.isdigit() and len(token) < 6:
            continue
        out.add(token)
    return out


def owner_closed_identifiers(store: Any, now_dt: datetime, *, exclude: str = "") -> set[str]:
    """The references (in the title and the identifiers) of the tasks the owner closed with their own hand within
    OWNER_CLOSED_DAYS. A question that names one of them is the closed subject coming back through another task:
    on 2026-11-10 the invoice the owner had settled with «already resolved» the day before was asked about again
    from the task of the parcel it came with, whose title shared no word with the closed one."""
    try:
        closed = store.recent_closed_tasks(iso(now_dt - timedelta(days=OWNER_CLOSED_DAYS)), limit=60)
    except Exception:
        return set()
    out: set[str] = set()
    for task in closed:
        if str(task.get("id") or "") == exclude or not owner_closed(task):
            continue
        out |= identifier_tokens(task.get("title"))
        for value in _identifiers_of(task):
            out |= identifier_tokens(value)
    return out


def asked_elsewhere(store: Any, task_id: str, title: str, now_dt: datetime) -> bool:
    """True when the same question (word-stem overlap) already sits in the owner's chat from another task, sent
    within the last day and still unanswered: two tasks about one subject must not ask it twice."""
    want = stems(title)
    if not want:
        return False
    since = iso(now_dt - timedelta(hours=24))
    try:
        rows = store.list_proposals(statuses=("sent",), limit=100)
    except Exception:
        return False
    for row in rows:
        if str(row.get("task_id") or "") == task_id or not decision_like(row) or str(row.get("updated_at") or "") < since:
            continue
        if overlap(want, stems(row.get("title"))) >= SAME_QUESTION_OVERLAP:
            return True
    return False


def owner_fact_keys(store: Any) -> set[str]:
    """The keys of the facts the owner stated themselves: what they already answered is never asked again."""
    try:
        facts = store.list_facts(limit=300)
    except Exception:
        return set()
    out: set[str] = set()
    for fact in facts:
        provenance = fact.get("provenance") if isinstance(fact.get("provenance"), dict) else {}
        if str(provenance.get("origin") or "") == "owner" and fact.get("key"):
            out.add(" ".join(str(fact["key"]).split()).lower())
    return out


def _identifiers_of(task: dict[str, Any]) -> set[str]:
    entities = task.get("entities")
    if isinstance(entities, str):
        try:
            entities = json.loads(entities)
        except ValueError:
            entities = {}
    items = (entities or {}).get("identifiers") if isinstance(entities, dict) else []
    return {str(i.get("value") or "").strip().lower() for i in items or [] if isinstance(i, dict) and str(i.get("value") or "").strip()}


def another_item(store: Any, task: dict[str, Any], *, account: str = "", identifiers: list[str] | tuple[str, ...] = ()) -> bool:
    """True when ``task``, closed on the same subject, was a different item: its mail reached another mailbox than
    ``account``, or it carried other identifiers (receipt numbers, PR numbers, booking codes) than ``identifiers``.
    On 2026-12-09 (simulated) two receipts of the same vendor, one per mailbox a minute apart, read as one subject:
    the second was filed on the first, which a preference had just left unregistered, and never got its step."""
    if account:
        try:
            theirs = {str((s.get("meta") or {}).get("account") or "").lower() for s in store.signals_for_task(task["id"], limit=20)} - {""}
        except Exception:
            theirs = set()
        if theirs and account.lower() not in theirs:
            return True
    if identifiers:
        known = _identifiers_of(task)
        if known and not (known & {str(i or "").strip().lower() for i in identifiers}):
            return True
    return False


def recently_resolved(store: Any, kind: str, title: str, now_dt: datetime, group_key: str = "", *, account: str = "",
                      identifiers: list[str] | tuple[str, ...] = ()) -> str:
    """The id of a closed task this would duplicate, or "". Three nets, all deterministic (word-stem Jaccard ≥
    MIN_OVERLAP, thread identity), never the model's judgement: within OWNER_CLOSED_DAYS, a task the owner closed
    in the same thread (``group_key``) or on the same subject whatever its kind; within the cooldown, a task of the
    same kind and subject closed by anyone (a lagging job result or a re-plan raising what just finished). A closed
    task that was :func:`another_item` (another mailbox, other identifiers) is never the duplicate of this one."""
    want = stems(title)
    long_ago = iso(now_dt - timedelta(days=OWNER_CLOSED_DAYS))
    if group_key:
        try:
            for task in store.tasks_for_group(group_key, statuses=tuple(TASK_TERMINAL)):
                if owner_closed(task) and str(task.get("updated_at") or "") >= long_ago:
                    return str(task.get("id") or "")
        except Exception:
            pass
    if not want:
        return ""
    try:
        closed = store.recent_closed_tasks(long_ago, limit=60)
    except Exception:
        return ""
    since = iso(now_dt - timedelta(hours=RECENT_CLOSED_COOLDOWN_HOURS))
    for task in closed:
        same_subject = overlap(want, stems(task.get("title"))) >= MIN_OVERLAP
        if same_subject and another_item(store, task, account=account, identifiers=identifiers):
            continue
        if same_subject and owner_closed(task):
            return str(task.get("id") or "")
        if same_subject and str(task.get("updated_at") or "") >= since and str(task.get("kind") or "") == str(kind or ""):
            return str(task.get("id") or "")
    return ""


@dataclass
class ActResult:
    task_id: str | None
    task_status: str | None
    decision: str
    disposition: str
    attention_reason: str
    proposals: list[str] = field(default_factory=list)
    facts: int = 0
    message: str = ""
    created: bool = False


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def lies_ahead(task: dict[str, Any], now_dt: datetime) -> bool:
    """True while the task's own dates (its entities' dates, its due date) reach beyond tomorrow."""
    entities = task.get("entities") if isinstance(task.get("entities"), dict) else {}
    dates = [parse_iso(d.get("when")) for d in entities.get("dates") or [] if isinstance(d, dict)]
    dates.append(parse_iso(task.get("due_at")))
    latest = max((d for d in dates if d is not None), default=None)
    return latest is not None and latest > now_dt + timedelta(hours=24)


def _merge_entities(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(existing or {})
    for key, value in (new or {}).items():
        if isinstance(value, list):
            current = list(merged.get(key) or []) if isinstance(merged.get(key), list) else []
            seen = {canonical_hash(v) for v in current}
            for item in value:
                digest = canonical_hash(item)
                if digest not in seen:
                    current.append(item)
                    seen.add(digest)
            merged[key] = current[:40]
        elif value not in (None, "", [], {}):
            merged[key] = value
    return merged


def _merge_expectations(existing: list[Any], new: list[Any]) -> list[Any]:
    merged: list[Any] = [e for e in existing if isinstance(e, dict)]
    seen = {str(e.get("description", "")).strip().lower() for e in merged}
    for item in new:
        if isinstance(item, dict):
            key = str(item.get("description", "")).strip().lower()
            if key and key not in seen:
                merged.append(item)
                seen.add(key)
    return merged[:15]


def _observation(signals: list[dict[str, Any]], task_kind: str) -> dict[str, Any]:
    meta = (signals[0].get("meta") or {}) if signals else {}
    kind = signals[0].get("kind", "") if signals else ""
    return {
        "account": meta.get("account", ""), "sender": meta.get("sender", ""), "message_type": kind,
        "project": meta.get("project", ""), "kind": task_kind, "source_kind": meta.get("source_kind", kind),
        "repo": meta.get("repo", ""),
    }



def _clip_title(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:") + "…"


DECLINE_MEMORY_HOURS = 24


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def declined_recently(store: Any, task_id: str, now_dt: datetime, hours: int = DECLINE_MEMORY_HOURS) -> list[dict[str, str]]:
    """Steps the owner refused for this task lately (not auto-expired ones, not whole-task snooze/mute)."""
    since = iso(now_dt - timedelta(hours=hours))
    out: list[dict[str, str]] = []
    for proposal in store.list_proposals(statuses=("declined",), task_id=task_id, limit=50):
        response = proposal.get("response") or {}
        if response.get("auto") or response.get("task_action"):
            continue
        if str(proposal.get("responded_at") or "") < since:
            continue
        out.append({
            "operation": str(proposal.get("operation_id") or ""), "action_class": str(proposal.get("action_class") or ""),
            "title": _norm(proposal.get("title")),
        })
    return out


def awaiting_owner(store: Any, task_id: str) -> bool:
    """True while a decision about this task sits unanswered in the owner's chat (or sleeps until they asked).
    A piece of news that went out is not a question: it never holds the task."""
    return any(
        p.get("operation_id") != "inform" or (p.get("params") or {}).get("ask")
        for p in store.list_proposals(statuses=("sent", "snoozed"), task_id=task_id, limit=20)
    )


def noticed_recently(store: Any, config: dict[str, Any], task_id: str, at: datetime) -> bool:
    """True when the owner got a message about this task less than ``interrupt_cooldown_hours`` before ``at``
    (now, or the moment a scheduled step would knock). One cold interruption per task per cooldown, whatever
    each plan restates: on 2026-11-03 a re-plan withdrew the two checks the owner had just been asked, the task
    no longer «awaited» them, and a reply step timed seven minutes ahead went out twelve minutes after the first."""
    hours = float((config.get("notifications") or {}).get("interrupt_cooldown_hours", INTERRUPT_COOLDOWN_HOURS))
    last = parse_iso(store.last_notice_at(task_id)) if hasattr(store, "last_notice_at") else None
    return last is not None and at - last < timedelta(hours=hours)


def interrupt_again(store: Any, config: dict[str, Any], task_id: str, urgency: str, now_dt: datetime) -> bool:
    """A task the owner was already told about may interrupt once more only for something that cannot wait and
    only after the last message about it is older than the cooldown: five messages in two hours about one thread
    is noise, whatever each mail says."""
    return urgency == "now" and not noticed_recently(store, config, task_id, now_dt)


def matches_declined(item: dict[str, Any], declined: list[dict[str, str]]) -> bool:
    """Same title, or the same operation with the same effect class, counts as the step the owner refused."""
    operation = str(item.get("operation") or "")
    action_class = str(item.get("action_class") or "read")
    title = _norm(item.get("title"))
    for entry in declined:
        if title and entry["title"] == title:
            return True
        if operation and entry["operation"] == operation and entry["action_class"] == action_class:
            return True
    return False


MAX_DRAFT_CHARS = 6000


def remember_draft(store: Any, task: dict[str, Any], extract: dict[str, Any], now: str) -> dict[str, Any] | None:
    """Keep the latest drafted reply on the task (``entities.draft``) so every notice can show it and
    the send step uses exactly that text. Returns the draft in force, if any."""
    text = " ".join(str(extract.get("proposed_reply") or "").split("\r"))
    text = text.strip()
    entities = dict(task.get("entities") or {})
    if text and not _has_correspondent(store, task["id"]):
        text = ""  # nobody to reply to (a chat session, a calendar event): a "reply" makes no sense
    if text:
        refs = [str(r) for r in (extract.get("job") or {}).get("refs") or [] if isinstance(r, (str, int))]
        ref = next((r for r in refs if "draft" in r.lower()), "")
        entities["draft"] = {"text": text[:MAX_DRAFT_CHARS], "ref": ref, "at": now}
        try:
            store.update_task(task["id"], now=now, bump_version=False, entities=entities)
        except (KeyError, ValueError, RuntimeError):
            pass
        return entities["draft"]
    current = entities.get("draft")
    return current if isinstance(current, dict) and current.get("text") else None


def remember_branch(store: Any, task: dict[str, Any], extract: dict[str, Any], now: str) -> dict[str, Any] | None:
    """Keep the branch a job pushed (``entities.branch``) so a later step opens the draft PR natively."""
    job = extract.get("job") if isinstance(extract.get("job"), dict) else {}
    name = " ".join(str(job.get("branch") or "").split())
    entities = dict(task.get("entities") or {})
    if name and job.get("status") in (None, "succeeded"):
        entities["branch"] = {
            "name": name, "repo": str(job.get("repo") or entities.get("repo") or ""), "base": str(job.get("base") or ""), "at": now,
            "findings": [str(f) for f in (job.get("findings") or [])[:8]], "refs": [str(r) for r in (job.get("refs") or [])[:6]],
        }
        if job.get("repo") and not entities.get("repo"):
            entities["repo"] = str(job["repo"])
        if job.get("pr"):
            entities["pr"] = str(job["pr"])
        try:
            store.update_task(task["id"], now=now, bump_version=False, entities=entities)
        except (KeyError, ValueError, RuntimeError):
            pass
        return entities["branch"]
    current = entities.get("branch")
    return current if isinstance(current, dict) and current.get("name") else None


def remember_pr(store: Any, task_id: str, url: str, now: str) -> None:
    """The draft PR exists: planners stop proposing to open it."""
    if not url:
        return
    try:
        task = store.get_task(task_id)
        entities = dict(task.get("entities") or {})
        entities["pr"] = url
        store.update_task(task_id, now=now, bump_version=False, entities=entities)
    except (KeyError, ValueError, RuntimeError):
        pass


def owner_addresses(config: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for source in config.get("sources") or []:
        if isinstance(source, dict) and source.get("kind") == "gmail" and source.get("address"):
            out.add(str(source["address"]).strip().lower())
    return out


def owner_authored(signals: list[dict[str, Any]], config: dict[str, Any]) -> bool:
    """True when the newest email in the bundle was written by the owner: they already know its content."""
    emails = [s for s in signals if s.get("kind") == "email"]
    if not emails:
        return False
    sender = str((emails[-1].get("meta") or {}).get("sender") or "").lower()
    address = sender[sender.rfind("<") + 1:].rstrip(">").strip() if "<" in sender else sender.strip()
    return bool(address) and address in owner_addresses(config)


def _has_correspondent(store: Any, task_id: str) -> bool:
    """True when the task has someone to answer: an email thread or a GitHub issue/PR."""
    try:
        return any(s.get("kind") in ("email", "github") for s in store.signals_for_task(task_id, limit=20))
    except Exception:
        return True


def forget_draft(store: Any, task_id: str, now: str) -> None:
    """The drafted reply was sent: stop showing it."""
    try:
        task = store.get_task(task_id)
    except KeyError:
        return
    entities = dict(task.get("entities") or {})
    if entities.pop("draft", None) is not None:
        store.update_task(task_id, now=now, bump_version=False, entities=entities)


def apply(
    store: Any, config: dict[str, Any], *, signals: list[dict[str, Any]], extract: dict[str, Any] | None,
    plan: dict[str, Any], now_dt: datetime, preferences: list[dict[str, Any]],
    existing_task: dict[str, Any] | None = None, owner_followup: bool = False, recall: dict[str, Any] | None = None,
    owner_bundle: bool = False, trigger: str = "",
) -> ActResult:
    """``owner_followup`` marks a bundle that answers a step the owner approved: its proposals are
    delivered now and outside the daily notice cap, because the owner was promised a result. ``recall`` is
    the summary of what memory gave the planner; it goes to the audit, the only place it is visible.
    ``owner_bundle`` says the bundle is the owner's own words (an answer, a chat turn, mail they sent): the only
    thing that reopens a task they closed. ``trigger`` says what set this plan off when there is no bundle to read it
    from: "review" (the task's timer, nothing new) or "job_result" (a step Radar ran came back)."""
    now = iso(now_dt)
    language = config.get("owner", {}).get("language", "es")
    extract = extract or {}
    match = plan["task_match"]
    decision = match["decision"]
    planned = plan["task"]
    facts_written = 0
    # What a model reads into a mail or a chat is a hypothesis, never knowledge: it is stored as "inferred",
    # capped below any fact the owner stated, and the planner must ask before building on it.
    for fact in extract.get("facts_learned") or []:
        if fact.get("confidence", 0) >= MIN_FACT_CONFIDENCE:
            try:
                store.upsert_fact(
                    key=fact["key"], value=fact["value"], confidence=min(float(fact["confidence"]), INFERRED_FACT_CAP),
                    provenance={"signal_id": signals[0]["id"] if signals else None, "at": now, "origin": "inferred"}, now=now,
                )
                facts_written += 1
            except (ValueError, KeyError, TypeError):
                continue

    task = existing_task
    if task is None and decision in ("advances", "completes", "contradicts") and match.get("task_id"):
        try:
            task = store.get_task(match["task_id"])
        except KeyError:
            task = None
            decision = "new"
    asks = [c for c in plan.get("checks") or [] if c.get("ask_user")]
    promoted = False
    if task is None and decision == "none" and not (plan.get("proposals") or asks):
        # «Nothing to track» does not stand against the owner's own rule: a sender they asked to hear about, or engaged
        # with before («prefer»), is recorded and told as news even when the planner would have let it go (on
        # 2026-11-07 the same cancellation was «none» on one run and «new» on the next; the rule must not depend on that).
        kind_hint = planned.get("kind") if planned.get("kind") in TASK_KINDS else (extract.get("category") if extract.get("category") in TASK_KINDS else "other")
        louder = [p for p in applicable(preferences, _observation(signals, kind_hint)) if p.get("effect") in ("notify", "prefer")]
        if not louder or not plan.get("message"):
            return ActResult(None, None, "none", "silent", "no_task", facts=facts_written, message=plan.get("message", ""))
        decision, promoted = "new", True

    created = False
    if task is None:
        kind = planned.get("kind") if planned.get("kind") in TASK_KINDS else (extract.get("category") if extract.get("category") in TASK_KINDS else "other")
        if decision == "none":
            kind = "check" if asks else kind
            decision = "new"
        task_id = str(uuid4())
        title = planned.get("title") or extract.get("title") or (signals[0].get("title") if signals else "") or kind
        resolved_id = recently_resolved(
            store, kind, title, now_dt, group_key=str(signals[0].get("group_key") or "") if signals else "",
            account=str((signals[0].get("meta") or {}).get("account") or "") if signals else "",
            identifiers=[str(i.get("value") or "") for i in (extract.get("entities") or {}).get("identifiers") or [] if isinstance(i, dict)],
        )
        if resolved_id:
            # The owner finished this subject (or it closed minutes ago); a re-plan, a lagging job result or a new
            # message of the thread is raising it again. The signals stay on the closed task, visible in the panel.
            for signal in signals:
                try:
                    store.link_task_signal(resolved_id, signal["id"], "context", now)
                except Exception:
                    pass
            return ActResult(resolved_id, None, "suppressed", "silent", "recently_resolved", facts=facts_written, message=plan.get("message", ""))
        checklist = merge_checklist(template_for(kind), plan.get("checklist") or [])
        due_at = parse_iso(planned.get("due_at"))
        review_at = parse_iso(planned.get("next_review_at"))
        entities = dict(extract.get("entities") or {})
        if session_of(signals):
            entities["session_id"] = session_of(signals)
        if extract.get("implications"):
            entities["implications"] = list(extract["implications"])  # what the bundle implies: re-asked of memory at every review
        if str(extract.get("sender_kind") or "") == "cold":
            entities["sender_kind"] = "cold"  # who wrote does not change with what a later lookup finds
        store.create_task(
            task_id=task_id, kind=kind, title=title[:200], summary=planned.get("summary") or extract.get("summary") or "",
            entities=entities, expectations=extract.get("expectations") or [],
            checklist=checklist, now=now, next_review_at=iso(review_at) if review_at else None,
            due_at=iso(due_at) if due_at else None, confidence=planned.get("confidence", 0.5),
            origin_signal_id=signals[0]["id"] if signals else None,
        )
        task = store.get_task(task_id)
        created = True
        roles = ["origin"] + ["context"] * (len(signals) - 1)
    else:
        task_id = task["id"]
        fields: dict[str, Any] = {}
        if planned.get("title") and decision in ("advances", "new"):
            fields["title"] = planned["title"][:200]
        if planned.get("summary"):
            fields["summary"] = planned["summary"]
        fields["entities"] = _merge_entities(task.get("entities") or {}, extract.get("entities") or {})
        if extract.get("implications"):
            fields["entities"] = _merge_entities(fields["entities"], {"implications": list(extract["implications"])})
        fields["expectations"] = _merge_expectations(task.get("expectations") or [], extract.get("expectations") or [])
        fields["checklist"] = merge_checklist(task.get("checklist") or template_for(task.get("kind", "other")), plan.get("checklist") or [])
        due_at = parse_iso(planned.get("due_at"))
        if due_at:
            fields["due_at"] = iso(due_at)
        review_at = parse_iso(planned.get("next_review_at"))
        fields["next_review_at"] = iso(review_at) if review_at else None
        try:
            fields["confidence"] = float(planned.get("confidence", task.get("confidence", 0.5)))
        except (TypeError, ValueError):
            pass
        task = store.update_task(task_id, now=now, **fields)
        role = {"advances": "advances", "completes": "completes", "contradicts": "contradicts"}.get(decision, "context")
        roles = [role] * len(signals)
    for signal, role in zip(signals, roles):
        try:
            store.link_task_signal(task_id, signal["id"], role, now)
        except Exception:
            pass

    draft = remember_draft(store, task, extract, now)
    remember_branch(store, task, extract, now)
    # Questions of this task sitting in the owner's chat right now; a closing plan withdraws them (below, by the
    # status change) and may owe the owner a word for it (see ``correction``).
    asked = [] if created else [p for p in store.list_proposals(statuses=("sent",), task_id=task_id, limit=20) if decision_like(p)]

    target_status = planned.get("status") or ("done" if decision == "completes" else task.get("status"))
    if decision == "completes" and target_status not in TASK_TERMINAL:
        target_status = "done"
    if promoted:
        target_status = "done"  # news the planner saw nothing to track in: told because the owner's rule says so, then filed
    # What the owner closed stays closed to everyone but them: the plan's status, steps and news are dropped, the
    # bundle only lands on the task. Their own words (an answer, a chat, their mail) and the result of a step they
    # approved still get through.
    kept_closed = owner_closed(task) and not owner_bundle and not owner_followup
    if kept_closed:
        target_status = task.get("status")
    kept_open = False
    if target_status == "done" and decision not in ("completes", "contradicts") and task.get("kind") == "trip" and lies_ahead(task, now_dt):
        # A booking confirmation is a booked step, never the end of the trip: on 2026-10-28 «reserva completada»
        # closed a car rental two weeks before the pickup. Only the owner's word or a cancellation ends it early.
        target_status = task.get("status") if task.get("status") not in TASK_TERMINAL else "open"
        kept_open = True
    if target_status and target_status != task.get("status"):
        try:
            task = store.set_task_status(task_id, target_status, now=now, reason=match.get("why") or plan.get("notify", {}).get("reason"))
        except (ValueError, RuntimeError):
            pass

    attention = resolve(
        _observation(signals, task.get("kind", "other")), preferences,
        model_decision=plan["notify"]["disposition"], risk_flags=extract.get("risk_flags") or [],
        info_complete=True, muted=bool(task.get("muted")),
    )
    disposition = attention.disposition
    if attention.raised and str(plan["notify"].get("importance") or "") != "high":
        # The owner has engaged with this family before (or asked to be told): the model's «low» does not stand.
        plan = {**plan, "notify": {**plan["notify"], "importance": "high"}}
    urgency = str(plan["notify"].get("urgency") or "today")
    if disposition == "notify_now" and attention.reason in ("model_evaluation", "protected_exception", "preference_prefer") and urgency != "now":
        disposition = "digest"  # only what cannot wait interrupts; a protected risk flag keeps it out of silence, not out of the summary
    if task_session_active(store, task, now_dt, config) and not owner_followup:
        disposition = "silent"  # the owner is already in that conversation
    if kept_closed:
        disposition = "silent"
    escalation = "inform" if promoted else str((plan.get("escalation") or {}).get("decision") or "ask")
    terminal = task.get("status") in TASK_TERMINAL
    conversation = bool(session_of(signals))
    if (conversation or owner_authored(signals, config)) and (terminal or escalation == "inform") and not owner_followup:
        disposition = "silent"  # the owner lived it or wrote it: nothing to report back
    if conversation and disposition == "notify_now" and not owner_followup:
        # The owner was in that chat: what it leaves pending rides the reminder, never an interruption. On 2026-11-04 a
        # checkout chat judged after its order confirmation had arrived interrupted them to «confirm the payment».
        disposition = "digest"
    if extract.get("owner_reply") and not owner_followup:
        disposition = "silent"  # the owner answered the thread themselves: they know; the plan only updates the task
    # A task born in a chat is the owner's own work with their agent. It reaches them again only for something that
    # chat did not see (a mail, the result of a step) or a deadline; a plan made from the chat itself or from the
    # task's timer leaves its decisions in the panel (owner's rule, 2026-11-11: seven of the eight notices that were
    # noise on 9-10 November restated as a question what they had just worked through in the chat). And what Radar
    # itself set off there (a timed review, a step of its own) interrupts only when the task is due within a day.
    chat_born = bool((task.get("entities") or {}).get("session_id"))
    due_dt = parse_iso(task.get("due_at"))
    has_deadline = due_dt is not None and due_dt > now_dt
    due_within_horizon = has_deadline and due_dt - now_dt <= timedelta(hours=INTERRUPT_HORIZON_HOURS)
    in_panel = chat_born and (conversation or trigger == "review") and not owner_followup and not has_deadline
    own_doing = chat_born and (conversation or trigger in ("review", "job_result")) and not owner_followup
    if in_panel:
        disposition = "silent"
    elif disposition == "notify_now" and own_doing and not due_within_horizon and attention.reason != "protected_exception":
        disposition = "digest"  # what only Radar's own review or step raised in a chat task interrupts for nothing due days away
    if (extract or {}).get("manipulation"):
        # A message that gives the assistant orders gets no step of work and none towards its sender: only the owner's
        # check or the news. Nothing runs on its behalf (2026-11-03: the planner proposed «investigate the origin of the
        # mail» and a job started reading it, free to visit the link the mail pushed).
        plan = {**plan, "proposals": [p for p in plan.get("proposals") or [] if p.get("operation") in MANIPULATION_STEPS]}
    # A probable mistake in a booking (a rental far short of the drive, hotel nights short of the flight, a delivery due
    # while away: a check of severity high) is told at once, while it can still be undone, and counts as important
    # whatever the plan said (owner's rule, 2026-11-07: «es suficientemente importante y podría ser un error al reservar»).
    high_check = any(c.get("severity") == "high" for c in asks) and not owner_followup and not conversation and not own_doing  # the chat rides the reminder
    if high_check and disposition == "digest":
        disposition, urgency = "notify_now", "now"
    # The owner just answered a question of this task: the decision their answer leads to is put to them at once, not
    # in the next reminder. On 2026-11-11 «cancel it» led to «cancel the subscription?», which waited for the summary
    # under a notice that had just said «I'll carry on from there and report back».
    answered_now = any(s.get("kind") == "user_reply" and (s.get("meta") or {}).get("proposal_id") for s in signals)
    owner_steps = [p for p in plan.get("proposals") or [] if p.get("action_class") != "read" or p.get("operation") == "ask"]
    answer_step = (answered_now and bool(owner_steps) and disposition == "digest" and not terminal and not kept_closed
                   and not (extract or {}).get("manipulation"))
    if answer_step:
        disposition, urgency = "notify_now", "now"
    read_steps = [p for p in plan.get("proposals") or [] if p.get("action_class") == "read" and p.get("operation") not in ("check", "inform")]
    if (escalation == "handle" and read_steps and not owner_followup and disposition != "silent" and not high_check
            and may_auto_run(config.get("trust") or {}, "read", kind=task.get("kind"), operation_id=read_steps[0].get("operation") or None)):
        disposition = "silent"  # the assistant looks first; the result re-plans and decides inform or ask
    inform_only = escalation == "inform" and not (plan.get("proposals") or asks) and disposition != "silent" and bool(plan.get("message")) and not kept_closed
    # A decision only the owner can take, with no step to offer: the message itself travels, answered in words.
    ask_only = (not terminal and not inform_only and not owner_followup and (escalation == "ask" or task.get("status") == "needs_user")
                and not (plan.get("proposals") or asks) and disposition != "silent" and bool(plan.get("message")))
    proposal_ids: list[str] = []
    if not terminal or inform_only:  # news about a task that just completed still reaches the owner
        batch_key = "now" if owner_followup else {"notify_now": "now", "digest": "digest", "silent": "silent"}[disposition]
        if batch_key == "now" and not owner_followup and not answer_step and noticed_recently(store, config, task_id, now_dt):
            batch_key = "digest"  # the owner was just told about this task (answered or not): no second interruption
        slot = next_delivery(now_dt, "today" if urgency == "now" else urgency, config) if batch_key == "digest" else None
        candidates: list[dict[str, Any]] = []
        for check in asks:
            candidates.append({
                "title": check["description"], "rationale": "", "action_class": "read", "operation": "check", "severity": check.get("severity"),
                "when": "", "urgency": "high" if check.get("severity") == "high" else "normal",
                "options": [{"id": "ok", "label": t(language, "btn_ok")}, {"id": "wrong", "label": t(language, "btn_wrong")}],
            })
        candidates.extend(plan.get("proposals") or [])
        important = str(plan["notify"].get("importance") or "normal") == "high"
        pressing = due_soon(task, now_dt)
        # Who writes decides what may be asked or sent: a cold sender (a pitch, a sale, a confirmation of something the
        # owner never started) gets no reply and raises no question; an invitation from someone the owner has never
        # corresponded with is recorded for the panel and raised nowhere.
        cold = ("cold" in (str(extract.get("sender_kind") or ""), str((task.get("entities") or {}).get("sender_kind") or ""))) and not owner_bundle
        history = extract.get("invitation_history") if isinstance(extract.get("invitation_history"), dict) else {}
        invitation_unknown = (str(extract.get("message_kind") or "") == "invitation" and extract.get("known_correspondent") is False
                              and not history.get("attends") and not owner_bundle and not owner_followup)
        # A plan that settles the task and takes back a question the owner was just asked owes them a word now, not in
        # the next reminder: «Brightbot cancelled the meeting; forget my question» (2026-11-03 the question went out at
        # 11:53, the cancellation at 11:56, and the owner heard of it at 23:52).
        correction = terminal and inform_only and not owner_followup and bool(asked) and noticed_recently(store, config, task_id, now_dt)
        withheld = ""
        if inform_only or ask_only:
            headline = re.split(r"(?<=[.!?])\s", str(plan.get("message") or "").strip(), maxsplit=1)[0]
            protected = attention.reason == "protected_exception"  # a security flag: the news itself crosses quiet hours
            # News is told only when it matters (money at risk, security, a dated commitment, what the owner engaged with
            # before), a step they approved reports back, or a question they were asked is withdrawn. A decision left to them
            # in words is sent when it matters or presses; otherwise both wait on the task, visible in the panel. In the week
            # of 2026-10-31 eight receipts, closed issues and "can you confirm with a 👍?" rode the reminder for nothing.
            told = (important or protected or owner_followup or correction or (ask_only and pressing)) and not invitation_unknown and (protected or not cold)
            if told:
                candidates.append({
                    "title": _clip_title(headline, 80), "rationale": "", "action_class": "read", "operation": "inform", "when": "",
                    "urgency": "high" if (important and ask_only) or protected else "normal" if disposition == "notify_now" else "low",
                    "options": [{"id": "ok", "label": t(language, "btn_ack")}], "ask": ask_only, "protected": protected,
                })
            else:
                withheld = "invitation_unknown" if invitation_unknown else "cold_sender" if cold else "question" if ask_only else "news"
        if invitation_unknown:
            candidates, withheld = [], "invitation_unknown"
        # A sensitive change to an account (its mail, its second factor, where it pays) is told, never asked about: the
        # owner does not want «was it you?» (2026-11-09), and whatever the plan wrote this floor holds.
        account_asks = 0
        if rules.account_change_bundle(signals):
            kept = [c for c in candidates if c.get("operation") not in ("ask", "check")]
            account_asks, candidates = len(candidates) - len(kept), kept
            for item in candidates:
                if item.get("operation") == "inform":
                    item["ask"] = False
            if not any(c.get("operation") == "inform" for c in candidates):
                candidates.append({
                    "title": _clip_title(str(task.get("title") or ""), 80), "rationale": "", "action_class": "read", "operation": "inform", "when": "",
                    "urgency": "high", "options": [{"id": "ok", "label": t(language, "btn_ack")}], "ask": False, "protected": True,
                })
                withheld = ""
        candidates = collapse(candidates)  # the same doubt as a check and as an ask is one question
        if (extract or {}).get("manipulation"):
            candidates = [c for c in candidates if c.get("operation") in MANIPULATION_STEPS]  # see the plan filter above
        declined = declined_recently(store, task_id, now_dt)
        superseded_ids: list[str] = []
        if not created:
            # A re-plan replaces the open steps: the same step is updated in place, the rest is withdrawn below.
            candidates, superseded_ids = reconcile(store, task_id, candidates, now)
        suppressed = 0
        updated = 0
        restored = 0
        attempted = 0
        asks_dropped = account_asks
        drafts_dropped = 0
        closed_subject = 0
        guarded = not owner_bundle and not owner_followup  # the owner's own words may raise anything again
        closed_refs = owner_closed_identifiers(store, now_dt, exclude=task_id) if guarded else set()
        answered_keys = owner_fact_keys(store) if guarded else set()
        held: list[dict[str, Any]] = []
        offered: set[str] = set()
        for item in candidates:
            if attempted >= 4:
                break
            operation = str(item.get("operation") or "")
            if not ask_leads_somewhere(item, important=important, pressing=pressing):
                asks_dropped += 1  # an answer that would change nothing is not asked for
                continue
            question = operation in ("ask", "check") or bool(item.get("ask"))
            if question and closed_refs and identifier_tokens(item.get("title")) & closed_refs:
                closed_subject += 1  # it names what the owner closed in another task: not asked again from this one
                continue
            if operation == "ask" and isinstance(item.get("fact"), dict) and " ".join(str(item["fact"].get("key") or "").split()).lower() in answered_keys:
                asks_dropped += 1  # the owner already answered this very question: the fact is on record
                continue
            if question and guarded and not item.get("existing_id") and asked_elsewhere(store, task_id, str(item.get("title") or ""), now_dt):
                asks_dropped += 1  # the same question is already in front of them from another task
                continue
            if cold and (operation == "ask" or operation.startswith("gmail.")):
                drafts_dropped += 1  # nothing goes back to, or is asked about, a cold sender
                continue
            if operation.startswith("gmail.") and not draft_basis_holds(store, task, item, extract, owner_bundle=owner_bundle):
                drafts_dropped += 1  # a reply with nothing verifiable behind it is not offered
                continue
            if matches_declined(item, declined):
                suppressed += 1
                continue
            awaits, fallback = str(item.get("awaits") or "").strip(), parse_iso(item.get("when"))
            if awaits and fallback and fallback > now_dt and not item.get("existing_id") and not item.get("restore_id"):
                # A step that can only be done once something announces itself (the carrier opening the check-in, a
                # parcel delivered, a refund landed) is not offered before the announcement: on 2026-11-07 a check-in
                # step timed by guesswork would have gone out before the airline's mail, and before the check-in was
                # open had the flight been later in the day. The task expects the event and is reviewed at the
                # fallback moment, the latest the announcement should have come; the step is proposed then, or when
                # the announcement arrives and the plan sees the expectation fulfilled.
                held.append({"description": awaits, "kind": "event", "by": iso(fallback), "then": item["title"]})
                continue
            attempted += 1
            offered.add(" ".join(str(item.get("title") or "").lower().split()))
            when = fallback
            item_batch = batch_key
            if when is not None and when > now_dt:
                # A step for a chosen moment is its own notice at that moment, never summary filler — unless that
                # moment falls within the cooldown of a message the owner just got about the task: then it waits
                # for its moment and rides the next reminder (a planner timing a step minutes ahead is no emergency).
                item_batch = "now" if owner_followup or not noticed_recently(store, config, task_id, when) else "digest"
            elif slot is not None and (when is None or when < slot):
                when = slot  # grouped news waits for its summary slot
            if item.get("operation") not in ("check", "inform", "ask") and not needs_consent(config, task, item):
                item_batch = "auto"  # the assistant's own work: it runs by itself and is never offered as a button
            elif item_batch == "silent" and (important or pressing) and needs_consent(config, task, item) and not in_panel:
                # A decision that matters is never left where nobody sees it: on 2026-11-13 the check-in step of the
                # next morning's flight was proposed with a silent plan, which would have kept it in the panel unseen.
                item_batch = "digest"
            elif item_batch == "silent" and operation == "check" and item.get("severity") in ("warning", "high") and not in_panel:
                item_batch = "digest"  # a question the owner must answer (a hotel night short of the flight, 2026-11-07) is never silent
            if high_check and operation != "check" and item_batch == "now" and needs_consent(config, task, item):
                item_batch = "digest"  # the interruption carries the mistake alone; the other decisions ride the reminder
            timed = when is not None and when > now_dt  # a step for a chosen moment keeps its moment
            if (item_batch == "now" and not timed and not owner_followup and not correction and not high_check
                    and attention.reason != "protected_exception" and not item.get("protected")
                    and own_doing and not due_within_horizon):
                item_batch = "digest"
            item_importance = "high" if operation == "check" and item.get("severity") == "high" else str(plan["notify"].get("importance") or "normal")
            if (item.get("operation") == "inform" and item_batch == "now" and not owner_followup
                    and getattr(store, "last_notice_at", None) and store.last_notice_at(task_id)):
                # Never a second cold interrupt about one task: a re-raised piece of news or a question the owner
                # already saw and let lapse waits for the reminder. A first, genuinely urgent one still interrupts.
                item_batch = "digest"
            if correction and item.get("operation") == "inform":
                item_batch, when = "now", None  # the one exception: the word that cancels a question just asked
            deliver_after = iso(when) if when and when > now_dt else ""
            ttl = timedelta(hours=SCHEDULED_TTL_HOURS) if deliver_after else timedelta(hours=PROPOSAL_TTL_HOURS)
            expires_at = iso((when if deliver_after else now_dt) + ttl)
            fingerprint = canonical_hash({"task": task_id, "title": " ".join(item["title"].lower().split())})
            options = item.get("options") or [{"id": "do", "label": t(language, "btn_do")}, {"id": "no", "label": t(language, "btn_no")}]
            params = {"when": item.get("when", ""), "deliver_after": deliver_after, "rationale": item.get("rationale", ""),
                      "message": plan.get("message", ""), "urgency": urgency, "importance": item_importance,
                      **({"owner_followup": True} if owner_followup else {}),
                      **({"correction": True} if correction and item.get("operation") == "inform" else {}),
                      **({"ask": True} if item.get("ask") else {}),
                      **({"protected": True} if item.get("protected") else {}),  # a security flag: crosses quiet hours
                      **({"own_doing": True} if own_doing else {}),  # raised by Radar's own review or step in a chat task: never wakes the owner
                      **({"fact": item["fact"]} if isinstance(item.get("fact"), dict) and item["fact"].get("key") else {}),
                      **({"then": item["then"]} if item.get("then") else {}), **({"basis": item["basis"]} if item.get("basis") else {}),
                      **({"draft": draft["text"], "draft_ref": draft.get("ref", "")} if draft else {})}
            existing_id = item.get("existing_id") or item.get("restore_id")
            if existing_id:
                # The step the owner already has (open, or withdrawn by the last plan and wanted again) is rewritten in
                # place: its id, and the number on their screen, keep pointing at it; nothing is sent twice.
                try:
                    if item.get("restore_id"):
                        store.restore_proposal(existing_id, now=now)
                    row = store.update_proposal(
                        existing_id, now=now, title=item["title"], reason_text=item.get("rationale") or "", params=params, options=options,
                        urgency=item.get("urgency", "normal"), expires_at=max(str(item.get("existing_expires_at") or ""), expires_at),
                        fingerprint=fingerprint,
                        # a plan with nothing new never takes back from the reminder what an earlier one put there
                        batch_key=item_batch if item.get("existing_status") == "pending" and not in_panel else None,
                    )
                except (KeyError, ValueError, RuntimeError):
                    suppressed += 1
                    continue
                if item.get("restore_id"):
                    restored += 1
                else:
                    updated += 1
                if row.get("status") == "pending":
                    proposal_ids.append(existing_id)
                continue
            result = store.create_task_proposal(
                proposal_id=str(uuid4()), task_id=task_id, title=item["title"], reason_text=item.get("rationale") or "",
                action_class=item.get("action_class", "read"), operation_id=item.get("operation") or None, params=params,
                options=options, urgency=item.get("urgency", "normal"), expires_at=expires_at, fingerprint=fingerprint,
                now=now, batch_key=item_batch,
            )
            if result.get("created"):
                proposal_ids.append(result["id"])
        withdrawn = store.supersede_proposals(superseded_ids, now=now, by="replan") if superseded_ids else 0
        # The steps the task awaits: those held now and those held by an earlier plan and still unfulfilled. The step
        # the plan finally offers fulfils its expectation; while any is pending, the task is reviewed at the earliest
        # fallback moment at the latest, whatever this plan said (a review in between must not lose the fallback).
        expectations = [dict(e) for e in (task.get("expectations") or []) if isinstance(e, dict)]
        fulfilled = 0
        for expectation in expectations:
            if expectation.get("then") and not expectation.get("fulfilled") and " ".join(str(expectation["then"]).lower().split()) in offered:
                expectation["fulfilled"] = True
                fulfilled += 1
        moments = [parse_iso(h["by"]) for h in held] + [parse_iso(e.get("by")) for e in expectations if e.get("then") and not e.get("fulfilled")]
        ahead = [m for m in moments if m is not None and m > now_dt]
        if held or fulfilled or ahead:
            fields = {"expectations": _merge_expectations(expectations, held)}
            planned_review = parse_iso(planned.get("next_review_at"))
            if ahead and (planned_review is None or planned_review > min(ahead)):
                planned["next_review_at"] = iso(min(ahead))  # the review below is scheduled for the fallback moment at the latest
                fields["next_review_at"] = planned["next_review_at"]
            task = store.update_task(task_id, now=now, bump_version=False, **fields)
        review_at = parse_iso(planned.get("next_review_at"))
        if review_at and review_at > now_dt:
            store.upsert_followup(
                followup_id=f"task:{task_id}:review", goal_id="tasks", goal_title="Task reviews", reason="next_step",
                subject_key=f"task:{task_id}", next_check_at=iso(review_at), expires_at=iso(review_at + timedelta(days=30)),
                assumptions={"kind": "task_review", "task_id": task_id}, now=now, task_id=task_id,
                timezone=config.get("owner", {}).get("timezone", "UTC"),
            )
    else:
        suppressed = updated = restored = withdrawn = asks_dropped = drafts_dropped = closed_subject = 0
        held = []
        withheld = ""
        store.cancel_task_followups(task_id, now)
    if withheld == "invitation_unknown":
        disposition = "silent"
    store.add_audit(
        audit_id=str(uuid4()), event_type="pipeline", subject_type="task", subject_id=task_id,
        correlation_id=signals[0]["id"] if signals else None, actor_kind="scheduler", decision_code=decision,
        metadata={"disposition": disposition, "attention": attention.reason, "urgency": urgency,
                  "importance": "high" if high_check else str(plan["notify"].get("importance") or "normal"),
                  "proposals": len(proposal_ids),
                  "suppressed": suppressed, "updated": updated, "superseded": withdrawn, "status": task.get("status"), "created": created,
                  **({"restored": restored} if restored else {}),
                  **({"recall": recall} if recall else {}), **({"kept_open": "future_dates"} if kept_open else {}),
                  **({"kept_closed": "owner_closed"} if kept_closed else {}),
                  **({"asks_dropped": asks_dropped} if asks_dropped else {}), **({"drafts_dropped": drafts_dropped} if drafts_dropped else {}),
                  **({"closed_subject": closed_subject} if closed_subject else {}), **({"panel": "chat_nothing_new"} if in_panel else {}),
                  **({"held": len(held)} if held else {}), **({"withheld": withheld} if withheld else {})}, now=now,
    )
    return ActResult(
        task_id=task_id, task_status=task.get("status"), decision=decision, disposition=disposition,
        attention_reason="owner_closed" if kept_closed else attention.reason, proposals=proposal_ids, facts=facts_written,
        message=plan.get("message", ""), created=created,
    )
