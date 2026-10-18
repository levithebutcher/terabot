aimport config


def get_welcome_text(first_name: str = "User") -> str:
    storage_mode = "Enabled" if config.PRIVATE_CHAT_ID else "Direct mode"
    return (
        f"👋 **Welcome, {first_name}!**\n\n"
        "I am your high-speed **TeraBox & Mega Downloader Bot**.\n\n"
        "✨ **Features**:\n"
        "• Native direct link resolution (no dead workers)\n"
        f"• Multi-stream parallel Range engine (`{config.DOWNLOAD_STREAMS}` connections)\n"
        f"• Support up to `{config.MAX_FILE_SIZE_MB}` MB files\n"
        "• Streaming video playback with fast seek & thumbnails\n"
        f"• Instant caching ({storage_mode})\n\n"
        "📥 Simply **send or forward any TeraBox or Mega link** to begin downloading!"
    )


def get_help_text() -> str:
    return (
        "📖 **TeraBox & Mega Bot Commands & Usage**\n\n"
        "• `/start` - Start the bot & view system status\n"
        "• `/filter [video|photo|all]` - Filter files in folders\n"
        "• `/delivery [channel|both]` - Delivery mode (Default: Silent Storage Channel only)\n"
        "• `/stop` or `/cancel` - Abort active download/upload\n"
        "• `/ping` - Check bot responsiveness & latency\n"
        "• `/help` - Show this help menu\n"
        "• `/broadcast <msg>` - (Admin only) Broadcast message to all users\n\n"
        "🎯 **Smart Filtering Tips**:\n"
        "• `/filter video` - Download only videos (.mp4, .mkv, etc.)\n"
        "• `/filter photo` - Download only photos (.jpg, .png, etc.)\n"
        "• `/filter all` - Reset to download all files\n"
        "• You can also send a link with `video` or `photo`:\n"
        "  `https://terabox.com/s/... video`\n\n"
        "🌐 **Supported Links**:\n"
        "• **TeraBox**: `terabox.com`, `terabox.app`, `1024tera.com`, `4funbox.com`, `mirrobox.com`, `nephobox.com`, `tibibox.com` and all mirror domains.\n"
        "• **Mega.nz**: `mega.nz/file/...`, `mega.nz/folder/...`, and legacy `mega.co.nz` links.\n\n"
        f"⚙️ **Limits**: Files up to `{config.MAX_FILE_SIZE_MB}` MB. Files larger than this will be delivered as direct browser links."
    )


ACCESS_DENIED_TEXT = (
    "🔒 **Access Restricted**\n\n"
    "This is a private personal downloader bot.\n"
    "All downloads share the owner's private credentials.\n\n"
    "You are not on the authorized user list."
)

COOKIE_EXPIRED_USER = (
    "❌ **Authentication Failed** `[ERR-COOKIE]`\n\n"
    "The bot's TeraBox session cookie has expired or triggered a verification challenge.\n"
    "Please update `TERABOX_COOKIE` in Render Environment."
)

COOKIE_EXPIRED_ADMIN = (
    "🚨 **ADMIN ALERT: TeraBox Cookie Expired!**\n\n"
    "The configured `TERABOX_COOKIE` has expired or returned `need verify_v2`.\n"
    "Please extract a fresh `ndus` cookie from your browser and update Render Environment."
)

CLOUDFLARE_BLOCKED_TEXT = (
    "❌ **CDN Challenge** `[ERR-CF]`\n\n"
    "Cloudflare temporarily blocked the request. Please retry in 1–2 minutes."
)

DELIVERY_BOTH_ACTIVE = (
    "📬 **Delivery Mode: Both (Channel + Bot DM)** [Active]\n\n"
    "• Naye files aapke **Storage Channel** mein save honge\n"
    "• Naye files **is bot chat mein bhi forward** honge\n"
    "• Storage mein pehle se maujood files direct **silently skip** honge (no spam!)\n\n"
    "💡 _Agar bot chat ko silent rakhna ho (Vault Mode), use karein:_ `/delivery channel`"
)

DELIVERY_CHANNEL_ACTIVE = (
    "🛡️ **Delivery Mode: Storage Channel Only (Vault Mode)** [Default Active]\n\n"
    "• Naye files **sirf Storage Channel** mein save honge\n"
    "• Bot chat bilkul silent rahegi (koi media forward nahi hoga)\n"
    "• Kaam khatam hone par yahan sirf **Final Summary Card** aayegi!\n\n"
    "💡 _Dono jagah pane ke liye switch karein:_ `/delivery both`"
)


def get_delivery_menu_text(current_mode: str) -> str:
    current_label = (
        "🛡️ Storage Channel Only (Vault Mode) [Default]"
        if current_mode == "channel"
        else "📬 Both (Channel + Bot DM)"
    )
    return (
        f"📦 **Current Delivery Mode**: `{current_label}`\n\n"
        "**Neeche button dabakar mode badlein ya command use karein:**\n"
        "• `/delivery channel` - Sirf Channel mein save karo (Bot DM silent, 0 spam) [Default]\n"
        "• `/delivery both` - Channel mein bhi save karo + Bot DM mein forward karo\n\n"
        "💡 _Storage mein pehle se maujood files hamesha silently skip hoti hain taaki duplicate spam na ho._"
    )
