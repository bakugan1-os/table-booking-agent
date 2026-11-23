"""Environment variables, facts.yaml and the start-time guard."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
FACTS_FILE = ROOT / "facts.yaml"
STATE_FILE = ROOT / "state.json"
SESSION_NAME = str(ROOT / "tg")          # Telethon appends ".session"
SESSION_FILE = ROOT / "tg.session"
LOG_FILE = ROOT / "logs" / "decisions.jsonl"

#: The agent refuses to start later than this before the booking.
START_MARGIN = timedelta(hours=1)

_PLACEHOLDERS = {"", "change me", "...", "none", "null", "your name"}


class ConfigError(RuntimeError):
    """Raised when .env or facts.yaml is unusable."""


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    manager_username: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    # The guest's own details stay out of git: facts files may omit them and
    # let these take over.
    guest_name: str | None = None
    guest_phone: str | None = None


def load_settings() -> Settings:
    """Read .env. Raises ConfigError with a readable message if anything is missing."""
    load_dotenv(ROOT / ".env")

    def need(name: str) -> str:
        value = (os.getenv(name) or "").strip()
        if not value:
            raise ConfigError(f"{name} is missing from .env (see .env.example)")
        return value

    def optional(name: str) -> str | None:
        return (os.getenv(name) or "").strip() or None

    raw_id = need("TG_API_ID")
    try:
        api_id = int(raw_id)
    except ValueError as exc:
        raise ConfigError("TG_API_ID must be an integer") from exc

    return Settings(
        api_id=api_id,
        api_hash=need("TG_API_HASH"),
        manager_username=need("MANAGER_USERNAME").lstrip("@"),
        llm_base_url=need("LLM_BASE_URL").rstrip("/"),
        llm_api_key=need("LLM_API_KEY"),
        llm_model=need("LLM_MODEL"),
        guest_name=optional("GUEST_NAME"),
        guest_phone=optional("GUEST_PHONE"),
    )


def human_time(hhmm: str) -> str:
    """'17:00' -> '5pm', '17:30' -> '5.30pm' (British style)."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    suffix = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    return f"{hour12}{suffix}" if minute == 0 else f"{hour12}.{minute:02d}{suffix}"


_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}


def human_party(size: int) -> str:
    """2 -> 'two people'."""
    word = _NUMBER_WORDS.get(size, str(size))
    return f"{word} {'person' if size == 1 else 'people'}"


@dataclass(frozen=True)
class Facts:
    party_size: int
    time: str
    date: date_cls
    timezone: str
    guest_name: str
    guest_phone: str
    restaurant_name: str | None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def booking_at(self) -> datetime:
        hour, minute = (int(part) for part in self.time.split(":"))
        return datetime(
            self.date.year, self.date.month, self.date.day, hour, minute, tzinfo=self.tz
        )

    @property
    def date_human(self) -> str:
        return self.booking_at.strftime("%A %d %B").replace(" 0", " ")

    @property
    def time_human(self) -> str:
        return human_time(self.time)

    @property
    def party_human(self) -> str:
        return human_party(self.party_size)


def _resolve_date(raw: object, tz: ZoneInfo, where: str = "facts.yaml") -> date_cls:
    today = datetime.now(tz).date()
    if raw is None:
        return today
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date_cls):
        return raw
    text = str(raw).strip().lower()
    if text in ("", "today"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date_cls.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(
            f"{where}: date must be 'today', 'tomorrow' or an ISO date, got {raw!r}"
        ) from exc


def _pick_guest_field(
    key: str, env_key: str, file_value: object, env_value: str | None, where: str
) -> str:
    """The facts file wins; .env is the fallback. Placeholders count as missing."""
    for candidate in (file_value, env_value):
        text = str(candidate).strip() if candidate is not None else ""
        if text and text.lower() not in _PLACEHOLDERS:
            return text
    raise ConfigError(
        f"{key} is not set: add it to {where} or set {env_key} in .env"
    )


def load_facts(
    path: Path = FACTS_FILE,
    *,
    guest_name: str | None = None,
    guest_phone: str | None = None,
) -> Facts:
    """Read and validate a facts file (facts.yaml, or whatever --facts points at).

    guest_name/guest_phone are the .env fallbacks used when the file omits them,
    so the real guest's details never have to be committed.
    """
    if not path.exists():
        raise ConfigError(f"{path.name} not found")
    here = path.name  # named in every message, since the path is selectable
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{here} must be a mapping")

    tz_name = str(data.get("timezone") or "Europe/London")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # unknown zone name
        raise ConfigError(f"{here}: unknown timezone {tz_name!r}") from exc

    try:
        party_size = int(data.get("party_size"))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{here}: party_size must be a whole number") from exc
    if party_size < 1:
        raise ConfigError(f"{here}: party_size must be at least 1")

    time_raw = str(data.get("time") or "").strip()
    parts = time_raw.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ConfigError(f"{here}: time must look like \"17:00\"")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ConfigError(f"{here}: time {time_raw!r} is not a real time of day")

    name = _pick_guest_field(
        "guest_name", "GUEST_NAME", data.get("guest_name"), guest_name, here
    )
    phone = _pick_guest_field(
        "guest_phone", "GUEST_PHONE", data.get("guest_phone"), guest_phone, here
    )

    restaurant = data.get("restaurant_name")
    restaurant_name = str(restaurant).strip() if restaurant else None
    if restaurant_name and restaurant_name.lower() in _PLACEHOLDERS:
        restaurant_name = None

    return Facts(
        party_size=party_size,
        time=f"{hour:02d}:{minute:02d}",
        date=_resolve_date(data.get("date"), tz, here),
        timezone=tz_name,
        guest_name=name,
        guest_phone=phone,
        restaurant_name=restaurant_name,
    )


def start_guard(
    facts: Facts, now: datetime | None = None, where: str = "facts.yaml"
) -> str | None:
    """Return a warning if it is too late to start, otherwise None."""
    now = now or datetime.now(facts.tz)
    latest_start = facts.booking_at - START_MARGIN
    if now <= latest_start:
        return None
    return (
        "Refusing to start: there is not enough time left to arrange this booking.\n"
        f"  now:            {now:%Y-%m-%d %H:%M} ({facts.timezone})\n"
        f"  booking:        {facts.booking_at:%Y-%m-%d %H:%M} ({facts.time_human})\n"
        f"  latest start:   {latest_start:%Y-%m-%d %H:%M} "
        f"(booking minus {int(START_MARGIN.total_seconds() // 3600)}h)\n"
        f"Edit the time or date in {where} and run again, "
        "or pass --skip-time-check to start regardless."
    )
