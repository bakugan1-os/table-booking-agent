"""Unit checks: facts loading, the time guard, guardrails, state, JSON contract.

Plain script, no pytest:

    python tests/test_units.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import guardrails  # noqa: E402
from agent.config import (  # noqa: E402
    ConfigError,
    Facts,
    load_facts,
    start_guard,
)
from agent.llm import _extract_json, _validate  # noqa: E402
from agent.prompts import build_system_prompt  # noqa: E402
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

check("recap has party size", "two people" in guardrails.recap_line(facts, "The Ivy"))
check("recap names restaurant", "The Ivy" in guardrails.recap_line(facts, "The Ivy"))
check("recap survives unknown name", "at " in guardrails.recap_line(facts, None))
check("recap <= 3 sentences", guardrails.recap_line(facts, "The Ivy").count(". ") <= 2)
print("recap:", guardrails.recap_line(facts, "The Ivy"))

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
check("disclosure is honest", "AI assistant" in guardrails.bot_disclosure_line(facts))

# --- 5. state machine -------------------------------------------------------

c = Conversation()
check("greeting -> requesting", c.next_state("requesting", restaurant_known=False) == "requesting")
c.state = "requesting"
check("no backwards", c.next_state("greeting", restaurant_known=True) == "requesting")
check("confirmed blocked without name", c.next_state("confirmed", restaurant_known=False) == "negotiating")
check("confirmed allowed with name", c.next_state("confirmed", restaurant_known=True) == "confirmed")
check("done blocked outside confirmed", c.next_state("done", restaurant_known=True) == "requesting")
c.state = "confirmed"
check("done allowed from confirmed", c.next_state("done", restaurant_known=True) == "done")
check("garbage state ignored", c.next_state("banana", restaurant_known=True) == "confirmed")

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

# --- 8. the prompt ----------------------------------------------------------

prompt = build_system_prompt(facts, "negotiating", None)
check("prompt has facts", "Oliver Hart" in prompt and "5pm" in prompt)
check("prompt marks unknown restaurant", "UNKNOWN" in prompt)
check("prompt has state", "CURRENT STATE: negotiating" in prompt)
check("prompt has contract", '"requested_change"' in prompt)
check("prompt carries a known restaurant", "The Ivy" in build_system_prompt(facts, "negotiating", "The Ivy"))

print(f"\npassed: {len(ok)}")
if bad:
    print("FAILED:")
    for name in bad:
        print("  -", name)
    raise SystemExit(1)
print("all good")
