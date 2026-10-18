from telethon import TelegramClient, events

from core.database import db
from core.mega_downloader import mega_mgr
from core.task_manager import task_mgr
from keyboards import get_delivery_buttons
from strings import DELIVERY_CHANNEL_ACTIVE, DELIVERY_BOTH_ACTIVE
from utils.helpers import is_allowed_user


def _to_str(val) -> str:
    return val.decode("utf-8") if isinstance(val, bytes) else str(val)


def register_callbacks(client: TelegramClient) -> None:
    """Register all inline button CallbackQuery handlers onto the TelegramClient."""

    @client.on(events.CallbackQuery(pattern=r"^set_deliv:(channel|both)$"))
    async def handle_delivery_callback(event: events.CallbackQuery.Event):
        user_id = event.sender_id
        if not is_allowed_user(user_id):
            await event.answer("⛔ Access denied.", alert=True)
            return
        mode = _to_str(event.pattern_match.group(1))
        await db.set_user_delivery_mode(user_id, mode)
        await event.answer(f"✅ Delivery Mode: {mode.upper()}", alert=False)

        msg = DELIVERY_CHANNEL_ACTIVE if mode == "channel" else DELIVERY_BOTH_ACTIVE
        buttons = get_delivery_buttons(mode)
        try:
            await event.edit(msg, buttons=buttons)
        except Exception:
            pass

    @client.on(events.CallbackQuery(pattern=r"^stop:(\d+)$"))
    async def handle_stop_callback(event: events.CallbackQuery.Event):
        target_user_id = int(_to_str(event.pattern_match.group(1)))
        if event.sender_id != target_user_id:
            await event.answer("⛔ You cannot stop another user's download!", alert=True)
            return

        if task_mgr.cancel_task(target_user_id):
            await event.answer("🛑 Task Aborted!", alert=False)
            try:
                await event.edit("🛑 **Task stopped by user.**\nAll ongoing operations aborted.", buttons=None)
            except Exception:
                pass
        else:
            await event.answer("ℹ️ No active task running.", alert=False)

    @client.on(events.CallbackQuery(pattern=r"^act:(video|photo|other|all|cancel):([a-f0-9]+)$"))
    async def handle_action_callback(event: events.CallbackQuery.Event):
        action = _to_str(event.pattern_match.group(1))
        session_id = _to_str(event.pattern_match.group(2))

        prompt_info = task_mgr.get_prompt(session_id)
        if not prompt_info:
            await event.answer("⚠️ Session expired! Please resend the link.", alert=True)
            return

        owner_id, future = prompt_info
        if event.sender_id != owner_id:
            await event.answer("⛔ This choice is for another user!", alert=True)
            return

        success, status = task_mgr.resolve_prompt(session_id, event.sender_id, action)
        if success:
            if action == "cancel":
                await event.answer("❌ Cancelled.", alert=False)
            else:
                label_map = {
                    "video": "Videos Only",
                    "photo": "Photos Only",
                    "other": "Other Files Only",
                    "all": "All Files",
                }
                label = label_map.get(action, action.upper())
                await event.answer(f"🚀 Selected {label}!", alert=False)

    @client.on(events.CallbackQuery(pattern=r"^m_"))
    async def handle_mega_explorer_callback(event: events.CallbackQuery.Event):
        await mega_mgr.handle_callback(event, client, task_mgr.active_tasks)
