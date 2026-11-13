"""Attention preferences: scoped, versioned, append-only rules the owner teaches the plugin.

Ported from mail-intelligence. A preference never executes anything: it only raises or lowers
how loudly a signal is surfaced (silent, digest, notify, prefer).
"""
from __future__ import annotations

from typing import Any

FIELDS = frozenset({"id", "dimension", "scope", "effect", "exceptions", "instruction", "origin", "status"})
DIMENSIONS = frozenset({"notification", "investigation", "response"})
EFFECTS = frozenset({"silent", "digest", "notify", "prefer"})
ORIGINS = frozenset({"explicit", "inferred", "imported"})
STATUSES = frozenset({"active", "candidate", "disabled"})
SCOPE_FIELDS = ("account", "sender", "message_type", "project", "kind", "source_kind", "repo", "subject_stem")


def sender_address(sender: Any) -> str:
    """The bare email address of a sender, lowercased: «Octopus Energy <hola@octopusenergy.example>» → «hola@octopusenergy.example».
    Preferences are scoped by address (that is what the owner means by "this sender"), so matching normalises the
    signal's sender the same way before comparing."""
    import re

    text = str(sender or "")
    match = re.search(r"<([^>]+)>", text)
    return (match.group(1) if match else text).strip().lower()


def sender_name(sender: Any) -> str:
    """The display name of a sender, lowercased and without quotes: «"coderabbitai[bot]" <notifications@github.com>» →
    «coderabbitai[bot]». GitHub relays bot mail from its own address, keeping the bot's name in the display name, so a
    rule aimed at one bot has to scope by name: scoping by ``notifications@github.com`` would silence every GitHub
    notice. Returns "" when the sender carries no display name."""
    import re

    text = str(sender or "").strip()
    match = re.match(r'^\s*"(?P<name>.*?)"\s*<', text) or re.match(r"^\s*(?P<name>[^<]*?)\s*<", text)
    if match:
        return match.group("name").strip().strip('"').lower()
    return "" if "@" in text else text.strip().strip('"').lower()


def subject_stem(subject: Any) -> str:
    """The part of a subject that repeats across a family of messages: lowercase, without list tags,
    numbers, dates or reply prefixes, first five words. «[newsroom-org] A security advisory on markdown-it
    affects…» and «Security alert» each become a stem a mute can hang on."""
    import re

    text = str(subject or "").lower()
    text = re.sub(r"^\s*((re|fwd?|fw|rv|aw|sv)\s*:\s*)+", "", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"[#\d][\w./-]*", " ", text)
    words = [w for w in re.split(r"[^\wáéíóúüñ]+", text) if w]
    return " ".join(words[:5])


def validate_preference(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != FIELDS:
        raise ValueError("preference fields must match the schema")
    for key in ("id", "instruction"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    for key, allowed in (("dimension", DIMENSIONS), ("effect", EFFECTS), ("origin", ORIGINS), ("status", STATUSES)):
        if not isinstance(data[key], str) or data[key] not in allowed:
            raise ValueError(f"invalid {key}")
    scope = data["scope"]
    if not isinstance(scope, dict) or set(scope) - set(SCOPE_FIELDS):
        raise ValueError("invalid scope")
    if any(not isinstance(v, str) or not v.strip() for v in scope.values()):
        raise ValueError("scope values must be non-empty strings")
    if not isinstance(data["exceptions"], list) or any(
        not isinstance(v, str) or not v.strip() for v in data["exceptions"]
    ):
        raise ValueError("exceptions must be a list of strings")
    return data


def new_preference(
    preference_id: str, *, instruction: str, dimension: str = "notification", scope: dict[str, str] | None = None,
    effect: str = "silent", exceptions: tuple[str, ...] = (), origin: str = "explicit", status: str = "active",
) -> dict[str, Any]:
    return validate_preference(
        {
            "id": preference_id, "dimension": dimension, "scope": dict(scope or {}), "effect": effect,
            "exceptions": list(exceptions), "instruction": instruction, "origin": origin, "status": status,
        }
    )


def valid_scope(scope: Any) -> bool:
    """A scope Radar can judge: only known fields, each a non-empty string."""
    return (isinstance(scope, dict) and not (set(scope) - set(SCOPE_FIELDS))
            and all(isinstance(v, str) and v.strip() for v in scope.values()))


def scope_matches(scope: dict[str, Any], observation: dict[str, Any]) -> bool:
    """Fails closed: a scope with a field Radar does not know matches nothing. On 2026-11-04 a row written by hand
    into the store carried ``{"event": "EDMO Hubs Townhall"}``; the unknown field was skipped, the scope was then empty
    and the rule silenced ninety signals of every kind in a day."""
    if not valid_scope(scope):
        return False
    for key in SCOPE_FIELDS:
        expected = scope.get(key)
        if not expected:
            continue
        actual = str(observation.get(key) or "")
        if key == "sender":  # a rule may store the bare address or «Name <addr>»; compare by address either way
            if "@" in str(expected):  # an address scope keeps matching the address, whatever the display name
                if sender_address(actual) != sender_address(expected):
                    return False
            elif sender_name(actual) != sender_name(expected):  # a name scope: GitHub relays bots from its own address
                return False
        elif actual.lower() != str(expected).lower():
            return False
    return True


def applicable(preferences: list[dict[str, Any]], observation: dict[str, Any], dimension: str = "notification") -> list[dict[str, Any]]:
    """Active preferences of one dimension whose scope matches, explicit ones first. A silence needs a scope: a rule
    that would mute everything is not a preference (that is ``/radar pause``), so one with an empty scope never applies."""
    usable = [
        p for p in preferences
        if p.get("status") == "active" and p.get("dimension") == dimension and p.get("effect") in EFFECTS
        and not (p.get("effect") == "silent" and not p.get("scope"))
        and scope_matches(p.get("scope") or {}, observation)
    ]
    usable.sort(key=lambda p: 0 if p.get("origin") == "explicit" else 1)
    return usable
