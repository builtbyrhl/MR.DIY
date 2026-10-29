from __future__ import annotations

import os
from dotenv import load_dotenv
load_dotenv()
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass
class Settings:
    bot_token: str = field(default_factory=lambda: os.environ["BOT_TOKEN"])
    admin_ids: set[int] = field(default_factory=lambda: {
        int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
    })
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/mrdiy"
    ))
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    channel_id: str = field(default_factory=lambda: os.getenv("CHANNEL_ID", ""))  # source channel for lectures
    upload_inbox_chat_id: str = field(default_factory=lambda: os.getenv("UPLOAD_INBOX_CHAT_ID", ""))
    destination_group_id: str = field(default_factory=lambda: os.getenv("DESTINATION_GROUP_ID", ""))
    webhook_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", ""))
    webhook_secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET", "secret"))
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8080")))
    premium_price_inr: int = field(default_factory=lambda: int(os.getenv("PREMIUM_PRICE_INR", "499")))
    razorpay_key_id: str = field(default_factory=lambda: os.getenv("RAZORPAY_KEY_ID", ""))
    razorpay_key_secret: str = field(default_factory=lambda: os.getenv("RAZORPAY_KEY_SECRET", ""))
    free_lectures_per_day: int = field(default_factory=lambda: int(os.getenv("FREE_LECTURES_PER_DAY", "3")))
    access_default_days: int = field(default_factory=lambda: int(os.getenv("ACCESS_DEFAULT_DAYS", "0")))
    media_retention_hours: int = field(default_factory=lambda: int(os.getenv("MEDIA_RETENTION_HOURS", "24")))
    media_cleanup_interval_seconds: int = field(default_factory=lambda: int(os.getenv("MEDIA_CLEANUP_INTERVAL_SECONDS", "300")))
    reminder_hour_utc: int = field(default_factory=lambda: int(os.getenv("REMINDER_HOUR_UTC", "14")))
    digest_weekday: int = field(default_factory=lambda: int(os.getenv("DIGEST_WEEKDAY", "6")))
    digest_hour_utc: int = field(default_factory=lambda: int(os.getenv("DIGEST_HOUR_UTC", "14")))

    @property
    def is_webhook(self) -> bool:
        return bool(self.webhook_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
