"""Telethon wiring: debounce, state machine, escalation, reminder."""

from __future__ import annotations

import asyncio

from telethon import TelegramClient, events

from . import guardrails
from .config import STATE_FILE, Facts, Settings, greeting_word, human_time
from .journal import Journal
from .llm import Brain, Decision
from .prompts import OPEN_CONVERSATION, build_system_prompt
from .state import ROLE_AGENT, ROLE_MANAGER, ROLE_OPERATOR, Conversation

DEBOUNCE_SECONDS = 5
REMINDER_AFTER_SECONDS = 10 * 60
WATCHDOG_TICK_SECONDS = 20
MAX_CLARIFICATIONS = 2


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

        self.convo.ensure_agreed(facts)
        self.manager = None
        self.me_id: int | None = None
        self._debounce: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
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

        # Keep the reference: a bare create_task may be garbage collected.
        self._watchdog_task = asyncio.create_task(self._watchdog())
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
            verdict = guardrails.operator_verdict(text)
            if verdict == "unclear":
                # Never guess what the operator meant: ask again and keep waiting.
                await self._ask_operator_again(topic)
                return
            if topic not in self.convo.resolved_changes:
                self.convo.resolved_changes.append(topic)
            if verdict == "yes":
                self._apply_approved_change(topic, text)
            else:
                self.convo.add(
                    ROLE_OPERATOR,
                    f"Declined: do not agree to that change of {topic.replace('_', ' ')}. "
                    "Keep to what is in KNOWN FACTS.",
                )
                self.journal.write(
                    "change_declined",
                    topic=topic,
                    value=self.convo.pending_change_value,
                    operator=text,
                )

        self.convo.pending_operator = False
        self.convo.pending_topic = None
        self.convo.pending_change_value = None
        self.convo.clarifications = 0
        self.convo.reminder_sent = False
        self._save()

        await self._respond()

    async def _ask_operator_again(self, topic: str) -> None:
        """One more try at a yes or no; after that we simply keep waiting."""
        value = self.convo.pending_change_value
        self.convo.clarifications += 1
        if self.convo.clarifications > MAX_CLARIFICATIONS:
            self.journal.write(
                "operator_unclear",
                level="warning",
                topic=topic,
                value=value,
                note="asked twice already, waiting quietly",
            )
            self._save()
            return

        question = f"[Q{self.convo.question_counter}] {guardrails.clarify_line(topic, value)}"
        message = await self.client.send_message("me", question)
        self.convo.agent_saved_ids.append(message.id)
        self._save()
        self.journal.write(
            "operator_unclear",
            level="warning",
            direction="out",
            chat="saved",
            topic=topic,
            value=value,
            text=question,
            attempt=self.convo.clarifications,
        )
        print(f"Operator's answer was unclear; asked again about {topic}.")

    def _apply_approved_change(self, topic: str, operator_text: str) -> None:
        """The agreed booking moves only here, and only on a clear yes."""
        value = self.convo.pending_change_value
        if not value:
            self.journal.write(
                "change_not_applied", topic=topic, reason="no value to apply"
            )
            return

        if topic == "time":
            parsed = guardrails.parse_clock(value)
            key = "time"
        elif topic == "party_size":
            parsed = guardrails.parse_party_size(value)
            key = "party_size"
        else:  # deposit, payment: nothing in the booking itself moves
            self.journal.write("change_approved", topic=topic, value=value)
            return

        if parsed is None:
            self.journal.write(
                "change_not_applied",
                level="warning",
                topic=topic,
                value=value,
                reason="could not read the value",
            )
            return

        before, after = self.convo.agree(key, parsed)
        spoken = human_time(str(after)) if key == "time" else str(after)
        # Spell the approval out for the model, or it asks for the same
        # change again on the next turn and the escalations loop.
        self.convo.add(
            ROLE_OPERATOR,
            f"Approved: {key.replace('_', ' ')} is now {spoken}. Do not ask me "
            "again about it. Tell the manager you are happy with it and ask "
            f"them to book it under the name {self.convo.agreed['guest_name']}.",
        )
        self.journal.write(
            "change_approved",
            topic=topic,
            field=key,
            **{"from": before, "to": after},
            operator=operator_text,
        )
        print(f"Operator approved a change: {key} {before} -> {after}.")

    def _record_outcome(self, new_state: str, decision: Decision, last_manager: str) -> None:
        """Write down how this ended, once, the first time we know."""
        if self.convo.outcome:
            return
        if new_state == "confirmed":
            outcome = "booked"
        elif new_state == "done":
            outcome = "no_booking"
        else:
            return

        self.convo.outcome = outcome
        self.journal.write(
            "booking_outcome",
            outcome=outcome,
            state=new_state,
            understood=decision.understood,
            manager=last_manager,
            booking=dict(self.convo.agreed),
        )
        print(f"Outcome: {outcome}.")

    def _already_agreed(self, decision: Decision) -> bool:
        """True when the model is asking to approve what is already agreed."""
        topic = decision.requested_change
        if topic not in ("time", "party_size") or not decision.change_value:
            return False
        if topic == "time":
            parsed = guardrails.parse_clock(decision.change_value)
        else:
            parsed = guardrails.parse_party_size(decision.change_value)
        if parsed is None:
            return False
        return str(parsed) == str(self.convo.agreed.get(topic))

    # --- the turn --------------------------------------------------------

    async def _respond(self) -> None:
        async with self._lock:
            # Worked out here, not by the model: London's clock decides this.
            greeting = greeting_word(self.facts.tz)
            booking = self.convo.booking(self.facts)
            system_prompt = build_system_prompt(
                booking, self.convo.state, booking.restaurant_name, greeting
            )
            history = list(self.convo.history)

            async with self.client.action(self.manager, "typing"):
                decision = await asyncio.to_thread(
                    self.brain.decide, system_prompt, history
                )

            batch = self.convo.manager_batch()
            last_manager = "\n".join(batch) or self.convo.last_manager_text()

            if decision is None:
                await self._send(guardrails.holding_line())
                await self._escalate(
                    "invalid_output",
                    "The model did not return usable output twice in a row.",
                    batch,
                )
                return

            if decision.restaurant_name and not self.convo.restaurant_name:
                self.convo.restaurant_name = decision.restaurant_name

            # Flag a disagreement for the log; never overrule the model here.
            markers = guardrails.confirmation_markers(last_manager)
            if markers and not decision.manager_confirmed:
                self.journal.write(
                    "confirmation_mismatch",
                    level="warning",
                    markers=markers,
                    text=last_manager,
                    state=self.convo.state,
                    understood=decision.understood,
                )

            secret = guardrails.asks_for_secrets(last_manager)
            payment = guardrails.mentions_payment(last_manager) and not (
                {"deposit", "payment"} & set(self.convo.resolved_changes)
            )
            change = decision.requested_change
            if change == "none" or change in self.convo.resolved_changes:
                change = ""

            # The model does not always notice that its request was granted.
            # Asking the operator to approve what is already agreed is how the
            # live run ended up in an escalation loop.
            settled = self._already_agreed(decision)
            if settled:
                change = ""
                self.journal.write(
                    "escalation_skipped",
                    topic=decision.requested_change,
                    value=decision.change_value,
                    reason="already agreed",
                )

            # 1. Off topic, including role-change attempts.
            if decision.off_topic and not secret:
                line = guardrails.off_topic_line(booking, self.convo.off_topic_count)
                self.convo.off_topic_count += 1
                await self._send(line, decision=decision, note="off_topic")
                return

            # 2. "Are you a bot?" - honest, and the wording comes from the code.
            if decision.asked_if_bot or guardrails.asks_if_bot(last_manager):
                await self._send(
                    guardrails.bot_disclosure_line(booking),
                    decision=decision,
                    note="bot_disclosure",
                )
                return

            # 3. Anything needing the operator's say-so.
            if (decision.needs_operator and not settled) or change or payment or secret:
                topic = (
                    "secret"
                    if secret
                    else change or ("payment" if payment else "other")
                )
                await self._send(
                    guardrails.holding_line(), decision=decision, note="holding"
                )
                await self._escalate(topic, decision.understood, batch, decision)
                return

            # 4. Ordinary turn.
            new_state = self.convo.next_state(
                decision.next_state,
                restaurant_known=bool(self.convo.restaurant_name),
                manager_confirmed=decision.manager_confirmed,
                manager_refused=decision.manager_refused,
            )
            self._record_outcome(new_state, decision, last_manager)
            recap = new_state == "confirmed" and self.convo.state != "confirmed"
            if recap:
                # Do not confirm a booking the manager described differently.
                clash = guardrails.conflicting_values(last_manager, booking)
                if clash:
                    self.journal.write(
                        "recap_withheld", level="warning", conflicts=clash, text=last_manager
                    )
                    await self._send(
                        guardrails.holding_line(), decision=decision, note="holding"
                    )
                    await self._escalate(
                        "time_mismatch",
                        "The manager's confirmation does not match what we agreed: "
                        + "; ".join(clash),
                        last_manager,
                        decision,
                    )
                    return
                # Built from what was agreed, not from facts.yaml: a table
                # moved to 9.30pm must not be confirmed back as 9pm.
                reply = guardrails.recap_line(booking)
                missing = guardrails.recap_missing(reply, booking)
                if missing:
                    self.journal.write(
                        "recap_incomplete", level="warning", missing=missing, text=reply
                    )
            else:
                reply = guardrails.sanitize(
                    decision.reply,
                    allow_phone=self.convo.phone_requested,
                    greeting=greeting,
                    booking=booking.time,
                )
                # The agent may not offer the manager anything the operator
                # has not approved.
                clash = guardrails.conflicting_values(reply, booking)
                if clash:
                    self.journal.write(
                        "reply_blocked", level="warning", conflicts=clash, text=reply
                    )
                    await self._send(
                        guardrails.holding_line(), decision=decision, note="holding"
                    )
                    await self._escalate(
                        "reply_conflict",
                        "The drafted reply offered something we have not agreed: "
                        + "; ".join(clash),
                        last_manager,
                        decision,
                    )
                    return

            if not reply:
                await self._send(guardrails.holding_line(), decision=decision)
                await self._escalate(
                    "other",
                    "The drafted reply was blocked by the guardrails.",
                    last_manager,
                    decision,
                )
                return

            await self._send(
                reply,
                decision=decision,
                new_state=new_state,
                note="recap" if recap else None,
            )

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
        messages: list[str] | str,
        decision: Decision | None = None,
    ) -> None:
        self.convo.question_counter += 1
        tag = f"[Q{self.convo.question_counter}]"
        value = decision.change_value if decision else None
        proposed = f" ({value})" if value else ""

        batch = [messages] if isinstance(messages, str) else list(messages)
        batch = [line for line in batch if line]
        if not batch:
            said = "Manager said: (nothing yet)"
        elif len(batch) == 1:
            said = f"Manager said: {batch[0]}"
        else:
            # Show the whole burst: the last line alone loses the context.
            said = "Manager said:\n" + "\n".join(f"- {line}" for line in batch)

        footer = "Reply in this chat and I will carry on."
        if topic in ("time", "party_size", "deposit", "payment"):
            footer += " Say yes to approve the change."

        body = (
            f"{tag} {question or 'I need your decision before replying.'}\n\n"
            f"{said}\n"
            f"Topic: {topic}{proposed} | state: {self.convo.state}\n\n"
            f"{footer}"
        )
        message = await self.client.send_message("me", body)

        self.convo.agent_saved_ids.append(message.id)
        self.convo.pending_operator = True
        self.convo.pending_topic = topic
        self.convo.pending_change_value = value
        self._save()

        fields = {
            "direction": "out",
            "chat": "saved",
            "tag": tag,
            "topic": topic,
            "value": value,
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
                    guardrails.reminder_line(self.convo.booking(self.facts)),
                    note="reminder",
                    event="reminder",
                )
                self.convo.reminder_sent = True
                self._save()

    # --- helpers ---------------------------------------------------------

    def _save(self) -> None:
        self.convo.save(STATE_FILE)
