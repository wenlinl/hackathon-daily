"""技术指标计算（全部为向量化 pandas 实现）。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    """简单移动平均。"""
    return series.rolling(n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    """指数移动平均。"""
    return series.ewm(span=n, adjust=False).mean()


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD：返回 (DIF, DEA, 柱状图)。柱状图按国内软件惯例取 2*(DIF-DEA)。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = 2.0 * (dif - dea)
    return dif, dea, hist


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """RSI（Wilder 平滑近似）。"""
    diff = close.diff()
    gain = diff.clip(lower=0.0)
    loss = (-diff).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False).mean()
    out = pd.Series(np.nan, index=close.index, dtype=float)
    valid = avg_loss != 0
    out[valid] = 100.0 - 100.0 / (1.0 + avg_gain[valid] / avg_loss[valid])
    out[~valid & avg_gain.notna()] = 100.0
    return out.ffill()


def bollinger(
    close: pd.Series, n: int = 20, k: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """布林带：返回 (中轨, 上轨, 下轨)。"""
    mid = close.rolling(n).mean()
    std = close.rolling(n).std(ddof=0)
    return mid, mid + k * std, mid - k * std


def momentum(close: pd.Series, n: int) -> pd.Series:
    """n 日动量（收益率）。"""
    return close.pct_change(n)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """平均真实波幅 ATR（Wilder 平滑）。"""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()
