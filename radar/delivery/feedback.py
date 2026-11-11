"""What the owner meant by a written reply to a notice.

Two layers. :func:`classify` is the deterministic floor, in Spanish and English: it settles trivial replies («sí»,
«no», «2», «gracias») on its own and stands in when the model is unavailable. :func:`understand` asks the model
(slot «intent») about everything else, in any language, with the message and its numbered decisions as context, and
code keeps the floor: a negation never approves, an answer must be one the decision offers, a number must exist.
The callback buttons remain the unambiguous path.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..i18n import language_name
from ..pipeline.schemas import INTENT_SCHEMA
from ..prompts import INTENT_SYSTEM

log = logging.getLogger(__name__)
OPEN_PROPOSAL = ("pending", "sent", "snoozed")
_TAGS = re.compile(r"<[^>]+>")

_EDIT = re.compile(r"m[áa]s breve|m[áa]s corto|cambia|corrige|reescribe|edita|shorter|rewrite|change it|edit")
_PREF = re.compile(r"no me avises|no avisar|siempre|todos los|estas alertas|nunca me|don'?t notify|stop notifying|always|never notify|mute these")
# A reply that draws a lasting line through a kind of thing («las de X sí, las otras no», «the ones from X yes, the rest
# no», «a partir de ahora», «from now on»): whatever it answers, it also lays down a rule (intent.rule).
_RULE = re.compile(
    r"\b(?:las|los) (?:de|que) \S.*\b(?:s[íi]|no)\b|\b(?:las|los) (?:otr[ao]s|dem[áa]s)\b|\bthe (?:ones|rest|others)\b"
    r"|a partir de ahora|en adelante|de ahora en adelante|from now on|desde ahora|siempre que|whenever|each time|cada vez que",
    re.I,
)
_LATER = re.compile(r"m[áa]s tarde|luego|ma[ñn]ana|recu[ée]rdamelo|later|tomorrow|remind me|not now|ahora no")
_REJECT = re.compile(
    r"^(no|nope|nah)\b|desc[aá]rta|\bcancela\b|rechazo|ignora|no lo env|no la env|no env|\bdismiss\b|\bcancel\b|\breject\b|don'?t send|do not send|\bskip\b"
    r"|no le escrib|no les escrib|no escrib|no respond|no contest|no le mand|no lo mand|no la mand|don'?t (?:write|reply|answer|respond|contact)|do not (?:write|reply|answer|respond|contact)"
)
_APPROVE = re.compile(r"env[íi]a|adelante|apruebo|\bvale\b|\bok\b|de acuerdo|hazlo|\byes\b|\bgo ahead\b|\bapprove\w*\b|\bdo it\b|\bsend it\b|\bsure\b")
# A written Spanish «sí» is a yes only when it opens the reply and nothing but an approval follows («sí», «sí,
# adelante», «sí, es correcto»: yes / yes, go ahead / yes, that's right). Mid-sentence «si» is "if": on 2026-11-06 the
# order "delete the repo if it exists and forget this topic" (in Spanish, with a mid-sentence «si») confirmed a check
# as correct and the order itself was lost.
_YES = re.compile(r"^s[íi]\b(?:[\s,.!]*(?:adelante|hazlo|dale|claro|por favor|vale|ok|perfecto|correcto|es correcto|de acuerdo|apruebo|env[íi]a\w*|se[ñn]or|please|$))")
# An order in the imperative is for the agent, not a button: it acts on the task with its tools.
_ORDER = re.compile(
    r"^(?:elimina|b[oó]rra|quita|cierra|abre|crea|escribe|escr[íi]bele|llama|avisa|dile|olvida|olv[íi]date|mira|revisa|busca|comprueba"
    r"|deja|pon|mueve|instala|desinstala|reinicia|sube|baja|actualiza|descarga|guarda|apunta|anota|recuerda|resuelve|arregla|contesta"
    r"|responde|pregunta|p[íi]dele|haz(?!lo)|delete|remove|close|open|create|write|call|check|look|forget|install|restart|fix|ask|tell)\b")
_QUESTION = re.compile(r"\?|^(qu[ée]|c[óo]mo|cu[áa]l|has |has comprobado|por qu|what|how|which|why|did you)")
_THANKS = re.compile(r"gracias|thanks|thank you")
_NEGATION = re.compile(
    r"\bno\b|\bnunca\b|\bjam[áa]s\b|\bpara nada\b|\btodav[íi]a no\b|\ba[úu]n no\b|\bespera\b|\besp[ée]rate\b|\bmejor no\b"
    r"|\bnot\b|\bnever\b|\bdon'?t\b|\bdo not\b|\bwait\b|\bhold on\b"
)
_DONE = re.compile(r"ya est[áa]|ya lo (he )?hecho|hecho ya|already done|done already|i did it|ya lo compr|ya lo reserv|already (booked|bought|sent)")

INTENTS = ("approve", "reject", "edit_request", "preference", "question", "thanks", "later", "done", "instruction", "unclear")

def _affirms(text: str) -> bool:
    return bool(_YES.match(text) or _APPROVE.search(text))


# Any yes-like word counts as a cue here: "yes, but don't send it" is never an approval, whoever reads it.
_CUE = re.compile(r"\bs[íi]\b|\byes\b|\byep\b|\byeah\b|\bok\b|\bokay\b|\bvale\b")


def _negated_approval(lowered: str) -> bool:
    for clause in re.split(r"[.;!?\n]", lowered):
        if (_CUE.search(clause) or _APPROVE.search(clause)) and _NEGATION.search(clause):
            return True
    return False


def has_negation(lowered: str) -> bool:
    return bool(_NEGATION.search(lowered))


def lays_down_a_rule(text: str) -> bool:
    """True when the reply draws a lasting line («las de yo@… sí, las otras no», «from now on…»), besides whatever
    it answers; the floor's guarantee that such a reply is learnt even when the model reads it as a plain yes or no."""
    return bool(_RULE.search(" ".join(str(text or "").split())))


def classify(text: str) -> dict[str, Any]:
    """Deterministic floor. Never returns ``approve`` for a negated reply; ``rule`` says the reply also lays down a
    lasting rule (see :func:`lays_down_a_rule`)."""
    verdict = _classify(text)
    if lays_down_a_rule(text):
        verdict["rule"] = True
    return verdict


def _classify(text: str) -> dict[str, Any]:
    lowered = " ".join(str(text or "").split()).lower()
    if not lowered:
        return {"intent": "unclear"}
    index = 0
    number = re.match(r"^\s*(\d{1,2})(?!\d)", lowered)  # «3», «12 no», «3 luego»; never a year
    if number:
        index = int(number.group(1))
        lowered = lowered[number.end():].strip(" .,;:)-")  # the order is what follows the number
        if not lowered:
            return {"intent": "approve", "proposal_index": index}
    if _DONE.search(lowered):
        return {"intent": "done", "proposal_index": index}
    if _PREF.search(lowered):
        return {"intent": "preference", "proposal_index": index}
    if _LATER.search(lowered) and not _APPROVE.search(lowered):
        return {"intent": "later", "proposal_index": index}
    if _EDIT.search(lowered):
        return {"intent": "edit_request", "proposal_index": index}
    # A reply that opens with a yes and then takes part of it back («go ahead, but don't send it», «sí, pero no lo
    # mandes») is neither an approval nor a refusal: unclear, for the agent. A plain refusal («don't send it», «no le
    # escribas, pero gracias») is a no, whatever follows.
    first = re.split(r"[.;!?,\n]|\s(?:pero|but)\s", lowered, maxsplit=1)[0]
    opens_yes = bool(_YES.match(lowered) or _CUE.search(first) or _APPROVE.search(first)) and not _NEGATION.search(first)
    if opens_yes and _NEGATION.search(lowered):
        return {"intent": "unclear", "proposal_index": index}
    if _REJECT.search(lowered):
        return {"intent": "reject", "proposal_index": index}
    if _negated_approval(lowered):
        return {"intent": "unclear", "proposal_index": index}
    if _ORDER.match(lowered):
        return {"intent": "unclear", "proposal_index": index}  # an instruction: the agent carries it out
    if _THANKS.search(lowered) and "?" not in lowered:
        return {"intent": "thanks", "proposal_index": index}
    if _QUESTION.search(lowered):
        return {"intent": "question", "proposal_index": index}
    if _affirms(lowered) or (number and len(lowered) <= 3):
        return {"intent": "approve", "proposal_index": index}
    return {"intent": "unclear", "proposal_index": index}


def validate_intent(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"not JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("intent") not in INTENTS:
        raise ValueError("invalid intent")
    index = raw.get("proposal_index", 0)
    return {"intent": raw["intent"], "proposal_index": int(index) if isinstance(index, int) else 0, "reason": str(raw.get("reason") or "")[:200],
            "option": str(raw.get("option") or "").strip().lower()[:64], "rule": raw.get("rule") is True}


def trivial(text: str) -> bool:
    """A reply the floor settles on its own, without the model: a bare number, or up to three words that are a plain
    yes, no, thanks, later or done ("yes", "no", "2", "ok thanks", "better tomorrow", in Spanish or English)."""
    lowered = " ".join(str(text or "").split()).lower()
    if re.fullmatch(r"\d{1,2}", lowered):
        return True
    words = lowered.replace(",", " ").split()
    return len(words) <= 3 and classify(lowered)["intent"] in ("approve", "reject", "thanks", "later", "done")


def decisions_in(store: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The numbered decisions of a message, as the model needs them: number, title, operation, offered answers, open."""
    out: list[dict[str, Any]] = []
    for index, pid in enumerate([p for p in payload.get("proposal_ids") or [] if isinstance(p, str)], 1):
        try:
            proposal = store.get_proposal(pid)
        except KeyError:
            continue
        out.append({
            "index": index, "id": pid, "title": str(proposal.get("title") or "")[:160], "operation": str(proposal.get("operation_id") or ""),
            "answers": [{"id": str(o.get("id")), "label": str(o.get("label") or "")[:64]} for o in proposal.get("options") or [] if isinstance(o, dict) and o.get("id")],
            "open": proposal.get("status") in OPEN_PROPOSAL,
        })
    return out


def names_the_action(text: str, decisions: list[dict[str, Any]]) -> tuple[int, str] | None:
    """``(number, answer id)`` when the reply, with no negation in it, is the first answer of an open decision said in
    the owner's own words («cancela el hotel» for «Cancelar el hotel», «send it» for «Send the draft»): the verbs that
    name an action (cancel, dismiss, remove) also appear in the refusal floor, so a reply that repeats the offered
    action must be matched before that floor can read it as a no. The first answer only: it is the one that does it."""
    from ..pipeline.reconcile import MIN_OVERLAP, overlap, stems

    lowered = " ".join(str(text or "").split()).lower()
    if not lowered or has_negation(lowered):
        return None
    said = stems(lowered)
    best: tuple[float, int, str] | None = None
    for decision in decisions:
        answers = decision.get("answers") or []
        if not decision.get("open") or not answers:
            continue
        first = answers[0]
        score = overlap(said, stems(first.get("label")))
        if score >= MIN_OVERLAP and (best is None or score > best[0]):
            best = (score, int(decision.get("index") or 0), str(first.get("id") or ""))
    return (best[1], best[2]) if best else None


async def understand(gate: Any, text: str, *, outbox_row: dict[str, Any], store: Any, language: str = "es") -> dict[str, Any]:
    """What the owner meant: the floor for a trivial reply or when the model is unavailable; otherwise the model, in any
    language, with the message and its decisions as context, merged with the floor's guarantees. On 2026-11-06 the floor
    alone read «Elimina el repo si existe y olvida este tema» as a yes («si existe») and confirmed a check instead of
    passing the order on; and the floor only speaks Spanish and English."""
    floor = classify(text)
    floor["source"] = "floor"
    payload = outbox_row.get("payload") or {}
    decisions = decisions_in(store, payload)
    named = names_the_action(text, decisions)
    if named is not None:
        floor.update({"intent": "approve", "proposal_index": floor.get("proposal_index") or named[0], "option": named[1]})
    if trivial(text) or gate is None or not getattr(gate, "available", False):
        return floor
    data = {
        "reply": " ".join(str(text or "").split())[:1000],
        "message": " ".join(_TAGS.sub("", str(payload.get("text") or "")).split())[:600],
        "decisions": decisions, "owner_language": language_name(language),
    }
    try:
        raw = await gate.structured(
            "intent", instructions=INTENT_SYSTEM, input_text=json.dumps(data, ensure_ascii=False), schema=INTENT_SCHEMA,
            purpose="radar.intent", subject_id=str(outbox_row.get("task_id") or payload.get("task_id") or "") or None,
        )
        verdict = validate_intent(raw)
    except Exception as exc:  # budget, host, schema: the floor is always there
        log.info("reply intent from the floor: %s", exc)
        return floor
    return merge(floor, verdict, text, decisions)


def merge(floor: dict[str, Any], verdict: dict[str, Any], text: str, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """The model's reading within the floor's guarantees: a negation never approves, a number must exist (the floor's
    leading number is kept when the model gives none), an answer must be one the referenced decision offers."""
    lowered = " ".join(str(text or "").split()).lower()
    out = dict(verdict)
    out["source"] = "model"
    if floor.get("rule"):
        out["rule"] = True  # a line drawn through a kind of thing is a rule whatever the model made of the answer
    if out["intent"] == "approve" and ((floor.get("intent") == "reject" and has_negation(lowered)) or _negated_approval(lowered)):
        out["intent"], out["option"] = "unclear", ""  # a real no in the words beats a model yes; a verb that names the action does not
    if floor.get("intent") == "approve" and floor.get("option") and out["intent"] in ("reject", "unclear") and not has_negation(lowered):
        out["intent"], out["option"] = "approve", str(floor["option"])  # the owner said the offered action in their own words
    if not (0 <= int(out.get("proposal_index") or 0) <= len(decisions)):
        out["proposal_index"] = 0
    if not out["proposal_index"] and floor.get("proposal_index"):
        out["proposal_index"] = int(floor["proposal_index"]) if floor["proposal_index"] <= len(decisions) else 0
    if out.get("option"):
        index = out["proposal_index"]
        open_ones = [d for d in decisions if d.get("open")]
        target = decisions[index - 1] if index else (open_ones[0] if len(open_ones) == 1 else None)
        if target is None or out["option"] not in {a["id"].lower() for a in target.get("answers") or []}:
            out["option"] = ""
    return out


def authorizes_execution(intent: Any) -> bool:
    value = intent.get("intent") if isinstance(intent, dict) else intent
    return value == "approve"
