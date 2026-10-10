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
                                "videos": [],
                                "photos": [],
                                "others": [],
                            }
                    except Exception:
                        continue

        # Link parent-child directory hierarchy
        for h, d in dirs.items():
            p = d["p"]
            if p in dirs:
                dirs[p]["children"].append(h)

        # Identify top-level subfolders
        root_candidates = [h for h, d in dirs.items() if d["p"] not in dirs]
        root_h = root_candidates[0] if root_candidates else None
        top_subfolder_handles = dirs[root_h]["children"] if root_h else list(dirs.keys())

        # Map each top-level subfolder to all its descendant directory handles
        def get_descendants(dh):
            res = {dh}
            for ch in dirs.get(dh, {}).get("children", []):
                res.update(get_descendants(ch))
            return res

        top_folder_descendants = {sf_h: get_descendants(sf_h) for sf_h in top_subfolder_handles}
        dir_to_top_folder = {}
        for sf_h, desc_set in top_folder_descendants.items():
            for d_h in desc_set:
                dir_to_top_folder[d_h] = sf_h

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

                item = {
                    "name": name,
                    "size": size,
                    "h": node["h"],
                    "file_key": file_key,
                    "iv_int": iv_int,
                }

                # Global lists
                if ext in VIDEO_EXTENSIONS:
                    global_videos.append(item)
                elif ext in IMAGE_EXTENSIONS:
                    global_photos.append(item)
                else:
                    global_others.append(item)

                # Subfolder-level assignment
                parent_h = node.get("p")
                top_sf_h = dir_to_top_folder.get(parent_h)
                if top_sf_h and top_sf_h in dirs:
                    if ext in VIDEO_EXTENSIONS:
                        dirs[top_sf_h]["videos"].append(item)
                    elif ext in IMAGE_EXTENSIONS:
                        dirs[top_sf_h]["photos"].append(item)
                    else:
                        dirs[top_sf_h]["others"].append(item)

            except Exception:
                continue

        # Filter active top subfolders (those containing files directly or in nested subfolders)
        active_top_folders = [
            sf_h
            for sf_h in top_subfolder_handles
            if (len(dirs[sf_h]["videos"]) + len(dirs[sf_h]["photos"]) + len(dirs[sf_h]["others"])) > 0
        ]
        active_top_folders.sort(key=lambda x: dirs[x]["name"].lower())

        for sf_h in active_top_folders:
            d = dirs[sf_h]
            d["total_files"] = len(d["videos"]) + len(d["photos"]) + len(d["others"])
            d["total_size"] = sum(f["size"] for f in d["videos"] + d["photos"] + d["others"])

        total_files = len(global_videos) + len(global_photos) + len(global_others)
        total_size = sum(f["size"] for f in global_videos + global_photos + global_others)

        return {
            "folder_id": folder_id,
            "folder_key": folder_key,
            "dirs": dirs,
            "top_subfolders": active_top_folders,
            "videos": global_videos,
            "photos": global_photos,
            "others": global_others,
            "total_files": total_files,
            "total_size": total_size,
        }
    except Exception as e:
        logger.warning(f"Error inspecting Mega folder via API: {e}", exc_info=True)
        return None


async def download_mega_node_stream(
    session: aiohttp.ClientSession,
    folder_id: str,
    node: dict,
    dest_path: Path,
    progress_callback=None,
    stop_event: Optional[asyncio.Event] = None,
) -> bool:
    """Download a single file node directly via Mega CDN streaming and AES-128-CTR decryption."""
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
        self.current_dir_h = None
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
        """Process keyword sent by user for folder name filtering."""
        user_id = event.sender_id
        sess_id = self.user_to_session.get(user_id)
        sess = self.sessions.get(sess_id)
        if not sess:
            return

        query = event.raw_text.strip()
        sess.awaiting_search = False
        sess.last_active = time.time()

        matches = []
        for sf_h in sess.folder_info["top_subfolders"]:
            d = sess.folder_info["dirs"][sf_h]
            if query.lower() in d["name"].lower():
                matches.append(sf_h)

        sess.search_query = query
        sess.search_results = matches
        await self.render_search_results(sess)
        try:
            await event.delete()
        except Exception:
            pass

    async def render_explorer(self, sess: MegaSession, page: int = 0):
        """Render paginated folder buttons list (6 folders per page)."""
        sess.last_active = time.time()
        sess.current_dir_h = None
        sess.awaiting_search = False
        top_folders = sess.folder_info["top_subfolders"]
        total_folders = len(top_folders)

        PAGE_SIZE = 6
        total_pages = max(1, (total_folders + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        sess.current_page = page

        start_idx = page * PAGE_SIZE
        end_idx = min(start_idx + PAGE_SIZE, total_folders)
        page_folders = top_folders[start_idx:end_idx]

        total_files = sess.folder_info["total_files"]
        total_size_str = format_bytes(sess.folder_info["total_size"])

        text = (
            f"🗂️ **Mega Subfolders Explorer**\n\n"
            f"📊 **Total Content**: `{total_folders} Folders` • `{total_files} Files` • `{total_size_str}`\n"
            f"📄 **Page**: `{page + 1} / {total_pages}` (Showing {start_idx + 1}–{end_idx})\n\n"
            f"👇 **Neeche kisi bhi folder pe click karo uski videos/photos chunne ke liye:**"
        )

        buttons = []
        for sf_h in page_folders:
            d = sess.folder_info["dirs"][sf_h]
            raw_name = d["name"]
            disp_name = (raw_name[:18] + "…") if len(raw_name) > 18 else raw_name
            tot = d["total_files"]
            sz_str = format_bytes(d["total_size"])
            btn_text = f"📂 {disp_name} ({tot} • {sz_str})"
            buttons.append([Button.inline(btn_text, data=f"m_dir:{sess.session_id}:{sf_h}")])

        # Pagination row
        nav_row = []
        if page > 0:
            nav_row.append(Button.inline("⬅️ Prev", data=f"m_nav:{sess.session_id}:{page - 1}"))
        nav_row.append(Button.inline(f"📄 {page + 1}/{total_pages}", data=f"m_nav:{sess.session_id}:{page}"))
        if page < total_pages - 1:
            nav_row.append(Button.inline("Next ➡️", data=f"m_nav:{sess.session_id}:{page + 1}"))
        buttons.append(nav_row)

        # Action row
        buttons.append([
            Button.inline("🔍 Search by Name", data=f"m_srch:{sess.session_id}"),
            Button.inline("🌐 Global Download", data=f"m_home:{sess.session_id}"),
        ])
        buttons.append([Button.inline("❌ Cancel", data=f"m_cancel:{sess.session_id}")])

        try:
            await sess.status_msg.edit(text, buttons=buttons)
        except Exception:
            pass

    async def render_folder_details(self, sess: MegaSession, dir_h: str):
        """Render breakdown (videos vs photos) for a selected subfolder with selective download buttons."""
        sess.last_active = time.time()
        sess.current_dir_h = dir_h
        d = sess.folder_info["dirs"].get(dir_h)
        if not d:
            await self.render_explorer(sess, page=sess.current_page)
            return

        folder_name = d["name"]
        num_v = len(d["videos"])
        num_p = len(d["photos"])
        num_o = len(d["others"])
        tot_files = d["total_files"]
        tot_size = format_bytes(d["total_size"])

        v_size = format_bytes(sum(f["size"] for f in d["videos"]))
        p_size = format_bytes(sum(f["size"] for f in d["photos"]))
        o_size = format_bytes(sum(f["size"] for f in d["others"]))

        text = (
            f"📂 **Subfolder**: `{folder_name}`\n\n"
            f"📊 **Content**: `{tot_files} Files` • `{tot_size}`\n"
        )
        if num_v > 0:
            text += f"• 🎬 **Videos**: `{num_v} Files` (~`{v_size}`)\n"
        if num_p > 0:
            text += f"• 🖼️ **Photos**: `{num_p} Files` (~`{p_size}`)\n"
        if num_o > 0:
            text += f"• 📄 **Others**: `{num_o} Files` (~`{o_size}`)\n"

        text += "\n👇 **Aapko is folder mein se kya download karna hai?**"

        buttons = []
        row1 = []
        if num_v > 0:
            row1.append(Button.inline(f"🎬 Videos Only ({num_v})", data=f"m_act:{sess.session_id}:{dir_h}:video"))
        if num_p > 0:
            row1.append(Button.inline(f"🖼️ Photos Only ({num_p})", data=f"m_act:{sess.session_id}:{dir_h}:photo"))
        if row1:
            buttons.append(row1)

        row2 = []
        if num_o > 0:
            row2.append(Button.inline(f"📄 Others ({num_o})", data=f"m_act:{sess.session_id}:{dir_h}:other"))
        row2.append(Button.inline(f"📁 Download Full Folder ({tot_files})", data=f"m_act:{sess.session_id}:{dir_h}:all"))
        buttons.append(row2)

        buttons.append([
            Button.inline("🔙 Back to Subfolders", data=f"m_nav:{sess.session_id}:{sess.current_page}"),
            Button.inline("❌ Cancel", data=f"m_cancel:{sess.session_id}"),
        ])

        try:
            await sess.status_msg.edit(text, buttons=buttons)
        except Exception:
            pass

    async def render_search_results(self, sess: MegaSession):
        """Render results from keyword search."""
        sess.last_active = time.time()
        matches = sess.search_results or []
        if not matches:
            text = (
                f"🔍 **Search Results for** `\"{sess.search_query}\"`\n\n"
                f"❌ Koi matching folder nahi mila.\n\n"
                f"💡 _Try searching with a shorter keyword or check spelling._"
            )
            buttons = [
                [Button.inline("🔍 Try Another Search", data=f"m_srch:{sess.session_id}")],
                [Button.inline("🔙 Back to All Folders", data=f"m_nav:{sess.session_id}:0")],
                [Button.inline("❌ Cancel", data=f"m_cancel:{sess.session_id}")],
            ]
            try:
                await sess.status_msg.edit(text, buttons=buttons)
            except Exception:
                pass
            return

        text = (
            f"🔍 **Search Results for** `\"{sess.search_query}\"`\n"
            f"Found **{len(matches)}** matching folder(s):\n\n"
            f"👇 Click any folder to choose its videos & photos:"
        )
        buttons = []
        for sf_h in matches[:8]:
            d = sess.folder_info["dirs"][sf_h]
            raw_name = d["name"]
            disp_name = (raw_name[:20] + "…") if len(raw_name) > 20 else raw_name
            tot = d["total_files"]
            sz_str = format_bytes(d["total_size"])
            buttons.append([Button.inline(f"📂 {disp_name} ({tot} • {sz_str})", data=f"m_dir:{sess.session_id}:{sf_h}")])

        buttons.append([
            Button.inline("🔍 Search Again", data=f"m_srch:{sess.session_id}"),
            Button.inline("🔙 All Folders", data=f"m_nav:{sess.session_id}:0"),
        ])
        buttons.append([Button.inline("❌ Cancel", data=f"m_cancel:{sess.session_id}")])

        try:
            await sess.status_msg.edit(text, buttons=buttons)
        except Exception:
            pass

    async def render_home(self, sess: MegaSession):
        """Render global view of the entire share."""
        sess.last_active = time.time()
        info = sess.folder_info
        num_folders = len(info["top_subfolders"])
        tot_files = info["total_files"]
        tot_size = format_bytes(info["total_size"])

        num_v = len(info["videos"])
        num_p = len(info["photos"])
        num_o = len(info["others"])

        v_sz = format_bytes(sum(f["size"] for f in info["videos"]))
        p_sz = format_bytes(sum(f["size"] for f in info["photos"]))

        text = (
            f"🌐 **Mega Share Global Hub**\n\n"
            f"📊 **Total Content**: `{num_folders} Subfolders` • `{tot_files} Files` • `{tot_size}`\n"
            f"• 🎬 **All Videos**: `{num_v} Files` (~`{v_sz}`)\n"
            f"• 🖼️ **All Photos**: `{num_p} Files` (~`{p_sz}`)\n\n"
            f"💡 _Tip: Specific subfolders chunna fast aur reliable download deta hai._\n\n"
            f"👇 **Choose an option:**"
        )

        buttons = [
            [Button.inline(f"🗂️ Browse Subfolders ({num_folders})", data=f"m_nav:{sess.session_id}:0")],
            [Button.inline("🔍 Search by Name / Keyword", data=f"m_srch:{sess.session_id}")],
            [
                Button.inline(f"🎬 All Videos ({num_v})", data=f"m_allact:{sess.session_id}:video"),
                Button.inline(f"🖼️ All Photos ({num_p})", data=f"m_allact:{sess.session_id}:photo"),
            ],
            [Button.inline(f"📁 Download Entire Share ({tot_files})", data=f"m_allact:{sess.session_id}:all")],
            [Button.inline("❌ Cancel", data=f"m_cancel:{sess.session_id}")],
        ]

        try:
            await sess.status_msg.edit(text, buttons=buttons)
        except Exception:
            pass

    async def handle_callback(self, event: events.CallbackQuery.Event, client, active_tasks: dict):
        """Unified callback handler for all Mega explorer events."""
        data = event.data.decode("utf-8")
        parts = data.split(":")
        action_prefix = parts[0]
        sess_id = parts[1] if len(parts) > 1 else ""

        sess = self.get_session(sess_id)
        if not sess:
            await event.answer("⚠️ Session expired (30 min)! Please resend the link.", alert=True)
            return

        if event.sender_id != sess.user_id:
            await event.answer("⛔ This session is for another user!", alert=True)
            return

        sess.last_active = time.time()

        if action_prefix == "m_nav":
            page = int(parts[2]) if len(parts) > 2 else 0
            await self.render_explorer(sess, page=page)
            await event.answer()

        elif action_prefix == "m_dir":
            dir_h = parts[2]
            await self.render_folder_details(sess, dir_h)
            await event.answer()

        elif action_prefix == "m_home":
            await self.render_home(sess)
            await event.answer()

        elif action_prefix == "m_srch":
            sess.awaiting_search = True
            text = (
                f"🔍 **Search Mega Subfolder**\n\n"
                f"Aapko kaunsa folder chahiye? Chat mein folder ka naam ya keyword bhejo (e.g., `VIP`, `Autumn`, `Thompson`):"
            )
            buttons = [
                [Button.inline("🔙 Back to Explorer", data=f"m_nav:{sess.session_id}:{sess.current_page}")],
                [Button.inline("❌ Cancel", data=f"m_cancel:{sess.session_id}")],
            ]
            await sess.status_msg.edit(text, buttons=buttons)
            await event.answer("🔍 Type keyword in chat!")

        elif action_prefix == "m_cancel":
            if not sess.future.done():
                sess.future.set_result(("cancel", None))
            await event.answer("❌ Cancelled")
            try:
                await sess.status_msg.edit("❌ **Mega exploration cancelled by user.**", buttons=None)
            except Exception:
                pass

        elif action_prefix == "m_act":
            dir_h = parts[2]
            act_type = parts[3]
            label_map = {"video": "Videos", "photo": "Photos", "other": "Others", "all": "Full Folder"}
            await event.answer(f"🚀 Starting {label_map.get(act_type, 'Files')} download...")
            if not sess.future.done():
                sess.future.set_result((act_type, dir_h))

        elif action_prefix == "m_allact":
            act_type = parts[2]
            label_map = {"video": "All Videos", "photo": "All Photos", "all": "Everything"}
            await event.answer(f"🚀 Starting {label_map.get(act_type, 'Files')} download...")
            if not sess.future.done():
                sess.future.set_result((act_type, None))

        elif action_prefix == "m_reopen":
            await event.answer("🔄 Reopening Mega Subfolder Explorer...")
            # Reopen session for downloading another folder from the same link
            sess.future = asyncio.get_running_loop().create_future()
            sess.status_msg = await event.reply("🔍 **Loading Mega Subfolder Explorer...**", buttons=make_stop_btn(sess.user_id))
            await self.render_explorer(sess, page=sess.current_page)

            # Spawn downloader task for the reopened selection
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
        folder_display_name = target_dir["name"] if target_dir else "Subfolder"
        if chosen_action == "video":
            selected_nodes = target_dir["videos"]
        elif chosen_action == "photo":
            selected_nodes = target_dir["photos"]
        elif chosen_action == "other":
            selected_nodes = target_dir["others"]
        else:
            selected_nodes = target_dir["videos"] + target_dir["photos"] + target_dir["others"]
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
    """Stream download selected nodes directly from Mega and upload to Telegram."""
    # Sort files smallest first for quickest initial delivery
    selected_nodes = sorted(selected_nodes, key=lambda n: n.get("size", 0))
    total_selected = len(selected_nodes)
    total_selected_size = sum(n["size"] for n in selected_nodes)
    is_multi_file = total_selected > 1

    safe_id = f"{int(time.time())}_{user_id}"
    local_dir = config.DOWNLOAD_DIR / f"mega_{safe_id}"
    local_dir.mkdir(parents=True, exist_ok=True)
    stop_monitor = asyncio.Event()

    try:
        filter_label = filter_mode.upper()
        await status_msg.edit(
            f"🚀 **Starting Mega Download...**\n"
            f"📂 **Target**: `{folder_name}` [{filter_label} ONLY]\n"
            f"📦 **Selected**: `{total_selected} Files` • `{format_bytes(total_selected_size)}`\n"
            "⏳ *Downloading selected files directly (smallest first)...*",
            buttons=make_stop_btn(user_id),
        )
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
            nonlocal album_paths, album_captions, album_keys, album_size
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
                    workers=10,
                )
                if config.PRIVATE_CHAT_ID and msgs:
                    await event.client.forward_messages(event.chat_id, msgs)

                # Save cache records for each uploaded album item
                if msgs and isinstance(msgs, list):
                    for k, p, m in zip(album_keys, album_paths, msgs):
                        if k and m:
                            try:
                                sz = p.stat().st_size if p.exists() else 0
                                await db.save_cached_file(k, p.name, sz, target_chat_id, m.id)
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
                        sent = await uploader.upload_media(
                            chat_id=target_chat_id,
                            file_path=p,
                            caption=cap,
                            progress_callback=None,
                            workers=10,
                        )
                        if config.PRIVATE_CHAT_ID and sent:
                            await event.client.forward_messages(event.chat_id, sent)
                        if sent and k:
                            await db.save_cached_file(k, p.name, p.stat().st_size, target_chat_id, sent.id)
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
        async with aiohttp.ClientSession(timeout=timeout) as dl_session:
            for idx, node in enumerate(selected_nodes, start=1):
                if stop_monitor.is_set():
                    break

                file_size = node["size"]
                file_name = sanitize_filename(node["name"])
                file_prefix = f"[{idx}/{total_selected}] " if is_multi_file else ""

                # Telegram 2GB limit check
                max_size_bytes = config.MAX_FILE_SIZE_MB * 1024 * 1024
                if file_size > max_size_bytes:
                    await flush_album()
                    await event.reply(
                        f"📄 **{file_name}**\n\n"
                        f"💾 **Size**: {format_bytes(file_size)}\n"
                        f"🛑 **Direct Bot Upload Limit**: {config.MAX_FILE_SIZE_MB} MB\n\n"
                        f"⚠️ Exceeds Telegram's 2000 MB bot limit and was skipped."
                    )
                    continue

                file_key = f"mg_{node['h']}"

                # Storage Channel / DB cache check: If already in channel, forward instantly!
                cached_msg = await db.find_cached_media(
                    event.client, config.PRIVATE_CHAT_ID, file_key, file_name=file_name, file_size=file_size
                )
                if cached_msg:
                    logger.info(f"Storage cache hit for Mega node {node['h']} ({file_name})! Forwarding...")
                    await flush_album()
                    try:
                        await event.client.forward_messages(event.chat_id, cached_msg)
                        await status_msg.edit(
                            f"⚡ {file_prefix}**Found in Storage Channel!**\n\n"
                            f"🎬 `{file_name}`\n"
                            f"⏩ Forwarded instantly to your chat without re-downloading!",
                            buttons=make_stop_btn(user_id),
                        )
                        await asyncio.sleep(0.8)
                    except Exception as fwd_err:
                        logger.warning(f"Forward failed ({fwd_err}), proceeding to download...")
                        cached_msg = None
                    if cached_msg:
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
                    if now - last_dl_edit >= 2.0 or current == total:
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
                    album_captions.append(f"{icon} `{dest_path.name}` (`{format_bytes(actual_size)}`)\n`#{file_key}`")
                    album_size += actual_size

                    # If batch reached 10 items or 200MB, upload this completed batch immediately!
                    if len(album_paths) >= ALBUM_MAX_ITEMS or album_size >= TOTAL_ALBUM_MAX_SIZE:
                        await flush_album()
                    continue

                # Standalone media upload (Videos > 20MB, Documents, Archives, or Single Files)
                await flush_album()

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
                    if now - last_upload_edit >= 2.0 or current == total:
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

                caption = f"🎬 `{dest_path.name}` (`{format_bytes(actual_size)}`)\n`#{file_key}`"

                sent_msg = await uploader.upload_media(
                    chat_id=target_chat_id,
                    file_path=dest_path,
                    caption=caption,
                    thumb_path=thumb_path,
                    progress_callback=upload_progress,
                    workers=10,
                )

                if sent_msg:
                    if config.PRIVATE_CHAT_ID:
                        await event.client.forward_messages(event.chat_id, sent_msg)
                    await db.save_cached_file(file_key, dest_path.name, actual_size, target_chat_id, sent_msg.id)

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

        completion_buttons = None
        if sess_id:
            completion_buttons = [
                [Button.inline("🔄 Download Another Folder from Same Link", data=f"m_reopen:{sess_id}")]
            ]

        await event.reply(
            f"✅ **Mega Transfer Complete!**\n\n"
            f"📂 **Folder**: `{folder_name}`\n"
            f"🎯 **Filter**: `{filter_mode.upper()} ONLY`\n"
            f"📦 **Successfully Delivered**: `{total_selected} file(s)` ({format_bytes(total_selected_size)}).\n\n"
            f"💡 _Aap is link se doosre subfolders bhi bina link dobara send kiye download kar sakte hain!_",
            buttons=completion_buttons,
        )

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

                # CASE A: MULTI-SUBFOLDER SHARE (e.g. 53 Subfolders, 20 GB)
                if len(top_subfolders) > 1:
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
                    for sf_h in top_subfolders:
                        sf_name = folder_info["dirs"][sf_h]["name"].lower()
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
                            await mega_mgr.render_folder_details(sess, matched_sf_h)
                    else:
                        # Open the Subfolder Explorer on page 0
                        await mega_mgr.render_explorer(sess, page=0)

                    # Execute session download loop
                    await execute_session_download(
                        event=event,
                        sess=sess,
                        active_tasks=active_tasks_ref,
                    )
                    return

                # CASE B: FLAT FOLDER (1 or 0 subfolders)
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

        await status_msg.edit(f"📥 **Downloading from Mega...**\n`Engine: {engine_name}`", buttons=make_stop_btn(user_id))

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
                if now - last_upload_edit >= 2.0 or current == total:
                    last_upload_edit = now
                    txt = render_mega_progress(
                        action="📤 Uploading to Telegram",
                        name=f.name,
                        current=current,
                        total=total,
                        speed=speed,
                    )
                    try:
                        await status_msg.edit(txt, buttons=make_stop_btn(user_id))
                    except Exception:
                        pass

            file_key = f"mg_{abs(hash(f.name + str(file_size)))}"
            caption = f"🎬 `{f.name}` (`{format_bytes(file_size)}`)\n`#{file_key}`"

            sent_msg = await uploader.upload_media(
                chat_id=target_chat_id,
                file_path=f,
                caption=caption,
                thumb_path=thumb_path,
                progress_callback=upload_progress_cli,
                workers=10,
            )
            if sent_msg:
                if config.PRIVATE_CHAT_ID:
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
