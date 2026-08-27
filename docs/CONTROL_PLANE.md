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

### Berikutnya

- Deployment production server control plane dan database pusat
- Provisioning secret melalui secret manager production
- Daftar instance dan operasi admin pada interface terautentikasi
- Aggregated metrics lintas bot dan deduplication user global
- Broadcast queue lintas bot dengan satu pesan per `user_id`
- Dashboard super-admin, audit log, scheduled broadcast, dan command maintenance

## Task terakhir

- **Status:** Selesai untuk implementasi Phase 2A server dan test lokal
- **Perubahan:** Server `aiohttp`, registration/heartbeat, HMAC validation,
  timestamp/nonce replay protection, credential provisioning, repository MongoDB,
  health check, dan konfigurasi server terpisah
- **Validasi:** Python compile, test HTTP server, test konfigurasi/credential,
  test protocol agent, test telemetry, test security, dan validasi `app.json`
- **Risiko/sisa pekerjaan:** Deployment production, secret manager, backup,
  observability, dan admin interface belum dikerjakan
- **Commit/tag:** `v2.2.0-agent-server`

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
2. **Phase 2B** — telemetry ingestion dan agregasi metrics per bot.
3. **Phase 3** — broadcast job queue dengan unique key `(broadcast_id, user_id)`.
4. **Phase 4** — sender selection/fallback agar user yang terdaftar pada banyak bot
   menerima satu pesan saja.
5. **Phase 5** — Telegram super-admin interface dan web dashboard.

Setiap phase harus memiliki test lokal, perubahan backward-compatible, commit
terpisah, dan annotated tag milestone. Jangan menaruh token Telegram, database URI,
HMAC secret, atau isi `.clinerules/` ke repository.