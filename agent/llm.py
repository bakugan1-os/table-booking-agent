"""The LLM call: full history in, one validated JSON decision out."""

from __future__ import annotations

import json
from dataclasses import dataclass

from openai import OpenAI

from .config import Settings
from .journal import Journal
from .prompts import RETRY_NUDGE
from .state import ROLE_AGENT, ROLE_MANAGER, STATES

CHANGES = ("time", "party_size", "deposit", "payment", "none")
_NULLISH = {"", "null", "none", "unknown", "n/a"}


@dataclass
class Decision:
    reply: str
    understood: str
    decision: str
    next_state: str
    needs_operator: bool
    off_topic: bool
    requested_change: str
    asked_if_bot: bool
    manager_confirmed: bool
    restaurant_name: str | None

    def as_log(self) -> dict:
        return {
            "understood": self.understood,
            "decision": self.decision,
            "next_state": self.next_state,
            "needs_operator": self.needs_operator,
            "off_topic": self.off_topic,
            "requested_change": self.requested_change,
            "asked_if_bot": self.asked_if_bot,
            "manager_confirmed": self.manager_confirmed,
        }


def _extract_json(raw: str) -> dict | None:
    """Pull the first balanced {...} out of the reply, ignoring any wrapping."""
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        char = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(raw[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "yes", "1")


def _validate(data: dict | None) -> tuple[Decision | None, str]:
    if data is None:
        return None, "no JSON object found"

    reply = str(data.get("reply") or "").strip()
    if not reply:
        return None, "empty reply"

    next_state = str(data.get("next_state") or "").strip().lower()
    if next_state not in STATES:
        return None, f"bad next_state {next_state!r}"

    change = str(data.get("requested_change") or "none").strip().lower()
    if change not in CHANGES:
        change = "none"

    raw_name = data.get("restaurant_name")
    name = str(raw_name).strip() if raw_name else ""
    restaurant_name = name[:80] if name.lower() not in _NULLISH else None

    return (
        Decision(
            reply=reply,
            understood=str(data.get("understood") or "").strip()[:300],
            decision=str(data.get("decision") or "").strip()[:300],
            next_state=next_state,
            needs_operator=_as_bool(data.get("needs_operator")),
            off_topic=_as_bool(data.get("off_topic")),
            requested_change=change,
            asked_if_bot=_as_bool(data.get("asked_if_bot")),
            manager_confirmed=_as_bool(data.get("manager_confirmed")),
            restaurant_name=restaurant_name,
        ),
        "",
    )


def history_to_messages(history: list[dict]) -> list[dict]:
    """Replay the whole conversation; operator notes are marked as trusted."""
    messages: list[dict] = []
    for item in history:
        text = item.get("text") or ""
        role = item.get("role")
        if role == ROLE_AGENT:
            messages.append({"role": "assistant", "content": text})
        elif role == ROLE_MANAGER:
            messages.append({"role": "user", "content": text})
        else:  # operator
            messages.append({"role": "user", "content": f"[OPERATOR] {text}"})
    return messages


class Brain:
    """One LLM call per turn, with a single retry on malformed JSON."""

    def __init__(self, settings: Settings, journal: Journal) -> None:
        self.model = settings.llm_model
        self.journal = journal
        self._client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=60.0,
            max_retries=1,
        )

    def _complete(self, messages: list[dict]) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.3,
            max_tokens=500,
        )
        return response.choices[0].message.content or ""

    def decide(self, system_prompt: str, history: list[dict]) -> Decision | None:
        """Returns None when the model fails twice; the caller then escalates."""
        messages = [{"role": "system", "content": system_prompt}]
        messages += history_to_messages(history)

        for attempt in (1, 2):
            try:
                raw = self._complete(messages)
            except Exception as exc:  # network, auth, rate limit
                self.journal.write("llm_error", attempt=attempt, error=str(exc)[:300])
                return None

            decision, problem = _validate(_extract_json(raw))
            if decision is not None:
                return decision

            self.journal.write(
                "llm_invalid", attempt=attempt, problem=problem, raw=raw[:400]
            )
            messages = messages + [
                {"role": "assistant", "content": raw[:1000]},
                {"role": "user", "content": RETRY_NUDGE},
            ]
        return None
