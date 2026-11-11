"""Recalled context for the planner: the host's memory provider (whichever it is) and past chat sessions.

Provider-agnostic on purpose. Hermes exposes every memory provider through the same ``MemoryProvider``
contract (``get_tool_schemas()`` + ``handle_tool_call()``), so the provider's own search tool is used:
``hindsight_recall``, ``mnemosyne_recall``, a ``*_query`` tool with a ``question`` argument... When a
provider exposes no tools, ``queue_prefetch``/``prefetch`` is the fallback. Past sessions come from the
host's ``session_search`` tool, and so do precedents: the owner's own earlier turns about the same sender or
organisation, with the exchange around each (what the agent reported, what the owner asked for), so the
planner sees what the owner has had done with such things before. Everything recalled is data for the
model, never instructions, and every lookup is bounded by a timeout so a slow backend cannot stall the
pipeline.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("radar.recall")

MAX_QUERY = 200
MAX_QUERY_WORDS = 16  # a full-text search of many words finds nothing; the title, the names and the sender are enough
_FTS_SPECIALS = re.compile(r'[:"()*^{}]')  # characters the host's full-text search reads as syntax
TOOL_PRIORITY = ("recall", "search", "retrieve", "query", "find")
# Tools that read something else than the owner's memories, or that write.
_SIDE_TOOLS = ("shared", "canonical", "graph", "triple", "scratchpad", "retain", "remember", "forget", "write",
               "store", "curate", "status", "stats", "sleep", "reflect", "delete")
_NO_RESULTS = re.compile(r"^\s*(no (relevant )?(memories|results|matches|memory)( found)?\.?|nothing (found|relevant)\.?)\s*$", re.I)


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# -- memory provider ---------------------------------------------------------------------------
def pick_tool(schemas: Any, override: str = "") -> tuple[str, str] | None:
    """``(tool name, query parameter)`` of the provider's search tool, or None when it has none."""
    tools: list[tuple[str, str, list[str]]] = []
    for schema in schemas or []:
        if not isinstance(schema, dict):
            continue
        name = str(schema.get("name") or "")
        params = schema.get("parameters") if isinstance(schema.get("parameters"), dict) else {}
        props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
        required = [r for r in (params.get("required") or []) if isinstance(r, str)]
        if not name or not props:
            continue
        param = ""
        if isinstance(props.get("query"), dict):
            param = "query"
        else:
            strings = [k for k in required if isinstance(props.get(k), dict) and props[k].get("type") == "string"]
            if len(strings) == 1:
                param = strings[0]
        if param:
            tools.append((name, param, required))
    if override:
        return next(((name, param) for name, param, _ in tools if name == override), None)

    def rank(entry: tuple[str, str, list[str]]) -> tuple[int, int, int, int]:
        name, param, required = entry
        lowered = name.lower()
        priority = next((i for i, key in enumerate(TOOL_PRIORITY) if key in lowered), len(TOOL_PRIORITY))
        side = sum(1 for word in _SIDE_TOOLS if word in lowered)
        return (priority, side, len([r for r in required if r != param]), len(name))

    ranked = sorted((t for t in tools if any(key in t[0].lower() for key in TOOL_PRIORITY)), key=rank)
    return (ranked[0][0], ranked[0][1]) if ranked else None


def parse_tool_result(raw: Any) -> str:
    """Text of a provider tool answer: ``{"result": ...}`` envelopes, JSON lists or plain text; errors → ''."""
    value: Any = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
    if isinstance(value, dict):
        if value.get("error"):
            return ""
        inner = next((value[key] for key in ("result", "results", "memories", "content", "text") if key in value), value)
        value = inner
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value or "")
    text = text.strip()
    return "" if not text or _NO_RESULTS.match(text) else text


def load_host_provider(session_id: str) -> Any:
    """The host's active memory provider, initialised for read-only recall; None when there is none."""
    from plugins.memory import load_memory_provider  # type: ignore[import-not-found]  # host package

    name = None
    try:
        from hermes_cli.config import load_config as host_config  # type: ignore[import-not-found]

        memory = (host_config() or {}).get("memory") or {}
        name = memory.get("provider") if isinstance(memory, dict) else None
    except Exception as exc:  # the host config is unreadable: no provider, not an error of ours
        log.debug("memory provider name not resolved: %s", exc)
    if not isinstance(name, str) or not name.strip():
        return None
    provider = load_memory_provider(name.strip(), register_skills=False)
    if provider is None:
        return None
    available = getattr(provider, "is_available", None)
    if callable(available) and not available():
        log.info("memory provider %s is not available: %s", name, getattr(provider, "unavailable_reason", lambda: "")())
        return None
    try:
        from hermes_constants import get_hermes_home  # type: ignore[import-not-found]

        home = str(get_hermes_home())
    except Exception:
        home = str(Path.home() / ".hermes")
    provider.initialize(session_id, hermes_home=home, platform="radar", agent_context="subagent")
    return provider


class MemoryRecall:
    """Lazy, provider-agnostic handle on the host's memory provider."""

    def __init__(self, config: dict[str, Any], *, provider: Any = None, loader: Callable[[str], Any] | None = None,
                 session_id: str = "radar-recall", scope: Callable[[], Any] | None = None):
        self.config = config
        self.scope = scope or contextlib.nullcontext  # the host's profile secret scope (multiplexed gateways)
        self.provider = provider
        self._loader = loader or load_host_provider
        self._loaded = provider is not None
        self._tool: tuple[str, str] | None = None
        self._tool_resolved = False
        self.session_id = session_id
        self.last_error = ""
        self.calls = 0

    def _settings(self) -> dict[str, Any]:
        return self.config.get("context") or {}

    def load(self) -> Any:
        if not self._loaded:
            self._loaded = True
            try:
                self.provider = self._loader(self.session_id)
            except Exception as exc:  # a missing host package or a failing backend: recall is optional
                self.last_error = str(exc)[:200]
                log.info("memory provider not loaded: %s", exc)
                self.provider = None
        return self.provider

    def tool(self) -> tuple[str, str] | None:
        if not self._tool_resolved:
            self._tool_resolved = True
            provider = self.load()
            if provider is not None:
                try:
                    schemas = provider.get_tool_schemas()
                except Exception:
                    schemas = []
                self._tool = pick_tool(schemas, str(self._settings().get("memory_tool") or ""))
        return self._tool

    def recall_sync(self, query: str) -> str:
        provider = self.load()
        if provider is None:
            return ""
        self.calls += 1
        tool = self.tool()
        if tool is not None:
            name, param = tool
            return parse_tool_result(provider.handle_tool_call(name, {param: query}))
        queue = getattr(provider, "queue_prefetch", None)
        if callable(queue):
            queue(query, session_id=self.session_id)
        return parse_tool_result(provider.prefetch(query, session_id=self.session_id))

    async def recall(self, query: str, *, timeout: float) -> str:
        if not query:
            return ""
        try:
            with self.scope():  # to_thread copies the context, so the worker sees the profile's secrets
                return await asyncio.wait_for(asyncio.to_thread(self.recall_sync, query), timeout=timeout)
        except asyncio.TimeoutError:
            self.last_error = "timeout"
            log.info("memory recall timed out after %.0fs", timeout)
            return ""
        except Exception as exc:
            self.last_error = str(exc)[:200]
            log.info("memory recall failed: %s", exc)
            return ""

    async def warm(self) -> None:
        """Load the provider and resolve its search tool in a worker thread (startup, off the pipeline)."""
        try:
            with self.scope():
                await asyncio.to_thread(self.tool)
        except Exception as exc:
            self.last_error = str(exc)[:200]

    def close(self) -> None:
        provider, self.provider, self._loaded, self._tool_resolved = self.provider, None, False, False
        if provider is not None:
            try:
                provider.shutdown()
            except Exception:
                pass


# -- past sessions -----------------------------------------------------------------------------
def host_session_tool(ctx: Any) -> Callable[..., Any] | None:
    """``fn(**args) -> raw payload`` on the host's session_search tool in every shape it has (discovery, with or
    without a role filter; a window around one message), dispatched through the plugin context like any other
    tool call; None when the context cannot dispatch."""
    dispatch = getattr(ctx, "dispatch_tool", None)
    if callable(dispatch):
        return lambda **args: dispatch("session_search", dict(args))
    return None


def host_session_search(ctx: Any) -> Callable[..., Any] | None:
    """``fn(query, limit) -> raw payload``: the discovery shape of :func:`host_session_tool`."""
    tool = host_session_tool(ctx)
    return (lambda query, limit: tool(query=query, limit=limit)) if tool is not None else None


def parse_sessions(raw: Any, *, limit: int = 3, per_item: int = 400) -> list[dict[str, str]]:
    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if payload.get("success") is False:
        return []
    found: list[dict[str, str]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "").strip()
        snippet = _clip(item.get("snippet") or item.get("preview") or item.get("excerpt"), per_item)
        if not session_id or not snippet:
            continue
        found.append({"session_id": session_id, "when": str(item.get("when") or "")[:32], "title": _clip(item.get("title"), 120), "snippet": snippet})
        if len(found) >= limit:
            break
    return found


# -- precedents: what the owner had done before with the same sender or organisation -----------
# Sessions that are not the owner's own chat with their agent: Radar's jobs, cron runs, delegated workers.
AUTOMATION_SOURCES = frozenset({"oneshot", "cron", "kanban", "subagent", "tool", "webhook", "radar"})
# A "user" turn the host wrote, not the owner: compaction handoffs, cron deliveries, restated objectives, system notes.
_HOST_TURN = re.compile(r"^\[(context (compaction|summary)|still in progress|prior context|system:|important:|cron|radar job|current user objective)", re.I)
_GENERIC_DOMAINS = frozenset({"gmail", "googlemail", "outlook", "hotmail", "live", "yahoo", "icloud", "me", "proton", "protonmail", "gmx"})
_MARKS = re.compile(r">>>|<<<")  # the host's match marks in a discovery snippet


def sender_names(sender: str) -> list[str]:
    """The names a mail sender goes by in the owner's chats: a short display name ("Acme Cloud <receipts@acme.example>"
    → Acme Cloud) and the name of its domain (acme). A department-style display name («Departamento de Facturación |
    acmehosting») is skipped: nobody says it in a chat, and on 2026-11-08 it was the one query, found nothing, and
    the owner's earlier dealings with that registrar never reached the planner. Generic mailbox providers add no name."""
    sender = str(sender or "").strip()
    name, _, address = sender.rpartition("<") if "<" in sender else ("", "", sender)
    name = " ".join(name.strip(' "\'').split())
    names: list[str] = []
    if name and "@" not in name and len(name.split()) <= 3 and not any(sep in name for sep in "|/:·-"):
        names.append(name)
    domain = address.strip(" >").rpartition("@")[2].lower()
    label = domain.split(".")[0] if domain else ""
    if label and label not in _GENERIC_DOMAINS and label.lower() not in {n.lower() for n in names}:
        names.append(label)
    return names


def precedent_names(signals: list[dict[str, Any]], existing_task: dict[str, Any] | None, *, limit: int = 2) -> list[str]:
    """Whom to ask the owner's history about: the senders of the bundle's mail (never the owner's own), then the
    organisations of the task under review; distinct, in that order, at most ``limit``."""
    names: list[str] = []

    def add(value: Any) -> None:
        name = " ".join(str(value or "").split())
        if len(name) >= 3 and name.lower() not in {n.lower() for n in names}:
            names.append(name)

    for signal in signals:
        meta = signal.get("meta") or {}
        if signal.get("kind") == "email" and not meta.get("owner_sent"):
            for name in sender_names(meta.get("sender", "")):
                add(name)
    entities = (existing_task or {}).get("entities")
    for value in (entities.get("organizations") or []) if isinstance(entities, dict) else []:
        add(value)
    return names[:limit]


def _envelope(raw: Any) -> dict[str, Any]:
    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    return payload if isinstance(payload, dict) and payload.get("success") is not False else {}


def parse_precedents(raw: Any, *, limit: int = 3, per_item: int = 240) -> list[dict[str, str]]:
    """The owner's own turns the discovery shape matched: sessions of their interactive chats only (never a job, a
    cron run or a worker), ``[{session_id, when, title, owner_said, message_id}]``, match marks removed."""
    found: list[dict[str, str]] = []
    for item in _envelope(raw).get("results") or []:
        if not isinstance(item, dict):
            continue
        session_id = str(item.get("session_id") or "").strip()
        source = str(item.get("source") or "").lower()
        said = _clip(_MARKS.sub("", str(item.get("snippet") or "")), per_item)
        if (not session_id or source in AUTOMATION_SOURCES or str(item.get("matched_role") or "user") != "user" or not said
                or _HOST_TURN.match(said.lstrip(". "))):
            continue
        title = str(item.get("title") or "")
        found.append({"session_id": session_id, "when": str(item.get("when") or "")[:32], "title": _clip(title, 120) if title != "None" else "",
                      "owner_said": said, "message_id": str(item.get("match_message_id") or "")})
        if len(found) >= limit:
            break
    return found


def parse_window(raw: Any, *, anchor_id: str, per_message: int = 280) -> tuple[str, list[dict[str, str]]]:
    """``(the owner's whole turn, the exchange)`` from the scroll shape around that turn: the agent's report the owner
    was answering, their turn, and what the agent said next; tool output and empty turns are skipped. ``("", [])``
    when the anchor is the host speaking (a compaction handoff, a cron delivery) rather than the owner."""
    turns: list[dict[str, Any]] = []
    for message in _envelope(raw).get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        text = " ".join(str(message.get("content") or "").split())
        if role not in ("user", "assistant") or not text:
            continue
        anchor = str(message.get("anchor") or "").lower() == "true" or str(message.get("id") or "") == str(anchor_id)
        turns.append({"role": role, "text": text, "anchor": anchor})
    index = next((i for i, turn in enumerate(turns) if turn["anchor"]), None)
    if index is None or _HOST_TURN.match(turns[index]["text"]):
        return "", []
    before = [t for t in turns[:index] if t["role"] == "assistant"][-1:]
    after = [t for t in turns[index + 1:] if t["role"] == "assistant"][:1]
    exchange = [{"role": t["role"], "text": _clip(t["text"], per_message)} for t in before + [turns[index]] + after]
    return turns[index]["text"], exchange


# -- query and facade --------------------------------------------------------------------------
def build_query(extract: dict[str, Any] | None, signals: list[dict[str, Any]], existing_task: dict[str, Any] | None) -> str:
    """A short search query: the task's title and main entities, the senders and subjects behind it."""
    parts: list[str] = []
    for source in (existing_task or {}, extract or {}):
        parts.append(str(source.get("title") or ""))
        entities = source.get("entities") if isinstance(source.get("entities"), dict) else {}
        for key in ("people", "organizations", "items", "places"):
            parts.extend(str(v) for v in (entities.get(key) or [])[:3] if isinstance(v, str))
        if isinstance(entities.get("repo"), str):
            parts.append(entities["repo"])
    for signal in signals[:3]:
        meta = signal.get("meta") or {}
        sender = str(meta.get("sender") or "")
        parts.append(sender.split("<")[0].strip(' "') if "<" in sender else sender)
        parts.append(str(signal.get("title") or ""))
    seen: set[str] = set()
    words: list[str] = []
    for part in parts:
        for word in part.split():
            clean = _FTS_SPECIALS.sub("", word).strip(",.;:()[]«»\"'¿?¡!")  # «Acmehosting:» read as a column filter and broke the search
            key = clean.lower()
            if key and key not in seen and len(key) > 1:
                seen.add(key)
                words.append(clean)
            if len(words) >= MAX_QUERY_WORDS:
                break
    return _clip(" ".join(words), MAX_QUERY).rstrip("…")


class Recall:
    """Gathers memory, past-session and precedent context for one planning step, bounded in time and size."""

    def __init__(self, ctx: Any, config: dict[str, Any], *, memory: MemoryRecall | None = None,
                 search_fn: Callable[..., Any] | None = None, session_fn: Callable[..., Any] | None = None, hermes_home: Any = None):
        self.ctx = ctx
        self.config = config
        if memory is None:
            from ..hostscope import profile_secret_scope

            memory = MemoryRecall(config, scope=lambda: profile_secret_scope(hermes_home))
        self.memory = memory
        if session_fn is None and search_fn is None:
            session_fn = host_session_tool(ctx)  # the host's tool in every shape; a given search_fn alone means no host (tests)
        self.session_fn = session_fn
        if search_fn is None and session_fn is not None:
            search_fn = lambda query, limit: session_fn(query=query, limit=limit)  # noqa: E731 - the discovery shape
        self.search_fn = search_fn

    async def _sessions(self, query: str, *, limit: int, timeout: float) -> list[dict[str, str]]:
        if self.search_fn is None:
            return []
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(self.search_fn, query, limit), timeout=timeout)
        except asyncio.TimeoutError:
            log.info("session search timed out after %.0fs", timeout)
            return []
        except Exception as exc:
            log.info("session search failed: %s", exc)
            return []
        return parse_sessions(raw, limit=limit)

    async def _call(self, timeout: float, **args: Any) -> Any:
        return await asyncio.wait_for(asyncio.to_thread(self.session_fn, **args), timeout=timeout)

    async def _precedents(self, names: list[str], *, limit: int, max_chars: int, timeout: float) -> list[dict[str, Any]]:
        """The owner's earlier requests about ``names`` (one phrase query per name over their own turns), each with
        the exchange around it; distinct sessions in the host's ranking, within ``max_chars``, never raising."""
        names = list(dict.fromkeys(n for n in names if n))
        if self.session_fn is None or not names or limit <= 0:
            return []

        async def discover(name: str) -> list[dict[str, str]]:
            phrase = '"' + " ".join(name.replace('"', " ").split()) + '"'
            try:
                return parse_precedents(await self._call(timeout, query=phrase, role_filter="user", limit=limit), limit=limit)
            except Exception as exc:
                log.info("precedent search failed for %s: %s", name, exc)
                return []

        hits: list[dict[str, str]] = []
        for found in await asyncio.gather(*(discover(name) for name in names)):
            hits.extend(item for item in found if item["session_id"] not in {h["session_id"] for h in hits})
        hits = hits[:limit]

        async def hydrate(item: dict[str, str]) -> dict[str, Any] | None:
            if not item.get("message_id"):
                return {**item, "exchange": []}
            try:
                raw = await self._call(timeout, session_id=item["session_id"], around_message_id=int(item["message_id"]), window=2)
            except Exception as exc:
                log.info("precedent window failed for %s: %s", item["session_id"], exc)
                return {**item, "exchange": []}
            said, exchange = parse_window(raw, anchor_id=item["message_id"])
            if not said:
                return None  # the match was the host speaking (a compaction handoff), not the owner
            return {**item, "owner_said": _clip(said, 240), "exchange": exchange}

        out: list[dict[str, Any]] = []
        used = 0
        for item in await asyncio.gather(*(hydrate(hit) for hit in hits)):
            if item is None:
                continue
            item.pop("message_id", None)
            size = len(item["owner_said"]) + len(item["title"]) + sum(len(t["text"]) for t in item["exchange"]) + 40
            if out and used + size > max_chars:
                break
            out.append(item)
            used += size
        return out

    async def gather(self, query: str, names: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
        """Memory and past sessions for ``query``; precedents for ``names`` (the senders or organisations involved,
        see :func:`precedent_names`), which also need ``context.sessions`` since they read the same history."""
        settings = self.config.get("context") or {}
        result: dict[str, Any] = {"query": query, "memory": "", "past_sessions": [], "precedents": []}
        if not query and not names:
            return result
        timeout = float(settings.get("timeout_seconds") or 45)
        max_chars = int(settings.get("max_chars") or 1500)
        memory_job = self.memory.recall(query, timeout=timeout) if query and settings.get("memory", True) else None
        sessions_job = self._sessions(query, limit=int(settings.get("session_results") or 3), timeout=timeout) if query and settings.get("sessions", True) else None
        precedents_job = None
        if names and settings.get("sessions", True) and settings.get("precedents", True):
            precedents_job = self._precedents(list(names), limit=int(settings.get("precedent_results") or 3), max_chars=max_chars * 2, timeout=timeout)
        memory, sessions, precedents = await asyncio.gather(
            memory_job if memory_job is not None else asyncio.sleep(0, result=""),
            sessions_job if sessions_job is not None else asyncio.sleep(0, result=[]),
            precedents_job if precedents_job is not None else asyncio.sleep(0, result=[]),
        )
        result["memory"] = _clip(memory, max_chars)
        result["precedents"] = precedents
        kept: list[dict[str, str]] = []
        used = 0
        for item in sessions:
            room = max_chars - used
            if room <= 80:
                break
            item = {**item, "snippet": _clip(item["snippet"], min(len(item["snippet"]), room))}
            kept.append(item)
            used += len(item["snippet"]) + len(item["title"]) + 40
        result["past_sessions"] = kept
        return result

    async def answer(self, questions: list[str]) -> list[dict[str, str]]:
        """One targeted lookup per question (what each implication asks of the owner's memory), all at once
        and within one deadline: ``[{question, memory}]`` in the order asked, ``memory`` empty when nothing
        came back in time. Bounded by ``context.questions`` and ``context.timeout_seconds``; never raises."""
        settings = self.config.get("context") or {}
        if not settings.get("memory", True):
            return []
        asked: list[str] = []
        for question in questions:
            text = " ".join(str(question or "").split())[:MAX_QUERY]
            if text and text not in asked:
                asked.append(text)
        asked = asked[: max(0, int(settings.get("questions") or 3))]
        if not asked:
            return []
        timeout = float(settings.get("timeout_seconds") or 45)
        limit = max(200, int(settings.get("max_chars") or 1500) // 2)
        answers = await asyncio.gather(*(self.memory.recall(question, timeout=timeout) for question in asked))
        return [{"question": question, "memory": _clip(answer, limit)} for question, answer in zip(asked, answers)]


def recall_summary(recalled: dict[str, Any] | None) -> dict[str, int]:
    if not recalled:
        return {"memory_chars": 0, "sessions": 0, "answers": 0, "precedents": 0}
    return {"memory_chars": len(recalled.get("memory") or ""), "sessions": len(recalled.get("past_sessions") or []),
            "answers": sum(1 for item in recalled.get("answers") or [] if isinstance(item, dict) and item.get("memory")),
            "precedents": len(recalled.get("precedents") or [])}
