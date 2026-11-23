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

# "Can you type me the number?" - and then everything stays in writing.
_WANTS_WRITING = re.compile(
    r"\b(type|typed|typing|text me|in text|by text|write it|written|spell"
    r"|in writing"
    r"|напиши|текстом"
    r"|письмово)\b",
    re.IGNORECASE,
)
# Claims that only make sense in a written message.
_CLAIMS_WRITING = re.compile(
    r"\b(typed|written|in text|in writing|spelled out|here it is in)\b", re.IGNORECASE
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")

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


def wants_writing(text: str) -> bool:
    """The manager has asked for something in writing, not spoken."""
    return bool(_WANTS_WRITING.search(text or ""))


def unspeakable(text: str) -> str | None:
    """Why this message must be written, or None if it can be spoken.

    A phone number read aloud is useless, and a voice note saying "typed out
    for you" is a lie - the live run sent both.
    """
    body = text or ""
    if _LONG_DIGITS.search(body):
        return "it contains a number to write down"
    if _EMAIL.search(body):
        return "it contains an email address"
    if _CLAIMS_WRITING.search(body):
        return "it claims to be written"
    return None


def is_affirmative(text: str) -> bool:
    """A plain yes, in any of the three languages the operator writes in."""
    return operator_verdict(text) == "yes"


def ask_typed_name_line() -> str:
    """Asked of the manager when the name is still only something we heard."""
    return (
        "Lovely - could you type the restaurant's name for me so I have it "
        "exactly right?"
    )


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


def normalise_times(text: str, booking: str | None = None) -> str:
    """One house style for clock times: 9pm, 5pm, 9.30pm - never 21:00.

    `booking` is the facts' own time ("21:00"). A bare "9:30" with no am/pm
    and no leading zero is read as the booking's half of the day, so an
    evening table never comes out as 9.30am.
    """
    booked_hour = booked_minute = None
    if booking:
        booked_hour, booked_minute = (int(part) for part in booking.split(":"))

    def from_ampm(match: re.Match) -> str:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3).lower() == "p":
            hour += 12
        return _british(hour, minute)

    def from_24h(match: re.Match) -> str:
        raw_hour, hour, minute = match.group(1), int(match.group(1)), int(match.group(2))
        ambiguous = hour <= 12 and not raw_hour.startswith("0")
        if (
            ambiguous
            and booked_hour is not None
            and (hour % 12, minute) == (booked_hour % 12, booked_minute)
        ):
            hour = booked_hour
        return _british(hour, minute)

    return _24H.sub(from_24h, _AMPM.sub(from_ampm, text))


# "under Test Guest's name" reads like a form; "under the name Test Guest"
# is how a person says it.
_POSSESSIVE = re.compile(
    r"\b(?:under|in)\s+((?:[A-Z][\w.-]*)(?:\s+[A-Z][\w.-]*)*)['’]s\s+name\b"
)


def fix_possessive(text: str) -> str:
    return _POSSESSIVE.sub(lambda m: f"under the name {m.group(1)}", text)


# Phrases that put the agent on the wrong side of the conversation. The
# agent is the one who went away to check, so it is the manager who waited.
_ROLE_SLIPS = (
    (
        re.compile(
            r"\b(?:thanks?|thank you)(?: so much| very much)? for (?:coming|getting) back to me\b",
            re.IGNORECASE,
        ),
        "thank you for waiting",
    ),
    (
        re.compile(r"\bwe look forward to seeing you\b", re.IGNORECASE),
        "we're looking forward to it",
    ),
)


def fix_role_phrases(text: str) -> str:
    body = text or ""
    for pattern, replacement in _ROLE_SLIPS:
        def swap(match: re.Match, replacement: str = replacement) -> str:
            before = body[: match.start()].rstrip()
            starts_sentence = not before or before[-1] in ".!?"
            return replacement.capitalize() if starts_sentence else replacement

        body = pattern.sub(swap, body)
    return body


def fix_salutation(text: str, word: str) -> str:
    """'Good morning' sent at 9pm is the kind of slip a guest would notice."""
    return _SALUTATION.sub(lambda m: f"{m.group(1)}Good {word}", text, count=1)


def sanitize(
    reply: str,
    *,
    allow_phone: bool,
    greeting: str | None = None,
    booking: str | None = None,
) -> str:
    """Strip links, markup and emoji; fix the time and the salutation; cap length."""
    text = _URL.sub("", reply or "")
    text = _EMOJI.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = normalise_times(text, booking)
    text = fix_possessive(text)
    text = fix_role_phrases(text)
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

def verify_restaurant_line(name: str) -> str:
    """A name heard over voice, checked in writing - Whisper mishears names."""
    return (
        f"Sorry, just to make sure I have it right - is that {name}? "
        "Could you type it for me, please?"
    )


def voice_unreadable_line() -> str:
    """When a voice note cannot be transcribed - asked in text, on purpose."""
    return (
        "I'm terribly sorry, your voice note didn't come through clearly at my "
        "end. Would you mind typing it?"
    )


def holding_line() -> str:
    return "Let me check that with the guest and I will come straight back to you."


_OFF_TOPIC = (
    "I'm afraid I can only help with the reservation - shall we confirm the table "
    "for {time}?",
    "That's rather outside my remit, I'm afraid. Could we settle the table for "
    "{time} first?",
    "I'd better stick to the booking, if you don't mind - are we all right for "
    "{time}?",
)


def off_topic_line(facts: Facts, seen: int = 0) -> str:
    """Three ways of saying no, so a persistent manager is not met with a parrot."""
    return _OFF_TOPIC[seen % len(_OFF_TOPIC)].format(time=facts.time_human)


def bot_disclosure_line(facts: Facts) -> str:
    return (
        f"To be straight with you, I'm an AI assistant arranging this on behalf of "
        f"{facts.guest_name}. Happy to carry on either way - are we all right for "
        f"{facts.time_human}?"
    )


def reminder_line(facts: Facts) -> str:
    return (
        f"Apologies for chasing - have you had a chance to look at a table for "
        f"{facts.time_human}?"
    )


def recap_line(booking: Facts) -> str:
    """The confirmation, built from the agreed booking - the model never writes it.

    Five fields, every time: party size, time, date, restaurant, guest name.
    Written as the guest's side: a restaurant looks forward to *seeing you*,
    a guest looks forward *to it*.
    """
    when = (
        f"{booking.time_human} today, {booking.date_human}"
        if booking.is_today()
        else f"{booking.time_human} on {booking.date_human}"
    )
    where = f", at {booking.restaurant_name}" if booking.restaurant_name else ""
    return (
        f"Wonderful, thank you. Just to confirm: a table for {booking.party_word} "
        f"at {when}{where}, under the name {booking.guest_name}. "
        "Thanks so much - we're looking forward to it."
    )


def recap_missing(text: str, booking: Facts) -> list[str]:
    """Which of the five fields did not make it into the message that went out."""
    expected = {
        "party_size": booking.party_word,
        "time": booking.time_human,
        "date": booking.date_human,
        "restaurant": booking.restaurant_name or "",
        "guest_name": booking.guest_name,
    }
    return [field for field, value in expected.items() if value and value not in text]


# The operator answers in whatever language is to hand - the first dry run
# with an escalation was answered "так", which no English word list catches.
_APPROVES = re.compile(
    r"\b(yes|yep|yeah|ok|okay|agreed?|approved?|go ahead|fine|do it|sure"
    r"|accept|book it|confirm it|confirmed"
    r"|так|да|ага|ок|окей"
    r"|гаразд|добре|згоде?н"
    r"|згодна|згода|підтверджую"
    r"|давай|звісно|авжеж"
    r"|хорошо|подтверждаю|согласен)\b",
    re.IGNORECASE,
)
_REJECTS = re.compile(
    r"\b(no|nope|don't|do not|decline|refuse|reject|cancel|leave it"
    r"|ні|нє|нет"
    r"|не треба|не згоден"
    r"|не погоджуюсь"
    r"|відмов|скасуй"
    r"|не надо|не согласен)\b",
    re.IGNORECASE,
)


def operator_verdict(text: str) -> str:
    """'yes', 'no' or 'unclear'. Anything unclear is asked again, never assumed."""
    body = text or ""
    if _REJECTS.search(body):
        return "no"
    if _APPROVES.search(body):
        return "yes"
    return "unclear"


def operator_approves(text: str) -> bool:
    return operator_verdict(text) == "yes"


def parse_clock(value: str) -> str | None:
    """'9.30pm', '9:30 pm', '21:30' -> '21:30'. None if there is no time in it."""
    match = _AMPM.search(value or "")
    if match:
        hour = int(match.group(1)) % 12
        minute = int(match.group(2) or 0)
        if match.group(3).lower() == "p":
            hour += 12
        return f"{hour:02d}:{minute:02d}"
    match = _24H.search(value or "")
    if match:
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
    return None


_BRITISH_TIME = re.compile(r"\b(\d{1,2})(?:\.(\d{2}))?(am|pm)\b", re.IGNORECASE)


def conflicting_values(text: str, booking: Facts) -> list[str]:
    """Times or party sizes in `text` that are not what has been agreed.

    Used both ways: on our own draft, so the agent never offers the manager a
    time the operator has not approved, and on the manager's message, so a
    recap never confirms something they did not say.
    """
    conflicts: list[str] = []
    body = normalise_times(text or "", booking.time)

    agreed_time = booking.time_human.lower()
    for match in _BRITISH_TIME.finditer(body):
        found = match.group(0).lower()
        if found != agreed_time:
            conflicts.append(f"time {found}, agreed {agreed_time}")

    party = re.search(r"\btable for ([\w-]+)", body, re.IGNORECASE) or re.search(
        r"\bfor ([\w-]+) (?:people|guests|of us)\b", body, re.IGNORECASE
    )
    if party:
        size = parse_party_size(party.group(1))
        if size is not None and size != booking.party_size:
            conflicts.append(f"party size {size}, agreed {booking.party_size}")

    return sorted(set(conflicts))


def clarify_line(topic: str, value: str | None) -> str:
    """Asked in Saved Messages when the operator's answer could go either way."""
    pretty = value or topic
    if topic == "time":
        parsed = parse_clock(value or "")
        if parsed:
            pretty = human_time(parsed)
    elif topic == "party_size":
        size = parse_party_size(value or "")
        if size is not None:
            pretty = f"a table for {size}"
    return f"Sorry, is that a yes to {pretty}? Please reply yes or no."


def parse_party_size(value: str) -> int | None:
    match = re.search(r"\b(\d{1,2})\b", value or "")
    if match:
        size = int(match.group(1))
        return size if 1 <= size <= 30 else None
    for word, size in {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }.items():
        if re.search(rf"\b{word}\b", value or "", re.IGNORECASE):
            return size
    return None


# Markers that a booking was actually made, used only to flag a disagreement
# with the model - never to overrule it.
_CONFIRMATION = re.compile(
    # "fully booked" and "booked up" mean the opposite of a booking.
    r"\b(reserved|(?<!fully )(?<!not )(?<!no )booked(?! up)"
    r"|booking is (?:made|confirmed)|confirmed|that's done"
    r"|that is done|all set|see you then|see you (?:at|on))\b",
    re.IGNORECASE,
)


def confirmation_markers(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _CONFIRMATION.finditer(text or "")})
