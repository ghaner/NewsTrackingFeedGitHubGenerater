#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resolve_urls.py —— 把既有报道库里的 Google News 跳转链接批量还原为原文地址

功能：CI 抓取前的历史数据清洗。抓取器升级前入库的条目录的是
news.google.com 跳转页（不含正文、订阅者点开多一跳），而新抓的同题报道
会与这些旧条目标题重复、在去重时被丢弃，导致旧链接永远留在库里。
本脚本逐条还原并回写 articles.jsonl；已是原文的条目零请求跳过，
因此可以每轮都跑，无遗留时等于空转（0 请求 0 改动）。
"""

# ===== 参数配置区（需要时改这里）=====
DEFAULT_LIMIT = 60      # 单轮最多还原多少条（控制时长）
SKIP_MARKS = ("news.google.com",)
# ===== 配置区结束 =====

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_articles as fa


# --- 入口 ---
def main():
    ap = argparse.ArgumentParser(description="还原报道库中的 Google News 跳转链接")
    ap.add_argument("--data-dir", default="data", help="事件数据根目录（data/<slug>/articles.jsonl）")
    ap.add_argument("--slug", help="只处理某个事件")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="单轮最多还原条数")
    args = ap.parse_args()

    slugs = [args.slug] if args.slug else list_events(args.data_dir)
    if not slugs:
        print("[resolve] 未找到任何事件目录，跳过")
        return 0

    budget = args.limit
    total_fixed = total_left = 0
    for slug in slugs:
        path = os.path.join(args.data_dir, slug, "articles.jsonl")
        rows = load_articles(path)
        if not rows:
            continue
        fixed = left = 0
        for r in rows:
            u = (r.get("url") or "").strip()
            if not any(m in u for m in SKIP_MARKS):
                continue
            if budget <= 0:
                left += 1
                continue
            budget -= 1
            real = fa.resolve_google_link(u)
            if real and real != u:
                r["url"] = real
                r.pop("link_kind", None)
                fixed += 1
            else:
                left += 1
        if fixed:
            save_articles(path, rows)
        print("[resolve] %s：还原 %d 条，未还原 %d 条%s" % (slug, fixed, left, "" if fixed else "（未改动）"))
        total_fixed += fixed
        total_left += left

    print("[resolve] 合计：还原 %d 条，未还原 %d 条" % (total_fixed, total_left))
    return 0


# --- 代码区 ---
def list_events(data_dir):
    if not os.path.isdir(data_dir):
        return []
    return sorted(
        d for d in os.listdir(data_dir)
        if os.path.isfile(os.path.join(data_dir, d, "articles.jsonl"))
    )


def load_articles(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def save_articles(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# --- 出口 ---
if __name__ == "__main__":
    sys.exit(main())
