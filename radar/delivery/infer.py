"""Learn from repetition: when the owner silences several message families from the same sender, propose one
sender-wide rule as a *candidate* they confirm, instead of making them mute each family by hand.

A candidate never silences anything on its own: triage honours only ``active`` preferences (see
:func:`radar.preferences.applicable`), so a candidate only shows up as a suggestion in the panel and in
``/radar prefs`` until the owner confirms it. Confirming saves a new ``active`` version of the same
preference id; dismissing disables it (and we never propose it again)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..i18n import t
from ..preferences import new_preference, sender_address
from .learn import preference_id

log = logging.getLogger("radar.infer")

DEFAULT_THRESHOLD = 3


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _norm(sender: Any) -> str:
    """By email address, so «Name <addr>» and a bare «addr» count as the same sender."""
    return sender_address(sender)


def _silent_families(store: Any, sender: str) -> list[dict[str, Any]]:
    """Active silent rules that mute ONE family (sender + subject stem) of this sender."""
    sender = _norm(sender)
    out = []
    for pref in store.list_preferences(active_only=True):
        if pref.get("effect") != "silent" or (pref.get("dimension") or "notification") != "notification":
            continue
        scope = pref.get("scope") or {}
        if _norm(scope.get("sender")) == sender and scope.get("subject_stem"):
            out.append(pref)
    return out


def _sender_wide_seen(store: Any, sender: str) -> bool:
    """A sender-wide silent rule for this sender already exists in any state (active, candidate or dismissed):
    the owner has already been offered it, so we never propose it again."""
    sender = _norm(sender)
    for pref in store.list_preferences():
        if pref.get("effect") != "silent":
            continue
        scope = pref.get("scope") or {}
        if _norm(scope.get("sender")) == sender and not scope.get("subject_stem"):
            return True
    return False


def sender_candidate_id(sender: str) -> str:
    """The id a sender-wide silent rule for this sender gets, candidate or confirmed alike (same scope, same id),
    so confirming just saves a new version of it."""
    return preference_id({"dimension": "notification", "effect": "silent", "scope": {"sender": _norm(sender), "kind": "email"}})


def list_candidates(store: Any) -> list[dict[str, Any]]:
    return [p for p in store.list_preferences() if p.get("status") == "candidate"]


def maybe_infer_sender(store: Any, *, sender: str, now_dt: datetime, threshold: int = DEFAULT_THRESHOLD,
                       language: str = "es") -> dict[str, Any] | None:
    """When ``threshold`` families of one sender are already silenced, propose a single sender-wide candidate.
    Returns the new candidate, or None when there is nothing to propose. Never raises."""
    sender = _norm(sender)
    if not sender:
        return None
    try:
        families = _silent_families(store, sender)
        if len(families) < max(2, int(threshold or DEFAULT_THRESHOLD)):
            return None
        if _sender_wide_seen(store, sender):
            return None
        scope = {"sender": sender, "kind": "email"}
        pid = sender_candidate_id(sender)
        data = new_preference(
            pid, instruction=t(language, "pref_candidate_sender", sender=sender, n=len(families)),
            dimension="notification", scope=scope, effect="silent", origin="inferred", status="candidate",
        )
        now = _iso(now_dt)
        history = store.preference_history(pid)
        current = int(history[-1]["version"]) if history else 0
        version = store.save_preference(data, expected_version=current, now=now, feedback_ref="inferred")
    except (ValueError, KeyError, RuntimeError) as exc:
        log.info("sender rule not inferred for %s: %s", sender, exc)
        return None
    try:
        from ..models import canonical_hash

        store.add_audit(
            audit_id=canonical_hash({"candidate": pid, "v": version})[:32], event_type="preference",
            subject_type="preference", subject_id=pid, correlation_id=None, actor_kind="system",
            decision_code="preference_candidate", metadata={"sender": sender, "families": len(families)}, now=now,
        )
    except Exception as exc:  # audit is a courtesy
        log.debug("candidate audit skipped: %s", exc)
    return {**data, "version": version}


def after_silence(store: Any, config: dict[str, Any], *, sender: str, now_dt: datetime) -> dict[str, Any] | None:
    """Call after a silent rule is created: read the threshold and language from config and try to infer."""
    notifications = config.get("notifications", {}) if isinstance(config, dict) else {}
    threshold = notifications.get("mute_to_rule_threshold", DEFAULT_THRESHOLD)
    language = (config.get("owner", {}) if isinstance(config, dict) else {}).get("language", "es")
    return maybe_infer_sender(store, sender=sender, now_dt=now_dt, threshold=threshold, language=language)


def confirm_candidate(store: Any, preference_id_value: str, *, now_dt: datetime) -> dict[str, Any] | None:
    """Promote a candidate to an active rule (a new version of the same id). None when there is no such candidate."""
    history = store.preference_history(preference_id_value)
    if not history:
        return None
    latest = history[-1]
    if latest.get("status") != "candidate":
        return None
    data = new_preference(
        preference_id_value, instruction=str(latest.get("instruction") or "-"),
        dimension=str(latest.get("dimension") or "notification"), scope=dict(latest.get("scope") or {}),
        effect=str(latest.get("effect") or "silent"), exceptions=tuple(latest.get("exceptions") or ()),
        origin="explicit", status="active",  # the owner accepting the suggestion makes it an explicit rule of theirs
    )
    version = store.save_preference(data, expected_version=int(latest["version"]), now=_iso(now_dt), feedback_ref="confirmed")
    return {**data, "version": version}
