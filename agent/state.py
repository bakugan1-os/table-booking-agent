"""The state machine and its persistence in state.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date as date_cls, datetime, timezone
from pathlib import Path

from .config import Facts

STATES = ("greeting", "requesting", "negotiating", "confirmed", "done")
_ORDER = {name: index for index, name in enumerate(STATES)}

# Roles kept in the history that is replayed to the LLM on every call.
ROLE_MANAGER = "manager"
ROLE_AGENT = "agent"
ROLE_OPERATOR = "operator"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Conversation:
    """Everything that must survive a restart."""

    state: str = "greeting"
    greeted: bool = False
    history: list[dict] = field(default_factory=list)
    pending_operator: bool = False
    pending_topic: str | None = None
    pending_change_value: str | None = None
    clarifications: int = 0
    off_topic_count: int = 0
    #: How this ended: "booked", "no_booking", or None while it is still open.
    outcome: str | None = None
    #: The manager's last message was a voice note, so we answer in kind.
    last_manager_voice: bool = False
    #: A restaurant name heard in a voice note is not trusted until the
    #: manager has typed it: Whisper mishears proper nouns.
    restaurant_verified: bool = False
    restaurant_check_sent: bool = False
    resolved_changes: list[str] = field(default_factory=list)
    question_counter: int = 0
    agent_saved_ids: list[int] = field(default_factory=list)
    reminder_sent: bool = False
    phone_requested: bool = False
    last_agent_sent_at: str | None = None
    #: What has actually been agreed: seeded from facts.yaml, and changed
    #: only when the operator approves a change. The recap reads this, never
    #: the facts file, or a table moved to 9.30pm is confirmed as 9pm.
    agreed: dict = field(default_factory=dict)

    # --- what is on the table --------------------------------------------

    def ensure_agreed(self, facts: Facts) -> None:
        """Seed the agreed booking from the facts, keeping anything already set."""
        for key, value in (
            ("party_size", facts.party_size),
            ("time", facts.time),
            ("date", facts.date.isoformat()),
            ("guest_name", facts.guest_name),
            ("restaurant_name", None),
        ):
            self.agreed.setdefault(key, value)

    def booking(self, facts: Facts) -> Facts:
        """The facts as agreed - what the agent should be saying out loud."""
        self.ensure_agreed(facts)
        return replace(
            facts,
            party_size=int(self.agreed["party_size"]),
            time=str(self.agreed["time"]),
            date=date_cls.fromisoformat(str(self.agreed["date"])),
            guest_name=str(self.agreed["guest_name"]),
            restaurant_name=self.agreed.get("restaurant_name"),
        )

    @property
    def restaurant_name(self) -> str | None:
        return self.agreed.get("restaurant_name")

    @restaurant_name.setter
    def restaurant_name(self, value: str | None) -> None:
        """Setting a name counts as having it in writing.

        The one place that is not true is a name heard in a voice note, and
        that caller clears `restaurant_verified` straight afterwards.
        """
        self.agreed["restaurant_name"] = value
        self.restaurant_verified = bool(value)

    def agree(self, key: str, value: object) -> tuple[object, object]:
        """Record an operator-approved change; returns (before, after)."""
        before = self.agreed.get(key)
        self.agreed[key] = value
        return before, value

    # --- history ---------------------------------------------------------

    def add(self, role: str, text: str) -> None:
        self.history.append({"role": role, "text": text, "ts": _now_iso()})

    def manager_batch(self) -> list[str]:
        """Everything the manager has said since our last message."""
        batch: list[str] = []
        for item in reversed(self.history):
            if item["role"] == ROLE_AGENT:
                break
            if item["role"] == ROLE_MANAGER:
                batch.append(item["text"])
        return list(reversed(batch))

    def last_manager_text(self) -> str:
        for item in reversed(self.history):
            if item["role"] == ROLE_MANAGER:
                return item["text"]
        return ""

    def last_role(self) -> str | None:
        return self.history[-1]["role"] if self.history else None

    def mark_sent(self) -> None:
        self.last_agent_sent_at = _now_iso()

    def seconds_since_agent_message(self) -> float | None:
        if not self.last_agent_sent_at:
            return None
        sent = datetime.fromisoformat(self.last_agent_sent_at)
        return (datetime.now(timezone.utc) - sent).total_seconds()

    # --- state machine ---------------------------------------------------

    def next_state(
        self,
        proposed: str,
        *,
        restaurant_known: bool,
        manager_confirmed: bool = False,
        manager_refused: bool = False,
    ) -> str:
        """Validate the LLM's proposal: forward or stay, never backwards.

        'confirmed' needs the restaurant's name AND the manager actually
        saying the booking is made - a free table is not a booking. 'done' is
        only reachable from 'confirmed'.
        """
        # A refusal ends the conversation wherever it finds it: there is
        # nothing left to negotiate.
        if manager_refused and not manager_confirmed:
            return "done"
        # A confirmed booking always goes through 'confirmed', whatever the
        # model proposed: skipping straight to 'done' would swallow the recap.
        if (
            manager_confirmed
            and restaurant_known
            and _ORDER[self.state] < _ORDER["confirmed"]
        ):
            return "confirmed"
        if proposed not in _ORDER:
            return self.state
        if _ORDER[proposed] <= _ORDER[self.state]:
            return self.state
        if proposed == "confirmed" and not (restaurant_known and manager_confirmed):
            return "negotiating" if _ORDER[self.state] < _ORDER["negotiating"] else self.state
        if proposed == "done" and self.state != "confirmed":
            return self.state
        return proposed

    # --- persistence -----------------------------------------------------

    def save(self, path: Path) -> None:
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Conversation | None":
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not read {path.name} ({exc}); starting a new conversation.")
            return None
        known = {f for f in cls.__dataclass_fields__}
        convo = cls(**{k: v for k, v in data.items() if k in known})
        # state.json written before the agreed block existed
        legacy_restaurant = data.get("restaurant_name")
        if legacy_restaurant and not convo.agreed.get("restaurant_name"):
            convo.agreed["restaurant_name"] = legacy_restaurant
        # ...and before voice, when every name came from a typed message
        if convo.agreed.get("restaurant_name") and "restaurant_verified" not in data:
            convo.restaurant_verified = True
        if convo.state not in _ORDER:
            convo.state = "greeting"
        return convo
