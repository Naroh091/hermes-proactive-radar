"""Telegram surface: button callbacks, free-text replies to notices and the /radar command."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from ..i18n import t
from ..jobs.service import start_or_wait
from ..trust import describe, level_summary
from .brief import agent_brief
from .feedback import understand
from .dispatch import draft_document
from .notice import CALLBACK_PREFIX, DRAFT_INLINE, MAX_LABEL, TASK_PREFIX, decision_buttons, edited_text, fit, proposal_buttons, task_buttons
from .responses import OPEN_PROPOSAL, act_on_task, local_now, record_owner_words, resolve_reply, respond_to_proposal
from .snooze import deadline_of, later_button

log = logging.getLogger(__name__)


def authorize_update(config: dict[str, Any], update: Any) -> tuple[bool, str]:
    language = config.get("owner", {}).get("language", "es")
    destination = config.get("destination") or {}
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    if not config.get("enabled") or destination.get("platform") != "telegram":
        return False, t(language, "not_configured")
    if user is None or str(user.id) != str(destination.get("owner_platform_id")):
        return False, t(language, "actor_unauthorized")
    expected_chat = (destination.get("address") or {}).get("chat_id")
    if chat is None or str(chat.id) != str(expected_chat):
        return False, t(language, "wrong_destination")
    if getattr(chat, "type", "") != "private" and not destination.get("allow_groups"):
        return False, t(language, "groups_forbidden")
    return True, ""


def _after_accept(rt: Any, result: dict[str, Any], option: str) -> str:
    """Start the approved step and say honestly whether it is running. Returns text to append to the answer."""
    proposal = result.get("proposal") or {}
    if result.get("outcome") != "accepted" or not proposal or proposal.get("operation_id") in ("check", "inform"):
        return ""
    language = rt.config.get("owner", {}).get("language", "es")
    try:
        job, reason = start_or_wait(rt.store, rt.config, proposal=proposal, option=option, now_dt=rt.clock())
    except Exception as exc:
        log.warning("job not queued for proposal %s: %s", proposal.get("id"), exc)
        return t(language, "job_not_queued", reason=t(language, "block_error"))
    if job is not None:
        return t(language, "job_queued")
    return t(language, "job_not_queued", reason=t(language, f"block_{reason or 'unknown'}"))


def with_followup(result: dict[str, Any], suffix: str) -> dict[str, Any]:
    if suffix:
        result["text"] = f"{result.get('text', '')} {suffix}".strip()
    return result


def remaining_keyboard(rt: Any, outbox_row: dict[str, Any] | None) -> list[list[dict[str, str]]]:
    if not outbox_row:
        return []
    language = rt.config.get("owner", {}).get("language", "es")
    payload = outbox_row.get("payload") or {}
    rows: list[list[dict[str, str]]] = []
    task_id = outbox_row.get("task_id") or payload.get("task_id")
    open_count = 0
    reminder = outbox_row.get("kind") == "digest"
    several = len(payload.get("proposal_ids") or []) > 1
    for index, proposal_id in enumerate(payload.get("proposal_ids") or [], 1):
        try:
            proposal = rt.store.get_proposal(proposal_id)
        except KeyError:
            continue
        if proposal.get("status") not in OPEN_PROPOSAL:
            continue
        if reminder:
            row = decision_buttons(proposal, language, index=index if several else 0)
            if not row:
                continue
        else:
            row = proposal_buttons(proposal, language)
            if several:
                row[0]["label"] = fit(f"{index}️⃣ {row[0]['label']}", MAX_LABEL)
        rows.append(row)
        open_count += 1
    try:
        task = rt.store.get_task(task_id) if task_id else {}
    except KeyError:
        task = {}
    now_dt = rt.clock()
    if reminder:
        if task_id and open_count and not several:
            later = later_button(task_id, language, due_at=deadline_of(task, now_dt), now_dt=now_dt, config=rt.config)
            if later:
                rows.append([later])
        return rows
    if task_id and open_count and task:
        if task.get("status") not in {"done", "abandoned", "expired"} and not task.get("muted"):
            rows.extend(task_buttons(task_id, language, due_at=deadline_of(task, now_dt), now_dt=now_dt, config=rt.config))
    return rows


async def handle_callback(rt: Any, update: Any, context: Any = None) -> None:
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    data = str(getattr(query, "data", "") or "")
    ok, reason = authorize_update(rt.config, update)
    if not ok:
        await query.answer(reason[:200])
        return
    parts = data.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    prefix, ident, option = parts
    actor = f"telegram:{getattr(getattr(update, 'effective_user', None), 'id', '')}"
    now_dt = rt.clock()
    if prefix == CALLBACK_PREFIX:
        result = respond_to_proposal(rt.store, rt.config, proposal_id=ident, option=option, actor=actor, now_dt=now_dt)
        if result.get("outcome") == "review":
            try:
                await query.answer(str(result.get("text", ""))[:200])
            except Exception:
                pass
            await send_detail(rt, result)  # the notice and its buttons stay as they are
            return
        with_followup(result, _after_accept(rt, result, option))
    elif prefix == TASK_PREFIX:
        result = act_on_task(rt.store, rt.config, task_id=ident, action=option, actor=actor, now_dt=now_dt)
    else:
        await query.answer()
        return
    try:
        await query.answer(str(result.get("text", ""))[:200])
    except Exception:
        pass
    message = getattr(query, "message", None)
    if message is None or result.get("outcome") == "stale":
        return
    try:
        outbox_row = rt.store.outbox_by_platform_message(str(message.message_id))
        original = getattr(message, "text_html", None) or (outbox_row or {}).get("payload", {}).get("text") or getattr(message, "text", "")
        buttons = remaining_keyboard(rt, outbox_row)
        destination = {"address": {"chat_id": message.chat_id}}
        await rt.transports.edit("telegram", destination, str(message.message_id), {
            "text": edited_text(original, str(result.get("text", ""))), "parse_mode": "HTML", "buttons": buttons,
        })
    except Exception as exc:
        log.debug("notice edit skipped: %s", exc)


DETAIL_REPEAT_SECONDS = 120


def _detail_just_sent(store: Any, proposal_id: str, now_dt: Any) -> bool:
    """True when the detail of this proposal went out less than DETAIL_REPEAT_SECONDS ago (on 2026-11-09 two taps
    nine seconds apart sent the same long message twice, both times it was asked for)."""
    if not proposal_id:
        return False
    try:
        rows = store.list_outbox(limit=20)
    except Exception:
        return False
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if row.get("kind") != "detail" or proposal_id not in (payload.get("proposal_ids") or []):
            continue
        try:
            sent = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if abs((now_dt - sent).total_seconds()) < DETAIL_REPEAT_SECONDS:
            return True
    return False


async def send_detail(rt: Any, result: dict[str, Any]) -> bool:
    """Send the full draft or details as its own message and record it, so a reply to it still resolves."""
    proposal = result.get("proposal") or {}
    text = str(result.get("detail") or "")
    home = destination_platform(rt)
    if not text or not rt.transports.available(home):
        return False
    language = rt.config.get("owner", {}).get("language", "es")
    now_dt = rt.clock()
    if _detail_just_sent(rt.store, str(proposal.get("id") or ""), now_dt):
        return True  # a second tap on «see it first»: the detail is already on their screen
    payload: dict[str, Any] = {"text": fit(text, 3900), "parse_mode": None, "kind": "detail", "urgent": True, "exempt": True,
                               "proposal_ids": [proposal.get("id")], "task_id": proposal.get("task_id")}
    draft = str((proposal.get("params") or {}).get("draft") or "")
    if len(text) > 3900 and draft:
        document = draft_document(str(proposal.get("task_id") or proposal.get("id") or "draft"), draft, language=language, now_dt=now_dt)
        if document:
            payload["document"] = document
    try:
        destination = rt.store.destination("configured:primary")
        receipt = await rt.transports.send(home, destination, payload)
    except Exception as exc:
        log.warning("detail message not sent: %s", exc)
        return False
    message_id = getattr(receipt, "platform_message_id", None)
    if message_id:
        now = iso_now(rt)
        rt.store.record_sent(
            outbox_id=str(uuid4()), destination_id="configured:primary", notification_key=f"detail:{proposal.get('id')}:{now}",
            payload=payload, platform_message_id=str(message_id), now=now, task_id=proposal.get("task_id"), kind="detail",
        )
    return True


def destination_platform(rt: Any) -> str:
    """The platform the owner chose to be reached on (Telegram unless the configuration says otherwise)."""
    return str((rt.config.get("destination") or {}).get("platform") or "telegram").strip().lower()


async def handle_text_reply(rt: Any, event: Any) -> dict[str, Any] | None:
    """Free-text reply to a notice (``pre_gateway_dispatch``). Returns a skip directive when handled."""
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform = getattr(platform, "value", platform)
    home = destination_platform(rt)
    if str(platform or "").strip().lower() != home:
        return None
    reply_to = getattr(event, "reply_to_message_id", None)
    if not reply_to:
        return None
    destination = rt.config.get("destination") or {}
    if str(getattr(source, "user_id", "")) != str(destination.get("owner_platform_id")):
        return None
    if str(getattr(source, "chat_id", "")) != str((destination.get("address") or {}).get("chat_id")):
        return None
    row = rt.store.outbox_by_platform_message(str(reply_to))
    if not row or row.get("kind") not in {"notice", "digest", "detail"}:
        return None
    text = str(getattr(event, "text", "") or "")
    # The model reads the reply, in any language, with the message and its decisions in front of it; the regex floor
    # settles trivial replies and stands in when the model cannot.
    intent = await understand(getattr(rt, "gate", None), text, outbox_row=row, store=rt.store, language=rt.config.get("owner", {}).get("language", "es"))
    actor = f"{home}:{getattr(source, 'user_id', '')}"
    spoken_later = intent.get("intent") == "later" and intent.get("source") == "model"
    if spoken_later:
        # More than the bare «later» the floor settles: a day, an hour, a condition. The agent reads it and snoozes the
        # task for the moment they named; the reader has no field for that and is not given one.
        return to_agent(rt, row, text)
    result = resolve_reply(rt.store, rt.config, outbox_row=row, intent=intent, actor=actor, now_dt=rt.clock(), note=text)
    from .learn import learn_from_reply

    # A lasting rule in the reply (the whole of it, or beside its answer) becomes a preference.
    result = await learn_from_reply(rt, intent=intent, text=text, task_id=row.get("task_id") or (row.get("payload") or {}).get("task_id"),
                                    actor=actor, now_dt=rt.clock(), result=result, outbox_row=row)
    if result.get("outcome") == "accepted":
        with_followup(result, _after_accept(rt, result, "do"))
    if result.get("outcome") in ("unclear", "noted"):
        # Words no button stands for: the task hears them (the next plan starts from them). When they are unclear the
        # chat agent still answers the owner (it can act on the task with radar_task); a plain yes or no to a message
        # with nothing to decide («noted») is acknowledged here and goes no further than the task.
        record_owner_words(rt.store, outbox_row=row, text=text, actor=actor, channel=home, now_dt=rt.clock(),
                           language=rt.config.get("owner", {}).get("language", "es"))
        if result.get("outcome") == "unclear":
            return to_agent(rt, row, text)
    if result.get("outcome") == "question":
        return to_agent(rt, row, text)  # a question for the agent, not an answer
    ack = str(result.get("text") or "")
    if ack and rt.transports.available(home):
        try:
            await rt.transports.send(home, rt.store.destination("configured:primary"), {"text": ack, "parse_mode": None})
        except Exception as exc:
            log.debug("reply ack not sent: %s", exc)
    return {"action": "skip", "reason": f"radar reply handled: {result.get('outcome')}"}


def to_agent(rt: Any, row: dict[str, Any], text: str) -> dict[str, Any] | None:
    """Hand the owner's reply to the chat agent with what it answers in front of it. Without the brief the agent got
    the bare words and had to guess which task they were about."""
    try:
        return {"action": "rewrite", "text": agent_brief(rt.store, rt.config, row, text, local_now(rt.config, rt.clock()))}
    except Exception as exc:
        log.warning("reply passed on without its brief: %s", exc)
        return None


def session_identity(getenv: Callable[[str, str], str] | None = None) -> dict[str, str]:
    """Platform, chat, thread and user of the session running a slash command (host session env)."""
    if getenv is None:
        try:
            from gateway.session_context import get_session_env as getenv  # type: ignore[assignment]
        except ImportError:
            import os

            getenv = lambda name, default="": os.environ.get(name, default)  # noqa: E731
    return {
        "platform": getenv("HERMES_SESSION_PLATFORM", ""), "chat_id": getenv("HERMES_SESSION_CHAT_ID", ""),
        "chat_type": getenv("HERMES_SESSION_CHAT_TYPE", ""), "thread_id": getenv("HERMES_SESSION_THREAD_ID", ""),
        "user_id": getenv("HERMES_SESSION_USER_ID", ""),
    }


def verify_from_session(rt: Any, identity: dict[str, str]) -> str:
    """``/radar verify``: bind the configured destination to the chat and user issuing the command."""
    language = rt.config.get("owner", {}).get("language", "es")
    destination = rt.config.get("destination") or {}
    home = destination_platform(rt)
    if not rt.config.get("enabled") or not destination.get("platform"):
        return t(language, "not_configured")
    if str(identity.get("platform") or "").strip().lower() != home:
        return t(language, "not_configured")
    if str(identity.get("user_id") or "") != str(destination.get("owner_platform_id") or ""):
        return t(language, "actor_detail", user=identity.get("user_id") or "?", expected=destination.get("owner_platform_id") or "?")
    expected_chat = str((destination.get("address") or {}).get("chat_id") or "")
    if str(identity.get("chat_id") or "") != expected_chat:
        thread = identity.get("thread_id") or ""
        return t(language, "wrong_destination_detail", chat=identity.get("chat_id") or "?",
                 chat_type=identity.get("chat_type") or "?", thread=f", tema {thread}" if thread and language.startswith("es") else (f", topic {thread}" if thread else ""),
                 expected=expected_chat or "?")
    chat_type = str(identity.get("chat_type") or "private").lower()
    kind = "private" if chat_type in {"private", "dm", "direct", ""} else "group"
    if kind == "group" and not destination.get("allow_groups"):
        return t(language, "groups_forbidden")
    address = {"chat_id": expected_chat}
    thread_id = identity.get("thread_id") or (destination.get("address") or {}).get("thread_id")
    if thread_id:
        address["thread_id"] = str(thread_id)
    rt.store.verify_destination(
        destination_id="configured:primary", platform=home,
        gateway_connection_ref=destination.get("gateway_connection_ref") or f"{home}:default",
        address=address, owner_platform_id=str(identity["user_id"]), kind=kind,
        capabilities={"text": True, "buttons": home == "telegram", "documents": True}, now=iso_now(rt),
    )
    return t(language, "verified")


def launch_now(rt: Any, outbox_id: Any) -> bool:
    """Send a queued message right away (``/radar next``), without waiting for the tick: True when the send was
    handed to the gateway's loop. The host runs a slash command in a worker thread (no loop of its own), so the
    delivery is submitted to the loop the scheduler runs on (``rt.loop``); inside a running loop it is spawned as a
    task. Without either (the CLI, an old host) the next tick sends it, as the first version did on 2026-11-06 at
    01:57, when the owner's «/radar next» answered «ahora mismo» and the message waited for the tick anyway."""
    scheduler = getattr(rt, "scheduler", None)
    if not outbox_id or scheduler is None:
        return False
    coroutine = scheduler.deliver_now(str(outbox_id))
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is None:
        loop = getattr(rt, "loop", None)
        if loop is None or loop.is_closed():
            coroutine.close()
            log.warning("radar next: no gateway loop to send at once; the next tick sends it")
            return False
        asyncio.run_coroutine_threadsafe(coroutine, loop)
        return True
    spawn = getattr(getattr(rt, "ctx", None), "spawn_task", None)
    try:
        if callable(spawn):
            spawn(coroutine, name="radar:next")
        else:
            running.create_task(coroutine, name="radar:next")
    except Exception as exc:
        coroutine.close()
        log.warning("radar next: not sent at once (%s); the next tick sends it", exc)
        return False
    return True


def _submit(rt: Any, coroutine: Any, *, name: str) -> bool:
    """Run ``coroutine`` on the gateway's loop from a command (a worker thread) or a running loop; False, and the
    coroutine closed, when there is no loop to run it on."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    try:
        if running is not None:
            running.create_task(coroutine, name=name)
            return True
        loop = getattr(rt, "loop", None)
        if loop is None or loop.is_closed():
            coroutine.close()
            return False
        asyncio.run_coroutine_threadsafe(coroutine, loop)
        return True
    except Exception as exc:
        coroutine.close()
        log.warning("%s: not launched (%s)", name, exc)
        return False


def launch_warmup(rt: Any) -> bool:
    """Run the habits pass at once (``/radar warmup``) and tell the owner what it found; False when there is no loop
    to run it on (the command then leaves a request for the next tick)."""
    from ..warmup import describe, run as warmup_run

    language = rt.config.get("owner", {}).get("language", "es")

    async def job() -> None:
        try:
            text = describe(await warmup_run(rt, rt.clock()), language)
        except Exception as exc:
            log.warning("warmup failed: %s", exc)
            text = t(language, "warmup_failed")
        transports = getattr(rt, "transports", None)
        if transports is not None and transports.available(destination_platform(rt)):
            try:
                await transports.send(destination_platform(rt), rt.store.destination("configured:primary"), {"text": text, "parse_mode": None})
            except Exception as exc:
                log.debug("warmup result not sent: %s", exc)

    return _submit(rt, job(), name="radar:warmup")


def iso_now(rt: Any) -> str:
    return rt.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def command_text(rt: Any, raw_args: str, *, set_trust: Callable[[str], None] | None = None,
                 identity: dict[str, str] | None = None) -> str:
    language = rt.config.get("owner", {}).get("language", "es")
    args = (raw_args or "").split()
    sub = args[0].lower() if args else "status"
    store = rt.store
    if sub == "verify":
        try:
            return verify_from_session(rt, identity if identity is not None else session_identity())
        except Exception as exc:
            log.exception("radar verification failed")
            return t(language, "verify_failed") + f" ({type(exc).__name__})"
    if sub == "status":
        open_tasks = store.list_tasks(statuses=("open", "waiting", "needs_user", "in_progress", "snoozed"), limit=200)
        pending = store.list_proposals(statuses=("pending", "sent", "snoozed"), limit=200)
        waiting = store.list_proposals(statuses=("waiting",), limit=200)
        head = t(language, "status_header", tasks=len(open_tasks), proposals=len(pending), waiting=len(waiting), signals=store.pending_signal_count())
        extra = f"\n{t(language, 'trust_set', level=rt.config.get('trust', {}).get('level', 'balanced'))}"
        return head + extra + ("\n" + t(language, "paused") if rt.paused else "")
    if sub == "list":
        tasks = store.list_tasks(statuses=("open", "waiting", "needs_user", "in_progress", "snoozed"), limit=15, include_muted=False)
        if not tasks:
            return t(language, "no_tasks")
        lines = []
        for task in tasks:
            pending = [p for p in store.list_proposals(statuses=("pending", "sent", "snoozed"), task_id=task["id"], limit=10)
                       if p.get("batch_key") != "silent"]  # silent suggestions are never sent, so they are not "pending" for the owner
            waiting = store.list_proposals(statuses=("waiting", "executing"), task_id=task["id"], limit=5)
            marks = (f" ({len(pending)} ⏳)" if pending else "") + (f" ({len(waiting)} ⏸)" if waiting else "")
            lines.append(f"• [{task['kind']}] {task['title']} — {task['status']}{marks}\n  id: {task['id'][:8]}")
        return "\n".join(lines)
    if sub == "next":
        from .preview import deliver_next, format_sent

        try:
            result = deliver_next(store, rt.config, rt.clock())
        except Exception:
            log.exception("radar next delivery failed")
            raise
        return format_sent(result, rt.config, launched=launch_now(rt, result.get("outbox_id")))
    if sub == "warmup":
        from ..warmup import describe, last_run, request

        if len(args) > 1 and args[1].lower() == "status":
            return describe(last_run(store), language)
        if launch_warmup(rt):
            return t(language, "warmup_started", months=int((rt.config.get("warmup") or {}).get("months") or 12))
        request(store, iso_now(rt), by="command")
        return t(language, "warmup_requested")
    if sub == "prefs":
        from .infer import list_candidates
        from .learn import describe_preference, list_preferences_text

        text = list_preferences_text(store, language)
        candidates = list_candidates(store)
        if candidates:
            text += "\n\n" + t(language, "prefs_suggestions_header") + "\n" + "\n".join(
                f"• {p['id']}: {describe_preference(p, language)}" for p in candidates)
        return text
    if sub == "confirm" and len(args) > 1:
        from .infer import confirm_candidate, list_candidates
        from .learn import describe_preference

        prefix = args[1]
        target = next((p for p in list_candidates(store) if p["id"] == prefix or p["id"].startswith(prefix) or prefix in p["id"]), None)
        record = confirm_candidate(store, target["id"], now_dt=rt.clock()) if target else None
        if record is None:
            return t(language, "pref_no_candidate")
        refresh = getattr(rt, "refresh_preferences", None)
        if callable(refresh):
            refresh()
        return t(language, "pref_confirmed", id=record["id"], summary=describe_preference(record, language))
    if sub == "patterns":
        from ..patterns import describe as describe_pattern

        rows = store.list_patterns(limit=20)
        if not rows:
            return t(language, "patterns_empty")
        return "\n".join([t(language, "patterns_header")] + [f"• {describe_pattern(p, language)}" for p in rows])
    if sub == "forget" and len(args) > 1:
        from .learn import forget_preference_text

        return forget_preference_text(store, args[1], language, now_dt=rt.clock())
    if sub == "pause":
        rt.paused = True
        return t(language, "paused")
    if sub == "resume":
        rt.paused = False
        return t(language, "resumed")
    if sub == "mute" and len(args) > 1:
        prefix = args[1]
        for task in store.list_tasks(limit=500):
            if task["id"].startswith(prefix):
                return act_on_task(store, rt.config, task_id=task["id"], action="mute", actor="command", now_dt=rt.clock()).get("text", "")
        return t(language, "unknown_callback")
    if sub == "trust" and len(args) > 1:
        level = args[1].lower()
        if level not in ("cautious", "balanced", "autonomous"):
            return t(language, "help")
        if set_trust is not None:
            set_trust(level)
        rt.config.setdefault("trust", {})["level"] = level
        return t(language, "trust_set", level=level) + "\n" + level_summary(level, language)
    return t(language, "help")
