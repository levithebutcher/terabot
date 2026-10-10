import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import Callable, Optional

from telethon import TelegramClient, helpers, utils
from telethon.errors import FloodWaitError
from telethon.tl import functions, types, custom
from telethon.tl.types import DocumentAttributeVideo

from core.media import is_video_file, get_video_metadata, generate_video_thumbnail
from utils.helpers import format_bytes, format_duration
from utils.logger import logger


async def fast_upload_file(
    client: TelegramClient,
    file_path: Path,
    part_size_kb: int = 512,
    workers: int = 16,
    progress_callback: Optional[Callable[[int, int, float], None]] = None,
    max_retries: int = 3,
) -> types.TypeInputFile:
    """
    High-speed parallel file uploader for Telethon.
    Splits the file into 512KB chunks and uploads them in parallel across multiple workers.
    Returns InputFile / InputFileBig for use in client.send_file.
    """
    file_size = file_path.stat().st_size
    file_name = file_path.name
    part_size = part_size_kb * 1024
    part_count = (file_size + part_size - 1) // part_size
    is_big = file_size > 10 * 1024 * 1024
    file_id = helpers.generate_random_long()

    # Small files can use a single chunk/worker directly
    actual_workers = min(workers, part_count) if part_count > 0 else 1

    queue: asyncio.Queue[tuple[int, int, int]] = asyncio.Queue()
    for part_idx in range(part_count):
        start_offset = part_idx * part_size
        chunk_len = min(part_size, file_size - start_offset)
        queue.put_nowait((part_idx, start_offset, chunk_len))

    uploaded_bytes = 0
    start_time = time.monotonic()
    last_callback_time = start_time
    last_callback_bytes = 0
    lock = asyncio.Lock()
    md5_hash = hashlib.md5() if not is_big else None

    # For small files with MD5, we read sequential parts to maintain valid hash
    if not is_big:
        with open(file_path, "rb") as f:
            while chunk := f.read(part_size):
                md5_hash.update(chunk)

    async def worker_loop():
        nonlocal uploaded_bytes, last_callback_time, last_callback_bytes

        # Open dedicated read handle per worker
        with open(file_path, "rb") as f:
            while not queue.empty():
                try:
                    part_idx, offset, length = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                f.seek(offset)
                chunk_data = f.read(length)

                if is_big:
                    req = functions.upload.SaveBigFilePartRequest(
                        file_id=file_id,
                        file_part=part_idx,
                        file_total_parts=part_count,
                        bytes=chunk_data,
                    )
                else:
                    req = functions.upload.SaveFilePartRequest(
                        file_id=file_id,
                        file_part=part_idx,
                        bytes=chunk_data,
                    )

                # Retry loop per chunk
                for attempt in range(max_retries):
                    try:
                        ok = await client(req)
                        if not ok:
                            raise RuntimeError(f"Server returned false for part {part_idx}")
                        break
                    except FloodWaitError as fw:
                        logger.warning(f"FloodWait during chunk upload: {fw.seconds}s")
                        await asyncio.sleep(fw.seconds + 1)
                    except Exception as err:
                        if attempt == max_retries - 1:
                            logger.error(f"Failed to upload part {part_idx} after {max_retries} attempts: {err}")
                            raise
                        await asyncio.sleep(1 + attempt)

                async with lock:
                    uploaded_bytes += length
                    now = time.monotonic()
                    elapsed = now - last_callback_time
                    if progress_callback and (elapsed >= 2.0 or uploaded_bytes >= file_size):
                        bytes_diff = uploaded_bytes - last_callback_bytes
                        speed = bytes_diff / elapsed if elapsed > 0 else 0.0
                        last_callback_time = now
                        last_callback_bytes = uploaded_bytes

                        try:
                            if asyncio.iscoroutinefunction(progress_callback):
                                await progress_callback(uploaded_bytes, file_size, speed)
                            else:
                                progress_callback(uploaded_bytes, file_size, speed)
                        except Exception:
                            pass

                queue.task_done()

    # Launch parallel upload workers
    worker_tasks = [asyncio.create_task(worker_loop()) for _ in range(actual_workers)]
    try:
        await asyncio.gather(*worker_tasks)
    except Exception:
        for t in worker_tasks:
            t.cancel()
        raise

    if is_big:
        return types.InputFileBig(id=file_id, parts=part_count, name=file_name)
    else:
        return types.InputFile(
            id=file_id,
            parts=part_count,
            name=file_name,
            md5_checksum=md5_hash.hexdigest(),
        )


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
        workers: int = 16,
    ):
        """
        Upload file using multi-connection fast parallel chunk transfer.
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

        retry_count = 0
        while retry_count < max_retries:
            try:
                t0 = time.monotonic()

                # Step 1: Upload raw file in parallel chunks (up to 4-6x faster than default sequential upload)
                input_file = await fast_upload_file(
                    client=self.client,
                    file_path=file_path,
                    part_size_kb=512,
                    workers=workers,
                    progress_callback=progress_callback,
                    max_retries=max_retries,
                )

                # Step 2: Send the uploaded file handle with attributes
                msg = await self.client.send_file(
                    entity=chat_id,
                    file=input_file,
                    caption=caption,
                    thumb=str(thumb_path) if thumb_path and thumb_path.exists() and thumb_path.stat().st_size > 0 else None,
                    attributes=attributes if attributes else None,
                    supports_streaming=is_video,
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

    async def upload_album(
        self,
        chat_id: int,
        file_paths: list[Path],
        captions: list[str],
        progress_callback: Optional[Callable] = None,
        max_retries: int = 3,
        workers: int = 16,
    ):
        """
        Fast parallel album uploader for Telethon.
        Pre-uploads all files in parallel chunks with live progress tracking,
        then transmits the entire album as an atomic group.
        """
        if not file_paths:
            return None

        uploaded_inputs = []
        total_items = len(file_paths)
        for idx, fp in enumerate(file_paths, 1):
            if not fp.exists():
                continue

            async def file_progress(curr: int, tot: int, spd: float, item_idx=idx, item_name=fp.name):
                if progress_callback:
                    try:
                        if asyncio.iscoroutinefunction(progress_callback):
                            await progress_callback(item_idx, total_items, curr, tot, spd, item_name)
                        else:
                            progress_callback(item_idx, total_items, curr, tot, spd, item_name)
                    except TypeError:
                        if asyncio.iscoroutinefunction(progress_callback):
                            await progress_callback(curr, tot, spd)
                        else:
                            progress_callback(curr, tot, spd)

            input_file = await fast_upload_file(
                client=self.client,
                file_path=fp,
                part_size_kb=512,
                workers=workers,
                progress_callback=file_progress if progress_callback else None,
                max_retries=max_retries,
            )

            if is_video_file(fp):
                meta = get_video_metadata(fp)
                dur = meta.get("duration", 0)
                w = meta.get("width", 0)
                h = meta.get("height", 0)

                attrs = [
                    DocumentAttributeVideo(
                        duration=dur,
                        w=w,
                        h=h,
                        supports_streaming=True,
                    ),
                    types.DocumentAttributeFilename(file_name=fp.name),
                ]

                thumb_input = None
                thumb_path = generate_video_thumbnail(fp, max(1, dur // 10) if dur > 0 else 1)
                if thumb_path and thumb_path.exists():
                    try:
                        thumb_input = await self.client.upload_file(str(thumb_path))
                    except Exception:
                        pass
                    finally:
                        try:
                            thumb_path.unlink()
                        except Exception:
                            pass

                uploaded_inputs.append(
                    types.InputMediaUploadedDocument(
                        file=input_file,
                        mime_type="video/mp4",
                        attributes=attrs,
                        thumb=thumb_input,
                        supports_streaming=True,
                    )
                )
            else:
                uploaded_inputs.append(input_file)

        if not uploaded_inputs:
            return None

        return await self.client.send_file(
            entity=chat_id,
            file=uploaded_inputs,
            caption=captions[: len(uploaded_inputs)],
        )
