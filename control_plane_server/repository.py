"""MongoDB persistence for control-plane agents and replay nonces."""

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping

import pymongo
from pymongo.errors import DuplicateKeyError


class ControlPlaneRepository:
    def __init__(self, database, client=None):
        self.database = database
        self.client = client
        self.agents = database["agents"]
        self.nonces = database["request_nonces"]

    @classmethod
    def from_config(cls, config):
        client = pymongo.MongoClient(config.database_url, serverSelectionTimeoutMS=5_000)
        return cls(client[config.database_name], client=client)

    async def ensure_indexes(self) -> None:
        await asyncio.to_thread(self._ensure_indexes)

    def _ensure_indexes(self) -> None:
        self.agents.create_index("instance_id", unique=True, name="agents_instance_id")
        self.agents.create_index("last_seen_at", name="agents_last_seen")
        self.nonces.create_index("expires_at", expireAfterSeconds=0, name="request_nonces_ttl")

    async def consume_nonce(self, instance_id: str, nonce: str, ttl_seconds: int, now_epoch: int) -> bool:
        return await asyncio.to_thread(self._consume_nonce, instance_id, nonce, ttl_seconds, now_epoch)

    def _consume_nonce(self, instance_id: str, nonce: str, ttl_seconds: int, now_epoch: int) -> bool:
        import hashlib

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

    async def register_agent(self, payload: Mapping[str, Any], secret_hash: str, now: datetime) -> None:
        await asyncio.to_thread(self._register_agent, payload, secret_hash, now)

    def _register_agent(self, payload: Mapping[str, Any], secret_hash: str, now: datetime) -> None:
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

    async def heartbeat_agent(self, payload: Mapping[str, Any], now: datetime) -> bool:
        return await asyncio.to_thread(self._heartbeat_agent, payload, now)

    def _heartbeat_agent(self, payload: Mapping[str, Any], now: datetime) -> bool:
        result = self.agents.update_one(
            {"instance_id": payload["instance_id"]},
            {
                "$set": {
                    "telegram_bot_id": payload["telegram_bot_id"],
                    "username": payload.get("username"),
                    "version": payload.get("version"),
                    "started_at": payload["started_at"],
                    "last_seen_at": now,
                    "uptime_seconds": payload["uptime_seconds"],
                    "status": payload["status"],
                    "protocol_version": payload["protocol_version"],
                }
            },
        )
        return result.matched_count == 1

    async def agent_secret_matches(self, instance_id: str, secret_hash: str) -> bool:
        return await asyncio.to_thread(
            lambda: bool(
                self.agents.find_one(
                    {"instance_id": instance_id, "secret_hash": secret_hash}, {"_id": 1}
                )
            )
        )

    async def ping(self) -> bool:
        return await asyncio.to_thread(self._ping)

    def _ping(self) -> bool:
        self.database.command("ping")
        return True

    async def close(self) -> None:
        if self.client is not None:
            await asyncio.to_thread(self.client.close)