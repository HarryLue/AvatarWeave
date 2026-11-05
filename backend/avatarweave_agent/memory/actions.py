"""Private v2 motion bridge; only the bound RTC participant can report progress."""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from .action_protocol import (
    BOOTSTRAP_TOPIC,
    COMMAND_TOPIC_V2,
    HELLO_TOPIC,
    RECEIPT_TOPIC,
    ActionEnvelope,
    ActionReceipt,
    ClientHello,
    SessionBootstrap,
)
from .contracts import ClientCapabilities, OutputInput, OutputObservation, OutputPlan
from .execution import MemoryFault
from .http import meta
from .tables import Generation

logger = logging.getLogger(__name__)


class PrivateActions:
    def __init__(self, ctx, conversation, avatar, *, ready=True, on_connection_ready=None):
        self.ctx, self.conversation, self.avatar = ctx, conversation, avatar
        self.capabilities = ClientCapabilities.model_validate_json(
            conversation.session.capabilities_json
        )
        self.queue = asyncio.Queue(maxsize=32)
        self.plans = {}
        self.sequence = 0
        self.cancelled = set()
        self.hello_nonce = None
        self.hello_floor = 0
        self._on_connection_ready = on_connection_ready
        self._announced_nonce = None
        self._ready = asyncio.Event()
        if ready:
            self._ready.set()
        self.task = asyncio.create_task(self._consume())
        self.ctx.room.on("data_received", self._received)

    def mark_ready(self):
        self._ready.set()

    def _received(self, packet):
        if (
            packet.topic not in {RECEIPT_TOPIC, HELLO_TOPIC}
            or not packet.participant
            or packet.participant.identity != self.conversation.session.participant
        ):
            return
        if len(packet.data) > 4096:
            return
        try:
            receipt = (
                ClientHello if packet.topic == HELLO_TOPIC else ActionReceipt
            ).model_validate_json(packet.data)
            if receipt.session_id != self.conversation.session.id:
                return
            self.queue.put_nowait(receipt)
        except (ValidationError, asyncio.QueueFull):
            return

    async def _consume(self):
        while True:
            try:
                await self._expire()
            except SQLAlchemyError:
                pass
            try:
                receipt = await asyncio.wait_for(self.queue.get(), 5)
            except TimeoutError:
                continue
            try:
                if isinstance(receipt, ClientHello):
                    await self._ready.wait()
                    await self.bootstrap(receipt)
                    continue
                saved = self.plans.get(receipt.command_id)
                if not saved:
                    continue
                plan, fence = saved
                result = await self.conversation.service.output(
                    OutputInput(
                        meta=meta(self.conversation.service),
                        scope=self.conversation.scope,
                        fence=fence,
                        operation="observe",
                        plan=plan,
                        capabilities=self.capabilities,
                        observations=[
                            OutputObservation(
                                observation_id=uuid4().hex,
                                output_id=receipt.command_id,
                                generation_id=receipt.generation_id,
                                observed_at=receipt.observed_at,
                                received_at=self.conversation.service.clock(),
                                client_sequence=receipt.client_sequence,
                                state=receipt.state,
                                source="renderer",
                                segment_id=None,
                                position_ms=None,
                                media_clock_id=None,
                            )
                        ],
                    )
                )
                if result.data and result.data.state in {
                    "completed",
                    "failed",
                    "cancelled",
                    "unknown",
                }:
                    self.plans.pop(receipt.command_id, None)
            except (MemoryFault, SQLAlchemyError):
                if isinstance(receipt, ActionReceipt):
                    self.plans.pop(receipt.command_id, None)
            except Exception as error:  # noqa: BLE001 -- bounded client retries recover transient RTC failures
                logger.warning(
                    "private_receipt_failed", extra={"exception_type": type(error).__name__}
                )
            finally:
                self.queue.task_done()

    async def _expire(self):
        from sqlalchemy import update

        from .tables import OutputLedger

        now = self.conversation.service.clock()
        expired = [key for key, (plan, _) in self.plans.items() if plan.expires_at <= now]
        if not expired:
            return
        async with self.conversation.service.database.sessions.begin() as db:
            await db.execute(
                update(OutputLedger)
                .where(
                    OutputLedger.id.in_(expired),
                    OutputLedger.account_id == self.conversation.scope.user_id,
                    OutputLedger.state.in_(
                        ["planned", "published", "accepted", "started", "partial"]
                    ),
                )
                .values(state="unknown")
            )
        for key in expired:
            self.plans.pop(key, None)

    async def bootstrap(self, hello):
        conversation = self.conversation
        new_connection = self.hello_nonce != hello.nonce
        if self.hello_nonce is not None and new_connection:
            await conversation.cancel()
            await self.cancel()
        async with conversation.service.database.sessions() as db:
            await conversation.service._account(db, conversation.scope)
            session = await conversation.service._session(
                db, conversation.scope, conversation.session.id
            )
            if session.owner_epoch != conversation.owner_epoch or session.status != "active":
                raise MemoryFault("stale_generation", "Bootstrap owner changed")
            self.capabilities = ClientCapabilities.model_validate_json(session.capabilities_json)
            current = await db.get(Generation, session.id)
            serial = current.serial if current else 0
        self.hello_nonce = hello.nonce
        if new_connection:
            self.hello_floor = self.sequence
        if new_connection and self.plans:
            from sqlalchemy import update

            from .tables import OutputLedger

            async with conversation.service.database.sessions.begin() as db:
                await db.execute(
                    update(OutputLedger)
                    .where(
                        OutputLedger.account_id == conversation.scope.user_id,
                        OutputLedger.id.in_(list(self.plans)),
                        OutputLedger.state.in_(["planned", "published", "started", "partial"]),
                    )
                    .values(state="unknown")
                )
        if new_connection:
            self.plans.clear()
        envelope = SessionBootstrap(
            version=1,
            session_id=conversation.session.id,
            nonce=hello.nonce,
            owner_epoch=conversation.owner_epoch,
            generation_sequence=serial,
            sequence_floor=self.hello_floor,
            expires_at=conversation.service.clock() + timedelta(seconds=10),
        )
        await self.ctx.room.local_participant.publish_data(
            envelope.model_dump_json().encode(), reliable=True, topic=BOOTSTRAP_TOPIC
        )
        if self._on_connection_ready and self._announced_nonce != hello.nonce:
            self._on_connection_ready()
            self._announced_nonce = hello.nonce

    async def publish(self, action_id, *, generation_id=None, fence=None):
        conversation = self.conversation
        await self._expire()
        if self.hello_nonce is None:
            return "设备还在连接，请稍后再试。"
        if 2 not in self.capabilities.command_versions or not self.capabilities.action_cancel:
            return "当前设备暂不支持这个动作。"
        action_id = action_id or self.avatar.default_action
        if action_id not in self.avatar.actions:
            return "这个动作没有配置。"
        action = self.avatar.actions[action_id].command()
        if action["type"] != "motion":
            return "这个动作暂不可用，请试试其他动作。"
        if len(self.plans) >= 16:
            return "正在处理其他动作，请稍后再试。"
        generation, fence = generation_id or conversation.generation_id, fence or conversation.fence
        await conversation.check(generation, fence)
        async with conversation.service.database.sessions() as db:
            current = await db.get(Generation, conversation.session.id)
            if (
                current is None
                or current.generation_id != generation
                or current.owner_epoch != conversation.owner_epoch
            ):
                raise MemoryFault("stale_generation", "Action generation changed")
            serial = current.serial
        self.sequence += 1
        plan = OutputPlan(
            output_id=uuid4().hex,
            session_id=conversation.session.id,
            turn_id=conversation.current_event.turn_id,
            generation_id=generation,
            speech_id=None,
            spoken_text=None,
            action_ids=[action_id],
            sequence=self.sequence,
            expires_at=conversation.service.clock() + timedelta(seconds=30),
        )
        request = OutputInput(
            meta=meta(conversation.service),
            scope=conversation.scope,
            fence=fence,
            operation="plan",
            plan=plan,
            capabilities=self.capabilities,
            observations=[],
        )
        for operation in ["plan", "publish"]:
            result = await conversation.service.output(
                request.model_copy(update={"operation": operation})
            )
            if result.error:
                raise MemoryFault(result.error.code, result.error.message)
        self.plans[plan.output_id] = (plan, fence)
        envelope = ActionEnvelope(
            version=2,
            command_id=plan.output_id,
            connection_nonce=self.hello_nonce,
            session_id=plan.session_id,
            generation_id=generation,
            owner_epoch=fence.owner_epoch,
            generation_sequence=serial,
            sequence=self.sequence,
            expires_at=plan.expires_at,
            operation="execute",
            action=action,
        )
        await self.ctx.room.local_participant.set_attributes(
            {
                "avatarweave.session_ref": conversation.session.id,
                "avatarweave.owner_epoch": str(fence.owner_epoch),
                "avatarweave.generation_sequence": str(serial),
            }
        )
        await self.ctx.room.local_participant.publish_data(
            envelope.model_dump_json().encode(), reliable=True, topic=COMMAND_TOPIC_V2
        )
        return "动作请求已发出，完成情况还未确认。"

    async def cancel(self):
        conversation = self.conversation
        generation = conversation.generation_id
        if not generation or generation in self.cancelled or self.hello_nonce is None:
            return
        async with conversation.service.database.sessions() as db:
            current = await db.get(Generation, conversation.session.id)
            if (
                current is None
                or current.generation_id != generation
                or current.owner_epoch != conversation.owner_epoch
            ):
                return
            serial = current.serial
        self.plans = {
            key: value for key, value in self.plans.items() if value[0].generation_id != generation
        }
        self.cancelled.add(generation)
        if len(self.cancelled) > 64:
            self.cancelled.pop()
        envelope = ActionEnvelope(
            version=2,
            command_id=uuid4().hex,
            connection_nonce=self.hello_nonce,
            session_id=conversation.session.id,
            generation_id=generation,
            owner_epoch=conversation.owner_epoch,
            generation_sequence=serial,
            sequence=self.sequence + 1,
            expires_at=conversation.service.clock() + timedelta(seconds=30),
            operation="cancel",
            action=None,
        )
        await self.ctx.room.local_participant.publish_data(
            envelope.model_dump_json().encode(), reliable=True, topic=COMMAND_TOPIC_V2
        )

    async def close(self):
        self.ctx.room.off("data_received", self._received)
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
