from __future__ import annotations

import asyncio

from conftest import NOW, FakeContext, FakeLlm, active_config

from radar.delivery.learn import fallback_preference, forget_preference, learn_from_reply, learn_preference, list_preferences_text, notice_context, preference_from_fields, reply_context, store_preference
from radar.delivery.telegram_ui import command_text
from radar.pipeline.attention import resolve
from radar.pipeline.triage import triage_signal
from radar.prompts import PREFERENCE_PROMPT
from radar.runtime import Runtime

NOW_S = "2026-10-19T10:00:00Z"


def _seed(store):
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=NOW_S)
    store.create_task(task_id="t1", kind="check", title="Alertas de CI de Webapp", summary="CI falla", entities={}, expectations=[], checklist=[], now=NOW_S)
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at=NOW_S, external_ref="m1", group_key="g", title="[CI] build failed",
                     snippet="x", meta={"sender": "GitHub <ci@github.example>", "account": "me@example.org", "source_kind": "gmail"}, fingerprint="f1", now=NOW_S)
    store.link_task_signal("t1", "s1", "origin", NOW_S)


def _signal(sender="GitHub <ci@github.example>", kind="email"):
    return {"id": "x", "kind": kind, "title": "[CI] build failed", "snippet": "", "meta": {"sender": sender, "account": "me@example.org", "source_kind": "gmail"}}


def test_model_interprets_the_reply_stores_a_scoped_preference_and_triage_applies_it(store):
    cfg = active_config()
    _seed(store)
    answer = {"applies": True, "dimension": "notification", "effect": "digest", "scope": {"sender": "ci@github.example", "account": "", "message_type": "", "project": "", "kind": "", "source_kind": "", "repo": ""},
              "exceptions": ["security"], "instruction": "Los avisos de CI de GitHub solo en el resumen.", "why": "the owner wants CI mail batched"}
    llm = FakeLlm({"radar.preference": [answer, {**answer, "applies": False}]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    assert notice_context(store, "t1")["sender"] == "ci@github.example" and notice_context(store, "nope")["sender"] == ""
    result = asyncio.run(learn_preference(rt, text="los de CI mándamelos en el resumen", task_id="t1", actor="telegram:7", now_dt=NOW))
    pref = result["preference"]
    assert result["outcome"] == "preference" and pref["effect"] == "digest" and pref["scope"] == {"sender": "ci@github.example"} and pref["exceptions"] == ["security"]
    assert "solo en el resumen" in result["text"] and "remitente ci@github.example" in result["text"] and f"/radar forget {pref['id']}" in result["text"]
    assert store.list_preferences(active_only=True)[0]["id"] == pref["id"] and store.list_audit(subject_id=pref["id"])[0]["decision_code"] == "preference_learned"
    # Attention and triage honour it; a protected flag still breaks through.
    active = store.list_preferences(active_only=True)
    observation = {"account": "me@example.org", "sender": "GitHub <ci@github.example>", "message_type": "email", "kind": "check", "source_kind": "gmail", "project": "", "repo": ""}
    assert resolve({**observation, "sender": "ci@github.example"}, active, model_decision="notify_now").disposition == "digest"
    assert resolve({**observation, "sender": "ci@github.example"}, active, model_decision="notify_now", risk_flags=["security"]).disposition == "notify_now"
    # A reply the model says is no lasting rule is answered as unclear, and nothing new is stored.
    unclear = asyncio.run(learn_preference(rt, text="gracias", task_id="t1", actor="telegram:7", now_dt=NOW))
    assert unclear["outcome"] == "unclear" and len(store.list_preferences()) == 1
    # Restating the same rule makes a new version instead of a duplicate; forgetting disables it.
    again = store_preference(store, {"dimension": "notification", "effect": "digest", "scope": {"sender": "ci@github.example"}, "exceptions": (), "instruction": "CI en resumen"}, origin="explicit", now_dt=NOW)
    assert again["id"] == pref["id"] and again["version"] == 2 and len(store.list_preferences()) == 1
    assert "CI en resumen" in command_text(rt, "prefs")
    assert command_text(rt, f"forget {pref['id']}").startswith("Preferencia desactivada") and store.list_preferences(active_only=True) == []
    assert command_text(rt, "forget pref-nope").startswith("No encuentro") and command_text(rt, "prefs").startswith("No hay preferencias")


def test_without_a_model_the_owners_words_become_a_sender_scoped_rule_and_sources_refresh(store):
    cfg = active_config()
    _seed(store)
    rt = Runtime(FakeContext(cfg, llm=None), store, cfg, worker_id="w", clock=lambda: NOW)

    class Adapter:
        preferences: list = []

    rt.sources = {"src": Adapter()}
    result = asyncio.run(learn_preference(rt, text="No me avises más de este remitente", task_id="t1", actor="telegram:7", now_dt=NOW))
    pref = result["preference"]
    assert pref["effect"] == "silent" and pref["scope"] == {"sender": "ci@github.example"} and pref["instruction"] == "No me avises más de este remitente"
    assert rt.sources["src"].preferences and rt.sources["src"].preferences[0]["id"] == pref["id"]
    decision = triage_signal({**_signal(), "meta": {**_signal()["meta"], "sender": "ci@github.example"}}, rt.sources["src"].preferences, cfg)
    assert decision.keep is False
    assert triage_signal({**_signal(), "meta": {**_signal()["meta"], "sender": "other@example.org"}}, rt.sources["src"].preferences, cfg).keep is True
    # Wording decides the effect; account cues widen the scope; no context leaves a general rule.
    context = notice_context(store, "t1")
    assert fallback_preference("avísame siempre de esto", context)["effect"] == "notify"
    assert fallback_preference("agrúpalo en el resumen", context) == {"dimension": "notification", "effect": "digest", "scope": {"kind": "check"}, "exceptions": (), "instruction": "agrúpalo en el resumen"}
    assert fallback_preference("silencia toda esta cuenta", context)["scope"] == {"account": "me@example.org"}
    assert fallback_preference("no me avises de esto", notice_context(store, None))["scope"] == {}
    # The agent-facing tool validates fields the same way.
    direct = preference_from_fields({"instruction": "Interrumpir siempre por Webapp", "effect": "notify", "scope": {"repo": "newsroom-org/webapp", "bogus": "x"}, "exceptions": ["deadline", "nope"]})
    assert direct == {"dimension": "notification", "effect": "notify", "scope": {"repo": "newsroom-org/webapp"}, "exceptions": ("deadline",), "instruction": "Interrumpir siempre por Webapp"}
    try:
        preference_from_fields({"instruction": "", "effect": "silent"})
        raise AssertionError("empty instruction accepted")
    except ValueError:
        pass
    assert forget_preference(store, "missing", now_dt=NOW) is None


def test_a_reply_that_answers_and_lays_down_a_rule_teaches_both(store):
    cfg = active_config()
    _seed(store)
    answer = {"applies": True, "dimension": "response", "effect": "prefer", "scope": {"sender": "ci@github.example", "account": "", "message_type": "", "project": "", "kind": "", "source_kind": "", "repo": ""},
              "exceptions": [], "instruction": "Register the receipts that reach the personal mailbox; not the ones at work.", "why": "a line per mailbox"}
    llm = FakeLlm({"radar.preference": [answer, answer, {**answer, "applies": False}]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    declined = {"outcome": "declined", "text": "Anotado: no."}
    # A plain answer teaches nothing and is returned as it was.
    assert asyncio.run(learn_from_reply(rt, intent={"intent": "reject", "rule": False}, text="no", task_id="t1", actor="telegram:7", now_dt=NOW, result=declined)) == declined
    assert store.list_preferences() == []
    # An answer with a rule beside it keeps its outcome, stores the rule and says both.
    both = asyncio.run(learn_from_reply(rt, intent={"intent": "reject", "rule": True}, text="the personal ones yes, the others no", task_id="t1", actor="telegram:7", now_dt=NOW, result=declined))
    assert both["outcome"] == "declined" and both["text"].startswith("Anotado: no.\nPreferencia guardada") and both["preference"]["effect"] == "prefer"
    assert [p["effect"] for p in store.list_preferences(active_only=True)] == ["prefer"]
    # A reply that is a rule and nothing else goes the usual way.
    whole = asyncio.run(learn_from_reply(rt, intent={"intent": "preference"}, text="always register them", task_id="t1", actor="telegram:7", now_dt=NOW, result={"outcome": "preference", "text": ""}))
    assert whole["outcome"] == "preference" and whole["preference"]["version"] == 2  # the same rule restated: a new version, not a duplicate
    # When the model says the words are no lasting rule after all, the answer stands alone.
    assert asyncio.run(learn_from_reply(rt, intent={"intent": "reject", "rule": True}, text="no", task_id="t1", actor="telegram:7", now_dt=NOW, result=declined)) == declined


def test_the_preference_model_sees_the_question_the_owner_was_answering(store):
    cfg = active_config()
    _seed(store)
    store.create_task_proposal(proposal_id="q1", task_id="t1", title="Register this receipt in the books?", reason_text="", action_class="read", operation_id="ask",
                               params={}, options=[{"id": "yes", "label": "Register in the books"}, {"id": "no", "label": "Not this one"}], urgency="normal",
                               expires_at="2026-10-22T10:00:00Z", fingerprint="q1", now="2026-10-19T10:00:00Z", batch_key="now")
    row = {"task_id": "t1", "kind": "notice", "payload": {"proposal_ids": ["q1"], "text": "A receipt <b>arrived</b>. Register this receipt in the books?"}}
    context = reply_context(store, "t1", row)
    assert context["sender"] == "ci@github.example" and context["message"] == "A receipt arrived. Register this receipt in the books?"
    assert context["decisions"] == [{"question": "Register this receipt in the books?", "operation": "ask", "answers": ["Register in the books", "Not this one"]}]
    assert "decisions" not in reply_context(store, "t1", None) and "decisions" not in reply_context(store, "t1", {"payload": {}})
    answer = {"applies": True, "dimension": "response", "effect": "prefer", "scope": {"sender": "ci@github.example", "account": "", "message_type": "", "project": "", "kind": "", "source_kind": "", "repo": ""},
              "exceptions": [], "instruction": "Register the receipts that reach the personal mailbox in the books.", "why": "answers the question per mailbox"}
    llm = FakeLlm({"radar.preference": [answer]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    result = asyncio.run(learn_from_reply(rt, intent={"intent": "reject", "rule": True}, text="the personal ones yes, the others no", task_id="t1", actor="telegram:7", now_dt=NOW,
                                          result={"outcome": "declined", "text": "Noted."}, outbox_row=row))
    assert result["outcome"] == "declined" and result["preference"]["dimension"] == "response"
    sent = llm.calls[0]["input_text"] if "input_text" in llm.calls[0] else str(llm.calls[0])
    assert "Register this receipt in the books?" in sent and "Register in the books" in sent
    assert "context.decisions" in PREFERENCE_PROMPT


def test_a_receipt_the_model_calls_noise_is_kept_when_a_standing_rule_names_its_sender(store):
    from conftest import extract_answer, plan_answer
    from radar.pipeline.process import process_bundle, standing_rule

    cfg = active_config()
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now="2026-10-19T10:00:00Z")
    rule = store_preference(store, {"dimension": "response", "effect": "prefer", "scope": {"sender": "receipts@acme.example"}, "exceptions": (),
                                    "instruction": "Register the receipts from this sender that reach the personal mailbox in the books."}, origin="explicit", now_dt=NOW)
    signal = {"kind": "email", "meta": {"sender": "Acme Cloud <receipts@acme.example>", "account": "personal@example.org", "source_kind": "gmail"}}
    assert standing_rule(store.list_preferences(active_only=True), [signal])["id"] == rule["id"]
    assert standing_rule(store.list_preferences(active_only=True), [{"kind": "email", "meta": {"sender": "other@example.org"}}]) is None
    noise = extract_answer(relevant=False, category="noise", message_kind="receipt", title="Acme receipt 10 USD")
    llm = FakeLlm({"radar.extract": [noise], "radar.plan": [plan_answer(task={**plan_answer()["task"], "kind": "purchase", "title": "Acme receipt 10 USD"}, proposals=[])]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at="2026-10-19T10:00:00Z", external_ref="m1", group_key="g", title="Your Acme receipt",
                     snippet="paid 10 USD", meta=signal["meta"], fingerprint="f", now="2026-10-19T10:00:00Z")
    bundle = store.claim_signal_bundles("2026-10-19T10:00:00Z", "2026-10-19T10:10:00Z", 5)[0]
    outcome = asyncio.run(process_bundle(rt, bundle, NOW))
    assert outcome.get("kept_by_rule") == rule["id"] and outcome.get("result") != "noise" and outcome.get("task_id")
    plan_call = next(c for c in llm.calls if c.get("purpose") == "radar.plan")
    sent = str(plan_call)
    assert "kept_by_rule" in sent and "Register the receipts from this sender" in sent and '"category": "purchase"' in sent.replace("'", '"')
    assert "kept_by_rule means" in reason_prompt()


def reason_prompt() -> str:
    from radar.prompts import PLAN_PROMPT

    return PLAN_PROMPT
