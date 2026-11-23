# Table Booking Agent - Phase 1 (text only)

A Telegram **userbot** (your own account via MTProto, not the Bot API) that
messages a restaurant manager and books a table for you. It speaks British
English, only ever discusses this one reservation, and asks you - in Saved
Messages - about anything it does not already know.

## Requirements

- Python 3.11+
- Telegram API credentials from <https://my.telegram.org> (API development tools)
- An OpenAI-compatible LLM endpoint (Groq during development, DeepSeek later)
- For voice notes: ffmpeg on PATH (`winget install Gyan.FFmpeg`, then reopen
  the terminal) and a Whisper endpoint. Both are optional - without them the
  agent works in text.

## Install

PowerShell, from the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configure

1. Copy the template and fill it in:

   ```powershell
   Copy-Item .env.example .env
   ```

   | Variable | Meaning |
   | --- | --- |
   | `TG_API_ID` | numeric API ID from my.telegram.org |
   | `TG_API_HASH` | API hash from the same page |
   | `MANAGER_USERNAME` | the manager's Telegram username, with or without `@` |
   | `LLM_BASE_URL` | e.g. `https://api.groq.com/openai/v1` |
   | `LLM_API_KEY` | key for that endpoint |
   | `LLM_MODEL` | model name as the endpoint spells it |
   | `GUEST_NAME` | the guest's name, used when making the booking |
   | `GUEST_PHONE` | contact number, handed over only if the manager asks |
   | `STT_BASE_URL` | Whisper endpoint, e.g. `https://api.groq.com/openai/v1` |
   | `STT_API_KEY` | key for it (the same Groq key works) |
   | `STT_MODEL` | `whisper-large-v3` |
   | `TTS_VOICE` | optional; `en-GB-SoniaNeural` by default |

   The guest's details live in `.env` on purpose, so nothing personal is
   committed. The agent refuses to start if they are missing from both `.env`
   and the facts file.

2. Edit `facts.yaml`. Together with `GUEST_NAME`/`GUEST_PHONE` this is the
   **only** set of facts the agent may state:

   ```yaml
   party_size: 2
   time: "17:00"
   date: today            # "today", "tomorrow" or 2026-11-23
   timezone: Europe/London
   restaurant_name: null  # left unknown: the agent asks the manager
   ```

   A facts file may still carry `guest_name`/`guest_phone` - `facts.test.yaml`
   does, with test values - and a value in the file wins over `.env`.

## Run

```powershell
python login.py    # once, interactive: phone, code, 2FA password
python main.py     # starts the agent; it messages the manager first
```

`login.py` creates `tg.session` and also checks that `MANAGER_USERNAME`
resolves. `main.py` never prompts.

`main.py` refuses to start if the booking is less than an hour away in the
facts file's timezone - there would not be enough time to arrange it.

### Flags

| Flag | Effect |
| --- | --- |
| `--fresh` | ignore `state.json` (kept as `state.json.bak`) and start a new conversation |
| `--facts PATH` | use another facts file (default `facts.yaml`) |
| `--skip-time-check` | start anyway when the booking is too close; prints the warning it would have refused with |

For a dry run against a test manager account:

```powershell
python main.py --facts facts.test.yaml --skip-time-check --fresh
```

`facts.test.yaml` is committed: 9pm, a test name and a number from Ofcom's
reserved drama range, so there is nothing personal in git.

## Tests

Two plain scripts - no pytest, no network, no Telegram account. They stub the
Telegram client and the LLM, so they are safe to run at any time:

```powershell
python tests\test_units.py         # facts, time guard, guardrails, state, JSON contract
python tests\test_conversation.py  # one turn at a time: escalation, off-topic, recap
```

`test_units.py` also audits `login.py`, `main.py` and `agent/` for Telethon
coroutines called without `await`, and the stub client in
`test_conversation.py` keeps Telethon's async/sync split, so the startup and
sign-in paths fail here rather than in front of the manager.

### Checking the model before a run

Neither suite talks to the LLM. Before a real booking - and after changing
`LLM_MODEL` or the provider - check that the model returns usable json:

```powershell
python tools\probe_llm.py                     # 10 calls, exactly as the agent asks
python tools\probe_llm.py json_object text    # compare request styles
```

It sends the agent's real system prompt built from `facts.test.yaml`, so
nothing personal goes to the provider, and prints how many replies were
clean json, blank or prose. Ten out of ten is the bar; `NOTES.md` has the
measurements behind that. Note that this one does cost a few API calls.

Each prints `passed: N` and exits non-zero on the first failure, listing the
checks by name.

## How a conversation goes

1. The agent opens the conversation: it asks for the table and, since it does
   not know the restaurant's name, asks for that too.
2. Incoming messages are debounced for 5 seconds (a burst of three messages
   is one LLM call), with "typing..." showing while it thinks.
3. States run `greeting -> requesting -> negotiating -> confirmed -> done`.
   `confirmed` needs the manager to say the booking is *made* - a free table
   is not a booking - and entering it sends a recap built in code, carrying
   all five fields: party size, time, date, restaurant, guest name. The model
   never writes that message and cannot skip it.
4. A final no from the restaurant also ends the conversation: `done` with
   `outcome: "no_booking"` in `state.json`, against `"booked"` for a table
   secured. Either way a `booking_outcome` line lands in the journal.
5. In `done` the agent still answers the manager, briefly and politely.
6. After 10 minutes of silence it sends **one** polite nudge, never more.

## Voice notes

The agent answers in whatever the manager used. A voice note is downloaded,
transcribed by Whisper and handled exactly like a typed message; the reply
then goes back as a voice note (en-GB-SoniaNeural, encoded to ogg/opus, with
"recording voice" showing while it is made). A typed message gets a typed
reply.

**The confirmation recap is always text**, whatever the conversation has
been doing, so it can be read and checked at a glance.

A restaurant name picked up from a transcript is treated as a guess:
Whisper mishears proper nouns, and that name goes into the recap. The agent
asks about it once, in writing - "is that The Ivy? Could you type it for me,
please?" - and the typed answer replaces it (`restaurant_name_verified` in
the journal). No recap goes out until then. A name that was typed in the
first place is trusted straight away.

Either half can be missing and the agent keeps going:

| missing | what happens |
| --- | --- |
| `STT_*` not set, or Whisper fails | the agent asks the manager to type it, in text |
| ffmpeg or edge-tts missing | the reply goes out as text; a warning at startup says why |
| synthesis fails mid-conversation | that message goes out as text, `tts_failed` in the journal |

Startup prints what is on: `Voice: listening on, speaking off (the recap is
always text).` Transcripts and voice events are in the journal
(`voice_received`, `stt_done`, `tts_done`, `stt_failed`, `tts_failed`), and
the audio itself stays in `voice/`, which is git-ignored.

## When it needs you

Anything outside `facts.yaml` - a deposit, a different time, a different
party size, special requests, a demand for card details - makes the agent
tell the manager "let me check", then message you in **Saved Messages**:

```
[Q1] The manager wants a GBP 10 per head deposit.

Manager said: We can do 5pm but we take a 10 pound deposit per person.
Topic: deposit | state: negotiating

Reply in this chat and I will carry on.
```

Reply in Saved Messages and it continues immediately. Answer **yes** or
**no** (English, Ukrainian or Russian); anything it cannot read as either
gets one question back - "Sorry, is that a yes to 9.30pm?" - and the agent
keeps waiting rather than guessing. A clear **yes** moves
the agreed booking (and only then): the new time or party size is written to
`state.json` and journalled as `change_approved`, and every later message -
including the confirmation recap - uses the agreed values, not what is still
written in the facts file. While it waits, the manager's messages are
buffered, not answered. A note you write in Saved
Messages when nothing is pending is kept as context and sends nothing.

## Guardrails

Enforced in code, not just asked for in the prompt:

- only two chats are listened to: the manager's and Saved Messages
- off-topic messages (including "ignore previous instructions") get one
  polite refusal and a nudge back to the booking
- replies capped at 3 sentences / 300 characters, with markdown, emoji and
  links stripped
- clock times rewritten to one British form (`21:00` becomes `9pm`) and the
  salutation set from London's clock, not from the model
- the phone number is removed from replies unless the manager asked for a
  contact number; card details, passwords and codes are never given - they
  go to you
- no change (time, party size, deposit, payment) is accepted without your
  reply in Saved Messages
- a reply offering a time or party size you have not approved is never sent:
  it is dropped and escalated to you instead, and the same check stops a
  recap when the manager's confirmation names a different time
- "are you a bot?" gets an honest fixed answer: an AI assistant arranging
  this on behalf of the guest
- links and attachments are never opened; non-text messages are logged and
  ignored

## Files

| Path | Purpose |
| --- | --- |
| `main.py` | entry point, time guard, state loading |
| `login.py` | one-off interactive Telegram login |
| `facts.yaml` | the agent's only source of truth |
| `facts.test.yaml` | committed test facts (9pm, test name and number) |
| `agent/config.py` | `.env` + `facts.yaml` + start guard |
| `agent/prompts.py` | system prompt and JSON contract |
| `agent/llm.py` | OpenAI-compatible call, JSON validation, one retry |
| `agent/guardrails.py` | code-side limits and the fixed canned lines |
| `agent/state.py` | state machine and `state.json` persistence |
| `agent/conversation.py` | Telethon handlers, debounce, escalation, reminder |
| `agent/journal.py` | `logs/decisions.jsonl` |
| `agent/voice.py` | Whisper in, edge-tts and ffmpeg out |
| `tests/` | two stub-driven test scripts |
| `tools/probe_llm.py` | checks an LLM endpoint before a real run |
| `state.json` | state, history, pending question (git-ignored) |
| `logs/decisions.jsonl` | one line per message, escalation and decision |

See `NOTES.md` for the design decisions and what phase 1 deliberately leaves out.
