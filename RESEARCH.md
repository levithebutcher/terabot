# TeraBox Downloader Research & Technical Analysis

This document provides a comparative analysis of open-source TeraBox downloaders, link extractors, and Telegram bots. It evaluates their link resolution mechanisms, authentication requirements, Telegram upload architectures, and current operational status.

---

## 1. Reference Repositories Comparison

### 1. [abdul97233/TeraBox-Downloader-Bot](https://github.com/abdul97233/TeraBox-Downloader-Bot) *(Main Reference; License: GPL-3.0)*

* **Link Resolution Mechanism**:
  * Does **not** resolve TeraBox links natively through web scraping or official API reversing in the bot itself.
  * Offloads link resolution to external third-party HTTP API endpoints configured via `API_ENDPOINTS` (or legacy `TERABOX_API_TEMPLATE` / `TERABOX_FALLBACK_API_TEMPLATE`).
  * Features a load balancer (`utils/loadbalancer.py`) that distributes requests across multiple backend endpoints using round-robin with circuit-breaker fault tolerance (disabling endpoints failing 5+ consecutive times).
* **Authentication / Cookies**:
  * Does not use a direct TeraBox `ndus` cookie in the bot configuration.
  * Requires API tokens (`authkey`) for the external third-party resolver API services.
* **Telegram Upload Architecture**:
  * Built using **Telethon** with `FastTelethon.py` for parallel multi-part MTProto chunk uploading.
  * Supports self-hosted Telegram Bot API server (`TG_API_BASE`) allowing up to 2 GB file uploads directly via standard Bot API HTTP endpoints (`sendVideo`, `sendDocument`, `sendPhoto`) using a custom streaming wrapper `_ProgressFileWrapper`.
  * Generates video thumbnails with OpenCV (`cv2`), optionally adds video watermarks using `ffmpeg` subprocesses, and extracts duration/dimensions.
  * Stores file records and lock tokens in Redis.
* **Broken / Outdated / Shortcomings**:
  * **Completely inoperable out of the box** without deploying or purchasing access to a private external TeraBox resolving backend (`https://your-primary-api.com/`).
  * Heavy server dependencies: requires a running Redis instance, OpenCV (`opencv-python`), and FFmpeg. Not Windows-friendly out of the box.
  * GPL-3.0 license requires any derivative bot inheriting its code to be GPL-3.0. A clean-room rewrite is necessary to avoid licensing entanglements.

---

### 2. [Hrishi2861/Terabox-Downloader-Bot](https://github.com/Hrishi2861/Terabox-Downloader-Bot)

* **Link Resolution Mechanism**:
  * Relies completely on a single hardcoded third-party Cloudflare Worker endpoint:
    `https://teradlrobot.cheemsbackup.workers.dev/?url={encoded_url}`
* **Authentication / Cookies**:
  * None configured in the bot; completely reliant on whatever credentials the worker has baked in.
* **Telegram Upload Architecture**:
  * Built using **Pyrogram (PyroFork)**.
  * Delegates file downloading to an external **Aria2c RPC daemon** listening on `localhost:6800`.
  * Checks video length via `ffprobe` and splits videos exceeding 2 GB (or 4 GB if a Telegram user session string is provided) into parts using FFmpeg.
  * Uploads to Telegram via Pyrogram's `send_document` / `send_video` with progress callbacks.
* **Broken / Outdated / Shortcomings**:
  * **Dead endpoint**: The worker `teradlrobot.cheemsbackup.workers.dev` now returns HTTP 404.
  * External dependency overhead: Requires an active `aria2c` RPC server daemon and system `ffmpeg`. If the aria2 daemon is not running, the bot crashes immediately on message receipt.
  * Code bug: Line 288 in `terabox.py` invokes command `'xtra'` instead of `'ffmpeg'`.

---

### 3. [r0ld3x/terabox-downloader-bot](https://github.com/r0ld3x/terabox-downloader-bot)

* **Link Resolution Mechanism**:
  * Rewrites domain to `1024terabox.com`, makes an initial page GET to fetch an OpenGraph thumbnail, then queries a third-party API:
    `https://ytshorts.savetube.me/api/v1/terabox-downloader`
  * Parses JSON for `"Fast Download"` and `"HD Video"` download URLs.
* **Authentication / Cookies**:
  * No TeraBox cookies used.
* **Telegram Upload Architecture**:
  * Built using **Telethon** and `FastTelethon.py`.
  * Uploads media to a dedicated `PRIVATE_CHAT_ID` channel and caches the Telegram message ID in Redis. Subsequent requests for the same URL forward the cached message instantly.
  * Includes an inline "Stop" button to cancel active uploads.
* **Broken / Outdated / Shortcomings**:
  * **Dead endpoint**: The domain `ytshorts.savetube.me` no longer exists (DNS resolution fails with `getaddrinfo failed`).
  * Only handles single video files; completely ignores directories/multi-file shares.
  * Requires a Redis database.

---

### 4. [abhinai2244/TeraBox-Dl](https://github.com/abhinai2244/TeraBox-Dl)

* **Link Resolution Mechanism**:
  * **Native direct API resolution**!
  * Requests `https://dm.terabox.app/sharing/link?surl={surl}` with Chrome browser headers.
  * Extracts the internal `jsToken` using regex: `re.search(r'fn%28%22(.*?)%22%29', response.text)`.
  * Calls the official TeraBox API endpoint:
    `https://dm.terabox.app/share/list?app_id=250528&jsToken={jsToken}&site_referer=https://www.terabox.app/&shorturl={short_url}&root=1`
  * Parses file metadata (`server_filename`, `size`, `dlink`, `thumbs`) from the returned JSON.
  * Includes a Bun/TypeScript microservice wrapper with in-memory caching.
* **Authentication / Cookies**:
  * Requires a TeraBox account session cookie containing `ndus`.
* **Telegram Upload Architecture**:
  * None (this project is strictly an API/link extractor, not a Telegram bot).
* **Broken / Outdated / Shortcomings**:
  * Hardcoded sample `ndus` cookie in the repo is expired.
  * Only extracts the first file in `data.list[0]`; does not handle nested folders or pagination.
  * Lacks fallback mechanisms if Cloudflare blocks `dm.terabox.app`.

---

### 5. [MrAbhi2k3/TeraboxLinkExtractor](https://github.com/MrAbhi2k3/TeraboxLinkExtractor)

* **Link Resolution Mechanism**:
  * Follows redirect from the share URL to obtain the canonical `surl`.
  * Fetches the mobile wap page: `http://www.terabox.com/wap/share/filelist?surl={key}`.
  * Extracts `jsToken` using BeautifulSoup from script tags containing `try {eval(decodeURIComponent`.
  * Calls `https://www.terabox.com/share/list?app_id=250528&jsToken={jsToken}&shorturl={key}&root=1`.
* **Authentication / Cookies**:
  * Requires the `ndus` cookie in headers: `Cookie: ndus={TERA_COOKIE}`.
* **Telegram Upload Architecture**:
  * None. It uses Telethon solely to reply with the extracted text `dlink` link. Does not download or upload files.
* **Broken / Outdated / Shortcomings**:
  * Explicitly raises an error if the share link contains more than 1 file or a folder:
    `if item.get("isdir") != "0": raise DDLException("Folders are not supported")`
  * The TeraBox mobile wap page layout has changed; script tag extraction on `wap/share/filelist` frequently fails.
  * Hardcoded secrets directly in script variables.

---

## 2. Maintained TeraBox Bots & APIs (Recent 6 Months / 2024–2026)

| Project | Type / Language | Resolution Approach | Status / Observations |
| :--- | :--- | :--- | :--- |
| **[Damantha126/TeraboxDL](https://github.com/Damantha126/TeraboxDL)** (`terabox-downloader` on PyPI) | Python library (v1.8) | Reverse-engineered official `/share/list` endpoint with dynamic `jsToken`, `dp-logid`, and `bdstoken` extraction. | **Active & Maintained**. Supports resumable HTTP chunk downloads (`Range` header) and progress callbacks. Requires `ndus` & `lang` cookies. |
| **[saahiyo/terabox-gateway](https://github.com/saahiyo/terabox-gateway)** | Python / Flask API | Multi-mode gateway (`resolve`, `stream`, `lookup`) parsing TeraBox share pages and querying `/share/list`. | **Active**. Supports Vercel deployment. Requires `ndus` cookie for full `dlink` retrieval. |
| **[seiya-npm/terabox-api](https://github.com/seiya-npm/terabox-api)** | Node.js / NPM | Reverses internal TeraBox web endpoints (`share/list`, `shorturlinfo`). | **Active**. Emphasizes session token rotation and cookie management. |
| **[tttt369/tera-download](https://github.com/tttt369/tera-download)** | Userscript (JS) | Intercepts in-browser XHR responses when the user is logged into TeraBox web. | **Active**. Proves that `dlink` is directly bound to authenticated sessions. |
| **[aruxyz/trauso](https://github.com/aruxyz/trauso)** | Windows Desktop App | Multi-connection desktop downloader with built-in cookie manager. | **Active**. High-speed parallel chunk downloader for Windows. |

---

## 3. Comprehensive Feature Matrix

| Feature | `abdul97233` | `Hrishi2861` | `r0ld3x` | `abhinai2244` | `MrAbhi2k3` | `TeraboxDL` |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Direct TeraBox API** | ❌ (3rd party) | ❌ (Worker) | ❌ (3rd party) | ✅ (Direct) | ✅ (Direct) | ✅ (Direct) |
| **Cookie Required** | ❌ (Needs API key) | ❌ | ❌ | ✅ (`ndus`) | ✅ (`ndus`) | ✅ (`ndus` + `lang`) |
| **Direct Link (`dlink`) Generation** | ✅ (Via API) | ❌ (Dead) | ❌ (Dead) | ✅ | ✅ | ✅ |
| **Multi-File Folder Support** | ✅ | ❌ | ❌ | ❌ | ❌ | ⚠️ (Single file default) |
| **Telegram File Upload** | ✅ (Telethon / Bot API) | ✅ (Pyrogram) | ✅ (Telethon) | ❌ | ❌ (Link only) | ❌ |
| **Windows Local Execution** | ⚠️ Hard (Redis/Linux) | ⚠️ (Aria2 daemon) | ⚠️ (Redis) | ✅ | ✅ | ✅ |
| **License** | GPL-3.0 | Not specified | MIT | MIT | MIT | MIT |

---

## 4. Empirical Verification Findings (Step 2 Testing)

Live network tests were executed against real public TeraBox links (`https://1024tera.com/s/1HSEb8PZRUE7Z1Tvd3ZtT0g` and `https://terabox.app/s/...`):

1. **Third-Party Resolvers are Ephemeral**:
   * `teradlrobot.cheemsbackup.workers.dev` returned `HTTP 404`.
   * `ytshorts.savetube.me` failed at the DNS resolution stage (`getaddrinfo failed`).
   * Public web tools like `teraboxdownloader.pro` are gated by Cloudflare Turnstile CAPTCHA (HTTP 403).
   * **Conclusion**: Relying solely on hardcoded free third-party endpoints guarantees bot failure within weeks.
2. **Direct Official Web API (`share/list`) is Robust and Responsive**:
   * Requesting `https://www.1024tera.com/sharing/link?surl={surl}` or `https://dm.terabox.app/sharing/link?surl={surl}` returned `HTTP 200` with complete HTML.
   * `jsToken` is easily extracted from the HTML payload using `fn%28%22(.*?)%22%29`.
   * Querying `/share/list` with `app_id=250528&jsToken={jsToken}&shorturl={surl}&root=1` returns `errno: 0` and full file metadata:
     * File name: `Beautiful_Paki_CabinCrew.mp4`
     * File size: `8071052` bytes (~7.7 MB)
     * High-res thumbnails: `thumbs.url3`
     * Video dimensions: `352x640`, duration: `174s`
3. **The Role of the `ndus` Cookie**:
   * When `/share/list` is called without a cookie, TeraBox returns full file metadata, but leaves `dlink` omitted.
   * Calling `/api/shorturlinfo` or `/share/download` without an authenticated session yields `errno: 400210 ("need verify_v2")`.
   * When an authenticated `ndus` session cookie is sent with `/share/list`, TeraBox populates `dlink` directly in the file object.
   * Additionally, downloading the `dlink` requires sending the `Cookie: ndus=...` and standard browser `User-Agent` and `Referer: https://terabox.com/` headers to prevent HTTP 403 / 502 download errors.

---

## 5. Architectural Recommendations for Our Bot

1. **Primary Link Resolver**:
   * Direct native resolver using `aiohttp` / `requests`: fetches the share page, extracts `jsToken` and `dp-logid`, and calls the official `/share/list` endpoint with user's `TERABOX_COOKIE`.
   * Handles multi-file directories (loops over `list` items, supports `isdir == 1` recursive exploration).
2. **Fallback Resolver**:
   * A configurable secondary resolver endpoint in `.env` (`FALLBACK_API_URL` / `FALLBACK_API_KEY`) supporting external API services (or self-hosted workers/gateways).
   * If the primary direct resolver encounters a Cloudflare challenge or expired cookie, it automatically invokes the fallback resolver.
3. **Telegram Framework Choice**:
   * **Telethon** vs. **python-telegram-bot (PTB)**:
     * PTB (v20+) is pure Bot API HTTP. Standard Bot API limits file uploads to 50 MB (unless running a self-hosted Bot API server).
     * **Telethon** uses Telegram's MTProto protocol directly with `bot_token`. It can upload files up to **2,000 MB (2 GB)** without needing any external server!
     * With `FastTelethon` chunk parallelization (4 concurrent connections), Telethon uploads at 10-30 MB/s, compared to single-stream Bot API.
     * **Decision**: We recommend **Telethon** for high upload limits (up to 2GB) and maximum speed, or **python-telegram-bot** if pure Bot API simplicity is preferred. Telethon is significantly superior for video download bots because 50MB is too small for most videos.
4. **Local Windows Storage & Caching**:
   * Replace Redis with **SQLite** (`aiosqlite`): zero installation needed on Windows, single `.db` file, ACID-compliant link cache, rate limiting, and authorized user tracking.
5. **Clean Room Implementation**:
   * Write completely fresh, clean code inspired by the underlying HTTP protocol rather than copying GPL-3.0 code from `abdul97233`.
