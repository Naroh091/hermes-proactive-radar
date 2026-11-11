"""What arrived later outranks what a bundle left open, and what the owner's agent did at their request is their own doing."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from conftest import NOW, FakeContext, FakeLlm, active_config, extract_answer, plan_answer

from radar.pipeline import reason
from radar.pipeline.evidence import later_evidence, owner_activity
from radar.pipeline.process import process_bundle
from radar.prompts import EXTRACT_PROMPT, PLAN_PROMPT
from radar.runtime import Runtime

T = "2026-10-19T10:00:00Z"
OPENER = "You are carrying out one step of a long-lived task for your owner, on their behalf and without them watching."


def _at(minutes):
    return (NOW + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _turn(store, sid, minutes, said, *, session="s1", answered=""):
    store.add_signal(signal_id=sid, source_id="local:session-hooks", kind="session_turn", occurred_at=_at(minutes), external_ref=sid, group_key=f"session:{session}",
                     title=said[:60], snippet=said, meta={"session_id": session, "user_text": said, "assistant_text": answered}, fingerprint=sid, now=_at(minutes))
    return store.get_signal(sid)


def _mail(store, sid, minutes, title, snippet, *, sender="Freshly <hello@freshly.example>"):
    store.add_signal(signal_id=sid, source_id="src", kind="email", occurred_at=_at(minutes), external_ref=sid, group_key=f"thread:{sid}",
                     title=title, snippet=snippet, meta={"sender": sender, "account": "a@example.org"}, fingerprint=sid, now=_at(minutes))
    return store.get_signal(sid)


def _sources(store):
    store.ensure_local_observation_source(T, 30)
    store.add_source(source_id="src", kind="gmail", account_ref="a", resource_scope={}, retention_days=30, now=T)


def test_what_the_task_received_after_the_bundle_travels_with_it(store):
    """Freshly, 2026-11-04: the checkout chat ended at 11:13, the order confirmation arrived at 11:14 and opened the
    task, the chat was judged at 12:46 and the planner asked the owner to confirm a payment already made."""
    _sources(store)
    chat = [_turn(store, "c1", -18, "cómprame dos desodorantes más"), _turn(store, "c2", 0, "Paga", answered="Confirma Google Pay en el móvil.")]
    store.create_task(task_id="order", kind="purchase", title="Pedido Freshly", summary="", entities={"session_id": "s1"}, expectations=[], checklist=[], now=_at(1))
    _mail(store, "m-before", -40, "Carrito guardado", "Tu carrito te espera")
    confirmation = _mail(store, "m-after", 1, "¡Buena elección! - QWERTZXCV", "¡PEDIDO REALIZADO! TOTAL PAGADO 20,93 €")
    for sid, role in (("m-before", "context"), ("m-after", "origin"), ("c2", "context")):
        store.link_task_signal("order", sid, role, _at(1))
    later = later_evidence(store, ["order"], chat)
    assert [e["title"] for e in later] == ["¡Buena elección! - QWERTZXCV"]  # after the chat's last turn; not the earlier mail, not the chat itself
    assert later[0]["from"].startswith("Freshly") and "TOTAL PAGADO" in later[0]["excerpt"] and later[0]["at"] == confirmation["occurred_at"]
    assert later_evidence(store, [], chat) == [] and later_evidence(store, ["order"], []) == []


def test_the_owners_own_chat_around_a_bundle_travels_with_it_and_radars_jobs_do_not(store):
    """eBay, 2026-11-03: the owner asked their agent to log into eBay at 23:32; eBay's «password reset» and «new device»
    mails at 23:33 were judged on their own and read as an intrusion."""
    _sources(store)
    mails = [_mail(store, "e1", 0, "Your password was reset", "If this wasn't you…", sender="eBay <ebay@ebay.example>"),
             _mail(store, "e2", 2, "A new device is using your account", "Linux/Chrome, Madrid", sender="eBay <ebay@ebay.example>")]
    _turn(store, "o1", -1, "¿Puedes iniciar sesión en la cuenta de eBay y ver si la he usado?", answered="Entro, restablezco la contraseña y miro el historial.")
    _turn(store, "o0", -45, "otra cosa de hace rato")  # outside the window (30 min before the bundle)
    _turn(store, "j1", 1, OPENER, session="job-7", answered="…")  # one of radar's own jobs
    _turn(store, "h1", 3, "[IMPORTANT: Background process proc_01 completed normally (exit code 0).", session="s1")
    activity = owner_activity(store, mails)
    assert len(activity) == 1 and activity[0]["owner_said"].startswith("¿Puedes iniciar sesión") and "restablezco la contraseña" in activity[0]["agent_answered"]
    assert activity[0]["session"] == "s1" and activity[0]["at"] == _at(-1)[:16]
    # A conversation bundle of that same session does not list its own turns as "activity".
    assert owner_activity(store, [store.get_signal("o1")], session_id="s1") == []


def test_the_planner_is_told_what_both_mean():
    later = [{"task_id": "order", "at": T, "kind": "email", "from": "Freshly", "title": "¡Buena elección!", "excerpt": "PEDIDO REALIZADO"}]
    activity = [{"at": T[:16], "session": "s1", "owner_said": "entra en eBay", "agent_answered": "entro y restablezco la contraseña"}]
    payload = json.loads(reason.build_input(now=T, owner={}, trust_level="balanced", extract=None, signals=[], candidates=[], facts=[], preferences=[],
                                            existing_task=None, later_evidence=later, owner_activity=activity))
    assert payload["later_evidence"] == later and payload["owner_activity"] == activity
    bare = json.loads(reason.build_input(now=T, owner={}, trust_level="balanced", extract=None, signals=[], candidates=[], facts=[], preferences=[], existing_task=None))
    assert "later_evidence" not in bare and "owner_activity" not in bare
    for phrase in ("later_evidence is what the matched task received after this bundle's last item", "outranks what the bundle left open",
                   "owner_activity is the owner's own chat with their agent", "is not an intrusion and is never confirmed with them",
                   "means the payment is done", "never propose paying or confirming a payment again",
                   "What only the owner can do on their own device", "never proposed as a step"):
        assert phrase in " ".join(PLAN_PROMPT.split()), phrase
    flat = " ".join(EXTRACT_PROMPT.split())
    assert "financial_loss means money at risk of being lost" in flat and "never a payment the owner has yet to make" in flat


def test_a_conversation_judged_late_reaches_the_planner_with_the_later_confirmation(store):
    cfg = active_config()
    _sources(store)
    _turn(store, "c1", -18, "cómprame dos desodorantes más de Freshly")
    _turn(store, "c2", 0, "Paga", answered="Confirma Google Pay en el móvil.")
    store.create_task(task_id="order", kind="purchase", title="Pedido Freshly QWERTZXCV", summary="Pedido realizado", entities={"session_id": "s1"},
                      expectations=[], checklist=[], now=_at(1))
    _mail(store, "m-after", 1, "¡Buena elección! - QWERTZXCV", "¡PEDIDO REALIZADO! TOTAL PAGADO 20,93 €")
    store.link_task_signal("order", "m-after", "origin", _at(1))
    llm = FakeLlm({"radar.extract": [extract_answer(category="purchase", conversation_state="open")],
                   "radar.plan": [plan_answer(task_match={"decision": "advances", "task_id": "order", "why": "same order"}, proposals=[])]})
    rt = Runtime(FakeContext(cfg, llm=llm), store, cfg, worker_id="w", clock=lambda: NOW)
    later = NOW + timedelta(minutes=46)  # the session has been quiet for more than its quiet window
    bundle = next(b for b in store.claim_signal_bundles(_at(46), _at(56), 5) if b["group_key"] == "session:s1")
    asyncio.run(process_bundle(rt, bundle, later))
    plan_call = next(c for c in llm.calls if c["purpose"] == "radar.plan")
    payload = json.loads(plan_call["input"][0]["text"])
    assert payload["later_evidence"][0]["title"] == "¡Buena elección! - QWERTZXCV" and "TOTAL PAGADO" in payload["later_evidence"][0]["excerpt"]
    assert "owner_activity" not in payload  # the chat is the bundle itself
