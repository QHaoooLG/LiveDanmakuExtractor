"""凭据与隐私单测。核心断言：凭据永不进入日志，且只保留白名单字段。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from bili_danmaku.credentials import (
    ALLOWED_COOKIE_KEYS,
    Credentials,
    load_credentials,
    purge_credentials,
    save_credentials,
)
from bili_danmaku.utils import (
    RedactingFilter,
    anonymize_uid,
    anonymize_uname,
    clear_registered_secrets,
    mask,
    redact,
    register_secret,
)

FAKE_SESSDATA = "abcdef1234567890abcdef1234567890"


@pytest.fixture(autouse=True)
def _clear_bili_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(ALLOWED_COOKIE_KEYS) + ["LOG_LEVEL"]:
        monkeypatch.delenv(f"BILI_{key.upper()}", raising=False)
    clear_registered_secrets()


# --------------------------------------------------------------------------- #
# 掩码
# --------------------------------------------------------------------------- #


def test_mask_keeps_only_edges() -> None:
    assert mask(FAKE_SESSDATA) == "abcd********7890"
    assert FAKE_SESSDATA[4:-4] not in mask(FAKE_SESSDATA)


def test_mask_handles_short_and_empty() -> None:
    assert mask("") == ""
    assert mask(None) == ""
    assert mask("abc") == "***"


def test_redact_removes_cookie_values() -> None:
    text = f"Cookie: SESSDATA={FAKE_SESSDATA}; bili_jct=deadbeef; DedeUserID=123"
    cleaned = redact(text)
    assert FAKE_SESSDATA not in cleaned
    assert "deadbeef" not in cleaned
    assert "<redacted>" in cleaned


def test_redact_removes_json_style_secrets() -> None:
    cleaned = redact(json.dumps({"cookies": {"SESSDATA": FAKE_SESSDATA}}))
    assert FAKE_SESSDATA not in cleaned


def test_redacting_filter_cleans_log_record() -> None:
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="认证使用 cookie SESSDATA=%s",
        args=(FAKE_SESSDATA,),
        exc_info=None,
    )
    RedactingFilter().filter(record)
    assert FAKE_SESSDATA not in record.getMessage()


def test_logger_output_never_contains_secret(caplog: pytest.LogCaptureFixture) -> None:
    """只打印凭据值本身、不带字段名时也必须被脱敏 —— 靠登记表而非模式匹配。"""
    register_secret(FAKE_SESSDATA)
    logger = logging.getLogger("bili_danmaku.test_privacy")
    logger.addFilter(RedactingFilter())
    try:
        with caplog.at_level(logging.INFO):
            logger.info("token=%s", FAKE_SESSDATA)
    finally:
        logger.removeFilter(RedactingFilter())
    assert FAKE_SESSDATA not in caplog.text
    assert "<redacted>" in caplog.text


def test_credentials_registers_its_secrets() -> None:
    clear_registered_secrets()
    Credentials(cookies={"SESSDATA": FAKE_SESSDATA})
    assert FAKE_SESSDATA not in redact(f"raw={FAKE_SESSDATA}")


def test_short_values_are_not_registered() -> None:
    """过短的值不登记，避免误伤正常文本。"""
    clear_registered_secrets()
    register_secret("abc")
    assert redact("abc") == "abc"


def test_anonymize_helpers() -> None:
    assert anonymize_uid(0, "salt") is None
    assert anonymize_uid(12345, "salt").startswith("u_")  # type: ignore[union-attr]
    assert anonymize_uid(12345, "salt") != anonymize_uid(12345, "other-salt")
    assert anonymize_uname(None, "salt") is None
    assert anonymize_uname("某人", "salt").startswith("user_")  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def test_from_cookie_string_drops_non_whitelist_keys() -> None:
    creds = Credentials.from_cookie_string(
        f"SESSDATA={FAKE_SESSDATA}; bili_jct=jct; buvid3=b3; evil_tracker=xyz; sid=abc"
    )
    assert "evil_tracker" not in creds.cookies
    assert "sid" not in creds.cookies
    assert set(creds.cookies) <= set(ALLOWED_COOKIE_KEYS)
    assert creds.logged_in is True


def test_dede_user_id_parsing() -> None:
    assert Credentials(cookies={"DedeUserID": "8431064"}).dede_user_id == 8431064
    assert Credentials(cookies={"DedeUserID": "abc"}).dede_user_id == 0
    assert Credentials().dede_user_id == 0


def test_describe_never_leaks_plaintext() -> None:
    creds = Credentials(cookies={"SESSDATA": FAKE_SESSDATA, "buvid3": "buvid-value"})
    described = creds.describe()
    assert FAKE_SESSDATA not in described
    assert "buvid-value" not in described
    assert "abcd********7890" in described


def test_describe_when_anonymous() -> None:
    assert "未登录" in Credentials().describe()


def test_with_buvid_merges_without_losing_credentials() -> None:
    creds = Credentials(cookies={"SESSDATA": FAKE_SESSDATA})
    merged = creds.with_buvid("BV3", "BV4")
    assert merged.sessdata == FAKE_SESSDATA
    assert merged.buvid3 == "BV3"
    assert merged.cookies["buvid4"] == "BV4"


# --------------------------------------------------------------------------- #
# 加载优先级
# --------------------------------------------------------------------------- #


def test_load_prefers_env_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "cookies.json"
    save_credentials(Credentials(cookies={"SESSDATA": "from-file-value"}), store)
    monkeypatch.setenv("BILI_SESSDATA", "from-env-value")

    creds = load_credentials(str(store))
    assert creds.sessdata == "from-env-value"
    assert creds.source == "env"


def test_load_from_file(tmp_path: Path) -> None:
    store = tmp_path / "cookies.json"
    save_credentials(Credentials(cookies={"SESSDATA": FAKE_SESSDATA, "buvid3": "b3"}), store)

    creds = load_credentials(str(store))
    assert creds.sessdata == FAKE_SESSDATA
    assert creds.source == "file"


def test_load_falls_back_to_inline(tmp_path: Path) -> None:
    creds = load_credentials(str(tmp_path / "missing.json"), inline_cookie=f"SESSDATA={FAKE_SESSDATA}")
    assert creds.sessdata == FAKE_SESSDATA
    assert creds.source == "inline"


def test_load_returns_empty_when_nothing_available(tmp_path: Path) -> None:
    creds = load_credentials(str(tmp_path / "missing.json"))
    assert creds.logged_in is False
    assert creds.source == "none"


def test_corrupt_store_is_tolerated(tmp_path: Path) -> None:
    store = tmp_path / "cookies.json"
    store.write_text("{ not valid json", encoding="utf-8")
    assert load_credentials(str(store)).logged_in is False


# --------------------------------------------------------------------------- #
# 落盘与清除
# --------------------------------------------------------------------------- #


def test_saved_file_contains_only_whitelist_fields(tmp_path: Path) -> None:
    store = tmp_path / "cookies.json"
    save_credentials(
        Credentials(cookies={"SESSDATA": FAKE_SESSDATA, "uname": "不应保存", "mid": "1"}),
        store,
    )
    payload = json.loads(store.read_text(encoding="utf-8"))
    assert set(payload["cookies"]) <= set(ALLOWED_COOKIE_KEYS)
    assert "uname" not in payload["cookies"]
    assert "mid" not in payload["cookies"]
    # 账号资料不得以任何形式落盘
    assert "不应保存" not in store.read_text(encoding="utf-8")


def test_purge_removes_credentials(tmp_path: Path) -> None:
    store = tmp_path / "cookies.json"
    save_credentials(Credentials(cookies={"SESSDATA": FAKE_SESSDATA}), store)
    assert store.is_file()

    removed = purge_credentials(str(store))
    assert str(store) in removed
    assert not store.is_file()


def test_purge_on_missing_file_is_noop(tmp_path: Path) -> None:
    assert purge_credentials(str(tmp_path / "nope.json")) == []
