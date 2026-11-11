from __future__ import annotations

import asyncio
import json

from conftest import NOW, FakeContext, active_config, verify_destination

from radar.jobs.runner import JobOutcome
from radar.jobs.service import enqueue_job_for_proposal, record_job_outcome, settle_lost_job, start_or_wait
from radar.operations import native
from radar.runtime import Runtime

NOW_S = "2026-10-19T10:00:00Z"


def _task_with_email(store, task_id="t1"):
    store.add_source(source_id="src", kind="gmail", account_ref="me@example.org", resource_scope={}, retention_days=30, now=NOW_S)
    store.create_task(task_id=task_id, kind="work_item", title="Bug de Hans", summary="Faltan capturas", entities={"draft": {"text": "Hi Hans, could you send screenshots?", "ref": "Draft ID: r5778572255897499240", "at": NOW_S}},
                      expectations=[], checklist=[], now=NOW_S)
    store.add_signal(signal_id="s1", source_id="src", kind="email", occurred_at=NOW_S, external_ref="m-hans-1", group_key="g", title="Re: upload bug",
                     snippet="x", meta={"sender": "Hans <hans@example.com>", "account": "me@example.org", "thread_id": "th1"}, fingerprint="f1", now=NOW_S)
    store.add_signal(signal_id="s2", source_id="src", kind="email", occurred_at="2026-10-19T09:00:00Z", external_ref="m-me-1", group_key="g", title="Re: upload bug",
                     snippet="x", meta={"sender": "Alex <me@example.org>", "account": "me@example.org", "thread_id": "th1"}, fingerprint="f2", now=NOW_S)
    store.link_task_signal(task_id, "s1", "origin", NOW_S)
    store.link_task_signal(task_id, "s2", "advances", NOW_S)


def _proposal(store, task_id="t1", operation="gmail.reply", pid="p1", params=None):
    store.create_task_proposal(proposal_id=pid, task_id=task_id, title="Enviar la respuesta a Hans", reason_text="Listo", action_class="external", operation_id=operation,
                               params=params or {"draft": "Hi Hans, could you send screenshots?", "draft_ref": "Draft ID: r5778572255897499240"},
                               options=[{"id": "send", "label": "Enviar borrador"}, {"id": "no", "label": "No"}], urgency="normal", expires_at="2026-10-22T10:00:00Z",
                               fingerprint="fp" + pid, now=NOW_S)
    store.respond_proposal(pid, decision="accepted", response={"option": "send", "actor": "telegram:7", "channel": "telegram"}, now=NOW_S)
    return store.get_proposal(pid)


class Mail:
    def __init__(self, *, fail=False):
        self.calls: list = []
        self.fail = fail

    def send_draft(self, draft_id):
        self.calls.append(("send_draft", draft_id))
        if self.fail:
            raise RuntimeError("quota exceeded")
        return {"result": "Draft sent successfully. Message ID: 1a1558945ae274e6"}

    def reply(self, message_id, body, *, reply_all=False):
        self.calls.append(("reply", message_id, body))
        return {"result": "Email sent. Message ID: 1a1558945ae274e7"}

    def read(self, message_id):
        self.calls.append(("read", message_id))
        from radar.providers.base import MailMessage
        return MailMessage(id=message_id, subject="Re: upload bug", to="hans@example.com")


def test_plan_resolves_parameters_from_the_task_or_declines(store):
    _task_with_email(store)
    proposal = _proposal(store)
    plan = native.plan(store, proposal, store.get_task("t1"), "send")
    assert plan["operation"] == "gmail.reply" and plan["params"]["message_id"] == "m-hans-1" and plan["params"]["to"] == "hans@example.com"
    assert plan["params"]["draft_id"] == "r5778572255897499240" and plan["params"]["body"].startswith("Hi Hans") and plan["params"]["account"] == "me@example.org"
    assert native.resolve("gmail.send_reply") == "gmail.reply" and native.resolve("investigate") is None
    # Without a draft there is nothing native to send; an unknown operation is never native.
    bare = _proposal(store, pid="p2", params={})
    store.create_task(task_id="t2", kind="work_item", title="sin borrador", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    assert native.plan(store, {**bare, "task_id": "t2"}, store.get_task("t2"), "send") is None
    assert native.plan(store, {**proposal, "operation_id": "investigate"}, store.get_task("t1"), "send") is None
    # Calendar: the response comes from the option; GitHub: repo and number from the latest github signal.
    store.add_source(source_id="cal", kind="calendar", account_ref="primary", resource_scope={}, retention_days=30, now=NOW_S)
    store.create_task(task_id="t3", kind="commitment", title="Reunión", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    store.add_signal(signal_id="c1", source_id="cal", kind="calendar", occurred_at=NOW_S, external_ref="ev1@created:x", group_key="cal", title="Reunión con Hans",
                     snippet="", meta={"event_id": "ev1", "calendar_id": "primary", "account": "primary", "my_response": "needsAction"}, fingerprint="c1", now=NOW_S)
    store.link_task_signal("t3", "c1", "origin", NOW_S)
    store.create_task_proposal(proposal_id="p3", task_id="t3", title="Responder a la invitación", reason_text="", action_class="external", operation_id="calendar.respond", params={},
                               options=[{"id": "accept", "label": "Aceptar"}, {"id": "decline", "label": "Declinar"}, {"id": "no", "label": "Luego"}], urgency="normal",
                               expires_at="2026-10-22T10:00:00Z", fingerprint="fp3", now=NOW_S)
    plan3 = native.plan(store, store.get_proposal("p3"), store.get_task("t3"), "decline")
    assert plan3["params"]["response"] == "declined" and plan3["params"]["event_id"] == "ev1"
    assert native.plan(store, store.get_proposal("p3"), store.get_task("t3"), "no") is None  # no response could be derived


def test_native_job_consumes_consent_once_acts_and_verifies(store, tmp_path):
    cfg = active_config(sources=[{"id": "gmail-a", "kind": "gmail", "address": "me@example.org", "server": "gmail-a", "provider": "gmail_mcp"}])
    verify_destination(store)
    _task_with_email(store)
    proposal = _proposal(store)
    rt = Runtime(FakeContext(cfg, llm=None, call_mcp=lambda *a, **k: None), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    assert cfg["_hermes_home"] == str(tmp_path)
    job = enqueue_job_for_proposal(store, cfg, proposal=proposal, option="send", now_dt=NOW)
    assert job is not None and job["kind"] == "native" and job["action_class"] == "external"
    spec = json.loads(job["prompt"])
    assert spec["operation"] == "gmail.reply" and spec["params"]["draft_id"] == "r5778572255897499240" and spec["consent"]["actor_ref"] == "telegram:7"
    assert store.get_proposal("p1")["status"] == "executing" and store.get_task("t1")["status"] == "in_progress"
    assert store.budget_usage("2026-10-19", "jobs") == {}  # native actions do not spend the delegated-job budget
    mail = Mail()
    rt.write_provider = lambda kind, account="": mail if kind == "mail" and account == "me@example.org" else None
    claimed = store.claim_queued_jobs(NOW_S, "2026-10-19T10:10:00Z", 5)
    outcome = asyncio.run(native.run_job(rt, claimed[0]))
    assert outcome.status == "succeeded" and mail.calls[0] == ("send_draft", "r5778572255897499240") and mail.calls[1] == ("read", "1a1558945ae274e6")
    assert outcome.result["effects"][0].startswith("Email sent to hans@example.com") and outcome.result["verified"] is True
    assert record_job_outcome(store, cfg, job=claimed[0], outcome=outcome, now_dt=NOW) is True
    assert store.get_proposal("p1")["status"] == "succeeded"
    signal = store.list_signals(kind="job_result", limit=1)[0]
    assert signal["meta"]["native"] == "gmail.reply" and signal["meta"]["status"] == "succeeded" and "✔" not in signal["title"]
    # The consent is single-use: a replayed job spec is refused before touching the provider.
    replay = asyncio.run(native.run_job(rt, claimed[0]))
    assert replay.status == "failed" and "consent" in (replay.error or "") and len(mail.calls) == 2
    # Tampered parameters never match the consented plan.
    tampered = dict(claimed[0]); tampered["prompt"] = json.dumps({**spec, "params": {**spec["params"], "to": "attacker@example.com"}})
    assert "match" in (asyncio.run(native.run_job(rt, tampered)).error or "")


def test_provider_failures_are_uncertain_and_missing_consent_falls_back_to_a_delegated_job(store, tmp_path, monkeypatch):
    cfg = active_config(sources=[{"id": "gmail-a", "kind": "gmail", "address": "me@example.org", "server": "gmail-a", "provider": "gmail_mcp"}])
    verify_destination(store)
    _task_with_email(store)
    proposal = _proposal(store)
    rt = Runtime(FakeContext(cfg, llm=None, call_mcp=lambda *a, **k: None), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    job = enqueue_job_for_proposal(store, cfg, proposal=proposal, option="send", now_dt=NOW)
    rt.write_provider = lambda kind, account="": Mail(fail=True)
    claimed = store.claim_queued_jobs(NOW_S, "2026-10-19T10:10:00Z", 5)
    outcome = asyncio.run(native.run_job(rt, claimed[0]))
    assert outcome.status == "unknown" and "quota exceeded" in (outcome.error or "")
    record_job_outcome(store, cfg, job=claimed[0], outcome=outcome, now_dt=NOW)
    assert store.get_proposal("p1")["status"] == "unknown"  # acted or not: never retried blindly, never reported as a clean failure
    # When consent cannot be bound (no verified destination, store error), a comment runs as a delegated job instead.
    store.add_source(source_id="gh", kind="github", account_ref="o/r", resource_scope={}, retention_days=30, now=NOW_S)
    store.create_task(task_id="t9", kind="work_item", title="otro", summary="", entities={"draft": {"text": "Hi", "ref": "Draft ID: r1"}}, expectations=[], checklist=[], now=NOW_S)
    store.add_signal(signal_id="s9", source_id="gh", kind="github", occurred_at=NOW_S, external_ref="e9", group_key="g9", title="PR", snippet="x",
                     meta={"repo": "o/r", "number": 7}, fingerprint="f9", now=NOW_S)
    store.link_task_signal("t9", "s9", "origin", NOW_S)
    p9 = _proposal(store, task_id="t9", pid="p9", operation="github.comment")

    def refuse(*args, **kwargs):
        raise RuntimeError("destination not verified")

    monkeypatch.setattr(native, "grant", refuse)
    delegated = enqueue_job_for_proposal(store, cfg, proposal=p9, option="send", now_dt=NOW)
    assert delegated is not None and delegated["kind"] == "proposal" and "APPROVED DRAFT" in delegated["prompt"]
    assert "native_fallback" in {a["decision_code"] for a in store.list_audit(subject_id="p9")}


def _approved_mail(store):
    verify_destination(store)
    _task_with_email(store)
    return _proposal(store)


def _config(tmp_path):
    cfg = active_config()
    cfg["_hermes_home"] = str(tmp_path)
    return cfg


def test_mail_never_falls_back_to_an_agent_when_consent_cannot_be_recorded(store, tmp_path, monkeypatch):
    """A delegated job would hold a send tool and could mail on its own: the failure of 2026-10-27."""
    proposal = _approved_mail(store)

    def refuse(*args, **kwargs):
        raise RuntimeError("destination not verified")

    monkeypatch.setattr(native, "grant", refuse)
    job, reason = start_or_wait(store, _config(tmp_path), proposal=proposal, option="send", now_dt=NOW)
    assert job is None and reason == "consent_unavailable" and store.list_jobs(task_id="t1", limit=5) == []
    waiting = store.get_proposal("p1")
    assert waiting["status"] == "waiting" and waiting["params"]["wait_reason"] == "consent_unavailable"
    # It waits, and retrying every tick leaves one line in the audit, not one per minute.
    start_or_wait(store, _config(tmp_path), proposal=store.get_proposal("p1"), option="send", now_dt=NOW)
    assert [a["decision_code"] for a in store.list_audit(subject_id="p1")].count("native_fallback") == 1


def test_an_approved_mail_waits_for_a_busy_task_instead_of_expiring(store, tmp_path):
    proposal = _approved_mail(store)
    store.create_job(job_id="busy", task_id="t1", proposal_id=None, kind="proposal", prompt="x", action_class="read", now=NOW_S)
    job, reason = start_or_wait(store, _config(tmp_path), proposal=proposal, option="send", now_dt=NOW)
    assert job is None and reason == "active_job" and store.get_proposal("p1")["status"] == "waiting"


def _claimed_native(store, tmp_path):
    cfg = _config(tmp_path)
    enqueue_job_for_proposal(store, cfg, proposal=_approved_mail(store), option="send", now_dt=NOW)
    [claimed] = store.claim_queued_jobs(NOW_S, "2026-10-19T10:10:00Z", 5)
    assert claimed["kind"] == native.NATIVE_KIND
    return cfg, claimed


def test_a_send_interrupted_after_spending_its_consent_is_never_repeated(store, tmp_path):
    cfg, claimed = _claimed_native(store, tmp_path)
    spec = json.loads(claimed["prompt"])
    consent = spec["consent"]
    store.consume_consent(consent["consent_id"], plan_hash=consent["plan_hash"], actor_ref=consent["actor_ref"], destination_id=consent["destination_id"],
                          verified_transport=consent["verified_transport"], now=NOW_S)
    record_job_outcome(store, cfg, job=claimed, outcome=JobOutcome("cancelled", error="runtime stopped"), now_dt=NOW)
    assert store.get_proposal("p1")["status"] == "unknown" and store.get_job(claimed["id"])["status"] == "unknown"
    assert "may already have acted" in str(store.get_job(claimed["id"])["result_summary"])


def test_a_send_interrupted_before_spending_its_consent_may_start_again(store, tmp_path):
    cfg, claimed = _claimed_native(store, tmp_path)
    record_job_outcome(store, cfg, job=claimed, outcome=JobOutcome("cancelled", error="runtime stopped"), now_dt=NOW)
    assert store.get_proposal("p1")["status"] == "waiting" and store.get_job(claimed["id"])["status"] == "cancelled"


def test_a_lost_external_job_ends_unknown_whatever_its_attempt(store, tmp_path):
    cfg, claimed = _claimed_native(store, tmp_path)
    spec = json.loads(claimed["prompt"])["consent"]
    store.consume_consent(spec["consent_id"], plan_hash=spec["plan_hash"], actor_ref=spec["actor_ref"], destination_id=spec["destination_id"],
                          verified_transport=spec["verified_transport"], now=NOW_S)
    settle_lost_job(store, cfg, job=claimed, now_dt=NOW)
    assert store.get_proposal("p1")["status"] == "unknown"
    # A delegated external step cannot prove it did nothing: the same.
    store.create_task(task_id="t2", kind="trip", title="Hotel", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    store.create_task_proposal(proposal_id="w1", task_id="t2", title="Cancelar", reason_text="", action_class="external", operation_id="web.act", params={},
                               options=[{"id": "do", "label": "Hazlo"}], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fw1", now=NOW_S)
    store.respond_proposal("w1", decision="accepted", response={"option": "do", "actor": "telegram:7", "channel": "telegram"}, now=NOW_S)
    job = enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("w1"), option="do", now_dt=NOW)
    [running] = store.claim_queued_jobs(NOW_S, "2026-10-19T10:10:00Z", 5)
    assert running["id"] == job["id"]
    record_job_outcome(store, cfg, job=running, outcome=JobOutcome("cancelled", error="runtime stopped"), now_dt=NOW)
    assert store.get_proposal("w1")["status"] == "unknown"


def test_a_read_job_cancelled_by_a_restart_still_starts_again(store, tmp_path):
    cfg = _config(tmp_path)
    store.create_task(task_id="t3", kind="trip", title="Hotel", summary="", entities={}, expectations=[], checklist=[], now=NOW_S)
    store.create_task_proposal(proposal_id="r1", task_id="t3", title="Mirar", reason_text="", action_class="read", operation_id="investigate", params={},
                               options=[{"id": "do", "label": "Hazlo"}], urgency="normal", expires_at="2026-10-22T10:00:00Z", fingerprint="fr1", now=NOW_S)
    store.respond_proposal("r1", decision="accepted", response={"option": "do", "actor": "telegram:7", "channel": "telegram"}, now=NOW_S)
    enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("r1"), option="do", now_dt=NOW)
    [running] = store.claim_queued_jobs(NOW_S, "2026-10-19T10:10:00Z", 5)
    record_job_outcome(store, cfg, job=running, outcome=JobOutcome("cancelled", error="runtime stopped"), now_dt=NOW)
    assert store.get_proposal("r1")["status"] == "waiting"


class Repo:
    def __init__(self, existing=None, fail=False):
        self.existing = existing
        self.fail = fail
        self.calls: list = []

    def pull_request_by_branch(self, repo, head):
        self.calls.append(("by_branch", repo, head))
        return self.existing

    def default_branch(self, repo):
        return "master"

    def open_pr(self, repo, *, head, base, title, body, draft=True):
        self.calls.append(("open_pr", repo, head, base, title, draft, body))
        if self.fail:
            raise RuntimeError("422 validation failed")
        self.existing = {"number": 2010, "url": "https://github.com/newsroom-org/webapp/pull/2010", "isDraft": True, "state": "OPEN", "title": title}
        return {"number": 2010, "url": "https://github.com/newsroom-org/webapp/pull/2010", "draft": True}


def test_branch_from_a_job_result_becomes_a_native_draft_pr_with_origin_mark(store, tmp_path):
    from radar.pipeline import act, extract as extract_step
    from radar.pipeline.process import _settle_draft, _synthetic_extract_from_job

    cfg = active_config()
    verify_destination(store)
    store.create_task(task_id="t1", kind="work_item", title="Fix upload 500 for files over 8 MB", summary="Hans reported a 500 on big uploads",
                      entities={"repo": "newsroom-org/webapp"}, expectations=[], checklist=[], now=NOW_S)
    signal = {"id": "j1", "kind": "job_result", "title": "[succeeded] Reproducir y preparar el arreglo", "snippet": "Fixed the limit",
              "meta": {"status": "succeeded", "findings": ["nginx client_max_body_size was 8m"], "refs": ["app/config/nginx.conf"], "branch": "radar/upload-limit",
                       "repo": "newsroom-org/webapp", "pr": "", "effects": ["pushed radar/upload-limit"]}}
    extract = _synthetic_extract_from_job(signal, store.get_task("t1"))
    branch = act.remember_branch(store, store.get_task("t1"), extract, NOW_S)
    assert branch["name"] == "radar/upload-limit" and branch["repo"] == "newsroom-org/webapp" and branch["findings"] == ["nginx client_max_body_size was 8m"]
    assert store.get_task("t1")["entities"]["branch"]["refs"] == ["app/config/nginx.conf"]
    store.create_task_proposal(proposal_id="pr1", task_id="t1", title="Abrir el PR en borrador", reason_text="La rama está lista", action_class="local",
                               operation_id="github.open_pr", params={}, options=[{"id": "do", "label": "Sí"}, {"id": "no", "label": "No"}], urgency="normal",
                               expires_at="2026-10-22T10:00:00Z", fingerprint="fpr1", now=NOW_S)
    store.respond_proposal("pr1", decision="accepted", response={"option": "do", "actor": "scheduler", "channel": "policy", "auto": True}, now=NOW_S)
    plan = native.plan(store, store.get_proposal("pr1"), store.get_task("t1"), "do")
    assert plan["operation"] == "github.open_pr" and plan["params"]["head"] == "radar/upload-limit" and plan["params"]["repo"] == "newsroom-org/webapp"
    assert plan["params"]["title"].startswith("Fix upload 500") and "hermes-radar" in plan["params"]["body"] and "t1" in plan["params"]["body"] and "nginx client_max_body_size" in plan["params"]["body"]
    rt = Runtime(FakeContext(cfg, llm=None), store, cfg, worker_id="w", clock=lambda: NOW, hermes_home=str(tmp_path))
    job = enqueue_job_for_proposal(store, cfg, proposal=store.get_proposal("pr1"), option="do", now_dt=NOW, auto=True)
    assert job["kind"] == "native" and job["action_class"] == "local" and json.loads(job["prompt"])["consent"]["actor_ref"] == "policy:trust.auto_external"
    repo = Repo()
    rt.write_provider = lambda kind, account="": repo if kind == "repo" else None
    claimed = store.claim_queued_jobs(NOW_S, "2026-10-19T10:10:00Z", 5)
    outcome = asyncio.run(native.run_job(rt, claimed[0]))
    assert outcome.status == "succeeded" and repo.calls[1][:5] == ("open_pr", "newsroom-org/webapp", "radar/upload-limit", "master", plan["params"]["title"]) and repo.calls[1][5] is True
    assert outcome.result["refs"] == ["https://github.com/newsroom-org/webapp/pull/2010"] and "Draft pull request opened" in outcome.result["effects"][0]
    record_job_outcome(store, cfg, job=claimed[0], outcome=outcome, now_dt=NOW)
    result_signal = store.list_signals(kind="job_result", limit=1)[0]
    _settle_draft(store, "t1", result_signal, NOW_S)
    assert store.get_task("t1")["entities"]["pr"] == "https://github.com/newsroom-org/webapp/pull/2010"
    # With the PR remembered, the same step is no longer native (the planner is told not to propose it again).
    assert native.plan(store, store.get_proposal("pr1"), store.get_task("t1"), "do") is None
    # Opening again for a branch that already has a PR is idempotent, and a provider failure is uncertain, never retried.
    assert native._github_open_pr(plan["params"], Repo(existing=repo.existing)).status == "succeeded"
    failed = asyncio.run(native.run_job(rt, {**claimed[0], "prompt": claimed[0]["prompt"]}))
    assert failed.status == "failed" and "consent" in (failed.error or "")  # consent already consumed
    try:
        native.plan(store, {**store.get_proposal("pr1"), "params": {"native": {"auto_merge": True}}}, {**store.get_task("t1"), "entities": {"repo": "x/y", "branch": {"name": "b"}}}, "do")
        raise AssertionError("auto-merge must be refused")
    except ValueError:
        pass
