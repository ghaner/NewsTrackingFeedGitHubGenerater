# -*- coding: utf-8 -*-
"""
store.py — 事件持久化层
======================
每个被追踪的事件对应一个目录：<workdir>/<event_slug>/
  - articles.jsonl : 追加式原始报道（去重后写入，保留来源/版本回溯）
  - event.json     : 事件元信息 + 六维分析 + 当前时间轴节点 + 更新历史

设计要点：
  - 文章用 JSONL 追加，便于持续 ingest 新报道且可重放。
  - event.json 记录 versions 列表，每次重新组装时间轴都追加一条版本快照，
    满足“可迭代更新、版本可追溯”的诉求。
  - 来源链接、置信度随节点一起落盘，渲染与审计直接读取。
"""

import os
import json
from datetime import datetime, timezone, timedelta

from schema import read_jsonl, dump_jsonl, save_json, load_json, now_iso, DEFAULT_TZ


def event_dir(workdir, slug):
    d = os.path.join(workdir, slug)
    os.makedirs(d, exist_ok=True)
    return d


def add_articles(workdir, slug, articles, articles_path=None):
    """把（已去重的）文章追加写入 articles.jsonl，返回写入条数。
    articles_path 可显式指定文章文件位置（适配非标准目录布局）；缺省为 <workdir>/<slug>/articles.jsonl。"""
    path = articles_path or os.path.join(event_dir(workdir, slug), "articles.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    existing = read_jsonl(path)
    existing_ids = {a.get("id") for a in existing}
    n = 0
    for a in articles:
        if a.get("id") in existing_ids:
            continue
        existing.append(a)
        existing_ids.add(a.get("id"))
        n += 1
    dump_jsonl(path, existing)
    return n


def load_articles(workdir, slug):
    d = event_dir(workdir, slug)
    return read_jsonl(os.path.join(d, "articles.jsonl"))


def save_event(workdir, slug, meta, analysis, timeline, stats=None):
    """写入/更新 event.json，并追加一条版本快照。"""
    d = event_dir(workdir, slug)
    path = os.path.join(d, "event.json")
    if os.path.exists(path):
        ev = load_json(path)
    else:
        ev = {"slug": slug, "created_at": now_iso(), "versions": []}

    ev["meta"] = meta
    ev["analysis"] = analysis
    ev["timeline"] = timeline
    ev["updated_at"] = now_iso()
    if stats:
        ev["last_stats"] = stats

    # 版本快照：记录本次时间轴节点数与时间戳，便于回溯
    ev.setdefault("versions", []).append({
        "updated_at": ev["updated_at"],
        "node_count": len(timeline),
        "conflict_nodes": sum(1 for n in timeline if n.get("conflict")),
        "article_count": sum(n.get("article_count", 0) for n in timeline),
    })

    save_json(path, ev)
    return ev


def load_event(workdir, slug):
    d = event_dir(workdir, slug)
    path = os.path.join(d, "event.json")
    if os.path.exists(path):
        return load_json(path)
    return None


if __name__ == "__main__":
    print("store module loaded")
