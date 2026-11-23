# Notes - phase 1

## Scope

One restaurant, one manager, one table. Phase 1 is text; phase 2 adds voice
notes in both directions (see below). No calls, no images, no booking
websites.

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

**A confirmed booking always routes through `confirmed`.** The second dry run
lost its recap: the model returned `manager_confirmed: true` but proposed
`next_state: "done"`, the "done only from confirmed" rule refused the jump,
the state stayed `negotiating`, and the model's own "Thank you, looking
forward to it." went out instead. So `manager_confirmed` plus a known
restaurant now forces `confirmed` whatever the model proposed, and the recap
is checked in code: all five fields (party size, time, date, restaurant,
guest name) must appear in the text that went out, or a `recap_incomplete`
warning lands in the journal.

**The agreed booking, not the facts file.** `state.json` carries an `agreed`
block - party size, time, date, restaurant, guest name - seeded from the
facts file and moved only when the operator approves a change. The third dry
run settled on 9.30pm and the recap still read 9pm, because the recap was
built from `facts.yaml`; now the recap, `recap_missing()`, the prompt and
every canned line are built from `convo.booking(facts)`. A change needs three
things to land: the model's `change_value` (the concrete "21:30"), an
escalation on that topic, and a clear yes from the operator (`operator_approves`
- a rejection anywhere in the reply wins). Each move is journalled as
`change_approved` with `from`/`to`; refusals and unreadable values become
`change_not_applied`.

**An unclear operator answer is asked about, never assumed.** The fourth dry
run was answered "так" in Saved Messages. The English-only word list read
that as "no clear approval", the agreed time stayed at 9pm - and the model,
which had the same reply in its history as `[OPERATOR] так`, went ahead and
offered the manager 9.30pm. So: `operator_verdict` returns yes, no or
unclear (and knows Ukrainian and Russian yes/no words); unclear sends one
clarifying question back to Saved Messages ("Sorry, is that a yes to
9.30pm?") and keeps waiting rather than calling the model, with a cap of two
clarifications before it simply waits quietly.

**An approval is told to the model, not just to the state.** The fifth dry
run looped: the operator approved 9.30pm, the booking moved, and the model -
which had no idea - asked for the same approval again, so `[Q3]` went out.
Now an approved change appends an operator line to the history ("Approved:
time is now 9.30pm. Do not ask me again about it. Tell the manager you are
happy with it...") and a decline appends its own. On top of that,
`_already_agreed()` drops `needs_operator` when the model's own
`requested_change`/`change_value` already matches the agreed booking, and
logs `escalation_skipped`. Belt and braces, because the prompt alone had
already failed once.

**The model can no longer get ahead of the agreed booking.**
`conflicting_values()` compares the clock times and party sizes in a piece of
text against the agreed booking, and runs on both sides: on the agent's own
draft, so a time the operator has not approved is never offered to the
manager (the draft is dropped, the manager gets the holding line, the
operator gets the reason), and on the manager's message before a recap, so a
booking "confirmed" at a different time escalates instead of being read back
wrongly. The prompt says the same thing in words, but the prompt is not where
this is enforced.

**Every conversation ends somewhere.** `manager_refused` in the contract
closes the conversation from wherever it is: a final no ends in `done` with
`outcome: "no_booking"`, a confirmation ends in `confirmed` with
`outcome: "booked"`, and a `booking_outcome` line with the agreed booking
goes to the journal either way. A confirmation outranks a refusal in the
same decision. Before this, a refusal simply left the agent sitting in
`negotiating` with the reminder watchdog ticking.

**The agent speaks for the guest.** "We look forward to seeing you" is the
restaurant's line, and it went out in the recap. The recap now ends "Many
thanks, we look forward to it", the prompt has a paragraph on whose side the
agent is, and a test asserts no fixed line uses the restaurant's voice.

**Disagreements are logged, not acted on.** If the manager's message carries
a booking marker (reserved, booked, confirmed, that's done, see you then) and
the model still says `manager_confirmed: false`, a `confirmation_mismatch`
warning goes to the journal. The model's decision stands: a keyword list is
not good enough to confirm a booking on its own, but it is good enough to
tell us the model may be reading the room wrongly.

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

## Choosing the LLM

Nothing in the code is tied to a provider: `openai` as a plain
OpenAI-compatible client, and `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` in
`.env`. Switching providers is a change to those three lines. Everything
below is what the switching actually taught us.

**Groq, `openai/gpt-oss-120b` - development.** Free, fast, and the plan all
along was to develop here and move later. It is a reasoning model, and with
the original `max_tokens=500` it spent the whole budget thinking and
returned an empty string twice in a row, which turned a plain question about
the date into an operator escalation. Hence `max_tokens=2000` and
`reasoning_effort="low"`. It was also the model that kept re-asking for an
approval it had already been given, which is what the `_already_agreed()`
guardrail and the operator note in the history exist for.

**DeepSeek, `deepseek-flash` - the real run.** Steadier on the booking logic
and cheap enough not to think about. Its quirk is json mode: a run on
2026-11-23 came back as pure whitespace (63-146 characters of it) on roughly
a third of calls. Three request styles, ten calls each, same prompt and the
same "9pm is fully booked, 9.30pm instead?" turn:

| variant | clean json | whitespace | prose, not json |
| --- | --- | --- | --- |
| `response_format=json_object` | 6-7 / 10 | 3-4 | 0 |
| no `response_format`, json parsed out of the text | 4 / 10 | 0 | 6 |
| `json_object` + a trailing "reply now with the json object" turn | 10 / 10, twice over | 0 | 0 |

Dropping `response_format` is the worst of the three: the model simply
answers in prose ("Let me check whether 9.30pm would suit...") and no
amount of parsing recovers a decision from that. So json mode stays, the
system prompt says "json" in lower case and carries a filled example
(DeepSeek wants the word; the example settles the shape), and `TURN_NUDGE`
goes in as the last message of every call - the instruction has to be the
last thing the model reads, and the history had been ending on the
manager's line. A turn now gets three attempts rather than two, and a
whitespace reply is retried at once and never fed back as an assistant turn,
which would only teach the model that blank is acceptable.

Neither `reasoning_effort` nor `response_format` is accepted everywhere, so
each is dropped and the call retried if the endpoint complains, and the drop
is journalled as `llm_param_dropped`.

**Before a real booking**, point `tools/probe_llm.py` at whatever model is
configured. Ten clean replies out of ten is the bar; anything less will show
up as `llm_empty`, `llm_invalid` or an `invalid_output` escalation in the
middle of a conversation with a restaurant.

## Voice (phase 2)

**Transcribe, then forget it was audio.** A voice note is downloaded,
handed to Whisper, and from that point on it is an ordinary manager
message: same debounce, same guardrails, same state machine. Nothing
downstream knows the difference, and the transcript is journalled as
`voice_received` so a run can be read back as text. The one thing the
transcript carries with it is `last_manager_voice`, which decides the
medium of the reply.

**Answer in kind, except the recap.** Voice in, voice out; text in, text
out. The confirmation recap is always written, because it is the message
that has to be checkable at a glance - and because a wrong detail in a
voice note is much harder to spot than in a line of text.

**Both halves are optional and fail towards text.** Listening needs
`STT_BASE_URL`, `STT_API_KEY` and `STT_MODEL`; speaking needs edge-tts (no
key, Microsoft's voices) and ffmpeg, because Telegram voice notes must be
ogg/opus and edge-tts hands back mp3. Whatever is missing is reported once
at startup in plain English, and the agent carries on in text. If Whisper
fails on a particular note the agent asks the manager to type it - in text,
since a voice reply saying "I couldn't hear you" is a poor joke. If
synthesis fails, that message goes out as text and `tts_failed` lands in the
journal. The one thing that must never happen is a message not going out at
all.

**edge-tts, not a paid TTS.** No key, decent British voices, and
`en-GB-SoniaNeural` is the closest to the register the agent writes in.
Encoding is `libopus` at 32k mono 48kHz, which is what Telegram's own
clients send; the duration comes from ffprobe when it is there, so the
voice note does not show as 0:00.

**A name that was only heard is not a name yet.** Whisper is good at
sentences and poor at proper nouns, and the restaurant's name ends up in
the confirmation recap. So a name learned from a transcript is held as
unverified: the agent asks once, in writing ("is that The Ivy? Could you
type it for me, please?"), the typed answer replaces it and the swap is
journalled as `restaurant_name_verified` with `from`/`to`. No recap goes
out while the name is unverified, and if the manager confirms the booking
anyway the operator is asked rather than the agent guessing at a spelling.
A name that was typed in the first place is trusted immediately, so nothing
changes for a text-only conversation.

**What is not done.** Voice notes are transcribed, never replayed to the
operator: escalations in Saved Messages are text, with the transcript
quoted. The agent does not detect the manager's language, and it does not
listen to anything but voice notes - video notes, music and files are
ignored exactly as before.

## Known limits

- One manager, one booking; nothing multi-restaurant.
- No backoff on LLM failures beyond the SDK's single retry; a hard failure
  becomes an escalation.
- The reminder fires once per agent message; it is not a full follow-up
  schedule.
- Photos, files and video notes from the manager are logged and ignored, so
  the agent never acknowledges a photo of the menu. Voice notes are handled
  (phase 2).
- `state.json` holds the plain conversation history: git-ignored, but not
  encrypted.

## Possible next steps

- Summarise older history to keep prompts short.
- Acknowledge photos and files politely ("I'm afraid I can only read text"),
  the way voice notes are now handled.
- Extend `tests/test_conversation.py` into a full scripted dry run (a whole
  manager dialogue end to end, asserted turn by turn).
- Several candidate restaurants, chosen by who answers first.
