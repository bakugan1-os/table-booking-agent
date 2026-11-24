# Design notes

`README.md` says what the agent does. This file says why it does it that
way, and what each dry run with a real restaurant broke before it did.

**Contents** · [Scope](#scope) · [Architecture](#architecture) ·
[Facts and the agreed booking](#facts-and-the-agreed-booking) ·
[State machine and the recap](#state-machine-and-the-recap) ·
[The operator loop](#the-operator-loop) ·
[What the model may and may not say](#what-the-model-may-and-may-not-say) ·
[Choosing the LLM](#choosing-the-llm) · [Voice](#voice) ·
[What the dry runs broke](#what-the-dry-runs-broke) ·
[Known limits](#known-limits) · [Next steps](#next-steps)

---

## Scope

One restaurant, one manager, one table. Text in phase 1, voice notes both
ways in phase 2. No calls, no images, no booking websites.

The shape of the thing: the LLM decides *what is going on*, the code decides
*what is said and what is allowed*. Every rule below that could be left to
the prompt is also enforced in Python, because over six dry runs the prompt
lost that argument every time.

---

## Architecture

**Userbot, not a bot.** The manager talks to a normal account over MTProto
(Telethon). `login.py` does the interactive sign-in once and leaves
`tg.session`; `main.py` never prompts, so it can be restarted freely.

**No framework.** `openai` as a plain OpenAI-compatible client, one call per
turn. Swapping providers is three lines of `.env`.

**Full history every call.** The system prompt is rebuilt each turn (the
agreed booking, the state, the greeting for the hour) and the whole
conversation is replayed as messages: manager turns as `user`, the agent's
as `assistant`, the operator's as `user` prefixed `[OPERATOR]` - the one
voice the prompt is told to obey. Long conversations grow the token count
linearly; nothing is summarised yet.

**One JSON decision per turn.** Thirteen fields: the reply, what the model
understood, what it decided, the next state, and the flags the code acts on
(`needs_operator`, `off_topic`, `requested_change` + `change_value`,
`asked_if_bot`, `manager_confirmed`, `manager_refused`, `reply_channel`,
`restaurant_name`). `_extract_json` takes the first balanced object, so a
chatty model still parses. Three bad attempts and the operator is asked
instead - a broken model never improvises.

**Persistence.** `state.json` is written after every message with an atomic
replace: state, full history, the agreed booking, the pending question and
its topic, counters and flags. On restart the agent resumes mid-conversation
and does not greet again. `--fresh` moves the old file to `state.json.bak`.

**Debounce and typing.** A 5-second timer, restarted by each new message, so
a burst becomes one LLM call. The `typing...` action shows while the model
thinks (`record-voice` when a voice note is being made). The blocking SDK
call runs in a worker thread so it cannot stall the Telethon event loop.

**Asyncio wiring.** Telethon binds to whatever loop is running when the
client is built, and `client.loop` only resolves a *running* loop, so both
entry points build the client inside the coroutine and go through
`asyncio.run`. Everything on the client is awaited; `action()` and
`add_event_handler()` are the only plain calls, while
`run_until_disconnected()`, `start()` and `disconnect()` are plain defs that
hand back a coroutine, so they are awaited too. `tests/test_units.py`
derives that list from `TelegramClient` itself with
`inspect.iscoroutinefunction` and fails on any unawaited call - a missing
`await` is silent until an attribute lookup on the coroutine blows up, which
is exactly how it was found.

---

## Facts and the agreed booking

**The guest's details are not in git.** `guest_name` and `guest_phone` come
from `.env`; a facts file may override them, which is how `facts.test.yaml`
can be committed with test values. A value in the file wins, a placeholder
falls through to `.env`, and missing from both is a startup error naming
both places.

**The facts file is the input, not the state.** It holds what to ask for.
The restaurant's name is deliberately absent, because the agent is supposed
to pick it up from the conversation, and a learned name is never written
back.

**The agreed booking is the state.** `state.json` carries an `agreed` block
- party size, time, date, restaurant, guest name - seeded from the facts
file and moved only when the operator approves a change. The recap,
`recap_missing()`, the prompt and every canned line are built from
`convo.booking(facts)`, never from the file: a table moved to 9.30pm must
not be confirmed back as 9pm.

A change needs three things to land: the model's `change_value` (the
concrete "21:30"), an escalation on that topic, and a clear approval from
the operator. Each move is journalled as `change_approved` with `from`/`to`;
refusals and unreadable values become `change_not_applied`.

**Time guard.** `main.py` exits with a warning if the booking is less than
an hour away in the facts file's timezone. `date: today` is resolved once at
startup, so an agent left running overnight does not silently move the
booking to the next day.

**Testing flags.** `--facts PATH` selects the facts file, so `facts.test.yaml`
can be committed with a test name and an Ofcom drama-range number.
`--skip-time-check` still runs the guard and prints the warning it would
have refused with, then starts anyway. Both the facts file name and the skip
go into the `run` line of the journal, so a test log can never be mistaken
for a real booking attempt.

---

## State machine and the recap

**Transitions are validated in code.** Forward or stay, never backwards.

`confirmed` needs two things: a restaurant name and `manager_confirmed`, the
model's reading of whether the manager said the booking is actually *made*.
A table being free ("we have a table for two at 9pm") is availability, not a
booking, so the agent asks for it to be put in the guest's name and stays in
`negotiating`.

A confirmed booking always routes *through* `confirmed`, whatever the model
proposed: it once returned `manager_confirmed: true` with
`next_state: "done"`, the "done only from confirmed" rule refused the jump,
and the recap was silently skipped.

**The recap is built in code.** Five fields, every time: party size, time,
date, restaurant, guest name, from the agreed booking. `recap_missing()`
checks the text that actually went out and journals `recap_incomplete` if
anything is absent. The model never writes that message and cannot skip it.

**The restaurant's name is picked up, not demanded.** Asking a restaurant
which restaurant it is, in the first message, reads badly - and the live
runs showed managers say it unprompted. So the opening is a greeting and the
request. If the name still has not come up when they are ready to confirm,
the agent asks then, in writing, as a note-keeping detail: "Lovely - and
just so I have it right in my notes, could you confirm the restaurant's
name?"

**A name that was only heard is not a name yet.** Whisper is good at
sentences and poor at proper nouns, and the name ends up in the recap. A
name learned from a transcript is held unverified: the agent asks once, in
writing ("is that The Ivy? Could you type it for me, please?"), and the
typed answer replaces it - `restaurant_name_verified` with `from`/`to`.
Since the spelling was put in front of them in writing, a plain "yes, that's
right", spoken or typed, verifies it just as well. No recap goes out while
the name is unverified; if the manager confirms anyway they get one short
written question, because that is theirs to settle, not the operator's. A
name that was typed in the first place is trusted immediately.

**A booking can reopen.** An approved change of time or party size while the
booking is `confirmed` or `done` puts it back to `negotiating`, clears
`outcome` and journals `booking_reopened`; the topic also leaves
`resolved_changes`, since the value moved and may move again. So does a
release marker from the restaurant - cancelled, released, given away, no
longer have, fell through - which additionally goes to the operator, because
that is news they need rather than a change to approve. Either way, the
manager's fresh yes produces a fresh, full recap.

**Every conversation ends somewhere.** `manager_refused` closes it from
wherever it is: a final no ends in `done` with `outcome: "no_booking"`, a
confirmation in `confirmed` with `outcome: "booked"`, and a
`booking_outcome` line carrying the agreed booking goes to the journal
either way. A confirmation outranks a refusal in the same decision.

---

## The operator loop

**Operator detection in Saved Messages.** The agent and the operator are the
same Telegram account, so every message there is "outgoing". The agent tags
its own questions `[Qn]` and remembers their message ids; anything else in
that chat is the operator. A note written while nothing is pending is kept
as context and triggers no message to the manager - otherwise a shopping
list in Saved Messages would reach the restaurant.

**Escalation triggers are narrow and doubled.** `requested_change`
(`time|party_size|deposit|payment|none`) carries the general case; a keyword
regex fires only on `deposit`, `prepay`, `card`, `payment`, as a second line
of defence on money. Requests for card numbers, passwords and one-time codes
are a separate regex and always escalate, even if the model called the
message off-topic.

**Yes, no, or ask again.** `operator_verdict` returns one of three, and
knows Ukrainian and Russian yes/no words as well as English - a live run was
answered "так" and the English-only list read that as no approval. Anything
unclear sends one clarifying question back ("Sorry, is that a yes to
9.30pm?") and keeps waiting rather than calling the model, capped at two
clarifications. A rejection anywhere in the reply beats an approval
elsewhere in it.

**Naming an option is approving it.** Asked to choose between 6pm and 4pm,
an operator answers "6 pm", not "yes". A reply containing a concrete value
for the pending topic is taken as approval of that value, and it wins over
whatever the manager proposed.

**An approval is told to the model, not just to the state.** Once a change
is approved, an operator line goes into the history ("Approved: time is now
9.30pm. Do not ask me again about it..."), and a decline leaves its own.
On top of that, `_already_agreed()` drops `needs_operator` when the model's
own `requested_change`/`change_value` already matches the agreed booking,
and logs `escalation_skipped`. Belt and braces, because the model once
looped asking for an approval it had already been given.

**Repeat escalations.** Once the operator has answered about a topic it goes
into `resolved_changes` and stops re-triggering, so "so that's the deposit
settled" does not loop back. An applied change removes its topic again,
since the value moved.

---

## What the model may and may not say

**The code owns every fixed line.** The off-topic refusals, the holding
line, the bot disclosure, the reminder, the name questions and the recap all
live in `guardrails.py` and are built from the agreed booking. The model
decides *when* (`off_topic`, `asked_if_bot`, `next_state`), never *what*.
Off-topic has three wordings in rotation so a persistent manager is not met
with a parrot, and once the table is booked they say "the table for 9pm is
all set" rather than asking to confirm it again.

**The model cannot get ahead of the agreed booking.**
`conflicting_values()` compares clock times and party sizes in a piece of
text against the agreed booking, and runs both ways: on the agent's own
draft, so a time the operator has not approved is never offered to the
manager (the draft is dropped, the manager gets the holding line, the
operator gets the reason), and on the manager's message before a recap, so a
booking "confirmed" at a different time escalates instead of being read back
wrongly.

**Quotation marks only ever hold the manager's own words.** Asked to repeat
what they said, a model will paraphrase and present it as a quote. A request
to quote is answered in code - the last manager message that carried a
confirmation, verbatim, with the time it was sent - and every drafted reply
is checked: a quoted span that is not a verbatim substring of something the
manager wrote takes its whole sentence with it (`quote_dropped`). Matching
ignores case and spacing, nothing else.

**The agent speaks for the guest.** "We look forward to seeing you" is the
restaurant's line, and it went out in a recap once. The recap now ends
"Thanks so much - we're looking forward to it", the prompt has a paragraph
on whose side the agent is, and a test asserts no fixed line uses the
restaurant's voice. The same test family catches "thank you for coming back
to me", which is the manager's line, not the agent's: `sanitize` rewrites it
to "thank you for waiting".

**House style is enforced after the model, not asked for before it.** The
salutation comes from London's clock (`greeting_word`), and `sanitize`
rewrites a wrong "Good morning" rather than trusting the prompt. Clock times
are rewritten to one British form - `21:00`, `9 pm` and `9:00 PM` all become
`9pm`. Replies are capped at three sentences and 300 characters, with
markdown, emoji and links stripped, and the phone number removed unless it
was asked for.

**Disagreements are logged, not acted on.** If the manager's message carries
a booking marker (reserved, booked, confirmed, that's done, see you then)
and the model still says `manager_confirmed: false`, a
`confirmation_mismatch` warning goes to the journal and the model's decision
stands. A keyword list is not good enough to confirm a booking on its own,
but it is good enough to say the model may be reading the room wrongly.

---

## Choosing the LLM

Nothing in the code is tied to a provider: `openai` as a plain
OpenAI-compatible client, and `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` in
`.env`.

**Groq, `openai/gpt-oss-120b` - development.** Free, fast, and the plan all
along was to develop here and move later. It is a reasoning model, and with
the original `max_tokens=500` it spent the whole budget thinking and
returned an empty string twice in a row, which turned a plain question about
the date into an operator escalation. Hence `max_tokens=2000` and
`reasoning_effort="low"`.

**DeepSeek, `deepseek-flash` - the real runs.** Steadier on the booking
logic and cheap enough not to think about. Its quirk is json mode: a third
of calls came back as pure whitespace (63-146 characters of it). Three
request styles, ten calls each, same prompt and the same "9pm is fully
booked, 9.30pm instead?" turn:

| variant | clean json | whitespace | prose, not json |
| --- | --- | --- | --- |
| `response_format=json_object` | 6-7 / 10 | 3-4 | 0 |
| no `response_format`, json parsed out of the text | 4 / 10 | 0 | 6 |
| `json_object` + a trailing "reply now with the json object" turn | 10 / 10, twice over | 0 | 0 |

Dropping `response_format` is the worst of the three: the model simply
answers in prose and no amount of parsing recovers a decision from that. So
json mode stays, the system prompt says "json" in lower case and carries a
filled example (DeepSeek wants the word; the example settles the shape), and
`TURN_NUDGE` goes in as the last message of every call - the instruction has
to be the last thing the model reads, and the history had been ending on the
manager's line. A turn gets three attempts, and a whitespace reply is
retried at once and never fed back as an assistant turn, which would only
teach the model that blank is acceptable.

Neither `reasoning_effort` nor `response_format` is accepted everywhere, so
each is dropped and the call retried if the endpoint complains
(`llm_param_dropped`).

**Before a real booking**, point `tools/probe_llm.py` at whatever model is
configured. Ten clean replies out of ten is the bar; anything less shows up
as `llm_empty`, `llm_invalid` or an `invalid_output` escalation in the
middle of a conversation with a restaurant.

---

## Voice

**Transcribe, then forget it was audio.** A voice note is downloaded, handed
to Whisper, and from that point on it is an ordinary manager message: same
debounce, same guardrails, same state machine. The transcript is journalled
as `voice_received`, so a run reads back as text. The one thing it carries
with it is `last_manager_voice`, which decides the medium of the reply.

**Answer in kind - but the code has the last word on the channel.** Voice
in, voice out; text in, text out. Always written, though:

- the confirmation recap, because it has to be checkable at a glance;
- anything with a number to copy or an email address in it;
- anything claiming to be written - a voice note saying "typed out for you"
  is simply a lie, and the first voice run sent exactly that;
- everything from then on, once the manager has asked for writing ("can you
  type me the number?"), which sets `prefers_text`.

The model has a `reply_channel` field for what it thinks the manager asked
for, and it can only force text, never voice. Each withheld voice note is
journalled as `voice_withheld` with the reason.

**Both halves are optional and fail towards text.** Listening needs the
`STT_*` settings; speaking needs edge-tts (no key, Microsoft's voices) and
ffmpeg, because Telegram voice notes must be ogg/opus and edge-tts hands
back mp3. Whatever is missing is reported once at startup in plain English.
If Whisper fails on a note the agent asks the manager to type it - in text,
since a voice reply saying "I couldn't hear you" is a poor joke. If
synthesis fails, that message goes out as text and `tts_failed` lands in the
journal. The one thing that must never happen is a message not going out at
all.

**edge-tts, not a paid TTS.** No key, decent British voices, and
`en-GB-SoniaNeural` is closest to the register the agent writes in. Encoding
is `libopus` at 32k mono 48kHz, which is what Telegram's own clients send;
the duration comes from ffprobe when it is there, so the note does not show
as 0:00.

**The opening is written unless asked otherwise.** A manager meeting a
stranger's voice note cold is a worse first impression than a clear written
request, so `VOICE_OPENING` is off by default. When it is on, the first
message is spoken and every voice note carries its own words as a caption:
nothing is lost to a bad line, and the conversation stays readable in a
screenshot. After the opening the ordinary rules take over.

**What is not done.** Voice notes are transcribed, never replayed to the
operator - escalations are text, with the transcript quoted. The agent does
not detect the manager's language, and it listens to nothing but voice
notes: video notes, music and files are ignored as before.

---

## What the dry runs broke

Each of these was found by talking to a real account, and each is now a test
that replays the same dialogue.

| what happened | what changed |
| --- | --- |
| Confirmed on "we have a table for two at 9pm" - availability read as a booking | `manager_confirmed` in the contract, and `confirmed` requires it |
| Greeted "Good afternoon" at 8.46pm, and wrote `21:00` next to `9pm` | Salutation from London's clock, one British time format, both applied after the model |
| Manager said "that's reserved", agent answered "Thank you, looking forward to it" - no recap | A confirmed booking is forced through `confirmed`; the recap cannot be skipped |
| Agreed 9.30pm with the operator, recap still said 9pm | The `agreed` block in `state.json`; everything reads from it, not from the facts file |
| Operator answered "так"; the change silently did not apply, but the model offered 9.30pm anyway | Three-way `operator_verdict` with Ukrainian and Russian, a clarifying question, and `conflicting_values` on every draft |
| Operator approved, and the model asked for the same approval again - `[Q3]` | The approval is appended to the history, and `_already_agreed()` drops the repeat |
| Two empty completions in a row from a reasoning model | `max_tokens=2000`, `reasoning_effort="low"`, three attempts |
| A third of DeepSeek's json-mode calls came back as whitespace | `TURN_NUDGE` as the last message; measured 20/20 after |
| Phone number read aloud in a voice note, "typed out for you" | The channel is decided in code: numbers, emails and claims of writing go in text |
| Booking moved to 6pm after it was confirmed; the chat's last recap still said 5pm | An approved change reopens a confirmed booking, and a fresh recap follows |
| "The client cancelled, your table was given away" left the agent sitting in `confirmed` | Release markers reopen the booking and tell the operator |
| Whisper heard the restaurant's name and it went into the recap unchecked | A heard name is unverified until it is written, or agreed to in writing |

---

## Known limits

- One manager, one booking; nothing multi-restaurant.
- No backoff on LLM failures beyond the SDK's single retry; a hard failure
  becomes an escalation.
- The reminder fires once per agent message; it is not a follow-up schedule.
- Photos, files and video notes are logged and ignored, so the agent never
  acknowledges a photo of the menu.
- `state.json` holds the plain conversation history: git-ignored, not
  encrypted.
- The history is replayed in full on every call, so a very long conversation
  gets expensive.

## Next steps

- Summarise older history to keep prompts short.
- Acknowledge photos and files politely ("I'm afraid I can only read text"),
  the way voice notes are now handled.
- Several candidate restaurants, chosen by whoever answers first.
- Replay the exported journal of a real run through the stubs, as a
  regression fixture.
