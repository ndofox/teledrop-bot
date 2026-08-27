"""MongoDB persistence, kept behind async wrappers so PyMongo never blocks the loop."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pymongo

from config import (
    CLEANUP_MAX_ATTEMPTS,
    CLEANUP_RETRY_BASE_SECONDS,
    CLEANUP_RETRY_MAX_SECONDS,
    DB_NAME,
    DB_URI,
    LINK_TTL,
)
from cleanup_policy import cleanup_is_exhausted, cleanup_retry_delay
from security import new_token, token_hash
from telemetry import ACTIVITY_WINDOWS, activity_cutoff


dbclient = pymongo.MongoClient(DB_URI, serverSelectionTimeoutMS=5_000)
database = dbclient[DB_NAME]
user_data = database["users"]
link_data = database["share_links"]
delivery_data = database["deliveries"]


def _ensure_indexes() -> None:
    user_data.create_index("last_seen_at", name="users_last_seen_lookup")
    link_data.create_index("token_hash", unique=True, name="share_links_token_hash")
    link_data.create_index(
        [("expires_at", pymongo.ASCENDING), ("revoked_at", pymongo.ASCENDING)],
        name="share_links_active_lookup",
    )
    delivery_data.create_index(
        [("deleted_at", pymongo.ASCENDING), ("delete_at", pymongo.ASCENDING)],
        name="deliveries_due_lookup",
    )
    delivery_data.create_index(
        [
            ("deleted_at", pymongo.ASCENDING),
            ("cleanup_exhausted", pymongo.ASCENDING),
            ("delete_at", pymongo.ASCENDING),
            ("next_attempt_at", pymongo.ASCENDING),
        ],
        name="deliveries_retry_lookup",
    )


async def ensure_indexes() -> None:
    """Create the small set of indexes needed by link and cleanup queries."""
    await asyncio.to_thread(_ensure_indexes)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def present_user(user_id: int) -> bool:
    return bool(await asyncio.to_thread(user_data.find_one, {"_id": user_id}, {"_id": 1}))


async def add_user(user_id: int) -> None:
    await touch_user(user_id, interaction_type="start")


async def touch_user(user_id: int, interaction_type: str = "message") -> None:
    """Create a user or record the latest interaction for an existing user."""
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    if not isinstance(interaction_type, str) or not interaction_type.strip():
        raise ValueError("interaction_type must be a non-empty string")
    now = utc_now()
    await asyncio.to_thread(
        user_data.update_one,
        {"_id": user_id},
        {
            "$setOnInsert": {"created_at": now},
            "$set": {"last_seen_at": now, "last_interaction_type": interaction_type.strip()},
        },
        upsert=True,
    )


async def full_userbase() -> list[int]:
    return await asyncio.to_thread(lambda: [doc["_id"] for doc in user_data.find({}, {"_id": 1})])


async def user_statistics(now: datetime | None = None) -> dict[str, int]:
    """Return registered and rolling active-user counters."""
    reference = now or utc_now()

    def _count_statistics() -> dict[str, int]:
        statistics = {"registered": user_data.count_documents({})}
        for name, days in ACTIVITY_WINDOWS:
            statistics[name] = user_data.count_documents(
                {"last_seen_at": {"$gte": activity_cutoff(days, now=reference)}}
            )
        return statistics

    return await asyncio.to_thread(_count_statistics)


async def del_user(user_id: int) -> None:
    await asyncio.to_thread(user_data.delete_one, {"_id": user_id})


async def create_link(message_ids: list[int], created_by: int, ttl_seconds: int = LINK_TTL) -> str:
    if not message_ids or any(
        not isinstance(message_id, int) or isinstance(message_id, bool) or message_id < 1
        for message_id in message_ids
    ):
        raise ValueError("message_ids must contain positive integer message IDs")
    if not isinstance(created_by, int) or isinstance(created_by, bool) or created_by < 1:
        raise ValueError("created_by must be a positive integer")
    if ttl_seconds < 1:
        raise ValueError("ttl_seconds must be positive")
    token = new_token()
    now = utc_now()
    document = {
        "token_hash": token_hash(token),
        "message_ids": message_ids,
        "created_by": created_by,
        "created_at": now,
        "expires_at": now + timedelta(seconds=ttl_seconds),
        "revoked_at": None,
    }
    await asyncio.to_thread(link_data.insert_one, document)
    return token


async def find_active_link(token_digest: str) -> dict[str, Any] | None:
    now = utc_now()
    return await asyncio.to_thread(
        link_data.find_one,
        {
            "token_hash": token_digest,
            "revoked_at": None,
            "expires_at": {"$gt": now},
        },
    )


async def revoke_link(token_digest: str, revoked_by: int) -> bool:
    result = await asyncio.to_thread(
        link_data.update_one,
        {"token_hash": token_digest, "revoked_at": None},
        {"$set": {"revoked_at": utc_now(), "revoked_by": revoked_by}},
    )
    return result.modified_count == 1


async def create_delivery(chat_id: int, message_ids: list[int], notification_id: int, delete_at: datetime) -> None:
    await asyncio.to_thread(
        delivery_data.insert_one,
        {
            "chat_id": chat_id,
            "message_ids": message_ids,
            "notification_id": notification_id,
            "delete_at": delete_at,
            "next_attempt_at": delete_at,
            "deleted_at": None,
            "attempts": 0,
            "cleanup_exhausted": False,
        },
    )


async def due_deliveries(limit: int = 100) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        lambda: list(
            delivery_data.find(
                {
                    "deleted_at": None,
                    "cleanup_exhausted": {"$ne": True},
                    "delete_at": {"$lte": utc_now()},
                    "$or": [
                        {"next_attempt_at": {"$exists": False}},
                        {"next_attempt_at": {"$lte": utc_now()}},
                    ],
                },
                {"chat_id": 1, "message_ids": 1, "notification_id": 1},
            ).limit(limit)
        )
    )


async def mark_delivery_deleted(delivery_id: Any) -> None:
    await asyncio.to_thread(
        delivery_data.update_one,
        {"_id": delivery_id},
        {"$set": {"deleted_at": utc_now(), "cleanup_exhausted": False}},
    )


def _mark_delivery_attempt(delivery_id: Any) -> bool:
    """Record one failed cleanup and return whether retries are exhausted."""
    document = delivery_data.find_one({"_id": delivery_id}, {"attempts": 1}) or {}
    failure_number = int(document.get("attempts", 0)) + 1
    now = utc_now()
    exhausted = cleanup_is_exhausted(failure_number, CLEANUP_MAX_ATTEMPTS)
    update = {
        "$inc": {"attempts": 1},
        "$set": {"cleanup_exhausted": exhausted},
    }
    if exhausted:
        update["$set"]["next_attempt_at"] = None
    else:
        update["$set"]["next_attempt_at"] = now + timedelta(
            seconds=cleanup_retry_delay(
                failure_number, CLEANUP_RETRY_BASE_SECONDS, CLEANUP_RETRY_MAX_SECONDS
            )
        )
    result = delivery_data.update_one({"_id": delivery_id}, update)
    return result.modified_count == 1 and exhausted


async def mark_delivery_attempt(delivery_id: Any) -> bool:
    return await asyncio.to_thread(_mark_delivery_attempt, delivery_id)
