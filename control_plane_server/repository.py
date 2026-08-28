"""MongoDB persistence for control-plane agents, replay nonces, and aggregate metrics."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import secrets

import pymongo
from pymongo.errors import DuplicateKeyError

from control_plane import METRICS_SCHEMA_VERSION, PRIVACY_MODE_AGGREGATE_ONLY, canonical_json


def _parse_datetime(value):
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_stored_datetime(value):
    if value is None:
        return None
    if isinstance(value, str):
        return _parse_datetime(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now():
    return datetime.now(timezone.utc)


class MetricsIngestResult:
    """Outcome classification for one aggregate metrics batch."""

    def __init__(self, status, payload_hash):
        self.status = status
        self.payload_hash = payload_hash


class ControlPlaneRepository:
    def __init__(self, database, client=None):
        self.database = database
        self.client = client
        self.agents = database["agents"]
        self.nonces = database["request_nonces"]
        self.metrics_batches = database["metrics_batches"]
        self.instance_metrics_current = database["instance_metrics_current"]
        self.instance_metrics_daily = database["instance_metrics_daily"]

    @classmethod
    def from_config(cls, config):
        client = pymongo.MongoClient(config.database_url, serverSelectionTimeoutMS=5_000)
        return cls(client[config.database_name], client=client)

    async def ensure_indexes(self):
        await asyncio.to_thread(self._ensure_indexes)

    def _ensure_indexes(self):
        self.agents.create_index("instance_id", unique=True, name="agents_instance_id")
        self.agents.create_index("last_seen_at", name="agents_last_seen")
        self.nonces.create_index("expires_at", expireAfterSeconds=0, name="request_nonces_ttl")
        # metrics_batches._id is the canonical batch identity (inserted as batch_id
        # in _claim_batch). MongoDB's built-in _id_ index already enforces uniqueness,
        # so an explicit unique index on _id is redundant and rejected by MongoDB
        # (code 197, InvalidIndexSpecificationOption). Rely on the built-in _id_ only.
        self.metrics_batches.create_index(
            [("instance_id", pymongo.ASCENDING), ("status", pymongo.ASCENDING), ("lease_expires_at", pymongo.ASCENDING)],
            name="metrics_batches_claim",
        )
        self.metrics_batches.create_index(
            [("status", pymongo.ASCENDING), ("completed_at", pymongo.ASCENDING)],
            name="metrics_batches_cleanup",
        )
        self.instance_metrics_current.create_index("instance_id", unique=True, name="instance_metrics_current_instance")
        self.instance_metrics_daily.create_index(
            [("instance_id", pymongo.ASCENDING), ("date_utc", pymongo.ASCENDING)],
            unique=True,
            name="instance_metrics_daily_instance_date",
        )
        self.instance_metrics_daily.create_index("date_utc", name="instance_metrics_daily_date")

    async def consume_nonce(self, instance_id, nonce, ttl_seconds, now_epoch):
        return await asyncio.to_thread(self._consume_nonce, instance_id, nonce, ttl_seconds, now_epoch)

    def _consume_nonce(self, instance_id, nonce, ttl_seconds, now_epoch):
        nonce_key = hashlib.sha256(f"{instance_id}\0{nonce}".encode("utf-8")).hexdigest()
        try:
            self.nonces.insert_one(
                {
                    "_id": nonce_key,
                    "instance_id": instance_id,
                    "expires_at": datetime.fromtimestamp(now_epoch + ttl_seconds, timezone.utc),
                }
            )
        except DuplicateKeyError:
            return False
        return True

    async def register_agent(self, payload, secret_hash, now):
        await asyncio.to_thread(self._register_agent, payload, secret_hash, now)

    def _register_agent(self, payload, secret_hash, now):
        document = {
            "telegram_bot_id": payload["telegram_bot_id"],
            "username": payload.get("username"),
            "version": payload.get("version"),
            "started_at": payload["started_at"],
            "last_seen_at": now,
            "status": "online",
            "protocol_version": payload["protocol_version"],
            "secret_hash": secret_hash,
        }
        self.agents.update_one(
            {"instance_id": payload["instance_id"]},
            {"$set": document, "$setOnInsert": {"registered_at": now}},
            upsert=True,
        )

    async def heartbeat_agent(self, payload, now):
        return await asyncio.to_thread(self._heartbeat_agent, payload, now)

    def _heartbeat_agent(self, payload, now):
        result = self.agents.update_one(
            {"instance_id": payload["instance_id"]},
            {"$set": {
                "telegram_bot_id": payload["telegram_bot_id"],
                "username": payload.get("username"),
                "version": payload.get("version"),
                "started_at": payload["started_at"],
                "last_seen_at": now,
                "uptime_seconds": payload["uptime_seconds"],
                "status": payload["status"],
                "protocol_version": payload["protocol_version"],
            }},
        )
        return result.matched_count == 1

    async def agent_secret_matches(self, instance_id, secret_hash):
        return await asyncio.to_thread(
            lambda: bool(
                self.agents.find_one(
                    {"instance_id": instance_id, "secret_hash": secret_hash}, {"_id": 1}
                )
            )
        )

    async def ingest_metrics(self, payload, now, processing_lease_seconds=300):
        return await asyncio.to_thread(
            self._ingest_metrics, payload, now, processing_lease_seconds
        )

    def _claim_batch(self, batch_id, instance_id, payload_hash, observed_at, now, lease_seconds):
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        processing_token = secrets.token_urlsafe(32)
        try:
            # Atomic new claim: a fresh _id means exactly one worker wins.
            self.metrics_batches.insert_one(
                {
                    "_id": batch_id,
                    "instance_id": instance_id,
                    "status": "processing",
                    "payload_hash": payload_hash,
                    "observed_at": observed_at,
                    "received_at": now,
                    "processing_started_at": now,
                    "lease_expires_at": lease_expires_at,
                    "processing_token": processing_token,
                    "processing_generation": 1,
                    "attempts": 0,
                }
            )
            return "accepted", {
                "processing_token": processing_token,
                "processing_generation": 1,
            }
        except DuplicateKeyError:
            pass

        existing = self.metrics_batches.find_one({"_id": batch_id})
        if not existing:
            return "retryable", None
        if existing.get("status") == "completed":
            return ("duplicate" if existing.get("payload_hash") == payload_hash else "permanent_conflict"), None
        if existing.get("status") == "permanent_failure":
            return "permanent_conflict", None
        if existing.get("payload_hash") != payload_hash:
            return "permanent_conflict", None

        # Atomic stale reclaim: only an expired processing lease can be claimed.
        # The conditional filter includes status + lease expiry so a second worker
        # cannot re-claim a batch that another worker just refreshed.
        claimed = self.metrics_batches.find_one_and_update(
            {
                "_id": batch_id,
                "payload_hash": payload_hash,
                "status": "processing",
                "lease_expires_at": {"$lte": now},
            },
            {
                "$set": {
                    "status": "processing",
                    "processing_started_at": now,
                    "lease_expires_at": lease_expires_at,
                    "processing_token": processing_token,
                },
                "$inc": {
                    "attempts": 1,
                    "processing_generation": 1,
                },
            },
            return_document=pymongo.ReturnDocument.AFTER,
        )
        if claimed:
            return "accepted", {
                "processing_token": claimed["processing_token"],
                "processing_generation": claimed["processing_generation"],
            }
        return "retryable", None

    def _ingest_metrics(self, payload, now, lease_seconds):
        batch_id = payload["batch_id"]
        payload_hash = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        instance_id = payload["instance_id"]
        observed_at = _parse_datetime(payload["observed_at"])
        if now is None:
            now = _utc_now()

        claimed_status, claim = self._claim_batch(
            batch_id, instance_id, payload_hash, observed_at, now, lease_seconds
        )
        if claimed_status != "accepted":
            return MetricsIngestResult(status=claimed_status, payload_hash=payload_hash)
        processing_token = claim["processing_token"]
        processing_generation = claim["processing_generation"]
        claim_filter = {
            "_id": batch_id,
            "instance_id": instance_id,
            "status": "processing",
            "processing_token": processing_token,
            "processing_generation": processing_generation,
        }

        # Same instance + same observed_at with a *different* payload hash is a
        # permanent conflict, detected before any aggregate write.
        conflict = self.metrics_batches.find_one(
            {
                "instance_id": instance_id,
                "observed_at": observed_at,
                "_id": {"$ne": batch_id},
                "payload_hash": {"$ne": payload_hash},
            },
            {"_id": 1},
        )
        if conflict:
            self.metrics_batches.update_one(
                claim_filter,
                {"$set": {"status": "permanent_failure", "completed_at": now, "lease_expires_at": None}},
            )
            return MetricsIngestResult(status="permanent_conflict", payload_hash=payload_hash)

        current = payload["current"]
        outcomes = [self._write_current_aggregate(instance_id, batch_id, current, observed_at, now)]
        for item in payload["daily"]:
            outcomes.append(
                self._write_daily_aggregate(
                    instance_id,
                    batch_id,
                    item["date_utc"],
                    int(item["active_users"]),
                    int(item["interaction_count"]),
                    _parse_datetime(item["observed_at"]) or observed_at,
                    now,
                )
            )

        if "conflict" in outcomes:
            # Same timestamp, different batch content: resolve deterministically
            # as a permanent conflict instead of an arbitrary overwrite.
            self.metrics_batches.update_one(
                claim_filter,
                {"$set": {"status": "permanent_failure", "completed_at": now, "lease_expires_at": None}},
            )
            return MetricsIngestResult(status="permanent_conflict", payload_hash=payload_hash)

        # Batch becomes completed only after all current/daily writes succeeded.
        completion = self.metrics_batches.update_one(
            claim_filter,
            {"$set": {"status": "completed", "completed_at": now, "lease_expires_at": None}},
        )
        if completion.matched_count != 1:
            existing = self.metrics_batches.find_one({"_id": batch_id})
            if not existing:
                return MetricsIngestResult(status="retryable", payload_hash=payload_hash)
            if existing.get("payload_hash") != payload_hash:
                return MetricsIngestResult(status="permanent_conflict", payload_hash=payload_hash)
            if existing.get("status") == "completed":
                return MetricsIngestResult(status="duplicate", payload_hash=payload_hash)
            if existing.get("status") == "processing":
                return MetricsIngestResult(status="retryable", payload_hash=payload_hash)
            return MetricsIngestResult(status="permanent_conflict", payload_hash=payload_hash)
        return MetricsIngestResult(status="accepted", payload_hash=payload_hash)

    def _classify_freshness(self, existing, observed_at, batch_id):
        """Classify an atomic write after a unique-key insert conflict."""
        if existing is None:
            return "retryable"
        stored = _parse_stored_datetime(existing.get("observed_at"))
        if stored is None:
            return "fresh"
        if stored > observed_at:
            return "stale"
        if stored < observed_at:
            return "fresh"
        # Equal observed_at: same batch is idempotent; different batch is a
        # deterministic conflict (first-writer-wins).
        if existing.get("batch_id") == batch_id:
            return "fresh"
        return "conflict"

    def _write_current_aggregate(self, instance_id, batch_id, current, observed_at, now):
        set_fields = {
            "registered_users": int(current["registered_users"]),
            "reachable_users": int(current["reachable_users"]),
            "active_24h": int(current["active_24h"]),
            "active_7d": int(current["active_7d"]),
            "active_30d": int(current["active_30d"]),
            "observed_at": observed_at,
            "batch_id": batch_id,
            "updated_at": now,
            "metrics_schema_version": METRICS_SCHEMA_VERSION,
            "privacy_mode": PRIVACY_MODE_AGGREGATE_ONLY,
        }
        try:
            # Freshness predicate lives inside the atomic update/upsert.
            self.instance_metrics_current.update_one(
                {
                    "instance_id": instance_id,
                    "$or": [
                        {"observed_at": {"$exists": False}},
                        {"observed_at": {"$lt": observed_at}},
                        {"observed_at": observed_at, "batch_id": batch_id},
                    ],
                },
                {"$set": set_fields},
                upsert=True,
            )
            return "fresh"
        except DuplicateKeyError:
            existing = self.instance_metrics_current.find_one({"instance_id": instance_id})
            return self._classify_freshness(existing, observed_at, batch_id)

    def _write_daily_aggregate(self, instance_id, batch_id, date_utc, active_users,
                               interaction_count, day_observed_at, now):
        try:
            # Atomic $max update with freshness guard inside the query.
            self.instance_metrics_daily.update_one(
                {
                    "instance_id": instance_id,
                    "date_utc": date_utc,
                    "$or": [
                        {"observed_at": {"$exists": False}},
                        {"observed_at": {"$lt": day_observed_at}},
                        {"observed_at": day_observed_at, "batch_id": batch_id},
                    ],
                },
                {
                    "$set": {
                        "observed_at": day_observed_at,
                        "batch_id": batch_id,
                        "updated_at": now,
                        "metrics_schema_version": METRICS_SCHEMA_VERSION,
                        "privacy_mode": PRIVACY_MODE_AGGREGATE_ONLY,
                    },
                    "$max": {
                        "active_users": active_users,
                        "interaction_count": interaction_count,
                    },
                },
                upsert=True,
            )
            return "fresh"
        except DuplicateKeyError:
            existing = self.instance_metrics_daily.find_one(
                {"instance_id": instance_id, "date_utc": date_utc}
            )
            return self._classify_freshness(existing, day_observed_at, batch_id)

    async def metrics_summary(self, now, instance_id=None):
        return await asyncio.to_thread(self._metrics_summary, now, instance_id)

    def _metrics_summary(self, now, instance_id):
        query = {"instance_id": instance_id} if instance_id else {}
        docs = list(self.instance_metrics_current.find(query))
        summary = {"instance_count": len(docs)}
        for field in ("registered_users", "reachable_users", "active_24h", "active_7d", "active_30d"):
            total = sum(int(doc.get(field, 0)) for doc in docs)
            summary[f"global_{field}_observations"] = total
        return summary

    async def cleanup_metrics_batches(self, retention_days, limit):
        return await asyncio.to_thread(self._cleanup_metrics_batches, retention_days, limit)

    def _cleanup_metrics_batches(self, retention_days, limit):
        cutoff = _utc_now() - timedelta(days=retention_days)
        # Terminal states only (completed / permanent_failure). Processing batches
        # (active or stale-but-reclaimable) and retryable states are never removed.
        stale_ids = [
            doc["_id"]
            for doc in self.metrics_batches.find(
                {
                    "status": {"$in": ["completed", "permanent_failure"]},
                    "completed_at": {"$lte": cutoff},
                },
                {"_id": 1},
            ).limit(limit)
        ]
        if stale_ids:
            self.metrics_batches.delete_many({"_id": {"$in": stale_ids}})
        return len(stale_ids)

    async def ping(self):
        return await asyncio.to_thread(self._ping)

    def _ping(self):
        self.database.command("ping")
        return True

    async def close(self):
        if self.client is not None:
            await asyncio.to_thread(self.client.close)