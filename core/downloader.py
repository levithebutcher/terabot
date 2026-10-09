import asyncio
import os
import time
from pathlib import Path
from typing import Callable, Optional

import aiohttp

from config import TERABOX_COOKIE, DOWNLOAD_DIR
from utils.logger import logger
from utils.helpers import format_bytes, sanitize_filename


class DownloadError(Exception):
    """Exception raised when a file download fails."""
    pass


class TeraBoxDownloader:
    """
    High-speed asynchronous resumable file downloader with parallel HTTP Range connections,
    streaming chunk writes, live speed tracking, and single-stream fallback.
    """

    CHUNK_SIZE = 1024 * 1024  # 1 MB buffer
    DEFAULT_PARALLEL_CONNECTIONS = 6

    def __init__(self, cookie: Optional[str] = None, connections: int = DEFAULT_PARALLEL_CONNECTIONS):
        self.cookie = cookie if cookie is not None else TERABOX_COOKIE
        self.connections = max(1, min(connections, 16))

    def _get_headers(self) -> dict:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0"
            ),
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
            "Referer": "https://terabox.com/",
            "DNT": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def _download_range_chunk(
        self,
        session: aiohttp.ClientSession,
        dlink: str,
        start_byte: int,
        end_byte: int,
        output_file_path: Path,
        part_id: int,
        progress_tracker: dict,
        progress_callback: Optional[Callable[[int, int, float], None]],
        total_size: int,
        lock: asyncio.Lock,
    ) -> int:
        """Download an individual byte range and write directly to file at byte offset."""
        headers = self._get_headers()
        headers["Range"] = f"bytes={start_byte}-{end_byte}"
        timeout = aiohttp.ClientTimeout(total=3600, connect=30, sock_read=90)

        async with session.get(dlink, headers=headers, timeout=timeout) as resp:
            if resp.status not in (200, 206):
                raise DownloadError(f"Part {part_id} HTTP error: {resp.status}")

            part_downloaded = 0
            with open(output_file_path, "r+b") as f:
                f.seek(start_byte)
                async for chunk in resp.content.iter_chunked(self.CHUNK_SIZE):
                    if not chunk:
                        break
                    f.write(chunk)
                    chunk_len = len(chunk)
                    part_downloaded += chunk_len

                    # Update aggregate progress
                    async with lock:
                        progress_tracker["downloaded"] += chunk_len
                        progress_tracker["recent_bytes"] += chunk_len
                        now = time.monotonic()
                        elapsed = now - progress_tracker["last_time"]

                        if elapsed >= 1.0 or progress_tracker["downloaded"] >= total_size:
                            speed = progress_tracker["recent_bytes"] / elapsed if elapsed > 0 else 0.0
                            progress_tracker["last_time"] = now
                            progress_tracker["recent_bytes"] = 0
                            if progress_callback:
                                if asyncio.iscoroutinefunction(progress_callback):
                                    await progress_callback(progress_tracker["downloaded"], total_size, speed)
                                else:
                                    progress_callback(progress_tracker["downloaded"], total_size, speed)

            return part_downloaded

    async def _download_parallel(
        self,
        dlink: str,
        final_path: Path,
        part_path: Path,
        total_size: int,
        connections: int,
        progress_callback: Optional[Callable[[int, int, float], None]],
    ) -> Path:
        """Execute parallel chunk download using HTTP Range requests."""
        # Pre-allocate sparse/zeroed file on disk
        with open(part_path, "wb") as f:
            f.seek(total_size - 1)
            f.write(b"\0")

        part_size = total_size // connections
        ranges = []
        for i in range(connections):
            start = i * part_size
            end = (start + part_size - 1) if i < connections - 1 else (total_size - 1)
            ranges.append((start, end))

        progress_tracker = {
            "downloaded": 0,
            "recent_bytes": 0,
            "last_time": time.monotonic(),
        }
        lock = asyncio.Lock()

        timeout = aiohttp.ClientTimeout(total=3600, connect=30, sock_read=90)
        conn = aiohttp.TCPConnector(limit=connections + 4, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=conn) as session:
            tasks = [
                self._download_range_chunk(
                    session=session,
                    dlink=dlink,
                    start_byte=start,
                    end_byte=end,
                    output_file_path=part_path,
                    part_id=i,
                    progress_tracker=progress_tracker,
                    progress_callback=progress_callback,
                    total_size=total_size,
                    lock=lock,
                )
                for i, (start, end) in enumerate(ranges)
            ]
            results = await asyncio.gather(*tasks)

        actual_size = part_path.stat().st_size
        if actual_size != total_size:
            raise DownloadError(f"Parallel download size mismatch: expected {total_size}, got {actual_size}")

        if final_path.exists():
            final_path.unlink()
        part_path.rename(final_path)
        logger.info(f"Parallel download finished: {final_path.name} ({format_bytes(actual_size)}) via {connections} streams")
        return final_path

    async def _download_single(
        self,
        dlink: str,
        final_path: Path,
        part_path: Path,
        expected_size: int,
        progress_callback: Optional[Callable[[int, int, float], None]],
    ) -> Path:
        """Fallback single-connection streaming download with Range resume support."""
        downloaded_so_far = 0
        if part_path.exists():
            file_sz = part_path.stat().st_size
            if expected_size > 0 and file_sz >= expected_size:
                # Pre-allocated sparse file or completed size; reset for clean download
                try:
                    part_path.unlink()
                except Exception:
                    pass
            else:
                downloaded_so_far = file_sz

        headers = self._get_headers()
        if downloaded_so_far > 0:
            headers["Range"] = f"bytes={downloaded_so_far}-"

        timeout = aiohttp.ClientTimeout(total=3600, connect=30, sock_read=90)
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(dlink, timeout=timeout) as response:
                if response.status not in (200, 206):
                    if response.status == 416:  # Range Not Satisfiable (already completed)
                        part_path.rename(final_path)
                        return final_path
                    raise DownloadError(f"HTTP error {response.status} from download server.")

                # Calculate total size
                content_range = response.headers.get("Content-Range")
                if content_range and "/" in content_range:
                    try:
                        total_size = int(content_range.split("/")[-1])
                    except ValueError:
                        total_size = expected_size
                elif response.status == 200:
                    total_size = int(response.headers.get("Content-Length", expected_size or 0))
                    downloaded_so_far = 0
                else:
                    total_size = downloaded_so_far + int(response.headers.get("Content-Length", 0))

                mode = "ab" if downloaded_so_far > 0 and response.status == 206 else "wb"
                with open(part_path, mode) as f:
                    last_time = time.monotonic()
                    bytes_since_last = 0

                    async for chunk in response.content.iter_chunked(self.CHUNK_SIZE):
                        if not chunk:
                            break
                        f.write(chunk)
                        chunk_len = len(chunk)
                        downloaded_so_far += chunk_len
                        bytes_since_last += chunk_len

                        now = time.monotonic()
                        elapsed = now - last_time
                        if progress_callback and (elapsed >= 1.0 or downloaded_so_far == total_size):
                            speed = bytes_since_last / elapsed if elapsed > 0 else 0.0
                            last_time = now
                            bytes_since_last = 0
                            if asyncio.iscoroutinefunction(progress_callback):
                                await progress_callback(downloaded_so_far, total_size, speed)
                            else:
                                progress_callback(downloaded_so_far, total_size, speed)

                actual_downloaded = part_path.stat().st_size
                if total_size > 0 and actual_downloaded != total_size:
                    raise DownloadError(f"Size mismatch: expected {total_size}, got {actual_downloaded}")

                if final_path.exists():
                    final_path.unlink()
                part_path.rename(final_path)
                logger.info(f"Single-stream download finished: {final_path.name} ({format_bytes(actual_downloaded)})")
                return final_path

    async def download_file(
        self,
        dlink: str,
        filename: str,
        expected_size: int = 0,
        output_dir: Optional[Path] = None,
        progress_callback: Optional[Callable[[int, int, float], None]] = None,
        max_retries: int = 3,
    ) -> Path:
        """
        Download a file from TeraBox direct link.
        Uses parallel Range connections for files >= 3 MB, falling back to single stream if needed.
        """
        if not output_dir:
            output_dir = DOWNLOAD_DIR
        output_dir.mkdir(parents=True, exist_ok=True)

        safe_name = sanitize_filename(filename)
        final_path = output_dir / safe_name
        part_path = output_dir / f"{safe_name}.part"

        # Check if already completed
        if final_path.exists():
            actual_size = final_path.stat().st_size
            if expected_size > 0 and actual_size == expected_size:
                logger.info(f"File already completely downloaded: {final_path.name}")
                if progress_callback:
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(actual_size, actual_size, 0.0)
                    else:
                        progress_callback(actual_size, actual_size, 0.0)
                return final_path

        # If file is at least 3 MB and connections > 1, use parallel Range download
        use_parallel = self.connections > 1 and expected_size >= 3 * 1024 * 1024

        retry_count = 0
        while retry_count < max_retries:
            try:
                if use_parallel:
                    try:
                        logger.info(f"Initiating {self.connections}-stream parallel Range download for {safe_name}...")
                        return await self._download_parallel(
                            dlink=dlink,
                            final_path=final_path,
                            part_path=part_path,
                            total_size=expected_size,
                            connections=self.connections,
                            progress_callback=progress_callback,
                        )
                    except Exception as parallel_err:
                        logger.warning(f"Parallel download failed ({parallel_err}). Falling back to single stream...")
                        if part_path.exists():
                            try:
                                part_path.unlink()
                            except Exception:
                                pass
                        use_parallel = False  # Don't try parallel again on retry

                # Single-stream mode
                return await self._download_single(
                    dlink=dlink,
                    final_path=final_path,
                    part_path=part_path,
                    expected_size=expected_size,
                    progress_callback=progress_callback,
                )

            except (aiohttp.ClientError, asyncio.TimeoutError, DownloadError) as e:
                retry_count += 1
                logger.warning(f"Download attempt {retry_count}/{max_retries} failed: {e}")
                if retry_count >= max_retries:
                    raise DownloadError(f"Download failed after {max_retries} attempts: {e}") from e
                await asyncio.sleep(2 * retry_count)

        raise DownloadError(f"Download failed after reaching max retries.")
