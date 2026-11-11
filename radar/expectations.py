"""What a scenario claims should happen, checked against what the run did.

A scenario may carry ``expect`` at its top level (judged on the end of the run: the tasks as they finally stand, every
message the owner would have received) and on any phase (judged on that moment). The checks are about decisions, not
wording, so that a run with the real model can be compared with the claim::

    expect:
      triage: kept                      # kept | ignored: how every signal of the run fared
      extract: {relevant: true}         # any extract of the run with these fields
      task: none                        # no task at all; or
      task: {kind: [trip], status: [open, waiting], due: true, title_any: [Lisboa]}
      steps:
        include: [{class: read}]        # some open step matches each one
        exclude: [{operation: gmail.*}] # no step, whatever its status, matches any of these
      messages:
        count: 0                        # or {max: 1}, {min: 1}
        kind: notice                    # notice | reminder: at least one of that kind
        any: [overbooking, cancel]      # some message says one of these (case-insensitive)
        none: [IBAN]                    # no message says any of these
        no_kind: notice                 # none of that kind

A field of a step or of a task takes one value or a list of accepted ones; ``*`` at the end matches a prefix. An
unknown key is an error at load time, so that a typo cannot pass as a claim that held.
"""
from __future__ import annotations

from typing import Any

OPEN_STEP = ("pending", "sent", "snoozed", "waiting", "accepted", "executing")
_TOP = {"triage", "extract", "task", "steps", "messages"}
_TASK = {"kind", "status", "due", "title_any"}
_STEPS = {"include", "exclude"}
_STEP = {"operation", "class", "batch", "status", "urgency", "title_any", "ask", "when"}
_MESSAGES = {"count", "kind", "no_kind", "any", "none"}
_EXTRACT = {"relevant", "category", "message_kind", "sender_kind", "reply_expected", "conversation_state", "disposition", "urgency", "risk_flags"}


def _keys(value: Any, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {sorted(unknown)}")
    return value


def validate(spec: Any, where: str = "expect") -> None:
    """Refuse a malformed claim before any model is spent on it."""
    spec = _keys(spec, _TOP, where)
    if not spec:
        raise ValueError(f"{where} is empty")
    if "triage" in spec and spec["triage"] not in ("kept", "ignored"):
        raise ValueError(f"{where}.triage must be kept or ignored")
    if "extract" in spec:
        _keys(spec["extract"], _EXTRACT, f"{where}.extract")
    if "task" in spec and spec["task"] != "none":
        _keys(spec["task"], _TASK, f"{where}.task")
    if "steps" in spec:
        steps = _keys(spec["steps"], _STEPS, f"{where}.steps")
        for group in steps:
            if not isinstance(steps[group], list) or not steps[group]:
                raise ValueError(f"{where}.steps.{group} must be a non-empty list")
            for item in steps[group]:
                _keys(item, _STEP, f"{where}.steps.{group}[]")
    if "messages" in spec:
        messages = _keys(spec["messages"], _MESSAGES, f"{where}.messages")
        count = messages.get("count", 0)
        if isinstance(count, dict):
            _keys(count, {"min", "max"}, f"{where}.messages.count")
        elif not isinstance(count, int):
            raise ValueError(f"{where}.messages.count must be a number or {{min, max}}")
        for key in ("kind", "no_kind"):
            if key in messages and messages[key] not in ("notice", "reminder"):
                raise ValueError(f"{where}.messages.{key} must be notice or reminder")


def validate_scenario(scenario: dict[str, Any]) -> None:
    if "expect" in scenario:
        validate(scenario["expect"])
    for index, phase in enumerate(scenario.get("phases") or [], 1):
        if isinstance(phase, dict) and "expect" in phase:
            validate(phase["expect"], f"phases[{index}].expect")


def _accepts(wanted: Any, value: Any) -> bool:
    options = wanted if isinstance(wanted, list) else [wanted]
    for option in options:
        if isinstance(option, str) and option.endswith("*"):
            if str(value or "").startswith(option[:-1]):
                return True
        elif option == value or (isinstance(value, (list, tuple)) and option in value):
            return True
        elif isinstance(option, bool) and bool(value) == option:
            return True
    return False


def _says(text: Any, words: Any) -> bool:
    haystack = str(text or "").lower()
    return any(str(word).lower() in haystack for word in (words if isinstance(words, list) else [words]))


def _step_matches(step: dict[str, Any], wanted: dict[str, Any]) -> bool:
    for key, value in wanted.items():
        if key == "title_any":
            if not _says(step.get("title"), value):
                return False
        elif key in ("ask", "when"):
            if bool(step.get(key)) != bool(value):
                return False
        elif not _accepts(value, step.get(key)):
            return False
    return True


def _task_matches(task: dict[str, Any], wanted: dict[str, Any]) -> bool:
    for key, value in wanted.items():
        if key == "due":
            if bool(task.get("due_at")) != bool(value):
                return False
        elif key == "title_any":
            if not _says(f"{task.get('title')} {task.get('summary')}", value):
                return False
        elif not _accepts(value, task.get(key)):
            return False
    return True


def _brief_tasks(tasks: list[dict[str, Any]]) -> list[str]:
    return [f"[{t.get('kind')}] {t.get('title')} ({t.get('status')})" for t in tasks]


def _brief_steps(steps: list[dict[str, Any]]) -> list[str]:
    return [f"{s.get('title')} [{s.get('operation') or s.get('class')} · {s.get('batch')} · {s.get('status')}]" for s in steps]


def check(spec: dict[str, Any], *, signals: list[dict[str, Any]], extracts: list[dict[str, Any]], tasks: list[dict[str, Any]],
          steps: list[dict[str, Any]], messages: list[dict[str, Any]], where: str) -> list[dict[str, Any]]:
    """One result per claim: ``{"where", "claim", "ok", "got"}``."""
    out: list[dict[str, Any]] = []

    def note(claim: str, ok: bool, got: Any) -> None:
        out.append({"where": where, "claim": claim, "ok": bool(ok), "got": got})

    if "triage" in spec:
        wanted = spec["triage"]
        verdicts = [str(s.get("triage") or "") for s in signals]
        note(f"triage: every signal {wanted}", bool(verdicts) and all(v.startswith(wanted) for v in verdicts), verdicts)
    if "extract" in spec:
        wanted = spec["extract"]
        note(f"extract: {wanted}", any(all(_accepts(v, e.get(k)) for k, v in wanted.items()) for e in extracts),
             [{k: e.get(k) for k in wanted} for e in extracts])
    if "task" in spec:
        wanted = spec["task"]
        if wanted == "none":
            note("no task", not tasks, _brief_tasks(tasks))
        else:
            note(f"task: {wanted}", any(_task_matches(t, wanted) for t in tasks), _brief_tasks(tasks))
    block = spec.get("steps") or {}
    open_steps = [s for s in steps if s.get("status") in OPEN_STEP or s.get("status") in ("succeeded",)]
    for wanted in block.get("include") or []:
        pool = steps if "status" in wanted else open_steps
        note(f"step present: {wanted}", any(_step_matches(s, wanted) for s in pool), _brief_steps(pool))
    for wanted in block.get("exclude") or []:
        hits = [s for s in steps if _step_matches(s, wanted)]
        note(f"step absent: {wanted}", not hits, _brief_steps(hits))
    if "messages" in spec:
        wanted = spec["messages"]
        kinds = ["notice" if m.get("kind") == "notice" else "reminder" for m in messages]
        texts = [str(m.get("text") or "") for m in messages]
        if "count" in wanted:
            count = wanted["count"]
            low, high = (count.get("min", 0), count.get("max")) if isinstance(count, dict) else (count, count)
            note(f"messages: count {count}", len(messages) >= low and (high is None or len(messages) <= high), texts)
        if "kind" in wanted:
            note(f"messages: a {wanted['kind']}", wanted["kind"] in kinds, kinds)
        if "no_kind" in wanted:
            note(f"messages: no {wanted['no_kind']}", wanted["no_kind"] not in kinds, kinds)
        if "any" in wanted:
            note(f"messages: some says one of {wanted['any']}", any(_says(t, wanted["any"]) for t in texts), texts)
        if "none" in wanted:
            note(f"messages: none says {wanted['none']}", not any(_says(t, wanted["none"]) for t in texts), texts)
    return out


def _phase_messages(phase: dict[str, Any]) -> list[dict[str, Any]]:
    """What the owner would have received by the end of this moment: meanwhile, at it, and after their replies."""
    return [*((phase.get("between") or {}).get("messages") or []), *(phase.get("would_send") or []), *((phase.get("after_replies") or {}).get("would_send") or [])]


def evaluate(scenario: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Every claim of the scenario against the report: ``{"claims": n, "failed": [...], "results": [...]}``."""
    results: list[dict[str, Any]] = []
    phases = report.get("phases") or []
    declared = scenario.get("phases") if isinstance(scenario.get("phases"), list) else []
    for index, (claimed, phase) in enumerate(zip(declared, phases), 1):
        if not isinstance(claimed, dict) or "expect" not in claimed:
            continue
        bundles = [*(phase.get("bundles") or []), *((phase.get("after_replies") or {}).get("bundles") or [])]
        results.extend(check(claimed["expect"], signals=phase.get("signals") or [], extracts=[b["extract"] for b in bundles if b.get("extract")],
                             tasks=[b["task"] for b in bundles if b.get("task")], steps=[p for b in bundles for p in b.get("proposals") or []],
                             messages=_phase_messages(phase), where=f"phase {index}"))
    if isinstance(scenario.get("expect"), dict):
        bundles = [b for p in phases for b in [*(p.get("bundles") or []), *((p.get("after_replies") or {}).get("bundles") or [])]]
        final = report.get("final") or {}
        results.extend(check(scenario["expect"], signals=[s for p in phases for s in p.get("signals") or []], extracts=[b["extract"] for b in bundles if b.get("extract")],
                             tasks=final.get("tasks") or [], steps=[p for t in final.get("steps") or [] for p in t], messages=[m for p in phases for m in _phase_messages(p)],
                             where="end of the run"))
    return {"claims": len(results), "failed": [r for r in results if not r["ok"]], "results": results}


def render(outcome: dict[str, Any]) -> list[str]:
    if not outcome.get("claims"):
        return ["", "Expectations: none declared"]
    failed = outcome.get("failed") or []
    lines = ["", f"Expectations: {outcome['claims'] - len(failed)}/{outcome['claims']} held"]
    for item in failed:
        lines.append(f"  FAIL ({item['where']}) {item['claim']}")
        lines.append(f"    got: {str(item['got'])[:400]}")
    return lines
