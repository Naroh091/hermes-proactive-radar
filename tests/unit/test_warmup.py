"""The habits pass: what the owner's own mail and calendar say about them, read once and kept as habits.

The owner (2026-11-06): «nos falta un paso de warmup después del onboarding que nos permita conocer los patrones del
usuario, posiblemente leyendo su email»; and invitations are worth raising when there is a history of interaction
with the sender or their organisation, or of attending that kind of event.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from conftest import NOW, FakeContext, FakeLlm, active_config, config, extract_answer, plan_answer, verify_destination

from radar.delivery.telegram_ui import command_text
from radar.pipeline import act, extract as extract_step, reason
from radar.pipeline.correspondents import known_correspondent
from radar.providers.base import MailMessage
from radar.runtime import Runtime
from radar.scheduler import Scheduler
from radar.warmup import due, event_type_of, invitation_history, last_run, request, run

T = "2026-10-19T10:00:00Z"
_WINDOW = re.compile(r"after:(\d{4}/\d{2}/\d{2}) before:(\d{4}/\d{2}/\d{2})")


def _epoch(days_ago: float) -> float:
    return (NOW - timedelta(days=days_ago)).timestamp()


def _in_window(query: str, epoch: float) -> bool:
    match = _WINDOW.search(query)
    if not match:
        return True
    start = datetime.strptime(match.group(1), "%Y/%m/%d").replace(tzinfo=timezone.utc).timestamp()
    end = datetime.strptime(match.group(2), "%Y/%m/%d").replace(tzinfo=timezone.utc).timestamp()
    return start <= epoch < end


class FakeMail:
    """A mailbox: inbox messages (with senders) and sent messages (recipients only when read in full)."""

    def __init__(self, inbox, sent):
        self.inbox, self.sent, self.queries, self.reads = inbox, sent, [], []

    def search(self, query, *, max_results=20):
        self.queries.append(query)
        pool = self.inbox if "in:inbox" in query else self.sent if "in:sent" in query else []
        return [m for m in pool if _in_window(query, m.epoch)][:max_results]

    def read(self, message_id):
        self.reads.append(message_id)
        full = next(m for m in self.sent if m.id == message_id)
        return MailMessage(id=full.id, subject=full.subject, sender=full.sender, to=full.to, date=full.date, epoch=full.epoch)


class FakeCalendar:
    def __init__(self, events):
        self.events, self.calls = events, 0

    def list_events(self, *, time_min, time_max, calendar_id="primary"):
        self.calls += 1
        return [e for e in self.events if time_min <= e["start"] < time_max]


def _mail(mid, sender, subject, days_ago, *, to=""):
    return MailMessage(id=mid, subject=subject, sender=sender, to=to, date="", epoch=_epoch(days_ago))


def _event(title, days_ago, *, organizer, my_response):
    start = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    return {"id": title[:8], "title": title, "start": start, "end": start, "organizer": organizer, "my_response": my_response, "attendees": []}


def _rt(store, cfg, mail=None, calendar=None):
    sources = {}
    if mail is not None:
        sources["gmail"] = SimpleNamespace(provider=mail)
    if calendar is not None:
        sources["calendar"] = SimpleNamespace(provider=calendar)
    return SimpleNamespace(store=store, config=cfg, sources=sources, clock=lambda: NOW)


def test_event_titles_get_one_kind_in_any_of_the_owners_languages():
    assert event_type_of("Gala Avanza Tech 2026 - entrega de premios") == "award"
    assert event_type_of("Webinar: Community Notes at scale") == "webinar"
    assert event_type_of("Kick-off MEDIAHUB 3") == "meeting" and event_type_of("Conexión móvil para chatbot") == "meeting"
    assert event_type_of("EDMO Hubs Townhall") == "conference" and event_type_of("Moonshot Council on disinformation") == "conference"
    assert event_type_of("Fiesta 25 aniversario de Political Intelligence") == "social"
    assert event_type_of("Taller de verificación") == "workshop" and event_type_of("Curso de IA aplicada") == "course"
    assert event_type_of("Lanzamiento de la Biblioteca") == "launch" and event_type_of("") == "other" and event_type_of("Dentista") == "other"


def test_the_pass_reads_mail_and_calendar_and_keeps_what_it_learnt_as_habits(store):
    cfg = config(sources=[{"id": "gmail", "kind": "gmail", "address": "me@example.org"}])
    inbox = [
        _mail("l1", "Linode <billing@linode.example>", "Linode invoice #10001", 60), _mail("l2", "Linode <billing@linode.example>", "Linode invoice #10002", 30),
        _mail("l3", "Linode <billing@linode.example>", "Linode invoice #10003", 1),
        _mail("c1", "Elena Kovacs <elena@eurotech.example>", "EUROTECH night", 20), _mail("g1", "pat.sample@gmail.com", "hola", 10),
        _mail("me", "Me <me@example.org>", "note to self", 5),
    ]
    sent = [_mail("s1", "Me <me@example.org>", "Re: EUROTECH night", 19, to="Elena Kovacs <elena@eurotech.example>, cc@eurotech.example"),
            _mail("s2", "Me <me@example.org>", "Factura", 3, to="pablo@newsroom.example"),
            _mail("s3", "Me <me@example.org>", "draft", 400, to="old@nowhere.example")]  # outside the window
    events = [
        _event("Gala Avanza Tech 2025", 300, organizer="olga@avanzatech.example", my_response="accepted"),
        _event("Webinar on fact-checking", 100, organizer="events@webinars.example", my_response="declined"),
        _event("Webinar: AI and elections", 50, organizer="events@webinars.example", my_response="needsAction"),
        _event("Weekly sync", 7, organizer="me@example.org", my_response=""),
        _event("Next year's gala", -200, organizer="olga@avanzatech.example", my_response="needsAction"),  # the future is not a habit
    ]
    mail, calendar = FakeMail(inbox, sent), FakeCalendar(events)
    summary = asyncio.run(run(_rt(store, cfg, mail, calendar), NOW))
    assert summary["sources"] == 2 and summary["mail_listed"] == 6 and summary["mail_read"] == 2 and summary["events"] == 4 and summary["errors"] == 0
    assert summary["correspondents"] == 4 and summary["recurring"] == 1 and summary["event_types"] == 3 and summary["organizers"] == 2
    linode = store.get_habit("correspondent", "@linode.example")
    assert linode["received"] == 3 and linode["sent"] == 0 and linode["names"] == ["Linode"] and linode["last_received"] == (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    eurotech = store.get_habit("correspondent", "@eurotech.example")
    assert eurotech["received"] == 1 and eurotech["sent"] == 2 and eurotech["last_sent"] == (NOW - timedelta(days=19)).strftime("%Y-%m-%d")
    assert store.get_habit("correspondent", "pat.sample@gmail.com")["received"] == 1  # a public domain: the address is the party
    assert store.get_habit("correspondent", "@newsroom.example")["sent"] == 1 and store.get_habit("correspondent", "@example.org") is None  # never the owner
    [recurring] = store.list_habits("recurring")
    assert recurring["key"] == "@linode.example|linode invoice" and recurring["value"]["count"] == 3 and 28 <= recurring["value"]["cadence_days"] <= 31
    assert store.get_habit("event_type", "award") == {**store.get_habit("event_type", "award"), "accepted": 1, "declined": 0, "ignored": 0, "organized": 0}
    assert store.get_habit("event_type", "webinar")["declined"] == 1 and store.get_habit("event_type", "webinar")["ignored"] == 1
    assert store.get_habit("event_type", "meeting")["organized"] == 1
    assert store.get_habit("organizer", "@avanzatech.example")["accepted"] == 1 and store.get_habit("organizer", "@avanzatech.example")["types"] == {"award": 1}
    assert store.get_habit("organizer", "@webinars.example")["declined"] == 1 and store.get_habit("organizer", "@example.org") is None
    assert last_run(store)["ran_at"] == T and mail.reads == ["s2", "s1"] and calendar.calls == 12
    assert [a for a in store.list_audit(limit=5) if a["event_type"] == "warmup"][0]["decision_code"] == "completed"
    # A second pass refreshes in place.
    asyncio.run(run(_rt(store, cfg, mail, calendar), NOW))
    assert len(store.list_habits("correspondent")) == 4


def test_habits_decide_who_is_known_and_which_invitations_the_owner_attends(store):
    now = T
    store.upsert_habit(kind="correspondent", key="@eurotech.example", value={"sent": 2, "received": 1}, origin="warmup", now=now)
    store.upsert_habit(kind="event_type", key="award", value={"accepted": 2, "declined": 0, "ignored": 1, "organized": 0}, origin="warmup", now=now)
    store.upsert_habit(kind="event_type", key="webinar", value={"accepted": 0, "declined": 3, "ignored": 4, "organized": 0}, origin="warmup", now=now)
    store.upsert_habit(kind="organizer", key="@uni.example", value={"accepted": 1, "declined": 0, "ignored": 0, "organized": 0, "types": {"meeting": 1}}, origin="warmup", now=now)
    rt = SimpleNamespace(store=store, sources={}, clock=lambda: NOW)
    known = [{"id": "x", "occurred_at": T, "meta": {"sender": "Elena <elena@eurotech.example>"}}]
    assert asyncio.run(known_correspondent(rt, known, NOW)) is True  # no search needed
    gala = invitation_history(store, [{"title": "Gala de premios 2026", "meta": {"sender": "events@newfoundation.example"}}], {"title": "Gala de premios 2026"})
    assert gala["event_type"] == "award" and gala["attends"] is True and gala["type_history"]["accepted"] == 2 and gala["organizer_history"] == {}
    webinar = invitation_history(store, [{"title": "Webinar", "meta": {"sender": "x@webinars.example"}}], {"title": "Webinar: trust & safety"})
    assert webinar["attends"] is False
    from_unav = invitation_history(store, [{"title": "x", "meta": {"sender": "irene@uni.example"}}], {"title": "Charla sobre nada"})
    assert from_unav["event_type"] == "other" and from_unav["attends"] is True  # this organizer's invitations get accepted
    # An invitation from a stranger to the kind of event the owner attends gets through; to a kind they never do, it stays quiet.
    ask = {"title": "¿Asistes a la gala?", "rationale": "", "action_class": "read", "operation": "ask", "when": "", "urgency": "normal",
           "options": [{"id": "si", "label": "Sí"}, {"id": "no", "label": "No"}], "fact": {"key": "gala", "yes": "sí", "no": "no"}, "then": "step"}
    plan = reason.validate_plan(plan_answer(task={**plan_answer()["task"], "kind": "commitment", "title": "Gala de premios 2026", "status": "needs_user"},
                                            proposals=[ask], notify={"disposition": "digest", "urgency": "this_week", "importance": "high", "reason": "x"}))
    extract = extract_step.validate_extract(extract_answer(category="commitment", message_kind="invitation", title="Gala de premios 2026"))
    extract.update({"known_correspondent": False, "invitation_history": gala})
    through = act.apply(store, config(), signals=[], extract=extract, plan=plan, now_dt=NOW, preferences=[])
    assert [p["operation_id"] for p in store.list_proposals(task_id=through.task_id)] == ["ask"]
    extract.update({"invitation_history": webinar})
    quiet = act.apply(store, config(), signals=[], extract=extract, plan=plan, now_dt=NOW, preferences=[])
    assert store.list_proposals(task_id=quiet.task_id) == [] and quiet.disposition == "silent"


def test_the_scheduler_runs_the_pass_once_after_onboarding_then_on_request_or_monthly_in_quiet_hours(store):
    cfg = active_config()
    assert due(store, cfg, NOW, quiet=False) == "first"
    rt = Runtime(FakeContext(cfg, llm=FakeLlm({})), store, cfg, worker_id="w", clock=lambda: NOW)
    first = asyncio.run(Scheduler(store, cfg, worker_id="w", clock=lambda: NOW, runtime=rt).tick())
    assert first["warmup"]["why"] == "first" and first["warmup"]["sources"] == 0 and last_run(store)["ran_at"] == T
    assert asyncio.run(Scheduler(store, cfg, worker_id="w", clock=lambda: NOW, runtime=rt).tick())["warmup"] is None
    assert due(store, cfg, NOW + timedelta(days=6), quiet=True) == ""  # weekly: not yet
    assert due(store, cfg, NOW + timedelta(days=8), quiet=False) == "" and due(store, cfg, NOW + timedelta(days=8), quiet=True) == "refresh"
    request(store, T, by="cli")
    assert due(store, cfg, NOW, quiet=False) == "requested"
    asyncio.run(run(rt, NOW))
    assert due(store, cfg, NOW, quiet=False) == ""  # the request is consumed by the pass
    assert due(store, {**cfg, "warmup": {**cfg["warmup"], "enabled": False}}, NOW + timedelta(days=40), quiet=True) == ""


def test_the_command_runs_it_now_or_leaves_a_request_and_tells_the_status(store):
    cfg = active_config()
    verify_destination(store)
    rt = Runtime(FakeContext(cfg, llm=FakeLlm({})), store, cfg, worker_id="w", clock=lambda: NOW)
    assert command_text(rt, "warmup status") == "Todavía no he leído tus hábitos (correo y calendario)."
    assert command_text(rt, "warmup") == "Anotado: el gateway hará la lectura de hábitos en su próximo ciclo."  # no loop at hand: the next tick runs it
    assert due(store, cfg, NOW, quiet=False) == "requested"
    asyncio.run(run(rt, NOW))
    assert command_text(rt, "warmup status").startswith("Hábitos leídos el 2026-10-19 10:00 (12 meses): 0 contactos")
    assert "warmup" in command_text(rt, "help")
