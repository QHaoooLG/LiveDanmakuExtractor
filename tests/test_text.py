"""V2 词云单测：词频统计、表情占位符的单独处理、三种输出。

对应需求方要求里的"**表情占位符（`[dog]`）建议单独处理**，
不要把 `[dog]` 当词统计" —— 这一条在 `test_emote_is_counted_separately...`
与 `test_include_emotes_marks_them_with_brackets` 里被钉死。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bili_danmaku import text
from bili_danmaku.text import (
    DEFAULT_STOPWORDS,
    MissingDependency,
    WordCloudReport,
    clean_text,
    count_words,
    extract_emotes,
    fallback_tokens,
    find_cjk_font,
    jieba_available,
    load_stopwords,
    load_user_dict,
    render_html,
    render_png,
    render_text,
    strip_emotes,
    tokenize,
)

REAL_EMOTS = {
    "[dog]": {
        "url": "https://i0.hdslb.com/bfs/live/dog.png",
        "width": 20,
        "height": 20,
        "emoticon_id": 1,
        "emoticon_unique": "room_1234_1",
    }
}


# --------------------------------------------------------------------------- #
# 表情占位符
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("哈哈[dog]", ["dog"]),
        ("[大笑][狗头]", ["大笑", "狗头"]),
        ("no emote here", []),
        ("[abc_1]", ["abc_1"]),
        ("[]", []),  # 空占位符不算
        ("[a" + "b" * 40 + "]", []),  # 超长不算（避免吃掉正文方括号）
    ],
)
def test_extract_emotes(raw: str, expected: list[str]) -> None:
    assert extract_emotes(raw) == expected


def test_strip_emotes_keeps_rest_of_text() -> None:
    assert strip_emotes("哈哈[dog]真可爱") == "哈哈 真可爱"
    assert strip_emotes("") == ""


def test_clean_text_removes_urls_mentions_and_emotes() -> None:
    cleaned = clean_text("看 https://b23.tv/abc 和 www.x.com @某人 [dog]   好多   空格")
    assert "http" not in cleaned and "www" not in cleaned
    assert "@某人" not in cleaned
    assert "[dog]" not in cleaned
    assert "  " not in cleaned, "连续空白应被压缩"
    assert cleaned.startswith("看")


def test_emote_is_counted_separately_from_words() -> None:
    """核心契约：`[dog]` 不能进词语榜，但要在表情榜里。"""
    records = [
        ("danmaku", "哈哈[dog]真可爱[dog]", {"emots": REAL_EMOTS}),
        ("danmaku", "哈哈[dog]", {"emots": REAL_EMOTS}),
    ]
    report = count_words(records, top=20)

    words = dict(report.words)
    assert "dog" not in words, "表情占位符不能当词统计"
    assert "[dog]" not in words
    assert all("[" not in word for word, _ in report.words)

    assert [item.name for item in report.emotes] == ["dog"]
    assert report.emotes[0].count == 3
    assert report.emotes[0].url == REAL_EMOTS["[dog]"]["url"], "应带上图片链接"
    assert report.emotes[0].emoticon_unique == "room_1234_1"


def test_include_emotes_marks_them_with_brackets() -> None:
    """`--include-emotes` 时才进词语榜，且用带方括号的原形以示区分。"""
    records = [("danmaku", "打卡[dog]", {"emots": REAL_EMOTS})] * 3
    report = count_words(records, top=20, include_emotes=True)
    words = dict(report.words)
    assert words["[dog]"] == 3
    assert "dog" not in words
    assert [item.name for item in report.emotes] == ["dog"], "表情榜仍然照常给"


def test_emoticon_unique_fills_when_single_emote() -> None:
    """实测主播自定义表情的 text 就是表情名，此时 emots 为空、只有 emoticon_unique。"""
    records = [("danmaku", "大拇哥", {"emoticon_unique": "room_9_7"})]
    report = count_words(records, top=10)
    # 没有方括号占位符 → 表情榜为空（"大拇哥"会被当普通词，因为有文字）
    assert report.emotes == []
    assert dict(report.words).get("大拇哥") == 1


# --------------------------------------------------------------------------- #
# 分词
# --------------------------------------------------------------------------- #


def test_tokenize_uses_jieba_when_available() -> None:
    if not jieba_available():
        pytest.skip("未安装 jieba")
    result = tokenize("主播好可爱哈哈哈")
    assert "主播" in result.words
    assert "可爱" in result.words


def test_tokenize_fallback_bigram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(text, "jieba_available", lambda: False)
    result = tokenize("主播好可爱")
    # 二元组：主播/播好/好可/可爱（单字被 min_length=2 挡掉）
    assert result.words == ["主播", "播好", "好可", "可爱"]


def test_tokenize_rejects_unknown_tokenizer_mode() -> None:
    """未知模式不应静默当成 auto —— 明确报错比猜错分词器好。"""
    with pytest.raises(ValueError):
        tokenize("主播好", tokenizer="nope")


def test_tokenize_drops_stopwords_by_default() -> None:
    result = tokenize("这个是主播的弹幕")
    assert "这个" not in result.words
    assert "的" not in result.words
    assert "主播" in result.words
    assert "弹幕" in result.words


def test_tokenize_empty_stopwords_keeps_function_words() -> None:
    result = tokenize("这个是主播", stopwords=set())
    assert "这个" in result.words


def test_tokenize_length_bounds() -> None:
    result = tokenize("主播好可爱", min_length=1, max_length=2)
    assert all(len(word) <= 2 for word in result.words)
    assert "好" in result.words, "min_length=1 应保留单字"


def test_tokenize_keeps_ascii_words_and_numbers() -> None:
    result = tokenize("up主加油666 AAA")
    assert "up" in result.words
    assert "666" in result.words
    assert "aaa" in result.words, "英文应小写归一"


def test_tokenize_handles_blank_text() -> None:
    result = tokenize("   ")
    assert result.words == []
    assert result.cleaned == ""


def test_fallback_tokens_single_cjk_char() -> None:
    assert fallback_tokens("好") == ["好"]


def test_load_user_dict_reports_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        load_user_dict("definitely-not-a-dict.txt")


def test_load_user_dict_adds_words(tmp_path: Path) -> None:
    if not jieba_available():
        pytest.skip("未安装 jieba")
    dictionary = tmp_path / "room.dict"
    dictionary.write_text("# 房间黑话\n跑刀\n\n", encoding="utf-8")
    assert load_user_dict(dictionary) == 1
    # 同一个文件第二次加载是 no-op
    assert load_user_dict(dictionary) == 0


def test_load_user_dict_without_jieba_returns_zero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(text, "jieba_available", lambda: False)
    dictionary = tmp_path / "d.txt"
    dictionary.write_text("词\n", encoding="utf-8")
    assert load_user_dict(dictionary) == 0


def test_load_stopwords_file(tmp_path: Path) -> None:
    path = tmp_path / "stop.txt"
    path.write_text("# 注释\n哈哈\n 主播 \n", encoding="utf-8")
    assert load_stopwords(path) == {"哈哈", "主播"}


def test_load_stopwords_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        load_stopwords("definitely-missing.txt")


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


def test_count_words_accepts_plain_strings() -> None:
    report = count_words(["主播好", "主播棒"], top=10)
    assert report.total_texts == 2
    assert dict(report.words)["主播"] == 2


def test_count_words_sorts_by_count_then_word_for_stability() -> None:
    """同频词按词形升序 —— 否则同一份数据两次跑出来的榜单不一样。"""
    report = count_words(["bbb aaa", "aaa bbb", "ccc"], top=10)
    assert [word for word, _ in report.words] == ["aaa", "bbb", "ccc"]


def test_count_words_filters_by_type() -> None:
    records = [
        ("danmaku", "弹幕内容", {}),
        ("super_chat", "醒目留言", {}),
        ("interact", "进入了直播间", {}),
    ]
    only_danmaku = count_words(records, types=["danmaku"], top=10)
    assert {word for word, _ in only_danmaku.words} == {"弹幕", "内容"}

    both = count_words(records, types=["danmaku", "super_chat"], top=10)
    assert {word for word, _ in both.words} == {"弹幕", "内容", "醒目", "留言"}

    all_types = count_words(records, top=10)
    # 不传 types 时不过滤类型：interact 这类不在默认采集范围的事件也会被统计
    assert {"进入", "直播间"} <= {word for word, _ in all_types.words}


def test_count_words_respects_min_count_and_top() -> None:
    records = ["主播", "主播", "主播", "偶尔"]
    assert dict(count_words(records, min_count=2, top=10).words) == {"主播": 3}
    assert len(count_words(records, top=1).words) == 1


def test_count_words_skips_blank_text_and_counts_it() -> None:
    report = count_words(["主播", "", "   "], top=10)
    assert report.total_texts == 3
    assert report.skipped == 2
    assert report.total_words == 1


def test_count_words_records_filters_for_audit() -> None:
    report = count_words(["主播"], top=5, filters={"session": "s1", "room": 804456})
    payload = report.as_dict()
    assert payload["filters"] == {"session": "s1", "room": 804456}


def test_report_as_dict_shape() -> None:
    report = count_words([("danmaku", "主播[dog]", {"emots": REAL_EMOTS})], top=5)
    payload = report.as_dict()
    assert payload["total_texts"] == 1
    assert payload["distinct_words"] == 1
    assert payload["tokenizer"] in {"jieba", "fallback"}
    assert payload["stopwords"] == len(DEFAULT_STOPWORDS)
    assert isinstance(payload["words"], list)
    assert isinstance(payload["emotes"], list)
    json.dumps(payload, ensure_ascii=False)  # 必须可序列化（JSON 输出依赖它）


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


def test_render_text_contains_ranking_and_emotes() -> None:
    report = count_words(
        [("danmaku", "主播加油[dog]", {"emots": REAL_EMOTS})] * 3, top=10
    )
    rendered = render_text(report, top=5)
    assert "主播" in rendered
    assert "表情占位符" in rendered
    assert "[dog]" in rendered
    assert "https://i0.hdslb.com" in rendered


def test_render_text_handles_empty_report() -> None:
    rendered = render_text(WordCloudReport([], [], 0, 0, 0, "fallback", 0, 2, 12), top=5)
    assert "没有统计到任何词" in rendered


def test_render_html_is_self_contained_and_escaped() -> None:
    report = count_words(["主播<script>alert(1)</script>", "主播"], top=10, min_length=1)
    html = render_html(report, title="词云")
    assert "<html" in html and "</html>" in html
    assert "http://" not in html and "https://cdn" not in html, "不应依赖外部资源"
    assert "<script>" not in html, "正文必须转义，否则弹幕就是 XSS 载体"


def test_render_html_without_words() -> None:
    html = render_html(WordCloudReport([], [], 0, 0, 0, "fallback", 0, 2, 12))
    assert "没有统计到任何词" in html


def test_render_html_includes_emote_section() -> None:
    report = count_words([("danmaku", "打卡[dog]", {"emots": REAL_EMOTS})], top=10)
    assert "表情占位符" in render_html(report)


def test_find_cjk_font_returns_existing_path_or_none() -> None:
    font = find_cjk_font()
    if font is not None:
        assert font.is_file()


def test_render_png_writes_file(tmp_path: Path) -> None:
    pytest.importorskip("wordcloud")
    if find_cjk_font() is None:
        pytest.skip("系统没有中文字体")
    report = count_words([("danmaku", "主播加油打卡", {})] * 5, top=10)
    target = render_png(report, tmp_path / "cloud.png", width=320, height=200)
    assert target.is_file()
    assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "应是合法 PNG"


def test_render_png_requires_words(tmp_path: Path) -> None:
    pytest.importorskip("wordcloud")
    report = count_words([""], top=10)
    with pytest.raises(ValueError, match="没有可渲染的词"):
        render_png(report, tmp_path / "x.png")


def test_render_png_reports_missing_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 wordcloud 时必须给可执行的提示，而不是 ImportError 堆栈。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):
        if name == "wordcloud":
            raise ImportError("no wordcloud")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)
    report = count_words(["主播加油"], top=5)
    with pytest.raises(MissingDependency, match="wordcloud"):
        render_png(report, tmp_path / "x.png")


def test_render_png_reports_missing_font(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("wordcloud")
    monkeypatch.setattr(text, "find_cjk_font", lambda: None)
    report = count_words(["主播加油"], top=5)
    with pytest.raises(MissingDependency, match="字体"):
        render_png(report, tmp_path / "x.png")


def test_render_png_accepts_explicit_font(tmp_path: Path) -> None:
    pytest.importorskip("wordcloud")
    font = find_cjk_font()
    if font is None:
        pytest.skip("系统没有中文字体")
    report = count_words(["主播加油"], top=5)
    target = render_png(report, tmp_path / "f.png", font_path=font, width=256, height=160)
    assert target.is_file()


def test_module_has_no_hard_dependency_on_optional_packages() -> None:
    """`import bili_danmaku.text` 不该拖进 wordcloud/matplotlib（启动成本 + 沙箱写缓存）。"""
    source = Path(text.__file__).read_text(encoding="utf-8")
    top_level = [
        line for line in source.splitlines()
        if re.match(r"^(import|from)\s+(wordcloud|matplotlib|jieba)\b", line)
    ]
    assert top_level == [], f"可选依赖必须延迟导入: {top_level}"
