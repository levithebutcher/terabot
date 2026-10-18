import asyncio
import time
from telethon import TelegramClient, events

import config
from core.database import db
from core.task_manager import task_mgr
from keyboards import get_delivery_buttons
from strings import (
    get_welcome_text,
    get_help_text,
    ACCESS_DENIED_TEXT,
    DELIVERY_BOTH_ACTIVE,
    DELIVERY_CHANNEL_ACTIVE,
    get_delivery_menu_text,
)
from utils.helpers import is_allowed_user, update_env_file
from utils.logger import logger


def register_commands(client: TelegramClient) -> None:
    """Register all slash command handlers onto the TelegramClient."""

    @client.on(events.NewMessage(pattern=r"^/start$", incoming=True, func=lambda e: not e.out))
    async def handle_start(event: events.NewMessage.Event):
        if not event.is_private:
            return
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        user_id = event.sender_id
        await db.record_user(user_id, getattr(sender, "username", ""), getattr(sender, "first_name", ""))

        if not is_allowed_user(user_id):
            await event.reply(ACCESS_DENIED_TEXT)
            return

        welcome_text = get_welcome_text(getattr(sender, "first_name", "User"))
        await event.reply(welcome_text)

    @client.on(events.NewMessage(pattern=r"^/help$", incoming=True, func=lambda e: not e.out))
    async def handle_help(event: events.NewMessage.Event):
        if not event.is_private:
            return
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        user_id = event.sender_id
        if not is_allowed_user(user_id):
            return

        await event.reply(get_help_text())

    @client.on(events.NewMessage(pattern=r"^/ping$", incoming=True, func=lambda e: not e.out))
    async def handle_ping(event: events.NewMessage.Event):
        if not event.is_private:
            return
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        user_id = event.sender_id
        if not is_allowed_user(user_id):
            return

        t0 = time.monotonic()
        msg = await event.reply("🏓 **Pinging...**")
        latency_ms = int((time.monotonic() - t0) * 1000)
        await msg.edit(f"🏓 **Pong!** Latency: `{latency_ms} ms`")

    @client.on(events.NewMessage(pattern=r"^/(?:filter|only)(?:\s+(.*))?$", incoming=True, func=lambda e: not e.out))
    async def handle_filter(event: events.NewMessage.Event):
        if not event.is_private:
            return
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        user_id = event.sender_id
        if not is_allowed_user(user_id):
            return

        arg = (event.pattern_match.group(1) or "").strip().lower()

        if arg in ["video", "videos", "vid", "vids"]:
            await db.set_user_filter(user_id, "video")
            await event.reply(
                "🎬 **Filter Updated: Videos Only**\n\n"
                "From now on, the bot will **only download videos** (.mp4, .mkv, etc.) and skip all photos/thumbnails in folders.\n\n"
                "💡 _To reset, type `/filter all`_"
            )
        elif arg in ["photo", "photos", "image", "images", "img", "pic", "pics"]:
            await db.set_user_filter(user_id, "photo")
            await event.reply(
                "🖼️ **Filter Updated: Photos Only**\n\n"
                "From now on, the bot will **only download photos/images** and skip all video files.\n\n"
                "💡 _To reset, type `/filter all`_"
            )
        elif arg in ["other", "others", "doc", "docs", "zip"]:
            await db.set_user_filter(user_id, "other")
            await event.reply(
                "📄 **Filter Updated: Other Files Only**\n\n"
                "From now on, the bot will **only download other files** (archives, text, audio, docs) and skip photos and videos.\n\n"
                "💡 _To reset, type `/filter all`_"
            )
        elif arg in ["all", "off", "reset", "none"]:
            await db.set_user_filter(user_id, "all")
            await event.reply(
                "📁 **Filter Reset: All Files**\n\n"
                "The bot will now download **all files** without filtering."
            )
        else:
            current = await db.get_user_filter(user_id)
            current_label = {
                "video": "🎬 Videos Only",
                "photo": "🖼️ Photos Only",
                "other": "📄 Other Files Only",
                "all": "📁 All Files (No Filter)",
            }.get(current, "📁 All Files")
            await event.reply(
                f"🎯 **Current Filter Mode**: `{current_label}`\n\n"
                "**How to change:**\n"
                "• `/filter video` - Download only videos (.mp4, .mkv...)\n"
                "• `/filter photo` - Download only photos (.jpg, .png...)\n"
                "• `/filter other` - Download only other files (zip, txt, audio...)\n"
                "• `/filter all` - Download all files\n\n"
                "💡 **Inline Shortcut:**\n"
                "You can also attach it directly with any link:\n"
                "`<terabox_link> video` or `<terabox_link> photo` or `<terabox_link> other`"
            )

    @client.on(events.NewMessage(pattern=r"^/delivery(?:\s+(.*))?$", incoming=True, func=lambda e: not e.out))
    async def handle_delivery(event: events.NewMessage.Event):
        if not event.is_private:
            return
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return
        user_id = event.sender_id
        if not is_allowed_user(user_id):
            return

        arg = (event.pattern_match.group(1) or "").strip().lower()

        if arg in ["both", "dm"]:
            await db.set_user_delivery_mode(user_id, "both")
            buttons = get_delivery_buttons("both")
            await event.reply(DELIVERY_BOTH_ACTIVE, buttons=buttons)
        elif arg in ["channel", "vault", "silent", "channel_only", "default"]:
            await db.set_user_delivery_mode(user_id, "channel")
            buttons = get_delivery_buttons("channel")
            await event.reply(DELIVERY_CHANNEL_ACTIVE, buttons=buttons)
        else:
            current = await db.get_user_delivery_mode(user_id)
            buttons = get_delivery_buttons(current)
            await event.reply(get_delivery_menu_text(current), buttons=buttons)

    @client.on(events.NewMessage(pattern=r"^/broadcast(?:\s+([\s\S]+))?$", incoming=True, func=lambda e: not e.out))
    async def handle_broadcast(event: events.NewMessage.Event):
        user_id = event.sender_id
        if user_id not in config.ADMIN_IDS:
            await event.reply("⛔ This command is restricted to bot administrators.")
            return

        broadcast_text = event.pattern_match.group(1)
        if not broadcast_text:
            await event.reply("⚠️ Usage: `/broadcast <message>`")
            return

        users = await db.get_all_users()
        if not users:
            await event.reply("ℹ️ No registered users in database yet.")
            return

        status_msg = await event.reply(f"📢 Starting broadcast to `{len(users)}` users...")
        sent = 0
        failed = 0

        for uid in users:
            try:
                await client.send_message(uid, broadcast_text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1

        await status_msg.edit(f"✅ **Broadcast complete**\n\n• Sent: `{sent}`\n• Failed: `{failed}`")

    @client.on(events.NewMessage(pattern=r"^/setchannel(?:\s+(-?\d+))?$", incoming=True, func=lambda e: not e.out))
    async def handle_set_channel(event: events.NewMessage.Event):
        if not event.is_private:
            return
        user_id = event.sender_id
        if user_id not in config.ADMIN_IDS:
            return
        channel_arg = event.pattern_match.group(1)
        if not channel_arg:
            await event.reply(
                "⚠️ **Usage**: `/setchannel -100xxxxxxxxxx`\n\n"
                "💡 _Tip: Ya fir apne storage channel se koi bhi message bot ko forward kar do!_"
            )
            return
        try:
            full_chat_id = int(channel_arg.strip())
            config.PRIVATE_CHAT_ID = full_chat_id
            update_env_file("PRIVATE_CHAT_ID", str(full_chat_id))
            await event.reply(
                f"✅ **Storage Channel Successfully Set!**\n\n"
                f"🆔 **Channel ID**: `{full_chat_id}`\n\n"
                f"🚀 Ab se saari videos is channel mein cache hongi aur yahan se auto-forward hongi!"
            )
        except Exception as e:
            await event.reply(f"❌ Error setting channel ID: `{e}`")

    @client.on(events.NewMessage(pattern=r"(?i)^/(cancel|stop)$"))
    async def handle_cancel(event: events.NewMessage.Event):
        if not event.is_private:
            return
        user_id = event.sender_id
        if task_mgr.cancel_task(user_id):
            await event.reply("✅ **Download/Upload stopped.**\nAll ongoing operations have been aborted.")
        else:
            await event.reply("❌ **No active tasks to stop.**")
