"""Host LLM access with daily budgets, audit and schema-bound answers.

Every call goes through ``ctx.llm`` (``acomplete_structured``/``complete_structured``) and a
plugin-owned auxiliary task slot (``auxiliary.radar_<slot>`` in config), so the operator can
pin a cheap model per step without granting model overrides.

When the main model fails for any reason, the call is retried with the host's own ``fallback_providers``, in
order, and the main model is benched for a few minutes so the next calls go straight to the fallback. The host's
auxiliary ladder only fails over on a closed list of reasons (auth, payment, rate limit, timeout, connection) and
the Anthropic spend limit answers as a plain HTTP 400, which is on no list: on 2026-11-04/05 radar sat without a
model for thirty hours while the owner's chat and radar's own jobs, which run through the main agent loop, kept
answering via the fallback. Using the fallback is an override the owner grants to the plugin through Hermes: the
manifest declares the capabilities ``llm.provider_override`` and ``llm.model_override`` and ``hermes plugins
install`` / ``enable`` asks for them. Radar never writes that grant itself; without it the main error surfaces as
before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from .identity import PLUGIN_ID

log = logging.getLogger(__name__)

# After a main-model failure that a fallback rescued, the main model is skipped for this long.
FALLBACK_BENCH_MINUTES = 10
OVERRIDE_FLAGS = ("allow_provider_override", "allow_model_override")
OVERRIDE_CAPABILITIES = ("llm.provider_override", "llm.model_override")  # declared in plugin.yaml, granted through Hermes
GRANT_COMMAND = f"hermes plugins enable {PLUGIN_ID}"  # asks for the declared capabilities

AUX_PREFIX = "radar_"
SLOTS: dict[str, tuple[str, str]] = {
    "extract": ("Radar: extract", "Structured extraction and triage of one bundle of signals (email, chat turns, events)."),
    "plan": ("Radar: plan", "Task correlation, checklist gaps and proposals for one task."),
    "intent": ("Radar: intent", "Classify a free-text owner reply to a notice."),
    "preference": ("Radar: preferences", "Turn one owner reply into a scoped attention preference."),
    "pattern": ("Radar: patterns", "Name recurring patterns and propose automations."),
    "card": ("Radar: cards", "Fill presentation templates for the desktop panel."),
    "remind": ("Radar: reminders", "Write the owner's reminder: one pending thing, in prose, as an assistant would say it in passing."),
    "consolidate": ("Radar: consolidate", "Confirm whether a cluster of open tasks is one real subject and should be merged, and title the merge."),
    "connector": ("Radar: connector", "Work out, from a command-line mail tool's help, how its commands map onto Radar's mail operations."),
}
# Seconds the host may wait for each slot (``auxiliary.radar_<slot>.timeout``; the owner can override it). The host's
# default is 30 s, meant for interactive helpers: a plan over a 12k-token bundle ran past it on 2026-11-03 and the
# host then marked the provider unhealthy for ten minutes, leaving radar without a model. These calls run in the
# background, so they get the time they need.
SLOT_TIMEOUTS: dict[str, int] = {
    "extract": 150, "plan": 240, "intent": 45, "preference": 45, "pattern": 120, "card": 90, "remind": 90, "consolidate": 120, "connector": 120,
}


class LlmError(RuntimeError):
    """The model answer could not be trusted or the call failed; callers retry later. ``transient`` marks a failure
    of the provider or the host (a spend limit, a rate limit, a timeout, no connection) rather than of the answer:
    waiting for the provider to come back is nobody's fault, so such a failure costs the signal no attempt. On
    2026-11-04 the Anthropic spend limit answered every call for seven hours and three signals, among them the
    owner's own reply to a question, were written off as ``too_many_attempts``."""

    def __init__(self, message: str = "", *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


# Exception class names and message fragments that mean the provider or the host, not the request, failed.
_TRANSIENT_NAMES = (
    "APIError", "APIStatusError", "BadRequest", "RateLimit", "Connection", "Timeout", "Overloaded", "ServiceUnavailable",
    "InternalServer", "Authentication", "PermissionDenied", "Unavailable", "OSError", "HTTPError", "RemoteProtocol",
)
_TRANSIENT_WORDS = ("usage limit", "rate limit", "overloaded", "unavailable", "timed out", "timeout", "connection", "quota", "billing", "credit")


def transient_failure(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    return any(part in name for part in _TRANSIENT_NAMES) or any(word in text for word in _TRANSIENT_WORDS)


class BudgetExhausted(LlmError):
    """The daily LLM budget for a slot is spent; callers defer, never skip silently."""


class OverrideDenied(LlmError):
    """The host refused the provider/model override the fallback needs (the ``llm.*_override`` capabilities)."""


def host_fallbacks() -> list[dict[str, str]]:
    """The host's ``fallback_providers`` as ``[{"provider", "model"}, ...]``; entries without both are skipped."""
    try:
        from hermes_cli.config import load_config_readonly  # type: ignore[import-not-found]

        raw = (load_config_readonly() or {}).get("fallback_providers")
    except Exception:  # not running inside Hermes, or config unreadable
        return []
    return fallback_entries(raw)


def override_granted(host_config: Any) -> bool:
    """Whether the owner granted the plugin the two override capabilities. Read-only: the grant is Hermes' to record
    (``hermes plugins enable``), under ``plugins.entries.<id>``: the granted capability list, or the flags the host
    mirrors them into."""
    node = host_config if isinstance(host_config, dict) else {}
    for key in ("plugins", "entries", PLUGIN_ID):
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        return False
    granted = node.get("granted_capabilities")
    if isinstance(granted, list) and all(capability in granted for capability in OVERRIDE_CAPABILITIES):
        return True
    flags = node.get("llm")
    return isinstance(flags, dict) and all(bool(flags.get(flag)) for flag in OVERRIDE_FLAGS)


def fallback_entries(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if provider and model:
            out.append({"provider": provider, "model": model})
    return out


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def register_aux_tasks(ctx: Any) -> list[str]:
    registered: list[str] = []
    register = getattr(ctx, "register_auxiliary_task", None)
    if not callable(register):
        return registered
    for slot, (name, description) in SLOTS.items():
        key = AUX_PREFIX + slot
        try:
            register(key, display_name=name, description=description, defaults={"timeout": SLOT_TIMEOUTS.get(slot, 90)})
            registered.append(key)
        except Exception as exc:  # already registered on reload, or host without aux tasks
            log.debug("auxiliary task %s not registered: %s", key, exc)
    return registered


class LlmGate:
    def __init__(
        self, ctx: Any, store: Any, config: dict[str, Any], *, clock: Callable[[], datetime] = utc_now,
        fallbacks: Callable[[], list[dict[str, str]]] = host_fallbacks,
    ):
        self.ctx = ctx
        self.store = store
        self.config = config
        self.clock = clock
        self.fallbacks = fallbacks
        self.benched_until: datetime | None = None  # the main model is skipped until then
        self.override_denied_until: datetime | None = None  # the host refused the override; ask again after a while

    @property
    def available(self) -> bool:
        return getattr(self.ctx, "llm", None) is not None

    async def structured(
        self, slot: str, *, instructions: str, input_text: str, schema: dict[str, Any], purpose: str,
        temperature: float = 0.0, max_tokens: int | None = None, subject_id: str | None = None,
    ) -> dict[str, Any]:
        if slot not in SLOTS:
            raise ValueError(f"unknown llm slot {slot}")
        llm = getattr(self.ctx, "llm", None)
        if llm is None:
            raise LlmError("host does not expose ctx.llm")
        now_dt = self.clock()
        now = iso(now_dt)
        day = now_dt.date().isoformat()
        limit = int(self.config.get("limits", {}).get("llm_calls_per_day", 200))
        if not self.store.reserve_budget(
            bucket_start=day, resource_key=slot, units=1, limit=limit, now=now, bucket="llm", unit="call",
        ):
            self.store.add_audit(
                audit_id=str(uuid4()), event_type="budget", subject_type="llm", subject_id=subject_id,
                correlation_id=None, actor_kind="scheduler", decision_code="budget_exhausted",
                metadata={"slot": slot, "purpose": purpose}, now=now,
            )
            raise BudgetExhausted(slot)
        called = False
        kwargs: dict[str, Any] = {
            "instructions": instructions,
            "input": [{"type": "text", "text": input_text}],
            "json_schema": schema,
            "purpose": purpose,
            "temperature": temperature,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        started = time.monotonic()
        try:
            result, route = await self._call(llm, slot, kwargs)
            called = True
            parsed = getattr(result, "parsed", None)
            if parsed is None:
                text = getattr(result, "text", "") or ""
                try:
                    parsed = json.loads(text)
                except ValueError as exc:
                    raise LlmError("model returned no structured answer") from exc
            if not isinstance(parsed, dict):
                raise LlmError("structured answer is not an object")
            self._audit(result, slot=slot, purpose=purpose, subject_id=subject_id, now=iso(self.clock()), route=route,
                        elapsed_ms=int((time.monotonic() - started) * 1000))
            return parsed
        finally:
            self.store.settle_budget(
                bucket_start=day, resource_key=slot, units=1, consumed=called, now=iso(self.clock()), bucket="llm",
            )

    async def _call(self, llm: Any, slot: str, kwargs: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        """The main model first, then the host's fallbacks in order. Returns the host result and the route taken
        (``{}`` for the main model, ``{"fallback": True, "provider", "model"}`` otherwise)."""
        task = AUX_PREFIX + slot
        now = self.clock()
        denied = self.override_denied_until is not None and now < self.override_denied_until
        fallbacks = [] if denied else self.fallbacks()
        benched = bool(fallbacks) and self.benched_until is not None and now < self.benched_until
        if benched:
            main_error: LlmError = LlmError("main model benched after a recent failure", transient=True)
        else:
            try:
                return await self._invoke(llm, kwargs, task=task), {}
            except LlmError as exc:
                main_error = exc
        fallback_errors: list[str] = []
        for entry in fallbacks:
            try:
                result = await self._invoke(llm, kwargs, task=task, provider=entry["provider"], model=entry["model"])
            except OverrideDenied as exc:
                # The grant is read by the host on every call, so once the owner adds it the next ask succeeds.
                self.override_denied_until = now + timedelta(minutes=FALLBACK_BENCH_MINUTES)
                log.warning("radar cannot use the host fallback %s via %s: %s. Grant %s with `%s`",
                            entry["model"], entry["provider"], exc, " and ".join(OVERRIDE_CAPABILITIES), GRANT_COMMAND)
                break
            except LlmError as exc:
                log.debug("fallback %s via %s failed too: %s", entry["model"], entry["provider"], exc)
                fallback_errors.append(f"{entry['model']} via {entry['provider']}: {str(exc)[:160]}")
                continue
            if not benched:  # the bench starts at a real failure and is never extended by using the fallback
                log.warning("main model failed (%s); using %s via %s for the next %d min",
                            main_error, entry["model"], entry["provider"], FALLBACK_BENCH_MINUTES)
                self.benched_until = now + timedelta(minutes=FALLBACK_BENCH_MINUTES)
            return result, {"fallback": True, "provider": entry["provider"], "model": entry["model"]}
        if fallback_errors:
            # Say what the fallback itself said: «main model benched» alone hid a fallback timeout on 2026-11-07.
            raise LlmError(f"{main_error}; fallback failed: " + " | ".join(fallback_errors), transient=getattr(main_error, "transient", False))
        raise main_error

    async def _invoke(self, llm: Any, kwargs: dict[str, Any], *, task: str, provider: str = "", model: str = "") -> Any:
        """One host call; ``task`` is dropped for fakes and older hosts that refuse it. With ``provider``/``model``
        a trust refusal is the override being denied, not the task."""
        acomplete = getattr(llm, "acomplete_structured", None)
        for attempt_task in (task, None):
            call_kwargs = dict(kwargs)
            if attempt_task:
                call_kwargs["task"] = attempt_task
            if provider:
                call_kwargs["provider"] = provider
            if model:
                call_kwargs["model"] = model
            try:
                if callable(acomplete):
                    return await acomplete(**call_kwargs)
                return await asyncio.to_thread(llm.complete_structured, **call_kwargs)
            except TypeError as exc:
                # Fakes or older hosts without ``task``: fall through to a plain call.
                if attempt_task and "task" in str(exc):
                    continue
                raise LlmError(f"llm call failed: {exc}") from exc
            except Exception as exc:
                name = type(exc).__name__
                refused = "Trust" in name or "task" in str(exc).lower()
                if refused and (provider or model) and "override" in str(exc).lower():
                    raise OverrideDenied(str(exc)[:200]) from exc
                if attempt_task and refused:
                    log.debug("aux task %s refused (%s); retrying with the main model", attempt_task, exc)
                    continue
                raise LlmError(f"llm call failed: {name}: {str(exc)[:200]}", transient=transient_failure(exc)) from exc
        raise LlmError("llm call failed")

    def _audit(self, result: Any, *, slot: str, purpose: str, subject_id: str | None, now: str,
               route: dict[str, Any] | None = None, elapsed_ms: int = 0) -> None:
        usage = getattr(result, "usage", None)
        metadata = {
            "slot": slot,
            "purpose": purpose,
            "model": str(getattr(result, "model", "") or ""),
            "provider": str(getattr(result, "provider", "") or ""),
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "elapsed_ms": int(elapsed_ms),
        }
        if route and route.get("fallback"):
            metadata["fallback"] = {"provider": route.get("provider", ""), "model": route.get("model", "")}
        cost = getattr(usage, "cost_usd", None)
        if isinstance(cost, (int, float)):
            metadata["cost_usd"] = float(cost)
        try:
            self.store.add_audit(
                audit_id=str(uuid4()), event_type="llm_call", subject_type="llm", subject_id=subject_id,
                correlation_id=None, actor_kind="scheduler", decision_code="completed", metadata=metadata, now=now,
            )
        except Exception as exc:  # audit must never break the pipeline
            log.debug("llm audit skipped: %s", exc)
