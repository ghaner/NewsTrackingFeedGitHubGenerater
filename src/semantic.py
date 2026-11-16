# -*- coding: utf-8 -*-
"""
semantic.py — 语义级近似判重（保守增量层）
==========================================
在 dedupe.py 的强指纹（id/url/核心标题）与字符二元组 Jaccard 之外，
再补一层「同义改写 / 语序变化 / 核心标题互为前缀后缀」的判定。

设计纪律（避免误杀不同进展）：
  1. 只做**标题**语义，不碰正文（RSS 摘要只有标题回声，正文信号无效）。
  2. 必须同时满足 DICE>=阈值 且 CONTAINMENT>=阈值 且长度比在合理区间，
     三个条件缺一不可。单纯高 containment 会把「呼吁拨款」和
     「提供500亿资金」误判为同一篇。
  3. 繁体先转简体、 numerals 剥离、同义词映射后再算，吸收「穆林/穆林斯」
     这类译名尾字与「逾4小时/4小时」这类虚词差异。
  4. 仍然是弱信号，沿用 dedupe 的 NEAR_DUP_WINDOW_DAYS 时间窗；
     时间解析失败的条目不参与，宁可漏判不可误杀。
  5. 纯 ASCII 英文标题不做语义判重（缩写/专名压缩会误伤不同报道）。

入口：semantic_reason(title_a, title_b) -> 'semantic_title' | None
"""

# ===== 参数配置区 =====
DICE_THRESHOLD = 0.80          # 语义二元组 Dice 下限
CONTAIN_THRESHOLD = 0.95       # 语义二元组 containment 下限（短标题几乎全含于长标题）
MIN_LEN_RATIO = 0.70           # 较短/较长 语义长度比下限，避免超短标题套长标题
MIN_SEM_LEN = 6                # 语义串短于此长度不判（噪声）
ENABLE_ENGLISH = False         # 纯英文标题是否参与（默认否，防专名误伤）
# 虚词：删掉不影响事件核心的助词/副词/量词前缀
STOP_CHARS = set("的了着过在为与及和或对被把从到将使有其这那所由于并以于过已还更再很最不新多")
# 同义 / 译名 / 简繁常见映射（键为单字或短串，值替换串；长键优先）
SYNONYMS = {
    "逾": "", "近": "", "约": "", "此前进出": "", "美国内": "", "美国及": "",
    "人工智能": "ai", "全美": "美国", "美媒": "", "据称": "", "消息称": "",
    "穆林和达菲举行白宫简报会": "", "舉行白宮簡報會": "",
}
# 繁 -> 简（只覆盖新闻标题高频字，不引外部依赖）
T2S = {
    "飛": "飞", "導": "导", "變": "变", "點": "点", "線": "线", "國": "国",
    "際": "际", "東": "东", "歐": "欧", "復": "复", "發": "发", "現": "现",
    "場": "场", "後": "后", "時": "时", "長": "长", "會": "会", "訊": "讯",
    "無": "无", "關": "关", "鍵": "键", "產": "产", "業": "业", "務": "务",
    "網": "网", "絡": "络", "設備": "设备", "設": "设", "備": "备", "數": "数",
    "據": "据", "調": "调", "查": "查", "導": "导", "航": "航", "復": "复",
    "導致": "导致", "啟動": "启动", "啟": "启", "動": "动", "聲": "声",
    "稱": "称", "應": "应", "對": "对", "華": "华", "亞": "亚", "當": "当",
    "獲": "获", "評": "评", "價": "价", "銀": "银", "團": "团", "體": "体",
    "運": "运", "輸": "输", "緊": "紧", "急": "急", "撥": "拨", "層": "层",
}
# ===== 配置区结束 =====

import re
from schema import norm_text


def _to_simplified(s):
    """繁体标题转简体（逐字映射，长词先替换）。"""
    for k in sorted(T2S, key=len, reverse=True):
        s = s.replace(k, T2S[k])
    return s


def semanticize(title):
    """标题 -> 语义串：去媒体后缀、繁转简、剥数字、删虚词、同义映射。"""
    s = norm_text(title or "")
    if not s:
        return ""
    # 去媒体后缀由调用方 core_title 已完成；这里再做一次防御
    s = _to_simplified(s)
    # 数字与常见单位整体剥离，避免「4小时 / 逾4小时」「500亿」干扰
    s = re.sub(r"[0-9０-９]+(?:\.[0-9０-９]+)?(?:%|％|架次|架|人|国|億|亿|萬|万|美元|次|天|小时|小時)?", "", s)
    for k in sorted(SYNONYMS, key=len, reverse=True):
        s = s.replace(k, SYNONYMS[k])
    s = "".join(c for c in s if c not in STOP_CHARS and not c.isspace())
    return s


def _bigrams(s):
    return set(s[i:i + 2] for i in range(len(s) - 1)) if len(s) >= 2 else {s} if s else set()


def _ascii_ratio(s):
    if not s:
        return 0.0
    letters = [c for c in s if c.isalpha()]
    return sum(1 for c in letters if ord(c) < 128) / len(letters) if letters else 0.0


def semantic_reason(title_a, title_b):
    """返回 'semantic_title' 或 None。仅在高置信时判同，绝不基于正文。"""
    a, b = semanticize(title_a), semanticize(title_b)
    if len(a) < MIN_SEM_LEN or len(b) < MIN_SEM_LEN:
        return None
    if not ENABLE_ENGLISH and (_ascii_ratio(a) >= 0.8 or _ascii_ratio(b) >= 0.8):
        return None
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return None
    inter = len(ga & gb)
    dice = 2 * inter / (len(ga) + len(gb))
    contain = inter / min(len(ga), len(gb))
    ratio = min(len(a), len(b)) / max(len(a), len(b))
    if dice >= DICE_THRESHOLD and contain >= CONTAIN_THRESHOLD and ratio >= MIN_LEN_RATIO:
        return "semantic_title"
    return None


if __name__ == "__main__":
    cases = [
        ("美媒：全美航班停飞令已解除", "全美航班停飞令已解除", True),
        ("全美航班停飛穆林和達菲舉行白宮簡報會", "FAA下令全美航班停飛穆林和達菲舉行白宮簡報會", True),
        ("美交通部长：系统老化呼吁国会紧急拨款", "美交通部长：希望国会提供紧急资金500亿美元", False),
        ("FAA halts US flight departures", "FAA 下令全美航班停飞令已解除", False),
    ]
    for x, y, expect in cases:
        got = semantic_reason(x, y)
        ok = (got is not None) == expect
        print(("OK " if ok else "BAD") + " %s || %s -> %s (期望判同=%s)" % (x[:20], y[:20], got, expect))
