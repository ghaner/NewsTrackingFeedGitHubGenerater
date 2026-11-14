# -*- coding: utf-8 -*-
"""
merge.py — 冲突合并与时间轴组装
================================
输入：已去重、已时间戳对齐的文章列表（每项含 event_label / facts / parsed_time / credibility）。
输出：按时间排序的可迭代时间轴节点列表。

核心逻辑：
  - 以 event_label 为锚，将报道归入同一“事件节点”。
  - 节点时间 = 该组最早的可信发稿时间（sort_key 最小者）。
  - 节点摘要 = 该组置信度最高的一篇的正文/摘要。
  - 事实合并：聚合每篇提供的 facts（key→value）。同一 key 出现多个不同 value
    → 判定为“冲突”，保留全部版本（各自带置信度与来源），节点标记 conflict=True。
  - 置信度：文章级 = 信源可信度分值；节点级 = 组内最高文章置信度（取最早且最可信）。
  - 时间轴可迭代：每次 update 重新跑本模块，旧节点保留、新节点并入，天然支持增量。
"""

import hashlib
from schema import CREDIBILITY_SCORE, norm_text


def _conf(a):
    return CREDIBILITY_SCORE.get((a.get("credibility") or "unknown").lower(), 0.5)


def _slug(s):
    s = norm_text(s)
    return "n_" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def build_timeline(articles):
    """articles: list[dict]，需含 event_label、facts(可选)、parsed_time、credibility、title、url。
    返回 (nodes, stats)。nodes 按时间升序。"""
    groups = {}
    for a in articles:
        label = (a.get("event_label") or a.get("title") or "未命名事件").strip()
        groups.setdefault(label, []).append(a)

    nodes = []
    for label, group in groups.items():
        # 时间：取最早（sort_key 最小）且解析成功的
        timed = [g for g in group if g.get("parsed_time", {}).get("ok")]
        if timed:
            earliest = min(timed, key=lambda x: x["parsed_time"]["sort_key"])
            ts_local = earliest["parsed_time"]["iso_local"]
            ts_utc = earliest["parsed_time"]["iso_utc"]
            ts_sort = earliest["parsed_time"]["sort_key"]
        else:
            # 解析失败：放到最末，时间置 None
            ts_local = ts_utc = None
            ts_sort = float("inf")

        # 摘要：置信度最高的那篇
        best = max(group, key=_conf)
        summary = (best.get("summary") or best.get("content") or best.get("title") or "")[:500]

        # 事实聚合 + 冲突检测
        fact_versions = {}  # key -> list of {value, confidence, source}
        for a in group:
            facts = a.get("facts") or {}
            src = a.get("url") or a.get("title") or a.get("id")
            conf = _conf(a)
            for k, v in facts.items():
                fact_versions.setdefault(k, []).append({
                    "value": str(v),
                    "confidence": round(conf, 2),
                    "source": src,
                })
        conflict = False
        for k, vers in fact_versions.items():
            distinct = {v["value"] for v in vers}
            if len(distinct) > 1:
                conflict = True
            # 同值多条合并去重（保留置信度最高）
            seen = {}
            for v in vers:
                if v["value"] not in seen or v["confidence"] > seen[v["value"]]["confidence"]:
                    seen[v["value"]] = v
            fact_versions[k] = sorted(seen.values(), key=lambda x: -x["confidence"])

        node_conf = round(max(_conf(a) for a in group), 2)
        sources = []
        for a in group:
            s = a.get("url") or a.get("title")
            if s and s not in sources:
                sources.append(s)

        nodes.append({
            "node_id": _slug(label),
            "event_label": label,
            "timestamp_local": ts_local,
            "timestamp_utc": ts_utc,
            "timestamp_sort": ts_sort,
            "summary": summary,
            "facts": fact_versions,
            "conflict": conflict,
            "confidence": node_conf,
            "sources": sources,
            "article_count": len(group),
        })

    nodes.sort(key=lambda n: (n["timestamp_sort"] is None, n["timestamp_sort"] if n["timestamp_sort"] is not None else 0))
    stats = {
        "node_count": len(nodes),
        "conflict_nodes": sum(1 for n in nodes if n["conflict"]),
        "article_count": len(articles),
    }
    return nodes, stats


if __name__ == "__main__":
    demo = [
        {"event_label": "地震发生", "title": "地震", "content": "震情", "url": "u1",
         "credibility": "high", "facts": {"震级": "5.0", "地点": "甲县"},
         "parsed_time": {"ok": True, "iso_local": "2026-11-11T14:30:00+08:00",
                         "iso_utc": "2026-11-11T06:30:00+00:00", "sort_key": 100.0}},
        {"event_label": "地震发生", "title": "地震2", "content": "震情2", "url": "u2",
         "credibility": "medium", "facts": {"震级": "5.2", "地点": "甲县"},
         "parsed_time": {"ok": True, "iso_local": "2026-11-11T15:00:00+08:00",
                         "iso_utc": "2026-11-11T07:00:00+00:00", "sort_key": 200.0}},
        {"event_label": "救援启动", "title": "救援", "content": "救援", "url": "u3",
         "credibility": "high", "facts": {"出动": "300人"},
         "parsed_time": {"ok": True, "iso_local": "2026-11-11T16:00:00+08:00",
                         "iso_utc": "2026-11-11T08:00:00+00:00", "sort_key": 300.0}},
    ]
    nodes, stats = build_timeline(demo)
    import json
    print(json.dumps({"stats": stats, "nodes": nodes}, ensure_ascii=False, indent=2))
