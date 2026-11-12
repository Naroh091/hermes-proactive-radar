"""What the chat agent is told when the owner's reply to a notice is passed on to it.

A reply that is a plain answer to what the notice asked (yes, no, one of its options, done) is settled by the reader
in :mod:`radar.delivery.feedback`, at once and inside its guarantees. Anything else is the owner talking: «remind me on
December 10», «cancel it but tell Marta first», «what does this one cost?». The agent reads that, with the notice in
front of it, and acts through the radar tools; no new kind of reply needs a new field in a classifier (2026-11-11:
«remind me on December 10» was read as «later» and answered «tomorrow at 08:00»).

The brief travels as the first lines of the message the agent receives (``pre_gateway_dispatch`` rewrite) and is taken
off again before the turn is recorded as the owner's words.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .feedback import decisions_in

OPEN, CLOSE = "[radar reply]", "[/radar reply]"
NOTICE_CHARS = 700
_TAGS = re.compile(r"<[^>]+>")


def agent_brief(store: Any, config: dict[str, Any], outbox_row: dict[str, Any], text: str, now_local: datetime) -> str:
    """The owner's words preceded by what they answer: the task, the notice, its open decisions, the local time."""
    payload = outbox_row.get("payload") or {}
    task_id = str(outbox_row.get("task_id") or payload.get("task_id") or "")
    lines = [OPEN, "The owner is replying to a notice Radar sent them. Do what they ask with the radar tools, then answer "
                   "them in one or two sentences, in their language, saying what you did."]
    if task_id:
        try:
            task = store.get_task(task_id)
            lines.append(f"Task {task_id} ({task.get('status')}): {str(task.get('title') or '')[:160]}")
        except KeyError:
            pass
    notice = " ".join(_TAGS.sub("", str(payload.get("text") or "")).split())[:NOTICE_CHARS]
    if notice:
        lines.append(f"The notice said: {notice}")
    for item in decisions_in(store, payload):
        if not item.get("open"):
            continue
        answers = ", ".join(f"{a['id']} ({a['label']})" for a in item.get("answers") or [])
        lines.append(f"Open decision {item['index']}: {item['title']} [proposal {item['id']}; answers: {answers}]")
    lines.append(f"Owner's local time: {now_local.strftime('%Y-%m-%dT%H:%M %A')} ({(config.get('owner') or {}).get('timezone') or 'UTC'}).")
    lines.append("radar_task acts on the whole task (later, with `until` when they name a moment; wait; done; mute; reopen). "
                 "radar_resolve records their decision on one step, except approving a step that is external, sends mail or "
                 "always asks first: that approval is theirs alone, by the notice's button or the panel, and the tool refuses it "
                 "and sends them the notice again.")
    lines.append(CLOSE)
    return "\n".join(lines) + "\n\n" + str(text or "")


def strip_brief(text: Any) -> str:
    """The owner's own words, without the brief Radar put in front of them."""
    value = str(text or "")
    if not value.lstrip().startswith(OPEN):
        return value
    _, found, rest = value.partition(CLOSE)
    return rest.strip() if found else value
