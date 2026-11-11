"""The owner engaging with a notice teaches as much as muting one; and a message with nothing to decide asks nothing.

2026-11-06 22:55: a news-only reminder («Martin Holt canceló su membresía…») ended with a question the model made
up, «¿Le escribo para intentar retenerlo?». The owner answered; the code tried to decline a proposal that never
existed and said «Esa propuesta ya no está activa»; the words were lost, and nothing learnt that this mattered.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from conftest import NOW, FakeContext, FakeLlm, active_config, config, extract_answer, plan_answer, verify_destination

from radar.delivery.dispatch import dispatch
from radar.delivery.learn import ENGAGEMENTS_TO_LEARN, learn_from_engagement
from radar.delivery.remind import _item, compose, fallback_text, without_trailing_question
from radar.delivery.responses import respond_to_proposal
from radar.delivery.telegram_ui import handle_text_reply
from radar.llm import LlmGate
from radar.pipeline import act
from radar.pipeline.attention import resolve
from radar.prompts import REMIND_PROMPT
from radar.scheduler import Scheduler
from radar.transports import DeliveryReceipt, TransportRegistry

T = "2026-10-19T09:00:00Z"
BMC = "Buy Me a Coffee <no-reply@buymeacoffee.example>"


def _mail_task(store, task_id, title, *, sender=BMC, subject="Martin Holt cancelled their membership"):
    """A task born from a mail of ``sender`` (what a learnt interest hangs on)."""
    if not any(s["id"] == "src" for s in store.list_sources()):
        store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=T)
    sid = f"sig-{task_id}"
    store.add_signal(signal_id=sid, source_id="src", kind="email", occurred_at=T, external_ref=sid, group_key=f"thread:{task_id}", title=subject,
                     snippet="", meta={"sender": sender, "subject": subject, "account": "me@example.org", "source_kind": "gmail"}, fingerprint=sid, now=T)
    store.create_task(task_id=task_id, kind="other", title=title, summary="", entities={}, expectations=[], checklist=[], now=T, origin_signal_id=sid)
    store.link_task_signal(task_id, sid, "origin", T)
    return store.get_task(task_id)


def _news(store, pid, task_id, message):
    store.create_task_proposal(proposal_id=pid, task_id=task_id, title=message[:80], reason_text="", action_class="read", operation_id="inform",
                               params={"message": message, "importance": "low"}, options=[], urgency="low", expires_at="2026-10-22T10:00:00Z",
                               fingerprint=pid, now=T, batch_key="digest")
    return store.get_proposal(pid)


def _ask(store, pid, task_id, question):
    store.create_task_proposal(proposal_id=pid, task_id=task_id, title=question, reason_text="", action_class="read", operation_id="ask",
                               params={"fact": {"key": "k_" + pid, "yes": "yes", "no": "no"}},
                               options=[{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}], urgency="normal",
                               expires_at="2026-10-22T10:00:00Z", fingerprint=pid, now=T, batch_key="now")
    return store.get_proposal(pid)


def _sent_reminder(store, cfg):
    """Dispatch what is queued, deliver it as Telegram message msg-1 and return the transports."""
    verify_destination(store)
    transports = TransportRegistry()

    async def sender(destination, payload):
        return DeliveryReceipt("msg-1")

    transports.register("telegram", sender)
    dispatch(store, cfg, NOW)
    assert asyncio.run(Scheduler(store, cfg, worker_id="w", transports=transports, clock=lambda: NOW).tick())["delivered"] == 1
    return transports


def _rt(store, cfg, transports, answers=None):
    llm = FakeLlm({"radar.intent": list(answers or [])})
    gate = LlmGate(FakeContext(cfg, llm=llm), store, cfg) if answers is not None else None
    return SimpleNamespace(config=cfg, store=store, clock=lambda: NOW, transports=transports, gate=gate, llm=llm)


def _event(text):
    return SimpleNamespace(source=SimpleNamespace(platform="telegram", user_id="7", chat_id="42"), reply_to_message_id="msg-1", text=text)


def test_news_with_nothing_pending_is_told_and_never_ends_with_a_question(store):
    cfg = active_config()
    task = _mail_task(store, "bmc", "Cancelación de apoyo de 6 €/mes de Martin Holt")
    news = _news(store, "n", "bmc", "Martin Holt ha cancelado su membresía Contribuyente de 6 €/mes en Buy Me a Coffee porque el precio ya no le encaja.")
    item = _item({"task": task, "decision": None, "news": [news]}, "es")
    assert item["nothing_pending"] is True and item["news"][0].startswith("Martin Holt ha cancelado") and "done" not in item and "pending" not in item
    assert '"nothing_pending"' in REMIND_PROMPT and "Never end it with a question" in REMIND_PROMPT
    # Whatever the model writes, a message with no decision loses the question it ends with.
    llm = FakeLlm({"radar.remind": [
        {"text": "Martin Holt, tu miembro Contribuyente de 6 €/mes desde enero, canceló el 5 de noviembre en Buy Me a Coffee porque el precio ya no le encaja. ¿Le escribo para intentar retenerlo?"},
        {"text": "¿Le escribo a Martin Holt para intentar retenerlo?"},
    ]})
    gate = LlmGate(FakeContext(cfg, llm=llm), store, cfg, clock=lambda: NOW)
    items = [{"task": task, "decision": None, "news": [news]}]
    text = asyncio.run(compose(gate, items=items, language="es"))
    assert text.endswith("porque el precio ya no le encaja.") and "¿" not in text
    assert asyncio.run(compose(gate, items=items, language="es")) == fallback_text(items, "es")  # nothing but the question: the template
    assert without_trailing_question("Hecho. ¿Algo más? ¿Seguro?") == "Hecho." and without_trailing_question("¿Sí?") == ""
    # Behind a decision the question stays: it is the decision's.
    ask = _ask(store, "a", "bmc", "¿Le escribo para intentar retenerlo?")
    llm.answers["radar.remind"] = [{"text": "Peter canceló su apoyo. ¿Le escribo para intentar retenerlo?"}]
    assert asyncio.run(compose(gate, items=[{"task": task, "decision": ask, "news": [news]}], language="es")).endswith("¿Le escribo para intentar retenerlo?")


def test_a_reply_to_news_is_noted_reaches_the_task_and_teaches_at_once(store):
    cfg = active_config()
    _mail_task(store, "bmc", "Cancelación de apoyo de 6 €/mes de Martin Holt")
    _news(store, "n", "bmc", "Martin Holt ha cancelado su membresía Contribuyente de 6 €/mes en Buy Me a Coffee.")
    transports = _sent_reminder(store, cfg)
    row = store.outbox_by_platform_message("msg-1")
    assert row["payload"]["proposal_ids"] == [] and row["payload"]["news_ids"] == ["n"]
    acks = []

    async def sender(destination, payload):
        acks.append(payload["text"])
        return DeliveryReceipt("msg-2")

    transports.register("telegram", sender)
    rt = _rt(store, cfg, transports)  # «no» is trivial: the floor decides, no model needed
    handled = asyncio.run(handle_text_reply(rt, _event("no")))
    assert handled == {"action": "skip", "reason": "radar reply handled: noted"}
    assert acks == ["Anotado. Lo tengo en cuenta para este asunto."]  # never «Esa propuesta ya no está activa»
    [words] = [s for s in store.list_signals() if s["kind"] == "user_reply"]
    assert words["meta"]["task_id"] == "bmc" and words["meta"]["answer"] == "no" and words["meta"]["free_text"] is True
    # Writing back to what Radar rated as mere news says the rating was wrong: the sender matters from now on.
    [pref] = store.list_preferences(active_only=True)
    assert pref["effect"] == "prefer" and pref["scope"] == {"sender": "no-reply@buymeacoffee.example", "source_kind": "gmail"} and pref["origin"] == "inferred"
    assert "no-reply@buymeacoffee.example" in pref["instruction"] and "importancia alta" in pref["instruction"]
    [fb] = store.list_feedback(action="engaged")
    assert fb["target_id"] == "bmc" and json.loads(fb["reason"])["reaction"] == "words"
    learnt = [a for a in store.list_audit(subject_id=pref["id"]) if a["decision_code"] == "preference_learned"]
    assert learnt and learnt[0]["metadata_redacted"]["how"] == "engagement" and learnt[0]["metadata_redacted"]["underrated"] is True
    # The next mail of that sender: never silent, always important, no interruption of its own.
    observation = {"account": "me@example.org", "sender": BMC, "message_type": "email", "kind": "other", "source_kind": "gmail", "project": "", "repo": ""}
    quiet = resolve(observation, store.list_preferences(active_only=True), model_decision="silent")
    assert quiet.disposition == "digest" and quiet.reason == "preference_prefer" and quiet.raised
    assert resolve(observation, store.list_preferences(active_only=True), model_decision="notify_now").disposition == "notify_now"
    assert not resolve({**observation, "sender": "other@example.org"}, store.list_preferences(active_only=True), model_decision="silent").raised


def test_a_yes_in_words_to_news_is_noted_too_and_the_model_sees_no_decisions(store):
    cfg = active_config()
    _mail_task(store, "bmc", "Cancelación de apoyo de Martin Holt")
    _news(store, "n", "bmc", "Martin Holt ha cancelado su membresía.")
    transports = _sent_reminder(store, cfg)
    rt = _rt(store, cfg, transports, answers=[{"intent": "approve", "proposal_index": 0, "reason": "wants the mail written"}])
    handled = asyncio.run(handle_text_reply(rt, _event("sí, escríbele y ofrécele el plan de 3 €")))
    assert handled["reason"] == "radar reply handled: noted"
    assert json.loads(rt.llm.calls[0]["input"][0]["text"])["decisions"] == []
    [words] = [s for s in store.list_signals() if s["kind"] == "user_reply"]
    assert words["meta"]["answer"].startswith("sí, escríbele")  # the plan starts from these words


def test_engaging_twice_with_one_sender_marks_it_as_mattering_once_is_not_enough(store):
    cfg = active_config()
    _mail_task(store, "t1", "Factura de Linode de noviembre", sender="Linode <billing@linode.example>", subject="Invoice 1")
    _mail_task(store, "t2", "Factura de Linode de diciembre", sender="Linode <billing@linode.example>", subject="Invoice 2")
    _ask(store, "q1", "t1", "¿La pasamos a contabilidad?")
    _ask(store, "q2", "t2", "¿La pasamos a contabilidad?")
    assert ENGAGEMENTS_TO_LEARN == 2
    first = respond_to_proposal(store, cfg, proposal_id="q1", option="si", actor="telegram:7", now_dt=NOW)
    assert first["outcome"] == "answered" and store.list_preferences() == [] and len(store.list_feedback(action="engaged")) == 1
    # Answering the same task again does not count twice.
    assert learn_from_engagement(store, store.get_task("t1"), reaction="review", now_dt=NOW) is None and store.list_preferences() == []
    second = respond_to_proposal(store, cfg, proposal_id="q2", option="no", actor="desktop", now_dt=NOW, channel="desktop")
    assert second["outcome"] == "answered"
    [pref] = store.list_preferences(active_only=True)
    assert pref["effect"] == "prefer" and pref["scope"]["sender"] == "billing@linode.example" and "(2)" in pref["instruction"]
    # Muting, snoozing or closing teach nothing here; and a rule the owner disabled is never learnt back.
    assert learn_from_engagement(store, store.get_task("t2"), reaction="done", now_dt=NOW) is None
    from radar.delivery.learn import forget_preference

    forget_preference(store, pref["id"], now_dt=NOW)
    assert learn_from_engagement(store, store.get_task("t2"), reaction="accepted", now_dt=NOW, underrated=True) is None
    assert store.list_preferences(active_only=True) == []


def test_a_sender_the_owner_engaged_with_is_told_even_when_the_planner_sees_nothing_to_track(store):
    """2026-11-07, in the simulator: the same cancellation was «none» on one run and «new» on the next; with a learnt
    «prefer» rule the news must reach the owner either way."""
    from radar.pipeline import extract as extract_step, reason

    rule = {"id": "p-bmc", "status": "active", "dimension": "notification", "effect": "prefer", "scope": {"sender": "no-reply@buymeacoffee.example", "source_kind": "gmail"},
            "origin": "inferred", "exceptions": []}
    signal = {"id": "s-bmc", "kind": "email", "title": "Peter cancelled", "group_key": "thread:bmc", "meta": {"sender": BMC, "account": "me@example.org", "source_kind": "gmail"}}
    plan = reason.validate_plan(plan_answer(task_match={"decision": "none", "task_id": "", "why": "nothing to track"},
                                            task={**plan_answer()["task"], "kind": "other", "title": "Cancelación de Martin Holt"}, proposals=[],
                                            notify={"disposition": "digest", "urgency": "whenever", "importance": "low", "reason": "x"},
                                            message="Martin Holt ha cancelado su membresía de 6 €/mes."))
    extract = extract_step.validate_extract(extract_answer(category="other", title="Cancelación de Martin Holt"))
    nothing = act.apply(store, config(), signals=[signal], extract=extract, plan=plan, now_dt=NOW, preferences=[])
    assert nothing.decision == "none" and nothing.task_id is None
    told = act.apply(store, config(), signals=[signal], extract=extract, plan=plan, now_dt=NOW, preferences=[rule])
    task = store.get_task(told.task_id)
    assert told.decision == "new" and task["status"] == "done" and told.disposition == "digest" and told.attention_reason == "preference_prefer"
    [news] = store.list_proposals(task_id=task["id"])
    assert news["operation_id"] == "inform" and not news["params"].get("ask") and news["params"]["importance"] == "high"


def test_an_inferred_rule_applies_on_its_own_only_when_it_makes_something_louder(store):
    import pytest

    from radar.preferences import new_preference

    prefer = new_preference("p1", instruction="x", scope={"sender": "a@b.c"}, effect="prefer", origin="inferred", status="active")
    assert store.save_preference(prefer, expected_version=0, now=T) == 1
    silent = new_preference("p2", instruction="x", scope={"sender": "a@b.c"}, effect="silent", origin="inferred", status="active")
    with pytest.raises(ValueError):
        store.save_preference(silent, expected_version=0, now=T)
