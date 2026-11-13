"""Reversibility boundary (ported from mail-intelligence, generalised).

``read`` never changes anything. ``local`` changes are reversible and invisible to third
parties. ``external`` changes are irreversible or reach other people; they always require the
owner's explicit, single-use consent. Unknown operations are external by default.
"""
from __future__ import annotations

from typing import Any

ORIGIN_TAG = "hermes-radar"

READ_OPERATIONS = frozenset({
    "gmail.read", "gmail.search", "calendar.read", "repo.read", "web.read", "http_status.check",
    "session.search", "investigate",
})
LOCAL_OPERATIONS = frozenset({
    "git.create_branch", "github.open_pr", "cron.create", "file.write_report",
    "task.update", "task.add_evidence", "calendar.draft",
})
EXTERNAL_OPERATIONS = frozenset({
    "gmail.create_draft",  # a draft staged in the owner's mailbox is one tool-call from being sent: always the owner's call
    "gmail.send_email", "gmail.reply", "gmail.reply_all", "gmail.send_reply", "gmail.archive", "gmail.mark",
    "gmail.filter", "gmail.delete", "gmail.modify", "github.merge_pr", "github.close_pr", "github.comment",
    "deploy.run", "db.write", "db.delete", "mailbox.write", "mailbox.delete", "payments.charge",
    "publish.post", "calendar.respond", "calendar.create", "booking.reserve", "web.act",
})


def classify(operation: str | None) -> str:
    key = str(operation or "")
    if key in READ_OPERATIONS:
        return "read"
    if key in LOCAL_OPERATIONS:
        return "local"
    return "external"


_RANK = {"read": 0, "local": 1, "external": 2}
# Words to the owner: a question, a consistency check, a piece of news. They run nothing, so their label is kept.
CONVERSATION = frozenset({"ask", "check", "inform"})
# An entry in the owner's own calendar is local as long as nobody is invited, and that is not known when the step is
# planned: the job firewall holds it (``_own_calendar_entry`` refuses any invitee in a ``local`` step), so the floor
# here is ``local`` and a planner that says ``external`` is still believed.
FLOOR = {"calendar.create": "local"}


def effective_class(operation: str | None, label: Any = None) -> str:
    """The class a step is held to: the stricter of what the planner said and what this module knows of the operation.

    The planner reads mail, pages and pull requests written by others, so its label may only raise the bar, never
    lower it: a ``github.comment`` it calls ``local`` is external (catalog review, 2026-11-12). A named operation this
    module does not know is external. A step with no operation keeps its label: it can only run as a job, where the
    firewall denies by default whatever the label does not grant."""
    said = str(label or "read").lower()
    if said not in _RANK:
        said = "read"
    key = str(operation or "")
    if not key or key in CONVERSATION:
        return said
    known = FLOOR.get(key) or classify(key)
    return known if _RANK[known] > _RANK[said] else said


def proposal_class(proposal: dict[str, Any]) -> str:
    """:func:`effective_class` of a planned item (``operation``) or a stored proposal (``operation_id``), so rows
    stored before the rule are judged by it too."""
    return effective_class(proposal.get("operation_id") or proposal.get("operation"), proposal.get("action_class"))


def is_reversible(operation: str | None) -> bool:
    return classify(operation) in {"read", "local"}


def requires_approval(operation: str | None) -> bool:
    return classify(operation) == "external"


def build_origin_mark(task_id: str, evidence_refs: list[str] | None, language: str = "es") -> str:
    refs = ", ".join(str(r) for r in (evidence_refs or [])[:10])
    if language.startswith("es"):
        return (
            "\n\n---\n"
            f"Origen automático: {ORIGIN_TAG}, tarea {task_id}. "
            f"Evidencia: {refs or 'sin evidencia citada'}. "
            "Autoría del sistema, sin auto-merge; requiere revisión humana."
        )
    return (
        "\n\n---\n"
        f"Automatic origin: {ORIGIN_TAG}, task {task_id}. "
        f"Evidence: {refs or 'no evidence cited'}. "
        "System authorship, no auto-merge; human review required."
    )


def ensure_origin_mark(body: str, task_id: str, evidence_refs: list[str] | None, language: str = "es") -> str:
    text = str(body or "")
    if ORIGIN_TAG in text and str(task_id) in text:
        return text
    return text + build_origin_mark(task_id, evidence_refs, language)


def validate_no_automerge(operation: str | None, params: Any) -> None:
    data = params if isinstance(params, dict) else {}
    if str(operation or "") == "github.open_pr":
        for key in ("auto_merge", "automerge", "autoMerge", "merge"):
            value = data.get(key)
            if value is True or (isinstance(value, str) and value.strip().lower() in ("true", "yes", "1")):
                raise ValueError("auto-merge is forbidden: PRs never merge themselves")
    if str(operation or "") in ("github.merge_pr", "github.close_pr"):
        raise ValueError(f"{operation} is external and never runs proactively")
