"""D11: KIS_ENV ↔ URL_BASE 정합성 + 환경별 TR_ID / 호출 한도."""
import pytest
from pydantic import ValidationError

from src.clients.kis import KisClient
from src.config import KIS_ENV_PROFILES, KisEnv

from tests.clients.conftest import LIVE_URL, PAPER_URL, _FakeTokenStore, make_settings


def test_live_env_with_live_url_ok():
    s = make_settings()
    assert s.kis_profile.tr_ids["buy"] == "TTTC0012U"


def test_paper_env_with_paper_url_ok():
    s = make_settings(KIS_ENV=KisEnv.PAPER, URL_BASE=PAPER_URL)
    assert s.kis_profile.tr_ids["buy"] == "VTTC0012U"


def test_paper_env_with_live_url_aborts():
    with pytest.raises(ValidationError, match="does not match KIS_ENV=PAPER"):
        make_settings(KIS_ENV=KisEnv.PAPER, URL_BASE=LIVE_URL)


def test_live_env_with_paper_url_aborts():
    with pytest.raises(ValidationError, match="does not match KIS_ENV=LIVE"):
        make_settings(URL_BASE=PAPER_URL)


def test_trailing_slash_still_matches():
    assert make_settings(URL_BASE=LIVE_URL + "/").URL_BASE == LIVE_URL


@pytest.mark.parametrize("env", list(KisEnv))
def test_profiles_cover_all_tr_keys(env):
    keys = {"buy", "sell", "cancel", "daily_ccld", "balance", "psbl_order"}
    assert set(KIS_ENV_PROFILES[env].tr_ids) == keys


def test_paper_tr_ids_are_v_prefixed_live_counterparts():
    live = KIS_ENV_PROFILES[KisEnv.LIVE].tr_ids
    paper = KIS_ENV_PROFILES[KisEnv.PAPER].tr_ids
    for k, v in live.items():
        assert paper[k] == "V" + v[1:]


def test_bucket_rate_defaults_to_env_profile():
    live = KisClient(make_settings(), token_store=_FakeTokenStore())
    paper = KisClient(
        make_settings(KIS_ENV=KisEnv.PAPER, URL_BASE=PAPER_URL), token_store=_FakeTokenStore()
    )
    assert live.bucket.refill_rate == 18.0 and live.bucket.capacity == 18
    assert paper.bucket.refill_rate == 4.0 and paper.bucket.capacity == 4


def test_bucket_rate_explicit_override():
    c = KisClient(
        make_settings(TOKEN_BUCKET_REFILL_RATE=2.0, TOKEN_BUCKET_CAPACITY=3),
        token_store=_FakeTokenStore(),
    )
    assert c.bucket.refill_rate == 2.0 and c.bucket.capacity == 3


def test_safety_defaults_are_conservative():
    s = make_settings()
    assert s.SHADOW_MODE is True
    assert s.MAX_DEPLOY_PCT == 5.0
    assert s.ALLOC_NEWS_SWING_PCT == 0.0 and s.ALLOC_BREAKOUT_PCT == 20.0


@pytest.mark.parametrize("pct", [0.0, -1.0, 100.1])
def test_max_deploy_pct_bounds(pct):
    with pytest.raises(ValidationError):
        make_settings(MAX_DEPLOY_PCT=pct)


def test_kill_switch_flag_and_file(tmp_path):
    flag = tmp_path / "TRADING_DISABLED"
    s = make_settings(KILL_SWITCH_FILE=flag)
    assert s.kill_switch_on() is False
    flag.touch()
    assert s.kill_switch_on() is True
    assert make_settings(TRADING_DISABLED=True, KILL_SWITCH_FILE=tmp_path / "x").kill_switch_on()
