"""One-off interactive Telegram login. Creates tg.session next to this file.

Run it yourself: python login.py
It asks for your phone number, the code Telegram sends you, and your 2FA
password if you have one. main.py then reuses the session without prompting.
"""

from __future__ import annotations

import sys

from telethon import TelegramClient

from agent.config import SESSION_FILE, SESSION_NAME, ConfigError, load_settings


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1

    with TelegramClient(SESSION_NAME, settings.api_id, settings.api_hash) as client:
        me = client.get_me()
        print(f"Signed in as {me.first_name} (@{me.username}), id {me.id}.")
        print(f"Session saved to {SESSION_FILE.name} - keep it out of git.")

        try:
            manager = client.get_entity(settings.manager_username)
            print(f"Manager @{settings.manager_username} resolved, id {manager.id}.")
        except Exception as exc:
            print(
                f"Could not resolve @{settings.manager_username}: {exc}\n"
                "Check MANAGER_USERNAME in .env; the agent needs it at startup."
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
