# TeleDrop Control Plane

Dokumen ini adalah catatan arsitektur berkelanjutan untuk fitur multi-bot.
Control plane adalah service terpisah yang akan mengelola beberapa instance
TeleDrop. Repository bot ini berisi **agent client** yang terhubung secara
opsional; repository ini belum berisi server manager atau dashboard.

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
- Workflow dokumentasi berkelanjutan dan checklist review tersedia di
  `docs/DEVELOPMENT_WORKFLOW.md`

### Belum selesai

- Service server control plane dan database pusat
- Registrasi multi-instance di manager
- Endpoint server untuk validasi HMAC dan replay protection
- Aggregated metrics lintas bot dan deduplication user global
- Broadcast queue lintas bot dengan satu pesan per `user_id`
- Dashboard super-admin, audit log, scheduled broadcast, dan command maintenance

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