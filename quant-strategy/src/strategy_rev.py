"""短期反转因子：按近 N 日涨幅排名，买入最弱的一批，定期轮换。

这是均值回归更正统的截面实现：不预测"哪只反弹"，而是系统性持有
过去 N 日跌幅最大的股票组合，赌短期超跌回归。A 股短期反转效应有较多实证支持。

持仓层约束（全部可选）：
- 流动性过滤：20 日均成交额下限；
- 行业分散：每行业最多持有 N 只；
- 剔除 ST 与上市不足 N 天的次新股；
- 波动率倒数加权 + 单票上限在回测引擎的买入环节实现。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class ReversalParams:
    ret_days: int = 5
    bottom_n: int = 30
    min_amount: float = 0.0    # 20 日均成交额下限（元），0 = 不限
    max_industry: int = 0      # 每行业最多持有数，0 = 不限
    ipo_min_days: int = 60    # 上市不足 N 天剔除
    meta: dict | None = None  # {code: {industry, st, ipo_date}}


def build_rev_frames(
    dfs: dict[str, pd.DataFrame], params: ReversalParams
) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for code, df in dfs.items():
        close = df["close"]
        vol20 = close.pct_change().rolling(20).std() * np.sqrt(252)
        amt20 = (
            df["amount"].rolling(20).mean()
            if "amount" in df
            else pd.Series(np.nan, index=df.index)
        )
        out[code] = pd.DataFrame(
            {
                "close": close,
                "ret": close.pct_change(params.ret_days),
                "vol20": vol20,
                "liquid": amt20 >= params.min_amount,
            }
        )
    return out


def select_rev_targets(
    frames: dict[str, pd.DataFrame],
    dates: list[pd.Timestamp],
    params: ReversalParams,
) -> dict[pd.Timestamp, list[str]]:
    sig = pd.DataFrame({code: f["ret"] for code, f in frames.items()}).reindex(dates)
    meta = params.meta or {}
    st_codes = {c for c, m in meta.items() if m.get("st")}
    ipo_ts = {
        c: pd.Timestamp(m["ipo_date"])
        for c, m in meta.items()
        if m.get("ipo_date") and m["ipo_date"] != ""
    }
    idx = pd.DatetimeIndex(dates)
    elig = pd.DataFrame(True, index=idx, columns=sig.columns)
    for c, ipo in ipo_ts.items():
        if c in elig.columns:
            elig.loc[idx < ipo + pd.Timedelta(days=params.ipo_min_days), c] = False
    keep_cols = [c for c in sig.columns if c not in st_codes]
    sig = sig[keep_cols]
    if params.min_amount > 0:
        liq = (
            pd.DataFrame({code: f["liquid"] for code, f in frames.items()})
            .reindex(dates)
            .fillna(False)
        )[keep_cols]
        mask = liq & elig[keep_cols]
    else:
        mask = elig[keep_cols]
    industry = {c: m.get("industry") for c, m in meta.items()}

    targets: dict[pd.Timestamp, list[str]] = {}
    for d in dates:
        row = sig.loc[d].where(mask.loc[d]).dropna()
        if row.empty:
            targets[d] = []
            continue
        picked: list[str] = []
        ind_count: dict[str, int] = {}
        for c in row.sort_values().index:
            if len(picked) >= params.bottom_n:
                break
            if params.max_industry > 0 and industry.get(c):
                ind = industry[c]
                if ind_count.get(ind, 0) >= params.max_industry:
                    continue
                ind_count[ind] = ind_count.get(ind, 0) + 1
            picked.append(c)
        targets[d] = picked
    return targets


def latest_rev_snapshot(
    frames: dict[str, pd.DataFrame],
    names: dict[str, str],
    date: pd.Timestamp,
    params: ReversalParams,
) -> pd.DataFrame:
    rows = []
    for code, f in frames.items():
        if date not in f.index:
            continue
        row = f.loc[date]
        rows.append(
            {
                "code": code,
                "name": names.get(code, code),
                "close": round(float(row["close"]), 2),
                f"ret{params.ret_days}d_pct": (
                    round(float(row["ret"]) * 100, 2) if pd.notna(row["ret"]) else None
                ),
                "liquid": bool(row["liquid"]),
            }
        )
    df = pd.DataFrame(rows)
    return df.sort_values(f"ret{params.ret_days}d_pct", na_position="last")


def build_index_filters(
    index_close: pd.Series, vol_lookback: int = 20, mom_days: int = 20
) -> pd.DataFrame:
    """由指数收盘价计算风控序列：20 日已实现波动率（年化）与 20 日动量。"""
    ret = index_close.pct_change()
    out = pd.DataFrame(index=index_close.index)
    out["vol20"] = ret.rolling(vol_lookback).std() * np.sqrt(252)
    out["mom20"] = index_close.pct_change(mom_days)
    return out
