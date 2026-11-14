# -*- coding: utf-8 -*-
"""
fetch_articles.py — 自主检索器（GitHub / 无人值守模式）
=====================================================
供调度层在「没有 LLM 代理」的环境里拉取新报道，映射到本 skill 的 article schema。

支持来源：
  - google_news : 以关键词构造 Google News RSS 检索（免费、无需密钥）
  - rss_file    : 读取本地 RSS/Atom XML 文件（离线测试 / 私有源）
  - mock        : 产出合成 article（无网环境冒烟测试）

输出：标准 article JSONL（每行一个），写入 --out 指定文件（缺省 stdout）。
每条 article 字段（与流水线一致）：
  id, url, title, content, published_at(ISO), credibility, event_label, facts

时间处理：RSS 的 RFC822 发布时间先转 ISO 再交给 align.py；
          仅保留比上次拉取（events/<slug>/.last_fetch）更新的条目。
"""
import argparse
import json
import os
import re
import sys
import html
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from schema import article_id

DEFAULT_TZ = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (news-timeline-builder fetcher)"


def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _rfc822_to_iso(s):
    """把 RFC822 时间（如 'Wed, 11 Nov 2026 14:30:00 GMT'）转 ISO；失败返回 None。"""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(DEFAULT_TZ).isoformat()
    except Exception:
        return None


def _fetch_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


# ---- Google News 跳转链接还原 -------------------------------------------------
# 背景：Google News RSS 的 <link> 是 news.google.com/rss/articles/<密文> 的跳转页，
# 既不含正文也无法本地解码（新版为加密串）。入库前必须还原成原文地址，
# 否则后续归类拿不到正文，订阅者点开也多一跳。
_GOOGLE_HOSTS = ("news.google.com",)
_BLOCK_HOSTS = (
    "gstatic.com", "googleusercontent.com", "google.com", "googleapis.com",
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "googleadservices.com", "googlesyndication.com", "youtube.com", "bootstrapcdn.com",
)
_STATIC_EXT = (
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".json", ".xml", ".txt",
)


def _is_google_link(u):
    try:
        h = urllib.parse.urlsplit(u).netloc.lower()
    except Exception:
        return False
    if any(h == x or h.endswith("." + x) for x in _GOOGLE_HOSTS):
        return True
    return any(h == x or h.endswith("." + x) for x in _BLOCK_HOSTS)


def _looks_like_article(u):
    """判断一个 URL 是否像文章页：排除静态资源与统计/广告域名。"""
    try:
        p = urllib.parse.urlsplit(u)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = p.netloc.lower()
    if any(host == h or host.endswith("." + h) for h in _BLOCK_HOSTS + _GOOGLE_HOSTS):
        return False
    path = p.path.lower()
    if any(path.endswith(ext) for ext in _STATIC_EXT):
        return False
    return True


def _batchexecute(link, signature, timestamp, timeout=15):
    """调用 Google News 内部的 batchexecute 接口换取原文地址。

    用 json.dumps 构造请求体再整体做表单编码：手拼字符串时转义极易出错，
    Google 会直接返回 result=null（错误码 3），看起来像"接口失效"。
    """
    seg = link.split("/articles/")[-1].split("?")[0]
    inner = [
        "garturlreq",
        [["X", "X", ["X", "X"], None, None, 1, 1, "CN:zh-Hans", None, 1,
          None, None, None, None, None, 0, 1],
         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
        seg, timestamp, signature,
    ]
    rpc = [[["Fbv4je", json.dumps(inner, separators=(",", ":"))]]]
    payload = "f.req=" + urllib.parse.quote(json.dumps(rpc, separators=(",", ":")))
    req = urllib.request.Request(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je",
        data=payload.encode("utf-8"),
        headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Same-Domain": "1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "ignore")
    except Exception as e:
        if os.environ.get("DEBUG_RESOLVE") == "1":
            print("[resolve-debug] batchexecute 请求失败: %s" % str(e)[:90], file=sys.stderr)
        return None
    if os.environ.get("DEBUG_RESOLVE") == "1":
        print("[resolve-debug] batchexecute resp_len=%d head=%r" % (len(txt), txt[:200]), file=sys.stderr)
    # 响应里的地址是 JSON 转义形式（https:\/\/host\/path，可能多层反斜杠），统一还原
    flat = re.sub(r"\\+/", "/", txt)
    for cand in re.findall(r"https?://[^\s\"'<>]+", flat):
        cand = cand.rstrip("\\,")
        if _looks_like_article(cand):
            return cand
    return None


def resolve_google_link(link, timeout=15):
    """把 Google News 跳转链接还原为原文 URL；失败返回 None（调用方保留原链接）。"""
    if not _is_google_link(link):
        return link
    try:
        page = _fetch_url(link, timeout)
    except Exception:
        page = ""
    debug = os.environ.get("DEBUG_RESOLVE") == "1"
    if page:
        # 方式一：文章页的 data-n-au 属性直接带原文地址（权威来源）
        m = re.search(r'data-n-au="([^"]+)"', page)
        if m:
            u = html.unescape(m.group(1))
            if _looks_like_article(u):
                return u
        # 方式二：页面里的锚点链接，按"像文章页"过滤（静态资源/统计域名已排除）
        hrefs = []
        for cand in re.findall(r'href="(https?://[^"]+)"', page):
            cand = html.unescape(cand).rstrip("/")
            if cand not in hrefs:
                hrefs.append(cand)
        for cand in hrefs:
            if _looks_like_article(cand):
                if debug:
                    print("[resolve-debug] href 命中: %s" % cand[:110], file=sys.stderr)
                return cand
        if debug:
            hosts = []
            for u in hrefs:
                h = urllib.parse.urlsplit(u).netloc.lower()
                if h and h not in hosts:
                    hosts.append(h)
            print("[resolve-debug] page_len=%d href=%d hosts=%s"
                  % (len(page), len(hrefs), hosts[:12]), file=sys.stderr)
        # 方式三：用页面里的签名与时间戳调 batchexecute
        sg = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if sg and ts:
            u = _batchexecute(link, sg.group(1), ts.group(1), timeout)
            if _looks_like_article(u):
                return u
    elif debug:
        print("[resolve-debug] 页面抓取失败", file=sys.stderr)
    # 方式三：无签名直调 batchexecute（部分链接适用）
    u = _batchexecute(link, "", str(int(datetime.now().timestamp())), timeout)
    return u if _looks_like_article(u) else None


def _parse_rss(xml_text):
    """解析 RSS 2.0 / Atom，返回 [(title, link, pub_iso, description), ...]。"""
    import xml.etree.ElementTree as ET
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[fetch] RSS 解析失败: {e}", file=sys.stderr)
        return items

    # RSS 2.0
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or it.findtext("dc:date") or ""
        desc = it.findtext("description") or it.findtext("summary") or ""
        items.append((title, link, _rfc822_to_iso(pub), _strip_html(desc)))

    # Atom
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall(".//a:entry", ns):
            title = (e.findtext("a:title", namespaces=ns) or "").strip()
            link = ""
            for l in e.findall("a:link", ns):
                link = l.get("href", "")
                if l.get("rel") == "alternate" or link:
                    break
            pub = e.findtext("a:updated", namespaces=ns) or e.findtext("a:published", namespaces=ns) or ""
            pub_iso = None
            if pub:
                try:
                    pub_iso = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(DEFAULT_TZ).isoformat()
                except Exception:
                    pub_iso = None
            desc = e.findtext("a:summary", namespaces=ns) or e.findtext("a:content", namespaces=ns) or ""
            items.append((title, link, pub_iso, _strip_html(desc)))
    return items


def fetch_google_news(keywords, since_dt=None, timeout=20, resolve=True, resolve_limit=None):
    q = " OR ".join(f'"{k}"' for k in keywords) if len(keywords) > 1 else keywords[0]
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q) +
           "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
    try:
        xml = _fetch_url(url, timeout)
    except Exception as e:
        print(f"[fetch] Google News 拉取失败（跳过）: {e}", file=sys.stderr)
        return []
    return _filter(_parse_rss(xml), since_dt, resolve=resolve, resolve_limit=resolve_limit)


def fetch_rss_file(path, since_dt=None, resolve=True, resolve_limit=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            xml = f.read()
    except Exception as e:
        print(f"[fetch] 读取本地 RSS 失败: {e}", file=sys.stderr)
        return []
    return _filter(_parse_rss(xml), since_dt, resolve=resolve, resolve_limit=resolve_limit)


def fetch_mock(keywords, since_days=2):
    now = datetime.now(DEFAULT_TZ)
    templates = [
        "合成报道#1：化工园区储罐发生泄漏，应急连夜处置。",
        "合成报道#2：环保部门对周边水体开展加密监测，数据逐小时上报。",
        "合成报道#3：附近居民已临时转移安置，官方开通咨询热线。",
    ]
    rows = []
    for i, kw in enumerate(keywords[:3]):
        dt = now - timedelta(hours=(i + 1) * 5)
        rows.append((
            f"【测试】关于{kw}的最新进展报道（#{i+1}）",
            f"https://example.com/mock/{i+1}",
            dt.isoformat(),
            f"围绕「{kw}」的{templates[i]}",
        ))
    return _filter(rows, None)


def _filter(parsed, since_dt, resolve=False, resolve_limit=None):
    out = []
    ok = fail = 0
    for title, link, pub_iso, desc in parsed:
        if not title:
            continue
        if since_dt and pub_iso:
            try:
                if datetime.fromisoformat(pub_iso) < since_dt:
                    continue
            except Exception:
                pass

        final_url = link
        kind = "original"
        if resolve:
            budget_left = resolve_limit is None or (ok + fail) < resolve_limit
            if budget_left:
                real = resolve_google_link(link)
                if real and real != link:
                    final_url, kind = real, "original"
                    ok += 1
                elif _is_google_link(link):
                    kind = "google_redirect"
                    fail += 1
            elif _is_google_link(link):
                kind = "google_redirect"      # 超出预算，保留跳转链接

        out.append({
            "id": article_id(final_url, title, desc),
            "url": final_url,
            "title": title,
            "content": desc,
            "published_at": pub_iso or "",
            "credibility": "medium",
            "event_label": title,  # 自主模式无 LLM 归类，每条报道独立成节点
            "facts": {},
            "link_kind": kind,
        })

    if resolve:
        print(f"[fetch] 链接还原：成功 {ok} 条，未还原 {fail} 条", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser(description="自主检索新报道并映射为 article JSONL")
    ap.add_argument("--slug", required=True, help="事件 slug（用于写 .last_fetch）")
    ap.add_argument("--keywords", required=True, help="逗号分隔的关键词")
    ap.add_argument("--mode", choices=["google_news", "rss_file", "mock"], default="google_news")
    ap.add_argument("--rss-file", help="rss_file 模式的本地 XML 路径")
    ap.add_argument("--workdir", default="events", help="事件持久化根目录（写 .last_fetch）")
    ap.add_argument("--since-days", type=int, default=2, help="只取最近 N 天内的报道")
    ap.add_argument("--out", help="输出 JSONL 路径，缺省 stdout")
    ap.add_argument("--credibility", default="medium", help="来源可信度等级")
    ap.add_argument("--resolve", dest="resolve", action="store_true", default=True,
                    help="把 Google News 跳转链接还原为原文地址（默认开启）")
    ap.add_argument("--no-resolve", dest="resolve", action="store_false",
                    help="不还原链接，保留 Google 跳转地址")
    ap.add_argument("--resolve-limit", type=int, default=40,
                    help="单轮最多还原多少条链接（控制时长，缺省 40）")
    args = ap.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    since_dt = datetime.now(DEFAULT_TZ) - timedelta(days=args.since_days)

    if args.mode == "google_news":
        articles = fetch_google_news(keywords, since_dt, resolve=args.resolve,
                                     resolve_limit=args.resolve_limit)
    elif args.mode == "rss_file":
        articles = fetch_rss_file(args.rss_file, since_dt, resolve=args.resolve,
                                  resolve_limit=args.resolve_limit)
    else:
        articles = fetch_mock(keywords, args.since_days)

    for a in articles:
        a["credibility"] = args.credibility

    # 更新 last_fetch 标记
    if articles:
        edir = os.path.join(args.workdir, args.slug)
        os.makedirs(edir, exist_ok=True)
        with open(os.path.join(edir, ".last_fetch"), "w", encoding="utf-8") as f:
            f.write(datetime.now(DEFAULT_TZ).isoformat())

    text = "\n".join(json.dumps(a, ensure_ascii=False) for a in articles)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + ("\n" if text else ""))
        print(f"[fetch] 检索到 {len(articles)} 条新报道 -> {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
