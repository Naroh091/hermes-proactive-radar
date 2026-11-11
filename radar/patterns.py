"""Patterns: recurring activity mined from the signal log, named by the model, automated with the host cron.

Two shapes. ``periodic``: the same key (who did what) lands on the same weekday and hour band often
enough (support ≥ 3 across ≥ 3 weeks, ≥ 60 % concentration). ``sequence``: B tends to follow A within a
few days far more often than B happens anyway (lift ≥ 2). Candidates become a ``routine`` task with one
proposal, "Automate", that the owner always decides (``cron.create`` never runs unattended); accepting
creates a scheduled Hermes job that only prepares or reports, and its outputs come back as signals.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .i18n import language_name, t
from .jobs.mark import is_job_prompt
from .llm import LlmError
from .models import canonical_hash
from .pipeline.schemas import PATTERN_SCHEMA
from .prompts import PATTERN_PROMPT

log = logging.getLogger("radar.patterns")

WINDOW_WEEKS = 10
MIN_SUPPORT = 3
MIN_WEEKS = 3
CONCENTRATION = 0.6
LAG_DAYS = 7
MIN_LIFT = 2.0
MAX_CANDIDATES = 10
HOUR_BAND = 3
PROPOSAL_DAYS = 7
# A turn whose "user message" the host itself injected (a background process report) is not the owner acting.
HOST_NOTICE_PREFIXES = ("[IMPORTANT:",)
_STOP = {"the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "de", "la", "el", "los", "las", "un", "una", "y", "o", "que", "con", "por", "para", "me", "mi", "te", "lo", "al", "del", "es", "en"}
_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|rv|aw|wg|tr)\s*:\s*)+", re.I)

def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _address(sender: Any) -> str:
    match = re.search(r"<([^>]+)>", str(sender or ""))
    return (match.group(1) if match else str(sender or "")).strip().lower()


def _words(text: Any, limit: int) -> str:
    out = []
    for word in re.findall(r"[\wáéíóúñü#-]+", str(text or "").lower()):
        if word in _STOP or len(word) < 2:
            continue
        out.append(word)
        if len(out) >= limit:
            break
    return " ".join(out)


def event_key(signal: dict[str, Any]) -> tuple[str, str, str] | None:
    """(kind, actor, action) of a signal, or None when it is not worth mining."""
    kind = signal.get("kind")
    meta = signal.get("meta") or {}
    if kind == "github":
        action = "merged" if meta.get("merged") else str(meta.get("action") or "")
        return ("github", str(meta.get("actor") or "?"), f"{meta.get('event_type')}:{action}@{meta.get('repo')}")
    if kind == "email":
        subject = _SUBJECT_PREFIX.sub("", str(signal.get("title") or ""))
        return ("email", _address(meta.get("sender")) or "?", _words(re.sub(r"\d+", "#", subject), 6))
    if kind == "session_turn":
        raw = str(meta.get("user_text") or signal.get("snippet") or "")
        if is_job_prompt(raw) or raw.lstrip().startswith(HOST_NOTICE_PREFIXES):
            # Radar's own jobs at work (recorded as the owner's sessions until 2026-11-03) and the host's notices: on
            # 2026-11-04 every "pattern" found was «after a job prompt, a mail from X follows», support 260.
            return None
        text = _words(raw, 4)
        return ("session", "owner", text) if text else None
    if kind in ("job_result", "cron_result"):
        title = re.sub(r"^\[[a-z_]+\]\s*", "", str(signal.get("title") or ""))
        return ("hermes", "radar", _words(title, 6)) if title else None
    return None


def _moment(signal: dict[str, Any], zone: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(signal.get("occurred_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(zone)


def mine(signals: list[dict[str, Any]], *, now_dt: datetime, tz: str = "UTC", window_weeks: int = WINDOW_WEEKS,
         min_support: int = MIN_SUPPORT, concentration: float = CONCENTRATION, lag_days: int = LAG_DAYS, min_lift: float = MIN_LIFT) -> list[dict[str, Any]]:
    """Periodic and sequence candidates, strongest first."""
    try:
        zone = ZoneInfo(tz or "UTC")
    except Exception:
        zone = timezone.utc
    groups: dict[tuple[str, str, str], list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
    for signal in signals:
        key = event_key(signal)
        moment = _moment(signal, zone)
        if key and moment is not None:
            groups[key].append((moment, signal))
    for items in groups.values():
        items.sort(key=lambda pair: pair[0])
    candidates: list[dict[str, Any]] = []
    for key, items in groups.items():
        if len(items) < min_support or len({m.isocalendar()[:2] for m, _ in items}) < MIN_WEEKS:
            continue
        bands = Counter((m.weekday(), m.hour // HOUR_BAND) for m, _ in items)
        (weekday, band), top = bands.most_common(1)[0]
        share = top / len(items)
        if share < concentration:
            continue
        hours = [m.hour for m, _ in items if (m.weekday(), m.hour // HOUR_BAND) == (weekday, band)]
        hour = int(statistics.median(hours))
        candidates.append({
            "kind": "periodic", "key": list(key), "support": len(items), "concentration": round(share, 2), "weekday": weekday, "hour": hour,
            "schedule_cron": f"0 {hour} * * {(weekday + 1) % 7}", "fingerprint": canonical_hash(["periodic", *key]),
            "first_at": iso(items[0][0]), "last_at": iso(items[-1][0]), "weeks": len({m.isocalendar()[:2] for m, _ in items}),
            "examples": [{"at": iso(m), "title": str(s.get("title") or "")[:120]} for m, s in items[-5:]],
        })
    keys = list(groups)
    window_days = max(1, window_weeks * 7)
    for a in keys:
        occ_a = [m for m, _ in groups[a]]
        if len(occ_a) < min_support:
            continue
        for b in keys:
            if a == b:
                continue
            occ_b = [m for m, _ in groups[b]]
            follows, lags = 0, []
            for ta in occ_a:
                tb = next((x for x in occ_b if ta < x <= ta + timedelta(days=lag_days)), None)
                if tb is not None:
                    follows += 1
                    lags.append((tb - ta).total_seconds() / 3600)
            if follows < min_support:
                continue
            probability = follows / len(occ_a)
            base = min(1.0, len(occ_b) * lag_days / window_days)
            lift = probability / base if base else float("inf")
            if lift < min_lift or probability < 0.5:
                continue
            candidates.append({
                "kind": "sequence", "key": list(a), "then": list(b), "support": follows, "lift": round(min(lift, 99.0), 2), "probability": round(probability, 2),
                "median_lag_hours": round(statistics.median(lags), 1), "fingerprint": canonical_hash(["sequence", *a, *b]),
                "first_at": iso(occ_a[0]), "last_at": iso(occ_a[-1]), "weeks": len({m.isocalendar()[:2] for m in occ_a}),
                "examples": [{"at": iso(m), "title": str(s.get("title") or "")[:120]} for m, s in groups[a][-3:]],
            })
    candidates.sort(key=lambda c: (-c["support"], c["fingerprint"]))
    return candidates[:MAX_CANDIDATES]


def validate_pattern_answer(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    items = raw.get("patterns") if isinstance(raw, dict) else None
    out = []
    for item in items or []:
        if not isinstance(item, dict) or not isinstance(item.get("fingerprint"), str):
            continue
        out.append({
            "fingerprint": item["fingerprint"], "name": " ".join(str(item.get("name") or "").split())[:60],
            "description": " ".join(str(item.get("description") or "").split())[:300], "worth_automating": bool(item.get("worth_automating")),
            "schedule_cron": " ".join(str(item.get("schedule_cron") or "").split()), "prompt": str(item.get("prompt") or "").strip()[:4000],
            "rationale": " ".join(str(item.get("rationale") or "").split())[:300], "confidence": float(item.get("confidence") or 0),
        })
    return out


def _valid_cron(expression: str) -> bool:
    parts = expression.split()
    return len(parts) == 5 and all(re.fullmatch(r"[\d*,/-]+", p) for p in parts)


async def run_mining(rt: Any, now_dt: datetime) -> dict[str, Any]:
    """Daily routine: mine, remember candidates, ask the model about the new ones, propose automations."""
    store, config, gate = rt.store, rt.config, rt.gate
    owner = config.get("owner", {})
    now = iso(now_dt)
    since = iso(now_dt - timedelta(weeks=WINDOW_WEEKS))
    signals = store.signals_in_window(since, now, kinds=("email", "github", "session_turn", "job_result", "cron_result"))
    candidates = mine(signals, now_dt=now_dt, tz=str(owner.get("timezone") or "UTC"))
    summary = {"signals": len(signals), "candidates": len(candidates), "new": 0, "proposed": 0}
    fresh: list[dict[str, Any]] = []
    for candidate in candidates:
        existing = store.pattern_by_fingerprint(candidate["fingerprint"])
        record = store.upsert_pattern(
            pattern_id=existing["id"] if existing else str(uuid4()), kind=candidate["kind"], fingerprint=candidate["fingerprint"],
            evidence=candidate, support=int(candidate["support"]), confidence=float(candidate.get("concentration") or min(1.0, candidate.get("probability", 0))),
            now=now, last_seen_at=candidate["examples"][-1]["at"] if candidate.get("examples") else now,
        )
        if record.get("status") == "candidate":
            fresh.append({**candidate, "id": record["id"]})
    summary["new"] = len(fresh)
    if not fresh or not getattr(gate, "available", False):
        return summary
    language = str(owner.get("language") or "es")
    try:
        raw = await gate.structured(
            "pattern", instructions=PATTERN_PROMPT.format(language=language_name(language), timezone=owner.get("timezone", "UTC")),
            input_text=json.dumps({"now": now, "candidates": [{k: v for k, v in c.items() if k != "id"} for c in fresh]}, ensure_ascii=False, sort_keys=True),
            schema=PATTERN_SCHEMA, purpose="radar.pattern", subject_id="patterns",
        )
        answers = {a["fingerprint"]: a for a in validate_pattern_answer(raw)}
    except (LlmError, ValueError) as exc:
        log.info("pattern naming skipped: %s", exc)
        return summary
    for candidate in fresh:
        answer = answers.get(candidate["fingerprint"])
        if not answer:
            continue
        if not answer["worth_automating"] or not _valid_cron(answer["schedule_cron"]) or not answer["prompt"]:
            store.set_pattern_status(candidate["id"], "dismissed", now, name=answer["name"], description=answer["description"])
            continue
        task_id = str(uuid4())
        store.create_task(
            task_id=task_id, kind="routine", title=f"{t(language, 'pattern_prefix')}: {answer['name']}", summary=answer["description"],
            entities={"pattern_id": candidate["id"], "pattern_kind": candidate["kind"], "schedule_cron": answer["schedule_cron"], "prompt": answer["prompt"],
                      "evidence": candidate.get("examples") or [], "pattern": pattern_summary(candidate)},
            expectations=[], checklist=[], now=now,
        )
        store.create_task_proposal(
            proposal_id=str(uuid4()), task_id=task_id, title=f"{t(language, 'pattern_automate')}: {answer['name']}", reason_text=answer["rationale"],
            action_class="local", operation_id="cron.create",
            params={"message": answer["description"], "rationale": answer["rationale"],
                    "native": {"schedule": answer["schedule_cron"], "prompt": answer["prompt"], "name": answer["name"][:80]}},
            options=[{"id": "automate", "label": t(language, "btn_automate")}, {"id": "detail", "label": t(language, "btn_detail")},
                     {"id": "no_automate", "label": t(language, "btn_no_automate")}],
            urgency="low", expires_at=iso(now_dt + timedelta(days=PROPOSAL_DAYS)), fingerprint=f"pattern:{candidate['fingerprint']}", now=now, batch_key="digest",
        )
        store.set_pattern_status(candidate["id"], "proposed", now, task_id=task_id, name=answer["name"], description=answer["description"])
        summary["proposed"] += 1
    return summary


def pattern_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    """What the owner needs to picture the pattern: how many times, since when, and when or after what. Kept on the
    routine task so the reminder can explain what Radar saw instead of naming a «pattern» they never heard of."""
    out: dict[str, Any] = {
        "kind": str(candidate.get("kind") or ""), "times": int(candidate.get("support") or 0), "weeks": int(candidate.get("weeks") or 0),
        "since": str(candidate.get("first_at") or "")[:10], "until": str(candidate.get("last_at") or "")[:10],
    }
    if out["kind"] == "periodic":
        out["weekday"] = candidate.get("weekday")
        out["hour"] = candidate.get("hour")
    else:
        out["first"] = " ".join(str(part) for part in (candidate.get("key") or [])[2:3])
        out["then"] = " ".join(str(part) for part in (candidate.get("then") or [])[1:3])
        out["lag_hours"] = candidate.get("median_lag_hours")
    return out


def pattern_of_task(store: Any, task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    try:
        entities = store.get_task(task_id).get("entities") or {}
    except KeyError:
        return None
    pattern_id = entities.get("pattern_id")
    if not pattern_id:
        return None
    try:
        return store.get_pattern(str(pattern_id))
    except KeyError:
        return None


def dismiss(store: Any, task_id: str | None, now: str) -> bool:
    pattern = pattern_of_task(store, task_id)
    if pattern is None:
        return False
    store.set_pattern_status(pattern["id"], "dismissed", now)
    try:
        store.set_task_status(task_id, "done", now=now, reason="pattern_dismissed")
        store.cancel_task_followups(task_id, now)
    except (KeyError, ValueError, RuntimeError):
        pass
    return True


def mark_automated(store: Any, task_id: str | None, automation_ref: str, now: str) -> bool:
    pattern = pattern_of_task(store, task_id)
    if pattern is None:
        return False
    store.set_pattern_status(pattern["id"], "automated", now, automation_ref=automation_ref)
    try:
        store.set_task_status(task_id, "done", now=now, reason="pattern_automated")
        store.cancel_task_followups(task_id, now)
    except (KeyError, ValueError, RuntimeError):
        pass
    return True


def describe(pattern: dict[str, Any], language: str) -> str:
    evidence = pattern.get("evidence") or {}
    key = " ".join(str(k) for k in evidence.get("key") or [])
    if pattern.get("kind") == "sequence":
        then = " ".join(str(k) for k in evidence.get("then") or [])
        shape = f"{key} → {then} (x{pattern.get('support')}, lift {evidence.get('lift')})"
    else:
        shape = f"{key} ({evidence.get('schedule_cron', '')}, x{pattern.get('support')})"
    return f"{pattern.get('name') or shape} · {t(language, 'pattern_status_' + str(pattern.get('status')))} · {shape}"
