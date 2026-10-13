import asyncio
import base64
import json
import os
import re
import shutil
import struct
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
import pyaes
from telethon import Button, events

import config
from core.media import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    generate_video_thumbnail,
    get_video_metadata,
    is_photo_file,
    is_video_file,
)
from core.database import db
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


def get_channel_button(channel_id: Optional[int]):
    """Return inline button linking directly to the Storage Channel."""
    if not channel_id:
        return None
    raw_str = str(channel_id).strip()
    if raw_str.startswith("-100"):
        cid = raw_str[4:]
    elif raw_str.startswith("-"):
        cid = raw_str[1:]
    else:
        cid = raw_str
    url = f"https://t.me/c/{cid}/1"
    return Button.url("🎬 Open Your Storage Channel", url)


def render_mega_progress(
    action: str,
    name: str,
    current: int,
    total: int,
    speed: float,
    folder_header: str = "",
    file_prefix: str = "",
    skipped_count: int = 0,
) -> str:
    """Render a bold, clear, standardized transfer card for Telegram progress edits."""
    bar_width = 12
    speed_mb = speed / (1024 * 1024) if speed > 0 else 0.0
    clean_action = action.replace("**", "").strip()

    if total > 0:
        percent = (current / total) * 100.0
        filled = min(bar_width, int(bar_width * current // total))
        bar = "▰" * filled + "▱" * (bar_width - filled)
        remaining = max(0, total - current)
        eta = remaining / speed if speed > 0 else 0
        eta_str = format_duration(eta)
    else:
        percent = 0.0
        bar = "▱" * bar_width
        eta_str = "--:--"

    lines = [
        f"**{clean_action}**\n",
    ]

    prefix_str = f"`{file_prefix.strip()}` " if file_prefix and file_prefix.strip() else ""
    lines.append(f"🎬 **File**: {prefix_str}`{name}`")

    if folder_header and folder_header.strip():
        lines.append(f"{folder_header.strip()}")

    lines.extend([
        f"[{bar}] **{percent:.1f}%** (`{format_bytes(current)}` / `{format_bytes(total)}`)",
        f"⚡ **Speed**: `{speed_mb:.2f} MB/s` • ⏱ **ETA**: `{eta_str}`",
    ])

    if skipped_count > 0:
        lines.append(f"⏩ **Skipped**: `{skipped_count} file(s)` (already in storage)")

    return "\n".join(lines)



def sanitize_filename(name: str) -> str:
    """Remove unsafe filesystem characters from filename."""
    clean = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return clean or "unnamed_file"


def get_aes_ctr_decryptor_at_offset(file_key: bytes, iv_int: int, byte_offset: int = 0):
    """
    Return a fast decrypt function for AES-128-CTR initialized at an arbitrary byte_offset.
    Supports multi-stream parallel Range chunk downloads with perfect mathematical keystream alignment.
    Prefers cryptography (OpenSSL AES-NI C bindings, ~1 GB/s),
    then pycryptodome (C extension, ~500 MB/s),
    falling back to pyaes (pure Python).
    """
    base_iv_int = iv_int << 64
    block_idx = byte_offset // 16
    rem = byte_offset % 16
    slice_iv_int = (base_iv_int + block_idx) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
    slice_iv_bytes = slice_iv_int.to_bytes(16, "big")

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend

        cipher = Cipher(algorithms.AES(file_key), modes.CTR(slice_iv_bytes), backend=default_backend())
        decryptor = cipher.decryptor()
        if rem > 0:
            decryptor.update(b"\x00" * rem)
        return decryptor.update
    except ImportError:
        pass

    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Counter

        ctr = Counter.new(128, initial_value=slice_iv_int)
        cipher = AES.new(file_key, AES.MODE_CTR, counter=ctr)
        if rem > 0:
            cipher.decrypt(b"\x00" * rem)
        return cipher.decrypt
    except ImportError:
        pass

    import pyaes

    counter = pyaes.Counter(initial_value=slice_iv_int)
    aes_ctr = pyaes.AESModeOfOperationCTR(file_key, counter=counter)
    if rem > 0:
        aes_ctr.decrypt(b"\x00" * rem)
    return aes_ctr.decrypt


def get_aes_ctr_decryptor(file_key: bytes, iv_int: int):
    """Return a fast decrypt function for AES-128-CTR starting from byte 0."""
    return get_aes_ctr_decryptor_at_offset(file_key, iv_int, 0)


def detect_mega_engine() -> Tuple[Optional[str], Optional[list[str]]]:
    """Detect available Mega CLI download engine on host system."""
    if shutil.which("mega-get"):
        return "mega-cmd", ["mega-get", "--ignore-quota-warn"]
    if shutil.which("megadl"):
        return "megatools", ["megadl"]
    return None, None


def normalize_mega_url_for_engine(url: str, engine_name: str) -> str:
    """Ensure Mega URL format matches engine expectations."""
    if engine_name == "megatools":
        m_folder = re.search(r"mega\.(?:nz|co\.nz|io)/folder/([a-zA-Z0-9_\-]+)#([a-zA-Z0-9_\-]+)", url)
        if m_folder:
            return f"https://mega.co.nz/#F!{m_folder.group(1)}!{m_folder.group(2)}"
        m_file = re.search(r"mega\.(?:nz|co\.nz|io)/file/([a-zA-Z0-9_\-]+)#([a-zA-Z0-9_\-]+)", url)
        if m_file:
            return f"https://mega.co.nz/#!{m_file.group(1)}!{m_file.group(2)}"
    return url


def _b64_dec(data: str) -> bytes:
    data += "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data)


def _a32_to_str(a: tuple) -> bytes:
    return struct.pack(">%dI" % len(a), *a)


def _str_to_a32(b: bytes) -> tuple:
    if len(b) % 4:
        b += b"\0" * (4 - len(b) % 4)
    return struct.unpack(">%dI" % (len(b) // 4), b)


# ---------------- FAST PRE-DOWNLOAD FOLDER & TREE INSPECTOR ---------------- #

async def inspect_mega_folder(url: str) -> Optional[dict]:
    """
    Inspect a public Mega folder via Mega API in pure Python in seconds.
    Builds the full directory tree, maps nested subfolders, categorizes
    videos, photos, others per subfolder and globally, and derives keys for streaming.
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

        # 1. Parse and decrypt all directory nodes
        dirs = {}
        for node in nodes:
            if node.get("t") == 1:
                k_val = node.get("k", "")
                if ":" in k_val:
                    try:
                        enc_k = _b64_dec(k_val.split(":")[1])
                        dir_key = (
                            decrypt_ecb(enc_k[:16])
                            if len(enc_k) == 16
                            else decrypt_ecb(enc_k[:16]) + decrypt_ecb(enc_k[16:32])
                        )
                        enc_attr = _b64_dec(node["a"])

                        # CBC decryptor for dir attributes
                        try:
                            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                            from cryptography.hazmat.backends import default_backend

                            cipher_cbc = Cipher(
                                algorithms.AES(dir_key[:16]), modes.CBC(b"\0" * 16), backend=default_backend()
                            )
                            dec_attr = cipher_cbc.decryptor().update(enc_attr)
                        except ImportError:
                            try:
                                from Crypto.Cipher import AES

                                dec_attr = AES.new(dir_key[:16], AES.MODE_CBC, iv=b"\0" * 16).decrypt(enc_attr)
                            except ImportError:
                                import pyaes

                                aes_cbc = pyaes.AESModeOfOperationCBC(dir_key[:16], iv=b"\0" * 16)
                                dec_attr = b"".join(
                                    aes_cbc.decrypt(enc_attr[i : i + 16]) for i in range(0, len(enc_attr), 16)
                                )

                        m_json = re.search(b"MEGA({.+?})", dec_attr)
                        if m_json:
                            name = json.loads(m_json.group(1).decode("utf-8", errors="ignore"))["n"]
                            dirs[node["h"]] = {
                                "name": name.strip(),
                                "p": node.get("p"),
                                "h": node["h"],
                                "children": [],
                                "direct_videos": [],
                                "direct_photos": [],
                                "direct_others": [],
                                "all_videos": [],
                                "all_photos": [],
                                "all_others": [],
                                "videos": [],
                                "photos": [],
                                "others": [],
                                "path": "",
                                "active_children": [],
                                "total_files": 0,
                                "total_size": 0,
                            }
                    except Exception:
                        continue

        # Link parent-child directory hierarchy
        for h, d in dirs.items():
            p = d["p"]
            if p in dirs:
                dirs[p]["children"].append(h)

        # Identify root folder handle
        root_candidates = [h for h, d in dirs.items() if d["p"] not in dirs]
        root_h = root_candidates[0] if root_candidates else (list(dirs.keys())[0] if dirs else None)

        # Build full breadcrumb path for every directory starting from root
        def build_dir_paths(dh, current_path):
            if dh not in dirs:
                return
            dirs[dh]["path"] = current_path
            for ch in dirs[dh]["children"]:
                if ch in dirs:
                    ch_name = dirs[ch]["name"]
                    build_dir_paths(ch, f"{current_path} / {ch_name}")

        if root_h and root_h in dirs:
            build_dir_paths(root_h, dirs[root_h]["name"])
        else:
            for dh in dirs:
                dirs[dh]["path"] = dirs[dh]["name"]

        # 2. Parse and decrypt all file nodes
        global_videos = []
        global_photos = []
        global_others = []

        for node in nodes:
            if node.get("t") != 0:
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

                parent_h = node.get("p")
                if parent_h in dirs:
                    file_folder_path = dirs[parent_h]["path"]
                elif root_h and root_h in dirs:
                    file_folder_path = dirs[root_h]["path"]
                else:
                    file_folder_path = "Main Folder"

                item = {
                    "name": name,
                    "size": size,
                    "h": node["h"],
                    "file_key": file_key,
                    "iv_int": iv_int,
                    "p": parent_h,
                    "folder_name": file_folder_path,
                }

                # Global and direct folder lists
                if ext in VIDEO_EXTENSIONS:
                    global_videos.append(item)
                    if parent_h in dirs:
                        dirs[parent_h]["direct_videos"].append(item)
                elif ext in IMAGE_EXTENSIONS:
                    global_photos.append(item)
                    if parent_h in dirs:
                        dirs[parent_h]["direct_photos"].append(item)
                else:
                    global_others.append(item)
                    if parent_h in dirs:
                        dirs[parent_h]["direct_others"].append(item)

            except Exception:
                continue

        # 3. Recursive rollup: Compute all descendants and totals for every directory
        visited = set()
        def compute_recursive_files(dh):
            if dh not in dirs or dh in visited:
                return [], [], []
            visited.add(dh)
            d = dirs[dh]
            vids = list(d["direct_videos"])
            photos = list(d["direct_photos"])
            others = list(d["direct_others"])
            for ch in d["children"]:
                ch_v, ch_p, ch_o = compute_recursive_files(ch)
                vids.extend(ch_v)
                photos.extend(ch_p)
                others.extend(ch_o)
            d["all_videos"] = vids
            d["all_photos"] = photos
            d["all_others"] = others
            d["videos"] = vids
            d["photos"] = photos
            d["others"] = others
            d["total_files"] = len(vids) + len(photos) + len(others)
            d["total_size"] = sum(f["size"] for f in vids + photos + others)
            return vids, photos, others

        if root_h and root_h in dirs:
            compute_recursive_files(root_h)
        for dh in list(dirs.keys()):
            if dh not in visited:
                compute_recursive_files(dh)

        # Set active_children for every folder (only children that contain files directly or in subfolders)
        for dh, d in dirs.items():
            act_ch = [ch for ch in d["children"] if ch in dirs and dirs[ch]["total_files"] > 0]
            act_ch.sort(key=lambda x: dirs[x]["name"].lower())
            d["active_children"] = act_ch

        # Auto-focus to the first meaningful folder if intermediate roots are single empty wrappers
        current_root = root_h
        while (
            current_root
            and current_root in dirs
            and (len(dirs[current_root]["direct_videos"]) + len(dirs[current_root]["direct_photos"]) + len(dirs[current_root]["direct_others"])) == 0
            and len(dirs[current_root]["active_children"]) == 1
        ):
            current_root = dirs[current_root]["active_children"][0]
        if current_root:
            root_h = current_root

        # Determine if any subfolders exist anywhere in the hierarchy
        has_subfolders = any(len(d["active_children"]) > 0 for d in dirs.values())
        top_subfolders = dirs[root_h]["active_children"] if (root_h and root_h in dirs) else [
            dh for dh, d in dirs.items() if d["total_files"] > 0
        ]

        total_files = len(global_videos) + len(global_photos) + len(global_others)
        total_size = sum(f["size"] for f in global_videos + global_photos + global_others)

        return {
            "folder_id": folder_id,
            "folder_key": folder_key,
            "dirs": dirs,
            "root_h": root_h,
            "has_subfolders": has_subfolders,
            "top_subfolders": top_subfolders,
            "videos": global_videos,
            "photos": global_photos,
            "others": global_others,
            "total_files": total_files,
            "total_size": total_size,
        }
    except Exception as e:
        logger.warning(f"Error inspecting Mega folder via API: {e}", exc_info=True)
        return None


async def _download_mega_range_chunk(
    session: aiohttp.ClientSession,
    g_url: str,
    start_byte: int,
    end_byte: int,
    output_file_path: Path,
    part_id: int,
    file_key: bytes,
    iv_int: int,
    progress_tracker: dict,
    progress_callback,
    total_size: int,
    lock: asyncio.Lock,
    stop_event: Optional[asyncio.Event] = None,
    max_retries: int = 4,
) -> int:
    """Download and decrypt an individual Mega byte slice in parallel."""
    part_expected = (end_byte - start_byte) + 1
    part_downloaded = 0

    for attempt in range(max_retries):
        if stop_event and stop_event.is_set():
            return 0
        current_start = start_byte + part_downloaded
        if current_start > end_byte:
            break

        headers = {"Range": f"bytes={current_start}-{end_byte}"}
        timeout = aiohttp.ClientTimeout(total=1800, connect=30, sock_read=60)
        decrypt_func = get_aes_ctr_decryptor_at_offset(file_key, iv_int, current_start)

        try:
            async with session.get(g_url, headers=headers, timeout=timeout) as resp:
                if resp.status not in (200, 206):
                    raise RuntimeError(f"Part {part_id} HTTP status {resp.status}")
                if resp.status == 200 and part_expected < total_size:
                    raise RuntimeError("Server returned 200 (no Range support)")

                with open(output_file_path, "r+b") as f:
                    f.seek(current_start)
                    unreported = 0
                    async for chunk in resp.content.iter_chunked(512 * 1024):
                        if stop_event and stop_event.is_set():
                            return 0
                        plain = decrypt_func(chunk)
                        rem = (end_byte - f.tell()) + 1
                        if rem <= 0:
                            break
                        plain_chunk = plain[:rem]
                        f.write(plain_chunk)
                        chunk_len = len(plain_chunk)
                        part_downloaded += chunk_len
                        unreported += chunk_len

                        if unreported >= 512 * 1024 or part_downloaded >= part_expected:
                            async with lock:
                                progress_tracker["downloaded"] += unreported
                                progress_tracker["recent_bytes"] += unreported
                                now = time.monotonic()
                                elapsed = now - progress_tracker["last_time"]
                                if elapsed >= 1.2 or progress_tracker["downloaded"] >= total_size:
                                    instant = progress_tracker["recent_bytes"] / elapsed if elapsed > 0 else 0.0
                                    last_spd = progress_tracker.get("smoothed_speed", 0.0)
                                    speed = (0.7 * instant + 0.3 * last_spd) if last_spd > 0 else instant
                                    progress_tracker["smoothed_speed"] = speed
                                    progress_tracker["last_time"] = now
                                    progress_tracker["recent_bytes"] = 0
                                    if progress_callback:
                                        if asyncio.iscoroutinefunction(progress_callback):
                                            await progress_callback(progress_tracker["downloaded"], total_size, speed)
                                        else:
                                            progress_callback(progress_tracker["downloaded"], total_size, speed)
                            unreported = 0

            if part_downloaded >= part_expected:
                return part_downloaded

        except Exception as e:
            if "no Range support" in str(e):
                raise
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(1 + attempt)

    return part_downloaded


async def download_mega_node_stream(
    session: aiohttp.ClientSession,
    folder_id: str,
    node: dict,
    dest_path: Path,
    progress_callback=None,
    stop_event: Optional[asyncio.Event] = None,
) -> bool:
    """Download a single file node via high-speed parallel Mega Range streams and AES-128-CTR decryption."""
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

        # Multi-Stream Parallel Range engine for files >= 8MB (3x to 8x faster!)
        num_streams = min(8, max(4, target_size // (20 * 1024 * 1024))) if target_size >= 8 * 1024 * 1024 else 1

        if num_streams > 1:
            try:
                with open(dest_path, "wb") as f:
                    f.seek(target_size - 1)
                    f.write(b"\0")

                part_size = target_size // num_streams
                ranges = []
                for i in range(num_streams):
                    start = i * part_size
                    end = (start + part_size - 1) if i < num_streams - 1 else (target_size - 1)
                    ranges.append((start, end))

                progress_tracker = {
                    "downloaded": 0,
                    "recent_bytes": 0,
                    "last_time": time.monotonic(),
                }
                lock = asyncio.Lock()

                tasks = [
                    _download_mega_range_chunk(
                        session=session,
                        g_url=g_url,
                        start_byte=start,
                        end_byte=end,
                        output_file_path=dest_path,
                        part_id=i,
                        file_key=file_key,
                        iv_int=iv_int,
                        progress_tracker=progress_tracker,
                        progress_callback=progress_callback,
                        total_size=target_size,
                        lock=lock,
                        stop_event=stop_event,
                    )
                    for i, (start, end) in enumerate(ranges)
                ]
                await asyncio.gather(*tasks)

                if dest_path.exists() and dest_path.stat().st_size == target_size:
                    return True
            except Exception as par_err:
                logger.info(f"Mega parallel stream notice ({par_err}), falling back to single stream...")
                try:
                    if dest_path.exists():
                        dest_path.unlink()
                except Exception:
                    pass

        # Resilient Single-Stream Fallback with 512KB chunks
        decrypt_func = get_aes_ctr_decryptor_at_offset(file_key, iv_int, 0)
        timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=60)
        async with session.get(g_url, timeout=timeout) as stream_resp:
            if stream_resp.status not in (200, 206):
                logger.error(f"Mega CDN status {stream_resp.status} for {node['name']}")
                return False

            downloaded_bytes = 0
            last_callback_time = time.monotonic()
            last_callback_bytes = 0
            smoothed_speed = 0.0

            with open(dest_path, "wb") as f:
                async for chunk in stream_resp.content.iter_chunked(512 * 1024):
                    if stop_event and stop_event.is_set():
                        return False

                    plain_chunk = decrypt_func(chunk)
                    if downloaded_bytes + len(plain_chunk) > target_size:
                        plain_chunk = plain_chunk[: target_size - downloaded_bytes]

                    f.write(plain_chunk)
                    downloaded_bytes += len(plain_chunk)

                    now = time.monotonic()
                    elapsed = now - last_callback_time
                    if progress_callback and (elapsed >= 1.5 or downloaded_bytes >= target_size):
                        bytes_diff = downloaded_bytes - last_callback_bytes
                        instant = bytes_diff / elapsed if elapsed > 0 else 0.0
                        smoothed_speed = (0.7 * instant + 0.3 * smoothed_speed) if smoothed_speed > 0 else instant
                        last_callback_time = now
                        last_callback_bytes = downloaded_bytes
                        await progress_callback(downloaded_bytes, target_size, smoothed_speed)

        return dest_path.exists() and dest_path.stat().st_size == target_size
    except Exception as e:
        logger.error(f"Error streaming Mega node {node.get('name')}: {e}")
        if dest_path.exists():
            try:
                dest_path.unlink()
            except Exception:
                pass
        return False


# ---------------- INTERACTIVE MEGA EXPLORER SESSION MANAGER ---------------- #

class MegaSession:
    """Represents an active multi-subfolder Mega exploration session."""

    def __init__(
        self,
        session_id: str,
        user_id: int,
        url: str,
        folder_info: dict,
        status_msg,
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.url = url
        self.folder_info = folder_info
        self.status_msg = status_msg
        self.created_at = time.time()
        self.last_active = time.time()
        self.current_page = 0
        self.current_dir_h = folder_info.get("root_h")
        self.search_query = None
        self.search_results = []
        self.awaiting_search = False
        self.future = asyncio.get_running_loop().create_future()


class MegaExplorerManager:
    """Manages active sessions, interactive views, pagination, and callback routing."""

    def __init__(self):
        self.sessions: dict[str, MegaSession] = {}
        self.user_to_session: dict[int, str] = {}

    def cleanup_expired(self):
        """Remove sessions inactive for over 30 minutes."""
        now = time.time()
        expired_ids = [sid for sid, s in self.sessions.items() if now - s.last_active > 1800]
        for sid in expired_ids:
            s = self.sessions.pop(sid, None)
            if s and s.user_id in self.user_to_session:
                self.user_to_session.pop(s.user_id, None)

    def get_session(self, session_id: str) -> Optional[MegaSession]:
        self.cleanup_expired()
        return self.sessions.get(session_id)

    def has_awaiting_search(self, user_id: int) -> bool:
        sess_id = self.user_to_session.get(user_id)
        if not sess_id:
            return False
        sess = self.sessions.get(sess_id)
        return bool(sess and sess.awaiting_search)

    async def handle_search_input(self, event: events.NewMessage.Event):
        """Process keyword sent by user for folder name filtering across all depths."""
        user_id = event.sender_id
        sess_id = self.user_to_session.get(user_id)
        sess = self.sessions.get(sess_id)
        if not sess:
            return

        query = event.raw_text.strip()
        sess.awaiting_search = False
        sess.last_active = time.time()

        matches = []
        root_h = sess.folder_info.get("root_h")
        for dh, d in sess.folder_info["dirs"].items():
            if dh == root_h and len(sess.folder_info["dirs"]) > 1:
                continue
            if d.get("total_files", 0) == 0:
                continue
            if query.lower() in d["name"].lower():
                matches.append(dh)

        sess.search_query = query
        sess.search_results = matches
        await self.render_search_results(sess)
        try:
            await event.delete()
        except Exception:
            pass

    async def safe_edit_view(
        self,
        sess: MegaSession,
        text: str,
        buttons=None,
        event: Optional[events.CallbackQuery.Event] = None,
    ):
        """Safely edit explorer message via callback event or original status message without throwing."""
        if event and hasattr(event, "edit"):
            try:
                await event.edit(text, buttons=buttons)
                return
            except Exception:
                pass
        if sess and sess.status_msg:
            try:
                await sess.status_msg.edit(text, buttons=buttons)
            except Exception:
                pass

    async def render_folder(self, sess: MegaSession, dir_h: Optional[str] = None, page: int = 0, event=None):
        """Render interactive folder drill-down view showing path, child subfolders, and recursive download options."""
        sess.last_active = time.time()
        sess.awaiting_search = False

        root_h = sess.folder_info.get("root_h")
        if not dir_h:
            dir_h = sess.current_dir_h or root_h
        if not dir_h or dir_h not in sess.folder_info["dirs"]:
            dir_h = root_h

        sess.current_dir_h = dir_h
        sess.current_page = page
        d = sess.folder_info["dirs"].get(dir_h)
        if not d:
            return

        folder_path = d.get("path") or d["name"]
        active_children = d.get("active_children", [])
        total_children = len(active_children)

        tot_files = d.get("total_files", 0)
        tot_size = format_bytes(d.get("total_size", 0))

        vids = d.get("all_videos", d.get("videos", []))
        photos = d.get("all_photos", d.get("photos", []))
        others = d.get("all_others", d.get("others", []))

        num_v = len(vids)
        num_p = len(photos)
        num_o = len(others)

        v_sz = format_bytes(sum(f["size"] for f in vids))
        p_sz = format_bytes(sum(f["size"] for f in photos))
        o_sz = format_bytes(sum(f["size"] for f in others))

        text = (
            f"🗂️ **Mega Folder Explorer**\n\n"
            f"📁 **Path**: `{folder_path}`\n\n"
            f"📊 **Content (Includes Subfolders)**:\n"
        )
        if total_children > 0:
            text += f"• 📂 **Inner Subfolders**: `{total_children}`\n"
        if num_v > 0:
            text += f"• 🎬 **Videos**: `{num_v} Files` (~`{v_sz}`)\n"
        if num_p > 0:
            text += f"• 🖼️ **Photos**: `{num_p} Files` (~`{p_sz}`)\n"
        if num_o > 0:
            text += f"• 📄 **Others**: `{num_o} Files` (~`{o_sz}`)\n"
        text += f"• 💾 **Total**: `{tot_files} Files` • `{tot_size}`\n\n"

        if total_children > 0:
            text += "👇 **Subfolder chunne ke liye click karo (ya direct download karo):**"
        else:
            text += "👇 **Download karne ke liye option chuno:**"

        buttons = []

        # 1. Child Subfolder Buttons (Paginated, 6 per page)
        if total_children > 0:
            PAGE_SIZE = 6
            total_pages = max(1, (total_children + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(0, min(page, total_pages - 1))
            sess.current_page = page

            start_idx = page * PAGE_SIZE
            end_idx = min(start_idx + PAGE_SIZE, total_children)
            page_children = active_children[start_idx:end_idx]

            for ch_h in page_children:
                ch = sess.folder_info["dirs"].get(ch_h)
                if not ch:
                    continue
                raw_name = ch["name"]
                disp_name = (raw_name[:20] + "…") if len(raw_name) > 20 else raw_name
                tot = ch.get("total_files", 0)
                sz_str = format_bytes(ch.get("total_size", 0))
                btn_text = f"📂 {disp_name} ({tot} • {sz_str})"
                buttons.append([Button.inline(btn_text, data=f"m_cd:{sess.session_id}:{ch_h}")])

            if total_pages > 1:
                nav_row = []
                if page > 0:
                    nav_row.append(Button.inline("⬅️ Prev", data=f"m_cpage:{sess.session_id}:{page - 1}"))
                nav_row.append(Button.inline(f"📄 {page + 1}/{total_pages}", data=f"m_cpage:{sess.session_id}:{page}"))
                if page < total_pages - 1:
                    nav_row.append(Button.inline("Next ➡️", data=f"m_cpage:{sess.session_id}:{page + 1}"))
                buttons.append(nav_row)

        # 2. Download Action Buttons for THIS folder (Recursive)
        act_row1 = []
        if num_v > 0:
            act_row1.append(Button.inline(f"🎬 Videos ({num_v})", data=f"m_act:{sess.session_id}:{dir_h}:video"))
        if num_p > 0:
            act_row1.append(Button.inline(f"🖼️ Photos ({num_p})", data=f"m_act:{sess.session_id}:{dir_h}:photo"))
        if act_row1:
            buttons.append(act_row1)

        act_row2 = []
        if num_o > 0:
            act_row2.append(Button.inline(f"📄 Others ({num_o})", data=f"m_act:{sess.session_id}:{dir_h}:other"))
        if tot_files > 0:
            scope_label = "This Folder" if dir_h != root_h else "Full Share"
            act_row2.append(Button.inline(f"📁 Download {scope_label} ({tot_files})", data=f"m_act:{sess.session_id}:{dir_h}:all"))
        if act_row2:
            buttons.append(act_row2)

        # 3. Hierarchy Navigation Buttons (Up / Parent & Root)
        parent_h = d.get("p")
        nav_controls = []
        if parent_h and parent_h in sess.folder_info["dirs"] and dir_h != root_h:
            nav_controls.append(Button.inline("⬆️ Parent Folder", data=f"m_cd:{sess.session_id}:{parent_h}"))
        if dir_h != root_h:
            nav_controls.append(Button.inline("🏠 Root Folder", data=f"m_cd:{sess.session_id}:{root_h}"))
        if nav_controls:
            buttons.append(nav_controls)

        # 4. Utilities Row (Delivery Mode Toggle & Search)
        deliv_mode = await db.get_user_delivery_mode(sess.user_id)
        deliv_label = "🛡️ Mode: Channel Only" if deliv_mode == "channel" else "📬 Mode: Channel + DM"
        buttons.append([
            Button.inline(f"{deliv_label} 🔄", data=f"m_togdeliv:{sess.session_id}"),
            Button.inline("🔍 Search by Name", data=f"m_srch:{sess.session_id}"),
        ])

        # 5. Cancel Button
        buttons.append([Button.inline("❌ Cancel", data=f"m_cancel:{sess.session_id}")])

        await self.safe_edit_view(sess, text, buttons, event=event)

    # Backward compatibility helpers
    async def render_explorer(self, sess: MegaSession, page: int = 0, event=None):
        await self.render_folder(sess, dir_h=sess.folder_info.get("root_h"), page=page, event=event)

    async def render_folder_details(self, sess: MegaSession, dir_h: str, event=None):
        await self.render_folder(sess, dir_h=dir_h, page=0, event=event)

    async def render_home(self, sess: MegaSession, event=None):
        await self.render_folder(sess, dir_h=sess.folder_info.get("root_h"), page=0, event=event)

    async def render_search_results(self, sess: MegaSession, event=None):
        """Render results from global keyword search across all depths."""
        sess.last_active = time.time()
        matches = sess.search_results or []
        if not matches:
            text = (
                f"🔍 **Search Results for** `\"{sess.search_query}\"`\n\n"
                f"❌ Koi matching folder nahi mila.\n\n"
                f"💡 _Try searching with another keyword or check spelling._"
            )
            buttons = [
                [Button.inline("🔍 Try Another Search", data=f"m_srch:{sess.session_id}")],
                [Button.inline("🔙 Back to Folder", data=f"m_cd:{sess.session_id}:{sess.current_dir_h}")],
                [Button.inline("❌ Cancel", data=f"m_cancel:{sess.session_id}")],
            ]
            await self.safe_edit_view(sess, text, buttons, event=event)
            return

        text = (
            f"🔍 **Search Results for** `\"{sess.search_query}\"`\n"
            f"Found **{len(matches)}** matching folder(s):\n\n"
            f"👇 Click any folder to explore or download:"
        )
        buttons = []
        for sf_h in matches[:8]:
            d = sess.folder_info["dirs"][sf_h]
            path_str = d.get("path") or d["name"]
            disp_path = (path_str[:26] + "…") if len(path_str) > 26 else path_str
            tot = d.get("total_files", 0)
            sz_str = format_bytes(d.get("total_size", 0))
            buttons.append([Button.inline(f"📂 {disp_path} ({tot} • {sz_str})", data=f"m_cd:{sess.session_id}:{sf_h}")])

        buttons.append([
            Button.inline("🔍 Search Again", data=f"m_srch:{sess.session_id}"),
            Button.inline("🔙 Back to Explorer", data=f"m_cd:{sess.session_id}:{sess.current_dir_h}"),
        ])
        buttons.append([Button.inline("❌ Cancel", data=f"m_cancel:{sess.session_id}")])

        await self.safe_edit_view(sess, text, buttons, event=event)

    async def handle_callback(self, event: events.CallbackQuery.Event, client, active_tasks: dict):
        """Unified callback handler for all Mega explorer events."""
        try:
            await event.answer()
        except Exception:
            pass

        data = event.data.decode("utf-8")
        parts = data.split(":")
        action_prefix = parts[0]
        sess_id = parts[1] if len(parts) > 1 else ""

        sess = self.get_session(sess_id)
        if not sess:
            try:
                await event.answer("⚠️ Session expired (30 min)! Please resend the link.", alert=True)
            except Exception:
                pass
            return

        if event.sender_id != sess.user_id:
            try:
                await event.answer("⛔ This session is for another user!", alert=True)
            except Exception:
                pass
            return

        sess.last_active = time.time()

        if action_prefix in ("m_cd", "m_dir"):
            dir_h = parts[2] if len(parts) > 2 else sess.folder_info.get("root_h")
            await self.render_folder(sess, dir_h=dir_h, page=0, event=event)

        elif action_prefix in ("m_cpage", "m_nav"):
            page = int(parts[2]) if len(parts) > 2 else 0
            await self.render_folder(sess, dir_h=sess.current_dir_h, page=page, event=event)

        elif action_prefix == "m_home":
            await self.render_folder(sess, dir_h=sess.folder_info.get("root_h"), page=0, event=event)

        elif action_prefix == "m_srch":
            sess.awaiting_search = True
            text = (
                f"🔍 **Search Mega Subfolders (All Depths)**\n\n"
                f"Aapko kaunsa folder chahiye? Chat mein folder ka naam ya keyword bhejo (e.g., `VIP`, `Season`, `Episode`):"
            )
            buttons = [
                [Button.inline("🔙 Back to Folder", data=f"m_cd:{sess.session_id}:{sess.current_dir_h}")],
                [Button.inline("❌ Cancel", data=f"m_cancel:{sess.session_id}")],
            ]
            await self.safe_edit_view(sess, text, buttons, event=event)

        elif action_prefix == "m_cancel":
            if not sess.future.done():
                sess.future.set_result(("cancel", None))
            await self.safe_edit_view(sess, "❌ **Mega exploration cancelled by user.**", None, event=event)

        elif action_prefix == "m_act":
            dir_h = parts[2]
            act_type = parts[3]
            label_map = {"video": "Videos", "photo": "Photos", "other": "Others", "all": "Full Folder"}
            try:
                await event.answer(f"🚀 Starting {label_map.get(act_type, 'Files')} download...")
            except Exception:
                pass
            if not sess.future.done():
                sess.future.set_result((act_type, dir_h))

        elif action_prefix == "m_allact":
            act_type = parts[2]
            label_map = {"video": "All Videos", "photo": "All Photos", "all": "Everything"}
            try:
                await event.answer(f"🚀 Starting {label_map.get(act_type, 'Files')} download...")
            except Exception:
                pass
            if not sess.future.done():
                sess.future.set_result((act_type, sess.folder_info.get("root_h")))

        elif action_prefix == "m_togdeliv":
            curr_mode = await db.get_user_delivery_mode(sess.user_id)
            new_mode = "channel" if curr_mode == "both" else "both"
            await db.set_user_delivery_mode(sess.user_id, new_mode)
            mode_text = "🛡️ CHANNEL ONLY (Vault)" if new_mode == "channel" else "📬 CHANNEL + BOT DM"
            try:
                await event.answer(f"✅ Mode switched to: {mode_text}", alert=False)
            except Exception:
                pass
            await self.render_folder(sess, dir_h=sess.current_dir_h, page=sess.current_page, event=event)

        elif action_prefix == "m_reopen":
            try:
                await event.answer("🔄 Reopening Mega Subfolder Explorer...")
            except Exception:
                pass
            sess.future = asyncio.get_running_loop().create_future()
            sess.status_msg = await event.reply("🔍 **Loading Mega Subfolder Explorer...**", buttons=make_stop_btn(sess.user_id))
            await self.render_folder(sess, dir_h=sess.current_dir_h or sess.folder_info.get("root_h"), page=0)

            task = asyncio.create_task(
                execute_session_download(
                    event=event,
                    sess=sess,
                    active_tasks=active_tasks,
                )
            )
            active_tasks[sess.user_id] = task


mega_mgr = MegaExplorerManager()


# ---------------- EXECUTION PIPELINE FOR SESSIONS ---------------- #

async def execute_session_download(
    event: events.NewMessage.Event,
    sess: MegaSession,
    active_tasks: dict,
):
    """Wait for user selection in session and execute the targeted download."""
    user_id = sess.user_id
    status_msg = sess.status_msg
    folder_info = sess.folder_info

    try:
        # Wait up to 30 minutes for user choice
        chosen_action, chosen_dir_h = await asyncio.wait_for(sess.future, timeout=1800)
    except asyncio.TimeoutError:
        try:
            await status_msg.edit("⏱️ **Session timed out (30 min).** Resend link if needed.", buttons=None)
        except Exception:
            pass
        return
    finally:
        pass

    if chosen_action == "cancel":
        try:
            await status_msg.edit("❌ **Cancelled by user.**", buttons=None)
        except Exception:
            pass
        return

    # Determine targeted nodes
    if chosen_dir_h:
        target_dir = folder_info["dirs"].get(chosen_dir_h)
        folder_display_name = (target_dir.get("path") or target_dir.get("name")) if target_dir else "Subfolder"
        if chosen_action == "video":
            selected_nodes = target_dir.get("all_videos", target_dir.get("videos", []))
        elif chosen_action == "photo":
            selected_nodes = target_dir.get("all_photos", target_dir.get("photos", []))
        elif chosen_action == "other":
            selected_nodes = target_dir.get("all_others", target_dir.get("others", []))
        else:
            selected_nodes = (
                target_dir.get("all_videos", target_dir.get("videos", []))
                + target_dir.get("all_photos", target_dir.get("photos", []))
                + target_dir.get("all_others", target_dir.get("others", []))
            )
    else:
        folder_display_name = "Global Share"
        if chosen_action == "video":
            selected_nodes = folder_info["videos"]
        elif chosen_action == "photo":
            selected_nodes = folder_info["photos"]
        elif chosen_action == "other":
            selected_nodes = folder_info["others"]
        else:
            selected_nodes = folder_info["videos"] + folder_info["photos"] + folder_info["others"]

    if not selected_nodes:
        type_label = {"video": "Videos", "photo": "Photos", "other": "Other Files"}.get(chosen_action, "Files")
        await status_msg.edit(
            f"⚠️ **No {type_label} Found in** `{folder_display_name}`!\n\n"
            f"💡 _Please pick another option._",
            buttons=[[Button.inline("🔙 Choose Another Folder", data=f"m_reopen:{sess.session_id}")]],
        )
        return

    # Execute stream download & upload
    try:
        await stream_and_upload_nodes(
            event=event,
            folder_info=folder_info,
            selected_nodes=selected_nodes,
            status_msg=status_msg,
            user_id=user_id,
            folder_name=folder_display_name,
            filter_mode=chosen_action,
            sess_id=sess.session_id,
        )
    except Exception as e:
        logger.error(f"Error executing stream download in session: {e}", exc_info=True)
        try:
            await status_msg.edit(
                f"❌ **Transfer Error**: `{str(e)}`\n\n"
                "💡 _Aap `/delivery` se mode check kar sakte hain ya link dobara send kar sakte hain._",
                buttons=[[Button.inline("🔙 Try Again", data=f"m_reopen:{sess.session_id}")]],
            )
        except Exception:
            pass


async def stream_and_upload_nodes(
    event: events.NewMessage.Event,
    folder_info: dict,
    selected_nodes: list,
    status_msg,
    user_id: int,
    folder_name: str,
    filter_mode: str,
    sess_id: Optional[str] = None,
):
    """
    Stream download selected nodes directly from Mega and upload to Telegram.
    Processes folders sequentially (Folder-by-Folder) and silently skips cached files.
    """
    total_selected = len(selected_nodes)
    total_selected_size = sum(n.get("size", 0) for n in selected_nodes)
    is_multi_file = total_selected > 1

    # Group selected nodes by subfolder (preserves folder hierarchy)
    folder_groups = OrderedDict()
    for node in selected_nodes:
        fn = node.get("folder_name") or folder_name or "Main Folder"
        if fn not in folder_groups:
            folder_groups[fn] = []
        folder_groups[fn].append(node)

    total_folders = len(folder_groups)
    delivery_mode = await db.get_user_delivery_mode(user_id)

    safe_id = f"{int(time.time())}_{user_id}"
    local_dir = config.DOWNLOAD_DIR / f"mega_{safe_id}"
    local_dir.mkdir(parents=True, exist_ok=True)
    stop_monitor = asyncio.Event()

    # Transfer statistics
    uploaded_count = 0
    uploaded_size = 0
    skipped_count = 0
    skipped_size = 0

    try:
        filter_label = filter_mode.upper()
        folder_overview = f"📂 **Target**: `{folder_name}` [{filter_label} ONLY]"
        if total_folders > 1:
            folder_overview += f"\n📁 **Folders**: `{total_folders}` (Sequential Folder-by-Folder)"

        try:
            await status_msg.edit(
                f"🚀 **Starting Mega Download...**\n"
                f"{folder_overview}\n"
                f"📦 **Selected**: `{total_selected} Files` • `{format_bytes(total_selected_size)}`\n"
                "⏳ *Processing folder-by-folder with high-speed direct streaming...*",
                buttons=make_stop_btn(user_id),
            )
        except Exception:
            pass
        await asyncio.sleep(1.0)

        uploader = TelethonUploader(event.client)
        target_chat_id = config.PRIVATE_CHAT_ID if config.PRIVATE_CHAT_ID else event.chat_id
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
                    pct = (current / total) * 100.0 if total > 0 else 0.0
                    bar_w = 12
                    filled = min(bar_w, int(bar_w * current // total)) if total > 0 else 0
                    bar = "▰" * filled + "▱" * (bar_w - filled)
                    txt = (
                        f"📤 **Uploading Album ({album_count} {item_label}) to Telegram**\n\n"
                        f"[{bar}] **{pct:.1f}%** ({int(current)}/{total} files)\n"
                        f"⚡ *Sending high-speed album batch...*"
                    )
                    try:
                        asyncio.create_task(status_msg.edit(txt, buttons=make_stop_btn(user_id)))
                    except Exception:
                        pass

            try:
                await status_msg.edit(
                    f"📤 **Uploading Album ({album_count} {item_label}) to Telegram...**\n"
                    f"⚡ *Processing batch of {album_count} files...*",
                    buttons=make_stop_btn(user_id),
                )
                msgs = await uploader.upload_album(
                    chat_id=target_chat_id,
                    file_paths=album_paths,
                    captions=album_captions,
                    progress_callback=album_item_progress,
                    workers=12,
                )
                # Mode B: Forward to user DM if target is storage channel and user wants DM delivery
                if config.PRIVATE_CHAT_ID and msgs and delivery_mode == "both":
                    try:
                        await event.client.forward_messages(event.chat_id, msgs)
                    except Exception as fwd_err:
                        logger.warning(f"Could not forward album to user chat: {fwd_err}")

                # Save cache records for each uploaded album item
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

                # Delete successfully uploaded files
                for p in album_paths:
                    try:
                        if p.exists():
                            p.unlink()
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
                                await event.client.forward_messages(event.chat_id, sent)
                            except Exception as fwd_err:
                                logger.warning(f"Could not forward fallback to user chat: {fwd_err}")
                        if sent and k:
                            await db.save_cached_file(k, p.name, sz, target_chat_id, sent.id)
                            uploaded_count += 1
                            uploaded_size += sz
                    except Exception as fallback_err:
                        logger.error(f"Fallback upload failed for {p.name}: {fallback_err}")
                    finally:
                        try:
                            if p.exists():
                                p.unlink()
                        except Exception:
                            pass

            album_paths.clear()
            album_captions.clear()
            album_keys.clear()
            album_size = 0

        timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=60)
        connector = aiohttp.TCPConnector(limit=16, ttl_dns_cache=300)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, read_bufsize=1024 * 1024) as dl_session:
            global_file_counter = 0

            # FOLDER-BY-FOLDER SEQUENTIAL LOOP
            for folder_idx, (curr_folder_name, folder_nodes) in enumerate(folder_groups.items(), start=1):
                if stop_monitor.is_set():
                    break

                folder_total_size = sum(n.get("size", 0) for n in folder_nodes)

                # Sort smallest first within THIS specific folder
                folder_nodes = sorted(folder_nodes, key=lambda n: n.get("size", 0))

                folder_header = (
                    f"📁 **Folder [{folder_idx}/{total_folders}]**: `{curr_folder_name}`"
                    if total_folders > 1 else ""
                )

                # Upfront Zero-Bandwidth Pre-Check for this folder!
                missing_nodes, cached_nodes = await db.partition_nodes_by_cache(
                    event.client, config.PRIVATE_CHAT_ID, folder_nodes
                )
                if cached_nodes:
                    skipped_count += len(cached_nodes)
                    skipped_size += sum(n.get("size", 0) for n in cached_nodes)
                    logger.info(
                        f"Folder '{curr_folder_name}': {len(cached_nodes)} files already in Storage Channel! "
                        f"{len(missing_nodes)} files missing."
                    )

                if not missing_nodes:
                    # ALL files in this folder are already cached in Storage Channel!
                    # ZERO server calls to Mega!
                    skip_header = f"{folder_header.strip()}\n" if folder_header.strip() else ""
                    skip_txt = (
                        f"[{'▰' * 12}] **100%** ({format_bytes(folder_total_size)} / {format_bytes(folder_total_size)})\n\n"
                        f"{skip_header}"
                        "----------------------------------------------\n"
                        f"⚡ **Status**: All {len(cached_nodes)} files already saved in Storage Channel!\n"
                        f"⏩ **Skipped**: `{skipped_count} file(s)` (0 MB server bandwidth needed)"
                    )
                    try:
                        await status_msg.edit(skip_txt, buttons=make_stop_btn(user_id))
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
                    continue

                folder_nodes = missing_nodes
                folder_file_count = len(folder_nodes)

                for file_idx, node in enumerate(folder_nodes, start=1):
                    if stop_monitor.is_set():
                        break

                    global_file_counter += 1
                    file_size = node["size"]
                    file_name = sanitize_filename(node["name"])

                    if total_folders > 1:
                        file_prefix = f"[{file_idx}/{folder_file_count}] "
                    else:
                        file_prefix = f"[{global_file_counter}/{total_selected}] " if is_multi_file else ""

                    # Telegram 2GB limit check
                    max_size_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
                    if file_size > max_size_bytes:
                        await flush_album()
                        await event.reply(
                            f"📄 **{file_name}**\n\n"
                            f"📁 **Folder**: `{curr_folder_name}`\n"
                            f"💾 **Size**: {format_bytes(file_size)}\n"
                            f"🛑 **Direct Bot Upload Limit**: {config.MAX_FILE_SIZE_MB} MB\n\n"
                            f"⚠️ Exceeds Telegram's 2000 MB bot limit and was skipped."
                        )
                        continue

                    file_key = f"mg_{node['h']}"

                    # Storage Channel / DB cache check: Secondary safety net
                    cached_msg = await db.find_cached_media(
                        event.client, config.PRIVATE_CHAT_ID, file_key, file_name=file_name, file_size=file_size
                    )
                    if cached_msg:
                        logger.info(f"Storage cache hit for Mega node {node['h']} ({file_name})! Silently skipping...")
                        skipped_count += 1
                        skipped_size += file_size
                        skip_header = f"{folder_header.strip()}\n\n" if folder_header.strip() else ""
                        skip_txt = (
                            f"{skip_header}"
                            f"⚡ **{file_prefix}Skipped (Already in Storage)**\n\n"
                            f"🎬 `{file_name}`\n"
                            f"⏩ File pehle se storage channel mein hai — direct skip!"
                        )
                        try:
                            await status_msg.edit(skip_txt, buttons=make_stop_btn(user_id))
                        except Exception:
                            pass
                        continue

                    # Pre-download batch management:
                    node_ext = ("." + node["name"].split(".")[-1].lower()) if "." in node.get("name", "") else ""
                    node_is_photo = node_ext in IMAGE_EXTENSIONS
                    node_is_video = node_ext in VIDEO_EXTENSIONS
                    incoming_can_album = (
                        is_multi_file
                        and (
                            (node_is_photo and file_size <= PHOTO_ALBUM_MAX_SIZE)
                            or (node_is_video and file_size <= VIDEO_ALBUM_MAX_SIZE)
                        )
                    )

                    # If this incoming file is standalone, flush any pending album batch first
                    if not incoming_can_album and album_paths:
                        await flush_album()

                    # If album batch is already at 10 items or 200MB, upload that batch first before downloading this next file!
                    if incoming_can_album and (len(album_paths) >= ALBUM_MAX_ITEMS or album_size + file_size > TOTAL_ALBUM_MAX_SIZE):
                        await flush_album()

                    dest_path = local_dir / file_name
                    if dest_path.exists():
                        dest_path = local_dir / f"{dest_path.stem}_{node['h'][:4]}{dest_path.suffix}"

                    last_dl_edit = 0.0

                    async def dl_progress(current: int, total: int, speed: float):
                        nonlocal last_dl_edit
                        now = time.monotonic()
                        if now - last_dl_edit >= 1.5 or current == total:
                            last_dl_edit = now
                            txt = render_mega_progress(
                                action="📥 DOWNLOADING FROM MEGA [Step 1/2]",
                                name=file_name,
                                current=current,
                                total=total,
                                speed=speed,
                                folder_header=folder_header,
                                file_prefix=file_prefix,
                                skipped_count=skipped_count,
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
                    is_photo = is_photo_file(dest_path)
                    is_video = is_video_file(dest_path)

                    # Album Eligibility: Photos <= 100MB, Videos <= 20MB
                    can_be_album_item = (
                        is_multi_file
                        and (
                            (is_photo and actual_size <= PHOTO_ALBUM_MAX_SIZE)
                            or (is_video and actual_size <= VIDEO_ALBUM_MAX_SIZE)
                        )
                    )

                    if can_be_album_item:
                        album_paths.append(dest_path)
                        album_keys.append(file_key)
                        icon = "🎬" if is_video else "🖼️"
                        album_captions.append(f"{icon} `{dest_path.name}` (`{format_bytes(actual_size)}`)\n`#{file_key}` `#sz_{actual_size}`")
                        album_size += actual_size

                        # If batch reached 10 items or 200MB, upload this completed batch immediately!
                        if len(album_paths) >= ALBUM_MAX_ITEMS or album_size >= TOTAL_ALBUM_MAX_SIZE:
                            await flush_album()
                        continue

                    # Standalone media upload (Videos > 20MB, Documents, Archives, or Single Files)
                    await flush_album()

                    # INSTANTANEOUS SWITCH TO UPLOADING (0%) - Direct switch without frozen 100% download!
                    try:
                        init_up_txt = render_mega_progress(
                            action="📤 UPLOADING TO TELEGRAM [Step 2/2]",
                            name=dest_path.name,
                            current=0,
                            total=actual_size,
                            speed=0.0,
                            folder_header=folder_header,
                            file_prefix=file_prefix,
                            skipped_count=skipped_count,
                        )
                        await status_msg.edit(init_up_txt, buttons=make_stop_btn(user_id))
                    except Exception:
                        pass

                    thumb_path = None
                    if is_video_file(dest_path):
                        try:
                            meta = get_video_metadata(dest_path)
                            dur = meta.get("duration", 0) if meta else 0
                            thumb_ts = max(1, dur // 10) if dur > 0 else 1
                            thumb_path = generate_video_thumbnail(dest_path, timestamp_sec=thumb_ts)
                        except Exception as thumb_err:
                            logger.warning(f"Failed thumbnail for {dest_path.name}: {thumb_err}")

                    last_upload_edit = 0.0

                    async def upload_progress(current: int, total: int, speed: float):
                        nonlocal last_upload_edit
                        now = time.monotonic()
                        if now - last_upload_edit >= 1.5 or current == total:
                            last_upload_edit = now
                            txt = render_mega_progress(
                                action="📤 UPLOADING TO TELEGRAM [Step 2/2]",
                                name=dest_path.name,
                                current=current,
                                total=total,
                                speed=speed,
                                folder_header=folder_header,
                                file_prefix=file_prefix,
                                skipped_count=skipped_count,
                            )
                            try:
                                await status_msg.edit(txt, buttons=make_stop_btn(user_id))
                            except Exception:
                                pass

                    caption = f"🎬 `{dest_path.name}` (`{format_bytes(actual_size)}`)\n`#{file_key}` `#sz_{actual_size}`"

                    sent_msg = await uploader.upload_media(
                        chat_id=target_chat_id,
                        file_path=dest_path,
                        caption=caption,
                        thumb_path=thumb_path,
                        progress_callback=upload_progress,
                        workers=12,
                    )

                    if sent_msg:
                        if config.PRIVATE_CHAT_ID and delivery_mode == "both":
                            try:
                                await event.client.forward_messages(event.chat_id, sent_msg)
                            except Exception as fwd_err:
                                logger.warning(f"Could not forward media to user chat: {fwd_err}")
                        await db.save_cached_file(file_key, dest_path.name, actual_size, target_chat_id, sent_msg.id)
                        uploaded_count += 1
                        uploaded_size += actual_size

                    if thumb_path and thumb_path.exists():
                        try:
                            thumb_path.unlink()
                        except Exception:
                            pass
                    try:
                        dest_path.unlink()
                    except Exception:
                        pass

                # FLUSH ALBUM AT THE END OF THIS FOLDER (Prevents cross-folder mixing!)
                await flush_album()

        # Flush any remaining album items
        await flush_album()

        completion_buttons = []
        ch_btn = get_channel_button(config.PRIVATE_CHAT_ID)
        if ch_btn:
            completion_buttons.append([ch_btn])
        if sess_id:
            completion_buttons.append([
                Button.inline("🔄 Download Another Folder from Same Link", data=f"m_reopen:{sess_id}")
            ])

        mode_label = "📬 Channel + Bot DM" if delivery_mode == "both" else "🛡️ Storage Channel Only (Vault Mode)"
        summary_lines = [
            "🎉 **Mega Transfer Complete!**\n",
            f"📂 **Folder**: `{folder_name}`",
            f"🎯 **Filter**: `{filter_mode.upper()} ONLY`",
        ]
        if total_folders > 1:
            summary_lines.append(f"📁 **Folders Processed**: `{total_folders}` (Sequentially)")

        summary_lines.extend([
            f"📤 **Newly Uploaded**: `{uploaded_count} file(s)` ({format_bytes(uploaded_size)})",
            f"⏩ **Skipped (Already in Storage)**: `{skipped_count} file(s)` ({format_bytes(skipped_size)})",
            f"📊 **Total Handled**: `{total_selected} file(s)` ({format_bytes(total_selected_size)})",
        ])

        if config.PRIVATE_CHAT_ID:
            summary_lines.append(f"⚙️ **Delivery Mode**: `{mode_label}`")

        if ch_btn:
            summary_lines.append("\n👇 **Click below to view all files:**")
        else:
            summary_lines.append("\n💡 _Aap is link se doosre subfolders bhi bina link dobara send kiye download kar sakte hain!_")

        completion_text = "\n".join(summary_lines)
        sent_receipt = False
        if status_msg:
            try:
                await status_msg.edit(completion_text, buttons=completion_buttons if completion_buttons else None)
                sent_receipt = True
            except Exception:
                pass

        if not sent_receipt:
            try:
                await event.respond(completion_text, buttons=completion_buttons if completion_buttons else None)
            except Exception:
                try:
                    await event.reply(completion_text, buttons=completion_buttons if completion_buttons else None)
                except Exception:
                    pass

    finally:
        stop_monitor.set()
        if local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)


# ---------------- MAIN ENTRYPOINT FOR MEGA LINKS ---------------- #

async def process_mega_link(
    event: events.NewMessage.Event,
    url: str,
    filter_mode: str = "all",
    has_explicit_inline_filter: bool = False,
    pending_prompts: Optional[dict] = None,
):
    """
    Handle downloading from Mega.nz (file or folder) and uploading to Telegram.
    Inspects folder hierarchy in advance, launches the interactive Subfolder Explorer
    when multiple subfolders exist, or prompts flat category buttons for flat shares.
    """
    user_id = event.sender_id
    status_msg = None
    slot_acquired = False

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

        # Check if link is a folder
        if is_mega_folder_url(url):
            await status_msg.edit("📂 **Inspecting Mega folder & subfolders...**", buttons=make_stop_btn(user_id))
            folder_info = await inspect_mega_folder(url)

            if folder_info and folder_info["total_files"] > 0:
                top_subfolders = folder_info.get("top_subfolders", [])

                has_subfolders = folder_info.get("has_subfolders", False)

                # CASE A: SUBFOLDERS FOUND (Any depth: 1 subfolder, 50 subfolders, or nested)
                if has_subfolders:
                    session_id = uuid.uuid4().hex[:8]
                    sess = MegaSession(
                        session_id=session_id,
                        user_id=user_id,
                        url=url,
                        folder_info=folder_info,
                        status_msg=status_msg,
                    )
                    mega_mgr.sessions[session_id] = sess
                    mega_mgr.user_to_session[user_id] = session_id

                    # Check for inline subfolder search keyword (e.g. 'mega.nz/... Autumn video')
                    raw_text = event.raw_text
                    words = [w.lower() for w in raw_text.split() if not is_mega_url(w)]
                    matched_sf_h = None
                    for sf_h, d in folder_info["dirs"].items():
                        sf_name = d["name"].lower()
                        clean_name = re.sub(r"\[.*?\]|\(.*?\)", "", sf_name).strip()
                        if any(w in clean_name or clean_name in w for w in words if len(w) > 2):
                            matched_sf_h = sf_h
                            break

                    if matched_sf_h:
                        if any(w in words for w in ["video", "vid", "videos"]):
                            sess.future.set_result(("video", matched_sf_h))
                        elif any(w in words for w in ["photo", "image", "photos", "pic", "pics"]):
                            sess.future.set_result(("photo", matched_sf_h))
                        elif any(w in words for w in ["other", "others"]):
                            sess.future.set_result(("other", matched_sf_h))
                        else:
                            await mega_mgr.render_folder(sess, dir_h=matched_sf_h, page=0)
                    else:
                        # Open the Interactive Folder Explorer starting at root_h
                        await mega_mgr.render_folder(sess, dir_h=folder_info.get("root_h"), page=0)

                    # Execute session download loop
                    await execute_session_download(
                        event=event,
                        sess=sess,
                        active_tasks=active_tasks_ref,
                    )
                    return

                # CASE B: FLAT FOLDER (0 subfolders, all files directly in root)
                elif folder_info["total_files"] > 1:
                    raw_count = folder_info["total_files"]
                    num_videos = len(folder_info["videos"])
                    num_photos = len(folder_info["photos"])
                    num_others = len(folder_info["others"])
                    total_sz_readable = format_bytes(folder_info["total_size"])

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
                            await status_msg.edit("⏱️ **Selection timed out (5 min).** Resend link if needed.", buttons=None)
                            return
                        finally:
                            pending_prompts.pop(session_id, None)

                        if chosen_action == "cancel":
                            await status_msg.edit("❌ **Download cancelled by user.**", buttons=None)
                            return

                        filter_mode = chosen_action

                    if filter_mode == "video":
                        selected_nodes = folder_info["videos"]
                    elif filter_mode == "photo":
                        selected_nodes = folder_info["photos"]
                    elif filter_mode == "other":
                        selected_nodes = folder_info["others"]
                    else:
                        selected_nodes = folder_info["videos"] + folder_info["photos"] + folder_info["others"]

                    await stream_and_upload_nodes(
                        event=event,
                        folder_info=folder_info,
                        selected_nodes=selected_nodes,
                        status_msg=status_msg,
                        user_id=user_id,
                        folder_name="Mega Folder",
                        filter_mode=filter_mode,
                    )
                    return

        # ---------------- FALLBACK ENGINE FOR SINGLE FILES / CLI ---------------- #
        engine_name, base_cmd = detect_mega_engine()
        if not engine_name or not base_cmd:
            err_msg = (
                "❌ **Mega Downloader Engine Not Installed!**\n\n"
                "Host environment does not have `mega-cmd` or `megatools` installed.\n"
                "• **Debian/Ubuntu/Colab setup**:\n"
                "`sudo apt update && sudo apt install -y megatools`"
            )
            if status_msg:
                await status_msg.edit(err_msg, buttons=None)
            else:
                await event.reply(err_msg)
            return

        safe_id = f"{int(time.time())}_{user_id}"
        local_dir = config.DOWNLOAD_DIR / f"mega_{safe_id}"
        local_dir.mkdir(parents=True, exist_ok=True)

        engine_url = normalize_mega_url_for_engine(url, engine_name)
        if engine_name == "mega-cmd":
            cmd = base_cmd + [engine_url, str(local_dir)]
        else:
            cmd = base_cmd + ["--path", str(local_dir), engine_url]

        await status_msg.edit(f"**📥 DOWNLOADING FROM MEGA [Step 1/2]**\n`Engine: {engine_name}`", buttons=make_stop_btn(user_id))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            err_output = stderr.decode().strip() or stdout.decode().strip()
            await status_msg.edit(f"❌ **Mega Download Error:**\n`{err_output[-250:] if err_output else 'Unknown error'}`", buttons=None)
            return

        all_downloaded = sorted([f for f in local_dir.rglob("*") if f.is_file() and not f.name.startswith(".")])
        if not all_downloaded:
            await status_msg.edit("❌ **No files found in the downloaded Mega link.**", buttons=None)
            return

        uploader = TelethonUploader(event.client)
        target_chat_id = config.PRIVATE_CHAT_ID if config.PRIVATE_CHAT_ID else event.chat_id
        for f in all_downloaded:
            file_size = f.stat().st_size
            thumb_path = None
            if is_video_file(f):
                try:
                    meta = get_video_metadata(f)
                    if meta and meta.get("duration", 0) > 0:
                        thumb_path = await generate_video_thumbnail(f, meta["duration"])
                except Exception:
                    pass

            last_upload_edit = 0.0

            async def upload_progress_cli(current: int, total: int, speed: float):
                nonlocal last_upload_edit
                now = time.monotonic()
                if now - last_upload_edit >= 1.5 or current == total:
                    last_upload_edit = now
                    txt = render_mega_progress(
                        action="📤 UPLOADING TO TELEGRAM [Step 2/2]",
                        name=f.name,
                        current=current,
                        total=total,
                        speed=speed,
                    )
                    try:
                        await status_msg.edit(txt, buttons=make_stop_btn(user_id))
                    except Exception:
                        pass

            # Immediate instantaneous switch to Uploading (0%)
            try:
                init_up_txt = render_mega_progress(
                    action="📤 UPLOADING TO TELEGRAM [Step 2/2]",
                    name=f.name,
                    current=0,
                    total=file_size,
                    speed=0.0,
                )
                await status_msg.edit(init_up_txt, buttons=make_stop_btn(user_id))
            except Exception:
                pass

            file_key = f"mg_{abs(hash(f.name + str(file_size)))}"
            caption = f"🎬 `{f.name}` (`{format_bytes(file_size)}`)\n`#{file_key}` `#sz_{file_size}`"

            sent_msg = await uploader.upload_media(
                chat_id=target_chat_id,
                file_path=f,
                caption=caption,
                thumb_path=thumb_path,
                progress_callback=upload_progress_cli,
                workers=12,
            )
            if sent_msg:
                delivery_mode = await db.get_user_delivery_mode(user_id)
                if config.PRIVATE_CHAT_ID and delivery_mode == "both":
                    await event.client.forward_messages(event.chat_id, sent_msg)
                await db.save_cached_file(file_key, f.name, file_size, target_chat_id, sent_msg.id)

            if thumb_path and thumb_path.exists():
                try:
                    thumb_path.unlink()
                except Exception:
                    pass
            try:
                f.unlink()
            except Exception:
                pass

        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        await event.reply(f"✅ **Mega Transfer Complete!**\nDelivered {len(all_downloaded)} file(s).")

    except asyncio.CancelledError:
        logger.info(f"Mega task cancelled by user {user_id}")
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
    finally:
        if slot_acquired:
            queue_mgr.release_worker(user_id)


# Reference placeholder for active_tasks
active_tasks_ref: dict = {}


def set_active_tasks_ref(tasks_dict: dict):
    global active_tasks_ref
    active_tasks_ref = tasks_dict
