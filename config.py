import os
import sys
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# Load .env if present
load_dotenv()


class Config:
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_CHAT_ID: str = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHANNEL_ID", "")).strip()
    # Alias for backward compatibility
    TELEGRAM_CHANNEL_ID: str = TELEGRAM_CHAT_ID
    TELEGRAM_TOPIC_ID: Optional[int] = (
        int(os.getenv("TELEGRAM_TOPIC_ID").strip())
        if os.getenv("TELEGRAM_TOPIC_ID") and os.getenv("TELEGRAM_TOPIC_ID").strip().isdigit()
        else None
    )
    ADMIN_TG_ID: Optional[int] = (
        int(os.getenv("ADMIN_TG_ID").strip())
        if os.getenv("ADMIN_TG_ID") and os.getenv("ADMIN_TG_ID").strip().lstrip("-").isdigit()
        else None
    )

    VK_ACCESS_TOKEN: str = os.getenv("VK_ACCESS_TOKEN", "").strip()
    VK_GROUP_DOMAIN: str = os.getenv("VK_GROUP_DOMAIN", "irs2027").strip()

    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "60"))
    INITIAL_POSTS_COUNT: int = int(os.getenv("INITIAL_POSTS_COUNT", "5"))
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/bot.db").strip()

    @classmethod
    def validate(cls):
        errors = []
        if not cls.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN не указан в .env")
        if not cls.TELEGRAM_CHAT_ID:
            errors.append("TELEGRAM_CHAT_ID (или TELEGRAM_CHANNEL_ID) не указан в .env")
        if not cls.VK_ACCESS_TOKEN:
            errors.append("VK_ACCESS_TOKEN не указан в .env")
        if not cls.VK_GROUP_DOMAIN:
            errors.append("VK_GROUP_DOMAIN не указан в .env")

        if errors:
            print("❌ Ошибки конфигурации:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            print("\nПожалуйста, скопируйте .env.example в .env и заполните параметры.", file=sys.stderr)
            return False
        return True


config = Config()
