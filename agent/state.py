"""The state machine and its persistence in state.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

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
    resolved_changes: list[str] = field(default_factory=list)
    question_counter: int = 0
    agent_saved_ids: list[int] = field(default_factory=list)
    reminder_sent: bool = False
    phone_requested: bool = False
    restaurant_name: str | None = None
    last_agent_sent_at: str | None = None

    # --- history ---------------------------------------------------------

    def add(self, role: str, text: str) -> None:
        self.history.append({"role": role, "text": text, "ts": _now_iso()})

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
        self, proposed: str, *, restaurant_known: bool, manager_confirmed: bool = False
    ) -> str:
        """Validate the LLM's proposal: forward or stay, never backwards.

        'confirmed' needs the restaurant's name AND the manager actually
        saying the booking is made - a free table is not a booking. 'done' is
        only reachable from 'confirmed'.
        """
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
        if convo.state not in _ORDER:
            convo.state = "greeting"
        return convo
