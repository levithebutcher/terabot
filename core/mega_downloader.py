import asyncio
import re
import shutil
from pathlib import Path
import time
from telethon import events
from config import DOWNLOAD_DIR, ADMIN_IDS
from utils.logger import logger
from core.uploader import TelethonUploader
from core.media import is_video_file, get_video_metadata, generate_video_thumbnail

async def process_mega_link(event: events.NewMessage.Event, url: str):
    """
    Handle downloading from Mega.nz using mega-get CLI, and uploading to Telegram.
    """
    user_id = event.sender_id
    status_msg = await event.reply("🔍 **Resolving Mega link...**")
    
    # 1. Prepare local directory
    safe_id = str(time.time()).replace(".", "")
    local_dir = DOWNLOAD_DIR / f"mega_{safe_id}"
    local_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        # 2. Start mega-get as an async subprocess
        await status_msg.edit("📥 **Downloading from Mega...**\n(Using official MEGAcmd)")
        
        process = await asyncio.create_subprocess_exec(
            "mega-get", url, str(local_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Read stdout to parse progress (Wait for completion)
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            err_output = (stderr.decode().strip() or stdout.decode().strip())
            logger.error(f"Mega CLI Error: {err_output}")
            if "Bandwidth quota exceeded" in err_output or "Transfer quota exceeded" in err_output or "509" in err_output:
                await status_msg.edit("❌ **Mega Bandwidth Quota Exceeded!**\nPlease wait a few hours or change your server IP/proxy.")
            elif "not found" in err_output.lower():
                await status_msg.edit("❌ **Mega Link Invalid or Expired!**")
            else:
                await status_msg.edit(f"❌ **Mega Download Failed:**\n`{err_output[-200:]}`")
            return
        
        # 3. Walk downloaded files and upload to Telegram
        await status_msg.edit("📤 **Uploading to Telegram...**")
        uploader = TelethonUploader(event.client)
        
        files_to_upload = [f for f in local_dir.rglob("*") if f.is_file()]
        if not files_to_upload:
            await status_msg.edit("❌ **No files found in the Mega link.**")
            return
            
        for file_path in files_to_upload:
            # Generate thumbnail if it's a video
            thumb_path = None
            if is_video_file(file_path.name):
                meta = get_video_metadata(file_path)
                if meta and meta.get("duration", 0) > 0:
                    thumb_path = await generate_video_thumbnail(file_path, meta["duration"])
            
            # Simple progress callback to keep connection alive
            last_edit_time = 0.0
            async def upload_progress(current: int, total: int, speed: float):
                nonlocal last_edit_time
                now = time.monotonic()
                if now - last_edit_time >= 3.5 or current == total:
                    last_edit_time = now
                    try:
                        percent = (current / total) * 100 if total > 0 else 0
                        await status_msg.edit(f"📤 **Uploading:** {file_path.name}\nProgress: {percent:.1f}%")
                    except Exception:
                        pass
            
            await uploader.upload_media(
                file_path=file_path,
                reply_to_msg_id=event.id,
                caption=f"📁 **{file_path.name}**\n\n📥 Downloaded via Mega",
                thumb_path=thumb_path,
                progress_callback=upload_progress
            )
            
            if thumb_path and thumb_path.exists():
                thumb_path.unlink()
                
        await status_msg.delete()
        await event.reply("✅ **Mega Transfer Complete!**")
        
    except Exception as e:
        logger.error(f"Mega Error: {e}")
        await status_msg.edit(f"❌ **Unexpected Error:** `{e}`")
    finally:
        # 4. Clean up disk
        if local_dir.exists():
            shutil.rmtree(local_dir, ignore_errors=True)
