import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import aiohttp
from telethon import events
from telethon.tl.patched import Message

import config
from core.database import db
from core.downloader import TeraBoxDownloader, DownloadError
from core.media import (
    VIDEO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    is_photo_file,
    is_video_file,
)
from core.queue_manager import queue_mgr
from core.resolver import (
    TeraBoxResolver,
    CookieExpiredError,
    ShareLinkExpiredError,
    CloudflareBlockedError,
    ResolverError,
    TeraFile,
)
from core.task_manager import task_mgr
from core.uploader import TelethonUploader
from keyboards import (
    make_stop_btn,
    get_channel_button,
    get_channel_buttons_row,
    get_folder_action_buttons,
)
from strings import COOKIE_EXPIRED_USER, COOKIE_EXPIRED_ADMIN, CLOUDFLARE_BLOCKED_TEXT
from utils.helpers import format_bytes, format_duration
from utils.logger import logger
from utils.progress import render_progress_text, render_album_progress


async def notify_admins(client, text: str):
    """Send an alert message to all configured ADMIN_IDS."""
    for admin_id in config.ADMIN_IDS:
        try:
            await client.send_message(admin_id, text)
        except Exception as e:
            logger.warning(f"Failed to alert admin {admin_id}: {e}")


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


async def process_terabox_link(
    event: events.NewMessage.Event,
    url: str,
    filter_mode: str = "all",
    has_explicit_inline_filter: bool = False,
    uploader: Optional[TelethonUploader] = None,
):
    """Core pipeline for TeraBox link resolution, queueing, downloading, and uploading."""
    client = event.client
    if uploader is None:
        uploader = TelethonUploader(client)

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
        # Step 1: Resolve Link
        resolver = TeraBoxResolver()
        try:
            files = await resolver.resolve(url)
        except CookieExpiredError as e:
            logger.error(f"[ERR-COOKIE] Cookie expired or verify_v2 triggered: {e}", exc_info=True)
            await status_msg.edit(COOKIE_EXPIRED_USER)
            if user_id not in config.ADMIN_IDS:
                await notify_admins(client, COOKIE_EXPIRED_ADMIN)
            return
        except ShareLinkExpiredError as e:
            logger.warning(f"[ERR-SHARE] Share link inaccessible: {e}")
            await status_msg.edit(f"❌ **Link Not Accessible** `[ERR-SHARE]`\n\n{str(e)}")
            return
        except CloudflareBlockedError as e:
            logger.warning(f"[ERR-CF] Cloudflare blocked: {e}")
            await status_msg.edit(CLOUDFLARE_BLOCKED_TEXT)
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
        photo_files = [f for f in files if Path(f.file_name).suffix.lower() in IMAGE_EXTENSIONS]
        other_files = [f for f in files if f not in video_files and f not in photo_files]
        other_count = len(other_files)

        # Interactive Button Card: Prompt user if folder contains multiple items and no inline filter was specified
        if raw_count > 1 and not has_explicit_inline_filter:
            session_id = uuid.uuid4().hex[:8]
            future = task_mgr.create_prompt(session_id, user_id)

            total_bytes = sum(f.size for f in files)
            video_bytes = sum(f.size for f in video_files)
            photo_bytes = sum(f.size for f in photo_files)
            other_bytes = sum(f.size for f in other_files)

            from core.webapp_server import register_terabox_session, build_webapp_url

            register_terabox_session(session_id, files, title="TeraBox Share")
            webapp_url = build_webapp_url(session_id)

            buttons = get_folder_action_buttons(
                session_id=session_id,
                video_count=len(video_files),
                photo_count=len(photo_files),
                other_count=other_count,
                webapp_url=webapp_url,
            )

            msg_text = (
                f"📂 **Folder Discovered (TeraBox)**\n\n"
                f"📊 **Total Content**: `{raw_count} Files` • `{format_bytes(total_bytes)}`\n"
            )
            if len(video_files) > 0:
                msg_text += f"• 🎬 **Videos**: `{len(video_files)} Files` (~`{format_bytes(video_bytes)}`)\n"
            if len(photo_files) > 0:
                msg_text += f"• 🖼️ **Photos**: `{len(photo_files)} Files` (~`{format_bytes(photo_bytes)}`)\n"
            if other_count > 0:
                msg_text += f"• 📄 **Others**: `{other_count} Files` (~`{format_bytes(other_bytes)}`)\n"
            msg_text += "\n👇 **Aapko kya download karna hai? Choose karo:**"

            await status_msg.edit(msg_text, buttons=buttons)

            try:
                chosen_action = await asyncio.wait_for(future, timeout=300)
            except asyncio.TimeoutError:
                await status_msg.edit("⏱️ **Selection timed out (5 min).** Please resend link if needed.", buttons=None)
                return
            finally:
                task_mgr.remove_prompt(session_id)

            if isinstance(chosen_action, tuple) and chosen_action[0] == "multi":
                handle_list = set(str(h) for h in chosen_action[1])
                files = [f for f in files if str(f.fs_id) in handle_list or str(f.file_name) in handle_list]
                filter_mode = "custom"
            elif chosen_action == "cancel":
                await status_msg.edit("❌ **Download cancelled by user.**", buttons=None)
                return
            elif chosen_action == "video":
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

        # Sort files smallest first for quickest initial delivery
        files = sorted(files, key=lambda f: f.size)
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
                "⏳ *Beginning smart batch download (smallest first)...*",
                buttons=make_stop_btn(user_id),
            )
            await asyncio.sleep(1.5)

        # Acquire worker concurrency lease immediately before beginning downloads
        await queue_mgr.enter_worker(user_id)

        target_chat_id = config.PRIVATE_CHAT_ID if config.PRIVATE_CHAT_ID else event.chat_id
        delivery_mode = await db.get_user_delivery_mode(user_id)
        uploaded_count = 0
        uploaded_size = 0
        skipped_count = 0
        skipped_size = 0

        album_paths = []
        album_captions = []
        album_keys = []
        album_size = 0
        VIDEO_ALBUM_MAX_SIZE = 20 * 1024 * 1024   # 20 MB max per video in album
        PHOTO_ALBUM_MAX_SIZE = 100 * 1024 * 1024  # 100 MB max per photo in album
        TOTAL_ALBUM_MAX_SIZE = 200 * 1024 * 1024  # 200 MB max combined batch
        ALBUM_MAX_ITEMS = 10

        async def flush_album():
            nonlocal album_paths, album_captions, album_keys, album_size, uploaded_count, uploaded_size
            if not album_paths:
                return

            last_album_edit = 0.0
            album_count = len(album_paths)
            item_label = "items"
            if all(is_photo_file(p) for p in album_paths):
                item_label = "photos"
            elif all(is_video_file(p) for p in album_paths):
                item_label = "videos"

            def album_item_progress(current, total):
                nonlocal last_album_edit
                now = time.monotonic()
                if now - last_album_edit >= 2.0 or current >= total:
                    last_album_edit = now
                    txt = render_album_progress(album_count, item_label, current, total)
                    try:
                        asyncio.create_task(status_msg.edit(txt, buttons=make_stop_btn(user_id)))
                    except Exception:
                        pass

            try:
                await status_msg.edit(
                    f"**📤 UPLOADING ALBUM TO TELEGRAM [Step 2/2]**\n\n"
                    f"📦 **Batch**: `{album_count} {item_label}`\n"
                    f"⚡ *Preparing high-speed album batch of {album_count} files...*",
                    buttons=make_stop_btn(user_id),
                )
                msgs = await uploader.upload_album(
                    chat_id=target_chat_id,
                    file_paths=album_paths,
                    captions=album_captions,
                    progress_callback=album_item_progress,
                    workers=12,
                )
                if config.PRIVATE_CHAT_ID and msgs and delivery_mode == "both":
                    try:
                        await client.forward_messages(event.chat_id, msgs)
                    except Exception as fwd_err:
                        logger.warning(f"Could not forward album to user chat: {fwd_err}")

                # Save cache for album items
                if msgs and isinstance(msgs, list):
                    for k, p, m in zip(album_keys, album_paths, msgs):
                        if k and m:
                            try:
                                sz = p.stat().st_size if p.exists() else 0
                                await db.save_cached_file(k, p.name, sz, target_chat_id, m.id)
                                uploaded_count += 1
                                uploaded_size += sz
                            except Exception:
                                pass

                for p in album_paths:
                    try:
                        if p.exists(): p.unlink()
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Album upload failed: {e}. Falling back to standalone uploads...", exc_info=True)
                for p, cap, k in zip(album_paths, album_captions, album_keys):
                    if not p.exists():
                        continue
                    try:
                        sz = p.stat().st_size if p.exists() else 0
                        sent = await uploader.upload_media(
                            chat_id=target_chat_id,
                            file_path=p,
                            caption=cap,
                            progress_callback=None,
                            workers=12,
                        )
                        if config.PRIVATE_CHAT_ID and sent and delivery_mode == "both":
                            try:
                                await client.forward_messages(event.chat_id, sent)
                            except Exception as fwd_err:
                                logger.warning(f"Could not forward fallback upload to user chat: {fwd_err}")
                        if sent and k:
                            await db.save_cached_file(k, p.name, sz, target_chat_id, sent.id)
                            uploaded_count += 1
                            uploaded_size += sz
                    except Exception as fallback_err:
                        logger.error(f"Fallback upload failed for {p.name}: {fallback_err}")
                    finally:
                        try:
                            if p.exists(): p.unlink()
                        except Exception:
                            pass

            album_paths.clear()
            album_captions.clear()
            album_keys.clear()
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

            file_key = f"tb_{file_obj.fs_id}" if getattr(file_obj, "fs_id", None) else f"tb_{abs(hash(file_obj.file_name + str(file_obj.size)))}"

            # Storage Channel / DB cache check: SILENT SKIP IF ALREADY IN STORAGE!
            cached_msg = await db.find_cached_media(
                client, config.PRIVATE_CHAT_ID, file_key, file_name=file_obj.file_name, file_size=file_obj.size
            )
            if cached_msg:
                logger.info(f"Storage cache hit for TeraBox file {file_obj.file_name}! Silently skipping...")
                skipped_count += 1
                skipped_size += file_obj.size
                skip_msg = (
                    f"⚡ {file_prefix}**Skipped (Already in Storage)**\n\n"
                    f"🎬 `{file_obj.file_name}`\n"
                    f"⏩ File pehle se storage channel mein hai — direct skip!"
                )
                try:
                    await status_msg.edit(skip_msg, buttons=make_stop_btn(user_id))
                except Exception:
                    pass
                continue

            file_path_obj = Path(file_obj.file_name)
            is_photo = is_photo_file(file_path_obj)
            is_video = is_video_file(file_path_obj)

            # Album Eligibility: Photos <= 100MB, Videos <= 20MB
            can_be_album_item = (
                is_multi_file
                and (
                    (is_photo and file_obj.size <= PHOTO_ALBUM_MAX_SIZE)
                    or (is_video and file_obj.size <= VIDEO_ALBUM_MAX_SIZE)
                )
            )

            # Pre-download batch management:
            if not can_be_album_item and album_paths:
                await flush_album()

            if can_be_album_item and (len(album_paths) >= ALBUM_MAX_ITEMS or album_size + file_obj.size > TOTAL_ALBUM_MAX_SIZE):
                await flush_album()

            if not can_be_album_item:
                # Standalone download
                last_edit_time = 0.0
                async def download_progress(current: int, total: int, speed: float):
                    nonlocal last_edit_time
                    now = time.monotonic()
                    if now - last_edit_time >= 2.0 or current == total:
                        last_edit_time = now
                        txt = render_progress_text(
                            "📥 DOWNLOADING FROM TERABOX [Step 1/2]",
                            file_obj.file_name,
                            current,
                            total,
                            speed,
                            file_prefix=file_prefix,
                        )
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

                # Instant direct switch: download hits 100%, immediately flip to Uploading 0%
                try:
                    instant_txt = render_progress_text(
                        "📤 UPLOADING TO TELEGRAM [Step 2/2]",
                        file_obj.file_name,
                        0,
                        file_obj.size,
                        0.0,
                        file_prefix=file_prefix,
                    )
                    await status_msg.edit(instant_txt, buttons=make_stop_btn(user_id))
                except Exception:
                    pass

                # Upload File Standalone
                last_upload_edit = 0.0
                async def upload_progress(current: int, total: int, speed: float):
                    nonlocal last_upload_edit
                    now = time.monotonic()
                    if now - last_upload_edit >= 2.0 or current == total:
                        last_upload_edit = now
                        txt = render_progress_text(
                            "📤 UPLOADING TO TELEGRAM [Step 2/2]",
                            file_obj.file_name,
                            current,
                            total,
                            speed,
                            file_prefix=file_prefix,
                        )
                        try:
                            await status_msg.edit(txt, buttons=make_stop_btn(user_id))
                        except Exception:
                            pass

                caption = f"🎬 `{file_obj.file_name}` (`{file_obj.size_readable}`)\n`#{file_key}` `#sz_{file_obj.size}`"

                try:
                    sent_msg = await uploader.upload_media(
                        chat_id=target_chat_id,
                        file_path=downloaded_file,
                        caption=caption,
                        thumb_path=None,
                        progress_callback=upload_progress,
                        workers=12,
                    )
                    if sent_msg:
                        if config.PRIVATE_CHAT_ID and delivery_mode == "both":
                            try:
                                await client.forward_messages(event.chat_id, sent_msg)
                            except Exception as fwd_err:
                                logger.warning(f"Could not forward to user chat: {fwd_err}")
                        await db.save_cached_file(file_key, file_obj.file_name, file_obj.size, target_chat_id, sent_msg.id)
                        uploaded_count += 1
                        uploaded_size += file_obj.size
                except Exception as up_err:
                    await event.reply(f"❌ {file_prefix}**Upload Failed**\n\n{str(up_err)}")
                finally:
                    if downloaded_file.exists(): downloaded_file.unlink()
                continue

            # Album batch item download:
            last_edit_time = 0.0
            async def download_progress_album(current: int, total: int, speed: float):
                nonlocal last_edit_time
                now = time.monotonic()
                if now - last_edit_time >= 2.0 or current == total:
                    last_edit_time = now
                    txt = render_progress_text(
                        "📥 DOWNLOADING FROM TERABOX (Album Batch) [Step 1/2]",
                        file_obj.file_name,
                        current,
                        total,
                        speed,
                        file_prefix=file_prefix,
                    )
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
                album_keys.append(file_key)
                icon = "🎬" if is_video else "🖼️"
                album_captions.append(f"{icon} `{file_obj.file_name}` (`{file_obj.size_readable}`)\n`#{file_key}` `#sz_{file_obj.size}`")
                album_size += file_obj.size

                if len(album_paths) >= ALBUM_MAX_ITEMS or album_size >= TOTAL_ALBUM_MAX_SIZE:
                    await flush_album()
            except DownloadError as e:
                await event.reply(f"❌ {file_prefix}**Download Failed**: {str(e)}")
                continue

        # Flush remaining album files
        await flush_album()

        # Finish up - Deliver completion notification card to Bot DM
        mode_label = "📬 Channel + Bot DM" if delivery_mode == "both" else "🛡️ Storage Channel Only (Vault Mode)"
        ch_btn = get_channel_button(config.PRIVATE_CHAT_ID)
        ch_buttons = get_channel_buttons_row(config.PRIVATE_CHAT_ID)

        summary_lines = [
            "🎉 **TeraBox Transfer Complete!**\n",
            f"📤 **Newly Uploaded**: `{uploaded_count} file(s)` ({format_bytes(uploaded_size)})",
        ]
        if skipped_count > 0:
            summary_lines.append(f"⏩ **Skipped (Already in Storage)**: `{skipped_count} file(s)` ({format_bytes(skipped_size)})")
        summary_lines.append(f"📊 **Total Handled**: `{total_files} file(s)`")
        if config.PRIVATE_CHAT_ID:
            summary_lines.append(f"⚙️ **Delivery Mode**: `{mode_label}`")
        if ch_btn:
            summary_lines.append("\n👇 **Click below to view all files:**")

        receipt_text = "\n".join(summary_lines)
        sent_receipt = False
        if status_msg:
            try:
                await status_msg.edit(receipt_text, buttons=ch_buttons)
                sent_receipt = True
            except Exception:
                pass

        if not sent_receipt:
            try:
                await event.respond(receipt_text, buttons=ch_buttons)
            except Exception:
                try:
                    await event.reply(receipt_text, buttons=ch_buttons)
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
