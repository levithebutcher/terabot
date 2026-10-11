# 🤖 TeraBot v3.0 — TeraBox & Mega.nz High-Speed Telegram Downloader

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Telethon](https://img.shields.io/badge/Telethon-1.45+-orange.svg)](https://github.com/LonamiWebs/Telethon)
[![Mega.nz](https://img.shields.io/badge/Mega.nz-AES--CTR-red.svg)](https://mega.nz/)
[![Google Colab](https://img.shields.io/badge/Colab-v3.0%20Ready-yellow.svg)](https://colab.research.google.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

An asynchronous, high-performance Telegram bot that downloads files and folders from **Mega.nz** and **TeraBox** (plus 30+ mirror domains), processes them with **Zero-Bandwidth Deduplication**, and uploads them to your Telegram Storage Vault Channel and private DMs up to Telegram's 2000 MB (2 GB) MTProto limit.

---

## 🌟 What's New in v3.0

- ⚡ **Zero-Bandwidth Upfront Deduplication**: Deep-scans your Storage Channel messages (`ensure_channel_synced`). Files already in the storage channel are **instantly skipped** before opening any stream connection—saving server bandwidth and avoiding rate limits.
- 📁 **Mega Subfolder Explorer**: Interactive inline navigation for complex Mega directory trees with 6 folders per page, pagination (`⬅️ Previous` / `Next ➡️`), and search (`🔍 Search Subfolder`).
- 🔄 **Sequential Folder-by-Folder Downloads**: Downloads multi-folder shares strictly folder-by-folder (smallest to largest), flushing albums at each folder boundary to prevent cross-folder mixing.
- 🛡️ **Dual Delivery Modes (`/delivery`)**:
  - `channel` (Vault Mode / Default): Saves all media silently to your Storage Channel. DM receives only progress cards and completion receipts (zero spam).
  - `both` (Dual Mode / Optional): Saves to the Storage Channel AND forwards newly downloaded files to your bot DM. Cached files are skipped silently.
- ☁️ **MongoDB Atlas Cross-Environment Caching**: Shared cloud cache across Railway, Google Colab, and local workstation ensures that files are never re-downloaded even across server redeploys or environment switches. Fallback to local SQLite when unconfigured.
- 🎬 **10-Item Telegram Album Batching**: Automatically groups videos ($\le 20\text{ MB}$) and photos ($\le 100\text{ MB}$) into 10-item Telegram media albums.
- 🎯 **File Type Filtering (`/filter`)**: Select download filters on the fly (`/filter video`, `/filter photo`, `/filter other`, `/filter all`).
- 📊 **Real-Time Progress & Direct Channel Button**: Live progress cards with transfer speeds and estimated time of arrival (ETA), finishing with a one-click button linking directly to the storage channel.

---

## 🏗️ Architecture & Engines

```text
terabot/
├── bot.py                       # Main Telegram dispatcher, commands & event callbacks
├── config.py                    # Environment configuration loader (.env)
├── requirements.txt             # Async dependencies (Telethon, aiosqlite, curl_cffi, etc.)
├── terabot_v3_colab.ipynb       # Production Google Colab High-Speed notebook
├── core/
│   ├── database.py              # SQLite cache, channel deep-indexer & deduplication
│   ├── mega_downloader.py       # Mega folder tree parser, AES-CTR streaming, explorer
│   ├── downloader.py            # TeraBox parallel HTTP Range multi-stream downloader
│   ├── uploader.py              # Telethon high-speed multi-worker parallel uploader
│   ├── resolver.py              # TeraBox link resolver with curl_cffi TLS impersonation
│   ├── media.py                 # FFmpeg/FFprobe thumbnail & video duration extraction
│   └── queue_manager.py         # Concurrency slots & per-user rate-limiting
└── utils/
    ├── helpers.py               # URL regex extraction, domain matching, formatters
    ├── logger.py                # Rotating file logger
    └── jokes.py                 # Fun waiting notices
```

---

## 🤖 Bot Commands

| Command | Description | Permission |
| :--- | :--- | :--- |
| `/start` | Welcome card, active delivery mode, filter badges & usage guide | All allowed users |
| `/delivery` | View or toggle delivery mode (`🛡️ Channel Only` or `📬 Channel + DM`) | All allowed users |
| `/filter <mode>` | Change file download filter (`video`, `photo`, `other`, `all`) | All allowed users |
| `/stop` | Abort your active download or upload task | All allowed users |
| `/broadcast <msg>` | Broadcast an announcement message to all registered users | Admins only |

---

## 🌐 Supported Domains

- **Mega.nz**: `mega.nz`, `mega.co.nz`, `mega.io` (files, folders, embeds)
- **TeraBox & Mirrors**:
  `terabox.com`, `terabox.app`, `teraboxapp.com`, `1024tera.com`, `1024tera.co`, `1024terabox.com`,
  `4funbox.com`, `mirrobox.com`, `nephobox.com`, `freeterabox.com`, `momerybox.com`, `tibibox.com`,
  `gibibox.com`, `pebibox.com`, `dubox.com`, `bestclouddrive.com`, and all related shortlinks.

---

## 🚀 Deployment Options

### Option A: Google Colab (Recommended — High Speed & Free 100+ GB)

Run the bot with Google's gigabit network and 100+ GB free storage:

1. Open [`terabot_v3_colab.ipynb`](file:///c:/Users/Lenovo/Desktop/terabotv2/terabot_v3_colab.ipynb) in [Google Colab](https://colab.research.google.com/).
2. Run **Step 1**: Installs system tools (`ffmpeg`, `megatools`), clones repository, and installs requirements.
3. Configure **Step 2**: Enter your Telegram credentials and Storage Channel ID.
4. Run **Step 3**: Starts the anti-disconnect background keep-alive audio loop.
5. Run **Step 4**: Starts the bot (`python bot.py`).

---

### Option B: Local Workstation (Windows / Linux)

#### 1. Clone repository & setup virtual environment
```powershell
git clone https://github.com/levithebutcher/terabot.git
cd terabot
py -m venv .venv
.venv\Scripts\Activate.ps1    # On Windows PowerShell
# source .venv/bin/activate    # On Linux / macOS
```

#### 2. Install dependencies & tools
```powershell
pip install -r requirements.txt
```
> **Note**: Install [FFmpeg](https://ffmpeg.org/download.html) and add it to your system `PATH` for video thumbnail generation and duration metadata.

#### 3. Create your `.env` configuration
Copy the template and fill in your values:
```powershell
Copy-Item .env.example .env
notepad .env
```

#### 4. Run the bot
```powershell
py bot.py
```

---

### Option C: Docker Deployment

```powershell
docker build -t terabot .
docker run -d --name terabot --env-file .env -v terabot_data:/app/downloads terabot
```

---

## ⚙️ Configuration Reference (`.env`)

| Variable | Required | Default | Description |
| :--- | :---: | :---: | :--- |
| `BOT_TOKEN` | ✅ | — | Telegram Bot Token from [@BotFather](https://t.me/BotFather) |
| `API_ID` | ✅ | — | Telegram API ID from [my.telegram.org](https://my.telegram.org/apps) |
| `API_HASH` | ✅ | — | Telegram API Hash from [my.telegram.org](https://my.telegram.org/apps) |
| `ADMIN_IDS` | ✅ | — | Comma-separated admin Telegram user IDs |
| `ALLOWED_USERS` | ❌ | Admin IDs | Users permitted to use the bot (leave blank for admins only) |
| `PRIVATE_CHAT_ID` | ❌ | None | Storage Channel ID (e.g. `-1001234567890`) for vault archiving & deduplication |
| `TERABOX_COOKIE` | ❌ | None | TeraBox `ndus` browser cookie (required for TeraBox downloads) |
| `DOWNLOAD_STREAMS` | ❌ | `12` | Parallel download streams per file (1–24) |
| `MAX_FILE_SIZE_MB` | ❌ | `2000` | Maximum file size in MB (capped at Telegram's 2000 MB limit) |
| `MAX_CONCURRENT_DOWNLOADS` | ❌ | `1` | Global maximum simultaneous download tasks |
| `USER_RATE_LIMIT_SECONDS` | ❌ | `10` | Cooldown between requests per user |
| `DOWNLOAD_DIR` | ❌ | `downloads` | Local temporary download folder |

---

## 🍪 How to Obtain TeraBox Cookie (`ndus`)

1. Log into [1024tera.com](https://www.1024tera.com) in your browser.
2. Open Developer Tools (`F12`) → **Application** tab → **Cookies** → `https://www.1024tera.com`.
3. Locate the cookie named **`ndus`** and copy its value.
4. Add it to your `.env` file:
   ```env
   TERABOX_COOKIE=ndus=YOUR_NDUS_VALUE_HERE; lang=en
   ```

---

## 🔒 Security Best Practices

- **Never commit your `.env` file** or session credentials to public repositories.
- Keep `*.session`, `terabot.db`, and `cookie.env/` listed in your `.gitignore`.
- Run `/delivery channel` to preserve your private chat clean while keeping everything organized in your private storage channel.

---

## 📄 License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
