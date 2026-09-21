"""One-off login for reading private channels: prints TELEGRAM_SESSION and channel ids.

Run it once on your own computer (never in GitHub Actions):

    pip install telethon
    python scripts/telegram_login.py

It asks for api_id / api_hash (https://my.telegram.org → API development tools),
the phone number of the READING account and the login code Telegram sends.
The session string it prints is a full login to that account — store it only
as the GitHub Secret TELEGRAM_SESSION and never paste it into the repository.
Nothing is written to disk by this script.
"""

from __future__ import annotations

import asyncio
import getpass


async def main() -> None:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        raise SystemExit("Сначала установите библиотеку: pip install telethon")

    print("Данные с https://my.telegram.org → API development tools")
    api_id = int(input("api_id: ").strip())
    api_hash = getpass.getpass("api_hash (ввод скрыт): ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash)
    # start() asks for the phone, the code and the 2FA password interactively.
    await client.start()

    session = client.session.save()
    me = await client.get_me()
    print(f"\nВход выполнен: {me.first_name or ''} (id {me.id})")
    print("\n=== TELEGRAM_SESSION (положите в GitHub Secrets, НЕ в репозиторий) ===")
    print(session)
    print("=== конец TELEGRAM_SESSION ===\n")

    print("Каналы этого аккаунта (channel_id → название). Число подставляется в config/sources.yml:")
    async for dialog in client.iter_dialogs():
        if dialog.is_channel and not getattr(dialog.entity, "megagroup", False):
            username = getattr(dialog.entity, "username", None)
            kind = f"публичный @{username}" if username else "приватный"
            print(f"  {dialog.entity.id:>15}  {dialog.title}  ({kind})")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
