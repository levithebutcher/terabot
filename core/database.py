import aiosqlite
import time
from typing import Optional
from config import DB_PATH
from utils.logger import logger


class Database:
    """Async SQLite database manager for file caching and user management."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    async def init_db(self):
        """Initialize SQLite tables for caching and user tracking."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cached_files (
                    file_key TEXT PRIMARY KEY,
                    file_name TEXT,
                    file_size INTEGER,
                    channel_id INTEGER,
                    message_id INTEGER,
                    created_at REAL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_seen REAL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    filter_mode TEXT DEFAULT 'all',
                    delivery_mode TEXT DEFAULT 'both'
                )
            """)
            try:
                await db.execute("ALTER TABLE user_settings ADD COLUMN delivery_mode TEXT DEFAULT 'both'")
            except Exception:
                pass
            await db.commit()
        logger.info(f"Database initialized at {self.db_path}")

    async def get_cached_file(self, file_key: str) -> Optional[tuple[int, int]]:
        """Retrieve (channel_id, message_id) for a cached file key."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT channel_id, message_id FROM cached_files WHERE file_key = ?",
                (file_key,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return int(row[0]), int(row[1])
        return None

    async def save_cached_file(
        self, file_key: str, file_name: str, file_size: int, channel_id: int, message_id: int
    ):
        """Save an uploaded file record to cache."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO cached_files 
                (file_key, file_name, file_size, channel_id, message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (file_key, file_name, file_size, channel_id, message_id, time.time()),
            )
            await db.commit()
        logger.info(f"Cached file {file_key} -> channel: {channel_id}, msg: {message_id}")

    async def record_user(self, user_id: int, username: str, first_name: str):
        """Record or update user activity."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_seen = excluded.last_seen
                """,
                (user_id, username or "", first_name or "", time.time()),
            )
            await db.commit()

    async def get_all_users(self) -> list[int]:
        """Get all user IDs for broadcast."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT user_id FROM users") as cursor:
                rows = await cursor.fetchall()
                return [int(r[0]) for r in rows]

    async def get_user_filter(self, user_id: int) -> str:
        """Get user's default filter mode ('all', 'video', 'photo')."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT filter_mode FROM user_settings WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    return str(row[0]).lower()
        return "all"

    async def set_user_filter(self, user_id: int, filter_mode: str):
        """Set user's default filter mode ('all', 'video', 'photo')."""
        filter_mode = filter_mode.lower()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_settings (user_id, filter_mode)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET filter_mode = excluded.filter_mode
                """,
                (user_id, filter_mode),
            )
            await db.commit()

    async def get_user_delivery_mode(self, user_id: int) -> str:
        """Get user's delivery mode ('both' or 'channel'). Default is 'both' (Mode B)."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT delivery_mode FROM user_settings WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    return str(row[0]).lower()
        return "both"

    async def set_user_delivery_mode(self, user_id: int, delivery_mode: str):
        """Set user's delivery mode ('both' or 'channel')."""
        delivery_mode = delivery_mode.lower()
        if delivery_mode not in ["both", "channel"]:
            delivery_mode = "both"
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO user_settings (user_id, delivery_mode)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET delivery_mode = excluded.delivery_mode
                """,
                (user_id, delivery_mode),
            )
            await db.commit()

    async def get_cached_file_by_name_size(self, file_name: str, file_size: int = 0) -> Optional[tuple[int, int]]:
        """Retrieve (channel_id, message_id) by exact or matching name & size from SQLite cache."""
        clean_name = file_name.strip().lower()
        if not clean_name:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            if file_size > 0:
                async with db.execute(
                    "SELECT channel_id, message_id, file_size FROM cached_files WHERE LOWER(file_name) = ? OR LOWER(file_name) LIKE ?",
                    (clean_name, f"%{clean_name}%"),
                ) as cursor:
                    rows = await cursor.fetchall()
                    for c_id, m_id, sz in rows:
                        if abs(sz - file_size) < 2 * 1024 * 1024:
                            return int(c_id), int(m_id)
            else:
                async with db.execute(
                    "SELECT channel_id, message_id FROM cached_files WHERE LOWER(file_name) = ?",
                    (clean_name,),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return int(row[0]), int(row[1])
        return None

    async def sync_channel_cache(self, client, channel_id: Optional[int], limit: int = 300):
        """Index recent Storage Channel media messages into SQLite cache for 0-second matching."""
        if not channel_id:
            return
        try:
            count = 0
            async for msg in client.iter_messages(channel_id, limit=limit):
                if not msg or not msg.media:
                    continue
                caption = msg.text or msg.message or ""
                f_name = msg.file.name if getattr(msg, "file", None) else ""
                f_size = msg.file.size if getattr(msg, "file", None) else 0

                # Check for hashtag key (e.g. #mg_abc123 or #tb_12345)
                m_tag = re.search(r"#(mg_[a-zA-Z0-9_\-]+|tb_[a-zA-Z0-9_\-]+)", caption)
                file_key = m_tag.group(1) if m_tag else (f"raw_{abs(hash(f_name + str(f_size)))}" if f_name else None)

                if file_key:
                    await self.save_cached_file(file_key, f_name or "media", f_size, channel_id, msg.id)
                    count += 1
            logger.info(f"Storage channel cache synchronized: {count} media records indexed.")
        except Exception as e:
            logger.debug(f"Storage channel cache sync notice: {e}")

    async def find_cached_media(
        self,
        client,
        channel_id: Optional[int],
        file_key: str,
        file_name: str = "",
        file_size: int = 0,
    ):
        """
        Search for a media item in local SQLite or the remote Storage Channel.
        Supports:
        1. Exact file_key in local SQLite cache (instant O(1))
        2. Name + Size in local SQLite cache (instant O(1))
        3. Exact hashtag scan in channel (#file_key)
        4. Name & Size fallback in channel
        """
        if not file_key and not file_name:
            return None

        # 1. Check local SQLite cache by file_key (instant 0 ms)
        if file_key:
            cached = await self.get_cached_file(file_key)
            if cached:
                c_id, msg_id = cached
                try:
                    msg = await client.get_messages(c_id, ids=msg_id)
                    if msg and msg.media:
                        return msg
                except Exception as e:
                    logger.debug(f"Could not fetch cached message {msg_id} from {c_id}: {e}")

        # 2. Check local SQLite cache by name & size (instant 0 ms)
        if file_name:
            cached_by_name = await self.get_cached_file_by_name_size(file_name, file_size)
            if cached_by_name:
                c_id, msg_id = cached_by_name
                try:
                    msg = await client.get_messages(c_id, ids=msg_id)
                    if msg and msg.media:
                        return msg
                except Exception as e:
                    logger.debug(f"Could not fetch cached message {msg_id} from {c_id}: {e}")

        # 3. Check remote Storage Channel if channel_id is provided
        if channel_id:
            tag = f"#{file_key}" if file_key else ""
            clean_name = file_name.strip().lower() if file_name else ""

            try:
                # Scan recent channel messages for tag OR filename + size
                async for msg in client.iter_messages(channel_id, limit=80):
                    if msg and msg.media:
                        caption = msg.text or msg.message or ""
                        m_name = (msg.file.name or "").lower() if getattr(msg, "file", None) else ""
                        m_size = msg.file.size if getattr(msg, "file", None) else 0

                        # Match tag
                        if tag and tag in caption:
                            f_name = msg.file.name if getattr(msg, "file", None) else ""
                            f_size = msg.file.size if getattr(msg, "file", None) else 0
                            await self.save_cached_file(file_key, f_name, f_size, channel_id, msg.id)
                            return msg

                        # Match by filename & approximate size
                        if clean_name and (clean_name in m_name or clean_name in caption.lower()):
                            if file_size == 0 or abs(m_size - file_size) < 2 * 1024 * 1024:
                                logger.info(f"Matched file in storage channel history: {file_name}")
                                if file_key:
                                    await self.save_cached_file(file_key, file_name, m_size, channel_id, msg.id)
                                return msg
            except Exception as e:
                logger.debug(f"Storage channel scan notice: {e}")

        return None


db = Database()

