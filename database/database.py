"""MongoDB persistence, kept behind async wrappers so PyMongo never blocks the loop."""

import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pymongo
from pymongo.errors import DuplicateKeyError

from config import (
    CLEANUP_MAX_ATTEMPTS,
    CLEANUP_RETRY_BASE_SECONDS,
    CLEANUP_RETRY_MAX_SECONDS,
    DB_NAME,
    DB_URI,
    LINK_TTL,
)
from cleanup_policy import cleanup_is_exhausted, cleanup_retry_delay
from metrics_policy import (
    ACTIVE_OUTBOX_STATES,
    OUTBOX_ACTIVE_SLOT,
)
from security import new_token, token_hash
from telemetry import ACTIVITY_WINDOWS, activity_cutoff


dbclient = pymongo.MongoClient(DB_URI, serverSelectionTimeoutMS=5_000)
database = dbclient[DB_NAME]
user_data = database["users"]
daily_activity_data = database["daily_user_activity"]
metrics_outbox_data = database["metrics_outbox"]
link_data = database["share_links"]
delivery_data = database["deliveries"]


def _ensure_indexes() -> None:
    user_data.create_index(
        [("last_seen_at", pymongo.DESCENDING), ("blocked_at", pymongo.ASCENDING), ("deleted_at", pymongo.ASCENDING)],
        name="users_activity_lookup",
    )
    daily_activity_data.create_index(
        [("date", pymongo.ASCENDING), ("user_id", pymongo.ASCENDING)],
        unique=True,
        name="daily_activity_user_date",
    )
    # metrics_outbox._id is the canonical batch identity (every create/claim/
    # transition/cleanup operates on _id) and is already unique via MongoDB's
    # built-in _id_ index. An explicit unique index on _id is invalid (code 197)
    # and redundant, so it must never be requested.
    metrics_outbox_data.create_index(
        [("instance_id", pymongo.ASCENDING), ("status", pymongo.ASCENDING)],
        name="metrics_outbox_instance_status",
    )
    # Enforce a database-level single-active-outbox invariant: at most one row
    # with an active_slot exists per instance. Additive index; nothing is dropped.
    metrics_outbox_data.create_index(
        [("instance_id", pymongo.ASCENDING), ("active_slot", pymongo.ASCENDING)],
        unique=True,
        partialFilterExpression={"active_slot": {"$exists": True}},
        name="metrics_outbox_single_active",
    )
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
            "$set": {
                "last_seen_at": now,
                "last_interaction_type": interaction_type.strip(),
                "updated_at": now,
                "blocked_at": None,
                "deleted_at": None,
            },
        },
        upsert=True,
    )


async def full_userbase() -> list[int]:
    return await asyncio.to_thread(lambda: [doc["_id"] for doc in user_data.find({}, {"_id": 1})])


async def reachable_userbase() -> list[int]:
    """Return users not known to have blocked or left Telegram."""
    return await asyncio.to_thread(
        lambda: [
            doc["_id"]
            for doc in user_data.find(
                {"blocked_at": None, "deleted_at": None}, {"_id": 1}
            )
        ]
    )


def _recent_utc_dates(reference: datetime, days: int) -> list[str]:
    end = reference.date()
    start = end - timedelta(days=days - 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(days)]


def _daily_metrics(reference: datetime, days: int, observed_at: datetime) -> list[dict[str, Any]]:
    dates = _recent_utc_dates(reference, days)
    pipeline = [
        {"$match": {"date": {"$in": dates}}},
        {
            "$group": {
                "_id": "$date",
                "active_users": {"$sum": 1},
                "interaction_count": {"$sum": {"$ifNull": ["$interaction_count", 0]}},
            }
        },
    ]
    grouped = {item["_id"]: item for item in daily_activity_data.aggregate(pipeline)}
    daily = []
    for date in dates:
        row = grouped.get(date)
        daily.append(
            {
                "date_utc": date,
                "active_users": int(row["active_users"]) if row else 0,
                "interaction_count": int(row["interaction_count"]) if row else 0,
                "observed_at": observed_at,
            }
        )
    return daily


def _metrics_snapshot(reference: datetime, daily_days: int) -> dict[str, Any]:
    reachable_filter = {"blocked_at": None, "deleted_at": None}
    current: dict[str, Any] = {
        "registered_users": user_data.count_documents({}),
        "reachable_users": user_data.count_documents(reachable_filter),
    }
    for name, days in ACTIVITY_WINDOWS:
        current[name] = user_data.count_documents(
            {
                **reachable_filter,
                "last_seen_at": {"$gte": activity_cutoff(days, now=reference)},
            }
        )
    daily = _daily_metrics(reference, daily_days, reference)
    return {"current": current, "observed_at": reference, "daily": daily}


async def get_metrics_snapshot(reference: datetime | None = None, daily_days: int = 30) -> dict[str, Any]:
    """Return a local aggregate snapshot without any identifier-bearing data."""
    if not isinstance(daily_days, int) or isinstance(daily_days, bool) or daily_days < 1 or daily_days > 90:
        raise ValueError("daily_days must be an integer between 1 and 90")
    observed = reference or utc_now()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return await asyncio.to_thread(_metrics_snapshot, observed, daily_days)


async def record_user_activity(user_id: int, interaction_type: str = "message") -> None:
    """Record one event using a single timestamp for user and daily rollup."""
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id < 1:
        raise ValueError("user_id must be a positive integer")
    if not isinstance(interaction_type, str) or not interaction_type.strip():
        raise ValueError("interaction_type must be a non-empty string")
    now = utc_now()
    date = now.date().isoformat()

    def _record():
        user_data.update_one(
            {"_id": user_id},
            {
                "$setOnInsert": {"created_at": now},
                "$set": {
                    "last_seen_at": now,
                    "last_interaction_type": interaction_type.strip(),
                    "updated_at": now,
                    "blocked_at": None,
                    "deleted_at": None,
                },
            },
            upsert=True,
        )
        daily_activity_data.update_one(
            {"date": date, "user_id": user_id},
            {
                "$setOnInsert": {
                    "_id": f"{date}:{user_id}",
                    "date": date,
                    "user_id": user_id,
                    "first_seen_at": now,
                    "created_at": now,
                },
                "$set": {"last_seen_at": now, "updated_at": now},
                "$inc": {"interaction_count": 1},
            },
            upsert=True,
        )

    await asyncio.to_thread(_record)


# --- Local aggregate outbox persistence --------------------------------------

def _active_outbox_filter(instance_id: str) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "status": {"$in": list(ACTIVE_OUTBOX_STATES)},
    }


async def get_active_metrics_outbox(instance_id: str) -> dict[str, Any] | None:
    """Return the single outstanding active outbox batch for an instance, if any."""
    return await asyncio.to_thread(
        metrics_outbox_data.find_one, _active_outbox_filter(instance_id)
    )


async def get_active_metrics_outbox_delay(instance_id: str, now: datetime) -> float | None:
    """Return seconds until the active outbox becomes claimable, if applicable."""
    def _delay() -> float | None:
        record = metrics_outbox_data.find_one(
            _active_outbox_filter(instance_id),
            {"status": 1, "next_attempt_at": 1, "lease_expires_at": 1},
        )
        if record is None:
            return None
        status = record.get("status")
        if status == "pending":
            return 0.0
        if status in {"retryable", "blocked_auth"}:
            due_at = record.get("next_attempt_at")
        elif status == "sending":
            due_at = record.get("lease_expires_at")
        else:
            return None
        if due_at is None:
            return 0.0
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        return max(0.0, (due_at.astimezone(timezone.utc) - now).total_seconds())

    return await asyncio.to_thread(_delay)


async def get_active_metrics_outbox_count(instance_id: str) -> int:
    return await asyncio.to_thread(
        metrics_outbox_data.count_documents, _active_outbox_filter(instance_id)
    )


async def get_metrics_outbox(batch_id: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(metrics_outbox_data.find_one, {"_id": batch_id})


async def create_metrics_outbox(record: dict[str, Any]) -> bool:
    """Persist a new exact-payload batch unless one is already active for the instance.

    Active records carry ``active_slot`` so the unique partial index
    ``(instance_id, active_slot)`` enforces a single-active-batch invariant. A
    concurrent second create resolves deterministically to "already exists"
    instead of an unhandled exception.
    """
    def _create() -> bool:
        outbox_record = dict(record)
        if outbox_record.get("status") in ACTIVE_OUTBOX_STATES:
            outbox_record["active_slot"] = OUTBOX_ACTIVE_SLOT
        if metrics_outbox_data.find_one(_active_outbox_filter(outbox_record["instance_id"])):
            return False
        try:
            metrics_outbox_data.insert_one(outbox_record)
        except DuplicateKeyError:
            return False
        return True

    return await asyncio.to_thread(_create)


async def claim_metrics_outbox(batch_id: str, lease_seconds: int, now: datetime) -> dict[str, Any] | None:
    """Atomically claim one due outbox batch, including stale-send reclaim."""
    def _claim() -> dict[str, Any] | None:
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        owner = secrets.token_urlsafe(32)
        return metrics_outbox_data.find_one_and_update(
            {
                "_id": batch_id,
                "$or": [
                    {"status": "pending"},
                    {
                        "status": {"$in": ["retryable", "blocked_auth"]},
                        "$or": [
                            {"next_attempt_at": {"$exists": False}},
                            {"next_attempt_at": {"$lte": now}},
                        ],
                    },
                    {
                        "status": "sending",
                        "lease_expires_at": {"$lte": now},
                    },
                ],
            },
            {
                "$set": {
                    "status": "sending",
                    "sending_owner": owner,
                    "sending_started_at": now,
                    "lease_expires_at": lease_expires_at,
                },
                "$inc": {"claim_generation": 1},
            },
            return_document=pymongo.ReturnDocument.AFTER,
        )

    return await asyncio.to_thread(_claim)


async def _guarded_outbox_update(batch_id: str, owner: str, claim_generation: int, update: dict) -> str:
    def _update() -> str:
        result = metrics_outbox_data.update_one(
            {
                "_id": batch_id,
                "status": "sending",
                "sending_owner": owner,
                "claim_generation": claim_generation,
            },
            update,
        )
        if result.matched_count == 1:
            return "transitioned"
        return "missing" if metrics_outbox_data.find_one({"_id": batch_id}, {"_id": 1}) is None else "stale_result_ignored"

    return await asyncio.to_thread(_update)


async def mark_metrics_outbox_accepted(batch_id: str, *, owner: str, claim_generation: int, now: datetime) -> str:
    return await _guarded_outbox_update(
        batch_id, owner, claim_generation,
        {
            "$set": {
                "status": "accepted",
                "accepted_at": now,
                "sending_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            },
            "$unset": {"active_slot": 1},
        },
    )


async def mark_metrics_outbox_retryable(batch_id: str, *, owner: str, claim_generation: int,
                                        next_attempt_at: datetime, attempts: int, error_class: str, now: datetime) -> str:
    return await _guarded_outbox_update(
        batch_id, owner, claim_generation,
        {
            "$set": {
                "status": "retryable",
                "attempts": attempts,
                "next_attempt_at": next_attempt_at,
                "last_error_class": error_class,
                "last_error_at": now,
                "sending_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        },
    )


async def mark_metrics_outbox_permanent(batch_id: str, *, owner: str, claim_generation: int,
                                        attempts: int, error_class: str, now: datetime) -> str:
    return await _guarded_outbox_update(
        batch_id, owner, claim_generation,
        {
            "$set": {
                "status": "permanent_failure",
                "attempts": attempts,
                "next_attempt_at": None,
                "last_error_class": error_class,
                "last_error_at": now,
                "sending_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            },
            "$unset": {"active_slot": 1},
        },
    )


async def mark_metrics_outbox_blocked_auth(batch_id: str, *, owner: str, claim_generation: int,
                                           attempts: int, next_attempt_at: datetime, error_class: str,
                                           now: datetime) -> str:
    return await _guarded_outbox_update(
        batch_id, owner, claim_generation,
        {
            "$set": {
                "status": "blocked_auth",
                "attempts": attempts,
                "next_attempt_at": next_attempt_at,
                "last_error_class": error_class,
                "last_error_at": now,
                "sending_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        },
    )


async def reclaim_stale_metrics_outbox(instance_id: str, now: datetime) -> None:
    """Reclaim a stale sending batch whose lease has expired back to retryable."""
    await asyncio.to_thread(
        metrics_outbox_data.update_one,
        {
            "instance_id": instance_id,
            "status": "sending",
            "lease_expires_at": {"$lte": now},
        },
        {
            "$set": {
                "status": "retryable",
                "sending_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        },
    )


async def cleanup_metrics_outbox(*, accepted_retention_days: int, permanent_retention_days: int, limit: int) -> int:
    """Delete expired accepted/permanent outbox records in one bounded batch."""
    def _cleanup() -> int:
        now = utc_now()
        accepted_cutoff = now - timedelta(days=accepted_retention_days)
        permanent_cutoff = now - timedelta(days=permanent_retention_days)
        expired_ids = [
            doc["_id"]
            for doc in metrics_outbox_data.find(
                {
                    "$or": [
                        {"status": "accepted", "accepted_at": {"$lte": accepted_cutoff}},
                        {
                            "status": "permanent_failure",
                            "last_error_at": {"$lte": permanent_cutoff},
                        },
                    ]
                },
                {"_id": 1},
            ).limit(limit)
        ]
        if expired_ids:
            metrics_outbox_data.delete_many({"_id": {"$in": expired_ids}})
        return len(expired_ids)

    return await asyncio.to_thread(_cleanup)


async def user_statistics(now: datetime | None = None) -> dict[str, int]:
    """Return registered and rolling active-user counters."""
    reference = now or utc_now()

    def _count_statistics() -> dict[str, int]:
        statistics = {"registered": user_data.count_documents({})}
        reachable_filter = {"blocked_at": None, "deleted_at": None}
        statistics["reachable"] = user_data.count_documents(reachable_filter)
        for name, days in ACTIVITY_WINDOWS:
            statistics[name] = user_data.count_documents(
                {
                    **reachable_filter,
                    "last_seen_at": {"$gte": activity_cutoff(days, now=reference)},
                }
            )
        return statistics

    return await asyncio.to_thread(_count_statistics)


async def del_user(user_id: int) -> None:
    await asyncio.to_thread(user_data.delete_one, {"_id": user_id})


async def mark_user_unreachable(user_id: int, reason: str) -> None:
    """Keep user history while excluding a blocked/deleted account from delivery."""
    if reason not in {"blocked", "deleted"}:
        raise ValueError("reason must be blocked or deleted")
    field = "blocked_at" if reason == "blocked" else "deleted_at"
    await asyncio.to_thread(
        user_data.update_one,
        {"_id": user_id},
        {"$set": {field: utc_now(), "updated_at": utc_now()}},
    )


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
