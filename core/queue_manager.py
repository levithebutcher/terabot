import asyncio
import time
from typing import Dict, Tuple
from config import MAX_CONCURRENT_DOWNLOADS, USER_RATE_LIMIT_SECONDS
from utils.logger import logger


class QueueManager:
    """
    Manages task concurrency and per-user rate limiting for the bot.
    Prevents semaphore drift and prunes stale cooldown tracking.
    """

    def __init__(self, max_concurrent: int = MAX_CONCURRENT_DOWNLOADS, rate_limit_sec: int = USER_RATE_LIMIT_SECONDS):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent
        self.rate_limit_sec = rate_limit_sec
        self.active_users: set[int] = set()
        self.active_workers: set[int] = set()
        self.last_request_time: Dict[int, float] = {}
        self.queued_tasks: int = 0
        self.lock = asyncio.Lock()

    def _prune_rate_limits(self, now: float):
        """Prune rate limit records older than 1 hour to prevent memory growth."""
        if len(self.last_request_time) > 1000:
            threshold = now - 3600
            stale_keys = [uid for uid, t in self.last_request_time.items() if t < threshold]
            for uid in stale_keys:
                self.last_request_time.pop(uid, None)

    async def can_process_user(self, user_id: int) -> Tuple[bool, str]:
        """
        Check if user is allowed to start a new task.
        Enforces cooldown and single-active-task per user.
        """
        async with self.lock:
            now = time.time()
            self._prune_rate_limits(now)

            # Check if user already has an active task
            if user_id in self.active_users:
                return False, "⚠️ You already have an active download in progress. Please wait until it completes."

            # Check rate limit cooldown
            last_time = self.last_request_time.get(user_id, 0.0)
            elapsed = now - last_time
            if elapsed < self.rate_limit_sec:
                remaining = int(self.rate_limit_sec - elapsed)
                return False, f"⏳ Rate limit: please wait {remaining} seconds before sending another link."

            return True, ""

    async def acquire_slot(self, user_id: int) -> int:
        """
        Register user task in queue.
        Returns the queue position (0 if immediately processed, or position number).
        """
        async with self.lock:
            self.active_users.add(user_id)
            self.last_request_time[user_id] = time.time()
            self.queued_tasks += 1
            pos = self.queued_tasks

        return pos

    async def enter_worker(self, user_id: int = 0):
        """Wait for an available concurrency slot in the semaphore and record worker lease."""
        await self.semaphore.acquire()
        if user_id:
            async with self.lock:
                self.active_workers.add(user_id)

    def release_worker(self, user_id: int):
        """
        Release concurrency slot and clean up user active state safely.
        Ensures semaphore.release() is only called if this user actually held a worker slot.
        """
        if user_id in self.active_workers:
            self.active_workers.discard(user_id)
            self.semaphore.release()

        self.active_users.discard(user_id)
        if self.queued_tasks > 0:
            self.queued_tasks -= 1


queue_mgr = QueueManager()
