import asyncio
import re
import aiosqlite
import time
from typing import Optional
from config import DB_PATH
from utils.logger import logger


class Database:
    """Async SQLite database manager for file caching and user management."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.is_channel_synced: bool = False
        self._sync_lock: Optional[asyncio.Lock] = None

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
        stem = clean_name.rsplit(".", 1)[0] if "." in clean_name else clean_name
        async with aiosqlite.connect(self.db_path) as db:
            if file_size > 0:
                async with db.execute(
                    """
                    SELECT channel_id, message_id, file_size, file_name 
                    FROM cached_files 
                    WHERE LOWER(file_name) = ? 
                       OR LOWER(file_name) LIKE ? 
                       OR ? LIKE '%' || LOWER(file_name) || '%'
                       OR (LENGTH(?) > 4 AND LOWER(file_name) LIKE '%' || ? || '%')
                    """,
                    (clean_name, f"%{clean_name}%", clean_name, stem, stem),
                ) as cursor:
                    rows = await cursor.fetchall()
                    for c_id, m_id, sz, fn in rows:
                        # Tolerance: within 3 MB or exact match
                        if abs(sz - file_size) < 3 * 1024 * 1024:
                            return int(c_id), int(m_id)
            else:
                async with db.execute(
                    "SELECT channel_id, message_id FROM cached_files WHERE LOWER(file_name) = ? OR LOWER(file_name) LIKE ?",
                    (clean_name, f"%{clean_name}%"),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return int(row[0]), int(row[1])
        return None

    async def sync_channel_cache(self, client, channel_id: Optional[int], limit: int = 1000, force: bool = False):
        """Index recent Storage Channel media messages into SQLite cache for 0-second matching."""
        if not channel_id or not client:
            return
        if self._sync_lock is None:
            self._sync_lock = asyncio.Lock()

        async with self._sync_lock:
            if self.is_channel_synced and not force:
                return

            try:
                count = 0
                async for msg in client.iter_messages(channel_id, limit=limit):
                    if not msg or not msg.media:
                        continue
                    caption = msg.text or msg.message or ""
                    f_name = msg.file.name if getattr(msg, "file", None) else ""
                    f_size = msg.file.size if getattr(msg, "file", None) else 0

                    # If f_name is missing (standard for Telegram video uploads), extract from caption
                    if not f_name and caption:
                        # 1. Backtick extraction: `filename.mp4`
                        m_code = re.search(r"[`'\"]([^`'\"]+\.[a-zA-Z0-9]{2,5})[`'\"]", caption)
                        if m_code:
                            f_name = m_code.group(1).strip()
                        else:
                            # 2. Icon pattern: 🎬 filename.mp4 or 📄 filename.ext
                            m_icon = re.search(r"(?:🎬|🖼️|📄|📁)\s*([^\n\r()]+?\.[a-zA-Z0-9]{2,5})", caption)
                            if m_icon:
                                f_name = m_icon.group(1).strip()
                            else:
                                # 3. Common media extension
                                m_ext = re.search(
                                    r"([a-zA-Z0-9_\-\.\s]+\.(?:mp4|mkv|avi|mov|wmv|flv|webm|jpg|jpeg|png|zip|rar|tar))",
                                    caption,
                                    re.IGNORECASE,
                                )
                                if m_ext:
                                    f_name = m_ext.group(1).strip()

                    # Extract node hashtag key (e.g. #mg_w21kxbCB or #tb_12345)
                    m_tag = re.search(r"#(mg_[a-zA-Z0-9_\-]+|tb_[a-zA-Z0-9_\-]+)", caption)
                    file_key = m_tag.group(1) if m_tag else None

                    if not file_key and f_name and f_size > 0:
                        file_key = f"name_{f_name.lower().strip()}_{f_size}"

                    if file_key:
                        await self.save_cached_file(file_key, f_name or "media", f_size, channel_id, msg.id)
                        count += 1

                self.is_channel_synced = True
                logger.info(f"Storage channel cache synchronized: {count} media records indexed.")
            except Exception as e:
                logger.debug(f"Storage channel cache sync notice: {e}")

    async def ensure_channel_synced(self, client, channel_id: Optional[int], limit: int = 1000):
        """Ensure storage channel has been indexed into SQLite before running pre-check."""
        if channel_id and not self.is_channel_synced:
            await self.sync_channel_cache(client, channel_id, limit=limit)

    async def partition_nodes_by_cache(
        self, client, channel_id: Optional[int], nodes: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        """
        Upfront 0-bandwidth batch partitioner.
        Checks all Mega nodes against Storage Channel cache:
        Returns:
            missing_nodes: list of nodes that MUST be downloaded from Mega servers.
            cached_nodes: list of nodes already in Storage Channel (Zero download needed!).
        """
        if not nodes:
            return [], []

        if channel_id:
            await self.ensure_channel_synced(client, channel_id)

        missing_nodes = []
        cached_nodes = []

        for node in nodes:
            h = node.get("h", "")
            name = node.get("name", "")
            size = node.get("size", 0)

            # 1. Exact node key match (mg_<h>)
            matched = False
            if h:
                cached = await self.get_cached_file(f"mg_{h}")
                if cached:
                    matched = True

            # 2. Match by filename and size in SQLite
            if not matched and name:
                cached_by_name = await self.get_cached_file_by_name_size(name, size)
                if cached_by_name:
                    matched = True

            if matched:
                cached_nodes.append(node)
            else:
                missing_nodes.append(node)

        return missing_nodes, cached_nodes

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
        3. Fallback sync if channel cache was not loaded yet
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

        # 3. If still not found and channel has not been synced, sync once and retry
        if channel_id and not self.is_channel_synced:
            await self.ensure_channel_synced(client, channel_id)

            if file_key:
                cached = await self.get_cached_file(file_key)
                if cached:
                    c_id, msg_id = cached
                    try:
                        msg = await client.get_messages(c_id, ids=msg_id)
                        if msg and msg.media:
                            return msg
                    except Exception:
                        pass

            if file_name:
                cached_by_name = await self.get_cached_file_by_name_size(file_name, file_size)
                if cached_by_name:
                    c_id, msg_id = cached_by_name
                    try:
                        msg = await client.get_messages(c_id, ids=msg_id)
                        if msg and msg.media:
                            return msg
                    except Exception:
                        pass

        return None


db = Database()

