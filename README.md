# Table Booking Agent

A Telegram **userbot** - your own account over MTProto, not the Bot API - that
messages a restaurant manager and books a table for you. It writes in British
English, talks about nothing but this one reservation, answers voice notes
with voice notes, and asks you in Saved Messages before agreeing to anything
it was not told.

```
agent    Good evening, I'd like to book a table for two at 9pm on Monday
         23 November, please. Might you have one available?
manager  Good evening, this is The Ivy, Covent Garden. I'm afraid 9pm is
         full - we could do 9.30pm?
agent    Let me check that with the guest and I will come straight back to you.
```
> you, in Saved Messages: `[Q1] 9pm is full; they offer 9.30pm. Topic: time (21:30)` → you reply `yes`
```
agent    9.30pm would suit us nicely - could you put it under the name
         Test Guest?
manager  Done, that's reserved.
agent    Wonderful, thank you. Just to confirm: a table for two at 9.30pm
         today, Monday 23 November, at The Ivy, Covent Garden, under the
         name Test Guest. Thanks so much - we're looking forward to it.
```

**Contents** · [Quick start](#quick-start) ·
[How it works](#how-it-works) · [When it asks you](#when-it-asks-you) ·
[Voice notes](#voice-notes) · [Guardrails](#guardrails) ·
[Command line](#command-line) · [Tests](#tests) ·
[Project layout](#project-layout)

---

## Quick start

**You need:** Python 3.11+, Telegram API credentials from
<https://my.telegram.org>, and an OpenAI-compatible LLM endpoint (developed
on Groq, run on DeepSeek). Voice is optional and needs ffmpeg on PATH
(`winget install Gyan.FFmpeg`, then reopen the terminal) and a Whisper
endpoint.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env   # then fill it in, see below
# edit facts.yaml

python login.py               # once, interactive: phone, code, 2FA password
python main.py                # the agent messages the manager first
```

`login.py` creates `tg.session` and checks that `MANAGER_USERNAME` resolves;
`main.py` never prompts. It refuses to start if the booking is less than an
hour away in the facts file's timezone.

### `.env`

| Variable | Meaning |
| --- | --- |
| `TG_API_ID`, `TG_API_HASH` | from my.telegram.org, API development tools |
| `MANAGER_USERNAME` | the manager's Telegram username, with or without `@` |
| `LLM_BASE_URL` | e.g. `https://api.deepseek.com` |
| `LLM_API_KEY`, `LLM_MODEL` | key and model name for that endpoint |
| `GUEST_NAME` | the guest's name, used when making the booking |
| `GUEST_PHONE` | contact number, handed over only if the manager asks |
| `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` | Whisper, for incoming voice notes; the Groq key works |
| `TTS_VOICE` | optional; `en-GB-SoniaNeural` by default |
| `VOICE_OPENING` | `true` to open with a voice note; `false` by default |

The guest's details live in `.env` on purpose, so nothing personal is
committed. The agent refuses to start if they are missing from both `.env`
and the facts file.

### `facts.yaml`

Together with `GUEST_NAME`/`GUEST_PHONE`, this is the **only** set of facts
the agent may state. Anything else goes to you before it is said out loud.

```yaml
party_size: 2
time: "17:00"
date: today            # "today", "tomorrow" or 2026-11-24
timezone: Europe/London
restaurant_name: null  # unknown on purpose: picked up from what the manager says
```

A facts file may also carry `guest_name`/`guest_phone` - `facts.test.yaml`
does, with test values - and a value in the file wins over `.env`.

---

## How it works

1. **The agent opens.** A greeting and the request itself: party size, day,
   time. It does not ask which restaurant this is; the name is picked up
   from whatever the manager says. If it still has not come up when they are
   ready to confirm, the agent asks then, as a note-keeping detail.
2. **Incoming messages are debounced for 5 seconds**, so a burst of three
   messages is one LLM call, with "typing..." showing while it thinks.
3. **One LLM call per turn** gets the whole conversation, the agreed booking
   and the current state, and must return a single JSON decision - reply,
   what it understood, what it decided, the next state and a dozen flags.
   Three attempts; after that you are asked instead.
4. **The states run** `greeting → requesting → negotiating → confirmed →
   done`, validated in code. `confirmed` needs the manager to say the
   booking is *made* - a free table is not a booking - and entering it sends
   a recap built in code from the agreed booking, carrying all five fields:
   party size, time, date, restaurant, guest name. The model never writes
   that message and cannot skip it.
5. **A booking can reopen.** If the restaurant moves the time or lets the
   table go ("cancelled", "given away"), the state goes back to
   `negotiating`, the outcome is cleared, `booking_reopened` lands in the
   journal, and the manager's fresh yes produces a fresh, full recap.
6. **Every conversation ends somewhere:** `done` with `outcome: "booked"`
   or `outcome: "no_booking"`, and a `booking_outcome` line in the journal.
   In `done` the agent still answers briefly and politely.
7. **One nudge, never two.** After 10 minutes of silence the agent chases
   once.

Everything it does is written to `logs/decisions.jsonl`, one line per
message, escalation and decision - see [Command line](#command-line) for
turning that into a readable log.

---

## When it asks you

Anything outside the facts - a deposit, a different time, a different party
size, special requests, a demand for card details - makes the agent tell the
manager "let me check", and message you in **Saved Messages**:

```
[Q1] The manager wants a GBP 10 per head deposit.

Manager said: We can do 5pm but we take a 10 pound deposit per person.
Topic: deposit | state: negotiating

Reply in this chat and I will carry on. Say yes to approve the change.
```

- **Answer yes or no** in English, Ukrainian or Russian - or simply name the
  option you want: "6 pm" to a choice of 6pm or 4pm approves 6pm.
- **Anything unclear gets one question back** ("Sorry, is that a yes to
  9.30pm?") and the agent keeps waiting rather than guessing.
- **Only a clear yes moves the booking.** The new time or party size is
  written to `state.json` and journalled as `change_approved`, and every
  later message - the recap included - uses the agreed values, not what is
  still written in the facts file.
- **While it waits**, the manager's messages are buffered, not answered.
- A note you write in Saved Messages when nothing is pending is kept as
  context and sends nothing.

---

## Voice notes

The agent answers in whatever the manager used. A voice note is downloaded,
transcribed by Whisper and handled exactly like a typed message; the reply
goes back as a voice note (en-GB-SoniaNeural, ogg/opus, with "recording
voice" showing while it is made). A typed message gets a typed reply.

Written rather than spoken, always:

- **the confirmation recap**, so it can be checked at a glance;
- anything carrying a **phone number, an email address** or a claim of being
  written ("typed out for you" in a voice note is a lie);
- everything, once the manager has asked for writing ("can you type me the
  number?") - that preference sticks.

A restaurant name picked up from a transcript is treated as a guess, because
Whisper mishears proper nouns and that name goes into the recap. The agent
asks about it once, in writing - "is that The Ivy? Could you type it for me,
please?" - and the typed answer replaces it. A plain "yes, that's right" to
the spelling counts too. No recap goes out until the name is settled; a name
that was typed in the first place is trusted straight away.

`VOICE_OPENING=true` (or `--voice-opening`) makes the first message a voice
note and captions every voice note the agent sends with its own words.

Either half can be missing and the agent keeps going:

| missing | what happens |
| --- | --- |
| `STT_*` not set, or Whisper fails | the agent asks the manager to type it, in text |
| ffmpeg or edge-tts missing | the reply goes out as text; startup says why |
| synthesis fails mid-conversation | that message goes out as text, `tts_failed` in the journal |

Startup prints what is on: `Voice: listening on, speaking off (the recap is
always text).` and `Voice opening: off`. The audio itself stays in `voice/`,
which is git-ignored.

---

## Guardrails

Enforced in code, not merely asked for in the prompt:

**Scope** · only two chats are listened to, the manager's and Saved
Messages · off-topic messages, including "ignore previous instructions", get
one polite refusal and a nudge back to the booking · links and attachments
are never opened.

**Honesty** · "are you a bot?" gets a fixed honest answer: an AI assistant
arranging this on behalf of the guest · quotation marks only ever hold the
manager's own words, and a quote that is not verbatim from the conversation
is dropped before the message is sent · the agent never claims a message was
typed when it was spoken.

**The booking** · no change to time, party size, deposit or payment is
accepted without your reply in Saved Messages · a reply offering a time or
party size you have not approved is never sent - it is dropped and escalated
to you · the same check stops a recap when the manager's confirmation names
a different time.

**Privacy** · the phone number is removed from replies unless the manager
asked for a contact number · card details, passwords and one-time codes are
never given, they come to you.

**House style** · at most 3 sentences and 300 characters, with markdown,
emoji and links stripped · clock times in one British form (`21:00` becomes
`9pm`) · the salutation set from London's clock, not from the model.

---

## Command line

```powershell
python main.py [--fresh] [--facts PATH] [--skip-time-check] [--voice-opening]
```

| Flag | Effect |
| --- | --- |
| `--fresh` | ignore `state.json` (kept as `state.json.bak`) and start a new conversation |
| `--facts PATH` | use another facts file (default `facts.yaml`) |
| `--skip-time-check` | start anyway when the booking is too close; prints the warning it would have refused with |
| `--voice-opening` | open with a voice note and caption every voice note |

A dry run against a test manager account, with nothing personal in it:

```powershell
python main.py --facts facts.test.yaml --fresh
```

**Check the model before a real booking** - and after changing `LLM_MODEL`
or the provider. This one does cost a few API calls:

```powershell
python tools\probe_llm.py                     # 10 calls, exactly as the agent asks
python tools\probe_llm.py json_object text    # compare request styles
```

It sends the agent's real system prompt built from `facts.test.yaml` and
reports how many replies were clean JSON, blank or prose. Ten out of ten is
the bar; `NOTES.md` has the measurements behind that.

**Export a run** as something readable:

```powershell
python tools\export_log.py            # the last run
python tools\export_log.py --all      # every run in the journal
```

Writes `submission/decision_log.md` - one row per message with the time, the
direction, the text, what the agent understood and what it decided - and
`submission/decisions.jsonl`, the same events raw. Phone numbers and email
addresses are masked in both (`+380******597`); **names are not**, so read
them before sharing. `submission/` is git-ignored for that reason.

---

## Tests

Two plain scripts: no pytest, no network, no Telegram account, no ffmpeg.
The Telegram client, the LLM and both halves of the voice pipeline are
stubbed, so they are safe to run at any time.

```powershell
python tests\test_units.py         # facts, time guard, guardrails, state, JSON contract
python tests\test_conversation.py  # whole dialogues, turn by turn
```

Each prints `passed: N` and lists by name anything that failed. Between them
they replay every dry run that went wrong - the lost recap, the escalation
loop, the booking moved after it was confirmed, the table given away - so
those cannot come back.

`test_units.py` also audits `login.py`, `main.py` and `agent/` for Telethon
coroutines called without `await`, and the stub client keeps Telethon's
async/sync split, so the startup and sign-in paths fail here rather than in
front of the manager.

---

## Project layout

| Path | Purpose |
| --- | --- |
| `main.py` | entry point: flags, time guard, state loading |
| `login.py` | one-off interactive Telegram login |
| `facts.yaml` · `facts.test.yaml` | the booking to make; the committed test one |
| `agent/config.py` | `.env`, the facts file, the start guard |
| `agent/prompts.py` | system prompt and the JSON contract |
| `agent/llm.py` | the LLM call, JSON validation, retries |
| `agent/guardrails.py` | the code-side rules and every fixed line the agent says |
| `agent/state.py` | state machine, the agreed booking, `state.json` |
| `agent/conversation.py` | Telethon handlers, debounce, escalation, reminders |
| `agent/voice.py` | Whisper in, edge-tts and ffmpeg out |
| `agent/journal.py` | `logs/decisions.jsonl` |
| `tests/` | the two test scripts |
| `tools/probe_llm.py` · `tools/export_log.py` | check a model; export a run |

Git-ignored, created at runtime: `tg.session`, `state.json`, `logs/`,
`voice/`, `submission/`, `.venv/`.

**`NOTES.md`** is the companion to this file: why each decision was made,
what every dry run broke, and what is deliberately left undone.
