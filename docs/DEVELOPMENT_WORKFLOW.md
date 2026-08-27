# TeleDrop Development Workflow

Dokumen ini menjelaskan workflow development yang dapat dibagikan dan menjadi
pendamping `docs/CONTROL_PLANE.md`. Instruksi privat untuk coding agent berada di
`.clinerules/` dan sengaja tidak disimpan di repository.

## Pembagian dokumentasi

| Dokumen | Fungsi |
|---|---|
| `README.md` | Fitur, command, konfigurasi, deployment, dan ringkasan status |
| `docs/CONTROL_PLANE.md` | Arsitektur multi-bot, protocol, keputusan, status phase, dan roadmap |
| `docs/DEVELOPMENT_WORKFLOW.md` | Workflow development dan checklist yang dapat dibagikan |
| `.clinerules/` | Instruksi privat agent; ignored dan tidak boleh di-commit |
| Git commit/tag | Histori perubahan dan titik rollback |

## Sebelum mengerjakan task

1. Periksa branch, status working tree, commit terakhir, dan tag milestone.
2. Baca bagian relevan `docs/CONTROL_PLANE.md`.
3. Baca bagian relevan `README.md`.
4. Cari source code, pemanggil, konfigurasi, dan test terkait.
5. Tentukan scope, file yang berubah, risiko, compatibility concern, dan rollback.
6. Pilih status task: `Selesai`, `Sedang dikerjakan`, `Berikutnya`,
   `Direncanakan`, atau `Diblokir`.

Baca hanya bagian dokumen yang diperlukan. Hindari membaca atau menyalin file
rahasia dan hindari menampilkan source file penuh jika potongan fungsi cukup.

## Saat mengerjakan task

- Gunakan perubahan kecil dan terarah.
- Pertahankan compatibility konfigurasi dan data lama jika memungkinkan.
- Pisahkan perubahan code, test, dan dokumentasi secara jelas.
- Jangan menandai fitur selesai hanya karena rencana atau stub sudah dibuat.
- Untuk perubahan schema/protocol, catat format lama, format baru, migrasi,
  rollback, dan dampak operasional.
- Untuk control plane, jangan menyimpan token Telegram, database URI, API hash,
  HMAC secret, atau credential lain di repository.

## Setelah mengerjakan task

Perbarui `docs/CONTROL_PLANE.md` jika task menyentuh arsitektur, code control
plane, telemetry, schema, protocol, konfigurasi, keamanan, deployment, roadmap,
atau status milestone.

Perbarui `README.md` jika task mengubah fitur user, command, konfigurasi,
deployment, setup, atau status umum project.

Catatan minimal:

```markdown
### Task terakhir

- Status:
- Perubahan:
- Validasi:
- Risiko/sisa pekerjaan:
- Commit/tag:

### Berikutnya

- Item berikutnya:
- Dependensi:
- Risiko:
```

Dokumentasi harus menggambarkan implementasi aktual. Jika ada perbedaan antara
roadmap dan code, jelaskan perbedaannya dan ubah statusnya secara eksplisit.

## Validasi standar

Jalankan dari root project:

```text
python -m py_compile <file Python yang relevan>
python -m unittest <test lokal yang relevan> -v
git diff --check
git status --short
git check-ignore -v .env Bot.session .clinerules/
```

Test tidak boleh membutuhkan credential Telegram, koneksi MongoDB production,
atau service control plane production. Untuk test async/network, gunakan mock,
fake, atau local test server.

## Git dan rollback

- Commit hanya file yang memang dimaksudkan.
- Jangan memakai `git add .` tanpa meninjau daftar file.
- Buat annotated tag untuk milestone runtime yang sudah tervalidasi.
- Jangan memasukkan `.clinerules/`, `.env`, file session, atau secret ke commit/tag.
- Sebelum migrasi schema, siapkan backup database dan strategi rollback.

Milestone aktif project saat ini:

```text
v2.0.0-before-control-plane  baseline
v2.1.0-telemetry             local telemetry
v2.2.0-agent-foundation      instance identity and agent protocol
```

## Checklist review

- [ ] Scope task sesuai phase aktif.
- [ ] Source code dan pemanggil sudah diperiksa.
- [ ] Test baru/yang relevan tersedia.
- [ ] Compile dan test berhasil.
- [ ] `git diff --check` berhasil.
- [ ] README dan `CONTROL_PLANE.md` konsisten dengan code.
- [ ] Secret, `.env`, session, dan `.clinerules/` tidak staged.
- [ ] Risiko, rollback, dan item berikutnya sudah dicatat.