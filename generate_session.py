import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


load_dotenv()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


async def main() -> None:
    api_id = int(require_env("TG_API_ID"))
    api_hash = require_env("TG_API_HASH")

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print()
        print("Copy this value into your host as TELETHON_SESSION_STRING:")
        print()
        print(client.session.save())
        print()
        print("Keep it private. Anyone with this string can log in as this Telegram account.")


if __name__ == "__main__":
    asyncio.run(main())
