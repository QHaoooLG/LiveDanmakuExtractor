"""为「实时词频统计策略」的讨论收集事实。

三件事：
  1. 当前分词器在**真实弹幕**上产出什么词（哪些像"不符合日常表达"）；
  2. 这些词里有多少是"人名的碎片 / 平台专名 / 切错的词"；
  3. 本机有没有可用的**本地小模型**运行环境（Ollama / llama.cpp / transformers）。
"""

from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bili_danmaku import text as text_module  # noqa: E402
from bili_danmaku.analysis import word_frequency  # noqa: E402
from bili_danmaku.store import DanmakuStore  # noqa: E402

DB = ROOT / "data" / "danmaku.db"

print("=" * 86)
print("1. 分词器与停用词现状")
print("=" * 86)
print(f"  jieba 可用        = {text_module.jieba_available()}")
print(f"  默认停用词        = {len(text_module.DEFAULT_STOPWORDS)} 个")
print(f"  默认最小词长      = {text_module.DEFAULT_MIN_LENGTH}")
print(f"  默认最大词长      = {text_module.DEFAULT_MAX_LENGTH}")
print(f"  停用词含功能词    = {sorted(list(text_module.DEFAULT_STOPWORDS))[:12]} …")

print()
print("=" * 86)
print("2. 真实弹幕上的词频 TOP 40")
print("=" * 86)
store = DanmakuStore(DB)
try:
    rows = list(
        store.iter_events(types=["danmaku"], limit=4000)
    )
finally:
    store.close()

messages = [{"type": "danmaku", "text": r.text, "uid": r.uid, "recv_ts": r.recv_ts} for r in rows]
print(f"  取样弹幕          = {len(messages)} 条")

payload = word_frequency(messages, top=40)
items = payload.get("items") or []
print(f"  去重词数          = {payload.get('unique_words')}")
print(f"  总词数            = {payload.get('total_words')}")
print()
print(f"  {'词':<14}{'次数':>6}{'发言人数':>9}")
for item in items:
    print(f"  {item['word']:<14}{item['count']:>6}{item.get('users', 0):>9}")

print()
print("=" * 86)
print("3. 人工判断：哪些疑似「不像日常表达」")
print("=" * 86)
# 单字、数字、拉丁字母、以及很短的"疑似切错"词，是最容易被用户觉得突兀的
suspicious: list[tuple[str, int, str]] = []
for item in items:
    word = item["word"]
    reason = ""
    if len(word) == 1:
        reason = "单字（歧义大）"
    elif word.isdigit():
        reason = "纯数字"
    elif all(ord(ch) < 128 for ch in word):
        reason = "纯 ASCII（英文/缩写）"
    elif len(word) == 2 and (word[0] == word[1]):
        reason = "叠字（多为语气）"
    if reason:
        suspicious.append((word, item["count"], reason))

if suspicious:
    for word, count, reason in suspicious:
        print(f"  ⚠️ {word:<12} x{count:<5} {reason}")
else:
    print("  （TOP40 里没有明显可疑项）")

print()
print("=" * 86)
print("4. 本地小模型运行环境是否具备")
print("=" * 86)
for name in ("ollama", "llama-cli", "llama-server", "llamafile", "LM Studio"):
    found = shutil.which(name)
    print(f"  {name:<14} {'✅ ' + found if found else '❌ 未找到'}")
print()
try:
    import importlib.util

    for mod in ("llama_cpp", "transformers", "onnxruntime", "ctransformers", "torch"):
        spec = importlib.util.find_spec(mod)
        print(f"  python:{mod:<16} {'✅ 已装' if spec else '❌ 未装'}")
except Exception as exc:  # pragma: no cover
    print(f"  检查失败: {exc}")
print()
print("  （Ollama 常见的本地模型：qwen2.5:0.5b / 1.5b、gemma2:2b —— 都是 0.4~1.6 GB）")
