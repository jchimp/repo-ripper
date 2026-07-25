"""Environment configuration.

Sensitive / infrastructure values live here (auth, token, paths). Operational
values that a user may want to change at runtime (schedules, Telegram, toggles)
are seeded from these defaults into the DB on first boot and edited in the UI.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Auth (env only) ---
    admin_username: str = "admin"
    admin_password: str = "changeme"
    secret_key: str = "change-this-to-a-long-random-string"
    session_max_age: int = 60 * 60 * 24 * 7  # 7 days

    # --- GitHub (env only) ---
    github_token: str = ""
    github_api: str = "https://api.github.com"

    # --- Paths inside the container (map to NAS via volumes) ---
    mirror_root: str = "/data/mirrors"          # primary share
    protection_root: str = "/data/protection"   # secondary share
    db_path: str = "/data/repo-ripper.db"

    tz: str = "UTC"

    # --- Seed defaults for the DB-backed, UI-editable settings ---
    include_forks: bool = True
    include_archived: bool = True
    include_wikis: bool = True
    fetch_lfs: bool = True

    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_on_success: bool = True
    notify_on_fail: bool = True

    protection_delete: bool = True  # rsync --delete on the protection copy

    # cron expressions (minute hour day month weekday)
    schedule_scan: str = "0 3 * * *"        # discover repos, 03:00 daily
    schedule_pull: str = "30 3 * * *"       # mirror pulls, 03:30 daily
    schedule_protection: str = "0 5 * * 0"  # protection copy, 05:00 Sundays


@lru_cache
def get_settings() -> Settings:
    return Settings()
