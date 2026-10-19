from datetime import datetime, timedelta

import pytest
from pydantic_settings import SettingsConfigDict

from src.clients.kis import CachedToken, KisClient
from src.clients.rate_limiter import TokenBucket
from src.config import KisEnv, Settings


LIVE_URL = "https://openapi.koreainvestment.com:9443"
PAPER_URL = "https://openapivts.koreainvestment.com:29443"


class _HermeticSettings(Settings):
    """init 인자만 사용 — 실제 설정 파일·환경변수가 테스트에 섞이지 않게."""

    model_config = SettingsConfigDict(yaml_file=None, yaml_file_encoding=None, env_file=None)

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, *args, **kwargs):
        return (init_settings,)


def make_settings(**over) -> Settings:
    base = dict(
        APP_KEY="k" * 10,
        APP_SECRET="s" * 10,
        CANO="12345678",
        ACNT_PRDT_CD="01",
        URL_BASE=LIVE_URL,
        DISCORD_WEBHOOK_URL="https://example.invalid/webhook",
        KIS_ENV=KisEnv.LIVE,
    )
    base.update(over)
    return _HermeticSettings(**base)


class _FakeTokenStore:
    def read(self):
        return CachedToken(value="tok", valid_until=datetime.now() + timedelta(hours=1))

    def move_expired(self):
        return 0


class FakeResponse:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture
def live_client():
    return KisClient(
        make_settings(),
        bucket=TokenBucket(capacity=1000, refill_rate=1000.0),
        token_store=_FakeTokenStore(),
    )
