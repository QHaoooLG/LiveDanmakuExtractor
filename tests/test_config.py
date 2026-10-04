"""配置加载与校验单测。非法配置必须「启动即失败」。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bili_danmaku.config import Config, ConfigError

MISSING = "definitely-not-here.toml"


def test_defaults_are_valid_and_match_tech_design() -> None:
    config = Config.load(path=MISSING)
    assert config.client.heartbeat_interval == 30.0
    assert config.client.heartbeat_timeout == 90.0
    assert config.client.protover == 3
    assert config.sinks.keep_raw is True  # 需求：默认保留 raw
    assert config.sinks.types == ["jsonl", "csv", "sqlite"]
    assert config.web.port == 8787
    assert config.general.anonymize is False


def test_rejects_heartbeat_interval_over_30() -> None:
    with pytest.raises(ConfigError, match="heartbeat_interval"):
        Config.load(path=MISSING, overrides={"client": {"heartbeat_interval": 31}})


def test_rejects_heartbeat_timeout_not_greater_than_interval() -> None:
    with pytest.raises(ConfigError, match="heartbeat_timeout"):
        Config.load(
            path=MISSING,
            overrides={"client": {"heartbeat_interval": 20, "heartbeat_timeout": 20}},
        )


def test_rejects_unsupported_protover() -> None:
    with pytest.raises(ConfigError, match="protover"):
        Config.load(path=MISSING, overrides={"client": {"protover": 5}})


def test_rejects_empty_sink_types() -> None:
    with pytest.raises(ConfigError, match="sinks.types"):
        Config.load(path=MISSING, overrides={"sinks": {"types": []}})


def test_rejects_unknown_sink_type() -> None:
    with pytest.raises(ConfigError, match="未知类型"):
        Config.load(path=MISSING, overrides={"sinks": {"types": ["jsonl", "parquet"]}})


def test_rejects_non_loopback_host_without_token() -> None:
    with pytest.raises(ConfigError, match="token"):
        Config.load(path=MISSING, overrides={"web": {"host": "0.0.0.0"}})


def test_allows_non_loopback_host_with_token() -> None:
    config = Config.load(
        path=MISSING, overrides={"web": {"host": "0.0.0.0", "token": "s3cret"}}
    )
    assert config.web.host == "0.0.0.0"


def test_allows_loopback_without_token() -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        assert Config.load(path=MISSING, overrides={"web": {"host": host}}).web.host == host


def test_rejects_invalid_port() -> None:
    with pytest.raises(ConfigError, match="web.port"):
        Config.load(path=MISSING, overrides={"web": {"port": 70000}})


def test_rejects_zero_max_rooms() -> None:
    with pytest.raises(ConfigError, match="max_rooms"):
        Config.load(path=MISSING, overrides={"general": {"max_rooms": 0}})


def test_loads_from_toml_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[general]
log_level = "DEBUG"
anonymize = true

[sinks]
out_dir = "custom-out"
keep_raw = false
""",
        encoding="utf-8",
    )
    config = Config.load(path=path)
    assert config.general.log_level == "DEBUG"
    assert config.general.anonymize is True
    assert config.sinks.out_dir == "custom-out"
    assert config.sinks.keep_raw is False


def test_env_var_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[sinks]\nout_dir = "from-file"\n', encoding="utf-8")
    monkeypatch.setenv("BILI_OUT_DIR", "from-env")
    assert Config.load(path=path).sinks.out_dir == "from-env"


def test_cli_overrides_beat_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILI_OUT_DIR", "from-env")
    config = Config.load(path=MISSING, overrides={"sinks": {"out_dir": "from-cli"}})
    assert config.sinks.out_dir == "from-cli"


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("this is not = valid = toml", encoding="utf-8")
    with pytest.raises(ConfigError, match="解析失败"):
        Config.load(path=path)
