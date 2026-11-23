"""Unit checks: facts loading, the time guard, guardrails, state, JSON contract.

Plain script, no pytest:

    python tests/test_units.py
"""

from __future__ import annotations

import inspect
import re
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import guardrails  # noqa: E402
from agent.config import (  # noqa: E402
    ConfigError,
    Facts,
    greeting_word,
    load_facts,
    start_guard,
)
from agent.llm import _extract_json, _validate  # noqa: E402
from agent.prompts import OPEN_CONVERSATION, build_system_prompt  # noqa: E402
from agent.state import Conversation  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="booking-tests-"))
ok: list[str] = []
bad: list[str] = []


def check(name: str, cond: bool) -> None:
    (ok if cond else bad).append(name)


def write_facts(name: str, body: str) -> Path:
    path = TMP / name
    path.write_text(body, encoding="utf-8")
    return path


BASE = 'party_size: 2\ntime: "17:00"\ndate: today\ntimezone: Europe/London\n'

# --- 1. the guest's details come from .env when the file omits them ---------

no_guest = write_facts("no_guest.yaml", BASE + "restaurant_name: null\n")
try:
    load_facts(no_guest)
    check("missing guest details refused", False)
except ConfigError as exc:
    check("missing guest details refused", "GUEST_NAME" in str(exc))

facts = load_facts(no_guest, guest_name="Oliver Hart", guest_phone="+44 7700 900123")
check("guest name from .env", facts.guest_name == "Oliver Hart")
check("guest phone from .env", facts.guest_phone == "+44 7700 900123")

in_file = write_facts(
    "in_file.yaml",
    BASE + 'guest_name: "File Name"\nguest_phone: "+44 7700 900999"\nrestaurant_name: null\n',
)
both = load_facts(in_file, guest_name="Env Name", guest_phone="+44 7700 900123")
check("facts file wins over .env", both.guest_name == "File Name")

placeholder = write_facts(
    "placeholder.yaml", BASE + 'guest_name: "CHANGE ME"\nrestaurant_name: null\n'
)
fallback = load_facts(placeholder, guest_name="Env Name", guest_phone="+44 7700 900123")
check("placeholder falls through to .env", fallback.guest_name == "Env Name")

try:
    load_facts(placeholder, guest_phone="+44 7700 900123")
    check("placeholder alone is not enough", False)
except ConfigError as exc:
    check("placeholder alone is not enough", "guest_name" in str(exc))

# the files in the repo
repo_facts = load_facts(
    ROOT / "facts.yaml", guest_name="Oliver Hart", guest_phone="+44 7700 900123"
)
check("repo facts.yaml loads with .env fallbacks", repo_facts.time == "17:00")
test_facts = load_facts(ROOT / "facts.test.yaml")
check("facts.test.yaml needs no .env", test_facts.guest_name == "Test Guest")
check("facts.test.yaml is at 9pm", test_facts.time_human == "9pm")
try:
    load_facts(TMP / "nope.yaml")
    check("missing file named in the error", False)
except ConfigError as exc:
    check("missing file named in the error", "nope.yaml" in str(exc))

# --- 2. derived values ------------------------------------------------------

check("party_human", facts.party_human == "two people")
check("time_human 17:00 -> 5pm", facts.time_human == "5pm")
check(
    "time_human 17:30 -> 5.30pm",
    Facts(2, "17:30", facts.date, facts.timezone, "a", "b", None).time_human == "5.30pm",
)
check("date resolved to today", facts.date == datetime.now(ZoneInfo("Europe/London")).date())
check("restaurant unknown", facts.restaurant_name is None)
print("date_human:", facts.date_human, "| booking_at:", facts.booking_at.isoformat())

bad_time = write_facts("bad_time.yaml", 'party_size: 2\ntime: "25:00"\n')
try:
    load_facts(bad_time, guest_name="a", guest_phone="b")
    check("impossible time refused", False)
except ConfigError as exc:
    check("impossible time refused", "not a real time" in str(exc))

bad_date = write_facts("bad_date.yaml", BASE.replace("today", "someday"))
try:
    load_facts(bad_date, guest_name="a", guest_phone="b")
    check("bad date refused", False)
except ConfigError as exc:
    check("bad date refused", "ISO date" in str(exc))

# --- 3. the time guard ------------------------------------------------------

early = facts.booking_at - timedelta(hours=3)
late = facts.booking_at - timedelta(minutes=40)
check("guard allows 3h before", start_guard(facts, early) is None)
check("guard blocks 40min before", start_guard(facts, late) is not None)
check(
    "guard blocks after the booking",
    start_guard(facts, facts.booking_at + timedelta(minutes=1)) is not None,
)
check("guard names the facts file", "facts.test.yaml" in (start_guard(facts, late, "facts.test.yaml") or ""))

# --- 4. guardrails ----------------------------------------------------------

long_reply = (
    "**Hello** there \U0001f600 see https://example.com/menu for details. "
    "We would like a table. Our number is +44 7700 900123. "
    "A fourth sentence that should be cut. A fifth one too."
)
clean = guardrails.sanitize(long_reply, allow_phone=False)
check("markdown stripped", "*" not in clean)
check("emoji stripped", "\U0001f600" not in clean)
check("url stripped", "example.com" not in clean)
check("phone dropped when not asked", "900123" not in clean)
check("max 3 sentences", len([s for s in clean.split(". ") if s]) <= 3)
check("length capped", len(clean) <= guardrails.MAX_CHARS)
check(
    "phone kept when asked",
    "900123" in guardrails.sanitize("Our number is +44 7700 900123.", allow_phone=True),
)
print("sanitized:", clean)

booked = replace(facts, restaurant_name="The Ivy, Covent Garden")
recap = guardrails.recap_line(booked)
check("recap has party size", "two" in recap)
check("recap has the time", facts.time_human in recap)
check("recap has the date", facts.date_human in recap)
check("recap names the restaurant", "The Ivy, Covent Garden" in recap)
check("recap has the guest name", facts.guest_name in recap)
check("recap says today when it is today", "today" in recap)
check("recap says 'under the name'", f"under the name {facts.guest_name}" in recap)
check("recap <= 3 sentences", recap.count(". ") <= 2)
check("recap is complete", guardrails.recap_missing(recap, booked) == [])
check(
    "recap_missing names what is absent",
    set(guardrails.recap_missing("Thank you, looking forward to it.", booked))
    == {"party_size", "time", "date", "restaurant", "guest_name"},
)
check(
    "recap_missing spots one gap",
    guardrails.recap_missing(recap, replace(booked, restaurant_name="Some Other Place"))
    == ["restaurant"],
)
check("recap survives an unknown name", "at " in guardrails.recap_line(replace(facts, restaurant_name=None)))
# the agent speaks for the guest, not for the restaurant
check("recap is in the guest's voice", "we're looking forward to it" in recap.lower())
check("recap does not greet as the restaurant", "seeing you" not in recap.lower())
check("'many thanks' is gone", "many thanks" not in recap.lower())
check(
    "recap follows the agreed time, not the facts file",
    "9.30pm" in guardrails.recap_line(replace(booked, time="21:30")),
)
print("recap:", recap)

# markers of an actual booking: logged as a disagreement, never acted on
check("marker: reserved", guardrails.confirmation_markers("that's reserved under your name") == ["reserved"])
check("marker: booked", "booked" in guardrails.confirmation_markers("All booked!"))
# "fully booked" is the opposite of a booking
check("not a marker: fully booked", guardrails.confirmation_markers("9pm is fully booked") == [])
check("not a marker: booked up", guardrails.confirmation_markers("we are booked up") == [])
check("not a marker: not booked", guardrails.confirmation_markers("it is not booked yet") == [])
check(
    "fully booked plus a real one still counts",
    guardrails.confirmation_markers("9pm is fully booked, but 9.30pm is reserved for you")
    == ["reserved"],
)
check("marker: see you then", "see you then" in guardrails.confirmation_markers("Lovely, see you then."))
check("marker: confirmed", "confirmed" in guardrails.confirmation_markers("You are confirmed."))
check("no marker on availability", guardrails.confirmation_markers("Yes, we have a table for two at 9pm.") == [])
check("no marker on a question", guardrails.confirmation_markers("How many guests?") == [])

# "under X's name" -> "under the name X"
check(
    "possessive rewritten",
    guardrails.fix_possessive("Please reserve it under Test Guest's name.")
    == "Please reserve it under the name Test Guest.",
)
check(
    "curly apostrophe too",
    guardrails.fix_possessive("confirm it under Test Guest’s name?")
    == "confirm it under the name Test Guest?",
)
check(
    "'in X's name' too",
    "under the name Oliver Hart" in guardrails.fix_possessive("booked in Oliver Hart's name"),
)
check(
    "plain sentences untouched",
    guardrails.fix_possessive("under the name Test Guest") == "under the name Test Guest",
)

# the salutation follows London's clock, not the model's mood
LONDON = ZoneInfo("Europe/London")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 23, hour, minute, tzinfo=LONDON)


check("00:00 is morning", greeting_word(LONDON, at(0)) == "morning")
check("11:59 is morning", greeting_word(LONDON, at(11, 59)) == "morning")
check("12:00 is afternoon", greeting_word(LONDON, at(12)) == "afternoon")
check("17:59 is afternoon", greeting_word(LONDON, at(17, 59)) == "afternoon")
check("18:00 is evening", greeting_word(LONDON, at(18)) == "evening")
check("23:59 is evening", greeting_word(LONDON, at(23, 59)) == "evening")
check("greeting defaults to now", greeting_word(LONDON) in ("morning", "afternoon", "evening"))

check(
    "wrong salutation corrected",
    guardrails.sanitize("Good morning, might you have a table?", allow_phone=False, greeting="evening")
    .startswith("Good evening,"),
)
check(
    "right salutation left alone",
    guardrails.sanitize("Good evening, might you have a table?", allow_phone=False, greeting="evening")
    .startswith("Good evening,"),
)
check(
    "no salutation, nothing invented",
    not guardrails.sanitize("Might you have a table?", allow_phone=False, greeting="evening").startswith("Good"),
)
check(
    "salutation only at the start",
    "good afternoon" in guardrails.sanitize(
        "Lovely. I said good afternoon earlier.", allow_phone=False, greeting="evening"
    ),
)

# one house style for clock times
check("24h becomes British", guardrails.normalise_times("at 21:00 please") == "at 9pm please")
check("24h with minutes", guardrails.normalise_times("at 21:30") == "at 9.30pm")
check("morning 24h", guardrails.normalise_times("at 09:00") == "at 9am")
check("spaced am/pm closed up", guardrails.normalise_times("at 9 pm") == "at 9pm")
check("uppercase am/pm", guardrails.normalise_times("at 9 PM") == "at 9pm")
# "a.m." keeps its final dot on purpose: swallowing it would merge sentences.
check("dotted a.m.", guardrails.normalise_times("at 11 a.m.") == "at 11am.")
check("9:00 pm is not 9am", guardrails.normalise_times("at 9:00 pm") == "at 9pm")
check("half past kept", guardrails.normalise_times("at 7:30pm") == "at 7.30pm")
check("midnight", guardrails.normalise_times("at 00:30") == "at 12.30am")
check("dates untouched", guardrails.normalise_times("on Monday 23 November") == "on Monday 23 November")
# a bare "9:30" for an evening booking must not become half past nine in the morning
check("bare time follows the booking", guardrails.normalise_times("at 9:30", "21:30") == "at 9.30pm")
check("bare time, exact hour", guardrails.normalise_times("at 9:00", "21:00") == "at 9pm")
check("leading zero stays morning", guardrails.normalise_times("at 09:00", "21:00") == "at 9am")
check("a different time is literal", guardrails.normalise_times("at 8:00", "21:00") == "at 8am")
check("no hint, no guessing", guardrails.normalise_times("at 9:30") == "at 9.30am")
check("party size untouched", guardrails.normalise_times("a table for 2") == "a table for 2")
check(
    "sanitize normalises the wire",
    "9pm" in guardrails.sanitize("Could you confirm a table at 21:00?", allow_phone=False),
)
check(
    "no 24h time survives",
    "21:00" not in guardrails.sanitize("Could you confirm a table at 21:00?", allow_phone=False),
)

check("payment: deposit", guardrails.mentions_payment("we need a 10 pound deposit"))
check("payment: prepay", guardrails.mentions_payment("could you pre-pay please"))
check("payment: card", guardrails.mentions_payment("we hold a card"))
check("payment: not a plain yes", not guardrails.mentions_payment("Yes, 5pm is fine for two"))
check("secrets: card number", guardrails.asks_for_secrets("send the card number and cvv"))
check("secrets: not dress code", not guardrails.asks_for_secrets("the dress code is smart casual"))
check("phone wanted", guardrails.wants_phone("what is your contact number?"))
check("phone not wanted", not guardrails.wants_phone("how many guests?"))
check("bot question", guardrails.asks_if_bot("are you a bot?"))
check("bot question 2", guardrails.asks_if_bot("am i talking to a human"))
check("not a bot question", not guardrails.asks_if_bot("is the table by the window"))
check("off-topic line mentions time", "5pm" in guardrails.off_topic_line(facts))
check(
    "off-topic refusals vary",
    len({guardrails.off_topic_line(facts, n) for n in range(3)}) == 3,
)
check("off-topic refusals cycle", guardrails.off_topic_line(facts, 3) == guardrails.off_topic_line(facts, 0))
check(
    "every refusal names the time",
    all("5pm" in guardrails.off_topic_line(facts, n) for n in range(3)),
)
check("'may we settle' is gone", "may we settle" not in guardrails.bot_disclosure_line(facts).lower())
check("disclosure is honest", "AI assistant" in guardrails.bot_disclosure_line(facts))

# none of the fixed lines may speak as though we worked at the restaurant
RESTAURANT_VOICE = ("seeing you", "we have a table", "our kitchen", "our tables", "your table is ready")
for label, line in (
    ("holding", guardrails.holding_line()),
    ("off topic", guardrails.off_topic_line(facts)),
    ("disclosure", guardrails.bot_disclosure_line(facts)),
    ("reminder", guardrails.reminder_line(facts)),
    ("recap", recap),
):
    check(
        f"{label} line stays on the guest's side",
        not any(phrase in line.lower() for phrase in RESTAURANT_VOICE),
    )

# --- 5. state machine -------------------------------------------------------

c = Conversation()
check("greeting -> requesting", c.next_state("requesting", restaurant_known=False) == "requesting")
c.state = "requesting"
check("no backwards", c.next_state("greeting", restaurant_known=True) == "requesting")
check(
    "confirmed blocked without a name",
    c.next_state("confirmed", restaurant_known=False, manager_confirmed=True) == "negotiating",
)
check(
    "a free table is not a booking",
    c.next_state("confirmed", restaurant_known=True, manager_confirmed=False) == "negotiating",
)
check(
    "confirmed needs both",
    c.next_state("confirmed", restaurant_known=True, manager_confirmed=True) == "confirmed",
)
check(
    "manager_confirmed defaults to false",
    c.next_state("confirmed", restaurant_known=True) == "negotiating",
)
# the dry-run bug: the model jumped straight to done and the recap was lost
check(
    "a booking routes through confirmed",
    c.next_state("done", restaurant_known=True, manager_confirmed=True) == "confirmed",
)
c.state = "negotiating"
check(
    "from negotiating too",
    c.next_state("done", restaurant_known=True, manager_confirmed=True) == "confirmed",
)
check(
    "no shortcut without the restaurant",
    c.next_state("done", restaurant_known=False, manager_confirmed=True) == "negotiating",
)
c.state = "requesting"
check("done blocked outside confirmed", c.next_state("done", restaurant_known=True) == "requesting")
c.state = "confirmed"
check("done allowed from confirmed", c.next_state("done", restaurant_known=True) == "done")
check("garbage state ignored", c.next_state("banana", restaurant_known=True) == "confirmed")

# --- 5b. the agreed booking -------------------------------------------------

check("approval: yes", guardrails.operator_verdict("yes, go ahead") == "yes")
check("approval: ok", guardrails.operator_verdict("ok") == "yes")
check("approval: agreed", guardrails.operator_verdict("Agreed, book it.") == "yes")
# the dry run was answered in Ukrainian and the change silently did not land
check("approval: tak", guardrails.operator_verdict("так") == "yes")
check("approval: dobre", guardrails.operator_verdict("добре, давай") == "yes")
check("rejection: no", guardrails.operator_verdict("no, keep 9pm") == "no")
check("rejection: ni", guardrails.operator_verdict("ні") == "no")
check("rejection wins over yes", guardrails.operator_verdict("yes but no, decline it") == "no")
check("silence is unclear", guardrails.operator_verdict("hmm") == "unclear")
check("a question is unclear", guardrails.operator_verdict("what did they say?") == "unclear")
check("maybe is unclear", guardrails.operator_verdict("можливо") == "unclear")
check("approves wrapper still works", guardrails.operator_approves("yes"))
check("clarify names the time", guardrails.clarify_line("time", "9.30pm") == "Sorry, is that a yes to 9.30pm? Please reply yes or no.")
check("clarify normalises 24h", "9.30pm" in guardrails.clarify_line("time", "21:30"))
check("clarify handles party size", "a table for 4" in guardrails.clarify_line("party_size", "4"))

check("clock: 24h", guardrails.parse_clock("21:30") == "21:30")
check("clock: british", guardrails.parse_clock("9.30pm") == "21:30")
check("clock: spaced", guardrails.parse_clock("9:30 pm") == "21:30")
check("clock: on the hour", guardrails.parse_clock("8pm") == "20:00")
check("clock: morning", guardrails.parse_clock("11am") == "11:00")
check("clock: nothing there", guardrails.parse_clock("sometime later") is None)
check("party: digits", guardrails.parse_party_size("4") == 4)
check("party: words", guardrails.parse_party_size("four guests") == 4)
check("party: nonsense", guardrails.parse_party_size("a few") is None)

# nothing may be offered to the manager that is not in the agreed booking
at_nine = replace(facts, time="21:00", restaurant_name="The Ivy")
check("agreed time passes", guardrails.conflicting_values("a table at 9pm?", at_nine) == [])
check(
    "a different time is caught",
    guardrails.conflicting_values("could we do 9.30pm?", at_nine)
    == ["time 9.30pm, agreed 9pm"],
)
check(
    "24h form is caught too",
    guardrails.conflicting_values("confirm 21:30 please", at_nine)
    == ["time 9.30pm, agreed 9pm"],
)
check("agreed party passes", guardrails.conflicting_values("a table for two", at_nine) == [])
check(
    "a different party is caught",
    guardrails.conflicting_values("a table for four", at_nine)
    == ["party size 4, agreed 2"],
)
check("dates are not times", guardrails.conflicting_values("on Monday 23 November", at_nine) == [])
check("no numbers, no conflict", guardrails.conflicting_values("Thank you very much.", at_nine) == [])
check(
    "both can clash at once",
    len(guardrails.conflicting_values("a table for four at 10pm", at_nine)) == 2,
)

agreed = Conversation()
agreed.ensure_agreed(facts)
check("agreed seeded from facts", agreed.agreed["time"] == facts.time)
check("agreed seeds the guest name", agreed.agreed["guest_name"] == facts.guest_name)
check("agreed starts without a restaurant", agreed.restaurant_name is None)
agreed.restaurant_name = "The Ivy"
check("restaurant lands in agreed", agreed.agreed["restaurant_name"] == "The Ivy")
before, after = agreed.agree("time", "21:30")
check("agree returns the move", (before, after) == (facts.time, "21:30"))
moved = agreed.booking(facts)
check("booking follows the agreed time", moved.time_human == "9.30pm")
check("booking keeps the rest", moved.party_size == facts.party_size and moved.guest_name == facts.guest_name)
check("facts themselves are untouched", facts.time == "17:00")
agreed.agree("party_size", 4)
check("booking follows the agreed party", agreed.booking(facts).party_word == "four")
roundtrip = TMP / "agreed.json"
agreed.save(roundtrip)
restored = Conversation.load(roundtrip)
check("agreed survives a restart", restored is not None and restored.agreed["time"] == "21:30")
check("restaurant survives a restart", restored is not None and restored.restaurant_name == "The Ivy")

# --- 6. persistence ---------------------------------------------------------

c2 = Conversation()
c2.add("manager", "Hello")
c2.add("agent", "Good afternoon")
c2.restaurant_name = "The Ivy"
c2.pending_operator = True
c2.pending_topic = "deposit"
c2.agent_saved_ids.append(42)
path = TMP / "state_test.json"
c2.save(path)
back = Conversation.load(path)
check(
    "state round trip",
    back.restaurant_name == "The Ivy" and back.pending_operator and back.agent_saved_ids == [42],
)
check("history round trip", len(back.history) == 2 and back.last_manager_text() == "Hello")
check("last_role", back.last_role() == "agent")
check("no state file -> None", Conversation.load(TMP / "missing.json") is None)
broken = TMP / "broken.json"
broken.write_text("{not json", encoding="utf-8")
check("corrupt state file -> None", Conversation.load(broken) is None)

# --- 7. the JSON contract ---------------------------------------------------

good = """Sure! Here you go:
```json
{"reply": "Good afternoon.", "understood": "asked for a deposit", "decision": "escalate",
 "next_state": "negotiating", "needs_operator": "true", "off_topic": false,
 "requested_change": "deposit", "asked_if_bot": false, "restaurant_name": "null"}
```"""
d, problem = _validate(_extract_json(good))
check("json parsed from fenced chatter", d is not None)
check("manager_confirmed defaults false", d is not None and d.manager_confirmed is False)
d_ok, _ = _validate(
    _extract_json('{"reply": "hi", "next_state": "confirmed", "manager_confirmed": true}')
)
check("manager_confirmed parsed", d_ok is not None and d_ok.manager_confirmed is True)
check("bool coerced from string", d is not None and d.needs_operator is True)
check("'null' name -> None", d is not None and d.restaurant_name is None)
check("change kept", d is not None and d.requested_change == "deposit")
d2, p2 = _validate(_extract_json('{"reply": "hi", "next_state": "banana"}'))
check("bad next_state rejected", d2 is None and "next_state" in p2)
d3, _ = _validate(_extract_json("no json at all"))
check("missing json rejected", d3 is None)
d4, _ = _validate(_extract_json('{"reply": "", "next_state": "done"}'))
check("empty reply rejected", d4 is None)
d5, _ = _validate(_extract_json('{"reply": "hi", "next_state": "done", "requested_change": "pony"}'))
check("unknown change -> none", d5 is not None and d5.requested_change == "none")

# --- 7b. the request the model actually receives ----------------------------
#
# gpt-oss on Groq spent its 500 tokens reasoning and returned "" twice,
# which turned a question about the date into an escalation.

from types import SimpleNamespace  # noqa: E402

from agent.config import Settings  # noqa: E402
from agent.journal import Journal  # noqa: E402
from agent.llm import Brain  # noqa: E402


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))]
        )


def make_brain(script):
    brain = Brain(
        Settings(
            api_id=1, api_hash="h", manager_username="m",
            llm_base_url="http://example.invalid", llm_api_key="k", llm_model="model",
        ),
        Journal(TMP / "logs" / "units.jsonl"),
    )
    fake = FakeCompletions(script)
    brain._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return brain, fake


REPLY = '{"reply": "hello", "next_state": "requesting"}'
brain, fake = make_brain([REPLY])
check("a decision comes back", brain.decide("system", []) is not None)
sent = fake.calls[0]
check("json mode asked for", sent.get("response_format") == {"type": "json_object"})
check("thinking kept short", sent.get("reasoning_effort") == "low")
check("room to answer", sent.get("max_tokens") == 2000)
check("the json instruction is last", sent["messages"][-1]["content"].startswith("Reply now with the json"))
check("the word json is in the system prompt", "json" in build_system_prompt(facts, "greeting", None))
check("the contract shows an example", '"next_state": "requesting"' in build_system_prompt(facts, "greeting", None))

# whitespace back from DeepSeek's json mode: retry at once, do not feed it back
brain, fake = make_brain(["   \n  ", REPLY])
check("a blank reply is retried", brain.decide("system", []) is not None)
check("two calls made", len(fake.calls) == 2)
check(
    "the blank turn is not echoed back",
    all(message["role"] != "assistant" for message in fake.calls[1]["messages"]),
)
check("and the retry says so", "empty" in fake.calls[1]["messages"][-1]["content"])

brain, fake = make_brain(["   ", "still not json", REPLY])
check("three attempts are allowed", brain.decide("system", []) is not None)
check("all three were used", len(fake.calls) == 3)
brain, fake = make_brain(["   ", "nope", "   "])
check("after three it gives up", brain.decide("system", []) is None)
check("and stops at three", len(fake.calls) == 3)

brain, fake = make_brain([TypeError("unsupported parameter: reasoning_effort"), REPLY])
check("unsupported param survived", brain.decide("system", []) is not None)
check("it was dropped", brain._supports["reasoning_effort"] is False)
check("json mode kept", brain._supports["response_format"] is True)
check("and the retry carried it", "reasoning_effort" not in fake.calls[1])

brain, fake = make_brain([RuntimeError("quota exceeded")])
check("a real error is not swallowed", brain.decide("system", []) is None)

# --- 8. the prompt ----------------------------------------------------------

prompt = build_system_prompt(facts, "negotiating", None)
check("prompt has facts", "Oliver Hart" in prompt and "5pm" in prompt)
check("prompt marks unknown restaurant", "UNKNOWN" in prompt)
check("prompt has state", "CURRENT STATE: negotiating" in prompt)
check("prompt has contract", '"requested_change"' in prompt)
check("prompt has manager_confirmed", '"manager_confirmed"' in prompt)
check("prompt bans parroting", "quote the manager's words" in prompt)
check(
    "prompt carries the evening greeting",
    'greet with "Good evening"' in build_system_prompt(facts, "greeting", None, "evening"),
)
check(
    "prompt carries the morning greeting",
    'greet with "Good morning"' in build_system_prompt(facts, "greeting", None, "morning"),
)
check("prompt bans 24h times", "Never 21:00" in prompt)
check("prompt asks for 'under the name'", f"under the name {facts.guest_name}" in prompt)
check("prompt fixes the agent's side", "on the guest's side" in prompt)
check("prompt warns off the restaurant's voice", "we look forward to seeing you" in prompt)
check("prompt documents change_value", '"change_value"' in prompt)
check("prompt orders the opening", "Say what you want first, ask second" in prompt)
check(
    "opening instruction asks the name last",
    OPEN_CONVERSATION.index("party size") < OPEN_CONVERSATION.index("which restaurant"),
)
check("prompt carries a known restaurant", "The Ivy" in build_system_prompt(facts, "negotiating", "The Ivy"))

# --- 9. every Telethon coroutine call is awaited ----------------------------
#
# Catches the class of bug where `me = client.get_me()` silently yields a
# coroutine object instead of the user.

from telethon import TelegramClient  # noqa: E402

NEEDS_AWAIT = {
    name
    for name, _ in inspect.getmembers(TelegramClient, inspect.iscoroutinefunction)
    if not name.startswith("__")
}
# These are plain defs that hand back a coroutine while a loop is running,
# so they must be awaited too.
NEEDS_AWAIT |= {"run_until_disconnected", "start", "disconnect"}

CALL = re.compile(r"(?P<obj>[\w.]*client)\.(?P<attr>\w+)\s*\(")
sources = [ROOT / "login.py", ROOT / "main.py", *sorted((ROOT / "agent").glob("*.py"))]
offenders: list[str] = []
scanned = 0

for source in sources:
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0]
        for match in CALL.finditer(code):
            if match.group("attr") not in NEEDS_AWAIT:
                continue
            scanned += 1
            before = code[: match.start()]
            if before.rstrip().endswith("await") or "async with" in before:
                continue
            offenders.append(f"{source.name}:{number}: {line.strip()}")

check("found Telethon calls to audit", scanned >= 5)
check("every Telethon coroutine call is awaited", not offenders)
for offender in offenders:
    print("  missing await ->", offender)
check("get_me is in the audited set", "get_me" in NEEDS_AWAIT)
check("action is not awaited by mistake", "action" not in NEEDS_AWAIT)


def audit_line(code: str) -> bool:
    """True when the line would be reported as a missing await."""
    code = code.split("#", 1)[0]
    for match in CALL.finditer(code):
        if match.group("attr") not in NEEDS_AWAIT:
            continue
        before = code[: match.start()]
        if not (before.rstrip().endswith("await") or "async with" in before):
            return True
    return False


# The audit must catch the original bug and leave correct code alone.
check("audit catches a bare get_me", audit_line("    me = client.get_me()"))
check("audit catches self.client", audit_line("    x = self.client.send_message(p, t)"))
check("audit passes awaited calls", not audit_line("    me = await client.get_me()"))
check("audit passes async with action", not audit_line("    async with self.client.action(p, 'typing'):"))
check("audit ignores other objects", not audit_line("    self.journal.write('sent')"))

print(f"\npassed: {len(ok)}")
if bad:
    print("FAILED:")
    for name in bad:
        print("  -", name)
    raise SystemExit(1)
print("all good")
