# TeleDrop Phase 2B — harness infrastructure

The release-candidate runner executes only the selected B/C cases. It does not
execute A1–A5 or D1–E3, and `ALL` is not a release-candidate command.

## Commands

Run the safe infrastructure check:

```text
.venv\Scripts\python.exe tests\integration_real_mongo\run_phase2b_real_mongo.py --cases SELF_CHECK
```

Run the B/C release-candidate subsets:

```text
.venv\Scripts\python.exe tests\integration_real_mongo\run_phase2b_real_mongo.py --cases B1,B2,B3
.venv\Scripts\python.exe tests\integration_real_mongo\run_phase2b_real_mongo.py --cases B4,B5,B6,B7,C1,C2,C3,C4,C5
```

The local B cases use the production module's shared collection bindings and
report `LOCAL_SHARED_CLIENT_RUNTIME_PARITY`; server C concurrency uses one
independent bounded MongoClient and repository per claimant. Each subset has
its own generated database, marker guard, cleanup, fingerprint, and redaction
report.

Selectors are deterministic and comma-separated:

```text
--cases SELF_CHECK
--cases A1_INDEX_READBACK,A2_DATABASE_ISOLATION
--cases B1_PENDING_CONCURRENT_CLAIM,B2_ACTIVE_LEASE_NOT_CLAIMABLE
--cases C1_FIRST_CLAIM,C2_ACTIVE_LEASE
--cases D1_SAME_BATCH_IDEMPOTENCY,D2_NEWER_CURRENT_LOWER_VALUES
--cases E1_SERVER_RETENTION,E2_LOCAL_RETENTION,E3_CLEANUP_IDEMPOTENCY
--cases ALL
```

The default is `SELF_CHECK`. `ALL` must be explicit and is intentionally not
executed by this release-candidate runner; deferred matrix cases are reported
`NOT_RUN`.
Unknown IDs are a preflight error and duplicate IDs are deduplicated in order.

## Required environment

The runner is gated on two mandatory, exact-match test-only variables. There is
no fallback hostname and no broad allowlist; a dedicated third-party cluster is
required and the URI hostname must match the allowed value character-for-character
(after safe, case-insensitive DNS hostname normalization):

- `TELEDROP_TEST_MONGODB_URI` — the dedicated `mongodb+srv://` test URI.
- `TELEDROP_TEST_MONGODB_ALLOWED_HOST` — the exact cluster hostname only. It must
  contain no scheme, credentials, port, path, or query string.

Example placeholders (replace the angle-bracket values with your dedicated
test-only cluster values):

```powershell
$env:TELEDROP_TEST_MONGODB_URI = "<dedicated-test-mongodb-srv-uri>"
$env:TELEDROP_TEST_MONGODB_ALLOWED_HOST = "<dedicated-test-cluster-hostname>"
```

```bash
export TELEDROP_TEST_MONGODB_URI='<dedicated-test-mongodb-srv-uri>'
export TELEDROP_TEST_MONGODB_ALLOWED_HOST='<dedicated-test-cluster-hostname>'
```

Guidelines:

- The cluster must be a dedicated testing cluster, never a shared or production
  database. The harness may drop the generated database it creates.
- Both variables are mandatory; when either is missing the runner stops before
  any connection is attempted.
- The hostname must be an exact match with the URI hostname.
- The default selector remains `SELF_CHECK`; `ALL` stays explicit and is not a
  release-candidate command.

## Safety

The runner requires both `TELEDROP_TEST_MONGODB_URI` and
`TELEDROP_TEST_MONGODB_ALLOWED_HOST` but never reads `.env` or prints the URI,
credentials, username, password, encoded password, query string, or either
hostname value. It validates the exact allowed host, uses bounded timeouts, does
read-only leftover database auditing, and only creates a generated
`teledrop_phase2b_test_<10 hex>` database. It never accesses `admin`, `local`,
`config`, or `oplog.rs`, and deletes only its own exact marker-guarded database.
Redaction covers the URI and allowed host so neither can leak into the printed
report.

Concurrency workers must use `WorkerClientFactory.new_worker_client`; every
worker receives its own bounded `MongoClient`, uses the generated database, and
closes it in `finally`. The factory exposes created, closed, and final-active
client counters.

Reports are recursively normalized to JSON-native values. ObjectId, UUID,
datetime, sets, tuples, bytes, enums, and exceptions are represented safely;
unsupported values become an explicit `unsupported_type` object. The report is
serialized, parsed again, and scanned for URI schemes and credentials before it
is printed. A redaction failure emits only `REDACTION_FAILED`.

Production files and `filesharingbot.log` are fingerprinted in-process using
`pathlib.Path`, streaming `hashlib.sha256`, size, and mtime metadata. Final
results report production `IDENTICAL`/`CHANGED` and log `UNCHANGED` or metadata
differences. No file contents are printed.

Existing offline evidence maps C6 to
`LocalOutboxTests.test_all_old_owner_transitions_are_rejected` plus
`LocalOutboxTests.test_current_owner_retry_and_blocked_transitions_succeed`,
and C3 real-Mongo stale reclaim. D6 maps to
`RepositoryIngestionTests.test_stale_processing_is_reclaimed`,
`test_exact_duplicate_is_duplicate_without_double_count`, and
`test_daily_is_monotonic_and_does_not_double_on_retry`, with real-Mongo C3/C4
conditional operations. There is no real process-kill integration test; this
does not claim exactly-once delivery. Delivery remains at-least-once with
idempotent processing. Metrics sync remains disabled by default unless
`CONTROL_PLANE_METRICS_ENABLED` is explicitly enabled. Python 3.10 remains a
deployment compatibility gate.