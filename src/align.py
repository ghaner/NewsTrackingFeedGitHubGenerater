# -*- coding: utf-8 -*-
"""
align.py — 时间戳对齐
====================
把各种形态的发稿时间统一解析为带时区的 datetime，并输出：
  - iso_local：带时区（缺省按北京时间 +08:00）的 ISO 字符串
  - iso_utc  ：UTC ISO 字符串（用于跨源严格排序）
  - sort_key ：单调递增的浮点（UTC epoch 秒），供时间轴排序

支持形态：
  - ISO 8601：2026-11-11T14:30:00+08:00 / 2026-11-11 14:30:00
  - Unix 时间戳：1731306600 或 "1731306600"
  - 中文：2026年11月11日 14:30 / 2026年11月11日 / 2026/11/11 14:30
  - 相对：刚刚 / 3分钟前 / 2小时前 / 5天前 / 昨天14:30 / 前天09:00 / 今天10:00
全部为纯函数，不调用模型。
"""

import re
from datetime import datetime, timezone, timedelta

from schema import DEFAULT_TZ, now_iso

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s):
    """把中文数字（十/二十三等）转 int，失败返回 None。"""
    if s is None:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    # 处理 十一~十九、二十~九十九
    m = re.match(r"^([二三四五六七八九])?十([一二三四五六七八九])?$", s)
    if m:
        tens = _CN_NUM.get(m.group(1), 1)
        ones = _CN_NUM.get(m.group(2), 0)
        return tens * 10 + ones
    try:
        return int(s)
    except ValueError:
        return None


def parse_timestamp(raw, ref=None):
    """解析单条时间字符串。

    raw : 原始时间文本（可能来自文章 published_at 字段）
    ref : 参考“现在”（datetime, 带时区），默认当前北京时间
    返回: {"raw","iso_local","iso_utc","sort_key","ok"}；解析失败 ok=False。
    """
    result = {"raw": raw, "iso_local": None, "iso_utc": None, "sort_key": None, "ok": False}
    if raw is None:
        return result
    s = str(raw).strip()
    if not s:
        return result
    if ref is None:
        ref = datetime.now(DEFAULT_TZ)

    dt = None

    # 1) Unix 时间戳
    if re.fullmatch(r"\d{9,13}", s):
        ts = int(s)
        if len(s) >= 13:  # 毫秒
            ts = ts / 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return _pack(dt)

    # 2) 相对时间
    rel = _parse_relative(s, ref)
    if rel is not None:
        return _pack(rel)

    # 3) ISO 8601（含空格分隔）
    iso_candidate = s.replace("Z", "+00:00").replace("/", "-")
    # 尝试常见 ISO
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(iso_candidate if "T" not in fmt else s.replace(" ", "T"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=DEFAULT_TZ)
            return _pack(dt)
        except ValueError:
            continue

    # 4) 中文：2026年11月11日 14:30
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日[ \t]*(\d{1,2})?:?(\d{1,2})?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mm = int(m.group(5)) if m.group(5) else 0
        try:
            dt = datetime(y, mo, d, hh, mm, tzinfo=DEFAULT_TZ)
            return _pack(dt)
        except ValueError:
            pass

    # 5) 中文无年份：11月11日 14:30
    m = re.search(r"(\d{1,2})月(\d{1,2})日[ \t]*(\d{1,2})?:?(\d{1,2})?", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        hh = int(m.group(3)) if m.group(3) else 0
        mm = int(m.group(4)) if m.group(4) else 0
        try:
            dt = datetime(ref.year, mo, d, hh, mm, tzinfo=DEFAULT_TZ)
            return _pack(dt)
        except ValueError:
            pass

    # 6) 兜底：Python 3.11+ fromisoformat 可解析带小数秒的 ISO 8601（如 2026-11-11T15:18:00.123456+08:00）
    #    仅在前述格式全部失败时尝试，不改变既有已成功解析的结果。
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=DEFAULT_TZ)
        return _pack(dt)
    except ValueError:
        pass

    return result


def _parse_relative(s, ref):
    if s in ("刚刚", "刚才", "现在"):
        return ref
    m = re.match(r"^(\d+)\s*分钟前$", s)
    if m:
        return ref - timedelta(minutes=int(m.group(1)))
    m = re.match(r"^(\d+)\s*小时前$", s)
    if m:
        return ref - timedelta(hours=int(m.group(1)))
    m = re.match(r"^(\d+)\s*天前$", s)
    if m:
        return ref - timedelta(days=int(m.group(1)))
    if s.startswith("昨天"):
        base = (ref - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        hh, mm = _extract_hm(s)
        return base.replace(hour=hh, minute=mm)
    if s.startswith("前天"):
        base = (ref - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        hh, mm = _extract_hm(s)
        return base.replace(hour=hh, minute=mm)
    if s.startswith("今天"):
        base = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        hh, mm = _extract_hm(s)
        return base.replace(hour=hh, minute=mm)
    return None


def _extract_hm(s):
    m = re.search(r"(\d{1,2})[:：](\d{1,2})", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{1,2})点(\d{1,2})?", s)
    if m:
        return int(m.group(1)), int(m.group(2) or 0)
    return 0, 0


def _pack(dt):
    """把 datetime 封装成返回结构。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DEFAULT_TZ)
    utc = dt.astimezone(timezone.utc)
    return {
        "raw": None,  # 调用方回填
        "iso_local": dt.isoformat(),
        "iso_utc": utc.isoformat(),
        "sort_key": utc.timestamp(),
        "ok": True,
    }


def align_articles(articles, ref=None):
    """就地给 article 列表补充 parsed_time 字段；解析失败标记 ok=False。"""
    if ref is None:
        ref = datetime.now(DEFAULT_TZ)
    for a in articles:
        raw = a.get("published_at") or a.get("time") or a.get("date")
        p = parse_timestamp(raw, ref)
        p["raw"] = raw
        a["parsed_time"] = p
    return articles


if __name__ == "__main__":
    samples = ["2026-11-11T14:30:00+08:00", "2026年11月11日 14:30", "3小时前",
               "昨天 09:15", "1731306600", "今天 8点", "2026/11/11 14:30"]
    for s in samples:
        print(s, "->", parse_timestamp(s))
