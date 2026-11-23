"""One-off interactive Telegram login. Creates tg.session next to this file.

Run it yourself: python login.py
It asks for your phone number, the code Telegram sends you, and your 2FA
password if you have one. main.py then reuses the session without prompting.
"""

from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient

from agent.config import SESSION_FILE, SESSION_NAME, ConfigError, Settings, load_settings


async def sign_in(settings: Settings) -> int:
    # Built inside the running loop: Telethon binds to the loop it finds.
    client = TelegramClient(SESSION_NAME, settings.api_id, settings.api_hash)
    async with client:  # prompts for phone, code and 2FA password as needed
        me = await client.get_me()
        print(f"Signed in as {me.first_name} (@{me.username}), id {me.id}.")
        print(f"Session saved to {SESSION_FILE.name} - keep it out of git.")

        try:
            manager = await client.get_entity(settings.manager_username)
        except Exception as exc:
            print(
                f"Could not resolve @{settings.manager_username}: {exc}\n"
                "Check MANAGER_USERNAME in .env; the agent needs it at startup."
            )
            return 1

        print(f"Manager @{settings.manager_username} resolved, id {manager.id}.")
    return 0


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1
    return asyncio.run(sign_in(settings))


if __name__ == "__main__":
    sys.exit(main())
