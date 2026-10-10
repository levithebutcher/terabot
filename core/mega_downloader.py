import asyncio
import base64
import json
import os
import re
import shutil
import struct
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
import pyaes
from telethon import Button, events

import config
from core.media import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, generate_video_thumbnail, get_video_metadata, is_photo_file, is_video_file
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


def is_mega_folder_url(url: str) -> bool:
    """Return True if the link points to a Mega folder."""
    return bool(re.search(r"mega\.(?:nz|co\.nz|io)/(?:folder/|#F!)", url, re.IGNORECASE))


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


def sanitize_filename(name: str) -> str:
    """Remove unsafe filesystem characters from filename."""
    clean = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return clean or "unnamed_file"


def get_aes_ctr_decryptor(file_key: bytes, iv_int: int):
    """
    Return a fast decrypt function for AES-128-CTR.
    Prefers cryptography (OpenSSL AES-NI C bindings, ~1 GB/s),
    then pycryptodome (C extension, ~500 MB/s),
    falling back to pyaes (pure Python).
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        iv_bytes = struct.pack(">Q", iv_int) + (b"\x00" * 8)
        cipher = Cipher(algorithms.AES(file_key), modes.CTR(iv_bytes), backend=default_backend())
        decryptor = cipher.decryptor()
        return decryptor.update
    except ImportError:
        pass

    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Counter
        ctr = Counter.new(128, initial_value=(iv_int << 64))
        cipher = AES.new(file_key, AES.MODE_CTR, counter=ctr)
        return cipher.decrypt
    except ImportError:
        pass

    import pyaes
    aes_ctr = pyaes.AESModeOfOperationCTR(file_key, counter=pyaes.Counter(iv_int << 64))
    return aes_ctr.decrypt


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


def normalize_mega_url_for_engine(url: str, engine_name: str) -> str:
    """Ensure Mega URL format matches engine expectations (e.g. legacy #F! format for megatools)."""
    if engine_name == "megatools":
        m_folder = re.search(r"mega\.(?:nz|co\.nz|io)/folder/([a-zA-Z0-9_\-]+)#([a-zA-Z0-9_\-]+)", url)
        if m_folder:
            return f"https://mega.co.nz/#F!{m_folder.group(1)}!{m_folder.group(2)}"
        m_file = re.search(r"mega\.(?:nz|co\.nz|io)/file/([a-zA-Z0-9_\-]+)#([a-zA-Z0-9_\-]+)", url)
        if m_file:
            return f"https://mega.co.nz/#!{m_file.group(1)}!{m_file.group(2)}"
    return url


# ---------------- FAST PRE-DOWNLOAD FOLDER INSPECTOR ---------------- #

def _b64_dec(data: str) -> bytes:
    data += "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data)


def _a32_to_str(a: tuple) -> bytes:
    return struct.pack(">%dI" % len(a), *a)


def _str_to_a32(b: bytes) -> tuple:
    if len(b) % 4:
        b += b"\0" * (4 - len(b) % 4)
    return struct.unpack(">%dI" % (len(b) // 4), b)


async def inspect_mega_folder(url: str) -> Optional[dict]:
    """
    Inspect a public Mega folder via Mega API in pure Python in seconds.
    Returns folder file count, categorized into videos, photos, others, and total size,
    along with decrypted node keys and handles for selective downloading.
    """
    m = re.search(r"mega\.(?:nz|co\.nz|io)/(?:folder/|#F!)([a-zA-Z0-9_\-]+)[#!]([a-zA-Z0-9_\-]+)", url)
    if not m:
        return None

    folder_id, folder_key = m.group(1), m.group(2)
    api_url = f"https://g.api.mega.co.nz/cs?id=0&n={folder_id}"
    payload = json.dumps([{"a": "f", "c": 1, "ca": 1, "r": 1}])

    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(api_url, data=payload, headers={"Content-Type": "application/json"}) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.json()

        if not raw or not isinstance(raw, list) or "f" not in raw[0]:
            return None

        nodes = raw[0]["f"]
        folder_key_bytes = _b64_dec(folder_key)

        # ECB decryptor for folder master keys
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            cipher_ecb = Cipher(algorithms.AES(folder_key_bytes), modes.ECB(), backend=default_backend())
            decrypt_ecb = cipher_ecb.decryptor().update
        except ImportError:
            try:
                from Crypto.Cipher import AES
                decrypt_ecb = AES.new(folder_key_bytes, AES.MODE_ECB).decrypt
            except ImportError:
                import pyaes
                decrypt_ecb = pyaes.AESModeOfOperationECB(folder_key_bytes).decrypt

        videos = []
        photos = []
        others = []

        for node in nodes:
            if node.get("t") != 0:  # Skip directories
                continue
            k_parts = node.get("k", "").split(":")
            if len(k_parts) < 2:
                continue

            try:
                enc_k = _b64_dec(k_parts[1])
                dec_k = decrypt_ecb(enc_k[:16]) + decrypt_ecb(enc_k[16:32])
                k_a32 = _str_to_a32(dec_k)
                file_key = _a32_to_str(
                    (k_a32[0] ^ k_a32[4], k_a32[1] ^ k_a32[5], k_a32[2] ^ k_a32[6], k_a32[3] ^ k_a32[7])
                )
                iv_int = struct.unpack(">Q", _a32_to_str((k_a32[4], k_a32[5])))[0]

                enc_attr = _b64_dec(node["a"])

                # CBC decryptor for file metadata
                try:
                    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                    from cryptography.hazmat.backends import default_backend
                    cipher_cbc = Cipher(algorithms.AES(file_key), modes.CBC(b"\0" * 16), backend=default_backend())
                    dec_attr = cipher_cbc.decryptor().update(enc_attr)
                except ImportError:
                    try:
                        from Crypto.Cipher import AES
                        dec_attr = AES.new(file_key, AES.MODE_CBC, iv=b"\0" * 16).decrypt(enc_attr)
                    except ImportError:
                        import pyaes
                        aes_cbc = pyaes.AESModeOfOperationCBC(file_key, iv=b"\0" * 16)
                        dec_attr = b"".join(aes_cbc.decrypt(enc_attr[i : i + 16]) for i in range(0, len(enc_attr), 16))

                m_json = re.search(b"MEGA({.+?})", dec_attr)
                if not m_json:
                    continue
                attr = json.loads(m_json.group(1).decode("utf-8", errors="ignore"))
                name = attr.get("n", "")
                size = node.get("s", 0)
                ext = ("." + name.split(".")[-1].lower()) if "." in name else ""

                item = {
                    "name": name,
                    "size": size,
                    "h": node["h"],
                    "file_key": file_key,
                    "iv_int": iv_int,
                }
                if ext in VIDEO_EXTENSIONS:
                    videos.append(item)
                elif ext in IMAGE_EXTENSIONS:
                    photos.append(item)
                else:
                    others.append(item)
            except Exception:
                continue

        total_files = len(videos) + len(photos) + len(others)
        total_size = sum(f["size"] for f in videos + photos + others)

        return {
            "folder_id": folder_id,
            "folder_key": folder_key,
            "total_files": total_files,
            "videos": videos,
            "photos": photos,
            "others": others,
            "total_size": total_size,
        }
    except Exception as e:
        logger.warning(f"Error inspecting Mega folder via API: {e}")
        return None


async def download_mega_node_stream(
    session: aiohttp.ClientSession,
    folder_id: str,
    node: dict,
    dest_path: Path,
    progress_callback=None,
    stop_event: Optional[asyncio.Event] = None,
) -> bool:
    """
    Download a single file node from a Mega folder directly via CDN streaming & AES-CTR decryption.
    Downloads ONLY this single file without touching any other files in the folder.
    """
    target_size = node.get("size", 0)
    if target_size == 0:
        dest_path.touch(exist_ok=True)
        return True

    api_url = f"https://g.api.mega.co.nz/cs?id=0&n={folder_id}"
    payload = json.dumps([{"a": "g", "g": 1, "n": node["h"]}])

    try:
        async with session.post(api_url, data=payload, headers={"Content-Type": "application/json"}) as resp:
            if resp.status != 200:
                logger.error(f"Mega API error {resp.status} for node {node['h']}")
                return False
            data = await resp.json()

        if not data or not isinstance(data, list) or "g" not in data[0]:
            logger.error(f"Failed to obtain Mega CDN link for node {node['h']}: {data}")
            return False

        g_url = data[0]["g"]
        file_key = node["file_key"]
        iv_int = node["iv_int"]
        decrypt_func = get_aes_ctr_decryptor(file_key, iv_int)

        timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=60)
        async with session.get(g_url, timeout=timeout) as stream_resp:
            if stream_resp.status not in (200, 206):
                logger.error(f"Mega CDN status {stream_resp.status} for {node['name']}")
                return False

            downloaded_bytes = 0
            last_callback_time = 0.0
            start_time = time.monotonic()

            with open(dest_path, "wb") as f:
                async for chunk in stream_resp.content.iter_chunked(256 * 1024):
                    if stop_event and stop_event.is_set():
                        return False

                    plain_chunk = decrypt_func(chunk)
                    if downloaded_bytes + len(plain_chunk) > target_size:
                        plain_chunk = plain_chunk[: target_size - downloaded_bytes]

                    f.write(plain_chunk)
                    downloaded_bytes += len(plain_chunk)

                    now = time.monotonic()
                    if progress_callback and (now - last_callback_time >= 3.5 or downloaded_bytes >= target_size):
                        last_callback_time = now
                        speed = downloaded_bytes / (now - start_time) if (now - start_time) > 0 else 0.0
                        await progress_callback(downloaded_bytes, target_size, speed)

        return True
    except Exception as e:
        logger.error(f"Error streaming Mega node {node.get('name')}: {e}")
        if dest_path.exists():
            try:
                dest_path.unlink()
            except Exception:
                pass
        return False


# ---------------- MAIN MEGA PROCESSOR ---------------- #

async def process_mega_link(
    event: events.NewMessage.Event,
    url: str,
    filter_mode: str = "all",
    has_explicit_inline_filter: bool = False,
    pending_prompts: Optional[dict] = None,
):
    """
    Handle downloading from Mega.nz (file or folder) and uploading to Telegram.
    Inspects folders in advance, prompts Smart Action Buttons BEFORE downloading,
    downloads ONLY the chosen category (zero wasted bytes), batches photos into Smart Albums,
    and streams updates with live speed & ETA.
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

        if not status_msg:
            status_msg = await event.reply("🔍 **Resolving Mega link...**", buttons=make_stop_btn(user_id))
        else:
            await status_msg.edit("🔍 **Resolving Mega link...**", buttons=make_stop_btn(user_id))

        folder_info = None

        # ---------------- PRE-DOWNLOAD FOLDER INSPECTION & SMART BUTTONS ---------------- #
        if is_mega_folder_url(url):
            await status_msg.edit("📂 **Inspecting Mega folder contents...**", buttons=make_stop_btn(user_id))
            folder_info = await inspect_mega_folder(url)

            if folder_info and folder_info["total_files"] > 1:
                raw_count = folder_info["total_files"]
                num_videos = len(folder_info["videos"])
                num_photos = len(folder_info["photos"])
                num_others = len(folder_info["others"])
                total_sz_readable = format_bytes(folder_info["total_size"])

                # If no explicit inline filter was provided, present the interactive buttons FIRST
                if not has_explicit_inline_filter and pending_prompts is not None:
                    session_id = uuid.uuid4().hex[:8]
                    loop = asyncio.get_running_loop()
                    future = loop.create_future()
                    pending_prompts[session_id] = (user_id, future)

                    video_bytes = sum(f["size"] for f in folder_info["videos"])
                    photo_bytes = sum(f["size"] for f in folder_info["photos"])
                    other_bytes = sum(f["size"] for f in folder_info["others"])

                    buttons = []
                    row1 = []
                    if num_videos > 0:
                        row1.append(Button.inline("🎬 Videos", data=f"act:video:{session_id}"))
                    if num_photos > 0:
                        row1.append(Button.inline("🖼️ Photos", data=f"act:photo:{session_id}"))
                    if row1:
                        buttons.append(row1)

                    row2 = []
                    if num_others > 0:
                        row2.append(Button.inline("📄 Others", data=f"act:other:{session_id}"))
                    row2.append(Button.inline("📁 Download All", data=f"act:all:{session_id}"))
                    buttons.append(row2)

                    buttons.append([Button.inline("❌ Cancel", data=f"act:cancel:{session_id}")])

                    msg_text = (
                        f"📂 **Folder Discovered (Mega.nz)**\n\n"
                        f"📊 **Total Content**: `{raw_count} Files` • `{total_sz_readable}`\n"
                    )
                    if num_videos > 0:
                        msg_text += f"• 🎬 **Videos**: `{num_videos} Files` (~`{format_bytes(video_bytes)}`)\n"
                    if num_photos > 0:
                        msg_text += f"• 🖼️ **Photos**: `{num_photos} Files` (~`{format_bytes(photo_bytes)}`)\n"
                    if num_others > 0:
                        msg_text += f"• 📄 **Others**: `{num_others} Files` (~`{format_bytes(other_bytes)}`)\n"
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

                    filter_mode = chosen_action

                # Check if chosen filter has 0 files
                if filter_mode == "video" and num_videos == 0:
                    await status_msg.edit(
                        f"⚠️ **No Videos Found!**\nThis folder contains {raw_count} files, but 0 are videos.\n\n"
                        "💡 _Use 'all' to download everything without filtering._",
                        buttons=None,
                    )
                    return
                elif filter_mode == "photo" and num_photos == 0:
                    await status_msg.edit(
                        f"⚠️ **No Photos Found!**\nThis folder contains {raw_count} files, but 0 are photos.\n\n"
                        "💡 _Use 'all' to download everything without filtering._",
                        buttons=None,
                    )
                    return
                elif filter_mode == "other" and num_others == 0:
                    await status_msg.edit(
                        f"⚠️ **No Other Files Found!**\nThis folder contains {raw_count} files, but 0 other files.\n\n"
                        "💡 _Use 'all' to download everything without filtering._",
                        buttons=None,
                    )
                    return

        # Prepare isolated download directory
        safe_id = f"{int(time.time())}_{user_id}"
        local_dir = config.DOWNLOAD_DIR / f"mega_{safe_id}"
        local_dir.mkdir(parents=True, exist_ok=True)

        # ---------------- SELECTIVE STREAM DOWNLOADER FOR MEGA FOLDERS ---------------- #
        if is_mega_folder_url(url) and folder_info and folder_info.get("folder_id"):
            if filter_mode == "video":
                selected_nodes = folder_info["videos"]
            elif filter_mode == "photo":
                selected_nodes = folder_info["photos"]
            elif filter_mode == "other":
                selected_nodes = folder_info["others"]
            else:
                selected_nodes = folder_info["videos"] + folder_info["photos"] + folder_info["others"]

            total_selected = len(selected_nodes)
            if total_selected == 0:
                type_label = {"video": "Videos", "photo": "Photos", "other": "Other Files"}.get(filter_mode, "Files")
                await status_msg.edit(
                    f"⚠️ **No {type_label} Found!**\nNo files matched your filter (`{filter_mode.upper()}`).",
                    buttons=None,
                )
                return

            total_selected_size = sum(n["size"] for n in selected_nodes)
            is_multi_file = total_selected > 1

            filter_badge_text = f"\n🎯 **Filter Active**: `{filter_mode.upper()} ONLY` ({total_selected} files selected)" if filter_mode != "all" else ""
            await status_msg.edit(
                f"📂 **Processing {total_selected} files** in this Mega folder.{filter_badge_text}\n"
                f"📦 **Selected Size**: `{format_bytes(total_selected_size)}`\n"
                "⏳ *Beginning selective stream download & upload...*",
                buttons=make_stop_btn(user_id),
            )
            await asyncio.sleep(1.0)

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

            timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as dl_session:
                for idx, node in enumerate(selected_nodes, start=1):
                    if stop_monitor.is_set():
                        break

                    file_size = node["size"]
                    file_name = sanitize_filename(node["name"])
                    file_prefix = f"[{idx}/{total_selected}] " if is_multi_file else ""

                    # Check 2000 MB Telegram Bot upload limit
                    max_size_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
                    if file_size > max_size_bytes:
                        await flush_album()
                        await event.reply(
                            f"📄 **{file_name}**\n\n"
                            f"💾 **Size**: {format_bytes(file_size)}\n"
                            f"🛑 **Direct Bot Upload Limit**: {config.MAX_FILE_SIZE_MB} MB\n\n"
                            f"⚠️ This file exceeds Telegram's 2000 MB bot limit and was skipped."
                        )
                        continue

                    # Unique destination path to prevent collision
                    dest_path = local_dir / file_name
                    if dest_path.exists():
                        dest_path = local_dir / f"{dest_path.stem}_{node['h'][:4]}{dest_path.suffix}"

                    # Throttled download progress callback
                    last_dl_edit = 0.0
                    async def dl_progress(current: int, total: int, speed: float):
                        nonlocal last_dl_edit
                        now = time.monotonic()
                        if now - last_dl_edit >= 3.5 or current == total:
                            last_dl_edit = now
                            txt = render_mega_progress(
                                action=f"📥 {file_prefix}Downloading from Mega",
                                name=file_name,
                                current=current,
                                total=total,
                                speed=speed,
                            )
                            try:
                                await status_msg.edit(txt, buttons=make_stop_btn(user_id))
                            except Exception:
                                pass

                    success = await download_mega_node_stream(
                        session=dl_session,
                        folder_id=folder_info["folder_id"],
                        node=node,
                        dest_path=dest_path,
                        progress_callback=dl_progress,
                        stop_event=stop_monitor,
                    )

                    if not success or not dest_path.exists():
                        logger.warning(f"Skipping failed node download: {file_name}")
                        continue

                    actual_size = dest_path.stat().st_size

                    # Album grouping for photos and small files (< 100 MB)
                    can_be_album_item = (
                        is_multi_file
                        and actual_size < ALBUM_MAX_SIZE
                        and (is_photo_file(dest_path) or is_video_file(dest_path) or total_selected >= 3)
                    )

                    if can_be_album_item:
                        if album_size + actual_size > ALBUM_MAX_SIZE or len(album_paths) >= ALBUM_MAX_ITEMS:
                            await flush_album()
                        album_paths.append(dest_path)
                        album_captions.append(f"📁 **{dest_path.name}**\n☁️ `Mega.nz`")
                        album_size += actual_size
                        continue

                    # Standalone Upload for large file (> 100 MB)
                    await flush_album()

                    thumb_path = None
                    if is_video_file(dest_path):
                        try:
                            meta = get_video_metadata(dest_path)
                            if meta and meta.get("duration", 0) > 0:
                                thumb_path = await generate_video_thumbnail(dest_path, meta["duration"])
                        except Exception as thumb_err:
                            logger.warning(f"Failed thumbnail for {dest_path.name}: {thumb_err}")

                    last_upload_edit = 0.0
                    async def upload_progress(current: int, total: int, speed: float):
                        nonlocal last_upload_edit
                        now = time.monotonic()
                        if now - last_upload_edit >= 3.5 or current == total:
                            last_upload_edit = now
                            txt = render_mega_progress(
                                action=f"📤 {file_prefix}Uploading to Telegram",
                                name=dest_path.name,
                                current=current,
                                total=total,
                                speed=speed,
                            )
                            try:
                                await status_msg.edit(txt, buttons=make_stop_btn(user_id))
                            except Exception:
                                pass

                    caption = (
                        f"📁 **{dest_path.name}**\n"
                        f"📦 **Size**: `{format_bytes(actual_size)}`\n"
                        f"☁️ **Source**: `Mega.nz`"
                    )

                    await uploader.upload_media(
                        file_path=dest_path,
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
                        dest_path.unlink()
                    except Exception:
                        pass

            # Flush any remaining album items
            await flush_album()

            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

            await event.reply(
                f"✅ **Mega Transfer Complete!**\n"
                f"🎯 **Filter**: `{filter_mode.upper()}`\n"
                f"Successfully processed {total_selected} file(s) ({format_bytes(total_selected_size)})."
            )
            return

        # ---------------- FALLBACK ENGINE FOR SINGLE FILES / CLI ---------------- #
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

        # Build download command with normalized URL for engine
        engine_url = normalize_mega_url_for_engine(url, engine_name)
        if engine_name == "mega-cmd":
            cmd = base_cmd + [engine_url, str(local_dir)]
        else:  # megatools
            cmd = base_cmd + ["--path", str(local_dir), engine_url]

        filter_badge = f" [Filter: {filter_mode.upper()}]" if filter_mode != "all" else ""
        logger.info(f"Starting Mega download [{engine_name}] for user {user_id}{filter_badge}: {engine_url[:60]}")
        await status_msg.edit(
            f"📥 **Downloading from Mega...**{filter_badge}\n`Engine: {engine_name}`",
            buttons=make_stop_btn(user_id),
        )

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
                            name=f"Engine: {engine_name}{filter_badge}",
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
            err_output = stderr.decode().strip() or stdout.decode().strip()
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

        # Locate downloaded files on disk
        all_downloaded = sorted([f for f in local_dir.rglob("*") if f.is_file() and not f.name.startswith(".")])
        if not all_downloaded:
            await status_msg.edit("❌ **No files found in the downloaded Mega link.**", buttons=None)
            return

        # Apply filtering to downloaded files
        if filter_mode == "video":
            files_to_upload = [f for f in all_downloaded if is_video_file(f)]
        elif filter_mode == "photo":
            files_to_upload = [f for f in all_downloaded if is_photo_file(f)]
        elif filter_mode == "other":
            files_to_upload = [f for f in all_downloaded if not is_video_file(f) and not is_photo_file(f)]
        else:
            files_to_upload = all_downloaded

        # Delete unused filtered-out files immediately from disk
        for f in all_downloaded:
            if f not in files_to_upload:
                try:
                    f.unlink()
                except Exception:
                    pass

        total_files = len(files_to_upload)
        if total_files == 0:
            type_label = {"video": "Videos", "photo": "Photos", "other": "Other Files"}.get(filter_mode, "Files")
            await status_msg.edit(
                f"⚠️ **No {type_label} Found!**\nNo files matched your filter (`{filter_mode.upper()}`).",
                buttons=None,
            )
            return

        total_size = sum(f.stat().st_size for f in files_to_upload)
        is_multi_file = total_files > 1

        if is_multi_file:
            filter_badge_text = f"\n🎯 **Filter Active**: `{filter_mode.upper()} ONLY` ({total_files} files selected)" if filter_mode != "all" else ""
            await status_msg.edit(
                f"📂 **Processing {total_files} files** in this Mega share.{filter_badge_text}\n"
                "⏳ *Beginning smart batch upload...*",
                buttons=make_stop_btn(user_id),
            )
            await asyncio.sleep(1.0)

        # Smart Album Maker & Upload pipeline for CLI fallback
        uploader = TelethonUploader(event.client)
        album_paths = []
        album_captions = []
        album_size = 0
        ALBUM_MAX_SIZE = 100 * 1024 * 1024
        ALBUM_MAX_ITEMS = 10

        async def flush_album_cli():
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

            # Check Over-size (2GB limit)
            max_size_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
            if file_size > max_size_bytes:
                await flush_album_cli()
                await event.reply(
                    f"📄 **{file_path.name}**\n\n"
                    f"💾 **Size**: {format_bytes(file_size)}\n"
                    f"🛑 **Direct Bot Upload Limit**: {config.MAX_FILE_SIZE_MB} MB\n\n"
                    f"⚠️ This file exceeds Telegram's 2000 MB limit and was skipped."
                )
                try:
                    file_path.unlink()
                except Exception:
                    pass
                continue

            can_be_album_item = (
                is_multi_file
                and file_size < ALBUM_MAX_SIZE
                and (is_photo_file(file_path) or is_video_file(file_path) or total_files >= 3)
            )

            if can_be_album_item:
                if album_size + file_size > ALBUM_MAX_SIZE or len(album_paths) >= ALBUM_MAX_ITEMS:
                    await flush_album_cli()

                album_paths.append(file_path)
                album_captions.append(f"📁 **{file_path.name}**\n☁️ `Mega.nz`")
                album_size += file_size
                continue

            await flush_album_cli()

            thumb_path = None
            if is_video_file(file_path):
                try:
                    meta = get_video_metadata(file_path)
                    if meta and meta.get("duration", 0) > 0:
                        thumb_path = await generate_video_thumbnail(file_path, meta["duration"])
                except Exception as thumb_err:
                    logger.warning(f"Failed to generate thumbnail for {file_path.name}: {thumb_err}")

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

        await flush_album_cli()

        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        await event.reply(
            f"✅ **Mega Transfer Complete!**\n"
            f"🎯 **Filter**: `{filter_mode.upper()}`\n"
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
        if "local_dir" in locals() and local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)
        if slot_acquired:
            queue_mgr.release_worker(user_id)
