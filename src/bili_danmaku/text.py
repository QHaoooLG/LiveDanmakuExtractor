"""文本分析：把弹幕正文切成词，并单独处理表情占位符（V2 词云的基础）。

**为什么单独处理表情**：B 站的弹幕正文里，表情是内嵌的占位符文本
（`哈哈[dog]`），`extra.emots` 给出 `[dog]` → 图片元数据的映射。
如果把 `[dog]` 当普通词统计，词频榜会被表情刷屏（而且 `[`、`]` 也不该入词），
所以这里把表情抽成**独立的 token**，默认不计入词语统计，但可以单独出榜。

分词策略：

* 装了 `jieba` 就用它（中文分词质量好得多）；
* 没装则退回**内置兜底分词器**：中文按二元组（bigram）+ 英文数字按整词。
  兜底不是为了效果，而是为了"没装可选依赖时功能仍然可用、测试仍然可跑"
  （本机 pip 不可用，jieba 只有 sdist，装不上的环境不该直接报错）。

`count_words()` 是纯函数、无 IO，因此可以完全离线单测。
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

log = logging.getLogger("bili_danmaku.text")

#: B 站表情占位符：`[dog]`、`[大笑]`、`[abc_1]`。
#: 允许中英文、数字、下划线、连字符与空格；长度设上限是为了别把正文里的
#: 中文方括号（如「【】」不算，但半角 `[真的吗]` 会被误判）整段吃掉。
EMOT_PATTERN = re.compile(r"\[([A-Za-z0-9_\-\u4e00-\u9fff ]{1,20})\]")

#: 中文按"连续 CJK"整体切块，块内再分二元组
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
#: 英文/数字整词（保留 `v50`、`666`、`up`）
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_+#\-]*|[0-9]+(?:\.[0-9]+)?")
#: URL 与 @提及：整段丢掉，否则 `https`、`com` 会污染词频
_URL = re.compile(r"https?://\S+|www\.\S+")
_MENTION = re.compile(r"@[\w\u4e00-\u9fff\-]+")

#: 默认停用词：只放**真正的功能词/语气词**，刻意保持很短 ——
#: 弹幕里 `哈哈`/`主播`/`什么` 这类恰恰是有效信号，不能当噪声清掉。
DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    {
        "的", "了", "是", "在", "我", "你", "他", "她", "它", "们", "这", "那", "有",
        "和", "就", "都", "也", "还", "把", "被", "给", "让", "对", "到", "从", "与",
        "而", "但", "如果", "因为", "所以", "然后", "一个", "一下", "什么", "怎么",
        "这个", "那个", "一下", "啊", "呀", "吧", "吗", "呢", "哦", "嗯", "额",
        "the", "a", "an", "is", "are", "to", "of", "and", "or", "in", "on", "for",
    }
)

#: 单字默认丢弃（中文单字歧义太大）；想保留就传 min_length=1
DEFAULT_MIN_LENGTH = 2
#: 超长 token 丢弃（多半是没切开的长句）
DEFAULT_MAX_LENGTH = 12


@dataclass
class Tokenization:
    """一条弹幕的切词结果。"""

    cleaned: str
    words: list[str] = field(default_factory=list)
    emotes: list[str] = field(default_factory=list)

    @property
    def has_emote(self) -> bool:
        return bool(self.emotes)


@dataclass
class EmoteStat:
    """一个表情占位符的统计。"""

    name: str
    count: int = 0
    url: str | None = None
    emoticon_unique: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": self.count,
            "url": self.url,
            "emoticon_unique": self.emoticon_unique,
        }


@dataclass
class WordCloudReport:
    """词云数据（含元信息，便于 JSON 输出与排障）。"""

    words: list[tuple[str, int]]
    emotes: list[EmoteStat]
    total_texts: int
    total_words: int
    skipped: int
    tokenizer: str
    stopwords: int
    min_length: int
    max_length: int
    filters: dict[str, Any] = field(default_factory=dict)

    @property
    def distinct(self) -> int:
        return len(self.words)

    def as_dict(self, top: int | None = None) -> dict[str, Any]:
        words = self.words if top is None else self.words[:top]
        return {
            "total_texts": self.total_texts,
            "total_words": self.total_words,
            "distinct_words": self.distinct,
            "skipped_empty": self.skipped,
            "tokenizer": self.tokenizer,
            "stopwords": self.stopwords,
            "min_length": self.min_length,
            "max_length": self.max_length,
            "filters": self.filters,
            "words": [{"word": word, "count": count} for word, count in words],
            "emotes": [item.as_dict() for item in self.emotes],
        }


# --------------------------------------------------------------------------- #
# 切词
# --------------------------------------------------------------------------- #

_jieba: Any = None
_jieba_checked = False
_jieba_dicts: set[str] = set()
#: 已经提交给 jieba 的**用户词典**词（进程内去重；见 `add_custom_words`）
_LOADED_CUSTOM_WORDS: set[str] = set()


def jieba_available() -> bool:
    """探测 jieba 是否可用（结果缓存）。"""
    global _jieba, _jieba_checked
    if not _jieba_checked:
        _jieba_checked = True
        try:
            import jieba as _module  # type: ignore[import-not-found]

            # jieba 默认往 stderr 打"Building prefix dict…"，会污染 CLI 输出与管道
            _module.setLogLevel(60)
            _jieba = _module
        except Exception:  # noqa: BLE001 - 缺依赖不是错误，走兜底分词
            _jieba = None
    return _jieba is not None


def load_user_dict(path: str | Path) -> int:
    """加载 jieba 自定义词典（一行一个词，`#` 开头为注释）。

    房间黑话/主播名/梗常常分不出来，用这个补；没有 jieba 时返回 0。
    同一个文件只加载一次。
    """
    if not jieba_available():
        return 0
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"自定义词典不存在: {target}")

    key = str(target.resolve())
    if key in _jieba_dicts:
        return 0
    _jieba_dicts.add(key)

    count = 0
    for line in target.read_text(encoding="utf-8").splitlines():
        word = line.strip()
        if not word or word.startswith("#"):
            continue
        # jieba 的 load_userdict 支持 "词 词频 词性"，这里只喂词
        _jieba.add_word(word)
        count += 1
    log.debug("已加载自定义词典 %s（%d 词）", target, count)
    return count


def strip_emotes(text: str) -> str:
    """去掉表情占位符，保留其余正文。"""
    return EMOT_PATTERN.sub(" ", text or "")


def extract_emotes(text: str) -> list[str]:
    """抽出一行文本里的所有表情占位符名（不含方括号）。"""
    return [match.group(1) for match in EMOT_PATTERN.finditer(text or "")]


def clean_text(text: str) -> str:
    """抹掉链接、@提及、表情占位符，压缩空白。"""
    cleaned = _URL.sub(" ", text or "")
    cleaned = _MENTION.sub(" ", cleaned)
    cleaned = EMOT_PATTERN.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _ascii_words(chunk: str) -> Iterator[str]:
    for match in _ASCII_WORD.finditer(chunk):
        yield match.group(0)


def fallback_tokens(text: str) -> list[str]:
    """内置兜底分词：中文二元组 + 英文数字整词。

    只用于"没装 jieba"的场景；二元组会产生重叠词（`主播`/`播好`），
    这是刻意取舍 —— 宁可多几个噪声词，也不要整句当一个 token。
    """
    tokens: list[str] = []
    for chunk in _CJK_RUN.findall(text):
        if len(chunk) == 1:
            # 单个汉字：交给 min_length 过滤决定去留，这里先原样产出
            tokens.append(chunk)
            continue
        tokens.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
    tokens.extend(_ascii_words(text))
    return tokens


def tokenize(
    text: str,
    *,
    tokenizer: str = "auto",
    stopwords: Iterable[str] | None = None,
    min_length: int = DEFAULT_MIN_LENGTH,
    max_length: int = DEFAULT_MAX_LENGTH,
) -> Tokenization:
    """把一条弹幕切成词。

    `tokenizer`：`auto`（有 jieba 就用）/ `jieba` / `fallback`。
    `stopwords`：为 None 时用 `DEFAULT_STOPWORDS`；传空集表示不过滤。
    """
    if tokenizer not in ("auto", "jieba", "fallback"):
        raise ValueError(f"未知分词器 {tokenizer!r}，只支持 auto / jieba / fallback")

    cleaned = clean_text(text)
    emotes = extract_emotes(text)
    if not cleaned:
        return Tokenization(cleaned=cleaned, words=[], emotes=emotes)

    use_jieba = tokenizer == "jieba" or (tokenizer == "auto" and jieba_available())
    if use_jieba and jieba_available():
        raw = [piece.strip() for piece in _jieba.cut(cleaned)]
    else:
        raw = fallback_tokens(cleaned)

    blocked = DEFAULT_STOPWORDS if stopwords is None else frozenset(stopwords)
    words: list[str] = []
    for word in raw:
        token = word.strip().lower()
        if not token or token in blocked:
            continue
        if not (min_length <= len(token) <= max_length):
            continue
        # 纯符号/纯空白丢弃（兜底分词可能产出）
        if not any(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in token):
            continue
        words.append(token)

    return Tokenization(cleaned=cleaned, words=words, emotes=emotes)


def add_custom_words(words: Iterable[str]) -> int:
    """把词加进 jieba 的用户词典，返回**本次真正新增**的条数。

    为什么需要（需求方反馈"统计的词有一部分不符合日常表达"）：
    jieba 的通用词典里没有**主播名 / 房间标题里的专名**，于是
    `军哥`、`布拉德`、`沃夫` 这类名字会被切成碎片（`军`+`哥`），
    榜单里就会出现半截词。把它们加进用户词典，`jieba.cut` 就会整词输出。

    三个实现要点：

    * **jieba 不在时静默跳过**（返回 0）—— 用户词典是锦上添花，
      没有 jieba 时退到内置二元组分词器，功能不能因此不可用；
    * **进程内去重**：`jieba.add_word` 有一定开销，而同一次采集里
      同一批名字会被反复提交（每次分析/每次刷新），所以记下加过的词；
    * **只加长度 ≥2 的词**：单字词加进词典会显著改变切分行为（副作用大），
      而单字本来就被 `min_length=2` 过滤掉。
    """
    candidates = {
        str(word).strip()
        for word in words
        if word and len(str(word).strip()) >= 2
    }
    if not candidates:
        return 0
    fresh = sorted(candidates - _LOADED_CUSTOM_WORDS)
    if not fresh:
        return 0
    _LOADED_CUSTOM_WORDS.update(fresh)
    if not jieba_available():
        return 0
    for word in fresh:
        try:
            _jieba.add_word(word)
        except Exception:  # noqa: BLE001 - 词典问题绝不能让分析失败
            log.debug("add_word 失败，忽略：%r", word)
    return len(fresh)


def load_stopwords(path: str | Path) -> set[str]:
    """从文件读停用词（一行一个，`#` 注释）。"""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"停用词文件不存在: {target}")
    return {
        line.strip().lower()
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #


def count_words(
    records: Iterable[tuple[str, str, dict[str, Any]] | str],
    *,
    top: int = 50,
    min_count: int = 1,
    min_length: int = DEFAULT_MIN_LENGTH,
    max_length: int = DEFAULT_MAX_LENGTH,
    stopwords: Iterable[str] | None = None,
    tokenizer: str = "auto",
    types: Sequence[str] | None = None,
    include_emotes: bool = False,
    filters: dict[str, Any] | None = None,
) -> WordCloudReport:
    """统计词频。

    入参是 `(type, text, extra)` 三元组（`DanmakuStore.iter_texts()` 的形状）
    或纯字符串。`extra.emots` 存在时用来补全表情的图片元数据。

    返回**已按次数降序、次数相同按词形升序**排好的结果 —— 排序稳定，
    输出可复现（否则同一份数据两次跑出来的榜单会不一样）。
    """
    wanted = set(types) if types else None
    counter: Counter[str] = Counter()
    emote_counter: Counter[str] = Counter()
    emote_meta: dict[str, dict[str, Any]] = {}
    total_texts = 0
    total_words = 0
    skipped = 0

    for record in records:
        if isinstance(record, str):
            event_type, text, extra = "danmaku", record, {}
        else:
            event_type, text, extra = record

        if wanted is not None and event_type not in wanted:
            continue
        total_texts += 1

        if not (text or "").strip():
            skipped += 1
            continue

        result = tokenize(
            text,
            tokenizer=tokenizer,
            stopwords=stopwords,
            min_length=min_length,
            max_length=max_length,
        )
        counter.update(result.words)
        total_words += len(result.words)

        for name in result.emotes:
            emote_counter[name] += 1

        # extra.emots: {"[dog]": {"url": ..., "emoticon_unique": ...}}
        emots = (extra or {}).get("emots")
        if isinstance(emots, dict):
            for key, value in emots.items():
                clean_name = (key or "").strip().strip("[]")
                if not clean_name or not isinstance(value, dict):
                    continue
                emote_meta[clean_name] = {
                    "url": value.get("url"),
                    "emoticon_unique": value.get("emoticon_unique"),
                }
        unique = (extra or {}).get("emoticon_unique")
        if unique and len(result.emotes) == 1:
            emote_meta.setdefault(result.emotes[0], {})["emoticon_unique"] = unique

    if include_emotes:
        # 表情当作词一起参与排名：用带方括号的原形，避免和正文词混淆
        for name, count in emote_counter.items():
            counter[f"[{name}]"] += count

    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ranked = [(word, count) for word, count in ranked if count >= max(min_count, 1)]
    if top > 0:
        ranked = ranked[:top]

    emotes = [
        EmoteStat(
            name=name,
            count=count,
            url=(emote_meta.get(name) or {}).get("url"),
            emoticon_unique=(emote_meta.get(name) or {}).get("emoticon_unique"),
        )
        for name, count in sorted(emote_counter.items(), key=lambda item: (-item[1], item[0]))
    ]

    tokenizer_name = (
        "jieba" if (tokenizer == "jieba" or (tokenizer == "auto" and jieba_available())) else "fallback"
    )
    stopword_count = len(DEFAULT_STOPWORDS if stopwords is None else frozenset(stopwords))

    return WordCloudReport(
        words=ranked,
        emotes=emotes,
        total_texts=total_texts,
        total_words=total_words,
        skipped=skipped,
        tokenizer=tokenizer_name,
        stopwords=stopword_count,
        min_length=min_length,
        max_length=max_length,
        filters=dict(filters or {}),
    )


# --------------------------------------------------------------------------- #
# 输出：文本 / JSON / PNG / HTML
# --------------------------------------------------------------------------- #


def render_text(report: WordCloudReport, *, top: int = 30, width: int = 40) -> str:
    """终端排行榜（纯文本，不依赖任何可选包）。"""
    lines: list[str] = []
    lines.append(f"文本条数    {report.total_texts}（跳过空白 {report.skipped}）")
    lines.append(f"分词        {report.tokenizer}　词数 {report.total_words}　去重 {report.distinct}")
    lines.append(f"参数        停用词 {report.stopwords} 个，长度 {report.min_length}-{report.max_length}")
    if report.filters:
        lines.append(f"筛选        {report.filters}")
    lines.append("")

    if not report.words:
        lines.append("没有统计到任何词（可能数据为空，或长度/停用词把词都过滤掉了）。")
    else:
        peak = max(count for _, count in report.words[:top]) or 1
        lines.append(f"{'排名':<4} {'词':<16} {'次数':>7}  占比（出现该词的弹幕数）")
        for index, (word, count) in enumerate(report.words[:top], 1):
            bar = "█" * max(int(count / peak * width), 1)
            lines.append(f"{index:<4} {word:<16} {count:>7}  {bar}")

    if report.emotes:
        lines.append("")
        lines.append("表情占位符（建议单独看，不要混进词语榜）")
        for item in report.emotes[:top]:
            extra = f"  {item.url}" if item.url else ""
            lines.append(f"  [{item.name}] × {item.count}{extra}")

    return "\n".join(lines)


def render_html(report: WordCloudReport, *, title: str = "弹幕词云", top: int = 80) -> str:
    """生成自包含 HTML 词云。

    这是**不依赖 wordcloud/PIL/字体**的可视化路径：字号按词频映射，
    颜色按排名循环取色，浏览器负责渲染。中文一定不会出现方块。
    """
    words = report.words[:top]
    if not words:
        body = "<p class='empty'>没有统计到任何词。</p>"
    else:
        peak = max(count for _, count in words)
        floor = min(count for _, count in words)
        span = max(peak - floor, 1)
        palette = ["#7dd3fc", "#f472b6", "#fbbf24", "#a78bfa", "#34d399", "#f87171", "#e6e6e6"]
        items = []
        for index, (word, count) in enumerate(words):
            size = 14 + int((count - floor) / span * 42)
            color = palette[index % len(palette)]
            title_attr = f"{word} · {count} 次"
            items.append(
                f'<span style="font-size:{size}px;color:{color}" title="{title_attr}">{_escape(word)}</span>'
            )
        body = "<div class='cloud'>" + " ".join(items) + "</div>"
        if report.emotes:
            body += "<div class='emotes'><h3>表情占位符</h3>" + " ".join(
                f"<span>{_escape('[' + item.name + ']')} × {item.count}</span>" for item in report.emotes[:top]
            ) + "</div>"

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{_escape(title)}</title>
<style>
 body{{background:#14161a;color:#e6e6e6;font-family:system-ui,-apple-system,"Segoe UI",
      "Microsoft YaHei",sans-serif;margin:0;padding:28px}}
 h1{{font-size:20px;margin:0 0 6px}}
 .meta{{color:#8b93a1;font-size:13px;margin-bottom:20px}}
 .cloud{{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 18px;line-height:1.5}}
 .emotes{{margin-top:26px;border-top:1px solid #2c313a;padding-top:14px;color:#8b93a1}}
 .emotes h3{{font-size:15px;color:#e6e6e6}}
 .emotes span{{margin-right:14px;font-size:13px}}
 .empty{{color:#8b93a1}}
</style></head><body>
<h1>{_escape(title)}</h1>
<div class="meta">文本 {report.total_texts} 条 · 词数 {report.total_words} · 去重 {report.distinct} ·
 分词 {report.tokenizer} · 停用词 {report.stopwords} 个{_filters_text(report)}</div>
{body}
</body></html>
"""


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _filters_text(report: WordCloudReport) -> str:
    if not report.filters:
        return ""
    return " · " + ", ".join(f"{key}={value}" for key, value in report.filters.items() if value is not None)


#: 常见中日韩字体文件名（按优先级）。Windows/macOS/Linux 常见发行版都覆盖到。
CJK_FONT_CANDIDATES: tuple[str, ...] = (
    "msyh.ttc", "msyh.ttf", "msyhl.ttc", "simhei.ttf", "simsun.ttc", "Deng.ttf",
    "NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf", "NotoSansSC-Regular.otf",
    "SourceHanSansSC-Regular.otf", "SourceHanSansCN-Regular.otf",
    "PingFang.ttc", "Hiragino Sans GB.ttc", "STHeiti Medium.ttc",
    "wqy-zenhei.ttc", "wqy-microhei.ttc", "DroidSansFallbackFull.ttf",
)


def find_cjk_font() -> Path | None:
    """找一个能显示中文的字体文件。

    `wordcloud` 必须显式给字体路径，否则中文会渲染成方块（实测默认字体没有 CJK）。
    """
    import platform

    roots: list[Path] = []
    if os.name == "nt":
        roots += [
            Path("C:/Windows/Fonts"),
            Path(os.environ.get("LOCALAPPDATA") or ".") / "Microsoft/Windows/Fonts",
        ]
    elif platform.system() == "Darwin":
        roots += [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
    else:
        roots += [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
            Path.home() / ".local/share/fonts",
        ]

    lowered = {name.lower(): name for name in CJK_FONT_CANDIDATES}
    for root in roots:
        if not root.is_dir():
            continue
        for name in CJK_FONT_CANDIDATES:
            candidate = root / name
            if candidate.is_file():
                return candidate
        # 目录里可能放在子目录（如 /usr/share/fonts/truetype/...）
        for found in sorted(root.rglob("*")):
            if found.is_file() and found.name.lower() in lowered:
                return found
    return None


class MissingDependency(RuntimeError):
    """渲染 PNG 需要可选依赖（wordcloud / 中文字体）。"""


def render_png(
    report: WordCloudReport,
    out_path: str | Path,
    *,
    width: int = 1280,
    height: int = 720,
    background: str = "#14161a",
    font_path: str | Path | None = None,
    top: int = 200,
) -> Path:
    """渲染 PNG 词云（需要 `wordcloud` + 一个中文字体）。

    `include_emotes` 已开启时，表情形如 `[dog]`，会作为词一起渲染。
    """
    # wordcloud 会 import matplotlib，后者默认往用户目录写字体缓存；
    # 受限沙箱下会报 `Permission denied` 噪声。指到工作区内即可（MPLCONFIGDIR）。
    os.environ.setdefault("MPLCONFIGDIR", str(Path(".matplotlib-cache").resolve()))
    try:
        from wordcloud import WordCloud  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise MissingDependency(
            "渲染 PNG 需要 wordcloud：pip install 'bili-danmaku[wordcloud]'"
            "（本机可先 python tools/wheel_fetch.py --packages wordcloud）"
        ) from exc

    if not report.words:
        raise ValueError("没有可渲染的词，先检查数据范围与过滤参数")

    font = Path(font_path) if font_path else find_cjk_font()
    if font is None:
        raise MissingDependency(
            "找不到中文字体，PNG 里中文会变成方块。用 --font 指定一个 .ttf/.ttc/.otf，"
            "或改用 --format html（HTML 用浏览器字体，不需要字体文件）"
        )

    frequencies = dict(report.words[:top])
    cloud = WordCloud(
        font_path=str(font),
        width=width,
        height=height,
        background_color=background,
        colormap="cool",
        prefer_horizontal=0.9,
        max_words=top,
        relative_scaling=0.4,
        collocations=False,  # 二元组已经是我们自己切的了，不要再合并
    ).generate_from_frequencies(frequencies)

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cloud.to_file(str(target))
    return target
