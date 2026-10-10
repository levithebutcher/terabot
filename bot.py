import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import re
import uuid

import aiohttp
from telethon import TelegramClient, events, Button
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

# File extension filters
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".ts", ".3gp", ".mpg", ".mpeg"
}
PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".heic", ".heif"
}


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
        "• `/filter [video|photo|all]` - Filter files in folders\n"
        "• `/stop` or `/cancel` - Abort active download/upload\n"
        "• `/ping` - Check bot responsiveness & latency\n"
        "• `/help` - Show this help menu\n"
        "• `/broadcast <msg>` - (Admin only) Broadcast message to all users\n\n"
        "🎯 **Smart Filtering Tips**:\n"
        "• `/filter video` - Download only videos (.mp4, .mkv, etc.)\n"
        "• `/filter photo` - Download only photos (.jpg, .png, etc.)\n"
        "• `/filter all` - Reset to download all files\n"
        "• You can also send a link with `video` or `photo`:\n"
        "  `https://terabox.com/s/... video`\n\n"
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


@client.on(events.NewMessage(pattern=r"^/(?:filter|only)(?:\s+(.*))?$", incoming=True, func=lambda e: not e.out))
async def handle_filter(event: events.NewMessage.Event):
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


def make_stop_btn(user_id: int):
    return [[Button.inline("🛑 Abort Task", data=f"stop:{user_id}")]]


async def process_terabox_link(
    event: events.NewMessage.Event,
    url: str,
    filter_mode: str = "all",
    has_explicit_inline_filter: bool = False,
):
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

        raw_count = len(files)
        video_files = [f for f in files if Path(f.file_name).suffix.lower() in VIDEO_EXTENSIONS]
        photo_files = [f for f in files if Path(f.file_name).suffix.lower() in PHOTO_EXTENSIONS]
        other_files = [f for f in files if f not in video_files and f not in photo_files]
        other_count = len(other_files)

        # Interactive Button Card: Prompt user if folder contains multiple items and no inline filter was specified
        if raw_count > 1 and not has_explicit_inline_filter:
            session_id = uuid.uuid4().hex[:8]
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            pending_prompts[session_id] = (user_id, future)

            buttons = []
            row1 = []
            if len(video_files) > 0:
                row1.append(Button.inline(f"🎬 Only Videos ({len(video_files)})", data=f"act:video:{session_id}"))
            if len(photo_files) > 0:
                row1.append(Button.inline(f"🖼️ Only Photos ({len(photo_files)})", data=f"act:photo:{session_id}"))
            if row1:
                buttons.append(row1)

            row2 = []
            if other_count > 0:
                row2.append(Button.inline(f"📄 Others ({other_count})", data=f"act:other:{session_id}"))
            row2.append(Button.inline(f"📁 Download All ({raw_count})", data=f"act:all:{session_id}"))
            buttons.append(row2)

            buttons.append([Button.inline("❌ Cancel", data=f"act:cancel:{session_id}")])

            msg_text = (
                f"📂 **Folder Discovered**\n\n"
                f"📊 **Total Files**: `{raw_count}`\n"
                f"• 🎬 **Videos**: `{len(video_files)}`\n"
                f"• 🖼️ **Photos**: `{len(photo_files)}`\n"
            )
            if other_count > 0:
                msg_text += f"• 📄 **Other Files**: `{other_count}`\n"
            msg_text += "\n👇 **Aapko kya download karna hai? Choose karo:**"

            await status_msg.edit(msg_text, buttons=buttons)

            try:
                chosen_action = await asyncio.wait_for(future, timeout=300)
            except asyncio.TimeoutError:
                await status_msg.edit("⏱️ **Selection timed out (5 min).** Please resend link if needed.", buttons=None)
                return
            finally:
                pending_prompts.pop(session_id, None)

            if chosen_action == "cancel":
                await status_msg.edit("❌ **Download cancelled by user.**", buttons=None)
                return

            if chosen_action == "video":
                files = video_files
                filter_mode = "video"
            elif chosen_action == "photo":
                files = photo_files
                filter_mode = "photo"
            elif chosen_action == "other":
                files = other_files
                filter_mode = "other"
            else:
                filter_mode = "all"
        else:
            # Inline filter specified or single file
            if filter_mode == "video":
                files = video_files
            elif filter_mode == "photo":
                files = photo_files
            elif filter_mode == "other":
                files = other_files

        total_files = len(files)

        if total_files == 0:
            type_label = {
                "video": "Videos",
                "photo": "Photos",
                "other": "Other Files",
            }.get(filter_mode, "Files")
            await status_msg.edit(
                f"⚠️ **No {type_label} Found!**\n\n"
                f"This share contains **{raw_count} files**, but **0** matched your filter (`{filter_mode.upper()}`).\n\n"
                f"💡 _Tip: Use `/filter all` or add `all` to download all files without filtering._",
                buttons=None,
            )
            return

        is_multi_file = total_files > 1

        if is_multi_file:
            filter_badge = f"\n🎯 **Filter Active**: `{filter_mode.upper()} ONLY` ({total_files} of {raw_count} files selected)" if filter_mode != "all" else ""
            await status_msg.edit(
                f"📂 **Processing {total_files} files** in this share.{filter_badge}\n"
                "⏳ *Beginning smart batch download...*",
                buttons=make_stop_btn(user_id),
            )
            await asyncio.sleep(1.5)

        album_paths = []
        album_captions = []
        album_size = 0
        ALBUM_MAX_SIZE = 100 * 1024 * 1024  # 100 MB max limit per album
        ALBUM_MAX_ITEMS = 10

        async def flush_album():
            nonlocal album_paths, album_captions, album_size
            if not album_paths:
                return
            try:
                await status_msg.edit(
                    f"📤 Uploading Album ({len(album_paths)} items) to Telegram...",
                    buttons=make_stop_btn(user_id),
                )
                if config.PRIVATE_CHAT_ID:
                    msgs = await client.send_file(config.PRIVATE_CHAT_ID, album_paths, caption=album_captions)
                    await client.forward_messages(event.chat_id, msgs)
                else:
                    await client.send_file(event.chat_id, album_paths, caption=album_captions)
            except Exception as e:
                logger.error(f"Album upload failed: {e}")
                
            for p in album_paths:
                try:
                    if p.exists(): p.unlink()
                except:
                    pass
            album_paths.clear()
            album_captions.clear()
            album_size = 0

        # Step 2: Process each file
        for idx, file_obj in enumerate(files, 1):
            file_prefix = f"[{idx}/{total_files}] " if is_multi_file else ""

            # Check Over-size (2GB limit)
            max_size_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
            if file_obj.size > max_size_bytes:
                await flush_album()
                oversize_msg = (
                    f"📄 **{file_obj.file_name}**\n\n"
                    f"💾 **Size**: {file_obj.size_readable}\n"
                    f"🛑 **Direct Bot Upload Limit**: {config.MAX_FILE_SIZE_MB} MB\n\n"
                    f"⬇️ **Direct High-Speed Download Link**:\n"
                    f"[👉 Click Here to Download Directly]({file_obj.dlink})\n\n"
                    f"_Tip: Tap the link above to download at full speed in Chrome, ADM, or IDM._"
                )
                await event.reply(oversize_msg)
                continue

            # If Large file (> 100MB) OR Single File -> Standalone Fast Upload
            if file_obj.size > ALBUM_MAX_SIZE or not is_multi_file:
                await flush_album()
                
                # Standalone download
                last_edit_time = 0.0
                async def download_progress(current: int, total: int, speed: float):
                    nonlocal last_edit_time
                    now = time.monotonic()
                    if now - last_edit_time >= 3.0 or current == total:
                        last_edit_time = now
                        txt = render_progress_text(f"📥 {file_prefix}Downloading", file_obj.file_name, current, total, speed)
                        try:
                            await status_msg.edit(txt, buttons=make_stop_btn(user_id))
                        except Exception:
                            pass

                downloader = TeraBoxDownloader(connections=config.DOWNLOAD_STREAMS)
                try:
                    downloaded_file = await downloader.download_file(
                        dlink=file_obj.dlink, filename=file_obj.file_name,
                        expected_size=file_obj.size, progress_callback=download_progress
                    )
                except DownloadError as e:
                    await status_msg.edit(f"❌ {file_prefix}**Download Failed**: {str(e)}", buttons=None)
                    continue

                # Upload File Standalone
                last_upload_edit = 0.0
                async def upload_progress(current: int, total: int, speed: float):
                    nonlocal last_upload_edit
                    now = time.monotonic()
                    if now - last_upload_edit >= 3.5 or current == total:
                        last_upload_edit = now
                        txt = render_progress_text(f"📤 {file_prefix}Uploading to Telegram", file_obj.file_name, current, total, speed)
                        try:
                            await status_msg.edit(txt, buttons=make_stop_btn(user_id))
                        except Exception:
                            pass

                caption = f"📄 **{file_obj.file_name}**\n\n💾 **Size**: {file_obj.size_readable}\n"
                if file_obj.duration > 0:
                    caption += f"⏱ **Duration**: {format_duration(file_obj.duration)}\n"

                try:
                    if config.PRIVATE_CHAT_ID:
                        channel_msg = await uploader.upload_media(
                            chat_id=config.PRIVATE_CHAT_ID, file_path=downloaded_file,
                            caption=caption, thumb_path=None, progress_callback=upload_progress
                        )
                        await client.forward_messages(event.chat_id, channel_msg)
                    else:
                        await uploader.upload_media(
                            chat_id=event.chat_id, file_path=downloaded_file,
                            caption=caption, thumb_path=None, progress_callback=upload_progress
                        )
                except Exception as up_err:
                    await event.reply(f"❌ {file_prefix}**Upload Failed**\n\n{str(up_err)}")
                finally:
                    if downloaded_file.exists(): downloaded_file.unlink()
                continue
                
            # If Small File -> ALBUM LOGIC
            if album_size + file_obj.size > ALBUM_MAX_SIZE or len(album_paths) >= ALBUM_MAX_ITEMS:
                await flush_album()

            last_edit_time = 0.0
            async def download_progress_album(current: int, total: int, speed: float):
                nonlocal last_edit_time
                now = time.monotonic()
                if now - last_edit_time >= 3.0 or current == total:
                    last_edit_time = now
                    txt = render_progress_text(f"📥 {file_prefix}Downloading (Album Batch)", file_obj.file_name, current, total, speed)
                    try:
                        await status_msg.edit(txt, buttons=make_stop_btn(user_id))
                    except Exception:
                        pass

            # Download small file
            downloader = TeraBoxDownloader(connections=config.DOWNLOAD_STREAMS)
            try:
                downloaded_file = await downloader.download_file(
                    dlink=file_obj.dlink, filename=file_obj.file_name,
                    expected_size=file_obj.size, progress_callback=download_progress_album
                )
                album_paths.append(downloaded_file)
                album_captions.append(f"📄 **{file_obj.file_name}**")
                album_size += file_obj.size
            except DownloadError as e:
                await event.reply(f"❌ {file_prefix}**Download Failed**: {str(e)}")
                continue

        # Flush remaining album files
        await flush_album()

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


# ---------------- MESSAGE DISPATCHER & CALLBACKS ---------------- #

active_tasks: dict[int, asyncio.Task] = {}
pending_prompts: dict[str, tuple[int, asyncio.Future]] = {}

@client.on(events.CallbackQuery(pattern=r"^act:(video|photo|other|all|cancel):([a-f0-9]+)$"))
async def handle_action_callback(event: events.CallbackQuery.Event):
    action = event.pattern_match.group(1).decode("utf-8")
    session_id = event.pattern_match.group(2).decode("utf-8")

    fut_info = pending_prompts.get(session_id)
    if not fut_info:
        await event.answer("⚠️ Session expired! Please resend the link.", alert=True)
        return

    owner_id, future = fut_info
    if event.sender_id != owner_id:
        await event.answer("⛔ This choice is for another user!", alert=True)
        return

    if not future.done():
        future.set_result(action)
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


@client.on(events.CallbackQuery(pattern=r"^stop:(\d+)$"))
async def handle_stop_callback(event: events.CallbackQuery.Event):
    target_user_id = int(event.pattern_match.group(1).decode("utf-8"))
    if event.sender_id != target_user_id:
        await event.answer("⛔ You cannot stop another user's download!", alert=True)
        return

    if target_user_id in active_tasks:
        task = active_tasks.pop(target_user_id)
        task.cancel()
        await event.answer("🛑 Task Aborted!", alert=False)
        try:
            await event.edit("🛑 **Task stopped by user.**\nAll ongoing operations aborted.", buttons=None)
        except Exception:
            pass
    else:
        await event.answer("ℹ️ No active task running.", alert=False)


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

    if "mega.nz" in target_url or "mega.co.nz" in target_url:
        logger.info(f"Routing Mega link from user {user_id}: {target_url[:50]}...")
        from core.mega_downloader import process_mega_link
        task = asyncio.create_task(process_mega_link(event, target_url))
        active_tasks[user_id] = task
        return

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

    logger.info(f"Received download request from user {user_id} [Filter: {active_filter}]: {target_url[:50]}...")
    
    task = asyncio.create_task(
        process_terabox_link(
            event,
            target_url,
            filter_mode=active_filter,
            has_explicit_inline_filter=has_explicit_inline_filter,
        )
    )
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

