"""Run the booking agent. Log in once with login.py first."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from telethon import TelegramClient

from agent.config import (
    FACTS_FILE,
    LOG_FILE,
    SESSION_FILE,
    SESSION_NAME,
    STATE_FILE,
    ConfigError,
    load_facts,
    load_settings,
    start_guard,
)
from agent.conversation import BookingAgent
from agent.journal import Journal
from agent.llm import Brain
from agent.state import Conversation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="ignore state.json (kept as state.json.bak) and start a new conversation",
    )
    parser.add_argument(
        "--facts",
        metavar="PATH",
        default=str(FACTS_FILE),
        help=f"facts file to use (default: {FACTS_FILE.name})",
    )
    parser.add_argument(
        "--skip-time-check",
        action="store_true",
        help="start even if the booking is less than an hour away (testing only)",
    )
    args = parser.parse_args()

    facts_path = Path(args.facts)
    try:
        settings = load_settings()
        facts = load_facts(
            facts_path,
            guest_name=settings.guest_name,
            guest_phone=settings.guest_phone,
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    if facts_path.resolve() != FACTS_FILE.resolve():
        print(f"Facts: {facts_path} (not the default {FACTS_FILE.name}).")

    warning = start_guard(facts, where=facts_path.name)
    if warning:
        if not args.skip_time_check:
            print(warning)
            return 1
        print("WARNING: the time guard would have refused this start:")
        print(warning)
        print("WARNING: starting anyway because --skip-time-check was given.")

    if not SESSION_FILE.exists():
        print(f"No Telegram session ({SESSION_FILE.name}). Run: python login.py")
        return 1

    convo = None
    if args.fresh:
        if STATE_FILE.exists():
            backup = STATE_FILE.with_suffix(".json.bak")
            STATE_FILE.replace(backup)
            print(f"--fresh: previous state moved to {backup.name}.")
    else:
        convo = Conversation.load(STATE_FILE)

    if convo is None:
        convo = Conversation()
        print("Starting a new conversation.")
    else:
        print(
            f"Resuming from {STATE_FILE.name}: state={convo.state}, "
            f"messages={len(convo.history)}, "
            f"pending_operator={convo.pending_operator}."
        )

    print(
        f"Booking: a table for {facts.party_human} at {facts.time_human} "
        f"on {facts.date_human} ({facts.timezone})."
    )

    journal = Journal(LOG_FILE)
    journal.write(
        "run",
        facts_file=facts_path.name,
        fresh=args.fresh,
        time_check_skipped=args.skip_time_check,
    )
    brain = Brain(settings, journal)

    async def boot() -> int:
        # Built inside the running loop: Telethon binds to the loop it finds.
        client = TelegramClient(SESSION_NAME, settings.api_id, settings.api_hash)
        booking_agent = BookingAgent(client, settings, facts, convo, brain, journal)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                print("The session is not authorised. Run: python login.py")
                return 1
            await booking_agent.run()
            return 0
        finally:
            if client.is_connected():
                await client.disconnect()

    try:
        return asyncio.run(boot())
    except KeyboardInterrupt:
        print("\nStopped. State saved; run main.py again to carry on.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
