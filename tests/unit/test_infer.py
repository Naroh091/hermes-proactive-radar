from __future__ import annotations

from conftest import NOW

from radar.delivery import infer
from radar.delivery.learn import forget_preference
from radar.pipeline.triage import triage_signal
from radar.preferences import new_preference

NOW_S = "2026-10-19T10:00:00Z"
SENDER = "billing@linode.example"


def _mute(store, stem: str, idx: int, sender: str = SENDER) -> None:
    """Seed one narrow (sender + subject stem) silent rule, as a per-task mute would create."""
    pref = new_preference(
        f"mute:{sender}:{idx}", instruction=f"Silenciado: {stem}",
        scope={"sender": sender, "subject_stem": stem, "kind": "email"}, effect="silent", origin="explicit",
    )
    store.save_preference(pref, expected_version=0, now=NOW_S)


def _signal(subject: str, sender: str = f"Linode <{SENDER}>"):
    return {"id": "x", "kind": "email", "title": subject, "snippet": "",
            "meta": {"sender": sender, "subject": subject, "account": "me@example.org", "source_kind": "gmail"}}


def test_three_family_mutes_propose_one_sender_wide_candidate(store):
    for i, stem in enumerate(("factura marzo", "factura abril", "factura mayo")):
        _mute(store, stem, i)
    candidate = infer.maybe_infer_sender(store, sender=SENDER, now_dt=NOW, threshold=3)
    assert candidate is not None
    assert candidate["status"] == "candidate" and candidate["origin"] == "inferred"
    assert candidate["scope"] == {"sender": SENDER, "kind": "email"}
    assert [c["id"] for c in infer.list_candidates(store)] == [candidate["id"]]
    # a candidate is inert: a fresh message family of that sender still passes triage
    decision = triage_signal(_signal("Aviso de mantenimiento programado"), store.list_preferences())
    assert decision.keep is True


def test_below_threshold_proposes_nothing(store):
    for i, stem in enumerate(("factura marzo", "factura abril")):
        _mute(store, stem, i)
    assert infer.maybe_infer_sender(store, sender=SENDER, now_dt=NOW, threshold=3) is None
    assert infer.list_candidates(store) == []


def test_confirm_promotes_candidate_and_triage_then_silences(store):
    for i, stem in enumerate(("factura marzo", "factura abril", "factura mayo")):
        _mute(store, stem, i)
    candidate = infer.maybe_infer_sender(store, sender=SENDER, now_dt=NOW, threshold=3)
    confirmed = infer.confirm_candidate(store, candidate["id"], now_dt=NOW)
    assert confirmed is not None and confirmed["status"] == "active" and confirmed["id"] == candidate["id"]
    assert infer.list_candidates(store) == []  # no longer a candidate
    # now any mail from the sender, even a brand-new subject, is silenced at triage
    decision = triage_signal(_signal("Algo completamente nuevo"), store.list_preferences(active_only=True))
    assert decision.keep is False and decision.reason == f"preference:{confirmed['id']}"


def test_idempotent_and_dismissed_candidate_is_not_reproposed(store):
    for i, stem in enumerate(("factura marzo", "factura abril", "factura mayo")):
        _mute(store, stem, i)
    first = infer.maybe_infer_sender(store, sender=SENDER, now_dt=NOW, threshold=3)
    assert first is not None
    # proposing again while the candidate stands produces nothing new
    assert infer.maybe_infer_sender(store, sender=SENDER, now_dt=NOW, threshold=3) is None
    # the owner dismisses it; it must never come back
    forget_preference(store, first["id"], now_dt=NOW)
    assert infer.maybe_infer_sender(store, sender=SENDER, now_dt=NOW, threshold=3) is None
    assert infer.list_candidates(store) == []


def test_after_silence_reads_threshold_from_config(store):
    for i, stem in enumerate(("factura marzo", "factura abril")):
        _mute(store, stem, i)
    cfg = {"notifications": {"mute_to_rule_threshold": 2}, "owner": {"language": "es"}}
    candidate = infer.after_silence(store, cfg, sender=SENDER, now_dt=NOW)
    assert candidate is not None and candidate["scope"]["sender"] == SENDER


def test_blank_sender_is_ignored(store):
    assert infer.maybe_infer_sender(store, sender="", now_dt=NOW, threshold=3) is None
    assert infer.after_silence(store, {}, sender=None, now_dt=NOW) is None


def test_mixed_sender_formats_count_and_match_by_address(store):
    """A rule may store the bare address or «Name <addr>»; both must count as the same sender and both must
    silence incoming mail (the display name differs between messages)."""
    from radar.preferences import scope_matches

    gh = "notifications@github.example"
    for i, (sender, stem) in enumerate((
        (f'"linear-code[bot]" <{gh}>', "pr uno"),
        (f"octo-dev <{gh}>", "pr dos"),
        (gh, "pr tres"),  # bare address
    )):
        store.save_preference(new_preference(f"mute:{i}", instruction=stem, scope={"sender": sender, "subject_stem": stem, "kind": "email"},
                                             effect="silent", origin="explicit"), expected_version=0, now=NOW_S)
    candidate = infer.maybe_infer_sender(store, sender=f"Neil <{gh}>", now_dt=NOW, threshold=3)
    assert candidate is not None and candidate["scope"]["sender"] == gh  # three variants grouped into one address
    infer.confirm_candidate(store, candidate["id"], now_dt=NOW)
    # the confirmed address-scoped rule silences mail whatever display name it carries
    assert scope_matches({"sender": gh, "kind": "email"}, {"sender": f"Adam Smith <{gh}>", "kind": "email"})
    decision = triage_signal(_signal("Cualquier asunto", sender=f"Someone New <{gh}>"), store.list_preferences(active_only=True))
    assert decision.keep is False


def test_a_scope_radar_does_not_know_matches_nothing_and_a_silence_needs_a_scope(store):
    """2026-11-04: the chat agent wrote a preference row by hand with ``{"event": "EDMO Hubs Townhall"}``. The unknown
    field was skipped, the scope was empty and the rule silenced ninety signals of every kind in a day."""
    from radar.preferences import applicable, scope_matches

    hand_written = {"id": "pref-edmo-townhall", "status": "active", "dimension": "notification", "effect": "silent", "exceptions": [],
                    "origin": "explicit", "scope": {"event": "EDMO Hubs Townhall"}, "instruction": "No avisar más del EDMO Hubs Townhall"}
    observation = {"sender": "Recibos de Uber <noreply@uber.example>", "kind": "email", "account": "a@example.org"}
    assert scope_matches(hand_written["scope"], observation) is False
    assert applicable([hand_written], observation) == []
    assert triage_signal(_signal("Tu pedido del miércoles", sender="Recibos de Uber <noreply@uber.example>"), [hand_written]).keep is True
    everything = {**hand_written, "id": "pref-all", "scope": {}}
    assert applicable([everything], observation) == []  # a mute of everything is /radar pause, never a preference
    digest_all = {**everything, "effect": "digest"}
    assert applicable([digest_all], observation) == [digest_all]  # a global "never interrupt me" still applies
