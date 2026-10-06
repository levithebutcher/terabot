import asyncio
import time
from pathlib import Path
from typing import Callable, Optional

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeVideo

from core.media import is_video_file, get_video_metadata, generate_video_thumbnail
from utils.helpers import format_bytes, format_duration
from utils.logger import logger


class TelethonUploader:
    """
    Handles uploading files to Telegram chats/channels using Telethon,
    attaching video metadata, thumbnails, streaming attributes,
    and throttled live progress updates.
    """

    def __init__(self, client: TelegramClient):
        self.client = client

    async def upload_media(
        self,
        chat_id: int,
        file_path: Path,
        caption: str,
        thumb_path: Optional[Path] = None,
        progress_callback: Optional[Callable[[int, int, float], None]] = None,
        max_retries: int = 3,
    ):
        """
        Upload file using Telethon's native send_file with video attributes and progress callback.
        Returns the sent Message object.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"File to upload not found: {file_path}")

        file_size = file_path.stat().st_size
        is_video = is_video_file(file_path)

        attributes = []
        duration = 0
        width = 0
        height = 0
        generated_thumb = None

        if is_video:
            meta = get_video_metadata(file_path)
            duration = meta.get("duration", 0)
            width = meta.get("width", 0)
            height = meta.get("height", 0)

            attributes.append(
                DocumentAttributeVideo(
                    duration=duration,
                    w=width,
                    h=height,
                    supports_streaming=True,
                )
            )

            # If no thumb provided or doesn't exist, generate one
            if not thumb_path or not thumb_path.exists():
                thumb_timestamp = max(1, duration // 10) if duration > 0 else 1
                generated_thumb = generate_video_thumbnail(file_path, timestamp_sec=thumb_timestamp)
                if generated_thumb and generated_thumb.exists():
                    thumb_path = generated_thumb

        start_time = time.monotonic()
        last_callback_time = start_time
        last_callback_bytes = 0

        async def internal_progress(current: int, total: int):
            nonlocal last_callback_time, last_callback_bytes
            now = time.monotonic()
            elapsed_interval = now - last_callback_time

            # Throttle callback to fire every 3.5 seconds
            if progress_callback and (elapsed_interval >= 3.5 or current == total):
                bytes_diff = current - last_callback_bytes
                speed = bytes_diff / elapsed_interval if elapsed_interval > 0 else 0.0
                last_callback_time = now
                last_callback_bytes = current

                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(current, total, speed)
                else:
                    progress_callback(current, total, speed)

        retry_count = 0
        while retry_count < max_retries:
            try:
                t0 = time.monotonic()
                msg = await self.client.send_file(
                    entity=chat_id,
                    file=str(file_path),
                    caption=caption,
                    thumb=str(thumb_path) if thumb_path and thumb_path.exists() and thumb_path.stat().st_size > 0 else None,
                    attributes=attributes if attributes else None,
                    supports_streaming=is_video,
                    progress_callback=internal_progress,
                )
                total_upload_time = time.monotonic() - t0
                avg_speed = (file_size / (1024 * 1024)) / total_upload_time if total_upload_time > 0 else 0.0
                logger.info(
                    f"Uploaded {file_path.name} ({format_bytes(file_size)}) in {total_upload_time:.1f}s at {avg_speed:.2f} MB/s"
                )
                return msg

            except FloodWaitError as e:
                logger.warning(f"Telegram FloodWait: sleeping for {e.seconds} seconds")
                await asyncio.sleep(e.seconds + 1)
                retry_count += 1
            except Exception as e:
                logger.error(f"Error during file upload attempt {retry_count + 1}: {e}")
                retry_count += 1
                if retry_count >= max_retries:
                    raise
                await asyncio.sleep(2 * retry_count)
            finally:
                if generated_thumb and generated_thumb.exists():
                    try:
                        generated_thumb.unlink()
                    except Exception:
                        pass
