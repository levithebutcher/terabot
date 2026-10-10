import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

from telethon import Button, events

import config
from core.media import generate_video_thumbnail, get_video_metadata, is_photo_file, is_video_file
from core.queue_manager import queue_mgr
from core.uploader import TelethonUploader
from utils.helpers import format_bytes, format_duration
from utils.logger import logger


# Comprehensive regex matching all Mega link variations (modern & legacy)
MEGA_URL_REGEX = re.compile(
    r"https?://(?:www\.)?mega\.(?:nz|co\.nz)/(?:file/|folder/|embed/|#|#!|#F!)[a-zA-Z0-9_\-\~]+(?:[#!][a-zA-Z0-9_\-\~]+)?",
    re.IGNORECASE,
)


def is_mega_url(url: str) -> bool:
    """Return True if the URL is a recognized Mega.nz file or folder link."""
    return bool(MEGA_URL_REGEX.search(url))


def make_stop_btn(user_id: int):
    """Generate inline stop button for progress messages."""
    return [[Button.inline("🛑 Abort Task", data=f"stop:{user_id}")]]


def render_mega_progress(action: str, name: str, current: int, total: int, speed: float) -> str:
    """Render a clean progress bar string matching the bot's visual theme."""
    bar_width = 12
    speed_mb = speed / (1024 * 1024) if speed > 0 else 0.0

    if total > 0:
        percent = (current / total) * 100.0
        filled = int(bar_width * current // total)
        bar = "▰" * filled + "▱" * (bar_width - filled)
        remaining = total - current
        eta = remaining / speed if speed > 0 else 0
        eta_str = format_duration(eta)
        return (
            f"**{action}**: `{name}`\n\n"
            f"[{bar}] **{percent:.1f}%**\n"
            f"⚡ **Speed**: `{speed_mb:.2f} MB/s`\n"
            f"📦 **Processed**: `{format_bytes(current)}` / `{format_bytes(total)}`\n"
            f"⏱ **ETA**: `{eta_str}`"
        )
    else:
        return (
            f"**{action}**: `{name}`\n\n"
            f"⚡ **Speed**: `{speed_mb:.2f} MB/s`\n"
            f"📦 **Downloaded**: `{format_bytes(current)}`\n"
            f"⏳ **Status**: `Streaming high-speed chunks from Mega...`"
        )


def detect_mega_engine() -> Tuple[Optional[str], Optional[list[str]]]:
    """
    Detect available Mega CLI download engine on the host system.
    Returns (engine_name, base_cmd_list) or (None, None).
    """
    # 1. Prefer official MEGAcmd (mega-get)
    if shutil.which("mega-get"):
        return "mega-cmd", ["mega-get", "--ignore-quota-warn"]

    # 2. Fallback to megatools (megadl)
    if shutil.which("megadl"):
        return "megatools", ["megadl"]

    return None, None


async def process_mega_link(
    event: events.NewMessage.Event,
    url: str,
    filter_mode: str = "all",
    has_explicit_inline_filter: bool = False,
    pending_prompts: Optional[dict] = None,
):
    """
    Handle downloading from Mega.nz (file or folder) and uploading to Telegram.
    Includes Smart Folder Filter buttons, Smart Album grouping, live progress,
    cancellation support, quota handling, and guaranteed disk cleanup.
    """
    user_id = event.sender_id
    status_msg = None
    slot_acquired = False
    process = None
    monitor_task = None
    stop_monitor = asyncio.Event()

    # Rate limiting & concurrency slot check (Admins bypass)
    if user_id not in config.ADMIN_IDS:
        can_proceed, reason = await queue_mgr.can_process_user(user_id)
        if not can_proceed:
            await event.reply(reason)
            return

    queue_pos = await queue_mgr.acquire_slot(user_id)
    slot_acquired = True

    try:
        if queue_pos > 1:
            status_msg = await event.reply(
                f"⏳ **Queued in slot #{queue_pos}**\nYour Mega download will begin shortly...",
                buttons=make_stop_btn(user_id),
            )
        await queue_mgr.enter_worker()

        # Engine detection
        engine_name, base_cmd = detect_mega_engine()
        if not engine_name or not base_cmd:
            err_msg = (
                "❌ **Mega Downloader Engine Not Installed!**\n\n"
                "Host environment does not have `mega-cmd` or `megatools` installed.\n"
                "• **Debian/Ubuntu/Colab setup**:\n"
                "`sudo apt update && sudo apt install -y megatools`\n"
                "or install official MEGAcmd:\n"
                "`wget https://mega.nz/linux/repo/xUbuntu_22.04/amd64/megacmd-xUbuntu_22.04_amd64.deb && sudo apt install ./megacmd-xUbuntu_22.04_amd64.deb`"
            )
            if status_msg:
                await status_msg.edit(err_msg, buttons=None)
            else:
                await event.reply(err_msg)
            return

        # Prepare isolated download directory
        safe_id = f"{int(time.time())}_{user_id}"
        local_dir = config.DOWNLOAD_DIR / f"mega_{safe_id}"
        local_dir.mkdir(parents=True, exist_ok=True)

        if not status_msg:
            status_msg = await event.reply("🔍 **Resolving Mega link...**", buttons=make_stop_btn(user_id))
        else:
            await status_msg.edit("🔍 **Resolving Mega link...**", buttons=make_stop_btn(user_id))

        # Build download command
        if engine_name == "mega-cmd":
            cmd = base_cmd + [url, str(local_dir)]
        else:  # megatools
            cmd = base_cmd + ["--path", str(local_dir), url]

        logger.info(f"Starting Mega download [{engine_name}] for user {user_id}: {url[:60]}")
        await status_msg.edit(f"📥 **Downloading from Mega...**\n`Engine: {engine_name}`", buttons=make_stop_btn(user_id))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Background progress monitor tracking downloaded bytes on disk
        async def monitor_download_progress():
            last_bytes = 0
            last_time = time.monotonic()
            last_edit = 0.0

            while not stop_monitor.is_set():
                await asyncio.sleep(2.0)
                if stop_monitor.is_set():
                    break

                try:
                    total_bytes = sum(f.stat().st_size for f in local_dir.rglob("*") if f.is_file())
                except Exception:
                    total_bytes = 0

                now = time.monotonic()
                dt = now - last_time
                if dt > 0:
                    speed = (total_bytes - last_bytes) / dt
                    last_bytes = total_bytes
                    last_time = now

                    if now - last_edit >= 3.0 and total_bytes > 0:
                        last_edit = now
                        progress_txt = render_mega_progress(
                            action="📥 Downloading from Mega",
                            name=f"Engine: {engine_name}",
                            current=total_bytes,
                            total=0,
                            speed=max(0.0, speed),
                        )
                        try:
                            await status_msg.edit(progress_txt, buttons=make_stop_btn(user_id))
                        except Exception:
                            pass

        monitor_task = asyncio.create_task(monitor_download_progress())

        # Wait for download completion
        stdout, stderr = await process.communicate()
        stop_monitor.set()
        if monitor_task:
            monitor_task.cancel()

        # Check return code & parse errors
        if process.returncode != 0:
            err_output = (stderr.decode().strip() or stdout.decode().strip())
            logger.error(f"Mega download failed (code {process.returncode}): {err_output}")

            if any(term in err_output.lower() for term in ["bandwidth quota", "transfer quota", "error -17", "509"]):
                await status_msg.edit(
                    "❌ **Mega Bandwidth Quota Exceeded!**\n\n"
                    "Mega's free IP transfer quota limit has been reached.\n"
                    "Please wait a few hours for the quota to reset or try again later.",
                    buttons=None,
                )
            elif any(term in err_output.lower() for term in ["decryption error", "key", "invalid key"]):
                await status_msg.edit(
                    "❌ **Mega Decryption Error!**\n\n"
                    "The decryption key in the link is invalid, incomplete, or corrupted.",
                    buttons=None,
                )
            elif "not found" in err_output.lower() or "does not exist" in err_output.lower():
                await status_msg.edit(
                    "❌ **Mega File Not Found!**\n\n"
                    "The requested file or folder has been deleted or expired.",
                    buttons=None,
                )
            else:
                await status_msg.edit(
                    f"❌ **Mega Download Error:**\n`{err_output[-250:] if err_output else 'Unknown error'}`",
                    buttons=None,
                )
            return

        # Locate downloaded files (supporting both single files and recursive folders)
        all_downloaded = sorted([f for f in local_dir.rglob("*") if f.is_file() and not f.name.startswith(".")])
        if not all_downloaded:
            await status_msg.edit("❌ **No files found in the downloaded Mega link.**", buttons=None)
            return

        raw_count = len(all_downloaded)

        # Categorize files
        video_files = [f for f in all_downloaded if is_video_file(f)]
        photo_files = [f for f in all_downloaded if is_photo_file(f)]
        other_files = [f for f in all_downloaded if f not in video_files and f not in photo_files]
        other_count = len(other_files)

        files_to_upload = all_downloaded

        # ---------------- SMART ACTION BUTTONS (MULTI-FILE FOLDERS) ---------------- #
        if raw_count > 1 and not has_explicit_inline_filter and pending_prompts is not None:
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
                f"📂 **Folder Discovered (Mega.nz)**\n\n"
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
                files_to_upload = video_files
                filter_mode = "video"
            elif chosen_action == "photo":
                files_to_upload = photo_files
                filter_mode = "photo"
            elif chosen_action == "other":
                files_to_upload = other_files
                filter_mode = "other"
            else:
                files_to_upload = all_downloaded
                filter_mode = "all"
        else:
            # Inline filter specified or default filter active
            if filter_mode == "video":
                files_to_upload = video_files
            elif filter_mode == "photo":
                files_to_upload = photo_files
            elif filter_mode == "other":
                files_to_upload = other_files
            else:
                files_to_upload = all_downloaded

        # Check if filter resulted in 0 files
        total_files = len(files_to_upload)
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

        # Delete unused filtered-out files from local disk immediately to free space
        for f in all_downloaded:
            if f not in files_to_upload:
                try:
                    f.unlink()
                except Exception:
                    pass

        total_size = sum(f.stat().st_size for f in files_to_upload)
        is_multi_file = total_files > 1

        if is_multi_file:
            filter_badge = f"\n🎯 **Filter Active**: `{filter_mode.upper()} ONLY` ({total_files} of {raw_count} files selected)" if filter_mode != "all" else ""
            await status_msg.edit(
                f"📂 **Processing {total_files} files** in this Mega share.{filter_badge}\n"
                "⏳ *Beginning smart batch upload...*",
                buttons=make_stop_btn(user_id),
            )
            await asyncio.sleep(1.0)

        # ---------------- SMART ALBUM MAKER & UPLOAD PIPELINE ---------------- #
        uploader = TelethonUploader(event.client)
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
                    msgs = await event.client.send_file(config.PRIVATE_CHAT_ID, album_paths, caption=album_captions)
                    await event.client.forward_messages(event.chat_id, msgs)
                else:
                    await event.client.send_file(event.chat_id, album_paths, caption=album_captions)
            except Exception as e:
                logger.error(f"Album upload failed: {e}")

            for p in album_paths:
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
            album_paths.clear()
            album_captions.clear()
            album_size = 0

        for idx, file_path in enumerate(files_to_upload, start=1):
            file_size = file_path.stat().st_size
            file_prefix = f"[{idx}/{total_files}] " if is_multi_file else ""

            # Check Over-size (2GB standard Telegram limit)
            max_size_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
            if file_size > max_size_bytes:
                await flush_album()
                await event.reply(
                    f"📄 **{file_path.name}**\n\n"
                    f"💾 **Size**: {format_bytes(file_size)}\n"
                    f"🛑 **Direct Bot Upload Limit**: {config.MAX_FILE_SIZE_MB} MB\n\n"
                    f"This file exceeds the Telegram bot upload limit."
                )
                try:
                    file_path.unlink()
                except Exception:
                    pass
                continue

            # ALBUM LOGIC for photos and small videos/files (< 100 MB) when multiple files exist
            can_be_album_item = (
                is_multi_file
                and file_size < ALBUM_MAX_SIZE
                and (is_photo_file(file_path) or is_video_file(file_path) or total_files >= 3)
            )

            if can_be_album_item:
                if album_size + file_size > ALBUM_MAX_SIZE or len(album_paths) >= ALBUM_MAX_ITEMS:
                    await flush_album()

                album_paths.append(file_path)
                album_captions.append(f"📁 **{file_path.name}**\n☁️ `Mega.nz`")
                album_size += file_size
                continue

            # Large file (> 100 MB) OR standalone file -> Standalone Fast MTProto Upload
            await flush_album()

            # Video thumbnail generation
            thumb_path = None
            if is_video_file(file_path):
                try:
                    meta = get_video_metadata(file_path)
                    if meta and meta.get("duration", 0) > 0:
                        thumb_path = await generate_video_thumbnail(file_path, meta["duration"])
                except Exception as thumb_err:
                    logger.warning(f"Failed to generate thumbnail for {file_path.name}: {thumb_err}")

            # Throttled upload progress callback (FloodWait prevention)
            last_upload_edit = 0.0

            async def upload_progress(current: int, total: int, speed: float):
                nonlocal last_upload_edit
                now = time.monotonic()
                if now - last_upload_edit >= 3.5 or current == total:
                    last_upload_edit = now
                    txt = render_mega_progress(
                        action=f"📤 {file_prefix}Uploading to Telegram",
                        name=file_path.name,
                        current=current,
                        total=total,
                        speed=speed,
                    )
                    try:
                        await status_msg.edit(txt, buttons=make_stop_btn(user_id))
                    except Exception:
                        pass

            caption = (
                f"📁 **{file_path.name}**\n"
                f"📦 **Size**: `{format_bytes(file_size)}`\n"
                f"☁️ **Source**: `Mega.nz`"
            )

            await uploader.upload_media(
                file_path=file_path,
                reply_to_msg_id=event.id,
                caption=caption,
                thumb_path=thumb_path,
                progress_callback=upload_progress,
                workers=12,
            )

            if thumb_path and thumb_path.exists():
                try:
                    thumb_path.unlink()
                except Exception:
                    pass

            try:
                file_path.unlink()
            except Exception:
                pass

        # Flush any remaining album items
        await flush_album()

        # Delete status message
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        await event.reply(
            f"✅ **Mega Transfer Complete!**\n"
            f"Successfully processed {total_files} file(s) ({format_bytes(total_size)})."
        )

    except asyncio.CancelledError:
        logger.info(f"Mega task cancelled by user {user_id}")
        stop_monitor.set()
        if monitor_task:
            monitor_task.cancel()
        if process and process.returncode is None:
            try:
                process.kill()
            except Exception:
                pass
        if status_msg:
            try:
                await status_msg.edit("🛑 **Mega task aborted by user.**", buttons=None)
            except Exception:
                pass
        raise

    except Exception as e:
        logger.error(f"Unexpected Mega processing error: {e}", exc_info=True)
        if status_msg:
            try:
                await status_msg.edit(f"❌ **An unexpected error occurred:**\n`{str(e)}`", buttons=None)
            except Exception:
                pass
        else:
            await event.reply(f"❌ **Failed to process Mega link:** {str(e)}")

    finally:
        stop_monitor.set()
        if monitor_task and not monitor_task.done():
            monitor_task.cancel()
        if process and process.returncode is None:
            try:
                process.kill()
            except Exception:
                pass
        # Guaranteed disk cleanup
        if 'local_dir' in locals() and local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)
        if slot_acquired:
            queue_mgr.release_worker(user_id)
