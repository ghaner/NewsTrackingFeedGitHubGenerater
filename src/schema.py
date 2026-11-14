# -*- coding: utf-8 -*-
"""
schema.py — 新闻事件时间线组装 · 共享数据模型与工具
=================================================
本文件定义流水线各环节共用的数据结构与纯函数工具，不依赖任何外部大模型。
所有模块（align / dedupe / merge / store / render / run_pipeline）均从这里导入。

设计原则：
- 文章(article)、分析(analysis)、时间轴节点(timeline_node)三类结构用普通 dict 表示，
  便于 JSON 序列化与在 Skill 内由 Agent（会话内 LLM）产出后直接喂入。
- 确定性逻辑（去重/对齐/合并/组装）全部在此及相邻模块，不调用任何模型。
"""

import hashlib
import json
import re
from datetime import datetime, timezone, timedelta

# 信源可信度 → 置信度分值（0~1）。缺省按 medium 处理。
CREDIBILITY_SCORE = {
    "high": 0.90,
    "medium": 0.60,
    "low": 0.30,
    "unknown": 0.50,
}

# 默认时区：中文新闻若无时区标注，按北京时间（+08:00）解释。
DEFAULT_TZ = timezone(timedelta(hours=8))


def article_id(url=None, title=None, content=None):
    """为一篇文章生成稳定 id：优先用 url，其次标题，再次正文前段。
    用于去重主键与持久化去重。"""
    seed = (url or "").strip() or (title or "").strip() or (content or "")[:200]
    if not seed:
        seed = str(datetime.now().isoformat())
    return "a_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def norm_text(s):
    """规范化文本：去空白、去中文/英文标点，便于标题/正文相似度比较。"""
    if not s:
        return ""
    s = s.lower()
    # 去除常见标点与空白
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[，。、；：？！“”‘’（）()\[\]【】{}<>《》.,;:?!\'\"\-_/\\|@#$%^&*+=~`]", "", s)
    return s


def char_bigrams(s):
    """中文按字二元组切分（无空格语言的分词近似）。"""
    s = norm_text(s)
    if len(s) <= 1:
        return set(s)
    return set(s[i:i + 2] for i in range(len(s) - 1))


def jaccard(a, b):
    """两篇文章的二元组 Jaccard 相似度。"""
    if not a and not b:
        return 0.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def content_similarity(a, b):
    """正文相似度：二元组 Jaccard，作为去重判定的补充信号。"""
    return jaccard(char_bigrams(a), char_bigrams(b))


def now_iso(tz=DEFAULT_TZ):
    """当前时间 ISO 字符串（带时区）。"""
    return datetime.now(tz).isoformat()


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def dump_jsonl(path, rows):
    """覆盖写入 JSONL。"""
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_jsonl(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


import os  # 放末尾避免循环导入问题（本文件无循环）
