from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    bot_token: str = Field(alias="BOT_TOKEN")
    chat_id: int = Field(alias="CHAT_ID")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")


settings = Settings()

