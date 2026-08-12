#!/usr/bin/env python3
"""A股每日复盘 + 自选股监控，输出终端摘要并可写入 Notion 日历数据库。

用法:
    python collect.py                          # 采集并写入 Notion
    python collect.py --dry-run                # 只采集并打印，不写入
    python collect.py --check-notion           # 只校验 Notion token/数据库
    python collect.py --watchlist 路径.json    # 指定自选股清单（默认 config/watchlist.json）

数据来源：默认东方财富公开接口（无需 Key）；设置 HITHINK_FINANCE_API_KEY 后，
指数/自选股/涨停池/龙虎榜/热股榜改用同花顺官方数据，其余仍由东方财富补充。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # 低版本 Python 兜底：固定 UTC+8
    TZ = timezone(timedelta(hours=8))

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "").strip()
# 未配置数据库 ID 时的回退（与 hackathon-daily 共用的 2026 日历数据库）
FALLBACK_DB_ID = "33660e0b0bbf806ab9e9effb9cebb712"

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# 同花顺官方金融数据服务（https://fuyao.aicubes.cn）
THS_API_KEY = os.environ.get("HITHINK_FINANCE_API_KEY", "").strip()
THS_BASE = "https://fuyao.aicubes.cn"

QUOTE_API = "https://push2.eastmoney.com/api/qt"
POOL_API = "https://push2ex.eastmoney.com"
DATACENTER_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
NEWS_URL = "https://finance.eastmoney.com/a/czqyw.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

# 大盘指数：(secid, 名称)
INDICES = [
    ("1.000001", "上证指数"),
    ("0.399001", "深证成指"),
    ("0.399006", "创业板指"),
    ("1.000688", "科创50"),
    ("1.000300", "沪深300"),
    ("0.899050", "北证50"),
]

# 涨跌家数口径：(secid, 展示标签)
BREADTH = [
    ("1.000001", "沪市"),
    ("0.399001", "深市"),
    ("0.399006", "创业板"),
    ("1.000688", "科创板(科创50成分)"),
    ("0.899050", "北交所"),
]

REQUEST_GAP = 0.5  # 东方财富接口有频控，请求间最小间隔（秒）
QUOTE_FIELDS = "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18"
BREADTH_FIELDS = "f14,f113,f114,f115"
WATCHLIST_PATH = ""
SOURCE = "auto"

# 指数 thscode（同花顺官方）：仅支持 .SH/.SZ；北证50 不支持，由东方财富补充
THS_INDICES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
    ("000300.SH", "沪深300"),
]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Quote:
    code: str
    name: str
    price: float | None = None
    pct: float | None = None
    change: float | None = None
    amount: float | None = None  # 元
    high: float | None = None
    low: float | None = None
    open_: float | None = None


@dataclass
class Breadth:
    label: str
    up: int = 0
    down: int = 0
    flat: int = 0


@dataclass
class PoolItem:
    code: str
    name: str
    pct: float | None = None
    industry: str = ""
    reason: str = ""
    days: str = ""


@dataclass
class HotStock:
    code: str
    name: str
    heat: float = 0.0


@dataclass
class LHBItem:
    code: str
    name: str
    explanation: str
    price: float | None = None
    change_rate: float | None = None
    net_amt: float = 0.0  # 元


@dataclass
class Board:
    name: str
    pct: float


@dataclass
class NewsItem:
    title: str
    url: str


@dataclass
class WatchStock:
    name: str
    code: str
    quote: Quote | None = None


@dataclass
class Report:
    date: str
    indices: list[Quote] = field(default_factory=list)
    breadth: list[Breadth] = field(default_factory=list)
    zt_count: int = 0
    dt_count: int = 0
    zb_count: int = 0
    zt_items: list[PoolItem] = field(default_factory=list)
    dt_items: list[PoolItem] = field(default_factory=list)
    lhb_date: str = ""
    lhb_items: list[LHBItem] = field(default_factory=list)
    board_up: list[Board] = field(default_factory=list)
    board_down: list[Board] = field(default_factory=list)
    concept_up: list[Board] = field(default_factory=list)
    watchlist: list[WatchStock] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)
    hot_stocks: list[HotStock] = field(default_factory=list)
    source_label: str = "东方财富"
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 网络工具
# ---------------------------------------------------------------------------

def _get_json(url: str, params: dict | None = None, retries: int = 2) -> dict | None:
    """带重试的 JSON 请求；东方财富偶发空响应/频控，自动重试。"""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
            if resp.status_code == 200 and resp.text.strip():
                data = resp.json()
                if data is not None:
                    return data
        except Exception:
            pass
        if attempt < retries:
            time.sleep(2.0 * (attempt + 1))
    return None


def _get_text(url: str, retries: int = 2) -> str | None:
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text
        except Exception:
            pass
        if attempt < retries:
            time.sleep(2.0 * (attempt + 1))
    return None


def _fmt(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "--"
    return f"{v:.{digits}f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "--"
    return f"{v:+.2f}%"


def _fmt_yi(v: float | None) -> str:
    """元 -> 亿"""
    if v is None:
        return "--"
    return f"{v / 1e8:,.0f}亿"


def _fmt_yi1(v: float | None) -> str:
    """元 -> 亿（保留 1 位小数）"""
    if v is None:
        return "--"
    return f"{v / 1e8:,.1f}亿"


# ---------------------------------------------------------------------------
# 数据采集
# ---------------------------------------------------------------------------

def fetch_indices() -> list[Quote]:
    secids = ",".join(s for s, _ in INDICES)
    data = _get_json(
        f"{QUOTE_API}/ulist.np/get",
        {"fltt": 2, "invt": 2, "fields": QUOTE_FIELDS, "secids": secids},
    )
    if not data:
        return []
    diff = (data.get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    items: list[Quote] = []
    name_map = {secid.split(".")[1]: name for secid, name in INDICES}
    for d in diff:
        code = str(d.get("f12") or "")
        items.append(
            Quote(
                code=code,
                name=name_map.get(code, str(d.get("f14") or code)),
                price=d.get("f2"),
                pct=d.get("f3"),
                change=d.get("f4"),
                amount=d.get("f6"),
                high=d.get("f15"),
                low=d.get("f16"),
                open_=d.get("f17"),
            )
        )
    return items


def fetch_breadth() -> list[Breadth]:
    result: list[Breadth] = []
    for secid, label in BREADTH:
        data = _get_json(
            f"{QUOTE_API}/stock/get",
            {"secid": secid, "fields": BREADTH_FIELDS},
        )
        d = (data or {}).get("data") or {}
        result.append(
            Breadth(
                label=label,
                up=int(d.get("f113") or 0),
                down=int(d.get("f114") or 0),
                flat=int(d.get("f115") or 0),
            )
        )
        time.sleep(REQUEST_GAP)
    return result


def _fetch_pool(endpoint: str, date_compact: str) -> tuple[int, list[PoolItem], str]:
    data = _get_json(
        f"{POOL_API}/{endpoint}",
        {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "Pageindex": 0,
            "pagesize": 50,
            "sort": "fbt:asc",
            "date": date_compact,
        },
    )
    d = (data or {}).get("data") or {}
    total = int(d.get("tc") or 0)
    pool = d.get("pool") or []
    items = [
        PoolItem(
            code=str(x.get("c") or ""),
            name=str(x.get("n") or ""),
            pct=x.get("zdp"),
            industry=str(x.get("hybk") or ""),
        )
        for x in pool
        if isinstance(x, dict) and x.get("n")
    ]
    qdate = str(d.get("qdate") or "")
    return total, items[:10], qdate


def fetch_zt(date_compact: str) -> tuple[int, list[PoolItem], str]:
    total, items, qdate = _fetch_pool("getTopicZTPool", date_compact)
    return total, items, qdate


def fetch_dt(date_compact: str) -> tuple[int, list[PoolItem]]:
    total, items, _ = _fetch_pool("getTopicDTPool", date_compact)
    return total, items


def fetch_zb(date_compact: str) -> tuple[int, list[PoolItem]]:
    total, items, _ = _fetch_pool("getTopicZBPool", date_compact)
    return total, items


def fetch_lhb(date_str: str) -> tuple[str, list[LHBItem]]:
    """龙虎榜为盘后披露，当天可能为空：自动回退到最近一个有效交易日。"""
    probe = datetime.strptime(date_str, "%Y-%m-%d").date()
    for _ in range(8):
        filter_str = f"(TRADE_DATE='{probe.isoformat()}')"
        data = _get_json(
            DATACENTER_API,
            {
                "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
                "columns": (
                    "SECURITY_NAME_ABBR,SECURITY_CODE,EXPLANATION,CLOSE_PRICE,"
                    "CHANGE_RATE,BILLBOARD_NET_AMT,TRADE_DATE"
                ),
                "filter": filter_str,
                "pageNumber": 1,
                "pageSize": 10,
                "sortColumns": "BILLBOARD_NET_AMT",
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
            },
        )
        rows = (((data or {}).get("result") or {}).get("data")) or []
        if rows:
            items = [
                LHBItem(
                    code=str(r.get("SECURITY_CODE") or ""),
                    name=str(r.get("SECURITY_NAME_ABBR") or ""),
                    explanation=str(r.get("EXPLANATION") or ""),
                    price=r.get("CLOSE_PRICE"),
                    change_rate=r.get("CHANGE_RATE"),
                    net_amt=float(r.get("BILLBOARD_NET_AMT") or 0),
                )
                for r in rows
            ]
            # 同一只股票可能因多个上榜原因重复出现，按代码去重
            seen: set[str] = set()
            deduped: list[LHBItem] = []
            for it in items:
                if it.code and it.code not in seen:
                    seen.add(it.code)
                    deduped.append(it)
            return probe.isoformat(), deduped[:10]
        probe -= timedelta(days=1)
        while probe.weekday() >= 5:
            probe -= timedelta(days=1)
    return date_str, []


def _fetch_boards(fs: str, po: int, pz: int = 5) -> list[Board]:
    for attempt in range(4):
        data = _get_json(
            f"{QUOTE_API}/clist/get",
            {
                "pn": 1,
                "pz": pz,
                "po": po,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": fs,
                "fields": "f3,f12,f14",
            },
        )
        diff = (data or {}).get("data") or {}
        diff = diff.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        boards = [
            Board(name=str(d.get("f14") or ""), pct=d.get("f3") or 0.0)
            for d in diff
            if d.get("f14")
        ]
        if boards:
            return boards
        time.sleep(3.0 * (attempt + 1))
    return []


def fetch_boards() -> tuple[list[Board], list[Board], list[Board]]:
    industry_up = _fetch_boards("m:90+t:2+f:!50", po=1)
    time.sleep(REQUEST_GAP)
    industry_down = _fetch_boards("m:90+t:2+f:!50", po=0)
    time.sleep(REQUEST_GAP)
    concept_up = _fetch_boards("m:90+t:3+f:!50", po=1)
    return industry_up, industry_down, concept_up


def fetch_news() -> list[NewsItem]:
    html = _get_text(NEWS_URL)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    pattern = re.compile(r"^https://finance\.eastmoney\.com/a/\d+\.html$")
    items: list[NewsItem] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not pattern.match(href):
            continue
        title = " ".join(a.get_text().split())
        if not title or title in seen:
            continue
        seen.add(title)
        items.append(NewsItem(title=title, url=href))
        if len(items) >= 10:
            break
    return items


# ---------------------------------------------------------------------------
# 同花顺官方数据（HITHINK_FINANCE_API_KEY）
# ---------------------------------------------------------------------------

def ths_get(path: str, params: dict | None = None) -> dict | None:
    """同花顺官方 REST 请求：X-api-key 鉴权，code==0 才算成功。"""
    if not THS_API_KEY:
        return None
    for attempt in range(3):
        try:
            resp = requests.get(
                f"{THS_BASE}{path}",
                params=params,
                headers={**HEADERS, "X-api-key": THS_API_KEY},
                timeout=20,
            )
            if resp.status_code != 200:
                time.sleep(2.0 * (attempt + 1))
                continue
            body = resp.json()
            if body.get("code") != 0:
                if body.get("code") == 4001:  # 限流：退避重试
                    time.sleep(2.0 * (attempt + 1))
                    continue
                # 其他业务错误（参数/权限/数据未就绪）直接降级
                return None
            return body.get("data")
        except Exception:
            pass
        time.sleep(2.0 * (attempt + 1))
    return None


def _ths_code(code: str) -> str:
    """A股代码 -> 同花顺 thscode（6/9 沪，4/8 北，其余深）。"""
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def ths_fetch_indices() -> list[Quote]:
    codes = ",".join(c for c, _ in THS_INDICES)
    data = ths_get("/api/a-share-index/prices/snapshot", {"thscodes": codes})
    if not data:
        return []
    name_map = {c: n for c, n in THS_INDICES}
    items: list[Quote] = []
    for d in data.get("item") or []:
        thscode = str(d.get("thscode") or "")
        if not thscode:
            continue
        code = thscode.split(".")[0]
        items.append(
            Quote(
                code=code,
                name=name_map.get(thscode, str(d.get("ticker") or code)),
                price=d.get("last_price"),
                pct=d.get("price_change_ratio_pct"),
                change=d.get("price_change"),
                amount=d.get("turnover"),
                high=d.get("high_price"),
                low=d.get("low_price"),
                open_=d.get("open_price"),
            )
        )
    return items


def ths_fetch_watchlist_quotes(stocks: list[WatchStock]) -> list[WatchStock]:
    if not stocks:
        return stocks
    codes = ",".join(_ths_code(s.code) for s in stocks)
    data = ths_get("/api/a-share/prices/snapshot", {"thscodes": codes})
    if not data:
        return stocks
    by_code: dict[str, Quote] = {}
    for d in data.get("item") or []:
        ticker = str(d.get("ticker") or "")
        if not ticker:
            continue
        by_code[ticker] = Quote(
            code=ticker,
            name="",
            price=d.get("last_price"),
            pct=d.get("price_change_ratio_pct"),
            change=d.get("price_change"),
            amount=d.get("turnover"),
            high=d.get("high_price"),
            low=d.get("low_price"),
            open_=d.get("open_price"),
        )
    for s in stocks:
        q = by_code.get(s.code)
        if q:
            q.name = s.name or q.name
            s.quote = q
    return stocks


def ths_fetch_zt() -> tuple[int, list[PoolItem], str]:
    """同花顺涨停池：带涨停原因与连板数。返回 (总数, 前10, 交易日)。"""
    data = ths_get(
        "/api/a-share/special-data/limit-up-pool",
        {"sort_field": "limit_up_time", "sort_dir": "asc", "size": 100},
    )
    if not data:
        return 0, [], ""
    total = int(((data.get("pagination") or {}).get("total")) or 0)
    items = [
        PoolItem(
            code=str(d.get("ticker") or ""),
            name=str(d.get("name") or ""),
            pct=d.get("price_change_ratio_pct"),
            reason=str(d.get("limit_up_reason") or ""),
            days=str(d.get("continue_day_text") or ""),
        )
        for d in (data.get("item") or [])[:10]
    ]
    ts = data.get("timestamp")
    date_str = ""
    if ts:
        date_str = datetime.fromtimestamp(int(ts) / 1000, TZ).strftime("%Y-%m-%d")
    return total, items, date_str


def ths_fetch_lhb() -> tuple[str, list[LHBItem]]:
    data = ths_get("/api/a-share/special-data/dragon-tiger-list", {"board_type": "all"})
    if not data:
        return "", []
    rows = sorted(
        (data.get("stock_items") or []),
        key=lambda x: (x.get("net_value") or 0),
        reverse=True,
    )[:10]
    items = [
        LHBItem(
            code=str(x.get("ticker") or ""),
            name=str(x.get("name") or ""),
            explanation=str(x.get("limit_reason") or ""),
            change_rate=x.get("change"),
            net_amt=float(x.get("net_value") or 0),
        )
        for x in rows
    ]
    # 同一股票可能多条上榜记录，按代码去重
    seen: set[str] = set()
    deduped: list[LHBItem] = []
    for it in items:
        if it.code and it.code not in seen:
            seen.add(it.code)
            deduped.append(it)
    return str(data.get("trade_date") or ""), deduped


def ths_fetch_hot() -> list[HotStock]:
    data = ths_get("/api/a-share/special-data/hot-stock-list", {"period": "day"})
    if not data:
        return []
    return [
        HotStock(
            code=str(d.get("ticker") or ""),
            name=str(d.get("name") or ""),
            heat=float(d.get("heat") or 0),
        )
        for d in (data.get("item") or [])[:5]
    ]


def check_ths() -> int:
    if not THS_API_KEY:
        print("[error] 缺少环境变量 HITHINK_FINANCE_API_KEY", file=sys.stderr)
        return 2
    data = ths_get("/api/meta/tickers/search", {"q": "600519", "limit": 1})
    if not data:
        print("[error] 同花顺 API 校验失败：Key 无效或接口不可达", file=sys.stderr)
        return 1
    names = [str(x.get("name") or "") for x in (data.get("item") or [])]
    print(f"[ok] 同花顺 API 可用: {names}")
    return 0


def _secid(code: str) -> str:
    """A股代码 -> 东方财富 secid：6/9 开头为沪市，其余为深市/北交所。"""
    return ("1." if code.startswith(("6", "9")) else "0.") + code


def load_watchlist(path: str) -> list[WatchStock]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] 自选股配置读取失败 {path}: {exc}", file=sys.stderr)
        return []
    return [
        WatchStock(name=str(x.get("name") or ""), code=str(x.get("code") or ""))
        for x in data.get("watchlist", [])
        if x.get("code")
    ]


def fetch_watchlist_quotes(stocks: list[WatchStock]) -> list[WatchStock]:
    if not stocks:
        return []
    secids = ",".join(_secid(s.code) for s in stocks)
    data = _get_json(
        f"{QUOTE_API}/ulist.np/get",
        {"fltt": 2, "invt": 2, "fields": QUOTE_FIELDS, "secids": secids},
    )
    if not data:
        return stocks
    diff = (data.get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    by_code = {
        str(d.get("f12")): Quote(
            code=str(d.get("f12") or ""),
            name=str(d.get("f14") or ""),
            price=d.get("f2"),
            pct=d.get("f3"),
            change=d.get("f4"),
            amount=d.get("f6"),
            high=d.get("f15"),
            low=d.get("f16"),
            open_=d.get("f17"),
        )
        for d in diff
        if d.get("f12")
    }
    for s in stocks:
        s.quote = by_code.get(s.code)
    return stocks


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def render_text(r: Report) -> str:
    lines: list[str] = []
    lines.append("=" * 46)
    lines.append(f"{r.date} A股复盘与自选股")
    lines.append("=" * 46)

    lines.append("【大盘概况】")
    if r.indices:
        for q in r.indices:
            lines.append(
                f"- {q.name} {_fmt(q.price)} ({_fmt_pct(q.pct)}) 成交 {_fmt_yi(q.amount)}"
            )
        sh = next((q for q in r.indices if q.code == "000001"), None)
        sz = next((q for q in r.indices if q.code == "399001"), None)
        total = (sh.amount if sh else 0) + (sz.amount if sz else 0)
        lines.append(f"- 两市成交额合计约 {_fmt_yi(total)}")
    else:
        lines.append("- 指数数据获取失败，请稍后重试")

    lines.append("【市场情绪】")
    lines.append(f"- 涨停 {r.zt_count} 家 / 跌停 {r.dt_count} 家 / 炸板 {r.zb_count} 家")
    for b in r.breadth:
        lines.append(f"- {b.label}：上涨 {b.up} / 下跌 {b.down} / 平盘 {b.flat}")
    if r.zt_items:
        parts = []
        for x in r.zt_items[:8]:
            label = x.name if not x.days else f"{x.name}({x.days})"
            parts.append(f"{label} {_fmt_pct(x.pct)}")
        lines.append("- 涨停个股（部分）：" + "、".join(parts))
    reasons = [f"{x.name}：{x.reason}" for x in r.zt_items if x.reason][:3]
    if reasons:
        lines.append("- 涨停原因（部分）：" + "；".join(reasons))

    lines.append("【行业板块】")
    lines.append("- 涨幅前五：" + "、".join(f"{b.name} {_fmt_pct(b.pct)}" for b in r.board_up))
    lines.append("- 跌幅前五：" + "、".join(f"{b.name} {_fmt_pct(b.pct)}" for b in r.board_down))
    lines.append("【概念板块】")
    lines.append("- 涨幅前五：" + "、".join(f"{b.name} {_fmt_pct(b.pct)}" for b in r.concept_up))

    lines.append(f"【龙虎榜】（{r.lhb_date} 披露）")
    if r.lhb_items:
        for i, x in enumerate(r.lhb_items, 1):
            lines.append(
                f"{i}. {x.name} {x.code} 净买入 {_fmt_yi1(x.net_amt)} "
                f"涨跌 {_fmt_pct(x.change_rate)} {x.explanation}"
            )
    else:
        lines.append("- 暂无数据（盘后披露或当日非交易日）")

    if r.hot_stocks:
        lines.append("【热股榜】")
        for i, h in enumerate(r.hot_stocks, 1):
            lines.append(f"{i}. {h.name} {h.code} 热度 {h.heat:,.0f}")

    lines.append("【自选股】")
    if r.watchlist:
        for s in r.watchlist:
            q = s.quote
            if not q:
                lines.append(f"- {s.name} {s.code}：未取到行情")
                continue
            lines.append(
                f"- {s.name} {s.code} {_fmt(q.price)} ({_fmt_pct(q.pct)}) "
                f"最高 {_fmt(q.high)} 最低 {_fmt(q.low)} 成交 {_fmt_yi(q.amount)}"
            )
    else:
        lines.append("- 自选股配置为空")

    lines.append("【财经要闻】")
    if r.news:
        for i, n in enumerate(r.news, 1):
            lines.append(f"{i}. {n.title} {n.url}")
    else:
        lines.append("- 新闻获取失败")

    lines.append("【数据说明】")
    lines.append(f"- 数据来源：{r.source_label}，仅供研究参考，不构成投资建议。")
    lines.append("- 龙虎榜为盘后披露；周末/节假日运行会取最近交易日数据。")
    for w in r.warnings:
        lines.append(f"- ⚠️ {w}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Notion 写入
# ---------------------------------------------------------------------------

def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_create_page(parent: dict, properties: dict, children: list[dict] | None = None) -> dict:
    payload = {"parent": parent, "properties": properties}
    if children:
        payload["children"] = children
    resp = requests.post(f"{NOTION_API}/pages", headers=notion_headers(), json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Notion 创建失败 {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def notion_append_children(page_id: str, children: list[dict]) -> None:
    for i in range(0, len(children), 90):
        batch = children[i : i + 90]
        resp = requests.patch(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=notion_headers(),
            json={"children": batch},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Notion 追加块失败 {resp.status_code}: {resp.text[:500]}")


def _text_block(block_type: str, text: str) -> dict:
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _labeled_block(label: str, content: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [
                {"type": "text", "text": {"content": f"{label} "}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": content}},
            ]
        },
    }


def _link_block(label: str, text: str, url: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [
                {"type": "text", "text": {"content": f"{label} "}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": text, "link": {"url": url}}},
            ]
        },
    }


def build_children(r: Report) -> list[dict]:
    c: list[dict] = []
    c.append(_text_block("paragraph", f"**{r.date} A股复盘与自选股**（数据来源：{r.source_label}）"))

    c.append(_text_block("heading_2", "一、大盘概况"))
    if r.indices:
        for q in r.indices:
            c.append(_labeled_block(q.name, f"{_fmt(q.price)}（{_fmt_pct(q.pct)}），成交 {_fmt_yi(q.amount)}"))
        sh = next((q for q in r.indices if q.code == "000001"), None)
        sz = next((q for q in r.indices if q.code == "399001"), None)
        total = (sh.amount if sh else 0) + (sz.amount if sz else 0)
        c.append(_labeled_block("两市成交额", f"合计约 {_fmt_yi(total)}"))
    else:
        c.append(_labeled_block("指数数据", "获取失败，请稍后重试"))

    c.append(_text_block("heading_2", "二、市场情绪"))
    c.append(_labeled_block("涨跌停", f"涨停 {r.zt_count} 家 / 跌停 {r.dt_count} 家 / 炸板 {r.zb_count} 家"))
    for b in r.breadth:
        c.append(_labeled_block(b.label, f"上涨 {b.up} / 下跌 {b.down} / 平盘 {b.flat}"))
    if r.zt_items:
        parts = []
        for x in r.zt_items[:8]:
            label = x.name if not x.days else f"{x.name}({x.days})"
            parts.append(f"{label} {_fmt_pct(x.pct)}")
        c.append(_labeled_block("涨停个股（部分）", "、".join(parts)))
    reasons = [f"{x.name}：{x.reason}" for x in r.zt_items if x.reason][:3]
    if reasons:
        c.append(_labeled_block("涨停原因（部分）", "；".join(reasons)))

    c.append(_text_block("heading_2", "三、板块表现"))
    c.append(_labeled_block("行业涨幅前五", "、".join(f"{b.name} {_fmt_pct(b.pct)}" for b in r.board_up)))
    c.append(_labeled_block("行业跌幅前五", "、".join(f"{b.name} {_fmt_pct(b.pct)}" for b in r.board_down)))
    c.append(_labeled_block("概念涨幅前五", "、".join(f"{b.name} {_fmt_pct(b.pct)}" for b in r.concept_up)))

    c.append(_text_block("heading_2", f"四、龙虎榜（{r.lhb_date} 披露）"))
    if r.lhb_items:
        for x in r.lhb_items[:10]:
            c.append(
                _labeled_block(
                    f"{x.name} {x.code}",
                    f"净买入 {_fmt_yi1(x.net_amt)}，涨跌 {_fmt_pct(x.change_rate)}；{x.explanation}",
                )
            )
    else:
        c.append(_labeled_block("龙虎榜", "暂无数据（盘后披露或当日非交易日）"))

    if r.hot_stocks:
        c.append(_text_block("heading_2", "五、热股榜"))
        for h in r.hot_stocks[:5]:
            c.append(_labeled_block(h.name, f"{h.code}，热度 {h.heat:,.0f}"))

    c.append(_text_block("heading_2", "六、自选股"))
    if r.watchlist:
        for s in r.watchlist:
            q = s.quote
            if not q:
                c.append(_labeled_block(s.name, f"{s.code}：未取到行情"))
                continue
            c.append(
                _labeled_block(
                    s.name,
                    f"{s.code} {_fmt(q.price)}（{_fmt_pct(q.pct)}），最高 {_fmt(q.high)}，"
                    f"最低 {_fmt(q.low)}，成交 {_fmt_yi(q.amount)}",
                )
            )
    else:
        c.append(_labeled_block("自选股", "配置为空，请编辑 config/watchlist.json"))

    c.append(_text_block("heading_2", "七、财经要闻"))
    if r.news:
        for n in r.news[:10]:
            c.append(_link_block("📰", n.title, n.url))
    else:
        c.append(_labeled_block("要闻", "获取失败"))

    c.append(_text_block("heading_2", "说明"))
    c.append(_text_block("bulleted_list_item", f"⚠️ 数据来自{r.source_label}，仅供研究参考，不构成投资建议。"))
    c.append(_text_block("bulleted_list_item", "💡 龙虎榜为盘后披露；周末/节假日运行会取最近交易日数据。"))
    for w in r.warnings:
        c.append(_text_block("bulleted_list_item", f"⚠️ {w}"))
    return c


def query_database_rows(db_id: str) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    while True:
        payload: dict = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        resp = requests.post(
            f"{NOTION_API}/databases/{db_id}/query",
            headers=notion_headers(),
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"查询数据库 {db_id} 失败 {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


def write_to_database(r: Report) -> None:
    db_id = NOTION_DATABASE_ID or FALLBACK_DB_ID
    if NOTION_DATABASE_ID:
        print(f"[info] 使用 NOTION_DATABASE_ID={NOTION_DATABASE_ID}")
    else:
        print(f"[warn] 未设置 NOTION_DATABASE_ID，回退到默认数据库 {FALLBACK_DB_ID}")

    try:
        existing = query_database_rows(db_id)
    except RuntimeError as exc:
        print(f"[warn] 无法查询数据库，跳过写入: {exc}", file=sys.stderr)
        return

    summary_ids: list[str] = []
    existing_keys: set[tuple[str, str]] = set()
    for row in existing:
        props = row.get("properties", {})
        title, date = "", ""
        for p in props.values():
            if p.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in p.get("title", []))
            if p.get("type") == "date" and p.get("date"):
                date = p["date"].get("start", "")
        if title and date:
            existing_keys.add((title.strip(), date))
        if "股市复盘" in title:
            summary_ids.append(row["id"])

    for pid in summary_ids:
        try:
            resp = requests.patch(
                f"{NOTION_API}/pages/{pid}",
                headers=notion_headers(),
                json={"archived": True},
                timeout=30,
            )
            if resp.status_code == 200:
                print(f"[ok] 已归档旧复盘记录 {pid}")
        except requests.RequestException as exc:
            print(f"[warn] 归档旧记录失败 {pid}: {exc}", file=sys.stderr)

    title = f"{r.date} 股市复盘与自选股"
    if (title, r.date) in existing_keys:
        print(f"[skip] 数据库已存在: {title} @ {r.date}")
        return

    children = build_children(r)
    first_batch = children[:90]
    rest = children[90:]
    properties = {
        "名称": {"title": [{"type": "text", "text": {"content": title}}]},
        "日期": {"date": {"start": r.date}},
    }
    try:
        page = notion_create_page(
            {"type": "database_id", "database_id": db_id},
            properties,
            children=first_batch,
        )
        if rest:
            notion_append_children(page["id"], rest)
        print(f"[ok] 已写入复盘记录: {title} @ {r.date}")
    except RuntimeError as exc:
        print(f"[warn] 写入失败: {exc}", file=sys.stderr)


def validate_config() -> None:
    if not NOTION_TOKEN:
        print("[error] 缺少环境变量 NOTION_TOKEN", file=sys.stderr)
        sys.exit(2)


def check_notion() -> int:
    validate_config()
    db_id = NOTION_DATABASE_ID or FALLBACK_DB_ID
    resp = requests.get(
        f"{NOTION_API}/databases/{db_id}",
        headers=notion_headers(),
        timeout=30,
    )
    if resp.status_code != 200:
        print(
            f"[error] 无法访问数据库 {db_id}: {resp.status_code} {resp.text[:300]}",
            file=sys.stderr,
        )
        print(
            "请确认：1) token 有效；2) 数据库已分享给该 Notion Integration；"
            "3) 数据库有“名称”“日期”属性。",
            file=sys.stderr,
        )
        return 1
    data = resp.json()
    props = ", ".join(data.get("properties", {}).keys())
    print(f"[ok] 数据库可访问: {data.get('title', [{}])[0].get('plain_text', '')}，属性: {props}")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def collect() -> Report:
    global SOURCE
    today = datetime.now(TZ)
    date_str = today.strftime("%Y-%m-%d")
    date_compact = today.strftime("%Y%m%d")
    r = Report(date=date_str)

    use_ths = (SOURCE == "ths") or (SOURCE == "auto" and bool(THS_API_KEY))
    if use_ths:
        r.source_label = "同花顺(官方) + 东方财富(补充)"
        print("[info] 数据源：同花顺官方 + 东方财富补充")
    else:
        if SOURCE == "ths":
            print("[warn] 未设置 HITHINK_FINANCE_API_KEY，回退到东方财富数据源", file=sys.stderr)
        print("[info] 数据源：东方财富")

    print("[1/7] 拉取大盘指数…")
    if use_ths:
        r.indices = ths_fetch_indices()
    if not r.indices:
        if use_ths:
            print("[info] 同花顺指数获取失败，回退东方财富")
        r.indices = fetch_indices()
    elif use_ths:
        # 北证50 同花顺不支持，用东方财富补上
        em_quotes = fetch_indices()
        bj = next((q for q in em_quotes if q.code == "899050"), None)
        if bj:
            r.indices.append(bj)
    time.sleep(REQUEST_GAP)

    print("[2/7] 拉取涨跌家数…")
    r.breadth = fetch_breadth()

    print("[3/7] 拉取涨停/跌停/炸板池…")
    if use_ths:
        ths_zt = ths_fetch_zt()
        if ths_zt[1]:
            r.zt_count, r.zt_items, ths_date = ths_zt
            if ths_date:
                r.date = ths_date
        else:
            r.zt_count, r.zt_items, zt_qdate = fetch_zt(date_compact)
            if zt_qdate:
                r.date = f"{zt_qdate[:4]}-{zt_qdate[4:6]}-{zt_qdate[6:8]}"
    else:
        r.zt_count, r.zt_items, zt_qdate = fetch_zt(date_compact)
        if zt_qdate:
            r.date = f"{zt_qdate[:4]}-{zt_qdate[4:6]}-{zt_qdate[6:8]}"
    time.sleep(REQUEST_GAP)
    r.dt_count, r.dt_items = fetch_dt(date_compact)
    time.sleep(REQUEST_GAP)
    r.zb_count, _ = fetch_zb(date_compact)

    print("[4/7] 拉取龙虎榜…")
    if use_ths:
        r.lhb_date, r.lhb_items = ths_fetch_lhb()
    if not r.lhb_items:
        if use_ths:
            print("[info] 同花顺龙虎榜获取失败，回退东方财富")
        r.lhb_date, r.lhb_items = fetch_lhb(r.date)

    print("[4.5/7] 拉取热股榜…")
    if use_ths:
        r.hot_stocks = ths_fetch_hot()

    print("[5/7] 拉取板块表现…")
    r.board_up, r.board_down, r.concept_up = fetch_boards()

    print("[6/7] 拉取自选股行情…")
    default_watchlist = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config", "watchlist.json"
    )
    stocks = load_watchlist(WATCHLIST_PATH or default_watchlist)
    if use_ths:
        r.watchlist = ths_fetch_watchlist_quotes(stocks)
    if not any(s.quote for s in r.watchlist):
        if use_ths:
            print("[info] 同花顺自选股获取失败，回退东方财富")
        r.watchlist = fetch_watchlist_quotes(stocks)

    print("[7/7] 拉取财经要闻…")
    r.news = fetch_news()

    if not r.indices:
        r.warnings.append("指数行情获取失败（可能被频控），请稍后重试或增加 REQUEST_GAP。")
    if not r.news:
        r.warnings.append("财经要闻获取失败。")
    return r


def main() -> int:
    global SOURCE, WATCHLIST_PATH
    parser = argparse.ArgumentParser(description="A股每日复盘 + 自选股监控")
    parser.add_argument("--dry-run", action="store_true", help="只采集并打印，不写入 Notion")
    parser.add_argument("--check-notion", action="store_true", help="只校验 Notion token 与数据库")
    parser.add_argument("--check-ths", action="store_true", help="只校验同花顺 API Key")
    parser.add_argument("--source", choices=["auto", "ths", "em"], default="auto",
                        help="数据源：auto(有 Key 自动用同花顺)/ths/em")
    parser.add_argument("--watchlist", default="", help="自选股清单 JSON 路径（默认 config/watchlist.json）")
    args = parser.parse_args()

    if args.check_notion:
        return check_notion()
    if args.check_ths:
        return check_ths()

    SOURCE = args.source
    WATCHLIST_PATH = args.watchlist
    r = collect()
    print()
    print(render_text(r))
    print()

    if args.dry_run:
        print("[dry-run] 未写入 Notion")
        return 0

    validate_config()
    write_to_database(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
