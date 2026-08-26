

<h2 align="center">
    ──「 ᴛᴇʟᴇᴅʀᴏᴘ 」──
</h2>
<table>
<p align="center">
  Telegram file-sharing bot with expiring deep links and persistent delivery cleanup.
    
</table>
<details>
###<summary><b><i>🦄 More Information</i></b></summary>


Source repository: [ndofox/teledrop-bot](https://github.com/ndofox/teledrop-bot)



### Features
- Fully customisable.
- Customisable welcome & Forcesub messages.
- More than one Posts in One Link.
- Can be deployed on a VPS or a compatible process-based platform.
- Links expire automatically and can be revoked with `/revoke`.
- Delivered files can be deleted persistently after `TIME` seconds.

### Setup

- Add the bot to Database Channel with all permission
- Add bot to ForceSub channel as Admin with Invite Users via Link Permission if you enabled ForceSub 

##
### Installation

#### Deploy in your VPS
````bash
git clone https://github.com/ndofox/teledrop-bot
cd teledrop-bot
pip3 install -r requirements.txt
# Set the required environment variables; do not commit secrets.
python3 main.py
````

#### Deploy with Docker Compose

After creating or changing `.env`, build and recreate the container so Docker
loads the latest environment values:

```bash
cd /www/docker/teledrop-bot
docker-compose up -d --build --force-recreate
```

With the modern Docker Compose plugin, the equivalent command is:

```bash
docker compose up -d --build --force-recreate
```

The Compose file should provide `.env` through `env_file`; `.dockerignore` prevents
that file, Telegram sessions, local environments, logs, and Git metadata from being
copied into the Docker image by `COPY . .`. After rebuilding, you can verify an image
does not contain those files without exposing any environment values:

```bash
docker run --rm --entrypoint sh teledrop-bot-teledrop-bot -c \
  'test ! -e /app/.env && test ! -e /app/Bot.session && test ! -d /app/.git && echo "image hygiene: ok"'
```

Replace `teledrop-bot-teledrop-bot` with the image name shown by
`docker images` or `docker compose images`.

#### Run locally on Windows

Copy `.env.example` to `.env`, then replace every `REPLACE_*` value with your
own credential or Telegram ID. The `.env` file is ignored by Git and must not
be committed or shared.

In Git Bash:

```bash
source .venv/Scripts/activate
python -m pip install -r requirements.txt
cp .env.example .env
python main.py
```

In PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

If the MongoDB password contains characters such as `@`, `:`, `/`, or `#`,
URL-encode the password before putting it in `DATABASE_URL`.
</details>

## Penggunaan

Bot menerima command di **private chat**. `OWNER_ID` otomatis menjadi admin;
ID pada `ADMINS` adalah ID user Telegram, bukan username, ID bot, atau ID channel.

### Command umum

| Command | Fungsi | Cara pakai |
|---|---|---|
| `/start` | Menampilkan welcome atau mengambil file dari share link | Kirim `/start`, atau buka link `?start=TOKEN` |
| `/help` | Menampilkan panduan command | `/help` |

### Command admin

| Command | Fungsi | Cara pakai |
|---|---|---|
| `/genlink` | Membuat link untuk satu post di database channel | Kirim `/genlink`, lalu forward satu post dari database channel atau kirim URL post tersebut |
| `/batch` | Membuat satu link untuk beberapa post berurutan | Kirim `/batch`, lalu forward post pertama dan terakhir |
| `/revoke` | Mencabut link yang masih aktif | `/revoke TOKEN` atau `/revoke https://t.me/bot?start=TOKEN` |
| `/users` | Melihat jumlah user yang tersimpan | `/users` |
| `/stats` | Melihat uptime bot | `/stats` |
| `/ping` | Memastikan bot aktif | `/ping` |
| `/info` | Melihat konfigurasi runtime penting | `/info` |
| `/broadcast` | Mengirim **salinan** pesan ke semua user | `/broadcast teks`, atau reply pesan lalu kirim `/broadcast` |
| `/forward` | Meneruskan pesan asli ke semua user | Reply pesan lalu kirim `/forward` |
| `/restart` | Menampilkan instruksi restart | `/restart` |

#### Catatan `/broadcast` dan `/forward`

- Keduanya hanya dapat dipakai admin dan hanya di private chat.
- `/broadcast teks` mengirim teks baru tanpa perlu reply.
- Untuk media, caption, atau pesan yang sudah ada, reply pesan tersebut dengan `/broadcast`.
- `/forward` wajib memakai reply karena command ini meneruskan pesan asli.
- User yang memblokir bot atau akunnya sudah tidak aktif akan dibersihkan dari database.
- Pengiriman dilakukan berurutan dengan jeda untuk mengurangi risiko flood limit.

#### Restart

`/restart` **tidak mematikan atau menjalankan ulang proses Python secara langsung**.
Command ini hanya memberi notifikasi karena restart sebaiknya dilakukan oleh process
supervisor (misalnya systemd, Docker, atau platform deployment). Saat menjalankan lokal,
hentikan proses dengan `Ctrl+C`, lalu jalankan kembali:

```powershell
python main.py
```

#### Jika command malah membuat link

Pesan admin yang bukan command diperlakukan sebagai post baru dan disalin ke database
channel. Karena itu:

- `/ping`, `/info`, dan `/help` sekarang memiliki handler khusus.
- Typo command seperti `/pign` tidak lagi dibuat menjadi link; bot membalas dengan petunjuk `/help`.
- Pastikan command dikirim di private chat dan proses bot benar-benar sudah direstart.
- Jika `/users` dan `/stats` membalas tetapi command lain tidak, ikuti cara pakai command
  pada tabel di atas—khususnya pola reply untuk `/broadcast` dan `/forward`.

### Contoh alur membuat dan mencabut link

1. Pastikan bot menjadi admin di database channel dengan izin yang diperlukan.
2. Kirim file/post kepada bot di private chat; bot akan menyalinnya ke database channel
   dan membalas dengan link.
3. Atau gunakan `/genlink` untuk post yang sudah ada di database channel.
4. Cabut link dengan `/revoke <token atau URL>`.

### Konfigurasi admin

```env
OWNER_ID=YOUR_TELEGRAM_USER_ID
ADMINS=YOUR_TELEGRAM_USER_ID OTHER_ADMIN_USER_ID
```

`ADMINS` adalah daftar ID numerik yang dipisahkan spasi. Setelah `.env` diubah,
restart proses bot. Untuk memeriksa akun yang sedang mengirim pesan, gunakan bot
informasi Telegram seperti `@userinfobot`; jangan menyalin ID bot atau ID channel.

> **Keamanan:** jangan commit `.env`, bot token, API hash, atau MongoDB URI.
> Jika kredensial pernah masuk ke repository/log/chat, cabut atau rotasi kredensial tersebut.
<details>
<summary><b><blockquote>Explore Variables Set-up</blockquote></b></summary> 
    
### Variables

* `API_HASH` Your API Hash from my.telegram.org
* `APP_ID` Your API ID from my.telegram.org
* `TG_BOT_TOKEN` Your bot token from @BotFather
* `OWNER_ID` Must enter Your Telegram Id
* `CHANNEL_ID` Your Channel ID eg:- -100xxxxxxxx
* `DATABASE_URL` Your mongo db url
* `DATABASE_NAME` Your mongo db session name
* `ADMINS` Optional: A space separated list of Telegram user IDs for additional admins. Admins can use the admin commands documented above.
* `START_MESSAGE` Optional: pesan sambutan bot; mendukung HTML dan placeholder user
* `FORCE_SUB_MESSAGE` Optional: pesan force-sub; mendukung HTML dan placeholder user
* `FORCE_SUB_CHANNEL1` through `FORCE_SUB_CHANNEL4` Optional force-sub channel IDs, leave 0 to disable
* `PICS` Optional: gambar untuk pesan start dan force-sub; saat ini hanya gambar pertama yang digunakan
* `TIME` AUTO DELETE delay in seconds (`0` disables it)
* `LINK_TTL` Share-link validity in seconds (minimum 60)
* `PROTECT_CONTENT` Optional: True if you need to prevent files from forwarding

### EXTRA VARIABLES
* `PORT` Port HTTP health endpoint; default `8080`
* `CLEANUP_INTERVAL` Interval pengecekan delivery yang kedaluwarsa dalam detik; default `60`
* `MAX_BATCH_MESSAGES` Batas jumlah pesan dalam satu batch; default `100`
* `TG_BOT_WORKERS` Jumlah worker Pyrogram; default `4`
* `CUSTOM_CAPTION` Caption custom untuk dokumen; gunakan `{filename}` dan `{previouscaption}`
* `DISABLE_CHANNEL_BUTTON` Set `True` untuk menonaktifkan tombol share pada post database channel
* `ALLOW_LEGACY_LINKS` Set `True` hanya jika masih perlu menerima link format lama; default `False`
* `MAIN_CHANNEL_URL` URL channel utama untuk tombol di menu About
* `SOURCE_CODE_URL` URL repository source code untuk tombol di menu About
* `BOT_STATS_TEXT` Format pesan `/stats`; gunakan `{uptime}`
* `USER_REPLY_TEXT` Balasan untuk pesan biasa user; kosongkan untuk menonaktifkan

### Contoh optional tuning

```env
PORT=8080
CLEANUP_INTERVAL=60
MAX_BATCH_MESSAGES=100
TG_BOT_WORKERS=4
ADMINS=123456789 987654321
PICS=TELEGRAM_IMAGE_FILE_ID
START_MESSAGE="<b>👋 Halo {mention}!</b>\n\nGunakan link yang diberikan admin untuk mengambil file."
CUSTOM_CAPTION="<b>📁 {filename}</b>\n\n{previouscaption}"
DISABLE_CHANNEL_BUTTON=False
ALLOW_LEGACY_LINKS=False
MAIN_CHANNEL_URL=https://t.me/channel_saya
SOURCE_CODE_URL=https://github.com/ndofox/teledrop-bot
BOT_STATS_TEXT="<b>📊 Status TeleDrop</b>\n\n✅ Online\n⏱ Uptime: <code>{uptime}</code>"
USER_REPLY_TEXT="Silakan gunakan link file yang diberikan admin atau ketik /start."
```

Semua perubahan `.env` memerlukan restart bot. `ADMINS` harus berisi Telegram user
ID numerik yang dipisahkan spasi, bukan username. Gunakan Telegram `file_id` untuk
`PICS` agar pengiriman gambar tidak bergantung pada URL eksternal.


### Fillings
#### START_MESSAGE | FORCE_SUB_MESSAGE

* `{first}` - User first name
* `{last}` - User last name
* `{id}` - User ID
* `{mention}` - Mention the user
* `{username}` - Username

#### CUSTOM_CAPTION

* `{filename}` - file name of the Document
* `{previouscaption}` - Original Caption

#### CUSTOM_STATS

* `{uptime}` - Bot Uptime

</details>


# All Thanks To Our Contributors

<a href="https://github.com/ndofox/teledrop-bot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=ndofox/teledrop-bot" />
</a>

### Licence
[![GNU GPLv3 Image](https://www.gnu.org/graphics/gplv3-127x51.png)](http://www.gnu.org/licenses/gpl-3.0.en.html)  


##

   **Star this Repo if you Liked it ⭐⭐⭐**
