# 🤖 TeraBot — TeraBox Downloader Telegram Bot

A high-speed, personal TeraBox → Telegram downloader bot built with **Telethon** and **Python 3.10+**.

- Native direct-link resolver (no dead third-party workers)
- Parallel multi-stream Range download engine
- Async queue, per-user rate limiting
- SQLite cache (optional, requires a private channel)
- Windows-native — runs with a simple `python bot.py`

---

## 📋 Requirements

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.10+ | Download from [python.org](https://python.org) |
| ffmpeg / ffprobe | any | Optional but **strongly recommended** for video thumbnails and duration metadata. Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to your system `PATH`. |

---

## 🚀 Windows Setup (step-by-step)

### 1 — Clone / download the project

```
cd C:\Users\YourName\Desktop
git clone <repo-url> terabot
cd terabot
```

### 2 — Create and activate a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1      # PowerShell
# or
.venv\Scripts\activate.bat       # CMD
```

### 3 — Install dependencies

```powershell
pip install -r requirements.txt
```

### 4 — Configure your `.env` file

Copy the example and fill in your real values:

```powershell
Copy-Item .env.example .env
notepad .env
```

See the **Configuration** section below for details on each variable.

### 5 — Run the bot

```powershell
python bot.py
```

The bot will print its username and start listening. Press **Ctrl+C** to stop gracefully.

---

## 🍪 How to Get Your TeraBox Cookie (`ndus`)

The `ndus` cookie is a long-lived session token that lets the bot fetch direct download links from TeraBox's private API.

1. Log in to **[1024tera.com](https://www.1024tera.com)** in your browser (Chrome/Edge recommended).
2. Open **DevTools** → `F12` → **Application** tab → **Cookies** → `https://www.1024tera.com`.
3. Find the cookie named **`ndus`** and copy its **Value**.
4. Also copy the **`browserid`** value (optional but helps avoid verification challenges).
5. Paste into `.env` as:

```
TERABOX_COOKIE=ndus=YOUR_NDUS_VALUE_HERE; lang=en; browserid=YOUR_BROWSERID
```

> **Important:** Never share your `.env` file or commit it to git. The `.gitignore` excludes it.
>
> The cookie expires when you log out or TeraBox invalidates the session. When that happens the bot will notify admin IDs with an `[ERR-COOKIE]` alert.

---

## ⚙️ Configuration

All configuration lives in `.env`. See `.env.example` for a template.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BOT_TOKEN` | ✅ | — | Get from [@BotFather](https://t.me/BotFather) |
| `API_ID` | ✅ | — | Get from [my.telegram.org](https://my.telegram.org/apps) |
| `API_HASH` | ✅ | — | Get from [my.telegram.org](https://my.telegram.org/apps) |
| `TERABOX_COOKIE` | ✅ | — | Your `ndus` browser cookie (see above) |
| `ADMIN_IDS` | ✅ | — | Comma-separated Telegram user IDs for admins |
| `ALLOWED_USERS` | — | Same as `ADMIN_IDS` | Extra users allowed to use the bot. Leave blank for admin-only. |
| `PRIVATE_CHAT_ID` | — | disabled | Channel ID (e.g. `-1001234567890`) to store uploads for instant caching. Leave blank to disable. |
| `DOWNLOAD_STREAMS` | — | `6` | Parallel HTTP Range connections per file (1–12). More = faster but may trigger throttling. |
| `MAX_FILE_SIZE_MB` | — | `2000` | Files larger than this get a direct-download link instead of being uploaded. |
| `MAX_CONCURRENT_DOWNLOADS` | — | `1` | How many downloads can run at once across all users. |
| `USER_RATE_LIMIT_SECONDS` | — | `30` | Minimum seconds between requests per user. |
| `FALLBACK_API_URL` | — | — | Optional URL of a fallback resolver API (e.g. a custom Cloudflare Worker). |
| `FALLBACK_API_KEY` | — | — | Bearer token for the fallback API. |

---

## 📁 Project Structure

```
terabot/
├── bot.py                 # Main bot — handlers, pipeline, queue
├── config.py              # All configuration from .env
├── core/
│   ├── resolver.py        # TeraBox share-link → file list (native /share/list API)
│   ├── downloader.py      # Parallel Range download engine
│   ├── uploader.py        # Telethon upload with video attributes + progress
│   ├── media.py           # ffprobe metadata + ffmpeg thumbnail
│   ├── database.py        # Async SQLite: cache + user registry
│   └── queue_manager.py   # Concurrency semaphore + per-user rate limiting
├── utils/
│   ├── helpers.py         # URL extraction, domain list, formatting helpers
│   └── logger.py          # Rotating log (10 MB × 5 backups → logs/bot.log)
├── test_resolver.py       # CLI tool: resolve + download without Telegram
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## 🧪 CLI Test (no Telegram needed)

Test the resolver and downloader directly:

```powershell
python test_resolver.py https://1024tera.com/s/YOURSHAREKEY
```

This prints the file list, downloads the first file, and (if ffprobe is available) verifies media integrity.

---

## 🤖 Bot Commands

| Command | Access | Description |
|---------|--------|-------------|
| `/start` | Allowed users | Welcome message and bot status |
| `/help` | Allowed users | Command list and supported domains |
| `/ping` | Allowed users | Round-trip latency check |
| `/broadcast <msg>` | Admins only | Send a message to all registered users |

**To download**: just paste any TeraBox / TeraBoxApp / 1024tera link into the chat.

---

## 🌐 Supported Domains

`terabox.com`, `teraboxapp.com`, `1024tera.com`, `4funbox.com`, `mirrobox.com`,
`nephobox.com`, `freeterabox.com`, `momerybox.com`, `tibibox.com`, `gibibox.com`,
`pebibox.com`, `dubox.com`, and all their subdomains and mirrors.

---

## ⚠️ Folder / Multi-File Support (Experimental)

The bot can resolve share links that contain **multiple files or folders**, but this feature is **untested in production**.

- The bot will warn you: *"Folder support is experimental"*
- Files are processed one by one in sequence
- Very large folders (100+ files) may time out

---

## 📊 Measured Download Speeds

Benchmarked on a real 7.70 MB video file (`Beautiful_Paki_CabinCrew.mp4`):

| Streams | Speed | Time |
|---------|-------|------|
| 1 | 0.132 MB/s | 58.1 s |
| 4 | 0.185 MB/s | 41.6 s |
| 8 | 0.357 MB/s | 21.6 s |
| **6 (default)** | **0.490 MB/s avg, 1.64 MB/s peak** | **15.8 s** |

> These are honest measured numbers, not estimates. TeraBox throttles individual connections; parallel streams bypass the per-connection limit.

---

## 📝 Logs

Logs are written to `logs/bot.log` with automatic rotation (max 10 MB, 5 backups kept).

The log **never** records cookie values, bot tokens, or API keys.

Error codes in log / user messages:

| Code | Meaning |
|------|---------|
| `[ERR-COOKIE]` | TeraBox session cookie expired or triggered verify_v2 |
| `[ERR-SHARE]` | Share link expired, private, region-locked, or deleted |
| `[ERR-CF]` | Cloudflare blocked the request — retry in 1-2 minutes |
| `[ERR-RESOLVE]` | General resolver error (see log for full traceback) |
| `[ERR-UPLOAD]` | Telegram upload failed (see log for full traceback) |
| `[ERR-UNEXPECTED]` | Unhandled exception — always has full traceback in log |

---

## 🔒 Security Notes

- `.env` is excluded from git (`.gitignore`)
- `*.session`, `*.db`, `logs/`, `downloads/`, `cookie.env/` are all excluded
- Cookie and tokens are never logged or printed
- Only users in `ALLOWED_USERS` (defaults to `ADMIN_IDS`) can use the bot
- All downloads share your one TeraBox session — treat the bot as personal

---

## 🛑 Graceful Shutdown

Press **Ctrl+C**. The bot will:
1. Stop accepting new requests
2. Delete leftover `.part` download files
3. Close the database connection

---

## 🐳 Optional: Docker / Heroku

Not covered here. The bot runs cleanly on bare Windows without any container.
If you want to deploy to a VPS or Docker later, ensure:
- `ffmpeg` and `ffprobe` are in the container image
- The `.env` file is mounted as a secret (not baked into the image)
- Persist `terabot_session.session` and `terabot.db` across restarts (volume mount)
