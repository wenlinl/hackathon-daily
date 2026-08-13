"""均值回归策略：超跌反转入场，回归中轨 / RSI 回升出场。

入场（T 日收盘判断，T+1 开盘买入）：
- 前一日处于超跌状态：RSI < 阈值 或 收盘价跌破布林带下轨；
- 当日出现反转确认：阳线（收盘 > 开盘）。

出场（收盘判断）：
- 硬止损（防接飞刀）、目标价止盈、RSI 回升到阈值或站上布林带中轨、最长持有天数。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from . import indicators as ind


@dataclass
class MeanReversionParams:
    rsi_n: int = 14
    rsi_oversold: float = 30.0
    bb_n: int = 20
    bb_k: float = 2.0
    vol_ratio_min: float = 0.0  # 放量确认（默认关闭）
    top_n: int = 10


def build_mr_frames(
    dfs: dict[str, pd.DataFrame], params: MeanReversionParams
) -> dict[str, pd.DataFrame]:
    """为每只股票计算均值回归信号，返回 {code: DataFrame}。"""
    out: dict[str, pd.DataFrame] = {}
    for code, df in dfs.items():
        close = df["close"]
        o = pd.DataFrame(index=df.index)
        o["close"] = close
        o["open"] = df["open"]
        o["volume"] = df["volume"]
        o["rsi"] = ind.rsi(close, params.rsi_n)
        mid, _, lower = ind.bollinger(close, params.bb_n, params.bb_k)
        o["bb_mid"] = mid
        o["bb_lower"] = lower
        o["oversold"] = (o["rsi"] < params.rsi_oversold) | (close < o["bb_lower"])
        o["bullish"] = close > df["open"]
        signal = o["oversold"].shift(1) & o["bullish"]
        if params.vol_ratio_min > 0:
            vol_ratio = df["volume"] / df["volume"].rolling(5).mean()
            signal = signal & (vol_ratio >= params.vol_ratio_min)
        o["signal"] = signal
        out[code] = o
    return out


def select_mr_targets(
    frames: dict[str, pd.DataFrame],
    dates: list[pd.Timestamp],
    params: MeanReversionParams,
) -> dict[pd.Timestamp, list[str]]:
    """逐日选目标：有反转信号的股票按 RSI 升序（越超跌越优先），取前 top_n。"""
    sig = pd.DataFrame({code: f["signal"] for code, f in frames.items()}).reindex(dates)
    rsi = pd.DataFrame({code: f["rsi"] for code, f in frames.items()}).reindex(dates)
    targets: dict[pd.Timestamp, list[str]] = {}
    for d in dates:
        row = sig.loc[d].fillna(False).astype(bool)
        codes = row.index[row].tolist()
        if not codes:
            targets[d] = []
            continue
        r = rsi.loc[d, codes]
        targets[d] = r.sort_values().head(params.top_n).index.tolist()
    return targets


def latest_mr_snapshot(
    frames: dict[str, pd.DataFrame],
    names: dict[str, str],
    date: pd.Timestamp,
    params: MeanReversionParams,
) -> pd.DataFrame:
    """最新交易日信号快照。"""
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
                "rsi": round(float(row["rsi"]), 1),
                "bb_lower": round(float(row["bb_lower"]), 2),
                "oversold": bool(row["oversold"]),
                "signal": bool(row["signal"]),
            }
        )
    return pd.DataFrame(rows).sort_values("signal", ascending=False)
