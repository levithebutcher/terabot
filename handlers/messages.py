import asyncio
from telethon import TelegramClient, events

import config
from core.database import db
from core.task_manager import task_mgr
from pipelines.mega import is_mega_url, process_mega_link, mega_mgr
from pipelines.terabox import process_terabox_link
from strings import ACCESS_DENIED_TEXT
from utils.helpers import extract_urls, is_allowed_user, update_env_file
from utils.jokes import can_send_joke, get_random_joke
from utils.logger import logger


def register_messages(client: TelegramClient) -> None:
    """Register incoming message dispatcher onto the TelegramClient."""

    @client.on(events.NewMessage(incoming=True, func=lambda e: not e.out))
    async def handle_incoming_message(event: events.NewMessage.Event):
        # Ignore outgoing messages sent by the bot itself
        if event.out:
            return

        # If message is posted in a Channel where the bot is Admin:
        if event.is_channel:
            # Only allow auto-configuration if channel is not set and message is from an authorized admin
            if not config.PRIVATE_CHAT_ID and event.sender_id in config.ADMIN_IDS:
                config.PRIVATE_CHAT_ID = event.chat_id
                update_env_file("PRIVATE_CHAT_ID", str(event.chat_id))
                logger.info(f"Admin auto-detected storage channel ID: {event.chat_id}")
                asyncio.create_task(db.sync_channel_cache(client, event.chat_id))
                if config.ADMIN_IDS:
                    try:
                        await client.send_message(
                            config.ADMIN_IDS[0],
                            f"✅ **Storage Channel Auto-Connected!**\n\n"
                            f"🆔 **Channel ID**: `{event.chat_id}`\n\n"
                            f"🚀 Ab se saari videos is channel mein cache hongi aur instant forward hongi!",
                        )
                    except Exception as notify_err:
                        logger.warning(f"Failed to notify admin of channel auto-detection: {notify_err}")
            return

        # Only process private 1-on-1 messages for downloads
        if not event.is_private:
            return

        # Ignore messages sent by other bots
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return

        # Ignore slash commands (they have their own specific handlers in commands.py)
        if event.raw_text.startswith("/"):
            return

        user_id = event.sender_id
        await db.record_user(user_id, getattr(sender, "username", ""), getattr(sender, "first_name", ""))

        if not is_allowed_user(user_id):
            logger.warning(f"Unauthorized access attempt from user {user_id}")
            await event.reply(ACCESS_DENIED_TEXT)
            return

        urls = extract_urls(event.raw_text)
        if not urls:
            # Check if admin forwarded a post from a storage channel to configure PRIVATE_CHAT_ID
            if user_id in config.ADMIN_IDS and event.fwd_from:
                fwd_channel_id = getattr(event.fwd_from, "channel_id", None)
                if not fwd_channel_id and getattr(event.fwd_from, "from_id", None):
                    fwd_channel_id = getattr(event.fwd_from.from_id, "channel_id", None)
                if fwd_channel_id:
                    full_chat_id = int(f"-100{fwd_channel_id}") if not str(fwd_channel_id).startswith("-100") else int(fwd_channel_id)
                    config.PRIVATE_CHAT_ID = full_chat_id
                    update_env_file("PRIVATE_CHAT_ID", str(full_chat_id))
                    asyncio.create_task(db.sync_channel_cache(client, full_chat_id))
                    await event.reply(
                        f"✅ **Storage Channel Successfully Connected!**\n\n"
                        f"🆔 **Channel ID**: `{full_chat_id}`\n\n"
                        f"🚀 Ab se saari videos is channel mein upload hokar instant forward hongi, "
                        f"aur dubara aane par **0-second mein instant deliver** hongi!"
                    )
                    return

            # Check if user is replying to Mega folder search prompt
            if mega_mgr.has_awaiting_search(user_id):
                await mega_mgr.handle_search_input(event)
                return

            # User sent normal chat banter without any link!
            if can_send_joke(user_id):
                joke = get_random_joke()
                reply_text = (
                    f"😜 **Lo pehle ek mast joke suno:**\n\n"
                    f"{joke}\n\n"
                    f"💡 _Ab timepass band karo aur koi TeraBox ya Mega link drop karo download ke liye!_ 📥"
                )
                try:
                    await event.reply(reply_text)
                except Exception:
                    pass
            return

        # Process first found URL
        target_url = urls[0]

        # Check for inline filter cues in the message text
        raw_lower = event.raw_text.lower()
        words = raw_lower.split()
        inline_filter = None
        if any(w in words for w in ["video", "videos", "vid", "vids", "-v", "--video"]):
            inline_filter = "video"
        elif any(w in words for w in ["photo", "photos", "image", "images", "img", "pic", "pics", "-p", "--photo"]):
            inline_filter = "photo"
        elif any(w in words for w in ["other", "others", "-o", "--other", "doc", "docs", "zip"]):
            inline_filter = "other"
        elif any(w in words for w in ["all", "-a", "--all"]):
            inline_filter = "all"

        # Use inline filter if specified; otherwise load user's persistent default
        has_explicit_inline_filter = inline_filter is not None
        active_filter = inline_filter if inline_filter else await db.get_user_filter(user_id)

        # Mega Link Dispatcher (handles files, folders, and legacy mega links)
        if is_mega_url(target_url):
            logger.info(f"Routing Mega link from user {user_id} [Filter: {active_filter}]: {target_url[:50]}...")
            task = asyncio.create_task(
                process_mega_link(
                    event=event,
                    url=target_url,
                    filter_mode=active_filter,
                    has_explicit_inline_filter=has_explicit_inline_filter,
                )
            )
            task_mgr.register_task(user_id, task)
            return

        # TeraBox Link Dispatcher
        logger.info(f"Received download request from user {user_id} [Filter: {active_filter}]: {target_url[:50]}...")
        task = asyncio.create_task(
            process_terabox_link(
                event=event,
                url=target_url,
                filter_mode=active_filter,
                has_explicit_inline_filter=has_explicit_inline_filter,
            )
        )
        task_mgr.register_task(user_id, task)
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            task_mgr.remove_task(user_id, task)
