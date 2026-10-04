"""WBI 签名单测。测试向量取自 bilibili-API-collect 官方文档，必须逐字通过。"""

from __future__ import annotations

import pytest

from bili_danmaku.wbi import (
    MIXIN_KEY_ENC_TAB,
    WbiKeyCache,
    build_signed_query,
    encode_uri_component,
    get_mixin_key,
    parse_wbi_keys,
    sign_params,
)

IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"


def test_mixin_key_enc_tab_shape() -> None:
    assert len(MIXIN_KEY_ENC_TAB) == 64
    assert sorted(MIXIN_KEY_ENC_TAB) == list(range(64))


def test_get_mixin_key_official_vector() -> None:
    assert get_mixin_key(IMG_KEY + SUB_KEY) == "ea1db124af3c7062474693fa704f4ff8"


def test_get_mixin_key_rejects_short_input() -> None:
    with pytest.raises(ValueError):
        get_mixin_key("too-short")


def test_build_signed_query_official_vector() -> None:
    query = build_signed_query(
        {"foo": "114", "bar": "514", "zab": 1919810}, IMG_KEY, SUB_KEY, wts=1702204169
    )
    assert query == (
        "bar=514&foo=114&wts=1702204169&zab=1919810"
        "&w_rid=8f6f2b5b3d485fe1886cec6a0be8c5d4"
    )


def test_encode_uri_component_matches_javascript_semantics() -> None:
    # 空格必须是 %20（不是 +）
    assert encode_uri_component("one one four") == "one%20one%20four"
    # 中文百分号必须大写
    assert encode_uri_component("五一四") == "%E4%BA%94%E4%B8%80%E5%9B%9B"
    # !'()* 被过滤（与 encodeURIComponent 的差异在此处显式抹平）
    assert encode_uri_component("a!b'c(d)e*f") == "abcdef"


def test_sign_params_contains_wts_and_w_rid() -> None:
    signed = sign_params({"id": 804456, "type": 0}, IMG_KEY, SUB_KEY, wts=1702204169)
    assert signed["wts"] == "1702204169"
    assert len(signed["w_rid"]) == 32
    assert signed["id"] == "804456"


def test_parse_wbi_keys_from_nav_payload() -> None:
    payload = {
        "code": -101,
        "data": {
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
            }
        },
    }
    assert parse_wbi_keys(payload) == (IMG_KEY, SUB_KEY)


def test_parse_wbi_keys_missing_raises() -> None:
    with pytest.raises(ValueError):
        parse_wbi_keys({"data": {}})


def test_key_cache_ttl() -> None:
    cache = WbiKeyCache(ttl=100.0)
    assert not cache.is_fresh()
    cache.set(IMG_KEY, SUB_KEY)
    assert cache.is_fresh()
    assert cache.get() == (IMG_KEY, SUB_KEY)
    cache.invalidate()
    assert not cache.is_fresh()
