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
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cached_size ON cached_files(file_size)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cached_name ON cached_files(file_name)")
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

    async def get_cached_file_by_exact_size(self, file_size: int) -> Optional[tuple[int, int]]:
        """Retrieve (channel_id, message_id) if exact file_size exists in SQLite cache."""
        if file_size <= 0:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT channel_id, message_id FROM cached_files WHERE file_size = ?",
                (file_size,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return int(row[0]), int(row[1])
        return None

    async def get_cached_file_by_name_size(self, file_name: str, file_size: int = 0) -> Optional[tuple[int, int]]:
        """Retrieve (channel_id, message_id) by matching name & size from SQLite cache."""
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

    async def search_channel_media(
        self,
        client,
        channel_id: Optional[int],
        file_key: str = "",
        file_name: str = "",
        file_size: int = 0,
    ) -> Optional[tuple[int, int]]:
        """
        Proposal 2: Universal Server-Side Telegram Deep Search & Multi-Tier Deduplication.
        Works across Railway & Colab without message count restrictions.
        1. Fast local SQLite cache lookup (0ms)
        2. Telegram Server Search by unique key (#mg_<h> or #tb_<fs_id>)
        3. Telegram Server Search by exact size tag (#sz_<bytes>) -> Rule 1
        4. Telegram Server Search by clean filename stem (with size verification) -> Rule 2
        """
        if not channel_id or not client:
            return None

        # --- TIER 1: FAST LOCAL SQLITE LOOKUP (0ms) ---
        if file_key:
            cached = await self.get_cached_file(file_key)
            if cached:
                return cached

        if file_size > 0:
            cached_by_size = await self.get_cached_file_by_exact_size(file_size)
            if cached_by_size:
                return cached_by_size

        if file_name and file_size > 0:
            cached_by_name = await self.get_cached_file_by_name_size(file_name, file_size)
            if cached_by_name:
                return cached_by_name

        # --- TIER 2: TELEGRAM SERVER-SIDE NATIVE SEARCH ---
        try:
            # 2A. Search by unique key hashtag
            if file_key:
                async for msg in client.iter_messages(channel_id, search=f"#{file_key}", limit=2):
                    if msg and msg.media:
                        sz = msg.file.size if getattr(msg, "file", None) else file_size
                        fn = msg.file.name if getattr(msg, "file", None) and msg.file.name else file_name
                        await self.save_cached_file(file_key, fn or "media", sz, channel_id, msg.id)
                        return channel_id, msg.id

            # 2B. Search by exact byte size tag (#sz_<bytes>) — Rule 1: Same file with different name
            if file_size > 0:
                async for msg in client.iter_messages(channel_id, search=f"#sz_{file_size}", limit=2):
                    if msg and msg.media:
                        fn = msg.file.name if getattr(msg, "file", None) and msg.file.name else file_name
                        fk = file_key or f"sz_{file_size}"
                        await self.save_cached_file(fk, fn or "media", file_size, channel_id, msg.id)
                        return channel_id, msg.id

            # 2C. Fallback search by filename stem
            if file_name and len(file_name.strip()) > 3:
                clean_stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
                search_term = re.sub(r"[^\w\s]", " ", clean_stem).strip()
                if len(search_term) >= 3:
                    words = search_term.split()
                    query = words[0] if len(words[0]) >= 4 else (search_term[:20])
                    async for msg in client.iter_messages(channel_id, search=query, limit=4):
                        if not msg or not msg.media:
                            continue
                        msg_size = msg.file.size if getattr(msg, "file", None) else 0
                        caption = msg.text or msg.message or ""
                        m_sz = re.search(r"#sz_(\d+)", caption)
                        if m_sz:
                            msg_size = int(m_sz.group(1))

                        # Rule 1 & 2: Verify size match (Different size = Different file!)
                        if file_size > 0 and msg_size == file_size:
                            fk = file_key or f"sz_{file_size}"
                            await self.save_cached_file(fk, file_name, file_size, channel_id, msg.id)
                            return channel_id, msg.id

                        if file_size > 0 and msg_size > 0 and abs(msg_size - file_size) < 3 * 1024 * 1024:
                            fk = file_key or f"sz_{file_size}"
                            await self.save_cached_file(fk, file_name, file_size, channel_id, msg.id)
                            return channel_id, msg.id

        except Exception as search_err:
            logger.debug(f"Telegram server-side search notice ({file_name}): {search_err}")

        return None

    async def sync_channel_cache(self, client, channel_id: Optional[int], limit: int = 500, force: bool = False):
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

                    # Extract #sz_ tag if present in caption
                    m_sz = re.search(r"#sz_(\d+)", caption)
                    if m_sz:
                        f_size = int(m_sz.group(1))

                    if not f_name and caption:
                        m_code = re.search(r"[`'\"]([^`'\"]+\.[a-zA-Z0-9]{2,5})[`'\"]", caption)
                        if m_code:
                            f_name = m_code.group(1).strip()
                        else:
                            m_icon = re.search(r"(?:🎬|🖼️|📄|📁)\s*([^\n\r()]+?\.[a-zA-Z0-9]{2,5})", caption)
                            if m_icon:
                                f_name = m_icon.group(1).strip()
                            else:
                                m_ext = re.search(
                                    r"([a-zA-Z0-9_\-\.\s]+\.(?:mp4|mkv|avi|mov|wmv|flv|webm|jpg|jpeg|png|zip|rar|tar))",
                                    caption,
                                    re.IGNORECASE,
                                )
                                if m_ext:
                                    f_name = m_ext.group(1).strip()

                    m_tag = re.search(r"#(mg_[a-zA-Z0-9_\-]+|tb_[a-zA-Z0-9_\-]+)", caption)
                    file_key = m_tag.group(1) if m_tag else None

                    if not file_key and f_size > 0:
                        file_key = f"sz_{f_size}"

                    if file_key:
                        await self.save_cached_file(file_key, f_name or "media", f_size, channel_id, msg.id)
                        count += 1

                self.is_channel_synced = True
                logger.info(f"Storage channel cache synchronized: {count} media records indexed.")
            except Exception as e:
                logger.debug(f"Storage channel cache sync notice: {e}")

    async def ensure_channel_synced(self, client, channel_id: Optional[int], limit: int = 500):
        """Ensure recent storage channel records are cached in SQLite for instant local hits."""
        if channel_id and not self.is_channel_synced:
            await self.sync_channel_cache(client, channel_id, limit=limit)

    async def partition_nodes_by_cache(
        self, client, channel_id: Optional[int], nodes: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        """
        Upfront 0-bandwidth batch partitioner with Proposal 2 (Telegram Server Search)
        and Proposal 3 (In-Folder Internal Deduplication & Exact-Size Matching).
        """
        if not nodes:
            return [], []

        missing_nodes = []
        cached_nodes = []

        # --- STEP 1: IN-FOLDER INTERNAL DEDUPLICATION (Proposal 3 Rule 3) ---
        # Eliminate identical size duplicate copies within the same incoming share
        seen_sizes = {}
        deduped_nodes = []
        for node in nodes:
            sz = node.get("size", 0)
            if sz > 0 and sz in seen_sizes:
                logger.info(
                    f"In-folder duplicate detected: '{node.get('name')}' shares size {sz} bytes with '{seen_sizes[sz]}' (Skipping duplicate download)"
                )
                cached_nodes.append(node)
                continue
            if sz > 0:
                seen_sizes[sz] = node.get("name", "")
            deduped_nodes.append(node)

        # Warm up local cache with recent messages if channel provided
        if channel_id:
            await self.ensure_channel_synced(client, channel_id, limit=200)

        # --- STEP 2: MULTI-TIER SEARCH AGAINST STORAGE CHANNEL ---
        for node in deduped_nodes:
            h = node.get("h", "")
            name = node.get("name", "")
            size = node.get("size", 0)

            hit = await self.search_channel_media(
                client=client,
                channel_id=channel_id,
                file_key=f"mg_{h}" if h else "",
                file_name=name,
                file_size=size,
            )

            if hit:
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
        Search for a media item in local SQLite or via Telegram Server Deep Search.
        Returns Message object if found, or None.
        """
        if not channel_id or not client:
            return None

        hit = await self.search_channel_media(
            client=client,
            channel_id=channel_id,
            file_key=file_key,
            file_name=file_name,
            file_size=file_size,
        )
        if hit:
            c_id, msg_id = hit
            try:
                msg = await client.get_messages(c_id, ids=msg_id)
                if msg and msg.media:
                    return msg
            except Exception as e:
                logger.debug(f"Could not retrieve message {msg_id} from {c_id}: {e}")

        return None


db = Database()


