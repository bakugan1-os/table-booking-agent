"""Telethon wiring: debounce, state machine, escalation, reminder."""

from __future__ import annotations

import asyncio

from telethon import TelegramClient, events

from . import guardrails
from .config import STATE_FILE, Facts, Settings
from .journal import Journal
from .llm import Brain, Decision
from .prompts import OPEN_CONVERSATION, build_system_prompt
from .state import ROLE_AGENT, ROLE_MANAGER, ROLE_OPERATOR, Conversation

DEBOUNCE_SECONDS = 5
REMINDER_AFTER_SECONDS = 10 * 60
WATCHDOG_TICK_SECONDS = 20


class BookingAgent:
    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        facts: Facts,
        convo: Conversation,
        brain: Brain,
        journal: Journal,
    ) -> None:
        self.client = client
        self.settings = settings
        self.facts = facts
        self.convo = convo
        self.brain = brain
        self.journal = journal

        self.manager = None
        self.me_id: int | None = None
        self._debounce: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # --- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        me = await self.client.get_me()
        self.me_id = me.id
        self.manager = await self.client.get_entity(self.settings.manager_username)

        self.journal.write(
            "startup",
            state=self.convo.state,
            manager=self.settings.manager_username,
            manager_id=getattr(self.manager, "id", None),
            resumed=self.convo.greeted,
            model=self.brain.model,
        )

        # Only two chats are ever listened to: the manager's and Saved Messages.
        self.client.add_event_handler(
            self._on_manager,
            events.NewMessage(from_users=self.manager.id, incoming=True),
        )
        self.client.add_event_handler(
            self._on_saved, events.NewMessage(chats=self.me_id)
        )

        print(
            f"Agent running as @{getattr(me, 'username', me.id)}; "
            f"manager @{self.settings.manager_username}; state: {self.convo.state}."
        )
        print("Escalations arrive in Saved Messages; reply there to continue.")

        if not self.convo.greeted:
            await self._open_conversation()
        else:
            print("Resumed an existing conversation: not greeting again.")

        asyncio.create_task(self._watchdog())
        await self.client.run_until_disconnected()

    async def _open_conversation(self) -> None:
        self.convo.greeted = True
        self.convo.add(ROLE_OPERATOR, OPEN_CONVERSATION)
        self._save()
        await self._respond()

    # --- incoming --------------------------------------------------------

    async def _on_manager(self, event) -> None:
        if not event.is_private:
            return
        text = (event.raw_text or "").strip()
        if not text:
            # Phase 1 is text only: attachments are logged and ignored, never opened.
            self.journal.write(
                "received_ignored", reason="non-text message", state=self.convo.state
            )
            return

        self.convo.add(ROLE_MANAGER, text)
        self.convo.reminder_sent = False
        if guardrails.wants_phone(text):
            self.convo.phone_requested = True
        self._save()
        self.journal.write(
            "received", direction="in", chat="manager", text=text, state=self.convo.state
        )

        if self.convo.pending_operator:
            self.journal.write("buffered", reason="awaiting operator", text=text)
            return

        if self._debounce and not self._debounce.done():
            self._debounce.cancel()
        self._debounce = asyncio.create_task(self._debounced_respond())

    async def _debounced_respond(self) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        await self._respond()

    async def _on_saved(self, event) -> None:
        """A message in Saved Messages that the agent did not send is the operator."""
        if event.id in self.convo.agent_saved_ids:
            return
        text = (event.raw_text or "").strip()
        if not text:
            return

        self.convo.add(ROLE_OPERATOR, text)
        self.journal.write(
            "operator_reply",
            direction="in",
            chat="saved",
            text=text,
            state=self.convo.state,
            topic=self.convo.pending_topic,
            awaited=self.convo.pending_operator,
        )

        if not self.convo.pending_operator:
            # Not a reply to a question: keep it as context, do not message anyone.
            self._save()
            return

        topic = self.convo.pending_topic
        if topic and topic not in ("other", "secret", "invalid_output"):
            if topic not in self.convo.resolved_changes:
                self.convo.resolved_changes.append(topic)
        self.convo.pending_operator = False
        self.convo.pending_topic = None
        self.convo.reminder_sent = False
        self._save()

        await self._respond()

    # --- the turn --------------------------------------------------------

    async def _respond(self) -> None:
        async with self._lock:
            system_prompt = build_system_prompt(
                self.facts, self.convo.state, self.convo.restaurant_name
            )
            history = list(self.convo.history)

            async with self.client.action(self.manager, "typing"):
                decision = await asyncio.to_thread(
                    self.brain.decide, system_prompt, history
                )

            last_manager = self.convo.last_manager_text()

            if decision is None:
                await self._send(guardrails.holding_line())
                await self._escalate(
                    "invalid_output",
                    "The model did not return usable output twice in a row.",
                    last_manager,
                )
                return

            if decision.restaurant_name and not self.convo.restaurant_name:
                self.convo.restaurant_name = decision.restaurant_name

            secret = guardrails.asks_for_secrets(last_manager)
            payment = guardrails.mentions_payment(last_manager) and not (
                {"deposit", "payment"} & set(self.convo.resolved_changes)
            )
            change = decision.requested_change
            if change == "none" or change in self.convo.resolved_changes:
                change = ""

            # 1. Off topic, including role-change attempts.
            if decision.off_topic and not secret:
                await self._send(
                    guardrails.off_topic_line(self.facts),
                    decision=decision,
                    note="off_topic",
                )
                return

            # 2. "Are you a bot?" - honest, and the wording comes from the code.
            if decision.asked_if_bot or guardrails.asks_if_bot(last_manager):
                await self._send(
                    guardrails.bot_disclosure_line(self.facts),
                    decision=decision,
                    note="bot_disclosure",
                )
                return

            # 3. Anything needing the operator's say-so.
            if decision.needs_operator or change or payment or secret:
                topic = (
                    "secret"
                    if secret
                    else change or ("payment" if payment else "other")
                )
                await self._send(
                    guardrails.holding_line(), decision=decision, note="holding"
                )
                await self._escalate(topic, decision.understood, last_manager, decision)
                return

            # 4. Ordinary turn.
            new_state = self.convo.next_state(
                decision.next_state, restaurant_known=bool(self.convo.restaurant_name)
            )
            if new_state == "confirmed" and self.convo.state != "confirmed":
                reply = guardrails.recap_line(self.facts, self.convo.restaurant_name)
            else:
                reply = guardrails.sanitize(
                    decision.reply, allow_phone=self.convo.phone_requested
                )

            if not reply:
                await self._send(guardrails.holding_line(), decision=decision)
                await self._escalate(
                    "other",
                    "The drafted reply was blocked by the guardrails.",
                    last_manager,
                    decision,
                )
                return

            await self._send(reply, decision=decision, new_state=new_state)

    # --- outgoing --------------------------------------------------------

    async def _send(
        self,
        text: str,
        *,
        decision: Decision | None = None,
        new_state: str | None = None,
        note: str | None = None,
        event: str = "sent",
    ) -> None:
        await self.client.send_message(self.manager, text)
        self.convo.add(ROLE_AGENT, text)
        self.convo.mark_sent()
        self.convo.reminder_sent = False
        if new_state and new_state != self.convo.state:
            self.journal.write(
                "state_change", **{"from": self.convo.state, "to": new_state}
            )
            self.convo.state = new_state
        self._save()

        fields = {
            "direction": "out",
            "chat": "manager",
            "text": text,
            "state": self.convo.state,
            "note": note,
        }
        if decision is not None:
            fields.update(decision.as_log())
        self.journal.write(event, **fields)

    async def _escalate(
        self,
        topic: str,
        question: str,
        last_manager: str,
        decision: Decision | None = None,
    ) -> None:
        self.convo.question_counter += 1
        tag = f"[Q{self.convo.question_counter}]"
        body = (
            f"{tag} {question or 'I need your decision before replying.'}\n\n"
            f"Manager said: {last_manager or '(nothing yet)'}\n"
            f"Topic: {topic} | state: {self.convo.state}\n\n"
            "Reply in this chat and I will carry on."
        )
        message = await self.client.send_message("me", body)

        self.convo.agent_saved_ids.append(message.id)
        self.convo.pending_operator = True
        self.convo.pending_topic = topic
        self._save()

        fields = {
            "direction": "out",
            "chat": "saved",
            "tag": tag,
            "topic": topic,
            "text": body,
            "state": self.convo.state,
        }
        if decision is not None:
            fields.update(decision.as_log())
        self.journal.write("escalation", **fields)
        print(f"Escalated to Saved Messages: {tag} ({topic}).")

    # --- one polite nudge after 10 minutes of silence ---------------------

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_TICK_SECONDS)
            if self.convo.reminder_sent or self.convo.pending_operator:
                continue
            if self.convo.state in ("confirmed", "done"):
                continue
            if self.convo.last_role() != ROLE_AGENT:
                continue
            elapsed = self.convo.seconds_since_agent_message()
            if elapsed is None or elapsed < REMINDER_AFTER_SECONDS:
                continue
            async with self._lock:
                await self._send(
                    guardrails.reminder_line(self.facts),
                    note="reminder",
                    event="reminder",
                )
                self.convo.reminder_sent = True
                self._save()

    # --- helpers ---------------------------------------------------------

    def _save(self) -> None:
        self.convo.save(STATE_FILE)
