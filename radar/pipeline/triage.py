"""Rule-based triage of one signal: keep for extraction or ignore with a reason."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..preferences import applicable, subject_stem
from . import rules


@dataclass(frozen=True)
class TriageDecision:
    keep: bool
    reason: str
    priority: str = "normal"


def _observation(signal: dict[str, Any]) -> dict[str, Any]:
    meta = signal.get("meta") or {}
    return {
        "account": meta.get("account", ""),
        "sender": meta.get("sender", ""),
        "message_type": signal.get("kind", ""),
        "project": meta.get("project", ""),
        "kind": signal.get("kind", ""),
        "source_kind": meta.get("source_kind", signal.get("kind", "")),
        "repo": meta.get("repo", ""),
        "subject_stem": subject_stem(meta.get("subject") or signal.get("title") or ""),
    }


def triage_signal(signal: dict[str, Any], preferences: list[dict[str, Any]], config: dict[str, Any] | None = None, *, bound: bool = False) -> TriageDecision:
    """``bound``: the signal's conversation already has an open task (a session the task was born in), so even a
    short turn of it is worth judging: it may be the owner settling that task."""
    kind = signal.get("kind", "")
    meta = signal.get("meta") or {}
    filters = (config or {}).get("triage") or {}  # generic noise filters, each on unless the owner turned it off
    on = lambda name: filters.get(name, True)
    for pref in applicable(preferences, _observation(signal)):
        if pref.get("effect") == "silent" and not pref.get("exceptions"):
            return TriageDecision(False, f"preference:{pref.get('id')}")
    if kind == "email":
        subject = signal.get("title", "")
        text = f"{subject}\n{signal.get('snippet', '')}"
        automated = bool(set(meta.get("automation") or []) & rules.STRONG_AUTOMATION)
        if automated and rules.is_account_change(text):
            return TriageDecision(True, "email", "normal")  # a sensitive change to an account is told, whatever the noise filters would say
        if not meta.get("thread_reply"):
            if on("enotification") and rules.is_enotification(text):
                return TriageDecision(False, "e_notification")  # an official e-notification waiting in a sede: the owner collects it himself
            if on("access_notice") and rules.is_access_notice(text, automated=automated):
                return TriageDecision(False, "access_notification")  # a routine "you signed in" / portal-access notice: nothing to do
        transactional = rules.is_transactional(text)
        signals = set(meta.get("automation") or [])
        if on("noise_subject") and rules.subject_is_noise(subject) and not transactional:
            return TriageDecision(False, "noise_subject")
        if on("bulk_no_signal") and "list-unsubscribe" in signals and not transactional:
            return TriageDecision(False, "bulk_no_signal")
        if on("automated_no_signal") and signals & rules.STRONG_AUTOMATION and not transactional and not meta.get("thread_reply"):
            return TriageDecision(False, "automated_no_signal")
        return TriageDecision(True, "email", "normal" if transactional else "low")
    if kind == "session_turn":
        user_text = str(meta.get("user_text") or signal.get("snippet") or "")
        if user_text.lstrip().startswith("/"):
            return TriageDecision(False, "command")
        assistant_text = str(meta.get("assistant_text") or "")
        if len(user_text.strip()) < rules.MIN_TURN_CHARS and not bound and len(assistant_text.strip()) < rules.SUBSTANTIAL_REPLY_CHARS:
            return TriageDecision(False, "short_turn")
        if meta.get("radar_origin"):
            return TriageDecision(False, "own_prompt")
        return TriageDecision(True, "session_turn", "normal" if rules.is_tasky_turn(user_text) else "low")
    if kind == "github":
        actor = str(meta.get("actor") or "")
        if on("bot_actor") and actor.endswith("[bot]") and meta.get("event_type") not in {"workflow_run", "deployment_status"}:
            return TriageDecision(False, "bot_actor")
        return TriageDecision(True, "github", "normal" if meta.get("event_type") in {"pull_request", "issues", "workflow_run", "deployment_status", "pull_request_review"} else "low")
    if kind in {"calendar", "job_result", "cron_result"}:
        return TriageDecision(True, kind)
    return TriageDecision(True, "default", "low")


def triage_bundle(signals: list[dict[str, Any]], preferences: list[dict[str, Any]], config: dict[str, Any] | None = None, *, bound: bool = False) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], TriageDecision]]]:
    """Split a bundle into signals worth a model call and ignored ones (with reasons)."""
    kept: list[dict[str, Any]] = []
    ignored: list[tuple[dict[str, Any], TriageDecision]] = []
    for signal in signals:
        decision = triage_signal(signal, preferences, config, bound=bound)
        if decision.keep:
            kept.append(signal)
        else:
            ignored.append((signal, decision))
    return kept, ignored
