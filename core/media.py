import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from utils.logger import logger

FFPROBE_BIN = shutil.which("ffprobe")
FFMPEG_BIN = shutil.which("ffmpeg")

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v",
    ".ts", ".3gp", ".asf", ".vob", ".mpg", ".mpeg"
}

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".heic"
}


def is_video_file(file_path: Path) -> bool:
    """Check if file is a video by extension."""
    return file_path.suffix.lower() in VIDEO_EXTENSIONS


def is_photo_file(file_path: Path) -> bool:
    """Check if file is an image/photo by extension."""
    return file_path.suffix.lower() in IMAGE_EXTENSIONS


def get_video_metadata(file_path: Path) -> dict:
    """Extract duration, width, and height using ffprobe."""
    meta = {"duration": 0, "width": 0, "height": 0}
    if not FFPROBE_BIN or not file_path.exists():
        return meta

    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "format=duration:stream=width,height",
        "-of", "json",
        str(file_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            fmt = data.get("format", {})
            try:
                meta["duration"] = int(float(fmt.get("duration", 0)))
            except (ValueError, TypeError):
                meta["duration"] = 0

            streams = data.get("streams", [])
            for s in streams:
                if s.get("width") and s.get("height"):
                    meta["width"] = int(s["width"])
                    meta["height"] = int(s["height"])
                    break
    except Exception as e:
        logger.debug(f"ffprobe metadata extraction failed for {file_path.name}: {e}")

    return meta


def generate_video_thumbnail(file_path: Path, output_path: Optional[Path] = None, timestamp_sec: int = 2) -> Optional[Path]:
    """Generate a JPEG thumbnail from video using ffmpeg."""
    if not FFMPEG_BIN or not file_path.exists():
        return None

    if not output_path:
        output_path = file_path.parent / f"{file_path.stem}_thumb.jpg"

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-ss", str(timestamp_sec),
        "-i", str(file_path),
        "-vframes", "1",
        "-q:v", "2",
        str(output_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=20)
        if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return output_path
    except Exception as e:
        logger.debug(f"ffmpeg thumbnail generation failed: {e}")

    return None
