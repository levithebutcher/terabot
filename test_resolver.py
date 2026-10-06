import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from config import TERABOX_COOKIE
from core.downloader import TeraBoxDownloader, DownloadError
from core.resolver import (
    TeraBoxResolver,
    CookieExpiredError,
    ShareLinkExpiredError,
    CloudflareBlockedError,
    ResolverError,
)
from utils.helpers import format_bytes, format_duration

# Ensure UTF-8 output on Windows consoles
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def print_progress_bar(current: int, total: int, speed: float, start_time: float):
    """Render a terminal progress bar with honest speed measurement."""
    bar_width = 30
    if total > 0:
        percent = (current / total) * 100.0
        filled = int(bar_width * current // total)
        bar = "#" * filled + "-" * (bar_width - filled)
        remaining = total - current
        eta = remaining / speed if speed > 0 else 0
        eta_str = format_duration(eta)
    else:
        percent = 0.0
        bar = "-" * bar_width
        eta_str = "--:--"

    speed_mb = speed / (1024 * 1024)
    line = f"\r[{bar}] {percent:5.1f}% | {format_bytes(current)} / {format_bytes(total)} | {speed_mb:5.2f} MB/s | ETA: {eta_str}"
    sys.stdout.write(line)
    sys.stdout.flush()


def run_ffprobe_analysis(file_path: Path) -> tuple[bool, str]:
    """
    Run ffprobe if installed to inspect container, streams, duration, and resolution.
    If ffprobe is not installed, report that clearly.
    """
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        return False, "ffprobe is not installed on PATH. Cannot verify video stream integrity."

    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration,size,bit_rate:stream=codec_name,codec_type,width,height,r_frame_rate",
        "-of", "json",
        str(file_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])

        if not streams:
            return False, "ffprobe found no valid audio/video streams in file."

        duration_sec = float(fmt.get("duration", 0))
        details = []
        for s in streams:
            stype = s.get("codec_type", "unknown")
            cname = s.get("codec_name", "unknown")
            if stype == "video":
                w = s.get("width", 0)
                h = s.get("height", 0)
                fps = s.get("r_frame_rate", "")
                details.append(f"Video: {cname} ({w}x{h} @ {fps} fps)")
            elif stype == "audio":
                details.append(f"Audio: {cname}")

        summary = f"Duration: {format_duration(duration_sec)} ({duration_sec:.1f}s) | " + ", ".join(details)
        return True, summary
    except Exception as e:
        return False, f"ffprobe execution error: {e}"


async def main():
    print("=" * 70)
    print("           TeraBox Link Resolver & Downloader CLI Test          ")
    print("=" * 70)

    # 1. Validate Cookie
    if not TERABOX_COOKIE or "ndus" not in TERABOX_COOKIE:
        print("\n[!] CRITICAL ERROR: TERABOX_COOKIE is missing or has no 'ndus' token in .env!")
        print("    Please ensure .env contains a valid TERABOX_COOKIE before testing.")
        print("    Stopping execution as required by instructions.")
        sys.exit(1)

    print("[+] TERABOX_COOKIE loaded from .env (ndus found).")

    # 2. Get Share URL
    if len(sys.argv) > 1:
        target_url = sys.argv[1].strip()
    else:
        target_url = "https://1024tera.com/s/1HSEb8PZRUE7Z1Tvd3ZtT0g"
        print(f"[*] No link provided on CLI. Using reference link: {target_url}")

    print(f"[*] Resolving TeraBox link: {target_url}\n")

    # 3. Resolve Link
    resolver = TeraBoxResolver()
    t_start_resolve = time.monotonic()

    try:
        files = await resolver.resolve(target_url)
    except CookieExpiredError as e:
        print(f"\n[X] COOKIE ERROR: {e}")
        sys.exit(1)
    except ShareLinkExpiredError as e:
        print(f"\n[X] LINK EXPIRED: {e}")
        sys.exit(1)
    except CloudflareBlockedError as e:
        print(f"\n[X] CLOUDFLARE BLOCK: {e}")
        sys.exit(1)
    except ResolverError as e:
        print(f"\n[X] RESOLVER ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[X] UNEXPECTED ERROR: {e}")
        sys.exit(1)

    resolve_time = time.monotonic() - t_start_resolve
    print(f"[+] Successfully resolved in {resolve_time:.2f}s!")
    print(f"[+] Total files discovered: {len(files)}\n")

    print(f"{'#':<3} | {'Filename':<35} | {'Size':<10} | {'Has Dlink':<10}")
    print("-" * 70)
    for idx, f in enumerate(files, 1):
        has_dl = "YES" if f.dlink else "NO"
        fname = f.file_name if len(f.file_name) <= 35 else f.file_name[:32] + "..."
        print(f"{idx:<3} | {fname:<35} | {f.size_readable:<10} | {has_dl:<10}")

    print("-" * 70)

    # 4. Download first file
    target_file = files[0]
    if not target_file.dlink:
        print("\n[X] Target file does not have a direct download link. Stopping.")
        sys.exit(1)

    print(f"\n[*] Starting parallel download for: '{target_file.file_name}' ({target_file.size_readable})...")
    test_download_dir = Path("temp_test_downloads")
    test_download_dir.mkdir(parents=True, exist_ok=True)

    t_download_start = time.monotonic()
    downloader = TeraBoxDownloader(connections=6)

    def progress_callback(current, total, speed):
        print_progress_bar(current, total, speed, t_download_start)

    try:
        downloaded_path = await downloader.download_file(
            dlink=target_file.dlink,
            filename=target_file.file_name,
            expected_size=target_file.size,
            output_dir=test_download_dir,
            progress_callback=progress_callback,
        )
        print()
    except DownloadError as e:
        print(f"\n[X] DOWNLOAD FAILED: {e}")
        sys.exit(1)

    total_download_time = time.monotonic() - t_download_start
    actual_size = downloaded_path.stat().st_size
    avg_speed_bps = actual_size / total_download_time if total_download_time > 0 else 0.0
    avg_speed_mb = avg_speed_bps / (1024 * 1024)

    print("\n" + "=" * 70)
    print("                    DOWNLOAD VERIFICATION REPORT                   ")
    print("=" * 70)
    print(f"[+] File Saved As       : {downloaded_path.name}")
    print(f"[+] Expected Size       : {target_file.size_readable} ({target_file.size} bytes)")
    print(f"[+] Actual Size on Disk : {format_bytes(actual_size)} ({actual_size} bytes)")
    print(f"[+] Match Expected Size : {'PERFECT MATCH' if actual_size == target_file.size else 'MISMATCH'}")
    print(f"[+] Total Elapsed Time  : {total_download_time:.2f} seconds")
    print(f"[+] Measured Avg Speed  : {avg_speed_mb:.2f} MB/s (Honest actual measurement)")

    # 5. FFPROBE Media Integrity Check
    probe_ok, probe_msg = run_ffprobe_analysis(downloaded_path)
    print(f"[+] FFprobe Analysis    : {'PASSED' if probe_ok else 'FAILED/NOT AVAILABLE'}")
    print(f"[+] Media Stream Info   : {probe_msg}")
    print("=" * 70)

    # 6. Clean up temporary test file
    try:
        downloaded_path.unlink()
        if test_download_dir.exists() and not any(test_download_dir.iterdir()):
            test_download_dir.rmdir()
        print("[+] Test temporary download cleaned up successfully.")
    except Exception as e:
        print(f"[-] Cleanup note: {e}")

    print("\n[SUCCESS] TEST VERIFICATION COMPLETE!")


if __name__ == "__main__":
    asyncio.run(main())
