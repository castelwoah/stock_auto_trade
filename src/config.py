from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


class KisEnv(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Settings(BaseSettings):
    APP_KEY: SecretStr
    APP_SECRET: SecretStr
    CANO: str
    ACNT_PRDT_CD: str
    URL_BASE: str
    DISCORD_WEBHOOK_URL: SecretStr
    KIS_ENV: KisEnv = KisEnv.PAPER
    TOKEN_DIR: Path = Field(default_factory=lambda: PROJECT_ROOT)
    TOKEN_BUCKET_CAPACITY: int = 20
    TOKEN_BUCKET_REFILL_RATE: float = 20.0
    DATABASE_URL: str = Field(
        default_factory=lambda: f"sqlite:///{PROJECT_ROOT / 'trading.db'}"
    )
    DART_API_KEY: SecretStr | None = None

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        yaml_file=str(PROJECT_ROOT / "config.yaml"),
        yaml_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    @field_validator("URL_BASE")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("CANO")
    @classmethod
    def _validate_cano(cls, v: str) -> str:
        if not (v.isdigit() and len(v) == 8):
            raise ValueError("CANO must be 8 digits")
        return v

    @field_validator("ACNT_PRDT_CD")
    @classmethod
    def _validate_acnt(cls, v: str) -> str:
        if not (v.isdigit() and len(v) == 2):
            raise ValueError("ACNT_PRDT_CD must be 2 digits")
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
