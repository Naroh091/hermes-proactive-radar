"""The mark that tells a job's session apart from the owner's.

Since the host of 2026-10-22 a delegated ``hermes chat --oneshot`` is a thin client: the session runs inside the
gateway and its hooks fire in the gateway's plugin host, whose environment is the gateway's own. The variables the
runner hands the child (``HERMES_RADAR_JOB`` and friends) never reach the hooks, so from 2026-10-26 to 2026-11-03
Radar read its own jobs' conversations as the owner's (thirty to fifty signals a day, twin tasks born from a job's
transcript) and the tool firewall stood inert. The job is therefore marked where the hooks can see it: the first line
of its prompt names the job, its class and the tools a consented external step may use. ``pre_llm_call`` binds the
session id to that mark; the capture and firewall hooks look the session up, and the environment is only a fallback.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

MARK_RE = re.compile(
    r"\[radar job (?P<job>[A-Za-z0-9_-]{4,64}) class=(?P<cls>read|local|external)(?: allow=(?P<allow>[A-Za-z0-9_,.-]+))?\]"
)
HEAD = 400  # the mark is the prompt's first line; a message is searched this far only
# The first words of every delegated job's prompt (see ``radar.jobs.prompts``). Job sessions recorded before the mark
# existed (2026-10-26 to 2026-11-03, 281 signals) carry this opener as the owner's "message": anything that mines or
# reads the owner's own activity recognises them by it.
JOB_PROMPT_OPENER = "You are carrying out one step of a long-lived task for your owner"
_BOUND: "OrderedDict[str, dict[str, str]]" = OrderedDict()
_NOT_A_JOB: "OrderedDict[str, None]" = OrderedDict()
_CACHE = 500


def mark_line(job_id: str, action_class: Any, allow: str = "") -> str:
    """``[radar job <id> class=<read|local|external> allow=a,b]``: the prompt's first line."""
    cls = str(action_class or "read").lower()
    if cls not in ("read", "local", "external"):
        cls = "read"
    tools = ",".join(sorted({part.strip().lower() for part in str(allow or "").split(",") if part.strip()}))
    line = f"[radar job {job_id} class={cls}"
    if tools and cls == "external":
        line += f" allow={tools}"
    return line + "]"


def parse_mark(text: Any) -> dict[str, str] | None:
    found = MARK_RE.search(str(text or "")[:HEAD])
    if not found:
        return None
    return {"job_id": found.group("job"), "action_class": found.group("cls"), "allow": found.group("allow") or ""}


def is_job_prompt(text: Any) -> bool:
    """True for the text of a job's own prompt: the mark, or the opener of prompts older than the mark."""
    head = str(text or "")[:HEAD].lstrip()
    return parse_mark(head) is not None or head.startswith(JOB_PROMPT_OPENER)


def _remember(cache: "OrderedDict[str, Any]", key: str, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE:
        cache.popitem(last=False)


def bind(session_id: Any, mark: dict[str, str]) -> None:
    sid = str(session_id or "")
    if sid:
        _NOT_A_JOB.pop(sid, None)
        _remember(_BOUND, sid, dict(mark))


def bound(session_id: Any) -> dict[str, str] | None:
    """The mark bound to this session in this process, without touching the store."""
    return _BOUND.get(str(session_id or ""))


def forget_all() -> None:
    _BOUND.clear()
    _NOT_A_JOB.clear()


def job_session(store: Any, session_id: Any, *, user_message: Any = None) -> dict[str, str] | None:
    """The job behind a session, or None for the owner's own: the binding this process made, the mark carried by the
    message itself, or the job the store recorded for the session (a plugin host restarted under a running job)."""
    sid = str(session_id or "")
    found = _BOUND.get(sid) if sid else None
    if found is not None:
        return found
    found = parse_mark(user_message)
    if found is not None:
        bind(sid, found)
        return found
    if not sid or sid in _NOT_A_JOB or store is None:
        return None
    try:
        job = store.job_for_session(sid)
    except Exception:
        job = None
    if not job:
        _remember(_NOT_A_JOB, sid, None)
        return None
    allow = ""
    if job.get("action_class") == "external" and job.get("proposal_id"):
        from .firewall import allowed_tools_for

        try:
            allow = allowed_tools_for(store.get_proposal(job["proposal_id"]).get("operation_id"))
        except Exception:
            allow = ""
    found = {"job_id": str(job.get("id") or ""), "action_class": str(job.get("action_class") or "read"), "allow": allow}
    bind(sid, found)
    return found
