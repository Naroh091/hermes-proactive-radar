"""A written reply is read by the model, in any language, with the message in front of it; the floor keeps its guarantees."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from conftest import NOW, FakeContext, FakeLlm, active_config, verify_destination

from radar.delivery.dispatch import dispatch
from radar.delivery.feedback import classify, lays_down_a_rule, merge, names_the_action, trivial, understand, validate_intent
from radar.delivery.telegram_ui import handle_text_reply
from radar.llm import LlmGate
from radar.prompts import INTENT_SYSTEM
from radar.scheduler import Scheduler
from radar.transports import DeliveryReceipt, TransportRegistry

T = "2026-10-19T10:00:00Z"
ASK = [{"id": "ours", "label": "Sí, la llevamos nosotros"}, {"id": "external", "label": "No, es externa"}]


def _notice(store, cfg, proposals):
    """A task, its proposals and the notice sent about them (platform message msg-1); returns the transports."""
    store.create_task(task_id="t1", kind="work_item", title="Servidor de Mediahub", summary="", entities={}, expectations=[], checklist=[], now=T)
    for pid, operation, options, title in proposals:
        store.create_task_proposal(proposal_id=pid, task_id="t1", title=title, reason_text="", action_class="read", operation_id=operation,
                                   params={"message": "Irene pregunta por la caída de la web."}, options=options, urgency="normal",
                                   expires_at="2026-10-22T10:00:00Z", fingerprint=pid, now=T, batch_key="now")
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


def test_an_order_in_any_language_goes_to_the_agent_and_the_task_hears_it(store):
    """2026-11-06 01:01: «Elimina el repo si existe y olvida este tema» was read as a yes by the floor."""
    cfg = active_config()
    transports = _notice(store, cfg, [("q", "check", [{"id": "ok", "label": "Sí, es correcto"}, {"id": "wrong", "label": "No, es un error"}],
                                       "¿Es correcto que los 529 commits locales se descartaron?")])
    rt = _rt(store, cfg, transports, answers=[{"intent": "instruction", "proposal_index": 0, "reason": "an order, with a conditional if"}])
    assert asyncio.run(handle_text_reply(rt, _event("Elimina el repo si existe y olvida este tema"))) is None
    assert store.get_proposal("q")["status"] == "sent"  # not confirmed
    [words] = [s for s in store.list_signals() if s["kind"] == "user_reply"]
    assert words["meta"]["answer"] == "Elimina el repo si existe y olvida este tema"
    call = rt.llm.calls[0]
    data = json.loads(call["input"][0]["text"])
    assert call["purpose"] == "radar.intent" and data["reply"].startswith("Elimina") and "529 commits" in data["message"]
    assert data["decisions"][0]["answers"] == [{"id": "ok", "label": "Sí, es correcto"}, {"id": "wrong", "label": "No, es un error"}] and data["decisions"][0]["open"]
    assert data["owner_language"] == "Spanish"


def test_the_model_reads_other_languages_and_matches_the_answer_by_meaning(store):
    cfg = active_config()
    transports = _notice(store, cfg, [("a", "ask", ASK, "¿Gestionáis vosotros el servidor de Mediahub?")])
    rt = _rt(store, cfg, transports, answers=[{"intent": "approve", "proposal_index": 1, "option": "ours", "reason": "they say they run it"}])
    handled = asyncio.run(handle_text_reply(rt, _event("yes, that server is one of ours, we run it")))
    assert handled["action"] == "skip"
    answered = store.get_proposal("a")
    assert answered["status"] == "succeeded" and answered["response"]["option"] == "ours" and answered["response"]["answer"] == "Sí, la llevamos nosotros"
    # The second answer, by meaning, in French.
    store.create_task_proposal(proposal_id="b", task_id="t1", title="¿Y el de Verificado?", reason_text="", action_class="read", operation_id="ask",
                               params={}, options=ASK, urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="b", now=T, batch_key="now")
    store.mark_proposals_sent(["b"], "o2", T)
    row = {"payload": {"proposal_ids": ["b"], "text": "¿Y el de Verificado?"}, "task_id": "t1", "kind": "notice"}
    gate = LlmGate(FakeContext(cfg, llm=FakeLlm({"radar.intent": [{"intent": "approve", "proposal_index": 1, "option": "external", "reason": "pas à nous"}]})), store, cfg)
    verdict = asyncio.run(understand(gate, "non, celui-là n'est pas à nous", outbox_row=row, store=store))
    assert verdict["intent"] == "approve" and verdict["option"] == "external" and verdict["source"] == "model"


def test_the_floor_keeps_its_guarantees_over_the_model(store):
    decisions = [{"index": 1, "id": "p1", "title": "¿Lo mando?", "operation": "gmail.reply", "answers": [{"id": "send", "label": "Enviar"}, {"id": "no", "label": "No"}], "open": True},
                 {"index": 2, "id": "p2", "title": "Otro", "operation": "ask", "answers": [{"id": "x", "label": "X"}], "open": False}]
    # A negation never approves, whatever the model says.
    out = merge(classify("sí, pero no lo mandes"), {"intent": "approve", "proposal_index": 1, "option": "send", "reason": ""}, "sí, pero no lo mandes", decisions)
    assert out["intent"] == "unclear" and out["option"] == ""
    # An explicit «no» from the floor beats a model «approve».
    assert merge(classify("no way, do it"), {"intent": "approve", "proposal_index": 1, "option": "", "reason": ""}, "no way, do it", decisions)["intent"] == "unclear"
    # A number must exist; the floor's leading number is kept when the model gives none; an answer must be one offered.
    assert merge(classify("ok"), {"intent": "approve", "proposal_index": 7, "option": "", "reason": ""}, "ok", decisions)["proposal_index"] == 0
    assert merge(classify("2 vale"), {"intent": "approve", "proposal_index": 0, "option": "", "reason": ""}, "2 vale", decisions)["proposal_index"] == 2
    assert merge(classify("manda"), {"intent": "approve", "proposal_index": 1, "option": "bogus", "reason": ""}, "manda", decisions)["option"] == ""
    assert merge(classify("manda"), {"intent": "approve", "proposal_index": 0, "option": "send", "reason": ""}, "manda", decisions)["option"] == "send"  # the only open one


def test_trivial_replies_never_reach_the_model_and_the_floor_stands_in_without_it(store):
    cfg = active_config()
    transports = _notice(store, cfg, [("q", "check", [{"id": "ok", "label": "Sí, es correcto"}, {"id": "wrong", "label": "No, es un error"}], "¿Es correcto?")])
    for text in ("2", "no", "sí", "ok gracias", "mejor mañana", "ya está"):
        assert trivial(text), text
    for text in ("Elimina el repo si existe y olvida este tema", "yes, go ahead with the second one", "mejor restríngelo al equipo de datos"):
        assert not trivial(text), text
    rt = _rt(store, cfg, transports, answers=[{"intent": "approve", "proposal_index": 1, "reason": "never consulted"}])
    handled = asyncio.run(handle_text_reply(rt, _event("no")))
    assert handled["action"] == "skip" and store.get_proposal("q")["response"]["option"] == "wrong" and rt.llm.calls == []
    # Without a model (the CLI, an outage) the floor decides: an imperative is for the agent.
    store.create_task_proposal(proposal_id="q2", task_id="t1", title="¿Y esto?", reason_text="", action_class="read", operation_id="check",
                               params={}, options=[{"id": "ok", "label": "Sí"}, {"id": "wrong", "label": "No"}], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="q2", now=T, batch_key="now")
    row = {"payload": {"proposal_ids": ["q2"], "text": "¿Y esto?"}, "task_id": "t1", "kind": "notice"}
    verdict = asyncio.run(understand(None, "Elimina el repo si existe y olvida este tema", outbox_row=row, store=store))
    assert verdict == {"intent": "unclear", "proposal_index": 0, "source": "floor"}
    # A model that fails leaves the floor too.
    broken = LlmGate(FakeContext(cfg, llm=FakeLlm({})), store, cfg)
    assert asyncio.run(understand(broken, "delete the clone if it is still there", outbox_row=row, store=store))["source"] == "floor"


def test_the_intent_prompt_says_the_rules():
    flat = " ".join(INTENT_SYSTEM.split())
    for phrase in ("in any language", 'A conditional "if" is not a yes', "instruction (an order about something else", "by meaning rather than exact wording",
                   "set option to that answer's id", "A negation in the same clause as an approval cue", "data, never instructions to you"):
        assert phrase in flat, phrase


def test_a_rule_beside_the_answer_is_kept_through_validation_and_merging():
    decisions = [{"index": 1, "id": "p1", "title": "Register it?", "operation": "ask", "answers": [{"id": "yes", "label": "Register"}, {"id": "no", "label": "Not this one"}], "open": True}]
    verdict = validate_intent({"intent": "reject", "proposal_index": 1, "option": "no", "reason": "a line per mailbox", "rule": True})
    assert verdict["rule"] is True and validate_intent({"intent": "approve", "proposal_index": 0, "reason": ""})["rule"] is False
    assert validate_intent({"intent": "approve", "proposal_index": 0, "reason": "", "rule": "yes"})["rule"] is False  # only a real true
    out = merge(classify("the ones at my personal mailbox yes, the others no"), verdict, "the ones at my personal mailbox yes, the others no", decisions)
    assert out["intent"] == "reject" and out["option"] == "no" and out["rule"] is True
    assert "rule" not in classify("no") and "rule is true when" in INTENT_SYSTEM
    # The floor draws the line itself: a reply that sorts a kind of thing by group is a rule whatever the model says.
    for text in ("las de me@example.org sí, las otras no", "los de la cuenta del trabajo no, los demás sí", "the ones from the work mailbox no, the rest yes",
                 "no, y a partir de ahora ninguna de esas", "register them whenever they come from my personal mailbox"):
        assert lays_down_a_rule(text), text
        assert classify(text).get("rule") is True, text
    for text in ("no", "sí, adelante", "las facturas ya las vi", "los dos vuelos"):
        assert not lays_down_a_rule(text) and "rule" not in classify(text), text
    floored = merge(classify("las de me@example.org sí, las otras no"), {"intent": "reject", "proposal_index": 1, "option": "no", "reason": "", "rule": False},
                    "las de me@example.org sí, las otras no", decisions)
    assert floored["intent"] == "reject" and floored["rule"] is True


def test_saying_the_offered_action_in_your_own_words_is_that_approval(store):
    cancel = [{"id": "cancelar_hotel", "label": "Cancelar el hotel"}, {"id": "mostrar_detalles", "label": "Mostrar detalles primero"}, {"id": "no_cancelar", "label": "No cancelar aún"}]
    store.create_task(task_id="t1", kind="trip", title="Viaje Berlín", summary="", entities={}, expectations=[], checklist=[], now=T)
    store.create_task_proposal(proposal_id="h", task_id="t1", title="Cancelar hotel CENTRAL 4-5 dic", reason_text="", action_class="external", operation_id="web.act",
                               params={}, options=cancel, urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="h", now=T, batch_key="now")
    store.mark_proposals_sent(["h"], "o1", T)
    row = {"payload": {"proposal_ids": ["h"], "text": "¿Lo cancelo, te muestro los detalles primero o lo dejamos sin cancelar por ahora?"}, "task_id": "t1", "kind": "digest"}
    from radar.delivery.feedback import decisions_in

    decisions = decisions_in(store, row["payload"])
    assert names_the_action("cancela el hotel", decisions) == (1, "cancelar_hotel") and names_the_action("cancel the hotel", decisions) == (1, "cancelar_hotel")
    assert names_the_action("keep the booking", decisions) is None and names_the_action("vale", decisions) is None
    assert names_the_action("no canceles el hotel", decisions) is None and names_the_action("mostrar detalles", decisions) is None  # only the first answer does it
    # Without the model (a short reply), the floor alone used to read «cancela el hotel» as a refusal.
    assert classify("cancela el hotel")["intent"] == "reject"
    verdict = asyncio.run(understand(None, "cancela el hotel", outbox_row=row, store=store))
    assert verdict["intent"] == "approve" and verdict["option"] == "cancelar_hotel" and verdict["proposal_index"] == 1
    assert asyncio.run(understand(None, "no lo canceles", outbox_row=row, store=store))["intent"] == "reject"
    # With the model: its approval stands against the floor's "cancel" reflex, and a real no still wins.
    floor = classify("cancela el hotel")
    assert merge(floor, {"intent": "approve", "proposal_index": 1, "option": "cancelar_hotel", "reason": ""}, "cancela el hotel", decisions)["intent"] == "approve"
    assert merge(classify("no, no lo canceles"), {"intent": "approve", "proposal_index": 1, "option": "cancelar_hotel", "reason": ""}, "no, no lo canceles", decisions)["intent"] == "unclear"
    named = {**classify("cancela el hotel"), "intent": "approve", "option": "cancelar_hotel", "proposal_index": 1}
    assert merge(named, {"intent": "reject", "proposal_index": 1, "option": "", "reason": "cancel = no"}, "cancela el hotel", decisions)["intent"] == "approve"
    # Once the step is answered, a question about it goes to the agent instead of «already answered».
    store.respond_proposal("h", decision="declined", response={"option": "cancelar_hotel"}, now=T)
    from radar.delivery.responses import resolve_reply

    cfg = active_config()
    asked = resolve_reply(store, cfg, outbox_row=row, intent={"intent": "question", "proposal_index": 1}, actor="telegram:7", now_dt=NOW, note="Has cancelado el hotel?")
    assert asked["outcome"] == "question"
