"""The reminder: what an assistant would say in passing about one pending thing.

Until 2026-10-23 the summary was an inventory: nine tasks, a paragraph and a mail header each, steps numbered
across tasks, eleven rows of buttons, 3,800 characters cut mid-tag. The owner: "this is completely unworkable; the
point is that a message arrives the way a human assistant would mention something: by the way, you still have X
pending". So a reminder carries one thing (two at most), written by the model as a colleague would
mention it, with the exact question at the end; the rest waits its turn. When the model is unavailable, a template
says the same thing more plainly.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from ..i18n import language_name, t
from ..pipeline.schemas import REMIND_SCHEMA
from .notice import fit
from ..prompts import REMIND_PROMPT

log = logging.getLogger(__name__)

MAX_CHARS = 500  # what the model is asked for
HARD_CAP = 700  # a longer answer is cut at a word boundary
LIST_LINE = re.compile(r"(?:^|\n)\s*(?:[-•*]|\d+[.)])\s+")

def _labels(proposal: dict[str, Any]) -> list[str]:
    return [str(o.get("label")) for o in proposal.get("options") or [] if isinstance(o, dict) and o.get("label")][:3]


def _news_text(proposal: dict[str, Any]) -> str:
    params = proposal.get("params") or {}
    return str(params.get("message") or proposal.get("title") or "")


def _facts(task: dict[str, Any]) -> dict[str, Any]:
    """The human-recognisable context of a task, so the reminder can place it: who and what it involves,
    the dates, amounts and references. Internal or sensitive fields (ids, codes, drafts, sessions) stay out."""
    ent = task.get("entities") or {}

    def _strs(key: str, limit: int) -> list[str]:
        return [fit(v, 80) for v in (ent.get(key) or []) if isinstance(v, str) and v.strip()][:limit]

    dates = [f"{fit(d.get('label'), 60)}: {d['when']}" for d in (ent.get("dates") or [])
             if isinstance(d, dict) and d.get("when")][:3]
    amounts: list[str] = []
    for a in ent.get("amounts") or []:
        if isinstance(a, dict) and a.get("value") is not None:
            amounts.append(" ".join(x for x in (fit(a.get("label"), 60), str(a.get("value")), str(a.get("currency") or "")) if x))
    facts: dict[str, Any] = {}
    for key, value in (("people", _strs("people", 4)), ("organizations", _strs("organizations", 3)),
                       ("items", _strs("items", 3)), ("places", _strs("places", 2)),
                       ("dates", dates), ("amounts", amounts[:3])):
        if value:
            facts[key] = value
    for key in ("repo", "pr", "url"):
        value = ent.get(key)
        if isinstance(value, str) and value.strip():
            facts[key] = fit(value, 120)
    return facts


_WEEKDAYS = {  # cron order: 0 = Sunday
    "es": ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"],
    "en": ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
}


def _weekday(index: Any, language: str) -> str:
    names = _WEEKDAYS.get(str(language or "es")[:2].lower(), _WEEKDAYS["en"])
    try:
        return names[int(index) % 7]
    except (TypeError, ValueError):
        return ""


def describe_schedule(cron: Any, language: str) -> str:
    """``0 11 * * 1`` → «los lunes a las 11:00»; anything else keeps the cron expression."""
    parts = str(cron or "").split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2] == "*" and parts[3] == "*":
        moment = f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        if parts[4] == "*":
            return t(language, "schedule_daily", time=moment)
        if parts[4].isdigit():
            return t(language, "schedule_weekly", weekday=_weekday(parts[4], language), time=moment)
    return t(language, "schedule_cron", cron=str(cron or ""))


def pattern_facts(task: dict[str, Any], language: str) -> dict[str, Any] | None:
    """What Radar saw and what it proposes, in words the owner can picture: a routine task's reminder explains the
    pattern instead of naming it. On 2026-11-04 the template said "Pattern: X: Automate: X" and the owner, rightly,
    could not tell what it meant."""
    entities = task.get("entities") or {}
    pattern = entities.get("pattern") if isinstance(entities.get("pattern"), dict) else {}
    if not pattern and not entities.get("schedule_cron"):
        return None
    out: dict[str, Any] = {"times": pattern.get("times"), "weeks": pattern.get("weeks"), "since": pattern.get("since"), "until": pattern.get("until")}
    when = ""
    if pattern.get("kind") == "periodic" and pattern.get("weekday") is not None:
        when = t(language, "pattern_when_periodic", weekday=_weekday((int(pattern["weekday"]) + 1) % 7, language), hour=int(pattern.get("hour") or 0))
    elif pattern.get("kind") == "sequence":
        when = t(language, "pattern_when_sequence", first=fit(pattern.get("first"), 60), then=fit(pattern.get("then"), 80), lag=pattern.get("lag_hours") or "?")
    if when:
        out["when"] = when
    key = "pattern_seen_periodic" if pattern.get("kind") == "periodic" else "pattern_seen_sequence"
    out["seen"] = t(language, key, times=pattern.get("times") or "?", since=pattern.get("since") or "?", when=when) if pattern else ""
    out["schedule"] = describe_schedule(entities.get("schedule_cron"), language)
    out["job"] = fit(entities.get("prompt"), 240)
    return out


def _item(entry: dict[str, Any], language: str = "es", mode: str = "reminder") -> dict[str, Any]:
    task = entry.get("task") or {}
    decision = entry.get("decision")
    news = entry.get("news") or []
    out: dict[str, Any] = {
        "task": fit(task.get("title"), 120), "kind": task.get("kind") or "other", "summary": fit(task.get("summary"), 300),
        "due_at": str(task.get("due_at") or ""),
    }
    facts = _facts(task)
    if facts:
        out["facts"] = facts
    if task.get("kind") == "routine":
        pattern = pattern_facts(task, language)
        if pattern:
            out["pattern"] = pattern
    if decision:
        params = decision.get("params") or {}
        operation = str(decision.get("operation_id") or "")
        in_words = bool(params.get("ask"))  # a decision with no buttons: the owner answers in their own words
        out["pending"] = {
            "what": fit(decision.get("title"), 160),
            "kind": "open_question" if in_words else "question" if operation in ("ask", "check") else "approval",
            "class": str(decision.get("action_class") or "read"), "operation": operation,
            "why": fit(decision.get("reason_text") or params.get("rationale"), 300),
            "context": fit(params.get("message"), 400), "answers": [] if in_words else _labels(decision),
        }
        timed = str(params.get("when") or params.get("deliver_after") or "")
        if timed:
            out["pending"]["when"] = timed  # the moment the plan timed the step for: at it, the step is possible now
    settled = [s for s in entry.get("settled") or [] if isinstance(s, dict) and s.get("question")]
    if settled:
        out["settled"] = [{"question": fit(s.get("question"), 160), "answer": fit(s.get("answer"), 80)} for s in settled[:4]]
    if news and decision and mode != "interrupt":
        out["done"] = [fit(_news_text(p), 300) for p in news[:2]]  # behind a decision, news is what was done meanwhile
    elif news:
        # News on its own (in a reminder) or what just happened (in an interruption): information, not a request.
        out["news"] = [fit(_news_text(p), 300) for p in news[:2]]
        if not decision:
            out["nothing_pending"] = True
    return out


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def without_trailing_question(text: str) -> str:
    """The text without the question(s) it ends with. A message that carries no decision has nothing to ask: on
    2026-11-06 the model closed the news of a cancelled membership with "shall I write to try to keep him?", a step
    no plan had proposed; the owner's answer then landed on nothing ("that proposal is no longer active")."""
    sentences = [s for s in _SENTENCE_END.split(tidy(text)) if s]
    while sentences and sentences[-1].rstrip().endswith("?"):
        sentences.pop()
    return " ".join(sentences).strip()


def one_question(text: str) -> str:
    """The text with a single question, the last one: a message that carries one decision asks one thing. On
    2026-11-10 a reminder asked whether to reactivate one account and whether to rotate another's password over
    buttons that only answered the second."""
    sentences = [s for s in _SENTENCE_END.split(tidy(text)) if s]
    questions = [i for i, s in enumerate(sentences) if s.rstrip().endswith("?")]
    if len(questions) < 2:
        return tidy(text)
    return " ".join(s for i, s in enumerate(sentences) if i not in questions[:-1]).strip()


def without_reply_word(text: str, language: str) -> str:
    """The text without the sentences that, after its question, tell the owner to answer with the acknowledgement
    word or a thumbs-up. A decision made in words has no such button: on 9-10 November 2026 five messages closed a
    choice between two things with «reply 👍 OK»."""
    word = re.sub(r"[^\w ]", "", t(language, "btn_ack")).strip().lower()
    sentences = [s for s in _SENTENCE_END.split(tidy(text)) if s]
    last = max((i for i, s in enumerate(sentences) if s.rstrip().endswith("?")), default=None)
    if last is None:
        return tidy(text)

    def names_it(sentence: str) -> bool:
        return "👍" in sentence or bool(word and re.search(rf"\b{re.escape(word)}\b", sentence.lower()))

    return " ".join(s for i, s in enumerate(sentences) if i <= last or not names_it(s)).strip()


def fallback_text(items: list[dict[str, Any]], language: str) -> str:
    """The template reminder, for when the model is unavailable: the same thing, said plainly."""
    parts: list[str] = []
    for entry in items[:2]:
        task = entry.get("task") or {}
        decision = entry.get("decision")
        pattern = pattern_facts(task, language) if decision and task.get("kind") == "routine" else None
        if pattern:
            parts.append(t(language, "remind_fallback_pattern", seen=pattern.get("seen") or fit(task.get("title"), 90),
                           schedule=pattern.get("schedule") or "", what=fit(task.get("summary"), 240) or fit(task.get("title"), 90)))
        elif decision:
            parts.append(t(language, "remind_fallback", title=fit(task.get("title"), 90), question=fit(decision.get("title"), 160)))
        elif entry.get("news"):
            parts.append(t(language, "remind_fallback_news", text=fit(_news_text(entry["news"][0]), 240)))
    return " ".join(part for part in parts if part)


def tidy(text: Any) -> str:
    return " ".join(str(text or "").replace("\r", "").split("\n")).strip()


async def compose(gate: Any, *, items: list[dict[str, Any]], language: str, backlog: int = 0, first_of_day: bool = False,
                  subject_id: str | None = None, mode: str = "reminder", template: bool = True, now: str = "") -> str:
    """One message in prose for ``items`` (each ``{"task", "decision", "news"}``); the template when the model cannot,
    or "" when ``template`` is False (an interruption then keeps its classic layout). ``mode`` is "reminder" for
    something pending said in passing, "interrupt" for something that just happened and cannot wait. ``now`` is the
    moment the message goes out (ISO; the clock when omitted): a step timed for a moment is written at that moment as
    something possible now, not as something to schedule (on 2026-11-13 a check-in step composed at its hour still
    said the check-in "opens on the 13th")."""
    fallback = fallback_text(items, language) if template else ""
    if gate is None or not getattr(gate, "available", False) or not items:
        return fallback
    payload = {"language": language_name(language), "backlog": int(backlog), "first_of_day": bool(first_of_day), "mode": mode,
               "now": now or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
               "items": [_item(e, language, mode) for e in items[:2]]}
    try:
        raw = await gate.structured(
            "remind", instructions=REMIND_PROMPT.format(language=language_name(language), limit=MAX_CHARS),
            input_text=json.dumps(payload, ensure_ascii=False, sort_keys=True), schema=REMIND_SCHEMA, purpose="radar.remind",
            subject_id=subject_id,
        )
    except Exception as exc:  # budget, host, schema: the template is always there
        log.info("reminder written from the template: %s", exc)
        return fallback
    text = raw.get("text") if isinstance(raw, dict) else ""
    if LIST_LINE.search(str(text or "")):
        return fallback  # a list is what this message must never be
    text = tidy(text)
    decisions = [e["decision"] for e in items[:2] if e.get("decision")]
    if not decisions:
        text = without_trailing_question(text)  # nothing to decide: the message tells and stops, whatever the model wrote
    elif len(decisions) == 1:
        text = one_question(text)  # one decision, one question: its buttons answer nothing else
        if (decisions[0].get("params") or {}).get("ask"):
            text = without_reply_word(text, language)
    if not text:
        return fallback
    return fit(text, HARD_CAP)
