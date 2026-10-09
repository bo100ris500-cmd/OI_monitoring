"""Application settings from environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(alias="BOT_TOKEN")
    admin_ids: str = Field(default="", alias="ADMIN_IDS")
    database_url: str = Field(
        default="postgresql+asyncpg://oi_bot:oi_bot@127.0.0.1:5432/oi_bot",
        alias="DATABASE_URL",
    )
    config_path: str = Field(default="./config.yaml", alias="CONFIG_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def admin_id_set(self) -> set[int]:
        if not self.admin_ids.strip():
            return set()
        return {int(x.strip()) for x in self.admin_ids.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
