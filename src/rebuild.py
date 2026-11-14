# -*- coding: utf-8 -*-
"""
rebuild.py — 事件数据增量合并与时间轴重算（GitHub Actions 无人值守用）
=====================================================================
职责单一：把 data/<slug>/incoming.jsonl（Actions 抓来的原始报道）合并进
data/<slug>/articles.jsonl，然后重算 data/<slug>/event.json 的时间轴并追加一条版本快照。

多事件支持：data/ 下一个事件一个目录（data/duanju/、data/<下一个 slug>/ …）。
  - 不带参数：遍历 data/ 下所有含 articles.jsonl 的子目录，逐个重算；
  - --slug duanju：只重算指定事件（可重复传入多个）。
事件清单的权威来源是 src/events.json（slug / keywords / feed 文件名）。

不做的事：不联网（抓取由 fetch_articles.py 负责）、不渲染 HTML、不调用大模型。
确定性规则全部复用 news-timeline-builder 的 dedupe / align / merge。

无新增时不改动 event.json（不追加空快照），并打印 NO_NEW 供工作流判断。
"""

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone, timedelta

# ===================== 参数配置区 =====================
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SRC_DIR)
DATA_ROOT = os.path.join(REPO_ROOT, "data")

TZ = timezone(timedelta(hours=8))

sys.path.insert(0, SRC_DIR)
import dedupe
import align
import merge
from schema import read_jsonl, dump_jsonl, load_json, save_json, now_iso


# ===================== 入口 =====================
def main():
    ap = argparse.ArgumentParser(description="合并增量报道并重算事件时间轴")
    ap.add_argument("--slug", action="append", help="只处理指定事件（可重复）；缺省遍历 data/ 下全部事件")
    args = ap.parse_args()

    slugs = args.slug or discover_slugs()
    if not slugs:
        print("SKIP data/ 下没有可处理的事件目录（需含 articles.jsonl）。")
        return 0

    failed = 0
    for slug in slugs:
        try:
            rebuild_one(slug)
        except Exception as e:
            failed += 1
            print("FAIL [%s] %s: %s" % (slug, type(e).__name__, e))
    return 1 if failed else 0


# ===================== 代码 =====================
def articles_fingerprint(rows):
    """报道库指纹：对 url 与 event_label 取摘要，用来识别"地址被清洗"这类静默变化。"""
    h = hashlib.sha1()
    for r in sorted(rows, key=lambda x: (x.get("url") or "")):
        h.update(((r.get("url") or "") + "|" + (r.get("event_label") or "")).encode("utf-8"))
    return h.hexdigest()[:16]


def discover_slugs():
    """data/ 下含 articles.jsonl 的子目录名，按字典序返回。"""
    if not os.path.isdir(DATA_ROOT):
        return []
    return sorted(
        d for d in os.listdir(DATA_ROOT)
        if os.path.exists(os.path.join(DATA_ROOT, d, "articles.jsonl"))
    )


def event_paths(slug):
    """一个事件的四类数据文件路径。"""
    d = os.path.join(DATA_ROOT, slug)
    return {
        "dir": d,
        "articles": os.path.join(d, "articles.jsonl"),
        "incoming": os.path.join(d, "incoming.jsonl"),
        "analysis": os.path.join(d, "analysis.json"),
        "meta": os.path.join(d, "meta.json"),
        "event": os.path.join(d, "event.json"),
    }


def rebuild_one(slug):
    p = event_paths(slug)
    if not os.path.exists(p["articles"]):
        print("SKIP [%s] 缺少 articles.jsonl" % slug)
        return

    arts = read_jsonl(p["articles"])
    inc = read_jsonl(p["incoming"])
    print("[%s 1/4] 已有报道 %d 条，新抓取 %d 条" % (slug, len(arts), len(inc)))

    # 已收录报道的地址被清洗（Google 跳转 → 原文）时 URL 集合不变、也不算新增，
    # 但 event.json 的 sources 仍指向旧地址，必须靠指纹判定触发一次重算。
    prev_fp = None
    if os.path.exists(p["event"]):
        try:
            prev_fp = (load_json(p["event"]) or {}).get("articles_fingerprint")
        except Exception:
            prev_fp = None
    cur_fp = articles_fingerprint(arts)
    # event.json 已存在却没有指纹（升级遗留）时也重算一次，把指纹补写进去
    link_changed = bool(cur_fp) and (
        (prev_fp is None and os.path.exists(p["event"])) or (prev_fp is not None and prev_fp != cur_fp)
    )

    if not inc and not link_changed:
        print("NO_NEW [%s] 本轮无新增报道，不重算时间轴（feed 保持上一版）。" % slug)
        return

    if not inc:
        print("[%s 0/4] 已收录报道的链接/内容有变化（%s → %s），按现有库重算" % (slug, prev_fp, cur_fp))
        kept, dropped = dedupe.dedupe(arts)
    else:
        # 合并 + 三重去重（URL／标题／正文相似度），新条目排在前，旧条目为权威历史
        kept, dropped = dedupe.dedupe(inc + arts)

        # 真实增量判定：本轮抓到的全是被去重掉的旧 URL 时，视为无新增。
        # 否则每小时运行都会追加一条内容相同的版本快照并产生空提交（改每小时后才暴露）。
        old_urls = {(a.get("url") or "").strip() for a in arts}
        fresh = [a for a in kept if (a.get("url") or "").strip() not in old_urls]
        if not fresh:
            if not link_changed:
                print("NO_NEW [%s] 本轮抓到的 %d 条均为已收录报道，不写 event.json。" % (slug, len(inc)))
                open(p["incoming"], "w", encoding="utf-8").close()
                return
            # 抓到的是旧报道，但库里的收录地址刚被清洗过，仍需重算以刷新来源链接
            print("[%s 0/4] 本轮抓取均为已收录，但收录地址有变化（%s → %s），重算以刷新来源链接"
                  % (slug, prev_fp, cur_fp))

    print("[%s 2/4] 去重：保留 %d 条，剔除重复 %d 条" % (slug, len(kept), len(dropped)))
    for d in dropped:
        print("      - 重复(%s): %s" % (d["reason"], d.get("title")))

    align.align_articles(kept)
    ok = sum(1 for a in kept if a.get("parsed_time", {}).get("ok"))
    print("[%s 3/4] 时间戳对齐：成功 %d/%d 条" % (slug, ok, len(kept)))

    nodes, stats = merge.build_timeline(kept)
    print("[%s 4/4] 重算时间轴：节点 %d 个，冲突节点 %d 个"
          % (slug, stats["node_count"], stats["conflict_nodes"]))

    # 持久化：articles.jsonl 覆盖为去重后的全集；event.json 追加版本快照
    dump_jsonl(p["articles"], kept)
    open(p["incoming"], "w", encoding="utf-8").close()
    save_event(slug, p, load_json(p["meta"]), load_json(p["analysis"]), nodes, stats,
               fingerprint=articles_fingerprint(kept))
    print("已更新 %s" % p["event"])


def save_event(slug, p, meta, analysis, timeline, stats, fingerprint=None):
    """写 event.json 并追加版本快照（字段与 news-timeline-builder 的 store.save_event 一致）。"""
    if os.path.exists(p["event"]):
        ev = load_json(p["event"])
    else:
        ev = {"slug": slug, "created_at": now_iso(), "versions": []}
    ev.setdefault("slug", slug)
    ev["meta"] = meta
    ev["analysis"] = analysis
    ev["timeline"] = timeline
    ev["updated_at"] = now_iso()
    ev["last_stats"] = stats
    if fingerprint:
        ev["articles_fingerprint"] = fingerprint
    ev.setdefault("versions", []).append({
        "updated_at": ev["updated_at"],
        "node_count": len(timeline),
        "conflict_nodes": sum(1 for n in timeline if n.get("conflict")),
        "article_count": sum(n.get("article_count", 0) for n in timeline),
        "source": "github-actions",
    })
    save_json(p["event"], ev)
    return ev


# ===================== 出口 =====================
if __name__ == "__main__":
    sys.exit(main())
