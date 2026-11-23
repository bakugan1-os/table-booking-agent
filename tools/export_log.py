"""Turn the last run's journal into something readable, with numbers masked.

    python tools\\export_log.py              # the last run
    python tools\\export_log.py --all        # everything in the journal
    python tools\\export_log.py --out docs   # somewhere other than submission/

Writes two files:

  submission/decision_log.md   one line per message: time, direction, text,
                               what the agent understood, what it decided
  submission/decisions.jsonl   the same run's raw events, masked

Phone numbers, long digit runs and email addresses are masked in both
(+380*****597). Names are NOT masked - read the files before sharing them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.config import LOG_FILE  # noqa: E402

#: Events that are a message to or from someone.
MESSAGE_EVENTS = {
    "sent": "agent -> manager",
    "received": "manager -> agent",
    "reminder": "agent -> manager",
    "escalation": "agent -> operator",
    "operator_reply": "operator -> agent",
    "operator_unclear": "agent -> operator",
    # voice_received is deliberately absent: the "received" event that
    # follows it carries the same transcript, with voice: true.
}

_NUMBER = re.compile(r"\+?\d[\d\s().-]{5,}\d")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")


def mask(text: str) -> str:
    """+380672006597 -> +380*****597, and emails to a stub."""

    def hide(match: re.Match) -> str:
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 7:  # a time or a short count: leave it alone
            return raw
        head = ("+" if raw.lstrip().startswith("+") else "") + digits[:3]
        stars = "*" * max(3, len(digits) - 6)
        return f"{head}{stars}{digits[-3:]}"

    return _EMAIL.sub("***@***", _NUMBER.sub(hide, text or ""))


def mask_value(value):
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, list):
        return [mask_value(item) for item in value]
    if isinstance(value, dict):
        return {key: mask_value(item) for key, item in value.items()}
    return value


def load_events(path: Path, only_last_run: bool) -> list[dict]:
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not only_last_run:
        return events
    for index in range(len(events) - 1, -1, -1):
        if events[index].get("event") in ("run", "startup"):
            return events[index:]
    return events


def local_time(stamp: str, tz: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone(ZoneInfo(tz)).strftime("%H:%M:%S")
    except Exception:
        return stamp


def one_line(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    text = text.replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--journal", default=str(LOG_FILE), help="journal to read")
    parser.add_argument("--out", default=str(ROOT / "submission"), help="where to write")
    parser.add_argument("--all", action="store_true", help="every run, not just the last")
    parser.add_argument("--tz", default="Europe/London", help="timezone for the times")
    args = parser.parse_args()

    journal = Path(args.journal)
    if not journal.exists():
        print(f"No journal at {journal}. Run the agent first.")
        return 1

    events = load_events(journal, only_last_run=not args.all)
    if not events:
        print("The journal is empty.")
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = out_dir / "decision_log.md"
    masked_journal = out_dir / "decisions.jsonl"

    started = events[0].get("ts", "")
    rows: list[str] = []
    for event in events:
        direction = MESSAGE_EVENTS.get(event.get("event", ""))
        if direction is None:
            continue
        text = event.get("text") or event.get("transcript") or ""
        voice = " (voice)" if event.get("voice") else ""
        rows.append(
            "| {time} | {direction}{voice} | {text} | {understood} | {decision} |".format(
                time=local_time(event.get("ts", ""), args.tz),
                direction=direction,
                voice=voice,
                text=one_line(mask(text)),
                understood=one_line(mask(event.get("understood", "")), 200),
                decision=one_line(mask(event.get("decision", "")), 200),
            )
        )

    outcome = next(
        (event.get("outcome") for event in reversed(events) if event.get("event") == "booking_outcome"),
        "still open",
    )
    header = [
        "# Decision log",
        "",
        f"- run started: {local_time(started, args.tz)} ({args.tz}), {started[:10]}",
        f"- messages: {len(rows)}",
        f"- outcome: {outcome}",
        "",
        "Phone numbers and email addresses are masked. Times are local to "
        f"{args.tz}.",
        "",
        "| time | direction | message | understood | decided |",
        "| --- | --- | --- | --- | --- |",
    ]
    markdown.write_text("\n".join(header + rows) + "\n", encoding="utf-8")

    with masked_journal.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(mask_value(event), ensure_ascii=False) + "\n")

    print(f"{markdown}: {len(rows)} messages, outcome {outcome}")
    print(f"{masked_journal}: {len(events)} events")
    print("Names are not masked - read both files before sharing them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
