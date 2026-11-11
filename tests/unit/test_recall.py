from __future__ import annotations

import asyncio
import json
import time

from conftest import NOW, FakeContext, FakeLlm, active_config, config, extract_answer, plan_answer

from radar.pipeline import reason
from radar.pipeline.process import process_bundle, review_task
from radar.pipeline.recall import MemoryRecall, Recall, build_query, parse_precedents, parse_sessions, parse_tool_result, parse_window, pick_tool, precedent_names
from radar.runtime import Runtime

_Q = {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}
HINDSIGHT = [{"name": "hindsight_retain", "parameters": {"type": "object", "required": ["content"], "properties": {"content": {"type": "string"}}}},
             {"name": "hindsight_recall", "parameters": _Q}, {"name": "hindsight_reflect", "parameters": _Q}]
MNEMOSYNE = [{"name": "mnemosyne_shared_recall", "parameters": _Q}, {"name": "mnemosyne_graph_query", "parameters": _Q},
             {"name": "mnemosyne_recall", "parameters": _Q}, {"name": "mnemosyne_scratchpad_read", "parameters": {"type": "object", "properties": {}}}]
QUESTION = [{"name": "brv_curate", "parameters": {"type": "object", "required": ["content"], "properties": {"content": {"type": "string"}}}},
            {"name": "brv_query", "parameters": {"type": "object", "required": ["question"], "properties": {"question": {"type": "string"}}}}]


class FakeProvider:
    def __init__(self, schemas, answer, *, delay=0.0):
        self.schemas, self.answer, self.delay = schemas, answer, delay
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def get_tool_schemas(self):
        return self.schemas

    def handle_tool_call(self, name, args, **kwargs):
        self.calls.append((name, args))
        if self.delay:
            time.sleep(self.delay)
        return self.answer

    def queue_prefetch(self, query, *, session_id=""):
        self.calls.append(("queue_prefetch", {"query": query}))

    def prefetch(self, query, *, session_id=""):
        self.calls.append(("prefetch", {"query": query}))
        return self.answer

    def shutdown(self):
        self.closed = True


def test_pick_tool_finds_each_providers_recall_tool_and_honours_overrides():
    assert pick_tool(HINDSIGHT) == ("hindsight_recall", "query")
    assert pick_tool(MNEMOSYNE) == ("mnemosyne_recall", "query")
    assert pick_tool(QUESTION) == ("brv_query", "question")
    assert pick_tool(HINDSIGHT, "hindsight_reflect") == ("hindsight_reflect", "query")
    assert pick_tool(HINDSIGHT, "nope") is None and pick_tool([]) is None
    assert parse_tool_result(json.dumps({"result": "1. Prefiere ventanilla\n2. Vuela con TAP"})) == "1. Prefiere ventanilla\n2. Vuela con TAP"
    assert parse_tool_result(json.dumps({"result": "No relevant memories found."})) == ""
    assert parse_tool_result(json.dumps({"error": "Failed to search memory: boom"})) == ""
    assert parse_tool_result(json.dumps({"results": [{"text": "a"}]})) == '[{"text": "a"}]'
    assert parse_tool_result("plain text memories") == "plain text memories"


def test_sessions_are_parsed_from_the_host_envelope_and_bounded():
    raw = json.dumps({"success": True, "results": [
        {"session_id": "s1", "when": "2026-10-10 09:00", "title": "Viaje Lisboa", "snippet": "  busqué  hoteles cerca del Chiado ", "link": "x"},
        {"session_id": "", "snippet": "sin id"}, {"session_id": "s3", "snippet": ""},
        {"session_id": "s4", "when": "2026-10-11", "title": "", "preview": "p" * 900},
    ]})
    found = parse_sessions(raw, limit=3, per_item=100)
    assert [f["session_id"] for f in found] == ["s1", "s4"] and found[0]["snippet"] == "busqué hoteles cerca del Chiado"
    assert len(found[1]["snippet"]) == 100 and found[1]["snippet"].endswith("…")
    assert parse_sessions(json.dumps({"success": False, "error": "x"})) == [] and parse_sessions("not json") == []
    assert parse_sessions({"result": {"results": [{"session_id": "a", "snippet": "b"}]}}) == [{"session_id": "a", "when": "", "title": "", "snippet": "b"}]


def test_build_query_is_short_and_mentions_title_entities_and_senders():
    extract = extract_answer()
    signals = [{"title": "Confirmación de reserva TAP", "meta": {"sender": "TAP Air Portugal <noreply@tap.example>"}}]
    query = build_query(extract, signals, {"title": "Viaje a Lisboa", "entities": {"people": ["Ana"], "repo": "org/app"}})
    assert query.startswith("Viaje Lisboa Ana org/app") and "TAP Air Portugal" in query and len(query) <= 200  # one-letter words are dropped
    assert build_query(None, [], None) == ""
    # Characters the host's full-text search reads as syntax never reach it, and a long title is cut to its first words.
    noisy = [{"title": "Registrar: envío de factura (B/1234567). Todos los servicios activos", "meta": {"sender": '"Billing | registrar" <billing@registrar.example>'}}]
    query = build_query({"title": 'Invoice "B/1234567" for domain.es'}, noisy, None)
    assert ":" not in query and '"' not in query and "(" not in query and query.startswith("Invoice B/1234567 for domain.es")
    assert len(build_query({"title": " ".join(f"w{i}" for i in range(40))}, [], None).split()) == 16


def test_gather_runs_memory_and_sessions_concurrently_and_survives_timeouts():
    cfg = config(context={"timeout_seconds": 1, "max_chars": 300, "session_results": 2})
    provider = FakeProvider(HINDSIGHT, json.dumps({"result": "El dueño prefiere hoteles con desayuno. " * 20}))
    searches: list[tuple[str, int]] = []

    def search(query, limit):
        searches.append((query, limit))
        return json.dumps({"success": True, "results": [{"session_id": f"s{i}", "when": "w", "title": "t", "snippet": "x" * 150} for i in range(5)]})

    recall = Recall(FakeContext(cfg), cfg, memory=MemoryRecall(cfg, provider=provider), search_fn=search)
    result = asyncio.run(recall.gather("Viaje a Lisboa"))
    assert provider.calls == [("hindsight_recall", {"query": "Viaje a Lisboa"})] and searches == [("Viaje a Lisboa", 2)]
    assert len(result["memory"]) == 300 and result["memory"].endswith("…")
    assert len(result["past_sessions"]) == 2 and sum(len(s["snippet"]) for s in result["past_sessions"]) <= 300
    # A slow backend returns nothing instead of stalling the pipeline; a provider without tools uses prefetch.
    slow = Recall(FakeContext(cfg), cfg, memory=MemoryRecall(cfg, provider=FakeProvider(HINDSIGHT, "late", delay=1.5)), search_fn=lambda q, l: time.sleep(1.5))

    async def timed():
        started = time.monotonic()
        return await slow.gather("q"), time.monotonic() - started

    empty, elapsed = asyncio.run(timed())  # asyncio.run itself then waits for the lingering worker threads
    assert empty["memory"] == "" and empty["past_sessions"] == [] and elapsed < 1.4
    plain = FakeProvider([], "# Memory\n- likes aisle seats")
    assert asyncio.run(MemoryRecall(cfg, provider=plain).recall("q", timeout=1)) == "# Memory\n- likes aisle seats"
    assert [c[0] for c in plain.calls] == ["queue_prefetch", "prefetch"]
    # Loader failures (no host, no provider) degrade to no memory, and close() shuts the provider down.
    broken = MemoryRecall(cfg, loader=lambda sid: (_ for _ in ()).throw(RuntimeError("no host")))
    assert asyncio.run(broken.recall("q", timeout=1)) == "" and broken.last_error == "no host"
    memory = MemoryRecall(cfg, provider=provider)
    memory.close()
    assert provider.closed and memory.provider is None
    # Disabled sections are skipped entirely.
    off = config(context={"memory": False, "sessions": False})
    assert asyncio.run(Recall(FakeContext(off), off, memory=MemoryRecall(off, provider=provider), search_fn=search).gather("q"))["memory"] == ""


def test_targeted_questions_run_in_parallel_within_one_deadline():
    cfg = config(context={"timeout_seconds": 1, "max_chars": 400, "questions": 2})
    provider = FakeProvider(HINDSIGHT, json.dumps({"result": "Los padres del dueño viven en Gijón."}), delay=0.4)
    recall = Recall(FakeContext(cfg), cfg, memory=MemoryRecall(cfg, provider=provider), search_fn=lambda q, l: "{}")

    async def timed():
        started = time.monotonic()
        return await recall.answer(["¿Familia en Asturias?", "¿Familia en Asturias?", "¿Seguro de coche?", "¿Ha estado en Berlín?"]), time.monotonic() - started

    answers, elapsed = asyncio.run(timed())
    assert [a["question"] for a in answers] == ["¿Familia en Asturias?", "¿Seguro de coche?"]  # deduplicated, capped at context.questions
    assert all("Gijón" in a["memory"] for a in answers) and elapsed < 0.75  # two lookups of 0.4 s ran side by side
    assert sorted(args["query"] for _, args in provider.calls) == ["¿Familia en Asturias?", "¿Seguro de coche?"]
    slow = Recall(FakeContext(cfg), cfg, memory=MemoryRecall(cfg, provider=FakeProvider(HINDSIGHT, "late", delay=1.5)), search_fn=lambda q, l: "{}")
    assert asyncio.run(slow.answer(["q"])) == [{"question": "q", "memory": ""}]
    off = config(context={"memory": False})
    assert asyncio.run(Recall(FakeContext(off), off, memory=MemoryRecall(off, provider=provider), search_fn=lambda q, l: "{}").answer(["q"])) == []


def test_planner_input_carries_memory_and_past_sessions():
    recall = {"memory": "Prefiere ventanilla", "past_sessions": [{"session_id": "s1", "when": "w", "title": "t", "snippet": "buscó hoteles"}]}
    text = reason.build_input(now="2026-10-19T10:00:00Z", owner={}, trust_level="balanced", extract=None, signals=[], candidates=[], facts=[], preferences=[],
                              existing_task=None, recall=recall)
    payload = json.loads(text)
    assert payload["memory"] == "Prefiere ventanilla" and payload["past_sessions"][0]["session_id"] == "s1"
    assert "memory" not in json.loads(reason.build_input(now="n", owner={}, trust_level="balanced", extract=None, signals=[], candidates=[], facts=[],
                                                          preferences=[], existing_task=None, recall={"memory": "", "past_sessions": []}))
    assert "past_sessions" in reason.PLAN_PROMPT and "possibly stale" in reason.PLAN_PROMPT


def test_pipeline_recalls_before_planning_and_reviewing(store):
    cfg = active_config(trust={"level": "cautious", "overrides": {}, "auto_external": []})
    llm = FakeLlm({"radar.extract": [extract_answer()], "radar.plan": [plan_answer(proposals=[]), plan_answer(task_match={"decision": "advances", "task_id": "", "why": "r"}, proposals=[])]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    provider = FakeProvider(HINDSIGHT, json.dumps({"result": "Vuela siempre con TAP y prefiere ventanilla"}))
    rt.recall = Recall(rt.ctx, cfg, memory=MemoryRecall(cfg, provider=provider),
                       search_fn=lambda q, l: json.dumps({"success": True, "results": [{"session_id": "sess-9", "when": "w", "title": "Lisboa", "snippet": "miró hoteles en Alfama"}]}))
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now="2026-10-19T10:00:00Z")
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at="2026-10-19T10:00:00Z", external_ref="m1", group_key="g",
                     title="Confirmación de reserva", snippet="vuelo", meta={"sender": "TAP <noreply@tap.example>", "account": "a@example.org"}, fingerprint="f", now="2026-10-19T10:00:00Z")
    bundle = store.claim_signal_bundles("2026-10-19T10:00:00Z", "2026-10-19T10:10:00Z", 5)[0]
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["recall"] == {"memory_chars": 43, "sessions": 1, "answers": 0, "precedents": 0}
    plan_call = next(c for c in llm.calls if c.get("purpose") == "radar.plan")
    sent = json.dumps(plan_call, default=str, ensure_ascii=False)
    assert "prefiere ventanilla" in sent and "sess-9" in sent and "miró hoteles en Alfama" in sent
    assert provider.calls[0][1]["query"].startswith("Reserva") or "TAP" in provider.calls[0][1]["query"]
    # Reviewing an existing task recalls too; a broken recall never blocks the plan.
    task = store.get_task(outcome["task_id"])
    rt.recall = Recall(rt.ctx, cfg, memory=MemoryRecall(cfg, loader=lambda sid: (_ for _ in ()).throw(RuntimeError("down"))), search_fn=lambda q, l: 1 / 0)
    review = asyncio.run(review_task(rt, task, NOW))
    assert review["recall"] == {"memory_chars": 0, "sessions": 0, "answers": 0, "precedents": 0} and "deferred" not in review


def test_recall_status_tolerates_name_properties_and_warm_loads_the_provider(store):
    cfg = config()
    rt = Runtime(FakeContext(cfg, llm=None), store, cfg, worker_id="w", clock=lambda: NOW)

    class Named(FakeProvider):
        @property
        def name(self):
            return "hindsight"

    provider = Named(HINDSIGHT, json.dumps({"result": "x"}))
    rt.recall = Recall(rt.ctx, cfg, memory=MemoryRecall(cfg, loader=lambda sid: provider), search_fn=None)
    assert rt.recall_status()["provider"] is None  # not loaded yet

    async def scenario():
        rt.warm_recall()
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    status = rt.recall_status()
    assert status["provider"] == "hindsight" and status["tool"] == "hindsight_recall" and status["sessions"] is False
    assert rt.ctx.spawned == ["radar:recall-warm"]


def test_memory_calls_run_inside_the_profile_secret_scope():
    import contextlib

    cfg = config()
    entered: list[str] = []

    @contextlib.contextmanager
    def scope():
        entered.append("in")
        yield
        entered.append("out")

    provider = FakeProvider(HINDSIGHT, json.dumps({"result": "ok"}))
    memory = MemoryRecall(cfg, loader=lambda sid: provider, scope=scope)

    async def scenario():
        await memory.warm()
        return await memory.recall("q", timeout=2)

    assert asyncio.run(scenario()) == "ok" and entered == ["in", "out", "in", "out"]


def test_precedent_names_come_from_mail_senders_then_the_tasks_organisations():
    signals = [{"kind": "email", "meta": {"sender": "Acme Cloud <receipts@acme-cloud.example>"}},
               {"kind": "email", "meta": {"sender": "notices@mutual.example"}},
               {"kind": "email", "meta": {"sender": "Me <me@gmail.com>", "owner_sent": True}},
               {"kind": "email", "meta": {"sender": "someone@gmail.com"}},  # a generic mailbox with no name says nothing
               {"kind": "session_turn", "meta": {"sender": "Not Mail <x@y.example>"}}]
    assert precedent_names(signals, None, limit=5) == ["Acme Cloud", "acme-cloud", "mutual"]  # the display name and the domain's name
    assert precedent_names([], {"entities": {"organizations": ["Acme Corp", "Beta SL", "Gamma"]}}) == ["Acme Corp", "Beta SL"]
    assert precedent_names(signals[:1], {"entities": {"organizations": ["acme cloud", "Stripe"]}}) == ["Acme Cloud", "acme-cloud"]  # distinct, two at most
    assert precedent_names([{"kind": "email", "meta": {"sender": "x@y"}}], {"entities": "odd"}) == []
    # A department-style display name is nobody's word for the sender: the domain's name is what the owner says.
    billing = [{"kind": "email", "meta": {"sender": '"Departamento de Facturación | registrar" <facturacion@registrar.example>'}}]
    assert precedent_names(billing, None) == ["registrar"]
    assert precedent_names([{"kind": "email", "meta": {"sender": "Booking.com <noreply-iam@booking.example>"}}], None) == ["Booking.com", "booking"]


def test_precedents_keep_the_owners_own_turns_and_the_exchange_around_them():
    discovery = json.dumps({"success": True, "results": [
        {"session_id": "job-1", "source": "oneshot", "matched_role": "user", "match_message_id": "1", "snippet": "You are carrying out one step for >>>Acme<<<"},
        {"session_id": "chat-1", "source": "desktop", "title": "Add a paid invoice to the books", "when": "November 03, 2026 at 11:53 PM",
         "matched_role": "user", "match_message_id": "22", "snippet": "yes please, >>>Acme<<< has no withholding"},
        {"session_id": "chat-2", "source": "mobile", "title": "None", "matched_role": "user", "match_message_id": "33", "snippet": "...contact created for >>>Acme<<<"},
        {"session_id": "chat-3", "source": "telegram", "matched_role": "assistant", "match_message_id": "44", "snippet": "Acme said"},
        {"session_id": "chat-4", "source": "tui", "matched_role": "user", "match_message_id": "55", "snippet": "[System: The active model for this chat has changed to acme-1]"},
    ]})
    found = parse_precedents(discovery, limit=5)
    assert [p["session_id"] for p in found] == ["chat-1", "chat-2"]  # not a job, not the agent's words, not the host's own notes
    assert found[0]["owner_said"] == "yes please, Acme has no withholding" and found[0]["message_id"] == "22" and found[1]["title"] == ""
    assert parse_precedents(json.dumps({"success": False})) == [] and parse_precedents("nope") == []
    window = json.dumps({"success": True, "messages": [
        {"id": "20", "role": "tool", "content": "{\"output\": \"rows\"}"},
        {"id": "21", "role": "assistant", "content": "Done.  The two Acme invoices are in the books as paid supplier invoices."},
        {"id": "22", "role": "user", "content": "yes please, Acme has no withholding", "anchor": True},
        {"id": "23", "role": "assistant", "content": ""},
        {"id": "24", "role": "assistant", "content": "Withholding removed from the Acme supplier."},
        {"id": "25", "role": "assistant", "content": "Anything else?"},
    ]})
    said, exchange = parse_window(window, anchor_id="22")
    assert said == "yes please, Acme has no withholding"
    assert [t["role"] for t in exchange] == ["assistant", "user", "assistant"]
    assert exchange[0]["text"] == "Done. The two Acme invoices are in the books as paid supplier invoices." and exchange[2]["text"].startswith("Withholding")
    # The anchor found by id when the host does not mark it; a handoff the host wrote is no precedent at all.
    assert parse_window(window.replace(', "anchor": true', ""), anchor_id="22")[0] == "yes please, Acme has no withholding"
    handoff = json.dumps({"success": True, "messages": [{"id": "33", "role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted", "anchor": True}]})
    assert parse_window(handoff, anchor_id="33") == ("", []) and parse_window("{}", anchor_id="1") == ("", [])


def test_gather_adds_precedents_from_the_owners_history_bounded_and_optional():
    cfg = config(context={"timeout_seconds": 1, "max_chars": 600, "precedent_results": 2})
    calls: list[dict] = []

    def session_fn(**args):
        calls.append(args)
        if "around_message_id" in args:
            if args["session_id"] == "chat-2":
                return json.dumps({"success": True, "messages": [{"id": "30", "role": "user", "content": "[STILL IN PROGRESS] restated", "anchor": True}]})
            return json.dumps({"success": True, "messages": [
                {"id": "9", "role": "assistant", "content": "The invoice is in the books."},
                {"id": "10", "role": "user", "content": "put this one in the books too", "anchor": True}]})
        if args.get("role_filter") == "user":
            return json.dumps({"success": True, "results": [
                {"session_id": "chat-1", "source": "desktop", "title": "Invoices", "when": "w", "matched_role": "user", "match_message_id": "10", "snippet": "put this one in the books too"},
                {"session_id": "cron-1", "source": "cron", "matched_role": "user", "match_message_id": "1", "snippet": "x"},
                {"session_id": "chat-2", "source": "mobile", "matched_role": "user", "match_message_id": "30", "snippet": "...Acme contact created"}]})
        return json.dumps({"success": True, "results": [{"session_id": "chat-7", "when": "w", "title": "t", "snippet": "plain discovery"}]})

    recall = Recall(FakeContext(cfg), cfg, memory=MemoryRecall(cfg, provider=FakeProvider(HINDSIGHT, "")), session_fn=session_fn)
    result = asyncio.run(recall.gather("Acme receipt", names=["Acme Cloud", "Acme Cloud", ""]))
    assert result["precedents"] == [{"session_id": "chat-1", "when": "w", "title": "Invoices", "owner_said": "put this one in the books too",
                                     "exchange": [{"role": "assistant", "text": "The invoice is in the books."}, {"role": "user", "text": "put this one in the books too"}]}]
    assert result["past_sessions"][0]["session_id"] == "chat-7"  # the discovery shape still runs through the same tool
    assert {"query": '"Acme Cloud"', "role_filter": "user", "limit": 2} in calls and calls.count({"query": '"Acme Cloud"', "role_filter": "user", "limit": 2}) == 1
    assert {"session_id": "chat-1", "around_message_id": 10, "window": 2} in calls and {"session_id": "chat-2", "around_message_id": 30, "window": 2} in calls
    assert {"query": "Acme receipt", "limit": 3} in calls
    # No names: no precedent lookup; the toggle (or sessions off) turns it off; a fake search_fn alone (no host) means none.
    assert asyncio.run(recall.gather("q"))["precedents"] == []
    for off in (config(context={"precedents": False}), config(context={"sessions": False})):
        assert asyncio.run(Recall(FakeContext(off), off, memory=MemoryRecall(off, provider=FakeProvider(HINDSIGHT, "")), session_fn=session_fn).gather("q", names=["Acme"]))["precedents"] == []
    plain = Recall(FakeContext(cfg), cfg, memory=MemoryRecall(cfg, provider=FakeProvider(HINDSIGHT, "")), search_fn=lambda q, l: "{}")
    assert plain.session_fn is None and asyncio.run(plain.gather("q", names=["Acme"]))["precedents"] == []
    # A slow or broken tool yields no precedents instead of stalling or raising.
    slow = Recall(FakeContext(cfg), cfg, memory=MemoryRecall(cfg, provider=FakeProvider(HINDSIGHT, "")), session_fn=lambda **a: time.sleep(1.5))
    assert asyncio.run(slow.gather("q", names=["Acme"]))["precedents"] == []
    broken = Recall(FakeContext(cfg), cfg, memory=MemoryRecall(cfg, provider=FakeProvider(HINDSIGHT, "")), session_fn=lambda **a: 1 / 0)
    assert asyncio.run(broken.gather("q", names=["Acme"]))["precedents"] == []


def test_planner_input_carries_precedents_and_the_mailbox_of_each_signal():
    recall = {"memory": "", "past_sessions": [], "precedents": [{"session_id": "chat-1", "when": "w", "title": "Invoices", "owner_said": "put it in the books",
                                                                   "exchange": [{"role": "assistant", "text": "Done"}, {"role": "user", "text": "put it in the books"}]}]}
    signals = [{"id": "s1", "kind": "email", "title": "Receipt", "snippet": "paid", "meta": {"account": "work@example.org", "sender": "Acme Cloud <receipts@acme.example>", "attachments": ["Invoice_42.pdf"]}},
               {"id": "s2", "kind": "calendar", "title": "Meeting", "snippet": "", "meta": {"organizer": "x"}}]
    payload = json.loads(reason.build_input(now="n", owner={}, trust_level="balanced", extract=None, signals=signals, candidates=[], facts=[], preferences=[],
                                            existing_task=None, recall=recall))
    assert payload["precedents"] == recall["precedents"] and "memory" not in payload
    assert payload["signals"][0]["account"] == "work@example.org" and payload["signals"][0]["sender"].startswith("Acme Cloud")
    assert "account" not in payload["signals"][1] and "sender" not in payload["signals"][1]
    assert payload["signals"][0]["attachments"] == ["Invoice_42.pdf"] and "attachments" not in payload["signals"][1]
    assert "The original travels with the record" in reason.PLAN_PROMPT
    assert "are the owner's own earlier requests to their agent" in reason.PLAN_PROMPT and "The past decides the default" in reason.PLAN_PROMPT


def test_pipeline_hands_the_planner_the_senders_precedents(store):
    cfg = active_config(trust={"level": "cautious", "overrides": {}, "auto_external": []})
    llm = FakeLlm({"radar.extract": [extract_answer()], "radar.plan": [plan_answer(proposals=[])]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    seen: list[dict] = []

    def session_fn(**args):
        seen.append(args)
        if "around_message_id" in args:
            return json.dumps({"success": True, "messages": [{"id": "5", "role": "assistant", "content": "Both Acme invoices are registered in the books."},
                                                             {"id": "6", "role": "user", "content": "register this Acme invoice as paid too", "anchor": True}]})
        if args.get("role_filter") == "user":
            return json.dumps({"success": True, "results": [{"session_id": "chat-acme", "source": "desktop", "title": "Acme invoice", "when": "w",
                                                            "matched_role": "user", "match_message_id": "6", "snippet": "register this >>>Acme<<< invoice as paid too"}]})
        return json.dumps({"success": True, "results": []})

    rt.recall = Recall(rt.ctx, cfg, memory=MemoryRecall(cfg, provider=FakeProvider(HINDSIGHT, "")), session_fn=session_fn)
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now="2026-10-19T10:00:00Z")
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at="2026-10-19T10:00:00Z", external_ref="m1", group_key="g",
                     title="Your Acme receipt", snippet="paid 10 USD", meta={"sender": "Acme Cloud <receipts@acme.example>", "account": "work@example.org"}, fingerprint="f", now="2026-10-19T10:00:00Z")
    bundle = store.claim_signal_bundles("2026-10-19T10:00:00Z", "2026-10-19T10:10:00Z", 5)[0]
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome["recall"]["precedents"] == 1 and {"query": '"Acme Cloud"', "role_filter": "user", "limit": 3} in seen
    plan_call = next(c for c in llm.calls if c.get("purpose") == "radar.plan")
    sent = json.dumps(plan_call, default=str, ensure_ascii=False)
    for expected in ("chat-acme", "register this Acme invoice as paid too", "Both Acme invoices are registered in the books.", "work@example.org", "Acme Cloud <receipts@acme.example>"):
        assert expected in sent, expected
