from __future__ import annotations

from radar.pipeline.triage import triage_signal
from radar.preferences import new_preference, scope_matches

GITHUB = "notifications@github.com"
CODERABBIT = f'"coderabbitai[bot]" <{GITHUB}>'


def test_a_name_scope_matches_the_bot_and_leaves_the_rest_of_github_alone():
    """GitHub relays bot mail from its own address, so a rule scoped by address would mute every GitHub notice."""
    bot = {"sender": CODERABBIT, "kind": "email", "source_kind": "gmail"}
    human = {"sender": f'"Adam Smith" <{GITHUB}>', "kind": "email", "source_kind": "gmail"}
    assert scope_matches({"sender": "coderabbitai[bot]"}, bot) is True
    assert scope_matches({"sender": "coderabbitai[bot]"}, human) is False


def test_a_name_scope_silences_only_that_bot_in_triage():
    preference = new_preference(
        "silence:coderabbit", instruction="Nunca avisar de los correos de CodeRabbit.",
        scope={"sender": "coderabbitai[bot]", "kind": "email"}, effect="silent", origin="explicit",
    )
    bot = {"id": "x", "kind": "email", "title": "Re: [jacanales/devops] fix(hermes): … (PR #439)",
           "snippet": "", "meta": {"sender": CODERABBIT, "account": "me@example.org", "source_kind": "gmail"}}
    human = {"id": "y", "kind": "email", "title": "Re: [jacanales/devops] fix(hermes): … (PR #439)",
             "snippet": "", "meta": {"sender": f'"Adam Smith" <{GITHUB}>', "account": "me@example.org", "source_kind": "gmail"}}
    decision = triage_signal(bot, [preference])
    assert decision.keep is False and decision.reason == "preference:silence:coderabbit"
    assert triage_signal(human, [preference]).keep is True


def test_an_address_scope_still_matches_by_address_and_not_by_name():
    observation = {"sender": f"Linode <billing@linode.example>", "kind": "email"}
    assert scope_matches({"sender": "billing@linode.example"}, observation) is True
    assert scope_matches({"sender": "billing@linode.example"}, {"sender": f"Otra <otra@linode.example>", "kind": "email"}) is False
