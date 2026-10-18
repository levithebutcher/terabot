from telethon import TelegramClient
from handlers.commands import register_commands
from handlers.callbacks import register_callbacks
from handlers.messages import register_messages


def register_all_handlers(client: TelegramClient) -> None:
    """Register all command, callback, and message handlers onto the client."""
    register_commands(client)
    register_callbacks(client)
    register_messages(client)


__all__ = ["register_all_handlers"]
