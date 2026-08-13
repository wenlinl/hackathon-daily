"""绩效报告：指标计算、净值曲线图、文件输出。"""

from __future__ import annotations

import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

PERIODS = 252


def compute_metrics(nav: pd.Series, trades: pd.DataFrame, total_turnover: float, capital: float) -> dict:
    """核心绩效指标。"""
    ret = nav.pct_change().dropna()
    total_return = nav.iloc[-1] / nav.iloc[0] - 1
    years = max(len(nav) / PERIODS, 1e-9)
    annual_return = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1
    vol = ret.std() * np.sqrt(PERIODS) if len(ret) else np.nan
    sharpe = (
        (ret.mean() / ret.std()) * np.sqrt(PERIODS)
        if len(ret) and ret.std() > 0
        else np.nan
    )
    dd = (nav / nav.cummax() - 1).min()
    calmar = annual_return / abs(dd) if dd < 0 else np.nan

    win_rate = None
    profit_factor = None
    avg_hold = None
    if len(trades):
        wins = trades[trades["pnl"] > 0]
        losses = trades[trades["pnl"] < 0]
        win_rate = len(wins) / len(trades)
        gross_win = wins["pnl"].sum()
        gross_loss = -losses["pnl"].sum()
        profit_factor = gross_win / gross_loss if gross_loss > 0 else np.nan
        avg_hold = float(trades["hold_days"].mean())

    return {
        "periods": len(nav),
        "total_return": float(total_return),
        "annual_return": float(annual_return),
        "annual_volatility": float(vol) if vol == vol else None,
        "sharpe": float(sharpe) if sharpe == sharpe else None,
        "max_drawdown": float(dd),
        "calmar": float(calmar) if calmar == calmar else None,
        "n_trades": int(len(trades)),
        "win_rate": float(win_rate) if win_rate is not None else None,
        "profit_factor": float(profit_factor) if profit_factor is not None else None,
        "avg_hold_days": avg_hold,
        "total_turnover": float(total_turnover),
        "turnover_ratio": float(total_turnover) / capital,
    }


def plot_equity_curve(
    nav: pd.Series, benchmark: pd.Series | None, out_path: str
) -> None:
    """净值曲线 + 回撤子图（英文标签避免中文字体问题）。"""
    fig, axes = plt.subplots(
        2, 1, figsize=(11, 7), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )
    strat = nav / nav.iloc[0]
    axes[0].plot(strat.index, strat, label="Strategy", lw=1.5)
    if benchmark is not None and len(benchmark):
        bench = benchmark / benchmark.iloc[0]
        bench = bench.reindex(strat.index).ffill()
        axes[0].plot(bench.index, bench, label="CSI 300", lw=1.2, alpha=0.7)
    axes[0].set_ylabel("Normalized NAV")
    axes[0].legend()
    axes[0].grid(alpha=0.3)
    axes[0].set_title("Quant Strategy Backtest")

    dd = nav / nav.cummax() - 1
    axes[1].fill_between(dd.index, dd * 100, 0, color="red", alpha=0.4)
    axes[1].set_ylabel("Drawdown (%)")
    axes[1].grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def write_report(out_dir: str, metrics: dict, params: dict, nav: pd.DataFrame, trades: pd.DataFrame, signals: pd.DataFrame) -> None:
    os.makedirs(out_dir, exist_ok=True)
    nav.to_csv(os.path.join(out_dir, "nav.csv"))
    if len(trades):
        trades.to_csv(os.path.join(out_dir, "trades.csv"), index=False)
    if len(signals):
        signals.to_csv(os.path.join(out_dir, "latest_signals.csv"), index=False)
    payload = {"metrics": metrics, "params": params}
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
