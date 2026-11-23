# Notes - phase 1

## Scope

Text only. No voice notes, no calls, no images, no booking websites. One
restaurant, one manager, one table.

## Decisions

**Userbot, not a bot.** The manager talks to a normal account over MTProto
(Telethon). `login.py` does the interactive sign-in once and leaves
`tg.session`; `main.py` never prompts, so it can be restarted freely.

**The guest's details are not in git.** `guest_name` and `guest_phone` come
from `.env` (`GUEST_NAME`, `GUEST_PHONE`); a facts file may override them,
which is how `facts.test.yaml` can be committed with test values. A value in
the file wins, a placeholder falls through to `.env`, and missing from both is
a startup error naming both places.

**facts.yaml is the only source of truth.** Everything the agent may state
lives there (plus those two .env fields). The restaurant's name is deliberately absent, so the agent has
to ask for it - which also exercises the "learn a fact from the manager"
path. A learned name is stored in `state.json`, never written back to
`facts.yaml`.

**No framework.** `openai` as a plain OpenAI-compatible client, one call per
turn. Swapping Groq for DeepSeek is a change to `.env` only.

**Full history every call.** The system prompt is rebuilt each turn (facts,
state, known restaurant name) and the whole conversation is replayed as
messages. Manager turns map to `user`, the agent's to `assistant`, and the
operator's to `user` prefixed `[OPERATOR]` - the one source the prompt says
to obey. Long conversations therefore grow the token count linearly; phase 1
does not summarise.

**JSON contract.** Nine fields; `_extract_json` takes the first balanced
object so a chatty model still parses. Invalid twice -> the manager gets the
holding line and the question comes to the operator, which keeps a broken
model from inventing anything.

**The code owns every fixed line.** Off-topic refusal, the holding line, the
bot disclosure, the reminder and the confirmation recap are all built in
`guardrails.py` from `facts.yaml`. The LLM decides *when* (`off_topic`,
`asked_if_bot`, `next_state`), not *what*.

**Escalation keywords are narrow.** `requested_change`
(`time|party_size|deposit|payment|none`) carries the general case; the
keyword regex only fires on `deposit`, `prepay`, `card`, `payment` as a
second line of defence on money. Requests for card numbers, passwords and
one-time codes are a separate regex and always escalate, even if the model
marked the message off-topic.

**Operator detection in Saved Messages.** The agent and the operator are the
same Telegram account, so every message there is "outgoing". The agent tags
its own questions `[Qn]` and remembers their message ids; anything else in
that chat is the operator. A note written while nothing is pending is kept as
context and triggers no message to the manager - otherwise a shopping list in
Saved Messages would reach the restaurant.

**Repeat escalations.** Once the operator has answered about a topic, that
topic goes into `resolved_changes` and stops re-triggering, so "so that's the
deposit settled" does not loop back to the operator. Trade-off: a *second*,
different deposit demand would not escalate on keywords alone - it still has
to come through `needs_operator` or `requested_change`.

**State transitions are validated in code.** Forward or stay, never
backwards. `confirmed` needs two things: the restaurant's name and
`manager_confirmed`, the model's reading of whether the manager said the
booking is actually made. A table being free ("we have a table for two at
9pm") is availability, not a booking, so the agent asks for it to be put in
the guest's name and stays in `negotiating`; the first dry run confirmed on
availability alone. `done` is reachable only from `confirmed`. A model that
jumps the gun lands in `negotiating` instead.

**House style is enforced after the model, not asked for before it.** The
salutation comes from London's clock (`greeting_word`: morning before noon,
afternoon before 6pm, evening after), and `sanitize` rewrites a wrong "Good
morning" rather than trusting the prompt. Clock times are rewritten to one
British form - `21:00`, `9 pm` and `9:00 PM` all become `9pm` - because the
first dry run mixed `9pm` and `21:00` in the same conversation.

**Time guard.** `main.py` exits with a warning if the booking is less than an
hour away in `facts.yaml`'s timezone. `date: today` is resolved once at
startup, so an agent left running overnight does not silently move the
booking to the next day.

**Testing flags.** `--facts PATH` selects the facts file, so `facts.test.yaml`
can be committed with a test name and an Ofcom drama-range number and no
personal data. `--skip-time-check` still runs the guard and prints the
warning it would have refused with, then starts anyway; both the facts file
name and the skip are recorded in the `run` line of the journal, so a log can
never be mistaken for a real booking attempt.

**Persistence.** `state.json` is written after every message with an
atomic replace, and holds state, full history, the pending question and its
topic, the learned restaurant name, counters and flags. On restart the agent
resumes mid-conversation and does not greet again. `--fresh` moves the old
file to `state.json.bak`.

**Asyncio wiring.** Telethon binds to whatever loop is running when the
client is built, and `client.loop` only resolves a *running* loop, so both
entry points create the client inside the coroutine and go through
`asyncio.run`. `login.py` uses `async with client`, which is Telethon's
`start()` and prompts for phone, code and password. Everything on the client
is awaited; `action()` and `add_event_handler()` are the only plain calls, and
`run_until_disconnected()`, `start()` and `disconnect()` are plain defs that
hand back a coroutine, so they are awaited too. `tests/test_units.py` derives
that list from `TelegramClient` itself with `inspect.iscoroutinefunction` and
fails on any unawaited call, since a missing `await` is silent until an
attribute lookup on the coroutine blows up.

**Debounce and typing.** 5-second timer, restarted by each new message, so a
burst becomes one LLM call. The `typing...` action shows while the model
thinks. The LLM call runs in a worker thread (`asyncio.to_thread`) so the
blocking SDK does not stall the Telethon event loop.

## Known limits

- One manager, one booking; nothing multi-restaurant.
- No backoff on LLM failures beyond the SDK's single retry; a hard failure
  becomes an escalation.
- The reminder fires once per agent message; it is not a full follow-up
  schedule.
- Non-text messages from the manager are logged and ignored, so the agent
  never acknowledges a photo of the menu.
- `state.json` holds the plain conversation history: git-ignored, but not
  encrypted.

## Possible phase 2

- Summarise older history to keep prompts short.
- Acknowledge non-text messages politely ("I'm afraid I can only read text").
- Extend `tests/test_conversation.py` into a full scripted dry run (a whole
  manager dialogue end to end, asserted turn by turn).
- Several candidate restaurants, chosen by who answers first.
