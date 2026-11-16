# -*- coding: utf-8 -*-
"""
dedupe.py — 报道去重（跨轮次增强版）
==================================
对一批文章做确定性去重，输出保留集与被合并的重复项（含原因）。

五重判定（强 → 弱，任一命中即判重复）：
  1) same_id         稳定 id 相同
  2) same_url        URL 归一后一致（只剔除追踪参数，保留定位参数）
  3) same_title      核心标题一致（去栏目前缀 / 媒体后缀 / 噪声词）
  4) similar_title   核心标题二元组 Jaccard >= 阈值（仅历史窗口内生效）
  5) similar_content 正文二元组 Jaccard >= 阈值（正文足够长才参与）

两个入口：
  dedupe(articles)                   批内去重（原 API，run_pipeline.py 使用）
  dedupe_against(incoming, history)  跨轮次去重（store.add_articles 使用）

跨轮次是本次增强的核心：把历史 articles.jsonl 全量作为比对池，
几天前已收录的报道即使换了 URL、被别家转载、改了时间戳或带了媒体后缀，
也不会再次入库。Google News 未还原的跳转链接每轮密文都不同，
不参与 URL 判同，改由核心标题指纹接管。

策略：保留「先到」的一篇，重复项记录 dup_of 与 reason，供审计与回溯。
"""

# ===== 参数配置区（调阈值只改这里）=====
SIM_THRESHOLD = 0.85            # 正文二元组 Jaccard 阈值
TITLE_SIM_THRESHOLD = 0.82      # 核心标题二元组 Jaccard 阈值
MIN_TITLE_LEN = 8               # 短于此长度的核心标题不做近似判重
MIN_CONTENT_LEN = 12            # 短于此长度的正文不参与相似度判定（RSS 摘要噪声）
NEAR_DUP_WINDOW_DAYS = 7        # 弱信号只对最近 N 天的历史条目生效
MAX_AGE_DAYS = 3                # 抓取层旧闻硬截止：早于 N 天不作为新增（首次回填除外）

# 归一化 URL 时剔除的追踪参数（保留 __biz / mid / item_id / articleId 等定位参数）
TRACK_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "timestamp", "t", "scm", "share_token", "tt_from",
    "wxshare", "scene", "ref", "src", "share", "share_source", "utm", "_t",
}
# 媒体后缀分隔符（仅当后缀段足够短才切，避免切断复合标题）
MEDIA_SEPS = (" - ", " — ", " | ", "｜", " |", "_", "－", "–", "丨", " -", "- ")
# 未还原的 Google News 跳转页：密文每轮变化，不能作为 URL 主键
GOOGLE_REDIRECT_HOSTS = ("news.google.com",)
# ===== 配置区结束 =====

import re
import time
from urllib.parse import urlsplit, parse_qsl, urlencode

from schema import norm_text, char_bigrams, jaccard, article_id
from semantic import semantic_reason  # 第六重：语义改写判重（保守增量层）

_NOISE_LABEL = re.compile(r"^\s*[\[【（(]\s*[^\]】）)]{1,10}\s*[\]】）)]\s*")
_NOISE_PREFIX = re.compile(
    r"^\s*(快讯|独家|突发|最新|重磅|直播|视频|组图|多图|刚刚|速递|滚动|专题|热讯)"
    r"\s*[|:：、·\-—\s]+\s*"
)
_TAIL_TAG = re.compile(r"[\[【（(]\s*(图|视频|组图|多图|全文|直播|独家|附视频)\s*[\]】）)]\s*$")


# --- 入口一：URL 归一 ---
def norm_url(u):
    """URL 归一：去 scheme/www/fragment/追踪参数，保留定位文章的 query。

    只剔除追踪参数而不再砍掉整个 query —— 否则 mp.weixin.qq.com/s?__biz=...
    这类站点的全部文章会被归一成同一个 URL 而误判重复。
    Google 跳转链接每轮密文不同，返回空串，改由标题指纹判同。
    """
    if not u:
        return ""
    u = str(u).strip()
    if not u:
        return ""
    if any(h in u.lower() for h in GOOGLE_REDIRECT_HOSTS):
        return ""
    p = urlsplit(u if "://" in u else "http://" + u)
    if not p.netloc:
        return norm_text(u)[:200]
    host = re.sub(r"^www\.", "", p.netloc.lower())
    path = p.path.rstrip("/") or "/"
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in TRACK_PARAMS]
    qs = urlencode(sorted(q))
    return host + path + (("?" + qs) if qs else "")


# --- 入口二：核心标题 ---
def core_title(t):
    """核心标题：去栏目前缀、媒体后缀、噪声词、标点，用于跨来源转载判同。

    例：「【快讯】美媒：全美航班停飞令已解除 - 搜狐」
        → 「美媒全美航班停飞令已解除」
    """
    s = (t or "").strip()
    if not s:
        return ""
    s = _NOISE_LABEL.sub("", s)
    s = _NOISE_PREFIX.sub("", s)
    for sep in MEDIA_SEPS:
        if sep in s:
            head, _, tail = s.partition(sep)
            if head.strip() and len(tail.strip()) <= 14:
                s = head
                break
    s = _TAIL_TAG.sub("", s)
    return norm_text(s)


# --- 代码区：指纹与判定 ---
def published_epoch(a):
    """取报道时间的 UTC epoch 秒；优先用已对齐的 parsed_time，解析失败返回 None。"""
    pt = a.get("parsed_time") or {}
    if pt.get("ok") and pt.get("sort_key") is not None:
        return pt["sort_key"]
    from align import parse_timestamp
    r = parse_timestamp(a.get("published_at") or "")
    return r.get("sort_key") if r.get("ok") else None


def _fingerprint(a):
    """把一篇文章压成比对用的指纹（不改动原 dict）。"""
    title = core_title(a.get("title"))
    body = norm_text(a.get("content") or "")
    return {
        "id": a.get("id") or article_id(a.get("url"), a.get("title"), a.get("content")),
        "url": norm_url(a.get("url")),
        "title": title,
        "title_grams": char_bigrams(title) if len(title) >= MIN_TITLE_LEN else set(),
        "body_grams": char_bigrams(body) if len(body) >= MIN_CONTENT_LEN else set(),
        "epoch": published_epoch(a),
    }


def _match(new, old, now_epoch, sim_threshold):
    """单对新旧指纹判定，返回 reason 或 None。强信号永久生效，弱信号受时间窗约束。"""
    if new["id"] == old["id"]:
        return "same_id"
    if new["url"] and new["url"] == old["url"]:
        return "same_url"
    if new["title"] and new["title"] == old["title"]:
        return "same_title"
    old_epoch = old.get("epoch")
    if now_epoch is not None and old_epoch is not None:
        if now_epoch - old_epoch > NEAR_DUP_WINDOW_DAYS * 86400:
            return None
    if new["title_grams"] and old["title_grams"]:
        if jaccard(new["title_grams"], old["title_grams"]) >= TITLE_SIM_THRESHOLD:
            return "similar_title"
    if new["body_grams"] and old["body_grams"]:
        if jaccard(new["body_grams"], old["body_grams"]) >= sim_threshold:
            return "similar_content"
    # 第六重（弱信号，受同一时间窗约束）：同义改写 / 繁简 / 语序 / 核心标题互为前缀后缀
    if new["title"] and old["title"] and semantic_reason(new["title"], old["title"]):
        return "semantic_title"
    return None


def _drop_record(a, aid, target, reason):
    return {
        "id": aid,
        "title": a.get("title"),
        "url": a.get("url"),
        "published_at": a.get("published_at"),
        "dup_of": target,
        "reason": reason,
    }


# --- 入口三：批内去重（保持原 API）---
def dedupe(articles, sim_threshold=SIM_THRESHOLD):
    """articles: list[dict]，每项应有 title/content/url(可选)/id(可选)。
    返回 (kept, dropped)，dropped 每项含 id/title/url/dup_of/reason。
    """
    kept, dropped = [], []
    hist = []
    for a in articles:
        fp = _fingerprint(a)
        a["id"] = fp["id"]
        hit = None
        for h in hist:
            reason = _match(fp, h, None, sim_threshold)
            if reason:
                hit = (h["id"], reason)
                break
        if hit:
            dropped.append(_drop_record(a, fp["id"], hit[0], hit[1]))
        else:
            kept.append(a)
            hist.append(fp)
    return kept, dropped


# --- 入口四：跨轮次去重（本次增强的核心）---
def dedupe_against(incoming, history, sim_threshold=SIM_THRESHOLD, now_epoch=None):
    """把 incoming 与已入库的 history 全量比对，返回 (kept, dropped)。

    history 是 articles.jsonl 的历史全量，不是本轮增量 —— 这才能拦住
    「几天前收过、今天换个链接/转载又冒出来」的旧闻。
    """
    if now_epoch is None:
        now_epoch = time.time()
    kept_inner, dropped = dedupe(list(incoming), sim_threshold=sim_threshold)
    hist = [_fingerprint(h) for h in (history or [])]
    kept = []
    for a in kept_inner:
        fp = _fingerprint(a)
        hit = None
        for h in hist:
            reason = _match(fp, h, now_epoch, sim_threshold)
            if reason:
                hit = (h["id"], reason)
                break
        if hit:
            dropped.append(_drop_record(a, fp["id"], hit[0], hit[1]))
        else:
            kept.append(a)
            hist.append(fp)
    return kept, dropped


# --- 入口五：旧闻硬截止 ---
def is_stale(article, now_epoch, max_age_days=MAX_AGE_DAYS):
    """发布时间早于 now - max_age_days 视为过期稿，不再作为「新增」入库。
    解析不出时间时返回 False，交给历史指纹判定，避免误杀无时间标注的有效报道。
    """
    ep = published_epoch(article)
    if ep is None:
        return False
    return ep < now_epoch - max_age_days * 86400


# --- 出口：自测 ---
if __name__ == "__main__":
    history = [
        {"title": "【快讯】美媒：全美航班停飞令已解除", "content": "联邦航空局宣布解除全国地面停飞令，航班逐步恢复。",
         "url": "https://news.cctv.com/2026/11/14/ARTIx.shtml", "published_at": "2026-11-14T09:33:00+08:00"},
    ]
    incoming = [
        {"title": "美媒：全美航班停飞令已解除 - 搜狐", "content": "航班逐步恢复。",
         "url": "https://m.sohu.com/a/1101416159_121443915?scm=10001.325", "published_at": "2026-11-16T08:00:00+08:00"},
        {"title": "美交通部长呼吁国会紧急拨款", "content": "系统老化需资金更新，部长在发布会上提出拨款请求。",
         "url": "https://news.google.com/rss/articles/CBMiXXX", "published_at": "2026-11-16T09:00:00+08:00"},
    ]
    kept, dropped = dedupe_against(incoming, history)
    print("kept:", len(kept), "dropped:", len(dropped))
    for x in dropped:
        print("  dup:", (x["title"] or "")[:28], "->", x["dup_of"], x["reason"])
