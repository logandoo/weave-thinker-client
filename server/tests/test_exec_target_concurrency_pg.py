"""A4.9 R1 — PostgreSQL execution-target/message concurrency proof."""
import asyncio
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select

from app.core.config import get_config
from app.db.database import AsyncSessionLocal, ClientExecutorDevice, Conversation, Message, SyncEvent, User, engine
from app.services import client_device_service as cds
from app.services.sync_apply import apply_change


async def _fixture():
    suffix = uuid.uuid4().hex[:12]
    async with AsyncSessionLocal() as db:
        user = User(username=f"race-{suffix}", password_hash="x", is_active=True)
        db.add(user)
        await db.flush()
        code = cds.issue_pairing_code(user.id)["code"]
        device, _token = await cds.register_device(db, code=code, name="race-device", platform="mac")
        conversation = Conversation(user_id=user.id, title="race")
        db.add(conversation)
        await db.commit()
        return user.id, device.device_id, conversation.id


async def _cleanup(user_id):
    async with AsyncSessionLocal() as db:
        await db.execute(delete(SyncEvent).where(SyncEvent.user_id == user_id))
        await db.execute(delete(Message).where(
            Message.conversation_id.in_(select(Conversation.id).where(Conversation.user_id == user_id))
        ))
        await db.execute(delete(Conversation).where(Conversation.user_id == user_id))
        await db.execute(delete(ClientExecutorDevice).where(ClientExecutorDevice.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _insert_with_lock(conversation_id, user_id, *, hold=None):
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id, Conversation.user_id == user_id)
            .with_for_update()
        )
        conversation = result.scalar_one()
        if hold is not None:
            hold.set()
            await asyncio.sleep(0.2)
        db.add(Message(conversation_id=conversation.id, role="user", content="first")
        )
        await db.commit()


def _sync_payload(conversation, target):
    return {
        "execution_target": target,
        "updated_at": (conversation.updated_at + timedelta(seconds=2)).isoformat(),
    }


def test_sync_first_lock_blocks_message_until_target_is_committed():
    async def scenario():
        user_id, device_id, conversation_id = await _fixture()
        config = get_config()
        old = config._config.setdefault("sync", {}).get("enabled")
        config._config["sync"]["enabled"] = False
        try:
            async with AsyncSessionLocal() as sync_db:
                conversation = await sync_db.get(Conversation, conversation_id)
                target = f"device:{device_id}"
                gate = asyncio.Event()
                checked = asyncio.Event()
                original = __import__("app.services.sync_apply", fromlist=["_execution_target_change_allowed"])._execution_target_change_allowed

                async def gated(db, user, row, data):
                    allowed = await original(db, user, row, data)
                    checked.set()
                    await gate.wait()
                    return allowed

                __import__("app.services.sync_apply", fromlist=["_execution_target_change_allowed"])._execution_target_change_allowed = gated
                sync_task = asyncio.create_task(apply_change(
                    sync_db, await sync_db.get(User, user_id),
                    "conversations", conversation_id, "update", _sync_payload(conversation, target),
                ))
                await checked.wait()
                message_task = asyncio.create_task(_insert_with_lock(conversation_id, user_id))
                await asyncio.sleep(0.2)
                assert not message_task.done(), "message insert must wait for the sync-held row lock"
                gate.set()
                __import__("app.services.sync_apply", fromlist=["_execution_target_change_allowed"])._execution_target_change_allowed = original
                assert await sync_task == "applied"
                await sync_db.commit()
                await message_task

                target_after = await sync_db.get(Conversation, conversation_id)
                assert target_after.execution_target == target
                count = (await sync_db.execute(
                    select(func.count(Message.id)).where(Message.conversation_id == conversation_id)
                )).scalar_one()
                assert count == 1
        finally:
            if old is None:
                config._config["sync"].pop("enabled", None)
            else:
                config._config["sync"]["enabled"] = old
            await _cleanup(user_id)
            await engine.dispose()

    asyncio.run(scenario())


def test_message_first_lock_makes_sync_reject_target_change():
    async def scenario():
        user_id, device_id, conversation_id = await _fixture()
        config = get_config()
        old = config._config.setdefault("sync", {}).get("enabled")
        config._config["sync"]["enabled"] = False
        try:
            locked = asyncio.Event()
            async with AsyncSessionLocal() as sync_db:
                conversation = await sync_db.get(Conversation, conversation_id)
                target = f"device:{device_id}"
                message_task = asyncio.create_task(_insert_with_lock(conversation_id, user_id, hold=locked))
                await locked.wait()
                sync_task = asyncio.create_task(apply_change(
                    sync_db, await sync_db.get(User, user_id),
                    "conversations", conversation_id, "update", _sync_payload(conversation, target),
                ))
                await asyncio.sleep(0.2)
                assert not sync_task.done(), "sync must wait for the first-message row lock"
                # The message transaction's hold elapsed; it commits first.
                assert await sync_task == "skipped"
                await message_task
                row = await sync_db.get(Conversation, conversation_id)
                assert row.execution_target is None
        finally:
            if old is None:
                config._config["sync"].pop("enabled", None)
            else:
                config._config["sync"]["enabled"] = old
            await _cleanup(user_id)
            await engine.dispose()

    asyncio.run(scenario())
