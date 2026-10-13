import asyncio
import re
import time
from typing import Optional
import aiosqlite
import config
from utils.logger import logger


class Database:
    """
    Dual-Backend Database Manager:
    1. MongoDB Atlas (Cloud) - Shared cross-environment persistence across Railway, Colab, & Local.
    2. SQLite (Local) - Fast zero-config embedded fallback if MongoDB is unreachable or unconfigured.
    """

    def __init__(self, db_path=config.DB_PATH):
        self.db_path = db_path
        self.mongo_client = None
        self.mongo_db = None
        self.use_mongo: bool = False
        self.is_channel_synced: bool = False
        self._sync_lock: Optional[asyncio.Lock] = None

    async def init_db(self):
        """Initialize SQLite tables and connect to MongoDB Atlas if configured."""
        # 1. Initialize local SQLite tables (Fallback & Local Cache)
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
                    delivery_mode TEXT DEFAULT 'channel'
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cached_size ON cached_files(file_size)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cached_name ON cached_files(file_name)")
            await db.commit()
        logger.info(f"Local SQLite database initialized at {self.db_path}")

        # 2. Connect to MongoDB Atlas (Shared persistent cloud store)
        if config.MONGO_URI:
            try:
                import motor.motor_asyncio
                self.mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
                    config.MONGO_URI,
                    serverSelectionTimeoutMS=4000,
                    connectTimeoutMS=4000,
                )
                # Verify cluster connection with a lightweight ping
                await self.mongo_client.admin.command("ping")
                self.mongo_db = self.mongo_client[config.MONGO_DB_NAME]
                self.use_mongo = True

                # Create persistent indexes in MongoDB
                await self.mongo_db.files.create_index("file_key", unique=True, sparse=True)
                await self.mongo_db.files.create_index("file_size")
                await self.mongo_db.files.create_index("file_name")
                await self.mongo_db.users.create_index("user_id", unique=True)
                await self.mongo_db.user_settings.create_index("user_id", unique=True)

                logger.info(
                    f"MongoDB Atlas connected successfully! (DB: '{config.MONGO_DB_NAME}') "
                    "Cross-environment deduplication active across Railway & Colab."
                )
            except Exception as e:
                self.use_mongo = False
                logger.warning(f"MongoDB Atlas unavailable ({e}). Continuing with local SQLite fallback.")
        else:
            logger.info("MONGO_URI not configured. Operating in SQLite standalone mode.")

    async def get_cached_file(self, file_key: str) -> Optional[tuple[int, int]]:
        """Retrieve (channel_id, message_id) for a cached file key from MongoDB or SQLite."""
        if self.use_mongo:
            try:
                doc = await self.mongo_db.files.find_one({"file_key": file_key})
                if doc and "channel_id" in doc and "message_id" in doc:
                    return int(doc["channel_id"]), int(doc["message_id"])
            except Exception as e:
                logger.debug(f"MongoDB get_cached_file notice: {e}")

        # Local SQLite lookup
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
        """Save an uploaded file record to both MongoDB Atlas and local SQLite."""
        now = time.time()

        # Save to MongoDB Atlas
        if self.use_mongo:
            try:
                await self.mongo_db.files.update_one(
                    {"file_key": file_key},
                    {
                        "$set": {
                            "file_key": file_key,
                            "file_name": file_name,
                            "file_size": file_size,
                            "channel_id": channel_id,
                            "message_id": message_id,
                            "updated_at": now,
                        }
                    },
                    upsert=True,
                )
            except Exception as e:
                logger.debug(f"MongoDB save_cached_file notice: {e}")

        # Save to local SQLite
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO cached_files 
                (file_key, file_name, file_size, channel_id, message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (file_key, file_name, file_size, channel_id, message_id, now),
            )
            await db.commit()
        logger.info(f"Cached file '{file_name}' ({file_key}) -> channel: {channel_id}, msg: {message_id}")

    async def record_user(self, user_id: int, username: str, first_name: str):
        """Record or update user activity."""
        now = time.time()
        if self.use_mongo:
            try:
                await self.mongo_db.users.update_one(
                    {"user_id": user_id},
                    {
                        "$set": {
                            "user_id": user_id,
                            "username": username or "",
                            "first_name": first_name or "",
                            "last_seen": now,
                        }
                    },
                    upsert=True,
                )
            except Exception as e:
                logger.debug(f"MongoDB record_user notice: {e}")

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
                (user_id, username or "", first_name or "", now),
            )
            await db.commit()

    async def get_all_users(self) -> list[int]:
        """Get all user IDs for broadcast."""
        if self.use_mongo:
            try:
                cursor = self.mongo_db.users.find({}, {"user_id": 1})
                docs = await cursor.to_list(length=10000)
                if docs:
                    return [int(d["user_id"]) for d in docs if "user_id" in d]
            except Exception as e:
                logger.debug(f"MongoDB get_all_users notice: {e}")

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT user_id FROM users") as cursor:
                rows = await cursor.fetchall()
                return [int(r[0]) for r in rows]

    async def get_user_filter(self, user_id: int) -> str:
        """Get user's default filter mode ('all', 'video', 'photo', 'other')."""
        if self.use_mongo:
            try:
                doc = await self.mongo_db.user_settings.find_one({"user_id": user_id})
                if doc and doc.get("filter_mode"):
                    return str(doc["filter_mode"]).lower()
            except Exception as e:
                logger.debug(f"MongoDB get_user_filter notice: {e}")

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
        """Set user's default filter mode ('all', 'video', 'photo', 'other')."""
        filter_mode = filter_mode.lower()
        if self.use_mongo:
            try:
                await self.mongo_db.user_settings.update_one(
                    {"user_id": user_id},
                    {"$set": {"user_id": user_id, "filter_mode": filter_mode}},
                    upsert=True,
                )
            except Exception as e:
                logger.debug(f"MongoDB set_user_filter notice: {e}")

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
        """
        Get user's delivery mode ('channel' or 'both').
        Default is 'channel' (Silent Vault Mode: files saved only to Storage Channel).
        """
        if self.use_mongo:
            try:
                doc = await self.mongo_db.user_settings.find_one({"user_id": user_id})
                if doc and doc.get("delivery_mode"):
                    return str(doc["delivery_mode"]).lower()
            except Exception as e:
                logger.debug(f"MongoDB get_user_delivery_mode notice: {e}")

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT delivery_mode FROM user_settings WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    return str(row[0]).lower()
        return "channel"

    async def set_user_delivery_mode(self, user_id: int, delivery_mode: str):
        """Set user's delivery mode ('channel' or 'both')."""
        delivery_mode = delivery_mode.lower()
        if delivery_mode not in ["both", "channel"]:
            delivery_mode = "channel"

        if self.use_mongo:
            try:
                await self.mongo_db.user_settings.update_one(
                    {"user_id": user_id},
                    {"$set": {"user_id": user_id, "delivery_mode": delivery_mode}},
                    upsert=True,
                )
            except Exception as e:
                logger.debug(f"MongoDB set_user_delivery_mode notice: {e}")

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
        """Retrieve (channel_id, message_id) if exact file_size exists in MongoDB or SQLite."""
        if file_size <= 0:
            return None

        # Check MongoDB first
        if self.use_mongo:
            try:
                doc = await self.mongo_db.files.find_one({"file_size": file_size})
                if doc and "channel_id" in doc and "message_id" in doc:
                    return int(doc["channel_id"]), int(doc["message_id"])
            except Exception as e:
                logger.debug(f"MongoDB exact size notice: {e}")

        # Check SQLite
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
        """Retrieve (channel_id, message_id) by matching EXACT name & EXACT byte size from MongoDB or SQLite."""
        clean_name = file_name.strip()
        if not clean_name:
            return None

        # Check MongoDB first
        if self.use_mongo:
            try:
                rgx_clean = f"^{re.escape(clean_name)}$"
                query = {"file_name": {"$regex": rgx_clean, "$options": "i"}}
                if file_size > 0:
                    query["file_size"] = file_size
                doc = await self.mongo_db.files.find_one(query)
                if doc and "channel_id" in doc and "message_id" in doc:
                    return int(doc["channel_id"]), int(doc["message_id"])
            except Exception as e:
                logger.debug(f"MongoDB get_cached_file_by_name_size notice: {e}")

        # Check SQLite
        async with aiosqlite.connect(self.db_path) as db:
            if file_size > 0:
                async with db.execute(
                    "SELECT channel_id, message_id FROM cached_files WHERE LOWER(file_name) = ? AND file_size = ?",
                    (clean_name.lower(), file_size),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return int(row[0]), int(row[1])
            else:
                async with db.execute(
                    "SELECT channel_id, message_id FROM cached_files WHERE LOWER(file_name) = ?",
                    (clean_name.lower(),),
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
        Universal Multi-Tier Deduplication:
        Tier 1: Fast Database lookup (MongoDB Atlas / SQLite) (0ms)
        Tier 2: Telegram Server-Side Native Search (Strict 0-byte tolerance fallback)
        """
        if not channel_id:
            return None

        # --- TIER 1: DATABASE LOOKUP (MongoDB Atlas + SQLite) (0ms) ---
        if file_key:
            cached = await self.get_cached_file(file_key)
            if cached:
                return cached

        # Only check by name AND size together (Strict duplicate: exact name + exact bytes)
        if file_name and file_size > 0:
            cached_by_name = await self.get_cached_file_by_name_size(file_name, file_size)
            if cached_by_name:
                return cached_by_name

        # --- TIER 2: TELEGRAM SERVER-SIDE SEARCH (Best-effort fallback) ---
        if not client:
            return None

        try:
            # 2A. Search by unique key hashtag
            if file_key:
                async for msg in client.iter_messages(channel_id, search=f"#{file_key}", limit=2):
                    if msg and msg.media:
                        sz = msg.file.size if getattr(msg, "file", None) else file_size
                        fn = msg.file.name if getattr(msg, "file", None) and msg.file.name else file_name
                        await self.save_cached_file(file_key, fn or "media", sz, channel_id, msg.id)
                        return channel_id, msg.id

            # 2B. Fallback search by filename stem with exact byte size verification
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

                        # Verify EXACT size match (0-byte tolerance!)
                        if file_size > 0 and msg_size == file_size:
                            fk = file_key or f"sz_{file_size}"
                            await self.save_cached_file(fk, file_name, file_size, channel_id, msg.id)
                            return channel_id, msg.id

        except Exception as search_err:
            logger.debug(f"Telegram server-side search notice ({file_name}): {search_err}")

        return None

    async def sync_channel_cache(self, client, channel_id: Optional[int], limit: int = 500, force: bool = False):
        """Index recent Storage Channel media messages into database cache."""
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
                # Fallback: Telegram restricts iter_messages for bot accounts.
                # Use ID batching via get_messages(ids=...) which is 100% supported for bots!
                try:
                    count = 0
                    probe = await client.send_message(channel_id, "🔄 Indexing storage channel...")
                    latest_id = probe.id
                    await client.delete_messages(channel_id, [probe.id])

                    batch_size = 100
                    start_min = max(1, latest_id - limit)
                    for start_id in range(start_min, latest_id + 1, batch_size):
                        batch_ids = list(range(start_id, min(start_id + batch_size, latest_id + 1)))
                        msgs = await client.get_messages(channel_id, ids=batch_ids)

                        for msg in msgs:
                            if not msg or not msg.media:
                                continue
                            caption = msg.text or msg.message or ""
                            f_name = msg.file.name if getattr(msg, "file", None) else ""
                            f_size = msg.file.size if getattr(msg, "file", None) else 0

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

                            if file_key or f_size > 0:
                                fk = file_key or f"msg_{msg.id}"
                                fn = f_name or f"media_{msg.id}"
                                await self.save_cached_file(fk, fn, f_size, channel_id, msg.id)
                                count += 1

                    self.is_channel_synced = True
                    logger.info(f"Storage channel cache synchronized via ID batching: {count} media records indexed.")
                except Exception as id_err:
                    logger.debug(f"Storage channel ID batching notice: {id_err}")

    async def ensure_channel_synced(self, client, channel_id: Optional[int], limit: int = 500):
        """Ensure recent storage channel records are cached for instant local hits."""
        if channel_id and not self.is_channel_synced:
            await self.sync_channel_cache(client, channel_id, limit=limit)

    async def partition_nodes_by_cache(
        self, client, channel_id: Optional[int], nodes: list[dict], progress_callback=None
    ) -> tuple[list[dict], list[dict]]:
        """
        Fast batch cache partitioner with 0-byte strict tolerance:
        - Exact file_key match OR (Exact file_name AND Exact file_size).
        - Different files with the same name but different sizes will NEVER be skipped.
        - Optional progress_callback(checked_count, total_count, cached_count) for live UI updates.
        """
        if not nodes:
            return [], []

        missing_nodes = []
        cached_nodes = []
        total_nodes = len(nodes)

        # Batch lookup in MongoDB or SQLite (fast ~0.05s)
        for idx, node in enumerate(nodes):
            h = node.get("h", "")
            name = node.get("name", "")
            size = node.get("size", 0)

            hit = None
            if channel_id:
                # 1. Fast DB lookup by immutable Mega node key
                if h:
                    hit = await self.get_cached_file(f"mg_{h}")
                # 2. Strict exact match by name AND exact byte size
                if not hit and name and size > 0:
                    hit = await self.get_cached_file_by_name_size(name, size)

            if hit:
                cached_nodes.append(node)
            else:
                missing_nodes.append(node)

            if progress_callback and (idx % 10 == 0 or idx == total_nodes - 1):
                try:
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(idx + 1, total_nodes, len(cached_nodes))
                    else:
                        progress_callback(idx + 1, total_nodes, len(cached_nodes))
                except Exception:
                    pass

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
        Search for a media item in database cache (MongoDB / SQLite) or via Telegram Server Search.
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
