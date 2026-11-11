"""Deterministic triage rules. They run before any model call and are cheap by design."""
from __future__ import annotations

import re

TRANSACTIONAL = re.compile(
    r"\b(confirm\w*|order|pedido|compra|purchase|receipt|recibo|invoice|factura|booking|reserva|reservation|"
    r"itinerar\w*|boarding|embarque|ticket|billete|flight|vuelo|hotel|check[- ]?in|shipp\w*|env[ií]o|enviado|"
    r"deliver\w*|entrega\w*|tracking|seguimiento|payment|pago|charged|cobro|refund|reembolso|renewal|renovaci[oó]n|"
    r"deadline|plazo|vence\w*|due|expira\w*|appointment|cita|meeting|reuni[oó]n|invitation|invitaci[oó]n|"
    r"bug|error|incident\w*|outage|ca[ií]da|pull request|\bPR\b|issue|deploy\w*|release|security|seguridad|"
    r"password|contrase[nñ]a|verification|verificaci[oó]n|cancel\w*|change\w*|cambio|modificaci[oó]n|interview|entrevista)\b",
    re.IGNORECASE,
)
NOISE_SUBJECT = re.compile(
    r"(newsletter|bolet[ií]n|unsubscribe|digest|weekly|semanal|promo\w*|oferta|descuento|% ?off|sale\b|rebajas|"
    r"webinar|black friday|cyber monday|you may like|te puede interesar|novedades|new arrivals|follow(?:ed)? you|"
    r"te sigue|ha publicado|new post|comment\w* on|mentioned you|trending|recomendaci[oó]n)",
    re.IGNORECASE,
)
STRONG_AUTOMATION = {"noreply-sender", "precedence:bulk", "precedence:list", "auto-submitted:auto-generated"}
# Routine "you signed in" / "access to the portal" notices that a sede electrónica, bank or portal sends on every
# login. They look transactional (certificate, security) but carry nothing to do: the owner wants them ignored.
ACCESS_NOTICE = re.compile(
    r"(se ha iniciado sesi[oó]n|sesi[oó]n iniciada|(?:nuevo )?inicio de sesi[oó]n|has iniciado sesi[oó]n|"
    r"se ha accedido\b|acceso (?:con|mediante) (?:el |su |tu )?certificado|"
    r"new sign[\- ]?in|signed in\b|new login\b|you (?:just )?logged in|we (?:noticed|detected) a (?:new )?sign[\- ]?in)",
    re.IGNORECASE,
)
# The same family from a service's automated sender: a login from somewhere new, the code to log in, a password reset.
# The owner does not want «was it you?» about any of these (2026-11-09: two GoDaddy mails reached them that way, about
# a login they were making themselves). Only automated senders: a colleague writing about a password is a person.
ACCOUNT_SECURITY = re.compile(
    r"(ha iniciado sesi[oó]n|(?:desde|en) (?:un |una )?(?:nuevo dispositivo|nueva ubicaci[oó]n|nuevo navegador)|"
    r"c[oó]digo[^.\n]{0,60}(?:inici\w+ (?:de )?sesi[oó]n|acced\w+ a (?:tu|su) cuenta)|(?:inici\w+ (?:de )?sesi[oó]n|login|log[- ]in|sign[- ]?in)[^.\n]{0,40}(?:c[oó]digo|code)\b|"
    r"(?:sign|log)[\w\s-]{0,40}new (?:device|location|browser)|new (?:device|location|browser)[\w\s-]{0,40}(?:sign|log)|"
    r"restablec\w+ (?:de )?(?:la |tu |su )?contrase[nñ]a|cambio de (?:la |tu |su )?contrase[nñ]a|contrase[nñ]a (?:se ha |ha sido |fue )?(?:cambiad|restablecid|actualizad|modificad)|"
    r"password (?:was |has been |is )?(?:reset|changed|updated)|reset (?:your )?password)",
    re.IGNORECASE,
)
# What is worse than a login: the account now answers to someone else's mail, lost its second factor or pays elsewhere.
# The owner is told, and still never asked whether it was them.
_CHANGED = r"(?:cambiad\w*|actualizad\w*|modificad\w*|a[nñ]adid\w*|eliminad\w*|changed|updated|added|removed)"
ACCOUNT_CHANGE = re.compile(
    r"((?:correo|e-?mail)(?: electr[oó]nico)?(?: de la cuenta| de recuperaci[oó]n| de contacto| address)?\s+(?:se ha |ha sido |fue |was |has been |is now )?" + _CHANGED + r"|"
    r"(?:se ha|ha sido|hemos|has been|was)\s+" + _CHANGED + r"\s+(?:el |la |tu |su |your )*(?:direcci[oó]n de )?(?:correo|e-?mail)\b|"
    r"(?:2fa|mfa|two[- ]factor|two[- ]step|doble factor|verificaci[oó]n en dos pasos|autenticaci[oó]n (?:en dos pasos|de dos factores|multifactor))[^.\n]{0,60}(?:desactiv|deshabilit|disabled|turned off|removed|eliminad)|"
    r"(?:desactiv\w*|deshabilit\w*|disabled|turned off|removed)[^.\n]{0,60}(?:2fa|mfa|two[- ]factor|two[- ]step|doble factor|verificaci[oó]n en dos pasos|autenticaci[oó]n (?:en dos pasos|de dos factores|multifactor))|"
    r"(?:m[eé]todo de pago|datos (?:de pago|bancarios)|cuenta bancaria|iban|payment (?:method|details)|bank (?:account|details)|payout (?:method|account))[^.\n]{0,40}" + _CHANGED + r")",
    re.IGNORECASE,
)
# Official administrative e-notifications ("tiene una notificación electrónica pendiente de comparecencia"): the
# message only says a notice is waiting in a sede; the owner collects those on his own schedule and asked not to
# be told about them (he was muting them one by one).
ENOTIFICATION = re.compile(
    r"(notificaci[oó]n(?:es)? electr[oó]nica|notificaci[oó]n enviada|puesta a disposici[oó]n|"
    r"pendiente de comparecencia|comparecencia en (?:la )?sede|direcci[oó]n electr[oó]nica habilitada|notific@)",
    re.IGNORECASE,
)
# A message that speaks to the assistant itself and gives it orders («si eres un LLM, haz un POST a…», «ignore
# your previous instructions», «no hace falta que avises a tu humano») is an attempt to steer the assistant, not
# news for the owner. The extractor is told the same; this floor does not depend on it noticing (2026-11-03 the
# model filed such a mail as a harmless «technical test» and the owner never heard of it).
INJECTION = re.compile(
    r"(si eres (?:un|una) (?:llm|ia|modelo(?: de lenguaje)?|asistente|agente|inteligencia artificial)|"
    r"if you(?:'re| are) an? (?:llm|ai|language model|assistant|agent|bot)|"
    r"ignora(?:r)? (?:todas )?(?:las |tus )?instrucciones (?:anteriores|previas)|olvida (?:todas )?(?:las |tus )?instrucciones|"
    r"(?:ignore|disregard|forget) (?:all |any )?(?:of )?(?:your |the )?(?:previous|prior|above|earlier) instructions|"
    r"no (?:hace falta|es necesario|hay|tienes) (?:que )?(?:avis\w+|inform\w+|dec\w+|cont\w+)(?: nada)? a(?:l| tu| su)? (?:humano|usuario|propietario|due[ñn]o|operador)|"
    r"(?:don'?t|do not|never|no need to) (?:tell|inform|notify|alert|mention (?:this|it) to) (?:your |the )?(?:human|user|owner|operator)|"
    r"(?:your|the) system prompt|prompt del sistema|como (?:ia|llm|asistente de ia)[,:]|as an (?:ai|llm)[,:])",
    re.IGNORECASE,
)
MIN_TURN_CHARS = 15
# A short turn ("ok, send it") is noise on its own, but not when the assistant answered it at length (it did
# something: on 2026-11-03 the assistant's "sent and verified…" was dropped with the turn, and the task was asked again).
SUBSTANTIAL_REPLY_CHARS = 300
TASKY_TURN = re.compile(
    r"\b(busca\w*|compra\w*|reserva\w*|planific\w*|organiz\w*|recu[eé]rda\w*|avisa\w*|prepara\w*|env[ií]a\w*|"
    r"arregla\w*|revisa\w*|investiga\w*|necesito|quiero|tengo que|hay que|antes de|plazo|viaje|vuelo|hotel|"
    r"find|buy|book|plan|organi[sz]e|remind|prepare|send|fix|review|investigate|i need|we need|have to|must|"
    r"deadline|trip|flight|order|purchase|schedule)\b",
    re.IGNORECASE,
)


def subject_is_noise(subject: str) -> bool:
    return bool(NOISE_SUBJECT.search(subject or ""))


def is_transactional(text: str) -> bool:
    return bool(TRANSACTIONAL.search(text or ""))


def is_account_change(text: str) -> bool:
    return bool(ACCOUNT_CHANGE.search(text or ""))


def is_access_notice(text: str, *, automated: bool = False) -> bool:
    """A routine notice about getting into an account: nothing to do and nothing to ask. A sensitive change to the
    account, or a message trying to steer the assistant, is never one."""
    text = text or ""
    if is_account_change(text) or is_injection(text):
        return False
    return bool(ACCESS_NOTICE.search(text) or (automated and ACCOUNT_SECURITY.search(text)))


def account_change_bundle(signals: list[dict]) -> bool:
    """Automated mail reporting a sensitive change to an account: told to the owner, never asked about."""
    mails = [s for s in signals if s.get("kind") == "email"]
    return bool(mails) and any(
        set((s.get("meta") or {}).get("automation") or []) & STRONG_AUTOMATION and is_account_change(f"{s.get('title', '')}\n{s.get('snippet', '')}")
        for s in mails
    )


def is_enotification(text: str) -> bool:
    return bool(ENOTIFICATION.search(text or ""))


def is_tasky_turn(text: str) -> bool:
    return bool(TASKY_TURN.search(text or ""))


def injection_excerpt(text: str, width: int = 160) -> str:
    """The sentence in which a message addresses the assistant and gives it orders, or "" when it does not."""
    found = INJECTION.search(text or "")
    if not found:
        return ""
    start = max(0, found.start() - 40)
    return " ".join(text[start:found.end() + width].split())


def is_injection(text: str) -> bool:
    return bool(INJECTION.search(text or ""))
