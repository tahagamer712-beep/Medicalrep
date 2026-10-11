"""Application settings.

All secrets come from the environment (or .env file, never committed).
Nothing secret is hardcoded here.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_env: str = "dev"
    data_dir: Path = Path("./data")
    telegram_owner_ids: str = ""
    telegram_owner_bot_token: str = ""
    # reminder delivery: "log" (default, no real sends) or "telegram"
    # (real sends via TelegramNotifier — operator opt-in only)
    reminder_delivery: str = "log"

    @property
    def resolved_data_dir(self) -> Path:
        p = self.data_dir if self.data_dir.is_absolute() else Path.cwd() / self.data_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def owner_ids(self) -> set[int]:
        ids: set[int] = set()
        for part in self.telegram_owner_ids.split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))
        return ids


settings = Settings()
