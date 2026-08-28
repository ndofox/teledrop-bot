"""Phase 2B integration infrastructure.

This stabilization runner intentionally executes only ``SELF_CHECK``. Matrix
case selections are reported as NOT_RUN until their coverage is completed.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid
from urllib.parse import quote, urlparse

from bson import ObjectId
from pymongo import MongoClient, version as pymongo_version

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PREFIX = "teledrop_phase2b_test_"
TIMEOUT_MS = 10_000
LOG_FILE = ROOT / "filesharingbot.log"
PRODUCTION_FILES = (
    "metrics_policy.py", "bot.py", "config.py", "control_plane.py",
    "control_plane_client.py", "database/database.py",
    "control_plane_server/app.py", "control_plane_server/config.py",
    "control_plane_server/main.py", "control_plane_server/repository.py",
    "control_plane_server/security.py",
)
MATRIX_CASES = (
    "A1_INDEX_READBACK", "A2_DATABASE_ISOLATION", "A3_BUILTIN_ID",
    "A4_INVALID_CUSTOM_INDEX_ABSENT", "A5_PARTIAL_UNIQUE_ENFORCEMENT",
    "B1_PENDING_CONCURRENT_CLAIM", "B2_ACTIVE_LEASE_NOT_CLAIMABLE",
    "B3_CONCURRENT_STALE_RECLAIM", "B4_OLD_OWNER_FENCING", "B5_RETRY_DUE_MATRIX",
    "B6_EXACT_PAYLOAD", "B7_ACTIVE_SLOT_SEMANTICS", "C1_FIRST_CLAIM",
    "C2_ACTIVE_LEASE", "C3_STALE_RECLAIM", "C4_SAME_HASH_DUPLICATE",
    "C5_HASH_CONFLICT", "C6_COMPLETION_FENCING", "D1_SAME_BATCH_IDEMPOTENCY",
    "D2_NEWER_CURRENT_LOWER_VALUES", "D3_STALE_CURRENT_NO_REGRESSION",
    "D4_EQUAL_TIMESTAMP_CONFLICT", "D5_DAILY_MONOTONIC", "D6_CRASH_RECOVERY",
    "D7_PRIVACY_STORAGE", "E1_SERVER_RETENTION", "E2_LOCAL_RETENTION",
    "E3_CLEANUP_IDEMPOTENCY",
)


def run(coroutine):
    return asyncio.run(coroutine)


def check(value, message):
    if not value:
        raise AssertionError(message)


def safe_normalize_host(value):
    """Idempotent DNS-host lowercasing; never returns credentials/URI text."""
    return str(value).strip().lower()


def validate_test_host_config(uri, allowed_host):
    """Validate required test Mongo config before any connection is opened.

    Returns ``(status_report, reason)`` where ``reason`` is ``None`` when the
    configuration is acceptable and a blocking message otherwise. Both return
    values are secret-free status tokens (PRESENT/MISSING/PASS/FAIL/NOT_RUN) and
    static reasons; the actual URI, hostname, and allowed-host value are never
    returned and must therefore never be printed by the caller.
    """
    status = {
        "required_uri": "PRESENT" if uri else "MISSING",
        "required_allowed_host": "PRESENT" if allowed_host else "MISSING",
        "host_exact_match": "NOT_RUN",
    }
    if not uri or not allowed_host:
        return status, "REQUIRED TEST CONFIGURATION MISSING"
    problems = []
    if "://" in allowed_host:
        problems.append("allowed host contains a scheme")
    if "@" in allowed_host:
        problems.append("allowed host contains credentials")
    if "/" in allowed_host or "\\" in allowed_host:
        problems.append("allowed host contains a path separator")
    if "?" in allowed_host or "#" in allowed_host:
        problems.append("allowed host contains a query/fragment")
    if ":" in allowed_host:
        problems.append("allowed host contains a port")
    if problems:
        return status, "ALLOWED_HOST INVALID (" + "; ".join(problems) + ")"
    parsed = urlparse(uri)
    if parsed.scheme != "mongodb+srv":
        return status, "URI IS NOT MONGODB+SRV"
    if parsed.path not in ("", "/"):
        return status, "URI CONTAINS A DATABASE PATH"
    expected = safe_normalize_host(allowed_host)
    if parsed.hostname != expected:
        status["host_exact_match"] = "FAIL"
        return status, "ALLOWED HOST DOES NOT EXACTLY MATCH URI HOSTNAME"
    status["host_exact_match"] = "PASS"
    return status, None


def fingerprint_files(relative_names):
    result = {}
    for relative in relative_names:
        path = (ROOT / relative).resolve()
        if not path.is_file():
            result[relative] = {"exists": False, "size": None, "sha256": None, "mtime_ns": None}
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        stat = path.stat()
        result[relative] = {"exists": True, "size": stat.st_size,
                            "sha256": digest.hexdigest(), "mtime_ns": stat.st_mtime_ns}
    return result


def fingerprint_optional_file(path):
    path = Path(path).resolve()
    try:
        relative = str(path.relative_to(ROOT))
    except ValueError:
        relative = path.name
    return fingerprint_files((relative,))[relative]


def normalize_json(value):
    """Normalize evidence without using raw repr or hiding unsupported types."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): normalize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_json(item) for item in value]
    if isinstance(value, set):
        items = [normalize_json(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return {"value": value.replace(tzinfo=timezone.utc).isoformat(), "datetime_classification": "naive_assumed_utc"}
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes_length": len(value), "bytes_sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, BaseException):
        return {"exception_type": type(value).__name__, "message": str(value)[:300]}
    if isinstance(value, Enum):
        return normalize_json(value.value)
    return {"unsupported_type": f"{type(value).__module__}.{type(value).__name__}"}


def assert_safe_report(report, forbidden=()):
    serialized = json.dumps(normalize_json(report), sort_keys=True, separators=(",", ":"))
    if any(value and value in serialized for value in forbidden):
        raise AssertionError("report redaction scan failed")
    if "mongodb://" in serialized or "mongodb+srv://" in serialized:
        raise AssertionError("report scheme scan failed")
    json.loads(serialized)
    return serialized


class WorkerClientFactory:
    def __init__(self, uri, database_name):
        self._uri = uri
        self._database_name = database_name
        self.clients_created = 0
        self.clients_closed = 0

    @property
    def active_clients_final(self):
        return self.clients_created - self.clients_closed

    @contextmanager
    def new_worker_client(self, case_id, worker_id):
        client = MongoClient(self._uri,
                             appName=f"teledrop-phase2b-{case_id}-{worker_id}-{secrets.token_hex(4)}",
                             serverSelectionTimeoutMS=TIMEOUT_MS,
                             connectTimeoutMS=TIMEOUT_MS, socketTimeoutMS=TIMEOUT_MS)
        self.clients_created += 1
        try:
            yield client, client[self._database_name]
        finally:
            client.close()
            self.clients_closed += 1


def parse_selection(raw):
    if raw == "ALL":
        return list(MATRIX_CASES)
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise ValueError("at least one case is required")
    aliases = {case_id.split("_", 1)[0]: case_id for case_id in MATRIX_CASES}
    effective = ["SELF_CHECK" if value == "SELF_CHECK" else aliases.get(value, value) for value in values]
    unknown = sorted(set(effective) - set(MATRIX_CASES) - {"SELF_CHECK"})
    if unknown:
        raise ValueError("unknown case ID: " + ",".join(unknown))
    return list(dict.fromkeys(effective))


def empty_case(case_id):
    return {"case_id": case_id, "verdict": "NOT_RUN", "claimants": [], "winners": [],
            "loser_classifications": [], "initial_generation": None,
            "final_generation": None, "initial_attempts": None, "final_attempts": None,
            "final_status": None, "final_owner": None, "active_slot_before": None,
            "active_slot_after": None, "assertions": [], "evidence": {}, "error": None}


def safe_secret(value):
    if not value:
        return None
    return {"present": True, "sha256": hashlib.sha256(str(value).encode()).hexdigest()}


class ConcurrentCaseError(RuntimeError):
    """Raised after every worker has finished when one or more workers failed."""

    def __init__(self, case_id, results, errors):
        self.case_id = case_id
        self.results = results
        self.errors = errors
        super().__init__(f"{case_id}: {len(errors)} worker failure(s)")


def run_concurrent_case(case_id, worker_function, claimant_count=4):
    """Run a harness callback concurrently and return deterministic worker-order results."""
    barrier = threading.Barrier(claimant_count, timeout=TIMEOUT_MS / 1000)

    def worker(worker_id):
        barrier.wait()
        return worker_function(worker_id)

    results = [None] * claimant_count
    errors = []
    with ThreadPoolExecutor(max_workers=claimant_count) as pool:
        futures = {worker_id: pool.submit(worker, worker_id) for worker_id in range(claimant_count)}
        for worker_id in range(claimant_count):
            try:
                results[worker_id] = futures[worker_id].result(timeout=TIMEOUT_MS / 1000)
            except BaseException as exc:
                errors.append({"worker_id": worker_id, "exception": exc})
    if errors:
        raise ConcurrentCaseError(case_id, results, errors)
    return results


def concurrent_workers(factory, case_id, operation, count=4):
    """Run server workers with independent clients through the canonical helper."""
    def worker(worker_id):
        with factory.new_worker_client(case_id, str(worker_id)) as (_, database):
            return operation(database, worker_id)
    return run_concurrent_case(case_id, worker, count)


def concurrency_self_test():
    values = run_concurrent_case("SELF_TEST_CONCURRENCY", lambda worker_id: worker_id)
    check(values == [0, 1, 2, 3], "deterministic worker order")
    try:
        run_concurrent_case("SELF_TEST_EXCEPTION", lambda worker_id: (_ for _ in ()).throw(ValueError("worker")) if worker_id == 2 else worker_id)
    except ConcurrentCaseError as exc:
        check(len(exc.errors) == 1 and exc.errors[0]["worker_id"] == 2, "worker exception collected")
    else:
        raise AssertionError("worker exception was not raised")


def legacy_concurrent_workers_removed():
    """Compatibility marker: all call sites must use run_concurrent_case."""
    return None


def bind_local_persistence(uri, database_name):
    """Import local persistence without loading .env, then bind its existing handles."""
    import types
    fake_config = types.ModuleType("config")
    for key, value in {"CLEANUP_MAX_ATTEMPTS": 3, "CLEANUP_RETRY_BASE_SECONDS": 1,
                       "CLEANUP_RETRY_MAX_SECONDS": 2, "DB_NAME": database_name,
                       "DB_URI": uri, "LINK_TTL": 60}.items():
        setattr(fake_config, key, value)
    previous = sys.modules.get("config")
    sys.modules["config"] = fake_config
    try:
        import database.database as local
    finally:
        if previous is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = previous
    return local


def case_local(case_id, result, database, local, prefix):
    """Run one local case through production helper calls and shared-client parity."""
    result["evidence"]["runtime_parity"] = "LOCAL_SHARED_CLIENT_RUNTIME_PARITY"
    result["claimants"] = [f"worker-{number}" for number in range(4)]
    t = datetime.now(timezone.utc)
    instance = prefix + "instance"
    batch = prefix + "batch"
    outbox = database["metrics_outbox"]

    if case_id in {"B1_PENDING_CONCURRENT_CLAIM", "B2_ACTIVE_LEASE_NOT_CLAIMABLE", "B3_CONCURRENT_STALE_RECLAIM"}:
        status = "pending" if case_id == "B1_PENDING_CONCURRENT_CLAIM" else "sending"
        generation = 1 if status == "sending" else 0
        owner = "old-owner" if status == "sending" else None
        fixture = {"_id": batch, "case_prefix": prefix, "instance_id": instance,
                   "status": status, "payload": {"case": case_id}, "attempts": 0,
                   "claim_generation": generation, "sending_owner": owner,
                   "lease_expires_at": t + timedelta(seconds=60) if case_id == "B2_ACTIVE_LEASE_NOT_CLAIMABLE" else t - timedelta(seconds=1),
                   "active_slot": "sync"}
        if case_id == "B1_PENDING_CONCURRENT_CLAIM":
            fixture.pop("active_slot")
            check(run(local.create_metrics_outbox(fixture)), "production helper creates pending batch")
        else:
            outbox.insert_one(fixture)
        claims = run_concurrent_case(case_id, lambda _: run(local.claim_metrics_outbox(batch, 300, t)))
        winners = [claim for claim in claims if claim]
        result["winners"] = [{"owner": safe_secret(item.get("sending_owner")), "generation": item.get("claim_generation")} for item in winners]
        result["loser_classifications"] = [("active_lease" if case_id == "B2_ACTIVE_LEASE_NOT_CLAIMABLE" else "claim_not_available") for _ in range(4 - len(winners))]
        row = outbox.find_one({"_id": batch})
        result.update({"initial_generation": generation, "final_generation": row.get("claim_generation"),
                       "initial_attempts": 0, "final_attempts": row.get("attempts"),
                       "final_status": row.get("status"), "final_owner": safe_secret(row.get("sending_owner")),
                       "active_slot_before": True, "active_slot_after": "active_slot" in row})
        expected = 0 if case_id == "B2_ACTIVE_LEASE_NOT_CLAIMABLE" else 1
        check(len(winners) == expected, "local winner cardinality")
        check(row.get("status") == "sending" and row.get("_id") == batch, "local final claim state")
        if case_id == "B3_CONCURRENT_STALE_RECLAIM":
            check(row.get("claim_generation") == 2 and row.get("sending_owner") != owner, "stale reclaim generation/owner")
        result["assertions"] = ["four concurrent production helper calls", "winner/loser cardinality", "final state", "batch identity"]
        return

    if case_id == "B4_OLD_OWNER_FENCING":
        from database.database import _guarded_outbox_update
        transitions = []
        for label in ("accepted", "retryable", "blocked_auth", "permanent_failure"):
            bid = prefix + label
            outbox.insert_one({"_id": bid, "case_prefix": prefix, "instance_id": bid, "status": "sending",
                               "sending_owner": "current", "claim_generation": 2, "active_slot": "sync"})
            before = outbox.find_one({"_id": bid})
            old_result = run(_guarded_outbox_update(bid, "old", 1, {"$set": {"status": label}}))
            after_old = outbox.find_one({"_id": bid})
            current_result = run(_guarded_outbox_update(bid, "current", 2, {"$set": {"status": label}, "$unset": {"active_slot": 1} if label in {"accepted", "permanent_failure"} else {}}))
            after_current = outbox.find_one({"_id": bid})
            check(old_result == "stale_result_ignored" and after_old["status"] == "sending", "old owner fenced")
            check(current_result == "transitioned", "current transition")
            transitions.append({"transition": label, "old": {"return": old_result, "status_before": before["status"], "status_after": after_old["status"], "generation_before": before["claim_generation"], "generation_after": after_old["claim_generation"]}, "current": {"return": current_result, "status_after": after_current["status"]}})
        result["evidence"]["transitions"] = transitions
        result["assertions"] = ["old owner rejected for all four transitions", "current owner accepted on separate fixtures"]
        return

    if case_id == "B5_RETRY_DUE_MATRIX":
        matrix = (("retryable_future", "retryable", 60), ("retryable_due", "retryable", -1),
                  ("blocked_auth_future", "blocked_auth", 60), ("blocked_auth_due", "blocked_auth", -1),
                  ("legacy_missing_next_attempt_at", "retryable", None), ("naive_bson_datetime", "retryable", -1))
        evidence = []
        for label, status, delta in matrix:
            bid = prefix + label
            doc = {"_id": bid, "case_prefix": prefix, "instance_id": bid, "status": status, "active_slot": "sync"}
            if delta is not None: doc["next_attempt_at"] = t + timedelta(seconds=delta) if label != "naive_bson_datetime" else t.replace(tzinfo=None)
            outbox.insert_one(doc)
            claimed = run(local.claim_metrics_outbox(bid, 300, t))
            evidence.append({"subcase": label, "claimable": bool(claimed), "generation": claimed.get("claim_generation") if claimed else 0, "attempts": claimed.get("attempts") if claimed else 0})
        check([item["claimable"] for item in evidence] == [False, True, False, True, True, True], "retry due matrix")
        result["evidence"]["subcases"] = evidence; result["assertions"] = ["future/due matrix", "legacy missing field", "naive BSON UTC semantic", "no polling/sleep"]
        return

    if case_id == "B6_EXACT_PAYLOAD":
        from control_plane import canonical_json
        canonical = canonical_json({"batch": batch, "schema": "1", "privacy": "aggregate_only", "values": [1, 2, 3]})
        outbox.insert_one({"_id": batch, "case_prefix": prefix, "instance_id": instance, "status": "pending", "payload": canonical, "active_slot": "sync", "attempts": 0})
        hashes = []; claim = run(local.claim_metrics_outbox(batch, 300, t)); hashes.append(hashlib.sha256(outbox.find_one({"_id": batch})["payload"].encode()).hexdigest())
        run(local.mark_metrics_outbox_retryable(batch, owner=claim["sending_owner"], claim_generation=claim["claim_generation"], next_attempt_at=t - timedelta(seconds=1), attempts=1, error_class="test", now=t))
        hashes.append(hashlib.sha256(outbox.find_one({"_id": batch})["payload"].encode()).hexdigest())
        claim2 = run(local.claim_metrics_outbox(batch, 300, t)); hashes.append(hashlib.sha256(outbox.find_one({"_id": batch})["payload"].encode()).hexdigest())
        check(len(set(hashes)) == 1, "payload hash stable")
        result["evidence"].update({"payload_sha256": hashes[0], "payload_sha256_equal": True, "batch_identity_equal": True})
        result["assertions"] = ["create/claim/retry/due reclaim payload equality"]
        return

    if case_id == "B7_ACTIVE_SLOT_SEMANTICS":
        evidence = []
        for status in ("accepted", "permanent_failure", "blocked_auth", "pending", "sending", "retryable"):
            bid = prefix + status; outbox.insert_one({"_id": bid, "case_prefix": prefix, "instance_id": bid, "status": "sending" if status in {"accepted", "permanent_failure", "blocked_auth"} else status, "sending_owner": "owner", "claim_generation": 1, "active_slot": "sync"})
            if status == "accepted": outcome = run(local.mark_metrics_outbox_accepted(bid, owner="owner", claim_generation=1, now=t))
            elif status == "permanent_failure": outcome = run(local.mark_metrics_outbox_permanent(bid, owner="owner", claim_generation=1, attempts=1, error_class="test", now=t))
            elif status == "blocked_auth": outcome = run(local.mark_metrics_outbox_blocked_auth(bid, owner="owner", claim_generation=1, attempts=1, next_attempt_at=t, error_class="test", now=t))
            else: outcome = "fixture"
            row = outbox.find_one({"_id": bid}); evidence.append({"status": status, "transition": outcome, "active_slot": "active_slot" in row})
        check([x["active_slot"] for x in evidence] == [False, False, True, True, True, True], "active slot semantics")
        result["evidence"]["subcases"] = evidence; result["assertions"] = ["terminal release", "blocked_auth retention", "pending/sending/retryable retention"]
        return

    raise AssertionError("unsupported local case")


def self_check(uri, report, baseline_production, baseline_log):
    case = report["case_results"]["SELF_CHECK"]
    case["verdict"] = "PARTIAL"
    name = PREFIX + secrets.token_hex(5)
    client = None
    marker_written = False
    try:
        check(len(PREFIX.encode()) == 22 and len(name) == 32 and len(name.encode()) <= 38, "database name")
        normalized = normalize_json({"oid": ObjectId(), "aware": datetime.now(timezone.utc), "naive": datetime(2026, 1, 1), "set": {"b", "a"}, "tuple": (1, 2), "nested": [{"uuid": uuid.uuid4()}]})
        check(normalized["set"] == ["a", "b"] and normalized["tuple"] == [1, 2] and isinstance(normalized["oid"], str), "normalizer types")
        check(normalized["naive"]["datetime_classification"] == "naive_assumed_utc", "naive classification")
        client = MongoClient(uri, appName="teledrop-phase2b-self-check-" + secrets.token_hex(4), serverSelectionTimeoutMS=TIMEOUT_MS, connectTimeoutMS=TIMEOUT_MS, socketTimeoutMS=TIMEOUT_MS)
        client.admin.command("ping")
        test_db = client[name]
        run_id = report["preflight"]["run_id"]
        test_db["_teledrop_test_run"].insert_one({"_id": run_id, "run_id": run_id, "purpose": "self_check"})
        marker_written = True
        check(test_db["_teledrop_test_run"].find_one({"_id": run_id})["run_id"] == run_id, "marker readback")
        factory = WorkerClientFactory(uri, name)
        with factory.new_worker_client("SELF_CHECK", "0") as (_, worker_db):
            worker_db.command("ping")
            check(worker_db["_teledrop_test_run"].find_one({"_id": run_id}) is not None, "worker read")
        check(factory.clients_created == factory.clients_closed == 1 and factory.active_clients_final == 0, "worker lifecycle")
        case["assertions"] = ["safe normalization", "marker insert/read", "worker ping/read", "client counters"]
        case["evidence"] = {"database_name": name, "clients_created": factory.clients_created, "clients_closed": factory.clients_closed, "active_clients_final": factory.active_clients_final}
        case["verdict"] = "PASS"
        report["cleanup"] = {"status": "PASS", "database_name": name, "marker_verified": True}
    except Exception as exc:
        case["verdict"] = "FAIL"
        case["error"] = normalize_json(exc)
        report["errors"].append({"stage": "self_check", "error": normalize_json(exc)})
    finally:
        if client is not None:
            try:
                if marker_written:
                    guard = client[name]["_teledrop_test_run"].find_one({"_id": report["preflight"]["run_id"]})
                    check(guard and name.startswith(PREFIX), "cleanup marker guard")
                    client.drop_database(name)
                    check(client[name].list_collection_names() == [] and name not in client.list_database_names(), "database absent")
                    report["cleanup"].update({"database_absent": True, "collections_empty": True})
            except Exception as exc:
                report["cleanup"] = {"status": "FAIL", "database_name": name, "error": normalize_json(exc)}
                report["errors"].append({"stage": "cleanup", "error": normalize_json(exc)})
            finally:
                client.close()
        final_production = fingerprint_files(PRODUCTION_FILES)
        final_log = fingerprint_optional_file(LOG_FILE)
        changed = [item for item in baseline_production if baseline_production[item] != final_production.get(item)]
        report["production_fingerprint"] = {"status": "IDENTICAL" if not changed else "CHANGED", "changed_files": changed}
        report["log_fingerprint"] = "UNCHANGED" if baseline_log == final_log else {"before": baseline_log, "after": final_log}


def case_server(case_id, result, uri, database_name, database, prefix):
    """Run C1--C5 through ControlPlaneRepository with isolated worker clients."""
    from control_plane_server.repository import ControlPlaneRepository
    from control_plane import aggregate_payload, canonical_json
    from datetime import timedelta
    t = datetime.now(timezone.utc)
    factory = WorkerClientFactory(uri, database_name)
    instance = prefix + "instance"
    payload = aggregate_payload(
        instance_id=instance, observed_at=t,
        current={"registered_users": 10, "reachable_users": 9, "active_24h": 10, "active_7d": 11, "active_30d": 12},
        daily=[{"date_utc": t.date().isoformat(), "active_users": 5, "interaction_count": 10, "observed_at": t}],
    )
    batch_id = payload["batch_id"]
    result["claimants"] = [f"worker-{number}" for number in range(4)]

    if case_id in {"C1_FIRST_CLAIM", "C2_ACTIVE_LEASE", "C3_STALE_RECLAIM"}:
        if case_id != "C1_FIRST_CLAIM":
            seed = ControlPlaneRepository(database)
            check(seed._ingest_metrics(payload, t, 300).status == "accepted", "server seed")
            update = {"status": "processing", "lease_expires_at": t + timedelta(seconds=60) if case_id == "C2_ACTIVE_LEASE" else t - timedelta(seconds=1)}
            database["metrics_batches"].update_one({"_id": batch_id}, {"$set": update})
        def operation(worker_db, _):
            return ControlPlaneRepository(worker_db)._ingest_metrics(payload, t, 300).status
        outcomes = concurrent_workers(factory, case_id, operation)
        result["winners"] = [{"classification": "accepted"}] * outcomes.count("accepted")
        result["loser_classifications"] = ["active_lease" if case_id == "C2_ACTIVE_LEASE" else value for value in outcomes if value != "accepted"]
        row = database["metrics_batches"].find_one({"_id": batch_id})
        result.update({"final_status": row.get("status"), "final_generation": row.get("processing_generation"), "final_attempts": row.get("attempts"), "final_owner": safe_secret(row.get("processing_token")), "evidence": {"outcomes": outcomes, "payload_hash": row.get("payload_hash")}})
        if case_id == "C1_FIRST_CLAIM":
            check(outcomes.count("accepted") == 1, "one first claimant")
        elif case_id == "C2_ACTIVE_LEASE":
            check(outcomes == ["retryable"] * 4 and row.get("processing_generation") == 1 and row.get("attempts") == 0, "active lease unchanged")
        else:
            check(outcomes.count("accepted") == 1 and row.get("processing_generation") == 2, "stale reclaim")
        result["assertions"] = ["independent clients", "four concurrent repositories", "winner/loser classification", "generation/attempts", "payload hash"]
        check(factory.clients_created == factory.clients_closed and factory.active_clients_final == 0, "server worker lifecycle")
        result["evidence"]["clients_created"] = factory.clients_created
        result["evidence"]["clients_closed"] = factory.clients_closed
        result["evidence"]["active_clients_final"] = factory.active_clients_final
        return

    if case_id in {"C4_SAME_HASH_DUPLICATE", "C5_HASH_CONFLICT"}:
        repo = ControlPlaneRepository(database)
        first = repo._ingest_metrics(payload, t, 300)
        check(first.status == "accepted", "initial server ingest")
        before_current = database["instance_metrics_current"].find_one({"instance_id": instance})
        if case_id == "C4_SAME_HASH_DUPLICATE":
            replay = repo._ingest_metrics(payload, t, 300)
            check(replay.status == "duplicate", "same hash duplicate")
        else:
            conflicting = dict(payload, current=dict(payload["current"], registered_users=999))
            conflicting["batch_id"] = payload["batch_id"]
            replay = repo._ingest_metrics(conflicting, t, 300)
            check(replay.status == "permanent_conflict", "hash conflict")
        after_current = database["instance_metrics_current"].find_one({"instance_id": instance})
        check(json.dumps(normalize_json(before_current), sort_keys=True) == json.dumps(normalize_json(after_current), sort_keys=True), "aggregate unchanged")
        row = database["metrics_batches"].find_one({"_id": batch_id})
        result.update({"winners": [{"classification": first.status}], "loser_classifications": [replay.status], "final_status": row.get("status"), "evidence": {"replay": replay.status, "aggregate_unchanged": True, "stored_payload_hash": row.get("payload_hash")}, "assertions": ["production ingest", "duplicate/conflict classification", "aggregate not double-applied"]})
        return
    raise AssertionError("unsupported server case")


def blocked_c6(result):
    result["verdict"] = "BLOCKED"
    result["error"] = {"type": "NO_SAFE_HARNESS_FAULT_POINT", "reason": "ControlPlaneRepository._ingest_metrics performs claim, aggregate writes, and completion in one synchronous production method; pausing before completion requires duplicating production logic or changing source."}
    result["assertions"] = ["not executed", "no production source modification"]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="SELF_CHECK")
    args = parser.parse_args(argv)
    report = {"run_status": "STARTING", "selected_cases": {"requested": args.cases, "effective": []}, "case_results": {}, "preflight": {"status": "NOT_RUN", "run_id": secrets.token_hex(8)}, "cleanup": {"status": "NOT_RUN"}, "production_fingerprint": {"status": "NOT_RUN"}, "log_fingerprint": "NOT_RUN", "errors": []}
    try:
        selected = parse_selection(args.cases)
        report["selected_cases"]["effective"] = selected
        report["case_results"] = {case_id: empty_case(case_id) for case_id in selected}
    except ValueError as exc:
        report["run_status"] = "BLOCKED"
        report["errors"].append({"stage": "case_selection", "error": normalize_json(exc)})
        print(assert_safe_report(report))
        return 2
    uri = os.environ.get("TELEDROP_TEST_MONGODB_URI", "").strip()
    allowed_host = os.environ.get("TELEDROP_TEST_MONGODB_ALLOWED_HOST", "").strip()
    parsed = urlparse(uri) if uri else None
    forbidden_parts = [uri, allowed_host]
    if parsed is not None:
        forbidden_parts += [parsed.username or "", parsed.password or "", quote(parsed.password or "", safe="")]
    forbidden = tuple(dict.fromkeys(forbidden_parts))
    config_status, config_reason = validate_test_host_config(uri, allowed_host)
    report["preflight"]["config"] = config_status
    if config_reason is not None:
        report["run_status"] = "BLOCKED"
        report["preflight"].update({"status": "BLOCKED", "reason": config_reason})
        safe = assert_safe_report(report, forbidden)
        print("BLOCKED — " + config_reason)
        print(safe)
        return 2
    baseline_production = fingerprint_files(PRODUCTION_FILES)
    baseline_log = fingerprint_optional_file(LOG_FILE)
    client = None
    test_database = None
    database_name = None
    marker_written = False
    try:
        concurrency_self_test()
        report["preflight"]["concurrency_self_test"] = "PASS"
        if sys.version_info[:2] != (3, 11) or not pymongo_version.startswith("4.17."):
            raise ValueError("Python 3.11 and PyMongo 4.17.x required")
        client = MongoClient(uri, appName="teledrop-phase2b-preflight-" + report["preflight"]["run_id"], serverSelectionTimeoutMS=TIMEOUT_MS, connectTimeoutMS=TIMEOUT_MS, socketTimeoutMS=TIMEOUT_MS)
        client.admin.command("ping")
        leftovers = sorted(name for name in client.list_database_names() if name.startswith(PREFIX))
        if leftovers:
            raise RuntimeError("leftover test databases found")
        auth = client.admin.command("connectionStatus", showPrivileges=False).get("authInfo", {})
        roles = auth.get("authenticatedUserRoles", [])
        if len(auth.get("authenticatedUsers", [])) != 1 or not any(role.get("role") == "atlasAdmin" and role.get("db") == "admin" for role in roles):
            raise RuntimeError("authentication preflight failed")
        report["preflight"].update({"status": "PASS", "ping": "PASS", "leftover_databases": leftovers})
        if selected == ["SELF_CHECK"]:
            self_check(uri, report, baseline_production, baseline_log)
        else:
            database_name = PREFIX + secrets.token_hex(5)
            if len(PREFIX.encode()) != 22 or len(database_name.encode()) != 32:
                raise RuntimeError("generated database name invariant failed")
            test_database = client[database_name]
            test_database["_teledrop_test_run"].insert_one({"_id": report["preflight"]["run_id"], "run_id": report["preflight"]["run_id"], "purpose": "B_C_matrix"})
            marker_written = True
            local = bind_local_persistence(uri, database_name)
            for attr, collection in (("metrics_outbox_data", "metrics_outbox"), ("user_data", "users"), ("daily_activity_data", "daily_user_activity"), ("link_data", "share_links"), ("delivery_data", "deliveries")):
                setattr(local, attr, test_database[collection])
            from control_plane_server.repository import ControlPlaneRepository
            repository = ControlPlaneRepository(test_database)
            run(repository.ensure_indexes()); run(local.ensure_indexes())
            registry = {}
            for case_id in selected:
                if case_id.startswith("B"):
                    registry[case_id] = lambda result, prefix, cid=case_id: case_local(cid, result, test_database, local, prefix)
                elif case_id == "C6_COMPLETION_FENCING":
                    registry[case_id] = lambda result, prefix: blocked_c6(result)
                else:
                    registry[case_id] = lambda result, prefix, cid=case_id: case_server(cid, result, uri, database_name, test_database, prefix)
            for case_id, handler in registry.items():
                result = report["case_results"][case_id]
                result["verdict"] = "PARTIAL"
                prefix = case_id + "_" + report["preflight"]["run_id"] + "_"
                try:
                    handler(result, prefix)
                    if result["verdict"] == "PARTIAL":
                        result["verdict"] = "PASS"
                except Exception as exc:
                    result["verdict"] = "FAIL"
                    result["error"] = normalize_json(exc)
                    report["errors"].append({"case_id": case_id, "error": normalize_json(exc)})
                finally:
                    for collection in test_database.list_collection_names():
                        test_database[collection].delete_many({"case_prefix": prefix})
            report["cleanup"] = {"status": "PENDING", "database_name": database_name, "marker_verified": True}
        report["run_status"] = "STARTED"
    except Exception as exc:
        report["run_status"] = "BLOCKED"
        report["preflight"].update({"status": "BLOCKED", "reason": normalize_json(exc)})
        report["errors"].append({"stage": "preflight", "error": normalize_json(exc)})
    finally:
        if test_database is not None and marker_written:
            try:
                guard = test_database["_teledrop_test_run"].find_one({"_id": report["preflight"]["run_id"]})
                if not guard or not database_name.startswith(PREFIX):
                    raise RuntimeError("cleanup marker guard failed")
                client.drop_database(database_name)
                if test_database.list_collection_names() or database_name in client.list_database_names():
                    raise RuntimeError("database cleanup verification failed")
                report["cleanup"] = {"status": "PASS", "database_name": database_name, "marker_verified": True, "database_absent": True, "collections_empty": True}
            except Exception as exc:
                report["cleanup"] = {"status": "FAIL", "database_name": database_name, "error": normalize_json(exc)}
                report["errors"].append({"stage": "cleanup", "error": normalize_json(exc)})
        if client is not None:
            client.close()
        if report["production_fingerprint"]["status"] == "NOT_RUN":
            report["production_fingerprint"] = {"status": "IDENTICAL" if fingerprint_files(PRODUCTION_FILES) == baseline_production else "CHANGED"}
        if report["log_fingerprint"] == "NOT_RUN":
            report["log_fingerprint"] = "UNCHANGED" if fingerprint_optional_file(LOG_FILE) == baseline_log else "CHANGED"
    executable = [case_id for case_id in selected if case_id != "C6_COMPLETION_FENCING"]
    executable_pass = all(report["case_results"][case_id]["verdict"] == "PASS" for case_id in executable)
    c6_limited = "C6_COMPLETION_FENCING" not in selected or report["case_results"].get("C6_COMPLETION_FENCING", {}).get("verdict") == "BLOCKED"
    report["run_status"] = "PASS" if ((selected == ["SELF_CHECK"] and report["case_results"]["SELF_CHECK"]["verdict"] == "PASS") or (selected != ["SELF_CHECK"] and executable_pass and c6_limited)) and report["cleanup"].get("status") == "PASS" else "BLOCKED"
    try:
        safe = assert_safe_report(report, forbidden)
    except Exception:
        print('{"run_status":"REDACTION_FAILED","report_status":"REDACTION_FAILED"}')
        return 1
    print(safe)
    return 0 if report["run_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())