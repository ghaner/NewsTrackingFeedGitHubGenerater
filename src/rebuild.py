# -*- coding: utf-8 -*-
"""
rebuild.py — 事件数据增量合并与时间轴重算（GitHub Actions 无人值守用）
=====================================================================
职责单一：把 data/incoming.jsonl（Actions 抓来的原始报道）合并进 data/articles.jsonl，
然后重算 data/event.json 的时间轴并追加一条版本快照。

不做的事：不联网（抓取由 fetch_articles.py 负责）、不渲染 HTML、不调用大模型。
确定性规则全部复用 news-timeline-builder 的 dedupe / align / merge。

无新增时不改动 event.json（不追加空快照），并打印 NO_NEW 供工作流判断。
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

# ===================== 参数配置区 =====================
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SRC_DIR)
DATA_DIR = os.path.join(REPO_ROOT, "data")

SLUG = "duanju_overseas_top9"
ARTICLES_PATH = os.path.join(DATA_DIR, "articles.jsonl")
INCOMING_PATH = os.path.join(DATA_DIR, "incoming.jsonl")
ANALYSIS_PATH = os.path.join(DATA_DIR, "analysis.json")
META_PATH = os.path.join(DATA_DIR, "meta.json")
EVENT_PATH = os.path.join(DATA_DIR, "event.json")

TZ = timezone(timedelta(hours=8))

sys.path.insert(0, SRC_DIR)
import dedupe
import align
import merge
from schema import read_jsonl, dump_jsonl, load_json, save_json, now_iso


# ===================== 入口 =====================
def main():
    arts = read_jsonl(ARTICLES_PATH)
    inc = read_jsonl(INCOMING_PATH)
    print("[1/4] 已有报道 %d 条，新抓取 %d 条" % (len(arts), len(inc)))

    if not inc:
        print("NO_NEW 本轮无新增报道，不重算时间轴（feed 保持上一版）。")
        return 0

    # 合并 + 三重去重（URL／标题／正文相似度），新条目排在前，旧条目为权威历史
    kept, dropped = dedupe.dedupe(inc + arts)
    print("[2/4] 去重：保留 %d 条，剔除重复 %d 条" % (len(kept), len(dropped)))
    for d in dropped:
        print("      - 重复(%s): %s" % (d["reason"], d.get("title")))

    align.align_articles(kept)
    ok = sum(1 for a in kept if a.get("parsed_time", {}).get("ok"))
    print("[3/4] 时间戳对齐：成功 %d/%d 条" % (ok, len(kept)))

    nodes, stats = merge.build_timeline(kept)
    print("[4/4] 重算时间轴：节点 %d 个，冲突节点 %d 个"
          % (stats["node_count"], stats["conflict_nodes"]))

    # 持久化：articles.jsonl 覆盖为去重后的全集；event.json 追加版本快照
    dump_jsonl(ARTICLES_PATH, kept)
    open(INCOMING_PATH, "w", encoding="utf-8").close()
    save_event(load_json(META_PATH), load_json(ANALYSIS_PATH), nodes, stats)
    print("已更新 %s" % EVENT_PATH)
    return 0


# ===================== 代码 =====================
def save_event(meta, analysis, timeline, stats):
    """写 event.json 并追加版本快照（字段与 news-timeline-builder 的 store.save_event 一致）。"""
    if os.path.exists(EVENT_PATH):
        ev = load_json(EVENT_PATH)
    else:
        ev = {"slug": SLUG, "created_at": now_iso(), "versions": []}
    ev.setdefault("slug", SLUG)
    ev["meta"] = meta
    ev["analysis"] = analysis
    ev["timeline"] = timeline
    ev["updated_at"] = now_iso()
    ev["last_stats"] = stats
    ev.setdefault("versions", []).append({
        "updated_at": ev["updated_at"],
        "node_count": len(timeline),
        "conflict_nodes": sum(1 for n in timeline if n.get("conflict")),
        "article_count": sum(n.get("article_count", 0) for n in timeline),
        "source": "github-actions",
    })
    save_json(EVENT_PATH, ev)
    return ev


# ===================== 出口 =====================
if __name__ == "__main__":
    sys.exit(main())
