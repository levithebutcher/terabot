import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from telethon import TelegramClient

import config
from core.database import db
from core.webapp_server import start_webapp_server, stop_webapp_server
from handlers import register_all_handlers
from utils.logger import logger


# Initialize Telethon client with automatic reconnect & infinite retries
client = TelegramClient(
    session="terabot_session",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    auto_reconnect=True,
    connection_retries=None,
    retry_delay=2,
    request_retries=5,
)

# Attach all command, callback, and message handlers
register_all_handlers(client)


def cleanup_leftover_parts():
    """Remove leftover .part files in DOWNLOAD_DIR on startup/shutdown."""
    if config.DOWNLOAD_DIR.exists():
        for p in config.DOWNLOAD_DIR.glob("*.part"):
            try:
                p.unlink()
                logger.info(f"Cleaned up orphaned part file: {p.name}")
            except Exception as e:
                logger.debug(f"Could not remove {p.name}: {e}")


# ---------------- STARTUP & SHUTDOWN ---------------- #

async def main():
    cleanup_leftover_parts()
    await db.init_db()
    try:
        await start_webapp_server()
    except Exception as e:
        logger.warning(f"Could not start Telegram Mini App web server: {e}")

    logger.info("Starting Telegram Bot via Telethon...")
    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN is missing in .env! Exiting.")
        sys.exit(1)
    if not config.API_ID or not config.API_HASH:
        logger.error("API_ID or API_HASH is missing in .env! Exiting.")
        sys.exit(1)

    while True:
        try:
            if not client.is_connected():
                await client.start(bot_token=config.BOT_TOKEN)
                me = await client.get_me()
                logger.info(f"Bot successfully logged in as @{me.username} (ID: {me.id})")
                print(f"\n[+] Bot is running as @{me.username} (ID: {me.id})")
                print("[+] Listening for incoming messages. Press Ctrl+C to stop.\n")

                if config.PRIVATE_CHAT_ID:
                    logger.info(f"Syncing storage channel cache for {config.PRIVATE_CHAT_ID}...")
                    asyncio.create_task(db.sync_channel_cache(client, config.PRIVATE_CHAT_ID))

            await client.run_until_disconnected()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Connection interrupted ({e}). Reconnecting in 3 seconds...", exc_info=True)
            await asyncio.sleep(3)
        finally:
            await stop_webapp_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot shutting down gracefully...")
        cleanup_leftover_parts()
