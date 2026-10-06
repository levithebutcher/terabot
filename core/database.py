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


db = Database()
