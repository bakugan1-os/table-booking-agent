"""Guardrails enforced in code, not only in the prompt.

The prompt can be talked around; these checks cannot.
"""

from __future__ import annotations

import re

from .config import Facts, human_time

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


# "9 pm", "9:30PM", "9.30 p.m." - matched before the 24h form, or "9:00 pm"
# would be read as nine in the morning.
_AMPM = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*([ap])\.?\s*m\.?\b", re.IGNORECASE)
# "21:00", "09:30"
_24H = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")

_SALUTATION = re.compile(r"^(\s*)good\s+(morning|afternoon|evening|day)\b", re.IGNORECASE)


def _british(hour: int, minute: int) -> str:
    return human_time(f"{hour % 24:02d}:{minute:02d}")


def normalise_times(text: str) -> str:
    """One house style for clock times: 9pm, 5pm, 9.30pm - never 21:00."""

    def from_ampm(match: re.Match) -> str:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3).lower() == "p":
            hour += 12
        return _british(hour, minute)

    def from_24h(match: re.Match) -> str:
        return _british(int(match.group(1)), int(match.group(2)))

    return _24H.sub(from_24h, _AMPM.sub(from_ampm, text))


def fix_salutation(text: str, word: str) -> str:
    """'Good morning' sent at 9pm is the kind of slip a guest would notice."""
    return _SALUTATION.sub(lambda m: f"{m.group(1)}Good {word}", text, count=1)


def sanitize(reply: str, *, allow_phone: bool, greeting: str | None = None) -> str:
    """Strip links, markup and emoji; fix the time and the salutation; cap length."""
    text = _URL.sub("", reply or "")
    text = _EMOJI.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = normalise_times(text)
    if greeting:
        text = fix_salutation(text, greeting)
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
