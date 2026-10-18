from typing import Optional, List
# pyrefly: ignore[missing-import]
from telethon import Button


def make_stop_btn(user_id: int) -> List[List[Button]]:
    """Generate inline abort/stop button for progress status messages."""
    return [[Button.inline("🛑 Abort Task", data=f"stop:{user_id}")]]


def get_channel_button(channel_id: Optional[int]) -> Optional[Button]:
    """Return inline button linking directly to the Telegram Storage Channel."""
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


def get_channel_buttons_row(channel_id: Optional[int]) -> Optional[List[List[Button]]]:
    """Return keyboard containing the Storage Channel button, or None if channel not set."""
    ch_btn = get_channel_button(channel_id)
    return [[ch_btn]] if ch_btn else None


def get_delivery_buttons(current_mode: str = "channel") -> List[List[Button]]:
    """
    Generate delivery mode toggle inline buttons.
    Highlights the currently active delivery mode.
    """
    mode = (current_mode or "channel").lower()
    is_channel = mode == "channel"
    is_both = mode == "both"

    ch_label = "✅ 🛡️ Channel Only (Active)" if is_channel else "🛡️ Switch to Channel Only"
    both_label = "✅ 📬 Channel + Bot DM (Active)" if is_both else "📬 Switch to Both"

    return [
        [
            Button.inline(ch_label, data="set_deliv:channel"),
            Button.inline(both_label, data="set_deliv:both"),
        ]
    ]


def get_folder_action_buttons(
    session_id: str,
    video_count: int = 0,
    photo_count: int = 0,
    other_count: int = 0,
) -> List[List[Button]]:
    """
    Generate interactive selection buttons for folders containing multiple file types.
    Allows user to filter by Videos, Photos, Others, or Download All.
    """
    buttons = []
    row1 = []
    if video_count > 0:
        row1.append(Button.inline("🎬 Videos", data=f"act:video:{session_id}"))
    if photo_count > 0:
        row1.append(Button.inline("🖼️ Photos", data=f"act:photo:{session_id}"))
    if row1:
        buttons.append(row1)

    row2 = []
    if other_count > 0:
        row2.append(Button.inline("📄 Others", data=f"act:other:{session_id}"))
    row2.append(Button.inline("📁 Download All", data=f"act:all:{session_id}"))
    buttons.append(row2)

    buttons.append([Button.inline("❌ Cancel", data=f"act:cancel:{session_id}")])
    return buttons


def get_cancel_button(session_id: str) -> List[List[Button]]:
    """Generate a single cancel button for interactive prompts."""
    return [[Button.inline("❌ Cancel", data=f"act:cancel:{session_id}")]]


def get_webapp_button(url: str, text: str = "📱 Open in Mini App"):
    """Generate a native Telegram WebApp sheet button, falling back to Button.url."""
    try:
        from telethon.tl import types
        return types.KeyboardInlineButton(
            text=text,
            type=types.InlineButtonTypeWebView(url=url),
        )
    except Exception:
        return Button.url(text, url)
