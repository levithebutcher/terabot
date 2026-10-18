from utils.helpers import format_bytes, format_duration


def build_progress_bar(current: int, total: int, width: int = 12) -> tuple[str, float]:
    """Return formatted visual progress bar (e.g. ▰▰▰▱▱) and percentage."""
    if total > 0:
        pct = (current / total) * 100.0
        filled = min(width, int(width * current // total))
        bar = "▰" * filled + "▱" * (width - filled)
        return bar, pct
    return "▱" * width, 0.0


def render_progress_text(
    action: str,
    filename: str,
    current: int,
    total: int,
    speed: float,
    file_prefix: str = "",
) -> str:
    """Render a standardized progress card for single file transfers."""
    bar_width = 12
    speed_mb = speed / (1024 * 1024) if speed > 0 else 0.0
    clean_action = action.replace("**", "").strip()

    bar, percent = build_progress_bar(current, total, bar_width)

    if total > 0 and speed > 0:
        remaining = max(0, total - current)
        eta_str = format_duration(remaining / speed)
    else:
        eta_str = "--:--"

    lines = [
        f"**{clean_action}**\n",
    ]

    prefix_str = f"`{file_prefix.strip()}` " if file_prefix and file_prefix.strip() else ""
    lines.append(f"🎬 **File**: {prefix_str}`{filename}`")

    lines.extend([
        f"[{bar}] **{percent:.1f}%** (`{format_bytes(current)}` / `{format_bytes(total)}`)",
        f"⚡ **Speed**: `{speed_mb:.2f} MB/s` • ⏱ **ETA**: `{eta_str}`",
    ])

    return "\n".join(lines)


def render_album_progress(
    album_count: int,
    item_label: str,
    current: int,
    total: int,
) -> str:
    """Render a progress card for batch album uploads."""
    bar, pct = build_progress_bar(current, total, width=12)
    return (
        f"**📤 UPLOADING ALBUM TO TELEGRAM [Step 2/2]**\n\n"
        f"📦 **Batch**: `{album_count} {item_label}`\n"
        f"[{bar}] **{pct:.1f}%** (`{int(current)}/{total} files`)\n"
        f"⚡ *Sending high-speed album batch to Telegram...*"
    )


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
    """Render a standardized progress card for Mega transfers."""
    bar_width = 12
    speed_mb = speed / (1024 * 1024) if speed > 0 else 0.0
    clean_action = action.replace("**", "").strip()

    bar, percent = build_progress_bar(current, total, bar_width)

    if total > 0 and speed > 0:
        remaining = max(0, total - current)
        eta_str = format_duration(remaining / speed)
    else:
        eta_str = "--:--"

    lines = []
    if folder_header:
        lines.append(f"{folder_header}\n")

    lines.append(f"**{clean_action}**\n")
    prefix_str = f"`{file_prefix.strip()}` " if file_prefix and file_prefix.strip() else ""
    lines.append(f"🎬 **File**: {prefix_str}`{name}`")

    lines.extend([
        f"[{bar}] **{percent:.1f}%** (`{format_bytes(current)}` / `{format_bytes(total)}`)",
        f"⚡ **Speed**: `{speed_mb:.2f} MB/s` • ⏱ **ETA**: `{eta_str}`",
    ])

    if skipped_count > 0:
        lines.append(f"⏩ **Skipped**: `{skipped_count}` file(s) already in storage")

    return "\n".join(lines)
