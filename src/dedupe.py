# -*- coding: utf-8 -*-
"""
dedupe.py — 报道去重
===================
对一批文章做确定性去重，输出保留集与被合并的重复项（含原因）。

三重判定（任一命中即判为重复）：
  1) URL 归一后完全一致（去 scheme/www、去尾部斜杠、去常见追踪参数）
  2) 标题归一（去标点空白）后完全一致
  3) 正文二元组 Jaccard 相似度 >= 阈值（默认 0.85）

策略：保留“先到”的一篇（按 ingested 顺序 / fetched_at 早的优先），
重复项记录其被合并到的目标 id 与原因，供审计与回溯。
"""

import re
from schema import norm_text, content_similarity, article_id

# 正文相似度去重阈值
SIM_THRESHOLD = 0.85
# 去重时忽略的 URL 追踪参数
_TRACK_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "spm", "from", "timestamp", "t"}


def norm_url(u):
    if not u:
        return ""
    u = u.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"[?#].*$", "", u)  # 去 query/fragment
    u = u.rstrip("/")
    return u


def norm_title(t):
    return norm_text(t)


def dedupe(articles, sim_threshold=SIM_THRESHOLD):
    """articles: list[dict]，每项应有 title/content/url(可选)/id(可选)。
    返回 (kept, dropped)，kept 为去重后列表，dropped 为重复项说明列表。
    """
    kept = []
    dropped = []
    # 已保留项的索引，便于命中判定
    by_url = {}
    by_title = {}
    by_id = {}

    for a in articles:
        aid = a.get("id") or article_id(a.get("url"), a.get("title"), a.get("content"))
        a["id"] = aid
        url = norm_url(a.get("url"))
        title = norm_title(a.get("title"))

        reason = None
        target = None

        if aid in by_id:
            reason, target = "same_id", by_id[aid]
        elif url and url in by_url:
            reason, target = "same_url", by_url[url]
        elif title and title in by_title:
            reason, target = "same_title", by_title[title]
        else:
            # 正文相似度比对（仅对 dedup 后的 kept 做，集合不大时足够）
            for k in kept:
                if content_similarity(a.get("content", ""), k.get("content", "")) >= sim_threshold:
                    reason, target = "similar_content", k["id"]
                    break

        if reason:
            dropped.append({
                "id": aid,
                "title": a.get("title"),
                "url": a.get("url"),
                "dup_of": target,
                "reason": reason,
            })
        else:
            kept.append(a)
            by_id[aid] = aid
            if url:
                by_url[url] = aid
            if title:
                by_title[title] = aid

    return kept, dropped


if __name__ == "__main__":
    demo = [
        {"title": "某地发生地震", "content": "据测定，某地发生5.0级地震。", "url": "https://news.com/a?utm_source=x"},
        {"title": "某地发生地震", "content": "据测定，某地发生5.0级地震。", "url": "https://news.com/a"},
        {"title": "地震最新通报", "content": "应急部门发布最新震情通报。", "url": "https://news.com/b"},
    ]
    k, d = dedupe(demo)
    print("kept:", len(k), "dropped:", len(d))
    for x in d:
        print("  dup:", x["id"], "of", x["dup_of"], x["reason"])
