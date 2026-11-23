"""Guardrails enforced in code, not only in the prompt.

The prompt can be talked around; these checks cannot.
"""

from __future__ import annotations

import re

from .config import Facts

MAX_SENTENCES = 3
MAX_CHARS = 300

# Escalate on money talk only (narrow on purpose: the LLM's requested_change
# field carries the general case).
_PAYMENT = re.compile(r"\b(deposit|pre-?pay\w*|card|payment)\b", re.IGNORECASE)

# Things we never hand over, whatever the reason given.
_SECRETS = re.compile(
    r"\b(card (?:number|details|digits)|credit card|debit card|cvv|cvc|expiry"
    r"|password|passcode|one[- ]time code|verification code|otp|iban|sort code"
    r"|account number)\b",
    re.IGNORECASE,
)

_PHONE_REQUEST = re.compile(
    r"\b(phone|mobile|telephone|contact number|contact details|number to call"
    r"|number to reach|reach you on|call you on)\b",
    re.IGNORECASE,
)

_BOT_QUESTION = re.compile(
    r"\b(are you (?:a )?(?:bot|robot|human|real|ai|machine)|is this (?:a )?bot"
    r"|am i (?:talking|speaking) to (?:a )?(?:bot|human|person|machine))\b",
    re.IGNORECASE,
)

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MARKDOWN = re.compile(r"[*_`~#>\[\]|]")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff←-⇿⌀-➿⬀-⯿️‍]"
)
# A run of six or more digits/separators: a phone number, a card, an account.
_LONG_DIGITS = re.compile(r"\d[\d\s().-]{5,}")


def mentions_payment(text: str) -> bool:
    return bool(_PAYMENT.search(text or ""))


def asks_for_secrets(text: str) -> bool:
    return bool(_SECRETS.search(text or ""))


def wants_phone(text: str) -> bool:
    return bool(_PHONE_REQUEST.search(text or ""))


def asks_if_bot(text: str) -> bool:
    """Backstop for the LLM's asked_if_bot field."""
    return bool(_BOT_QUESTION.search(text or ""))


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def sanitize(reply: str, *, allow_phone: bool) -> str:
    """Strip links, markup and emoji; drop numbers we were not asked for; cap length."""
    text = _URL.sub("", reply or "")
    text = _EMOJI.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()

    parts = _sentences(text)
    if not allow_phone:
        parts = [s for s in parts if not _LONG_DIGITS.search(s)]
    parts = parts[:MAX_SENTENCES]

    out = " ".join(parts).strip()
    if len(out) <= MAX_CHARS:
        return out

    kept: list[str] = []
    for sentence in parts:
        if len(" ".join(kept + [sentence])) > MAX_CHARS:
            break
        kept.append(sentence)
    if kept:
        return " ".join(kept)
    return out[:MAX_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "."


# --- canned lines: the code owns these, the model never writes them ----------

def holding_line() -> str:
    return "Let me check that with the guest and I will come straight back to you."


def off_topic_line(facts: Facts) -> str:
    return (
        "I'm afraid I can only help with the reservation - shall we confirm the table "
        f"for {facts.time_human}?"
    )


def bot_disclosure_line(facts: Facts) -> str:
    return (
        f"To be straight with you, I'm an AI assistant arranging this on behalf of "
        f"{facts.guest_name}. Happy to carry on either way - may we settle the table "
        f"for {facts.time_human}?"
    )


def reminder_line(facts: Facts) -> str:
    return (
        f"Apologies for chasing - have you had a chance to look at a table for "
        f"{facts.time_human}?"
    )


def recap_line(facts: Facts, restaurant_name: str | None) -> str:
    """Deterministic confirmation: time, date, party size, restaurant."""
    where = f" at {restaurant_name}" if restaurant_name else ""
    return (
        "Lovely, thank you very much. Just to confirm: a table for "
        f"{facts.party_human} at {facts.time_human} on {facts.date_human}{where}, "
        f"under the name {facts.guest_name}. We look forward to it."
    )
