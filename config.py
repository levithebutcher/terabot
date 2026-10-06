import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from workspace root
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

# Telegram Bot Credentials
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
API_ID_RAW: str = os.getenv("API_ID", "").strip()
API_HASH: str = os.getenv("API_HASH", "").strip()

try:
    API_ID: int = int(API_ID_RAW) if API_ID_RAW else 0
except ValueError:
    API_ID = 0

# Admin IDs
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: list[int] = []
if ADMIN_IDS_RAW:
    for aid in ADMIN_IDS_RAW.split(","):
        aid = aid.strip()
        if aid.isdigit() or (aid.startswith("-") and aid[1:].isdigit()):
            ADMIN_IDS.append(int(aid))

# Access Control: ALLOWED_USERS (defaults to ADMIN_IDS if empty)
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "").strip()
ALLOWED_USERS: list[int] = []
if ALLOWED_USERS_RAW:
    for uid in ALLOWED_USERS_RAW.split(","):
        uid = uid.strip()
        if uid.isdigit() or (uid.startswith("-") and uid[1:].isdigit()):
            ALLOWED_USERS.append(int(uid))
else:
    ALLOWED_USERS = list(ADMIN_IDS)

# Optional Storage Chat ID for file caching
PRIVATE_CHAT_ID_RAW = os.getenv("PRIVATE_CHAT_ID", "").strip()
PRIVATE_CHAT_ID: int | None = None
if PRIVATE_CHAT_ID_RAW:
    try:
        PRIVATE_CHAT_ID = int(PRIVATE_CHAT_ID_RAW)
    except ValueError:
        PRIVATE_CHAT_ID = None

# TeraBox Credentials
TERABOX_COOKIE: str = os.getenv("TERABOX_COOKIE", "").strip()

# Parallel Download Streams (Default 6, clamp between 1 and 12)
DOWNLOAD_STREAMS_RAW = os.getenv("DOWNLOAD_STREAMS", "6").strip()
try:
    DOWNLOAD_STREAMS: int = max(1, min(12, int(DOWNLOAD_STREAMS_RAW)))
except ValueError:
    DOWNLOAD_STREAMS = 6

# Max File Size in MB (Default 2000 MB for standard Telegram MTProto limits)
MAX_FILE_SIZE_MB_RAW = os.getenv("MAX_FILE_SIZE_MB", "2000").strip()
try:
    MAX_FILE_SIZE_MB: int = int(MAX_FILE_SIZE_MB_RAW)
except ValueError:
    MAX_FILE_SIZE_MB = 2000

# Concurrency & Rate Limiting
MAX_CONCURRENT_DOWNLOADS_RAW = os.getenv("MAX_CONCURRENT_DOWNLOADS", "1").strip()
try:
    MAX_CONCURRENT_DOWNLOADS: int = max(1, int(MAX_CONCURRENT_DOWNLOADS_RAW))
except ValueError:
    MAX_CONCURRENT_DOWNLOADS = 1

USER_RATE_LIMIT_SECONDS_RAW = os.getenv("USER_RATE_LIMIT_SECONDS", "30").strip()
try:
    USER_RATE_LIMIT_SECONDS: int = max(5, int(USER_RATE_LIMIT_SECONDS_RAW))
except ValueError:
    USER_RATE_LIMIT_SECONDS = 30

# Fallback API Configuration (Optional)
FALLBACK_API_URL: str = os.getenv("FALLBACK_API_URL", "").strip()
FALLBACK_API_KEY: str = os.getenv("FALLBACK_API_KEY", "").strip()

# File Paths
DOWNLOAD_DIR: Path = BASE_DIR / os.getenv("DOWNLOAD_DIR", "downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH: Path = BASE_DIR / "terabot.db"
LOG_DIR: Path = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE: Path = LOG_DIR / "bot.log"
