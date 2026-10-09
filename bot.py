import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.patched import Message

import config
from core.database import db
from core.downloader import TeraBoxDownloader, DownloadError
from core.queue_manager import queue_mgr
from core.resolver import (
    TeraBoxResolver,
    CookieExpiredError,
    ShareLinkExpiredError,
    CloudflareBlockedError,
    ResolverError,
    TeraFile,
)
from core.uploader import TelethonUploader
from utils.helpers import extract_urls, format_bytes, format_duration
from utils.logger import logger

# Initialize Telethon client with automatic reconnect & infinite retries
client = TelegramClient(
    session="terabot_session",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    auto_reconnect=True,
    connection_retries=None,
    retry_delay=2,
    request_retries=5,
)
uploader = TelethonUploader(client)


def is_allowed_user(user_id: int) -> bool:
    """Check if user is authorized to use the bot."""
    if not config.ALLOWED_USERS:
        return True
    return user_id in config.ALLOWED_USERS or user_id in config.ADMIN_IDS


def cleanup_leftover_parts():
    """Remove leftover .part files in DOWNLOAD_DIR on startup/shutdown."""
    if config.DOWNLOAD_DIR.exists():
        for p in config.DOWNLOAD_DIR.glob("*.part"):
            try:
                p.unlink()
                logger.info(f"Cleaned up orphaned part file: {p.name}")
            except Exception as e:
                logger.debug(f"Could not remove {p.name}: {e}")


async def notify_admins(text: str):
    """Send an alert message to all configured ADMIN_IDS."""
    for admin_id in config.ADMIN_IDS:
        try:
            await client.send_message(admin_id, text)
        except Exception as e:
            logger.warning(f"Failed to alert admin {admin_id}: {e}")


def render_progress_text(action: str, filename: str, current: int, total: int, speed: float) -> str:
    """Render a progress status string for Telegram message edits."""
    bar_width = 12
    if total > 0:
        percent = (current / total) * 100.0
        filled = int(bar_width * current // total)
        bar = "▰" * filled + "▱" * (bar_width - filled)
        remaining = total - current
        eta = remaining / speed if speed > 0 else 0
        eta_str = format_duration(eta)
    else:
        percent = 0.0
        bar = "▱" * bar_width
        eta_str = "--:--"

    speed_mb = speed / (1024 * 1024)
    return (
        f"**{action}**: `{filename}`\n\n"
        f"[{bar}] **{percent:.1f}%**\n"
        f"⚡ **Speed**: `{speed_mb:.2f} MB/s`\n"
        f"📦 **Processed**: `{format_bytes(current)}` / `{format_bytes(total)}`\n"
        f"⏱ **ETA**: `{eta_str}`"
    )


# ---------------- COMMAND HANDLERS ---------------- #

@client.on(events.NewMessage(pattern=r"^/start$", incoming=True, func=lambda e: not e.out))
async def handle_start(event: events.NewMessage.Event):
    sender = await event.get_sender()
    if not sender or getattr(sender, "bot", False):
        return
    user_id = event.sender_id
    await db.record_user(user_id, getattr(sender, "username", ""), getattr(sender, "first_name", ""))

    if not is_allowed_user(user_id):
        await event.reply(
            "🔒 **Access Restricted**\n\n"
            "This is a private personal TeraBox downloader bot. "
            "All downloads share the owner's private credentials.\n\n"
            "You are not on the authorized user list."
        )
        return

    welcome_text = (
        f"👋 **Welcome, {getattr(sender, 'first_name', 'User')}!**\n\n"
        "I am your high-speed **TeraBox Downloader Bot**.\n\n"
        "✨ **Features**:\n"
        "• Native direct link resolution (no dead workers)\n"
        f"• Multi-stream parallel Range engine (`{config.DOWNLOAD_STREAMS}` connections)\n"
        f"• Support up to `{config.MAX_FILE_SIZE_MB}` MB files\n"
        "• Streaming video playback with fast seek & thumbnails\n"
        f"• Instant caching ({'Enabled' if config.PRIVATE_CHAT_ID else 'Direct mode'})\n\n"
        "📥 Simply **send or forward any TeraBox link** to begin downloading!"
    )
    await event.reply(welcome_text)


@client.on(events.NewMessage(pattern=r"^/help$", incoming=True, func=lambda e: not e.out))
async def handle_help(event: events.NewMessage.Event):
    sender = await event.get_sender()
    if not sender or getattr(sender, "bot", False):
        return
    user_id = event.sender_id
    if not is_allowed_user(user_id):
        return

    help_text = (
        "📖 **TeraBox Bot Commands & Usage**\n\n"
        "• `/start` - Start the bot & view system status\n"
        "• `/ping` - Check bot responsiveness & latency\n"
        "• `/help` - Show this help menu\n"
        "• `/broadcast <msg>` - (Admin only) Broadcast message to all users\n\n"
        "🌐 **Supported Links**:\n"
        "`terabox.com`, `terabox.app`, `1024tera.com`, `4funbox.com`, `mirrobox.com`, `nephobox.com`, `tibibox.com` and all mirror domains.\n\n"
        f"⚙️ **Limits**: Files up to `{config.MAX_FILE_SIZE_MB}` MB. Files larger than this will be delivered as direct browser links."
    )
    await event.reply(help_text)


@client.on(events.NewMessage(pattern=r"^/ping$", incoming=True, func=lambda e: not e.out))
async def handle_ping(event: events.NewMessage.Event):
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


# ---------------- LINK DOWNLOAD PROCESSOR ---------------- #

async def download_thumbnail(thumb_url: str, output_path: Path) -> Optional[Path]:
    """Download thumbnail image to local file if available."""
    if not thumb_url:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession() as session:
            async with session.get(thumb_url, timeout=timeout) as resp:
                if resp.status == 200:
                    with open(output_path, "wb") as f:
                        f.write(await resp.read())
                    return output_path
    except Exception:
        pass
    return None


async def process_terabox_link(event: events.NewMessage.Event, url: str):
    """Core pipeline for link resolution, queueing, downloading, and uploading."""
    user_id = event.sender_id

    # Rate limiting & active check (Admins bypass rate limits)
    if user_id not in config.ADMIN_IDS:
        can_proceed, reason = await queue_mgr.can_process_user(user_id)
        if not can_proceed:
            await event.reply(reason)
            return

    queue_pos = await queue_mgr.acquire_slot(user_id)
    status_msg: Optional[Message] = None

    try:
        status_msg = await event.reply("🔍 **Resolving TeraBox link...**")
        if queue_pos > 1:
            await status_msg.edit(f"⏳ **Queue Position #{queue_pos}**\nWaiting for an available worker slot...")

        # Concurrency slot acquisition
        await queue_mgr.enter_worker()

        # Step 1: Resolve Link
        resolver = TeraBoxResolver()
        try:
            files = await resolver.resolve(url)
        except CookieExpiredError as e:
            logger.error(f"[ERR-COOKIE] Cookie expired or verify_v2 triggered: {e}", exc_info=True)
            await status_msg.edit(
                "❌ **Authentication Failed** `[ERR-COOKIE]`\n\n"
                "The bot's TeraBox session cookie has expired or triggered a verification challenge.\n"
                "Please update `TERABOX_COOKIE` in Render Environment."
            )
            # Only notify other admins if the person who sent the link is NOT an admin
            if user_id not in config.ADMIN_IDS:
                await notify_admins(
                    "🚨 **ADMIN ALERT: TeraBox Cookie Expired!**\n\n"
                    "The configured `TERABOX_COOKIE` has expired or returned `need verify_v2`.\n"
                    "Please extract a fresh `ndus` cookie from your browser and update Render Environment."
                )
            return
        except ShareLinkExpiredError as e:
            logger.warning(f"[ERR-SHARE] Share link inaccessible: {e}")
            await status_msg.edit(
                f"❌ **Link Not Accessible** `[ERR-SHARE]`\n\n{str(e)}"
            )
            return
        except CloudflareBlockedError as e:
            logger.warning(f"[ERR-CF] Cloudflare blocked: {e}")
            await status_msg.edit(
                "❌ **CDN Challenge** `[ERR-CF]`\n\n"
                "Cloudflare temporarily blocked the request. Please retry in 1–2 minutes."
            )
            return
        except ResolverError as e:
            logger.error(f"[ERR-RESOLVE] Resolver error: {e}", exc_info=True)
            await status_msg.edit(f"❌ **Resolver Error** `[ERR-RESOLVE]`\n\n`{str(e)}`")
            return
        except Exception as e:
            logger.error(f"[ERR-UNEXPECTED] Unexpected resolver error: {e}", exc_info=True)
            await status_msg.edit(f"❌ **Unexpected Error** `[ERR-UNEXPECTED]`\n\n`{str(e)}`")
            return

        total_files = len(files)
        
        MAX_FILES_PER_LINK = 100
        if total_files > MAX_FILES_PER_LINK:
            await status_msg.edit(
                f"❌ **Too many files!**\n\n"
                f"This folder contains **{total_files}** files.\n"
                f"To prevent spam and server crashes, this bot can only process up to **{MAX_FILES_PER_LINK}** files per link.\n\n"
                f"Please open the original link in your browser to view or download them."
            )
            return

        is_multi_file = total_files > 1

        if is_multi_file:
            await status_msg.edit(
                f"📂 **Discovered {total_files} files** in this share.\n"
                "⚠️ *Note: Folder and multi-file support is experimental.*\n"
                "Beginning sequential download..."
            )
            await asyncio.sleep(2)

        # Step 2: Process each file sequentially
        for idx, file_obj in enumerate(files, 1):
            file_prefix = f"[{idx}/{total_files}] " if is_multi_file else ""

            # Check File Size Limit
            max_size_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
            if file_obj.size > max_size_bytes:
                oversize_msg = (
                    f"📦 **{file_obj.file_name}**\n\n"
                    f"• **Size**: `{file_obj.size_readable}`\n"
                    f"• **Direct Bot Upload Limit**: `{config.MAX_FILE_SIZE_MB}` MB\n\n"
                    f"⚡ **Direct High-Speed Download Link**:\n"
                    f"[👉 Click Here to Download Directly]({file_obj.dlink})\n\n"
                    f"_Tip: Tap the link above to download at full speed in Chrome, ADM, or IDM._"
                )
                await event.reply(oversize_msg)
                continue

            # Cache Check (if PRIVATE_CHAT_ID is set)
            file_key = file_obj.fs_id or f"{file_obj.file_name}_{file_obj.size}"
            if config.PRIVATE_CHAT_ID:
                cached = await db.get_cached_file(file_key)
                if cached:
                    channel_id, cached_msg_id = cached
                    try:
                        await status_msg.edit(f"⚡ {file_prefix}**Retrieved from instant cache!** Forwarding media...")
                        await client.forward_messages(
                            entity=event.chat_id,
                            messages=cached_msg_id,
                            from_peer=channel_id,
                        )
                        continue
                    except Exception as fwd_err:
                        logger.warning(f"Failed to forward cached message {cached_msg_id}: {fwd_err}")

            # Throttled progress updater
            last_edit_time = 0.0

            async def download_progress(current: int, total: int, speed: float):
                nonlocal last_edit_time
                now = time.monotonic()
                if now - last_edit_time >= 3.0 or current == total:
                    last_edit_time = now
                    txt = render_progress_text(
                        action=f"📥 {file_prefix}Downloading",
                        filename=file_obj.file_name,
                        current=current,
                        total=total,
                        speed=speed,
                    )
                    try:
                        await status_msg.edit(txt)
                    except FloodWaitError as fw:
                        await asyncio.sleep(fw.seconds)
                    except Exception:
                        pass

            # Download File
            downloader = TeraBoxDownloader(connections=config.DOWNLOAD_STREAMS)
            try:
                downloaded_file = await downloader.download_file(
                    dlink=file_obj.dlink,
                    filename=file_obj.file_name,
                    expected_size=file_obj.size,
                    progress_callback=download_progress,
                )
            except DownloadError as e:
                await status_msg.edit(f"❌ {file_prefix}**Download Failed**: `{str(e)}`")
                continue

            # Download remote thumbnail if available
            thumb_path = None
            if file_obj.thumb:
                temp_thumb = config.DOWNLOAD_DIR / f"{downloaded_file.stem}_remote_thumb.jpg"
                thumb_path = await download_thumbnail(file_obj.thumb, temp_thumb)

            # Upload File
            last_upload_edit = 0.0

            async def upload_progress(current: int, total: int, speed: float):
                nonlocal last_upload_edit
                now = time.monotonic()
                if now - last_upload_edit >= 3.5 or current == total:
                    last_upload_edit = now
                    txt = render_progress_text(
                        action=f"📤 {file_prefix}Uploading to Telegram",
                        filename=file_obj.file_name,
                        current=current,
                        total=total,
                        speed=speed,
                    )
                    try:
                        await status_msg.edit(txt)
                    except FloodWaitError as fw:
                        await asyncio.sleep(fw.seconds)
                    except Exception:
                        pass

            caption = (
                f"✨ **{file_obj.file_name}**\n\n"
                f"💾 **Size**: `{file_obj.size_readable}`\n"
            )
            if file_obj.duration > 0:
                caption += f"⏱ **Duration**: `{format_duration(file_obj.duration)}`\n"

            try:
                # If PRIVATE_CHAT_ID is enabled, upload to storage channel and forward to user
                if config.PRIVATE_CHAT_ID:
                    channel_msg = await uploader.upload_media(
                        chat_id=config.PRIVATE_CHAT_ID,
                        file_path=downloaded_file,
                        caption=caption,
                        thumb_path=thumb_path,
                        progress_callback=upload_progress,
                    )
                    # Save to SQLite cache
                    await db.save_cached_file(
                        file_key=file_key,
                        file_name=file_obj.file_name,
                        file_size=file_obj.size,
                        channel_id=config.PRIVATE_CHAT_ID,
                        message_id=channel_msg.id,
                    )
                    # Forward to user
                    await client.forward_messages(
                        entity=event.chat_id,
                        messages=channel_msg.id,
                        from_peer=config.PRIVATE_CHAT_ID,
                    )
                else:
                    # Upload directly to user
                    await uploader.upload_media(
                        chat_id=event.chat_id,
                        file_path=downloaded_file,
                        caption=caption,
                        thumb_path=thumb_path,
                        progress_callback=upload_progress,
                    )

            except Exception as up_err:
                logger.error(f"[ERR-UPLOAD] Upload failed for {file_obj.file_name}: {up_err}", exc_info=True)
                await event.reply(f"❌ {file_prefix}**Upload Failed** `[ERR-UPLOAD]`\n\n`{str(up_err)}`")
            finally:
                # Clean up local file and thumb
                try:
                    if downloaded_file.exists():
                        downloaded_file.unlink()
                    if thumb_path and thumb_path.exists():
                        thumb_path.unlink()
                except Exception as clean_err:
                    logger.debug(f"Cleanup error: {clean_err}")

        # Finish up
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        if status_msg:
            try:
                await status_msg.edit(f"❌ **Error occurred**: `{str(e)}`")
            except Exception:
                pass
    finally:
        queue_mgr.release_worker(user_id)


# ---------------- MESSAGE DISPATCHER ---------------- #

active_tasks: dict[int, asyncio.Task] = {}

@client.on(events.NewMessage(pattern=r"(?i)^/(cancel|stop)$"))
async def handle_cancel(event: events.NewMessage.Event):
    user_id = event.sender_id
    if user_id in active_tasks:
        task = active_tasks.pop(user_id)
        task.cancel()
        await event.reply("✅ **Download/Upload stopped.**\nAll ongoing operations have been aborted.")
    else:
        await event.reply("❌ **No active tasks to stop.**")

@client.on(events.NewMessage(incoming=True, func=lambda e: not e.out))
async def handle_incoming_message(event: events.NewMessage.Event):
    # Ignore outgoing messages sent by the bot itself
    if event.out:
        return

    # Ignore messages sent by other bots
    sender = await event.get_sender()
    if not sender or getattr(sender, "bot", False):
        return

    # Ignore slash commands (they have their own specific handlers)
    if event.raw_text.startswith("/"):
        return

    urls = extract_urls(event.raw_text)
    if not urls:
        # Ignore normal chat messages to prevent any message ping-pong loops
        return

    user_id = event.sender_id
    await db.record_user(user_id, getattr(sender, "username", ""), getattr(sender, "first_name", ""))

    if not is_allowed_user(user_id):
        logger.warning(f"Unauthorized access attempt from user {user_id}")
        await event.reply(
            "🔒 **Access Restricted**\n\n"
            "This is a private personal TeraBox downloader bot.\n"
            "You are not authorized to download files with this bot."
        )
        return

    # Process first found URL
    target_url = urls[0]
    logger.info(f"Received download request from user {user_id}: {target_url[:50]}...")
    
    task = asyncio.create_task(process_terabox_link(event, target_url))
    active_tasks[user_id] = task
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if active_tasks.get(user_id) == task:
            del active_tasks[user_id]


async def start_dummy_web_server():
    """Start an optional dummy HTTP health server for cloud platforms (Render, Koyeb, HF)."""
    port_str = os.getenv("PORT")
    if not port_str:
        return
    try:
        from aiohttp import web
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text="TeraBox Bot is running!"))
        app.router.add_get("/health", lambda r: web.Response(text="OK"))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(port_str))
        await site.start()
        logger.info(f"Cloud health-check web server started on port {port_str}")
    except Exception as e:
        logger.warning(f"Could not start dummy web server on port {port_str}: {e}")


# ---------------- STARTUP & SHUTDOWN ---------------- #

async def main():
    cleanup_leftover_parts()
    await db.init_db()
    await start_dummy_web_server()

    logger.info("Starting Telegram Bot via Telethon...")
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is missing in .env! Exiting.")
        sys.exit(1)
    if not config.API_ID or not config.API_HASH:
        logger.error("API_ID or API_HASH is missing in .env! Exiting.")
        sys.exit(1)

    while True:
        try:
            if not client.is_connected():
                await client.start(bot_token=config.BOT_TOKEN)
                me = await client.get_me()
                logger.info(f"Bot successfully logged in as @{me.username} (ID: {me.id})")
                print(f"\n[+] Bot is running as @{me.username} (ID: {me.id})")
                print("[+] Listening for incoming messages. Press Ctrl+C to stop.\n")

            await client.run_until_disconnected()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Connection interrupted ({e}). Reconnecting in 3 seconds...", exc_info=True)
            await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutting down gracefully...")
        cleanup_leftover_parts()
