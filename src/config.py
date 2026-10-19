from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
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


@dataclass(frozen=True)
class KisEnvProfile:
    """D11: 환경별 URL · TR_ID · 초당 호출 한도. 전환은 KIS_ENV + 재기동만으로."""

    url_base: str
    tr_ids: dict[str, str]
    rate_per_sec: float


# TR_ID 출처: koreainvestment/open-trading-api examples_llm/domestic_stock (2026-10 확인)
# 초당 한도: 실전 20 / 모의 5 (커뮤니티 기준, 공식 미확인) — 여유분을 두고 18 / 4.
KIS_ENV_PROFILES: dict[KisEnv, KisEnvProfile] = {
    KisEnv.LIVE: KisEnvProfile(
        url_base="https://openapi.koreainvestment.com:9443",
        tr_ids={
            "buy": "TTTC0012U",
            "sell": "TTTC0011U",
            "cancel": "TTTC0013U",
            "daily_ccld": "TTTC0081R",
            "balance": "TTTC8434R",
            "psbl_order": "TTTC8908R",
        },
        rate_per_sec=18.0,
    ),
    KisEnv.PAPER: KisEnvProfile(
        url_base="https://openapivts.koreainvestment.com:29443",
        tr_ids={
            "buy": "VTTC0012U",
            "sell": "VTTC0011U",
            "cancel": "VTTC0013U",
            "daily_ccld": "VTTC0081R",
            "balance": "VTTC8434R",
            "psbl_order": "VTTC8908R",
        },
        rate_per_sec=4.0,
    ),
}


class Settings(BaseSettings):
    APP_KEY: SecretStr
    APP_SECRET: SecretStr
    CANO: str
    ACNT_PRDT_CD: str
    URL_BASE: str
    DISCORD_WEBHOOK_URL: SecretStr
    KIS_ENV: KisEnv = KisEnv.PAPER
    TOKEN_DIR: Path = Field(default_factory=lambda: PROJECT_ROOT)
    # None 이면 KIS_ENV 프로파일의 rate_per_sec 를 사용
    TOKEN_BUCKET_CAPACITY: int | None = None
    TOKEN_BUCKET_REFILL_RATE: float | None = None
    DATABASE_URL: str = Field(
        default_factory=lambda: f"sqlite:///{PROJECT_ROOT / 'trading.db'}"
    )
    DART_API_KEY: SecretStr | None = None
    ARCHIVE_ROOT: Path = Field(default_factory=lambda: PROJECT_ROOT / "reports")

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

    @model_validator(mode="after")
    def _url_matches_env(self) -> "Settings":
        """KIS_ENV 와 URL_BASE 호스트가 다르면 기동 중단 (PAPER 라벨로 실계좌 호출 방지)."""
        expected = urlparse(self.kis_profile.url_base).hostname
        actual = urlparse(self.URL_BASE).hostname
        if actual != expected:
            raise ValueError(
                f"URL_BASE host {actual!r} does not match KIS_ENV={self.KIS_ENV.value} "
                f"(expected {expected!r})"
            )
        return self

    @property
    def kis_profile(self) -> KisEnvProfile:
        return KIS_ENV_PROFILES[self.KIS_ENV]

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
