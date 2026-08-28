# TeleDrop Control Plane

Dokumen ini adalah catatan arsitektur berkelanjutan untuk fitur multi-bot.
Control plane adalah service terpisah yang akan mengelola beberapa instance
TeleDrop. Repository ini sekarang berisi agent client dan fondasi server di
package `control_plane_server/`; dashboard dan interface super-admin belum dibuat.

## Status implementasi

### Selesai

- Baseline source code: `v2.0.0-before-control-plane`
- Local telemetry: `v2.1.0-telemetry`
- `last_seen_at` dan `last_interaction_type` pada user
- Statistik registered, reachable, active 24 jam, 7 hari, dan 30 hari
- Soft-state `blocked_at`/`deleted_at` agar histori user tidak hilang
- Identitas instance melalui `CONTROL_PLANE_INSTANCE_ID`
- Metadata versi, Telegram bot ID, username, start time, dan uptime
- Optional registration dan heartbeat agent melalui HTTPS
- HMAC-SHA256 dengan timestamp, nonce, method, path, dan canonical JSON body
- Health endpoint aman tanpa token atau secret
- Test protocol yang tidak memakai Telegram, database production, atau network
- Server Phase 2A dengan route healthz, registration, dan heartbeat (`v2.2.0-agent-server`)
- Validasi HMAC server-side, timestamp, canonical body, dan replay nonce TTL
- Persistence instance dan hash secret melalui repository MongoDB async wrapper
- Test HTTP lokal dan configuration/credential tests tanpa database production
- Workflow dokumentasi berkelanjutan dan checklist review tersedia di
  `docs/DEVELOPMENT_WORKFLOW.md`
- Phase 2B aggregate metrics (`v2.2.1-metrics-aggregate`): local aggregate
  snapshot, persistent exact-payload outbox, endpoint
  `POST /api/v1/metrics/aggregates`, atomic/idempotent server ingestion,
  lease/owner stale-processing recovery, dan global observations (bukan unique users)
- Privacy: raw Telegram user ID tidak dikirim atau disimpan di control plane;
  metrics hanya aggregate per instance
- Aggregate `current`/`daily` sengaja memakai at-least-once idempotency tanpa
  exclusive ownership guard lintas collection. Batch ID mengikat canonical
  payload; payload hash conflict ditolak; current memakai absolute values dan
  daily memakai absolute `$max` dengan freshness guard. Completion batch tetap
  di-fence oleh processing token/generation. Duplicate same-batch boleh mengubah
  `updated_at`, tetapi bukan nilai metric authoritative; token/generation tidak
  pernah masuk aggregate document atau wire payload.
- Metrics readiness dipisahkan dari heartbeat task. Registration/re-registration
  memberi wake signal sticky ke metrics scheduler, sehingga recovery tidak harus
  menunggu metrics interval penuh; successful heartbeat biasa tidak memicu tight
  loop. Bounded body reader menolak oversized payload sebelum parser/auth.
- Reader path: storage + repository `metrics_summary()` tersedia; belum ada HTTP/read
  consumer. Dashboard/super-admin menjadi fase terpisah. Lihat "Status" di bawah.[^read]

[^read]: Phase 2B saat berstatus **ingestion foundation implemented and tested**;
  external operator read API masih pending.

### Berikutnya

- Deployment production server control plane dan database pusat
- Provisioning secret melalui secret manager production
- Daftar instance dan operasi admin pada interface terautentikasi
- Broadcast queue lintas bot (Phase 3) dengan recipient selection tetap lokal
- Dashboard super-admin, audit log, scheduled broadcast, dan command maintenance

## Task terakhir

- **Status:** Ingestion foundation Phase 2B implemented and tested; targeted
  Conditional-Pass gap fixes applied and independently re-auditable; external
  read API pending; belum release-ready untuk production tanpa real-Mongo gate,
  legacy-index duplicate check, dan rollout terkontrol.
- **Perubahan:** Local aggregate snapshot, persistent exact-payload outbox,
  endpoint `POST /api/v1/metrics/aggregates` (metrics schema v1, privacy mode
  `aggregate_only`), strict payload validation, idempotent aggregate ingestion
  (`instance_metrics_current`, `instance_metrics_daily`, `metrics_batches`),
  atomic conditional claim/reclaim (status + lease), atomic current/daily
  freshness (`$max` monotonic + guard `observed_at`/`batch_id`), single-active
  outbox invariant (partial unique `(instance_id, active_slot)`), bounded jitter,
  `blocked_auth` circuit untuk 401/403, dan terminal cleanup (completed/permanent_failure)
- **Validasi:** Python compile, test protocol/telemetry, contract/validation,
  server HTTP, repository ingestion/recovery/concurrency, local aggregation,
  exact-body HTTP, dan heartbeat isolation. Root 40 + server — lihat angka
  aktual pada hasil `unittest`.
- **Risiko/sisa pekerjaan:** External HTTP read API (summary) belum dibuat;
  dashboard/super-admin, deployment production, dan secret manager menjadi
  fase terpisah; correction daily yang menurunkan nilai serta identity dedup
  global tetap keputusan fase lanjutan.
- **Commit/tag:** belum di-commit (menunggu instruksi)

## Kontrak metrics agregat (Phase 2B)

### Endpoint dan versioning

```text
POST /api/v1/metrics/aggregates
```

- `protocol_version: "1"` (transport/auth, tidak berubah dari Phase 2A).
- `metrics_schema_version: "1"` versi schema metrics yang terpisah.
- `privacy_mode: "aggregate_only"` wajib.

Registration (`/api/v1/agents/register`) dan heartbeat (`/api/v1/agents/heartbeat`)
Phase 2A tidak diubah.

### Payload

```json
{
  "instance_id": "bot-01",
  "protocol_version": "1",
  "metrics_schema_version": "1",
  "privacy_mode": "aggregate_only",
  "batch_id": "<sha256>",
  "observed_at": "2026-08-27T12:00:00Z",
  "current": {
    "registered_users": 1000,
    "reachable_users": 900,
    "active_24h": 120,
    "active_7d": 300,
    "active_30d": 550
  },
  "daily": [
    {
      "date_utc": "2026-08-27",
      "active_users": 100,
      "interaction_count": 700,
      "observed_at": "2026-08-27T12:00:00Z"
    }
  ]
}
```

Field terlarang (reject 400): `users`, `user_id`, `username`, `first_name`,
`last_name`, `phone`, semua field cursor, `*_has_more`, dan field bebas lainnya
(allowlist ketat).

### Definis metrics

- `registered_users` jumlah record user lokal.
- `reachable_users` = `blocked_at == null` dan `deleted_at == null`.
- `active_24h/7d/30d` user reachable dengan `last_seen_at` dalam rolling window UTC.
- Daily `active_users` = distinct user aktif pada tanggal UTC; `interaction_count` =
  jumlah event. Keduanya absolute (bukan increment).
- Tidak ada istilah "online users"; bot tidak mengetahui status online Telegram.

### Global semantics

Global metrics dihitung sebagai **observations across instances**
(sum per-instance), bukan unique global users. Nama dibedakan dengan akhiran
`_observations`, dan user yang memakai lebih dari satu bot boleh dihitung lebih
dari sekali.

### Canonical batch id

`batch_id` = SHA-256 atas material canonical (termasuk protocol/schema version,
privacy mode, `instance_id`, `observed_at`, `current`, dan seluruh daily)
**tanpa field `batch_id`** (menghindari self-reference). Server menghitung ulang
dari payload dan membandingkan constant-time.

### Idempotency dan recovery

- Server menggunakan claim atomik: batch baru via insert (unique `_id`),
  duplicate dengan payload hash sama dianggap success `duplicate`,
  payload hash berbeda = `permanent_conflict` (409).
- Stale reclaim adalah conditional atomic update dengan filter
  (`status == processing` DAN `lease_expires_at <= now`), sehingga hanya satu
  worker yang berhasil mereclaim; worker kedua mendapat `retryable`. Ini
  menghindari pola read-then-update-by-id yang rentan race.
- Batch `processing` dengan lease aktif memberi `retryable` (503); lease
  kedaluwarsa dapat direclaim sekali.
- Freshness current/daily ditangani di dalam operasi atomic: predicate guard
  `observed_at`/`batch_id` ada pada filter `update_one`/upsert. Stale snapshot
  tidak menimpa yang lebih baru; retry batch yang sama idempotent.
- Current boleh naik atau turun pada snapshot yang lebih baru. Daily memakai
  `$max` (monotonic absolute, retry tidak menggandakan, tanpa downward correction).
- `observed_at` sama dengan `batch_id` berbeda untuk instance yang sama ditolak
  deterministik (`permanent_conflict`), bukan arbitrary overwrite.
- Equal timestamp / crash recovery aman: batch yang belum `completed` setelah
  sebagian write dapat di-reclaim dan ditulis ulang secara idempotent.
- `completed` hanya ditulis setelah seluruh current/daily writes selesai.
- Aggregate current/daily writes bersifat at-least-once dan tidak memerlukan
  exclusive aggregate writer: batch identity mengikat exact canonical payload,
  current memakai absolute values, daily memakai absolute `$max`, dan freshness
  guard mencegah batch lama meregresi state yang lebih baru. `updated_at` boleh
  berubah pada duplicate write tanpa mengubah metric values.
- Claim server memakai `processing_token` unik dan `processing_generation`
  monotonic. Semua mutation state setelah claim, termasuk completion, wajib
  conditional terhadap batch ID, instance, status processing, token, dan generation.
- Response berisi `status` (`accepted`/`duplicate`/`retryable`/`permanent_conflict`)
  dan tidak membocorkan secret atau payload.

> **Verification limitation:** release-candidate verification combines real-Mongo
> stale-reclaim/duplicate evidence with existing offline old-owner and
> idempotency tests. It does not run a real process-kill/fault-injection test;
> delivery remains at-least-once with idempotent processing, not an exactly-once
> guarantee.

### Outbox agent lokal

- Payload exact disimpan di outbox sebelum dikirim; retry memakai payload yang sama.
- Satu batch aktif per instance (pending/sending/retryable/blocked_auth) yang
  ditegakkan oleh partial unique index `(instance_id, active_slot)`; create
  concurrent kedua ditolak deterministik, bukan exception tidak tertangani.
- `blocked_auth` (permanent 401/403) tetap menempati active slot sehingga tidak ada
  snapshot baru pada setiap interval; payload diretry dengan bounded backoff dan
  menuju `accepted` setelah config/credential diperbaiki.
- Bounded exponential backoff (base `* 2^(attempt-1)`, capped) ditambah bounded
  equal jitter (<= 25% base); hasil tidak melebihi `max`. Retry habis pada
  payload/config error menjadi `permanent_failure`.
- Accepted dan normal permanent failure melepas active slot; keduanya dihapus oleh
  retention bounded. pending/sending/retryable/blocked_auth tidak dihapus.
- Claim local dilakukan dalam satu conditional atomic operation: `sending` hanya
  dapat diklaim bila lease kedaluwarsa; reclaim langsung menghasilkan owner token
  dan generation baru. Lease valid, retry belum due, dan terminal state tidak
  claimable.

### Collections server

- `metrics_batches` (batch claim/state)
- `instance_metrics_current` (satu record per instance)
- `instance_metrics_daily` (unique `(instance_id, date_utc)`)

Collection partial lama (`bot_users`, central `daily_user_activity`) yang pernah
di-ubah oleh code Phase 2B yang belum dirilis tidak lagi ditulis oleh code baru;
tidak di-drop dan tidak dijalankan migrasi.

## Aturan dokumentasi berkelanjutan

`docs/CONTROL_PLANE.md` adalah sumber utama konteks arsitektur dan status phase.
Setiap task yang mengubah arsitektur, code control plane, telemetry, schema,
protocol, konfigurasi, keamanan, deployment, roadmap, atau status milestone wajib
memperbarui dokumen ini setelah implementasi dan validasi selesai.

`README.md` diperbarui jika task berdampak pada fitur user, command, konfigurasi,
deployment, setup, atau status umum project. Workflow publik dan checklist tersedia
di [`DEVELOPMENT_WORKFLOW.md`](DEVELOPMENT_WORKFLOW.md). Instruksi privat agent
berada di `.clinerules/` dan tidak boleh di-commit atau dibagikan.

Status milestone hanya boleh diubah menjadi `Selesai` jika code, test, validasi,
dan dokumentasinya sudah selesai. Setiap perubahan status harus menyebutkan risiko,
pekerjaan tersisa, serta commit/tag bila ada.

## Konfigurasi agent

```env
APP_VERSION=2.2.0-dev
CONTROL_PLANE_URL=https://manager.example.com
CONTROL_PLANE_INSTANCE_ID=bot-01
CONTROL_PLANE_SECRET=unique-secret-per-instance
CONTROL_PLANE_HEARTBEAT_INTERVAL=60
CONTROL_PLANE_TIMEOUT=10
```

Jika `CONTROL_PLANE_URL` kosong, agent nonaktif dan bot berjalan seperti deployment
sebelumnya. URL production wajib HTTPS. HTTP hanya diperbolehkan untuk `localhost`,
`127.0.0.1`, atau `::1` saat development.

## Server control plane Phase 2A

Server berada di `control_plane_server/` agar lifecycle dan konfigurasi server
terpisah dari bot agent. Jalankan dari root project setelah menyiapkan environment
server (contoh tersedia di `control_plane_server/.env.example`):

```text
python -m control_plane_server.main
```

Variabel server utama:

```env
CONTROL_PLANE_DATABASE_URL=mongodb+srv://USER:PASSWORD@HOST/teledrop_control
CONTROL_PLANE_DATABASE_NAME=teledrop_control
CONTROL_PLANE_HOST=127.0.0.1
CONTROL_PLANE_PORT=8090
CONTROL_PLANE_MAX_CLOCK_SKEW=300
CONTROL_PLANE_NONCE_TTL=600
CONTROL_PLANE_AGENT_SECRETS_JSON={"bot-01":"REPLACE_WITH_16_PLUS_CHAR_SECRET"}
```

Jangan menaruh nilai nyata pada file contoh atau repository. Untuk production,
gunakan secret manager dan batasi endpoint server pada network yang dipercaya.
Server Phase 2A belum memiliki endpoint daftar instance publik, admin API,
dashboard, broadcast, atau command maintenance.

## Kontrak request agent

### Registration

```text
POST /api/v1/agents/register
```

Body:

```json
{
  "instance_id": "bot-01",
  "telegram_bot_id": 123456789,
  "username": "example_bot",
  "version": "2.2.0",
  "started_at": "2026-08-27T12:00:00Z",
  "protocol_version": "1"
}
```

### Heartbeat

```text
POST /api/v1/agents/heartbeat
```

Body menambahkan:

```json
{
  "uptime_seconds": 3600,
  "status": "online"
}
```

Status yang valid: `online`, `stopping`, dan `offline`.

### HMAC headers

```text
X-TeleDrop-Protocol: 1
X-TeleDrop-Timestamp: <unix-seconds>
X-TeleDrop-Nonce: <random-value>
X-TeleDrop-Signature: <hex-hmac-sha256>
```

Input yang ditandatangani:

```text
timestamp + "\n" + nonce + "\n" + METHOD + "\n" + path + "\n" + canonical_json(body)
```

Server wajib memvalidasi timestamp dalam toleransi waktu yang pendek, menolak
nonce yang pernah digunakan, mencocokkan instance ID dengan secret yang terdaftar,
dan membandingkan signature menggunakan constant-time comparison.

Secret tidak pernah dikirim dalam body, health response, atau log aplikasi.

## Kebijakan kegagalan

- Kegagalan control plane tidak menghentikan bot utama.
- Agent mencatat error ringkas ke logger dan mencoba lagi pada heartbeat berikutnya.
- HTTP response body tidak dicatat untuk mencegah kebocoran data dari server.
- HTTP session selalu ditutup saat bot berhenti.
- Registration diulang setelah kegagalan; heartbeat dikirim setelah registration sukses.

## Rencana berkesinambungan

1. **Phase 2A** — server registration, heartbeat validation, dan daftar instance.
2. **Phase 2B** — metrics ingestion agregat (aggregate-only, tanpa user ID central) dan
   aggregasi per instance serta global observations.
3. **Phase 3** — broadcast job queue dengan unique key `(broadcast_id, user_id)`.
4. **Phase 4** — sender selection/fallback agar user yang terdaftar pada banyak bot
   menerima satu pesan saja.
5. **Phase 5** — Telegram super-admin interface dan web dashboard.

Setiap phase harus memiliki test lokal, perubahan backward-compatible, commit
terpisah, dan annotated tag milestone. Jangan menaruh token Telegram, database URI,
HMAC secret, atau isi `.clinerules/` ke repository.