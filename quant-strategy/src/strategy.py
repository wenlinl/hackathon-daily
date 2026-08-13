"""信号层：市场择时 + 个股筛选 + 打分排序。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass
class StrategyParams:
    ma_fast: int = 20
    ma_slow: int = 60
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_n: int = 14
    rsi_min: float = 35.0
    rsi_max: float = 75.0
    bb_n: int = 20
    bb_k: float = 2.0
    breadth_up20: float = 0.50
    breadth_up60: float = 0.25
    recent_cross_days: int = 10
    require_recent_cross: bool = True
    vol_ratio_n: int = 5
    vol_ratio_min: float = 1.0
    require_volume: bool = False
    use_vol_adj_mom: bool = False
    require_board_breadth: bool = False
    board_breadth_min: float = 0.50
    board_mom_days: int = 20
    require_lhb_positive: bool = False
    require_index_trend: bool = False
    index_ma: int = 200
    top_n: int = 10
    score_hist: float = 0.40
    score_mom: float = 0.35
    score_rsi: float = 0.25


def build_indicator_frames(
    dfs: dict[str, pd.DataFrame], params: StrategyParams
) -> dict[str, pd.DataFrame]:
    """为每只股票计算全部指标，返回 {code: DataFrame}。"""
    out: dict[str, pd.DataFrame] = {}
    for code, df in dfs.items():
        close = df["close"]
        o = pd.DataFrame(index=df.index)
        o["close"] = close
        o["ema_fast"] = ind.ema(close, params.ma_fast)
        o["ema_slow"] = ind.ema(close, params.ma_slow)
        dif, dea, hist = ind.macd(
            close, params.macd_fast, params.macd_slow, params.macd_signal
        )
        o["dif"], o["dea"], o["hist"] = dif, dea, hist
        o["rsi"] = ind.rsi(close, params.rsi_n)
        o["atr"] = ind.atr(df["high"], df["low"], close, 14)
        mid, _, _ = ind.bollinger(close, params.bb_n, params.bb_k)
        o["bb_mid"] = mid
        o["mom20"] = ind.momentum(close, 20)
        o["mom60"] = ind.momentum(close, 60)
        o["vol_ratio"] = df["volume"] / df["volume"].rolling(params.vol_ratio_n).mean()
        o["mom_adj"] = o["mom20"] / close.pct_change().rolling(20).std()
        cross_up = (dif > dea) & (dif.shift(1) <= dea.shift(1))
        o["recent_cross"] = (
            cross_up.rolling(params.recent_cross_days, min_periods=1).max().fillna(0).astype(bool)
        )
        in_pool = (
            (o["ema_fast"] > o["ema_slow"])
            & (o["hist"] > 0)
            & o["rsi"].between(params.rsi_min, params.rsi_max)
            & (o["close"] > o["bb_mid"])
        )
        if params.require_recent_cross:
            in_pool &= o["recent_cross"]
        if params.require_volume:
            in_pool &= o["vol_ratio"] >= params.vol_ratio_min
        o["in_pool"] = in_pool
        out[code] = o
    return out


def market_ok(
    frames: dict[str, pd.DataFrame],
    date: pd.Timestamp,
    params: StrategyParams,
    sentiment: pd.DataFrame | None = None,
) -> bool:
    """市场择时：动量向上的股票占比达标才允许建仓。"""
    up20, up60 = [], []
    for f in frames.values():
        if date not in f.index:
            continue
        row = f.loc[date]
        if pd.isna(row["mom20"]) or pd.isna(row["mom60"]):
            continue
        up20.append(row["mom20"] > 0)
        up60.append(row["mom60"] > 0)
    if not up20:
        return False
    if not (np.mean(up20) >= params.breadth_up20 and np.mean(up60) >= params.breadth_up60):
        return False
    if params.require_board_breadth:
        if sentiment is None or date not in sentiment.index:
            return False
        val = sentiment.loc[date, "board_breadth"]
        if pd.isna(val) or float(val) < params.board_breadth_min:
            return False
    if params.require_lhb_positive:
        if sentiment is None or "lhb_net" not in sentiment or date not in sentiment.index:
            return False
        val = sentiment.loc[date, "lhb_net"]
        if pd.isna(val) or float(val) <= 0:
            return False
    if params.require_index_trend:
        if sentiment is None or "index_close" not in sentiment or date not in sentiment.index:
            return False
        val = sentiment.loc[date, "index_close"]
        ma = sentiment["index_close"].loc[:date].iloc[-params.index_ma:].mean()
        if pd.isna(val) or pd.isna(ma) or float(val) <= float(ma):
            return False
    return True


def score_candidates(
    frames: dict[str, pd.DataFrame], date: pd.Timestamp, params: StrategyParams
) -> pd.DataFrame:
    """对当日候选股按排名打分，返回按分数降序的 DataFrame（含 top_n 截断）。"""
    rows = []
    for code, f in frames.items():
        if date not in f.index:
            continue
        row = f.loc[date]
        if not bool(row["in_pool"]):
            continue
        rows.append(
            {
                "code": code,
                "close": row["close"],
                "hist": row["hist"],
                "mom20": row["mom20"],
                "mom_adj": row["mom_adj"],
                "rsi": row["rsi"],
            }
        )
    if not rows:
        return pd.DataFrame(columns=["code", "close", "score"])
    df = pd.DataFrame(rows)
    mom_col = "mom_adj" if params.use_vol_adj_mom else "mom20"
    df = df.dropna(subset=[mom_col, "hist", "rsi"])
    df["hist_pct"] = df["hist"].rank(pct=True)
    df["mom_pct"] = df[mom_col].rank(pct=True)
    df["rsi_pct"] = df["rsi"].rank(pct=True)
    df["score"] = (
        params.score_hist * df["hist_pct"]
        + params.score_mom * df["mom_pct"]
        + params.score_rsi * df["rsi_pct"]
    )
    df = df.sort_values("score", ascending=False).head(params.top_n)
    return df[["code", "close", "score"]]


def build_signal_matrix(
    frames: dict[str, pd.DataFrame],
    dates: list[pd.Timestamp],
    params: StrategyParams,
) -> dict[str, pd.DataFrame]:
    """把逐股指标转成「日期 × 股票」矩阵，批量选股用（800 只股票规模下的性能优化）。"""
    cols = ["close", "hist", "mom20", "mom60", "rsi", "mom_adj", "in_pool"]
    index = pd.DatetimeIndex(dates)
    mats: dict[str, pd.DataFrame] = {}
    for col in cols:
        mats[col] = pd.DataFrame({code: f[col] for code, f in frames.items()}).reindex(index)
    return mats


def market_ok_series(
    mats: dict[str, pd.DataFrame],
    params: StrategyParams,
    sentiment: pd.DataFrame | None = None,
) -> pd.Series:
    """市场择时（向量化）：动量广度 + 可选情绪过滤，返回每个交易日的布尔序列。"""
    valid = mats["mom20"].notna() & mats["mom60"].notna()
    denom = valid.sum(axis=1)
    up20 = ((mats["mom20"] > 0) & valid).sum(axis=1) / denom
    up60 = ((mats["mom60"] > 0) & valid).sum(axis=1) / denom
    ok = (up20 >= params.breadth_up20) & (up60 >= params.breadth_up60)
    if params.require_board_breadth:
        if sentiment is None:
            return pd.Series(False, index=mats["close"].index)
        bb = sentiment["board_breadth"].reindex(mats["close"].index)
        ok = ok & (bb >= params.board_breadth_min)
    if params.require_lhb_positive:
        if sentiment is None or "lhb_net" not in sentiment:
            return pd.Series(False, index=mats["close"].index)
        lhb = sentiment["lhb_net"].reindex(mats["close"].index)
        ok = ok & (lhb > 0)
    if params.require_index_trend:
        if sentiment is None or "index_close" not in sentiment:
            return pd.Series(False, index=mats["close"].index)
        # 先在完整指数序列上算均线，再对齐窗口（避免窗口切片导致均线缺失）
        full = sentiment["index_close"]
        ma_full = full.rolling(params.index_ma, min_periods=params.index_ma).mean()
        idx = full.reindex(mats["close"].index)
        ma = ma_full.reindex(mats["close"].index)
        ok = ok & (idx > ma)
    return ok.fillna(False)


def select_targets(
    frames: dict[str, pd.DataFrame],
    dates: list[pd.Timestamp],
    params: StrategyParams,
    sentiment: pd.DataFrame | None = None,
) -> dict[pd.Timestamp, list[str]]:
    """逐日计算目标持仓（矩阵化）。市场择时不通过时返回空列表（空仓）。"""
    mats = build_signal_matrix(frames, dates, params)
    ok = market_ok_series(mats, params, sentiment)
    pool = mats["in_pool"]
    h = mats["hist"].where(pool).rank(axis=1, pct=True)
    mom_col = "mom_adj" if params.use_vol_adj_mom else "mom20"
    m = mats[mom_col].where(pool).rank(axis=1, pct=True)
    r = mats["rsi"].where(pool).rank(axis=1, pct=True)
    score = params.score_hist * h + params.score_mom * m + params.score_rsi * r

    targets: dict[pd.Timestamp, list[str]] = {}
    for d in dates:
        if not bool(ok.loc[d]):
            targets[d] = []
            continue
        row = score.loc[d].dropna()
        if row.empty:
            targets[d] = []
        else:
            targets[d] = row.nlargest(params.top_n).index.tolist()
    return targets


def latest_signal_snapshot(
    frames: dict[str, pd.DataFrame],
    names: dict[str, str],
    date: pd.Timestamp,
    params: StrategyParams,
) -> pd.DataFrame:
    """最新交易日的信号快照：所有候选股及其得分，便于人工核对。"""
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
                "ema20": round(float(row["ema_fast"]), 2),
                "ema60": round(float(row["ema_slow"]), 2),
                "macd_hist": round(float(row["hist"]), 4),
                "rsi": round(float(row["rsi"]), 1),
                "vol_ratio": round(float(row["vol_ratio"]), 2) if pd.notna(row["vol_ratio"]) else None,
                "mom20_pct": round(float(row["mom20"]) * 100, 2) if pd.notna(row["mom20"]) else None,
                "mom_adj": round(float(row["mom_adj"]), 3) if pd.notna(row["mom_adj"]) else None,
                "mom60_pct": round(float(row["mom60"]) * 100, 2) if pd.notna(row["mom60"]) else None,
                "recent_cross": bool(row["recent_cross"]),
                "in_pool": bool(row["in_pool"]),
            }
        )
    return pd.DataFrame(rows).sort_values("in_pool", ascending=False)
