"""市场情绪数据层：板块强度 + 龙虎榜净买额（历史序列）。

数据源（均为东方财富公开接口，无需 Key）：
- 板块强度：26 个主要行业指数的日 K 线，计算「板块 20 日动量向上占比」；
- 龙虎榜：历史区间查询，按日汇总净买额。
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime

import pandas as pd
import requests

from . import data

CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 26 个主要行业（覆盖全市场，避免概念板块重叠）
BOARD_WATCH = [
    "电子", "半导体", "通信", "医药生物", "机械设备", "电力设备", "计算机",
    "有色金属", "基础化工", "传媒", "汽车", "公用事业", "电力", "银行",
    "酿酒", "食品饮料", "房地产", "证券Ⅱ", "保险Ⅱ", "煤炭", "钢铁", "石油石化",
    "家用电器", "交通运输", "国防军工", "农林牧渔",
    "白酒Ⅱ",
]

REQUEST_GAP = 0.35
MAX_RETRIES = 6


def _get_json(url: str, params: dict, timeout: int = 30) -> dict:
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=data.HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(2 ** attempt, 10) + random.random())
    raise RuntimeError(f"接口请求失败: {url}")


def fetch_board_codes() -> dict[str, str]:
    """拉取行业板块清单，返回 {板块名: BK 代码}。"""
    codes: dict[str, str] = {}
    page = 1
    while True:
        j = _get_json(
            CLIST_URL,
            {
                "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f12", "fs": "m:90+t:2", "fields": "f12,f14",
            },
        )
        diff = (j.get("data") or {}).get("diff") or []
        if not diff:
            break
        for row in diff:
            name = row.get("f14", "")
            if name in BOARD_WATCH:
                codes[name] = row["f12"]
        if len(diff) < 100:
            break
        page += 1
        if page > 20:
            break
        time.sleep(REQUEST_GAP)
    missing = set(BOARD_WATCH) - set(codes)
    if missing:
        print(f"  提示：以下板块未在清单中找到：{sorted(missing)}")
    return codes


def fetch_board_closes(
    codes: dict[str, str], start: datetime, end: datetime, cache_dir: str
) -> pd.DataFrame:
    """下载各行业指数日线收盘价，返回 {日期: 板块收盘价}。"""
    os.makedirs(cache_dir, exist_ok=True)
    closes = {}
    for name, bk in codes.items():
        cache_path = os.path.join(cache_dir, f"board_{bk}.csv")
        series = None
        if os.path.exists(cache_path):
            try:
                cached = pd.read_csv(cache_path, index_col=0, parse_dates=True)["close"]
                if (
                    len(cached)
                    and cached.index.min() - pd.Timedelta(days=7) <= pd.Timestamp(start)
                    and cached.index.max() + pd.Timedelta(days=7) >= pd.Timestamp(end)
                ):
                    series = cached
            except Exception:
                series = None
        if series is None:
            df = data.fetch_kline(bk, start, end, secid=f"90.{bk}")
            if len(df):
                df[["close"]].to_csv(cache_path)
                series = df["close"]
            time.sleep(REQUEST_GAP)
        if series is not None and len(series):
            closes[name] = series
    return pd.DataFrame(closes).sort_index()


def build_sentiment(board_closes: pd.DataFrame, mom_days: int = 20) -> pd.DataFrame:
    """由板块指数计算市场情绪序列。

    返回列：
    - board_breadth: 20 日动量向上的板块占比（板块赚钱效应）；
    - board_mom_avg: 板块 20 日动量均值。
    """
    pct = board_closes.pct_change(mom_days)
    out = pd.DataFrame(index=board_closes.index)
    out["board_breadth"] = (pct > 0).sum(axis=1) / pct.notna().sum(axis=1).replace(0, pd.NA)
    out["board_mom_avg"] = pct.mean(axis=1)
    return out


def fetch_lhb_net(start: datetime, end: datetime, cache_dir: str) -> pd.Series:
    """龙虎榜每日净买额（按日汇总），带本地缓存。"""
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "lhb_net.csv")
    if os.path.exists(cache_path):
        cached = pd.read_csv(cache_path, index_col=0, parse_dates=True)["lhb_net"]
        if (
            len(cached)
            and cached.index.min() - pd.Timedelta(days=7) <= pd.Timestamp(start)
            and cached.index.max() + pd.Timedelta(days=7) >= pd.Timestamp(end)
        ):
            return cached

    date_filter = (
        f"(TRADE_DATE>='{start:%Y-%m-%d}')(TRADE_DATE<='{end:%Y-%m-%d}')"
    )
    all_rows: list[dict] = []
    page = 1
    while True:
        j = _get_json(
            DATACENTER_URL,
            {
                "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
                "columns": "TRADE_DATE,SECURITY_CODE,SECURITY_NAME_ABBR,BILLBOARD_NET_AMT",
                "filter": date_filter,
                "pageNumber": page,
                "pageSize": 500,
                "sortColumns": "TRADE_DATE",
                "sortTypes": -1,
                "source": "WEB",
                "client": "WEB",
            },
            timeout=40,
        )
        result = j.get("result") or {}
        rows = result.get("data") or []
        all_rows.extend(rows)
        if page >= (result.get("pages") or 1) or not rows:
            break
        page += 1
        if page % 25 == 0:
            print(f"  龙虎榜进度：{page}/{result.get('pages')} 页")
        time.sleep(REQUEST_GAP)

    if not all_rows:
        raise RuntimeError("龙虎榜数据为空")
    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["TRADE_DATE"]).dt.normalize()
    df["net"] = pd.to_numeric(df["BILLBOARD_NET_AMT"], errors="coerce").fillna(0.0)
    daily = df.groupby("date")["net"].sum().sort_index()
    daily.to_csv(cache_path, header=["lhb_net"])
    print(f"  龙虎榜共 {len(daily)} 个交易日，覆盖 {daily.index.min():%Y-%m-%d} ~ {daily.index.max():%Y-%m-%d}")
    return daily
