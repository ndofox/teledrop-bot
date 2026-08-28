"""Offline tests for aggregate ingestion/recovery using an in-memory Mongo-like store."""

import unittest
from datetime import datetime, timedelta, timezone

import pymongo
from pymongo.errors import DuplicateKeyError, OperationFailure

from control_plane import aggregate_payload
from control_plane_server.repository import ControlPlaneRepository


def _index_fields(keys):
    """Normalize a PyMongo key spec (str, dict, or list of tuples) to field names."""
    if isinstance(keys, str):
        return {keys}
    if isinstance(keys, dict):
        return set(keys)
    if isinstance(keys, (list, tuple)):
        return {k for k, _ in keys}
    return set()



def _matches(doc, query):
    if not query:
        return True
    for key, value in query.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in value):
                return False
            continue
        if isinstance(value, dict) and any(op.startswith("$") for op in value):
            raw = doc.get(key)
            for op, operand in value.items():
                if op == "$ne":
                    if raw == operand:
                        return False
                elif op == "$in":
                    if raw not in operand:
                        return False
                elif op == "$lte":
                    if raw is None or raw > operand:
                        return False
                elif op == "$lt":
                    if raw is None or raw >= operand:
                        return False
                elif op == "$gte":
                    if raw is None or raw < operand:
                        return False
                elif op == "$gt":
                    if raw is None or raw <= operand:
                        return False
                elif op == "$exists":
                    if bool(operand) != (key in doc):
                        return False
        else:
            if doc.get(key) != value:
                return False
    return True


def _apply_update(doc, update, *, insert):
    for op, fields in update.items():
        if op == "$set":
            doc.update(fields)
        elif op == "$setOnInsert":
            if insert:
                doc.update(fields)
        elif op == "$inc":
            for key, delta in fields.items():
                doc[key] = doc.get(key, 0) + delta
        elif op == "$max":
            for key, val in fields.items():
                doc[key] = max(doc.get(key, 0), val)
        elif op == "$unset":
            for key in fields:
                doc.pop(key, None)
    return doc


class _UpdateResult:
    def __init__(self, matched_count, modified_count, upserted_id=None):
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_id = upserted_id


def _equality_fields(query):
    """Return the equality (non-operator) filter fields used as a doc identity."""
    eq = {}
    for key, value in (query or {}).items():
        if key.startswith("$"):
            continue
        if isinstance(value, dict) and any(op.startswith("$") for op in value):
            continue
        eq[key] = value
    return eq


class _Cursor:
    def __init__(self, items):
        self._items = items

    def limit(self, n):
        return self._items[:n]

    def __iter__(self):
        return iter(self._items)


class FakeCollection:
    def __init__(self):
        self.docs = {}
        self.index_requests = []

    def create_index(self, keys, **options):
        name = options.get("name")
        unique = bool(options.get("unique", False))
        fields = _index_fields(keys)
        self.index_requests.append(
            {
                "name": name,
                "keys": keys,
                "unique": unique,
                "fields": fields,
                "partial_filter": options.get("partialFilterExpression"),
            }
        )
        # Mirror MongoDB: an explicit unique index on _id is invalid (code 197);
        # the built-in _id_ unique index already covers it. Fail loudly rather than
        # silently swallowing the bad request so a regressed _ensure_indexes() cannot
        # pass while claiming an invalid _id index.
        if unique and "_id" in fields:
            from pymongo.errors import OperationFailure

            # code 197 == InvalidIndexSpecificationOption (as surfaced by MongoDB).
            raise OperationFailure(
                "The field 'unique' is not valid for an _id index specification.",
                code=197,
            )
        return None

    def _identity_conflict(self, identity):
        """Raise DuplicateKeyError when an existing doc shares the same identity
        fields (mirrors a unique index) but the full query did not match it."""
        if not identity:
            return False
        for existing in self.docs.values():
            if all(existing.get(key) == value for key, value in identity.items()):
                from pymongo.errors import DuplicateKeyError

                raise DuplicateKeyError("dup key")
        return False

    def insert_one(self, doc):
        doc_id = doc["_id"]
        if doc_id in self.docs:
            from pymongo.errors import DuplicateKeyError

            raise DuplicateKeyError("dup")
        self.docs[doc_id] = dict(doc)

    def find_one(self, query=None, projection=None):
        for doc in self.docs.values():
            if _matches(doc, query or {}):
                result = dict(doc)
                if projection:
                    result = {k: result[k] for k in projection if k in result}
                return result
        return None

    def _build_upsert(self, query, update, update_insert_key):
        new_doc = {"_id": query.get("_id", object())}
        for key, value in _equality_fields(query).items():
            new_doc[key] = value
        _apply_update(new_doc, update, insert=True)
        return new_doc

    def find_one_and_update(self, query, update, return_document=None, upsert=False):
        for doc_id, doc in self.docs.items():
            if _matches(doc, query or {}):
                updated = dict(doc)
                _apply_update(updated, update, insert=False)
                self.docs[doc_id] = updated
                return dict(updated)
        if upsert:
            new_doc = self._build_upsert(query, update, return_document)
            self._identity_conflict(_equality_fields(query))
            self.docs[new_doc["_id"]] = new_doc
            return dict(new_doc)
        return None

    def update_one(self, query, update, upsert=False):
        matched = [doc_id for doc_id, doc in self.docs.items() if _matches(doc, query)]
        if matched:
            for doc_id in matched:
                _apply_update(self.docs[doc_id], update, insert=False)
            return _UpdateResult(matched_count=len(matched), modified_count=len(matched))
        if not upsert:
            return _UpdateResult(matched_count=0, modified_count=0)
        new_doc = self._build_upsert(query, update, True)
        self._identity_conflict(_equality_fields(query))
        self.docs[new_doc["_id"]] = new_doc
        return _UpdateResult(matched_count=0, modified_count=1, upserted_id=new_doc["_id"])

    def count_documents(self, query=None):
        return sum(1 for doc in self.docs.values() if _matches(doc, query or {}))

    def find(self, query=None, projection=None):
        results = []
        for doc in self.docs.values():
            if _matches(doc, query or {}):
                result = dict(doc)
                if projection:
                    result = {k: result[k] for k in projection if k in result}
                results.append(result)
        return _Cursor(results)

    def aggregate(self, pipeline):
        docs = list(self.docs.values())
        for stage in pipeline:
            op = next(iter(stage))
            if op == "$match":
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif op == "$group":
                group = stage["$group"]
                key_expr = group["_id"]
                key_field = key_expr[1:] if key_expr.startswith("$") else key_expr
                groups = {}
                for d in docs:
                    key = d.get(key_field)
                    row = groups.setdefault(key, {"_id": key})
                    for out_field, acc in group.items():
                        if out_field == "_id":
                            continue
                        opkind = next(iter(acc))
                        operand = acc[opkind]
                        if opkind != "$sum":
                            continue
                        if operand == 1:
                            val = 1
                        elif isinstance(operand, list):
                            val = d.get(operand[0][1:], 0) if len(operand) and operand[0].startswith("$") else 0
                        elif isinstance(operand, dict) and "$ifNull" in operand:
                            expr = operand["$ifNull"]
                            field = expr[0][1:] if expr and isinstance(expr[0], str) and expr[0].startswith("$") else None
                            default = expr[1] if len(expr) > 1 else 0
                            val = d.get(field, default) if field else default
                        elif isinstance(operand, str) and operand.startswith("$"):
                            val = d.get(operand[1:], 0)
                        else:
                            val = operand
                        row[out_field] = row.get(out_field, 0) + val
                docs = list(groups.values())
        return docs

    def delete_many(self, query=None):
        ids = [doc_id for doc_id, doc in self.docs.items() if _matches(doc, query or {})]
        for doc_id in ids:
            del self.docs[doc_id]
        return len(ids)


class FakeDatabase(dict):
    def __getitem__(self, key):
        return self.setdefault(key, FakeCollection())


NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def _payload(current, daily, observed_at=None):
    observed_at = observed_at or NOW
    return aggregate_payload(instance_id="bot-01", observed_at=observed_at, current=current, daily=daily)


class RepositoryIngestionTests(unittest.TestCase):
    def setUp(self):
        self.repo = ControlPlaneRepository(FakeDatabase())
        self.repo.agents = FakeCollection()
        self.repo.nonces = FakeCollection()

    def current(self, registered=100, reachable=90, a24=10, a7=40, a30=70):
        return {
            "registered_users": registered,
            "reachable_users": reachable,
            "active_24h": a24,
            "active_7d": a7,
            "active_30d": a30,
        }

    def daily(self, count=5, interactions=20, observed_at=None):
        return [
            {
                "date_utc": "2026-08-27",
                "active_users": count,
                "interaction_count": interactions,
                "observed_at": observed_at or NOW,
            }
        ]

    def test_first_payload_accepted_and_stored(self):
        payload = _payload(self.current(), self.daily())
        result = self.repo._ingest_metrics(payload, NOW, 300)
        self.assertEqual(result.status, "accepted")
        current_doc = self.repo.instance_metrics_current.find_one({"instance_id": "bot-01"})
        self.assertEqual(current_doc["registered_users"], 100)
        daily_doc = self.repo.instance_metrics_daily.find_one({"instance_id": "bot-01", "date_utc": "2026-08-27"})
        self.assertEqual(daily_doc["interaction_count"], 20)
        self.assertEqual(daily_doc["active_users"], 5)

    def test_exact_duplicate_is_duplicate_without_double_count(self):
        payload = _payload(self.current(), self.daily())
        self.repo._ingest_metrics(payload, NOW, 300)
        result = self.repo._ingest_metrics(payload, NOW, 300)
        self.assertEqual(result.status, "duplicate")
        current_doc = self.repo.instance_metrics_current.find_one({"instance_id": "bot-01"})
        self.assertEqual(current_doc["registered_users"], 100)

    def test_payload_hash_conflict_is_permanent(self):
        first = _payload(self.current(registered=100), self.daily())
        self.repo._ingest_metrics(first, NOW, 300)
        second = dict(first)
        second["batch_id"] = "f" * 64
        result = self.repo._ingest_metrics(second, NOW, 300)
        self.assertEqual(result.status, "permanent_conflict")

    def test_stale_snapshot_does_not_regress_current(self):
        newer = NOW + timedelta(minutes=10)
        self.repo._ingest_metrics(
            _payload(self.current(registered=200), self.daily(count=50), observed_at=newer), NOW, 300
        )
        self.repo._ingest_metrics(
            _payload(self.current(registered=20), self.daily(count=2), observed_at=NOW), NOW, 300
        )
        current_doc = self.repo.instance_metrics_current.find_one({"instance_id": "bot-01"})
        self.assertEqual(current_doc["registered_users"], 200)

    def test_daily_is_monotonic_and_does_not_double_on_retry(self):
        newer = NOW + timedelta(seconds=30)
        self.repo._ingest_metrics(_payload(self.current(), self.daily(count=5, interactions=20)), NOW, 300)
        self.repo._ingest_metrics(
            _payload(
                self.current(),
                self.daily(count=7, interactions=25, observed_at=newer),
                observed_at=newer,
            ),
            NOW,
            300,
        )
        daily_doc = self.repo.instance_metrics_daily.find_one({"instance_id": "bot-01", "date_utc": "2026-08-27"})
        self.assertEqual(daily_doc["interaction_count"], 25)
        self.assertEqual(daily_doc["active_users"], 7)

    def test_active_lease_returns_retryable(self):
        payload = _payload(self.current(), self.daily())
        self.repo._ingest_metrics(payload, NOW, 300)
        self.repo.metrics_batches.update_one(
            {"_id": payload["batch_id"]},
            {"$set": {"status": "processing", "lease_expires_at": NOW + timedelta(seconds=300)}},
        )
        retry = self.repo._ingest_metrics(payload, NOW, 300)
        self.assertEqual(retry.status, "retryable")

    def test_stale_processing_is_reclaimed(self):
        payload = _payload(self.current(), self.daily())
        self.repo._ingest_metrics(payload, NOW, 300)
        self.repo.metrics_batches.update_one(
            {"_id": payload["batch_id"]},
            {"$set": {"status": "processing", "lease_expires_at": NOW - timedelta(seconds=10)}},
        )
        result = self.repo._ingest_metrics(payload, NOW, 300)
        self.assertEqual(result.status, "accepted")

    def test_no_per_user_write_collections(self):
        self.repo._ingest_metrics(_payload(self.current(), self.daily()), NOW, 300)
        self.assertFalse(hasattr(self.repo, "bot_users"))
        self.assertNotIn("bot_users", self.repo.database)
        self.assertNotIn("daily_user_activity", self.repo.database)

    def test_two_reclaimers_only_one_claims_stale_batch(self):
        batch_id = "c" * 64
        payload_hash = "d" * 64
        self.repo.metrics_batches.insert_one(
            {
                "_id": batch_id,
                "instance_id": "bot-01",
                "status": "processing",
                "payload_hash": payload_hash,
                "observed_at": NOW,
                "lease_expires_at": NOW - timedelta(seconds=10),
                "processing_token": "old-token",
                "processing_generation": 1,
                "attempts": 5,
            }
        )
        first, first_claim = self.repo._claim_batch(batch_id, "bot-01", payload_hash, NOW, NOW, 300)
        self.assertEqual(first, "accepted")
        self.assertEqual(first_claim["processing_generation"], 2)
        doc = self.repo.metrics_batches.find_one({"_id": batch_id})
        self.assertEqual(doc["processing_token"], first_claim["processing_token"])
        self.assertEqual(doc["attempts"], 6)  # 5 + one increment by the winner only
        self.assertGreater(doc["lease_expires_at"], NOW)
        second, second_claim = self.repo._claim_batch(batch_id, "bot-01", payload_hash, NOW, NOW, 300)
        self.assertEqual(second, "retryable")
        self.assertIsNone(second_claim)
        self.assertEqual(self.repo.metrics_batches.find_one({"_id": batch_id})["attempts"], 6)

    def test_equal_observed_different_batch_conflicts_deterministically(self):
        # Direct exercise of the atomic freshness guard: an existing record at the
        # same observed_at but a different batch must not be overwritten.
        self.repo.instance_metrics_current.insert_one(
            {
                "_id": "other",
                "instance_id": "bot-01",
                "observed_at": NOW,
                "batch_id": "other-batch",
                "registered_users": 100,
            }
        )
        outcome = self.repo._write_current_aggregate(
            "bot-01", "this-batch", self.current(registered=999), NOW, NOW
        )
        self.assertEqual(outcome, "conflict")
        doc = self.repo.instance_metrics_current.find_one({"instance_id": "bot-01"})
        self.assertEqual(doc["batch_id"], "other-batch")
        self.assertEqual(doc["registered_users"], 100)

    def test_current_can_fall_on_a_newer_snapshot(self):
        self.repo._ingest_metrics(_payload(self.current(registered=100), self.daily()), NOW, 300)
        newer = NOW + timedelta(minutes=5)
        self.repo._ingest_metrics(
            _payload(self.current(registered=50), self.daily(observed_at=newer), observed_at=newer),
            NOW,
            300,
        )
        doc = self.repo.instance_metrics_current.find_one({"instance_id": "bot-01"})
        self.assertEqual(doc["registered_users"], 50)

    def test_crash_after_current_write_recovers_idempotently(self):
        payload = _payload(self.current(registered=100), self.daily(interactions=5))
        self.repo._ingest_metrics(payload, NOW, 300)
        # Simulate a crash after aggregate writes but before the batch completes.
        self.repo.metrics_batches.update_one(
            {"_id": payload["batch_id"]},
            {"$set": {"status": "processing", "lease_expires_at": NOW - timedelta(seconds=5)}},
        )
        result = self.repo._ingest_metrics(payload, NOW, 300)
        self.assertEqual(result.status, "accepted")
        current_doc = self.repo.instance_metrics_current.find_one({"instance_id": "bot-01"})
        self.assertEqual(current_doc["registered_users"], 100)
        daily_doc = self.repo.instance_metrics_daily.find_one({"instance_id": "bot-01", "date_utc": "2026-08-27"})
        self.assertEqual(daily_doc["interaction_count"], 5)  # no double count

    def test_reclaimed_owner_interleaving_is_idempotent_and_completion_fenced(self):
        payload = _payload(self.current(registered=12), self.daily(count=6, interactions=18))
        payload_hash = __import__("hashlib").sha256(
            __import__("control_plane").canonical_json(payload).encode("utf-8")
        ).hexdigest()
        batch_id = payload["batch_id"]

        status_a, claim_a = self.repo._claim_batch(
            batch_id, "bot-01", payload_hash, NOW, NOW, 300
        )
        self.assertEqual(status_a, "accepted")
        self.repo.metrics_batches.update_one(
            {"_id": batch_id},
            {"$set": {"lease_expires_at": NOW - timedelta(seconds=1)}},
        )
        status_b, claim_b = self.repo._claim_batch(
            batch_id, "bot-01", payload_hash, NOW, NOW, 300
        )
        self.assertEqual(status_b, "accepted")
        self.assertNotEqual(claim_a["processing_token"], claim_b["processing_token"])
        self.assertEqual(claim_b["processing_generation"], 2)

        # A's late writes are safe at-least-once writes, but A cannot complete.
        self.repo._write_current_aggregate("bot-01", batch_id, payload["current"], NOW, NOW)
        for item in payload["daily"]:
            self.repo._write_daily_aggregate(
                "bot-01", batch_id, item["date_utc"], item["active_users"],
                item["interaction_count"], NOW, NOW,
            )
        old_completion = self.repo.metrics_batches.update_one(
            {"_id": batch_id, "status": "processing", "processing_token": claim_a["processing_token"],
             "processing_generation": claim_a["processing_generation"]},
            {"$set": {"status": "completed"}},
        )
        self.assertEqual(old_completion.matched_count, 0)

        # B repeats the same absolute aggregate writes and completes successfully.
        self.repo._write_current_aggregate("bot-01", batch_id, payload["current"], NOW, NOW + timedelta(seconds=1))
        for item in payload["daily"]:
            self.repo._write_daily_aggregate(
                "bot-01", batch_id, item["date_utc"], item["active_users"],
                item["interaction_count"], NOW, NOW + timedelta(seconds=1),
            )
        completion = self.repo.metrics_batches.update_one(
            {"_id": batch_id, "status": "processing", "processing_token": claim_b["processing_token"],
             "processing_generation": claim_b["processing_generation"]},
            {"$set": {"status": "completed"}},
        )
        self.assertEqual(completion.matched_count, 1)

        # A's final late duplicate remains idempotent and cannot alter completion.
        self.repo._write_current_aggregate("bot-01", batch_id, payload["current"], NOW, NOW + timedelta(seconds=2))
        for item in payload["daily"]:
            self.repo._write_daily_aggregate(
                "bot-01", batch_id, item["date_utc"], item["active_users"],
                item["interaction_count"], NOW, NOW + timedelta(seconds=2),
            )
        current_doc = self.repo.instance_metrics_current.find_one({"instance_id": "bot-01"})
        daily_doc = self.repo.instance_metrics_daily.find_one({"instance_id": "bot-01", "date_utc": "2026-08-27"})
        batch_doc = self.repo.metrics_batches.find_one({"_id": batch_id})
        self.assertEqual(
            {key: current_doc[key] for key in (
                "registered_users", "reachable_users", "active_24h", "active_7d", "active_30d",
                "observed_at", "batch_id", "metrics_schema_version", "privacy_mode",
            )},
            {**payload["current"], "observed_at": NOW, "batch_id": batch_id,
             "metrics_schema_version": "1", "privacy_mode": "aggregate_only"},
        )
        self.assertEqual(
            {key: daily_doc[key] for key in (
                "active_users", "interaction_count", "observed_at", "batch_id",
                "metrics_schema_version", "privacy_mode", "date_utc",
            )},
            {"active_users": 6, "interaction_count": 18, "observed_at": NOW,
             "batch_id": batch_id, "metrics_schema_version": "1",
             "privacy_mode": "aggregate_only", "date_utc": "2026-08-27"},
        )
        self.assertEqual(batch_doc["status"], "completed")
        self.assertEqual(batch_doc["processing_generation"], 2)
        self.assertNotIn("processing_token", current_doc)
        self.assertNotIn("processing_generation", current_doc)
        self.assertNotIn("processing_token", daily_doc)
        self.assertNotIn("processing_generation", daily_doc)

    def test_newer_batch_wins_over_late_old_aggregate_write(self):
        old = _payload(self.current(registered=10), self.daily(count=2, interactions=4), observed_at=NOW)
        newer_time = NOW + timedelta(minutes=5)
        new = _payload(self.current(registered=20), self.daily(count=8, interactions=16, observed_at=newer_time), observed_at=newer_time)
        self.repo._ingest_metrics(new, NOW, 300)
        self.repo._write_current_aggregate("bot-01", old["batch_id"], old["current"], NOW, NOW)
        self.repo._write_daily_aggregate("bot-01", old["batch_id"], "2026-08-27", 2, 4, NOW, NOW)
        current_doc = self.repo.instance_metrics_current.find_one({"instance_id": "bot-01"})
        daily_doc = self.repo.instance_metrics_daily.find_one({"instance_id": "bot-01", "date_utc": "2026-08-27"})
        self.assertEqual(
            {key: current_doc[key] for key in (
                "registered_users", "reachable_users", "active_24h", "active_7d", "active_30d",
                "observed_at", "batch_id", "metrics_schema_version", "privacy_mode",
            )},
            {**new["current"], "observed_at": newer_time, "batch_id": new["batch_id"],
             "metrics_schema_version": "1", "privacy_mode": "aggregate_only"},
        )
        self.assertEqual(
            {key: daily_doc[key] for key in (
                "active_users", "interaction_count", "observed_at", "batch_id",
                "metrics_schema_version", "privacy_mode", "date_utc",
            )},
            {"active_users": 8, "interaction_count": 16, "observed_at": newer_time,
             "batch_id": new["batch_id"], "metrics_schema_version": "1",
             "privacy_mode": "aggregate_only", "date_utc": "2026-08-27"},
        )
        self.assertNotIn("processing_token", current_doc)
        self.assertNotIn("processing_generation", current_doc)
        self.assertNotIn("processing_token", daily_doc)
        self.assertNotIn("processing_generation", daily_doc)

    def test_cleanup_removes_only_terminal_batches(self):
        old = NOW - timedelta(days=40)
        new = NOW - timedelta(days=1)
        self.repo.metrics_batches.insert_one(
            {"_id": "done-old", "status": "completed", "completed_at": old, "instance_id": "bot-01"}
        )
        self.repo.metrics_batches.insert_one(
            {"_id": "perm-old", "status": "permanent_failure", "completed_at": old, "instance_id": "bot-01"}
        )
        self.repo.metrics_batches.insert_one(
            {"_id": "done-new", "status": "completed", "completed_at": new, "instance_id": "bot-01"}
        )
        self.repo.metrics_batches.insert_one(
            {"_id": "processing-active", "status": "processing", "lease_expires_at": NOW + timedelta(seconds=300), "instance_id": "bot-01"}
        )
        self.repo.metrics_batches.insert_one(
            {"_id": "processing-stale", "status": "processing", "lease_expires_at": NOW - timedelta(seconds=5), "instance_id": "bot-01"}
        )
        removed = self.repo._cleanup_metrics_batches(30, 100)
        self.assertEqual(removed, 2)
        remaining = {d["_id"] for d in self.repo.metrics_batches.find({})}
        self.assertNotIn("done-old", remaining)
        self.assertNotIn("perm-old", remaining)
        self.assertIn("done-new", remaining)
        self.assertIn("processing-active", remaining)
        self.assertIn("processing-stale", remaining)

    def test_metrics_summary_per_instance_and_global_observations(self):
        from control_plane import aggregate_payload

        day = self.daily()
        first = aggregate_payload(instance_id="bot-01", observed_at=NOW, current=self.current(registered=100), daily=day)
        second = aggregate_payload(instance_id="bot-02", observed_at=NOW, current=self.current(registered=50), daily=day)
        self.repo._ingest_metrics(first, NOW, 300)
        self.repo._ingest_metrics(second, NOW, 300)
        global_summary = self.repo._metrics_summary(NOW, None)
        self.assertEqual(global_summary["instance_count"], 2)
        self.assertEqual(global_summary["global_registered_users_observations"], 150)
        per_instance = self.repo._metrics_summary(NOW, "bot-01")
        self.assertEqual(per_instance["instance_count"], 1)
        self.assertEqual(per_instance["global_registered_users_observations"], 100)


class LocalOutboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from unittest.mock import patch
        import os

        dummy = {
            "TG_BOT_TOKEN": "123:TESTTOKEN",
            "APP_ID": "123456",
            "API_HASH": "0" * 32,
            "CHANNEL_ID": "-100123",
            "OWNER_ID": "1",
            "DATABASE_URL": "mongodb://localhost:27017/test_teledrop",
            "DATABASE_NAME": "test_teledrop",
        }
        self.env_patch = patch.dict(os.environ, dummy, clear=False)
        self.env_patch.start()

        import database.database as db_module

        self.outbox = FakeCollection()
        self.patcher_outbox = patch.object(db_module, "metrics_outbox_data", self.outbox)
        self.patcher_outbox.start()

    def tearDown(self):
        self.patcher_outbox.stop()
        self.env_patch.stop()

    def _record(self, instance_id="bot-01", label="b1", status="pending", next_attempt_at=NOW):
        return {
            "_id": instance_id + "-" + label,
            "instance_id": instance_id,
            "batch_id": instance_id + "-" + label,
            "metrics_schema_version": "1",
            "privacy_mode": "aggregate_only",
            "canonical_payload": "{}",
            "status": status,
            "attempts": 0,
            "created_at": NOW,
            "updated_at": NOW,
            "next_attempt_at": next_attempt_at,
        }

    async def test_single_active_outbox_enforced(self):
        import database.database as db

        self.assertTrue(await db.create_metrics_outbox(self._record()))
        self.assertFalse(await db.create_metrics_outbox(self._record("bot-01")))
        # A different instance may have its own active batch.
        self.assertTrue(await db.create_metrics_outbox(self._record("bot-02")))

    async def test_blocked_auth_keeps_slot_and_no_new_batch(self):
        import database.database as db

        first = self._record()
        self.assertTrue(await db.create_metrics_outbox(first))
        claimed = await db.claim_metrics_outbox(first["_id"], 300, NOW)
        await db.mark_metrics_outbox_blocked_auth(
            first["_id"],
            owner=claimed["sending_owner"],
            claim_generation=claimed["claim_generation"],
            attempts=1,
            next_attempt_at=NOW + timedelta(hours=1),
            error_class="permanent_http_401",
            now=NOW,
        )
        active = await db.get_active_metrics_outbox("bot-01")
        self.assertIsNotNone(active)
        self.assertEqual(active["status"], "blocked_auth")
        self.assertEqual(active["active_slot"], "sync")
        # No new snapshot while blocked.
        self.assertFalse(await db.create_metrics_outbox(self._record("bot-01")))

    async def test_accepted_releases_slot(self):
        import database.database as db

        first = self._record()
        self.assertTrue(await db.create_metrics_outbox(first))
        claimed = await db.claim_metrics_outbox(first["_id"], 300, NOW)
        await db.mark_metrics_outbox_accepted(
            first["_id"],
            owner=claimed["sending_owner"],
            claim_generation=claimed["claim_generation"],
            now=NOW,
        )
        self.assertIsNone(await db.get_active_metrics_outbox("bot-01"))
        self.assertTrue(await db.create_metrics_outbox(self._record("bot-01", label="b2")))

    async def test_permanent_failure_releases_slot(self):
        import database.database as db

        first = self._record()
        self.assertTrue(await db.create_metrics_outbox(first))
        claimed = await db.claim_metrics_outbox(first["_id"], 300, NOW)
        await db.mark_metrics_outbox_permanent(
            first["_id"],
            owner=claimed["sending_owner"],
            claim_generation=claimed["claim_generation"],
            attempts=5,
            error_class="permanent_http_400",
            now=NOW,
        )
        self.assertIsNone(await db.get_active_metrics_outbox("bot-01"))
        self.assertTrue(await db.create_metrics_outbox(self._record("bot-01", label="b2")))

    async def test_valid_sending_lease_cannot_be_claimed_again(self):
        import database.database as db

        record = self._record()
        self.assertTrue(await db.create_metrics_outbox(record))
        first = await db.claim_metrics_outbox(record["_id"], 300, NOW)
        second = await db.claim_metrics_outbox(record["_id"], 300, NOW + timedelta(seconds=1))
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_retryable_future_is_not_claimable_but_due_is_claimable(self):
        import database.database as db

        future = self._record(status="retryable", next_attempt_at=NOW + timedelta(minutes=1))
        self.assertTrue(await db.create_metrics_outbox(future))
        self.assertIsNone(await db.claim_metrics_outbox(future["_id"], 300, NOW))
        due = NOW + timedelta(minutes=1)
        claimed = await db.claim_metrics_outbox(future["_id"], 300, due)
        self.assertIsNotNone(claimed)

    async def test_blocked_auth_future_is_not_claimable_but_due_is_claimable(self):
        import database.database as db

        record = self._record(status="blocked_auth", next_attempt_at=NOW + timedelta(minutes=1))
        self.assertTrue(await db.create_metrics_outbox(record))
        self.assertIsNone(await db.claim_metrics_outbox(record["_id"], 300, NOW))
        claimed = await db.claim_metrics_outbox(record["_id"], 300, NOW + timedelta(minutes=1))
        self.assertIsNotNone(claimed)

    async def test_missing_next_attempt_at_is_legacy_due(self):
        import database.database as db

        record = self._record(status="retryable")
        record.pop("next_attempt_at")
        self.assertTrue(await db.create_metrics_outbox(record))
        self.assertIsNotNone(await db.claim_metrics_outbox(record["_id"], 300, NOW))

    async def test_repeated_reclaim_generation_is_monotonic_without_attempt_increment(self):
        import database.database as db

        record = self._record()
        self.assertTrue(await db.create_metrics_outbox(record))
        first = await db.claim_metrics_outbox(record["_id"], 1, NOW)
        second = await db.claim_metrics_outbox(record["_id"], 1, NOW + timedelta(seconds=2))
        third = await db.claim_metrics_outbox(record["_id"], 1, NOW + timedelta(seconds=4))
        self.assertEqual((first["claim_generation"], second["claim_generation"], third["claim_generation"]), (1, 2, 3))
        current = await db.get_metrics_outbox(record["_id"])
        self.assertEqual(current["attempts"], 0)

    async def test_all_old_owner_transitions_are_rejected(self):
        import database.database as db

        record = self._record()
        self.assertTrue(await db.create_metrics_outbox(record))
        first = await db.claim_metrics_outbox(record["_id"], 1, NOW)
        second = await db.claim_metrics_outbox(record["_id"], 1, NOW + timedelta(seconds=2))
        kwargs = {
            "owner": first["sending_owner"],
            "claim_generation": first["claim_generation"],
            "now": NOW + timedelta(seconds=3),
        }
        retry = await db.mark_metrics_outbox_retryable(
            record["_id"], next_attempt_at=NOW + timedelta(hours=1), attempts=1,
            error_class="retryable_http_503", **kwargs,
        )
        blocked = await db.mark_metrics_outbox_blocked_auth(
            record["_id"], next_attempt_at=NOW + timedelta(hours=1), attempts=1,
            error_class="permanent_http_401", **kwargs,
        )
        accepted = await db.mark_metrics_outbox_accepted(record["_id"], **kwargs)
        permanent = await db.mark_metrics_outbox_permanent(
            record["_id"], attempts=1, error_class="permanent_http_400", **kwargs,
        )
        self.assertEqual({retry, blocked, accepted, permanent}, {"stale_result_ignored"})
        current = await db.get_metrics_outbox(record["_id"])
        self.assertEqual(current["sending_owner"], second["sending_owner"])
        self.assertEqual(current["status"], "sending")

    async def test_current_owner_retry_and_blocked_transitions_succeed(self):
        import database.database as db

        for target, marker in (("retryable", "retry"), ("blocked_auth", "blocked")):
            self.outbox.docs.clear()
            record = self._record(label=marker)
            self.assertTrue(await db.create_metrics_outbox(record))
            claim = await db.claim_metrics_outbox(record["_id"], 300, NOW)
            if target == "retryable":
                result = await db.mark_metrics_outbox_retryable(
                    record["_id"], owner=claim["sending_owner"], claim_generation=claim["claim_generation"],
                    next_attempt_at=NOW + timedelta(minutes=1), attempts=1,
                    error_class="retryable_http_503", now=NOW,
                )
            else:
                result = await db.mark_metrics_outbox_blocked_auth(
                    record["_id"], owner=claim["sending_owner"], claim_generation=claim["claim_generation"],
                    next_attempt_at=NOW + timedelta(minutes=1), attempts=1,
                    error_class="permanent_http_401", now=NOW,
                )
            self.assertEqual(result, "transitioned")

    async def test_expired_sending_claim_gets_new_owner_and_generation(self):
        import database.database as db

        record = self._record()
        self.assertTrue(await db.create_metrics_outbox(record))
        first = await db.claim_metrics_outbox(record["_id"], 10, NOW)
        expired = NOW + timedelta(seconds=11)
        second = await db.claim_metrics_outbox(record["_id"], 10, expired)
        self.assertNotEqual(first["sending_owner"], second["sending_owner"])
        self.assertEqual(second["claim_generation"], first["claim_generation"] + 1)
        self.assertEqual(second["attempts"], first["attempts"])

    async def test_old_owner_terminal_transition_is_ignored_after_reclaim(self):
        import database.database as db

        record = self._record()
        self.assertTrue(await db.create_metrics_outbox(record))
        first = await db.claim_metrics_outbox(record["_id"], 10, NOW)
        second = await db.claim_metrics_outbox(record["_id"], 10, NOW + timedelta(seconds=11))
        result = await db.mark_metrics_outbox_accepted(
            record["_id"],
            owner=first["sending_owner"],
            claim_generation=first["claim_generation"],
            now=NOW + timedelta(seconds=12),
        )
        self.assertEqual(result, "stale_result_ignored")
        current = await db.get_metrics_outbox(record["_id"])
        self.assertEqual(current["status"], "sending")
        self.assertEqual(current["sending_owner"], second["sending_owner"])

    async def test_current_owner_releases_slot_and_old_owner_cannot(self):
        import database.database as db

        record = self._record()
        self.assertTrue(await db.create_metrics_outbox(record))
        first = await db.claim_metrics_outbox(record["_id"], 10, NOW)
        second = await db.claim_metrics_outbox(record["_id"], 10, NOW + timedelta(seconds=11))
        stale = await db.mark_metrics_outbox_permanent(
            record["_id"],
            owner=first["sending_owner"],
            claim_generation=first["claim_generation"],
            attempts=1,
            error_class="permanent_http_400",
            now=NOW + timedelta(seconds=12),
        )
        self.assertEqual(stale, "stale_result_ignored")
        accepted = await db.mark_metrics_outbox_accepted(
            record["_id"],
            owner=second["sending_owner"],
            claim_generation=second["claim_generation"],
            now=NOW + timedelta(seconds=13),
        )
        self.assertEqual(accepted, "transitioned")
        self.assertIsNone(await db.get_active_metrics_outbox("bot-01"))


class LocalAggregateTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        import os

        dummy = {
            "TG_BOT_TOKEN": "123:TESTTOKEN",
            "APP_ID": "123456",
            "API_HASH": "0" * 32,
            "CHANNEL_ID": "-100123",
            "OWNER_ID": "1",
            "DATABASE_URL": "mongodb://localhost:27017/test_teledrop",
            "DATABASE_NAME": "test_teledrop",
        }
        self.env_patch = patch.dict(os.environ, dummy, clear=False)
        self.env_patch.start()

        import database.database as db_module

        self.user_data = FakeCollection()
        self.daily_data = FakeCollection()
        self.patcher_user = patch.object(db_module, "user_data", self.user_data)
        self.patcher_daily = patch.object(db_module, "daily_activity_data", self.daily_data)
        self.patcher_user.start()
        self.patcher_daily.start()

    def tearDown(self):
        self.patcher_user.stop()
        self.patcher_daily.stop()
        self.env_patch.stop()

    def seed_user(self, user_id, last_seen_at, blocked_at=None, deleted_at=None):
        self.user_data.docs[user_id] = {
            "_id": user_id,
            "last_seen_at": last_seen_at,
            "blocked_at": blocked_at,
            "deleted_at": deleted_at,
        }

    def test_empty_database_yields_zero_counts(self):
        import database.database as db_module

        snapshot = db_module._metrics_snapshot(NOW, daily_days=30)
        self.assertEqual(snapshot["current"]["registered_users"], 0)
        self.assertEqual(snapshot["current"]["reachable_users"], 0)
        self.assertFalse(any(key.startswith("user_id") for key in snapshot.keys()))

    def test_blocked_and_deleted_excluded_from_reachable(self):
        import database.database as db_module

        self.seed_user(1, NOW)
        self.seed_user(2, NOW, blocked_at=NOW)
        self.seed_user(3, NOW, deleted_at=NOW)
        snapshot = db_module._metrics_snapshot(NOW, daily_days=30)
        current = snapshot["current"]
        self.assertEqual(current["registered_users"], 3)
        self.assertEqual(current["reachable_users"], 1)
        self.assertEqual(current["active_24h"], 1)

    def test_active_rolling_windows(self):
        import database.database as db_module

        from datetime import timedelta

        self.seed_user(1, NOW)
        self.seed_user(2, NOW - timedelta(days=6))
        self.seed_user(3, NOW - timedelta(days=29))
        snapshot = db_module._metrics_snapshot(NOW, daily_days=30)
        current = snapshot["current"]
        self.assertEqual(current["active_24h"], 1)
        self.assertEqual(current["active_7d"], 2)
        self.assertEqual(current["active_30d"], 3)

    def test_daily_rollup_values_and_no_identifiers(self):
        import database.database as db_module

        for user_id, count in ((1, 3), (2, 5)):
            self.daily_data.docs[f"2026-08-27:{user_id}"] = {
                "date": "2026-08-27",
                "user_id": user_id,
                "interaction_count": count,
            }
        snapshot = db_module._metrics_snapshot(NOW, daily_days=30)
        today = next(item for item in snapshot["daily"] if item["date_utc"] == "2026-08-27")
        self.assertEqual(today["active_users"], 2)
        self.assertEqual(today["interaction_count"], 8)
        body = snapshot["current"]
        self.assertNotIn("user_id", body)
        self.assertNotIn("_id", body)

    def test_utc_date_boundary(self):
        import database.database as db_module

        boundary = datetime(2026, 8, 27, 23, 59, 59, tzinfo=timezone.utc)
        self.seed_user(1, boundary)
        snapshot = db_module._metrics_snapshot(boundary, daily_days=30)
        self.assertTrue(any(item["date_utc"] == "2026-08-27" for item in snapshot["daily"]))

    def test_blocked_user_kept_in_historical_daily_but_excluded_current(self):
        import database.database as db_module

        self.seed_user(1, NOW)
        self.daily_data.docs["2026-08-27:1"] = {
            "date": "2026-08-27",
            "user_id": 1,
            "interaction_count": 3,
        }
        before = db_module._metrics_snapshot(NOW, daily_days=30)
        self.assertEqual(before["current"]["reachable_users"], 1)
        # User blocks the bot after the recorded interaction.
        self.user_data.docs[1]["blocked_at"] = NOW
        after = db_module._metrics_snapshot(NOW, daily_days=30)
        self.assertEqual(after["current"]["reachable_users"], 0)
        self.assertEqual(after["current"]["active_24h"], 0)
        today = next(item for item in after["daily"] if item["date_utc"] == "2026-08-27")
        self.assertEqual(today["active_users"], 1)
        self.assertEqual(today["interaction_count"], 3)


class LocalOutboxIndexRegressionTests(unittest.TestCase):
    """Phase 2B regression: local _ensure_indexes() must never request an
    explicit (unique) index on _id, which MongoDB rejects (code 197,
    InvalidIndexSpecificationOption). metrics_outbox._id is the canonical batch
    identity and is already unique via MongoDB's built-in _id_; the
    instance_status and single_active operational indexes must remain available
    with their exact unique/partial semantics."""

    def _local_index_requests(self):
        import os
        from unittest.mock import patch

        dummy = {
            "TG_BOT_TOKEN": "123:TESTTOKEN",
            "APP_ID": "123456",
            "API_HASH": "0" * 32,
            "CHANNEL_ID": "-100123",
            "OWNER_ID": "1",
            "DATABASE_URL": "mongodb://localhost:27017/test_teledrop",
            "DATABASE_NAME": "test_teledrop",
        }
        env_patch = patch.dict(os.environ, dummy, clear=False)
        env_patch.start()
        try:
            import database.database as db_module

            collections = {}
            for attr in ("user_data", "daily_activity_data", "metrics_outbox_data",
                         "link_data", "delivery_data"):
                collections[attr] = FakeCollection()
            patchers = [patch.object(db_module, attr, collections[attr]) for attr in collections]
            for patcher in patchers:
                patcher.start()
            try:
                db_module._ensure_indexes()
                return collections["metrics_outbox_data"].index_requests
            finally:
                for patcher in patchers:
                    patcher.stop()
        finally:
            env_patch.stop()

    def test_local_ensure_indexes_never_requests_explicit_id_index(self):
        for req in self._local_index_requests():
            self.assertNotIn(
                "_id", req["fields"],
                "local _ensure_indexes requested an explicit _id index: %r" % req,
            )
            self.assertNotEqual(
                req.get("name"), "metrics_outbox_batch_id",
                "local _ensure_indexes requested the forbidden metrics_outbox_batch_id",
            )

    def test_local_keep_instance_status_and_single_active_indexes(self):
        requests = self._local_index_requests()
        by_name = {req["name"]: req for req in requests}
        self.assertIn("metrics_outbox_instance_status", by_name)
        single = by_name.get("metrics_outbox_single_active")
        self.assertIsNotNone(single, "metrics_outbox_single_active index missing")
        self.assertTrue(single["unique"], "metrics_outbox_single_active must be unique")
        self.assertEqual(
            single["partial_filter"],
            {"active_slot": {"$exists": True}},
            "metrics_outbox_single_active partial filter must match production",
        )
        self.assertNotIn("metrics_outbox_batch_id", by_name)
        # instance_status must be a plain (non-unique) compound index.
        self.assertFalse(
            by_name["metrics_outbox_instance_status"]["unique"],
            "metrics_outbox_instance_status must not be unique",
        )

    def test_local_invalid_unique_id_index_is_not_silently_accepted(self):
        # The fake must not hide the invalid unique _id index the way real
        # MongoDB rejects it (code 197). Re-introducing metrics_outbox_batch_id
        # must fail loudly.
        with self.assertRaises(OperationFailure):
            FakeCollection().create_index(
                [("_id", pymongo.ASCENDING)],
                unique=True,
                name="metrics_outbox_batch_id",
            )

    def test_builtin_id_uniqueness_is_not_faked_as_custom_index(self):
        # built-in _id_ uniqueness is inherent to MongoDB and must never be
        # emulated by requesting a custom (named) _id index in _ensure_indexes();
        # single_active is the only index that may carry a partial filter.
        requests = self._local_index_requests()
        for req in requests:
            self.assertNotIn("_id", req["fields"])
        partial_names = {req["name"] for req in requests if req.get("partial_filter") is not None}
        self.assertEqual(partial_names, {"metrics_outbox_single_active"})


class RepositoryIndexRegressionTests(unittest.TestCase):
    """Phase 2B regression: server _ensure_indexes() must never request an
    explicit (unique) index on _id, which MongoDB rejects (code 197,
    InvalidIndexSpecificationOption). metrics_batches._id is the canonical batch
    identity and is already unique via MongoDB's built-in _id_; the claim and
    cleanup operational indexes must remain available."""

    def _server_index_requests(self):
        repo = ControlPlaneRepository(FakeDatabase())
        repo._ensure_indexes()
        return {
            attr: list(getattr(repo, attr).index_requests)
            for attr in ("agents", "nonces", "metrics_batches",
                         "instance_metrics_current", "instance_metrics_daily")
        }

    def test_ensure_indexes_never_requests_explicit_id_index(self):
        for attr, requests in self._server_index_requests().items():
            for req in requests:
                self.assertNotIn(
                    "_id", req["fields"],
                    "%s requested an explicit _id index: %r" % (attr, req),
                )
                self.assertNotEqual(
                    req.get("name"), "metrics_batches_id",
                    "%s requested the forbidden metrics_batches_id" % attr,
                )

    def test_ensure_indexes_keeps_claim_and_cleanup_indexes(self):
        requests = self._server_index_requests()["metrics_batches"]
        names = {req["name"] for req in requests}
        self.assertIn("metrics_batches_claim", names)
        self.assertIn("metrics_batches_cleanup", names)
        self.assertNotIn("metrics_batches_id", names)

    def test_invalid_unique_id_index_is_not_silently_accepted(self):
        # The fake must not hide the invalid unique _id index the way real MongoDB
        # rejects it. Re-introducing metrics_batches_id must fail loudly.
        repo = ControlPlaneRepository(FakeDatabase())
        with self.assertRaises(OperationFailure):
            repo.metrics_batches.create_index(
                [("_id", pymongo.ASCENDING)], unique=True, name="metrics_batches_id"
            )


if __name__ == "__main__":
    unittest.main()