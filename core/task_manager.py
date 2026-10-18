import asyncio
from typing import Optional, Tuple, Dict
from utils.logger import logger


class TaskManager:
    """Centralized manager for active background tasks and interactive prompt futures."""

    def __init__(self):
        self.active_tasks: Dict[int, asyncio.Task] = {}
        self.pending_prompts: Dict[str, Tuple[int, asyncio.Future]] = {}

    def register_task(self, user_id: int, task: asyncio.Task) -> None:
        """Register an active task for a user, replacing any previous task."""
        if user_id in self.active_tasks:
            prev = self.active_tasks[user_id]
            if not prev.done():
                prev.cancel()
        self.active_tasks[user_id] = task
        task.add_done_callback(lambda t: self.remove_task(user_id, t))

    def get_task(self, user_id: int) -> Optional[asyncio.Task]:
        """Get the active task for a user."""
        return self.active_tasks.get(user_id)

    def remove_task(self, user_id: int, task: Optional[asyncio.Task] = None) -> None:
        """Safely remove a task once completed."""
        if user_id in self.active_tasks:
            if task is None or self.active_tasks[user_id] == task:
                del self.active_tasks[user_id]

    def cancel_task(self, user_id: int) -> bool:
        """Cancel an ongoing download/upload task for a user. Return True if task was cancelled."""
        if user_id in self.active_tasks:
            task = self.active_tasks.pop(user_id)
            if not task.done():
                task.cancel()
                logger.info(f"Task for user {user_id} was successfully cancelled.")
                return True
        return False

    def has_active_task(self, user_id: int) -> bool:
        """Check if user has an unfinished active task."""
        task = self.active_tasks.get(user_id)
        return task is not None and not task.done()

    def create_prompt(self, session_id: str, user_id: int) -> asyncio.Future:
        """Create and store an asyncio.Future for an interactive user prompt."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        future = loop.create_future()
        self.pending_prompts[session_id] = (user_id, future)
        return future

    def get_prompt(self, session_id: str) -> Optional[Tuple[int, asyncio.Future]]:
        """Retrieve the user_id and future for a pending interactive prompt."""
        return self.pending_prompts.get(session_id)

    def remove_prompt(self, session_id: str) -> None:
        """Remove a pending prompt after resolution or timeout."""
        self.pending_prompts.pop(session_id, None)

    def resolve_prompt(self, session_id: str, sender_id: int, action: str) -> Tuple[bool, str]:
        """
        Validate and resolve an interactive prompt future.
        Returns (success: bool, message: str).
        """
        prompt_info = self.get_prompt(session_id)
        if not prompt_info:
            return False, "expired"

        owner_id, future = prompt_info
        if sender_id != owner_id:
            return False, "unauthorized"

        if not future.done():
            future.set_result(action)
            return True, "resolved"
        return False, "already_done"


# Global singleton instance
task_mgr = TaskManager()
