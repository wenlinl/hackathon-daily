#!/usr/bin/env python3
"""均值回归策略滚动多折 walk-forward 验证（与动量版对比）。

每折：3 年训练段小网格（超卖阈值 × 最长持有）选优，6 个月测试段样本外评估；
同时报告固定默认参数的测试段表现。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
from datetime import datetime, timedelta

import pandas as pd

from src import backtest, data, report, strategy_mr

HERE = os.path.dirname(os.path.abspath(__file__))


def add_months(d: datetime, months: int) -> datetime:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return datetime(y, m, 1)


def run_window(
    dfs: dict[str, pd.DataFrame],
    frames_all: dict[str, pd.DataFrame],
    start: str,
    end: str,
    mrp: strategy_mr.MeanReversionParams,
    bp: backtest.BacktestParams,
) -> dict:
    dfs_w = {c: df.loc[start:end] for c, df in dfs.items() if len(df.loc[start:end])}
    frames_w = {
        c: f.loc[start:end]
        for c, f in frames_all.items()
        if c in dfs_w and len(f.loc[start:end])
    }
    dates = sorted({d for f in frames_w.values() for d in f.index})
    targets = strategy_mr.select_mr_targets(frames_w, dates, mrp)
    res = backtest.run_backtest(dfs_w, frames_w, targets, bp)
    return report.compute_metrics(
        res["nav"]["nav"], res["trades"], res["total_turnover"], bp.initial_capital
    )


def main() -> int:
    p = argparse.ArgumentParser(description="均值回归滚动 walk-forward 验证")
    p.add_argument("--universe", default=os.path.join(HERE, "config", "universe_csi.json"))
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--train-years", type=int, default=3)
    p.add_argument("--test-months", type=int, default=6)
    p.add_argument("--step-months", type=int, default=6)
    p.add_argument("--output", default=os.path.join(HERE, "output", "walkforward_mr"))
    args = p.parse_args()

    codes = data.load_universe(args.universe)
    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")
    print(f"股票池 {len(codes)} 只，数据 {args.start} ~ {args.end}")
    dfs = data.download_universe(
        codes, start, end,
        cache_dir=os.path.join(HERE, "output", "cache"),
        use_cache=True,
        workers=1,
        source="baostock",
    )
    if not dfs:
        print("数据加载失败")
        return 1
    print(f"成功加载 {len(dfs)} 只")

    folds = []
    test_start = datetime(2022, 1, 1)
    while test_start < end:
        train_start = test_start.replace(year=test_start.year - args.train_years)
        train_end = test_start - timedelta(days=1)
        test_end = add_months(test_start, args.test_months) - timedelta(days=1)
        folds.append(
            {
                "train_start": train_start.strftime("%Y-%m-%d"),
                "train_end": train_end.strftime("%Y-%m-%d"),
                "test_start": test_start.strftime("%Y-%m-%d"),
                "test_end": min(test_end, end).strftime("%Y-%m-%d"),
            }
        )
        test_start = add_months(test_start, args.step_months)

    grid = list(itertools.product([25.0, 30.0], [5, 10]))  # rsi_oversold × mr_max_hold
    rows = []
    for fold in folds:
        print(f"\n=== 折叠 {fold['test_start']} → {fold['test_end']} ===")
        best = None
        for rsi_ov, max_hold in grid:
            mrp = strategy_mr.MeanReversionParams(rsi_oversold=rsi_ov)
            bp = backtest.BacktestParams(exit_mode="mean_rev", mr_max_hold=max_hold)
            frames_all = strategy_mr.build_mr_frames(dfs, mrp)
            m_tr = run_window(dfs, frames_all, fold["train_start"], fold["train_end"], mrp, bp)
            m_te = run_window(dfs, frames_all, fold["test_start"], fold["test_end"], mrp, bp)
            tr_s = m_tr["sharpe"] if m_tr["sharpe"] is not None else -99
            if best is None or tr_s > best["train_sharpe"]:
                best = {
                    "rsi_oversold": rsi_ov,
                    "max_hold": max_hold,
                    "train_sharpe": tr_s,
                    "train_annual": m_tr["annual_return"],
                    "test_annual": m_te["annual_return"],
                    "test_sharpe": m_te["sharpe"],
                    "test_dd": m_te["max_drawdown"],
                    "test_trades": m_te["n_trades"],
                }

        mrp_fix = strategy_mr.MeanReversionParams()
        bp_fix = backtest.BacktestParams(exit_mode="mean_rev")
        frames_fix = strategy_mr.build_mr_frames(dfs, mrp_fix)
        m_te_fix = run_window(
            dfs, frames_fix, fold["test_start"], fold["test_end"], mrp_fix, bp_fix
        )
        rows.append(
            {
                "test_start": fold["test_start"],
                "test_end": fold["test_end"],
                **best,
                "fix_test_annual": m_te_fix["annual_return"],
                "fix_test_sharpe": m_te_fix["sharpe"],
                "fix_test_dd": m_te_fix["max_drawdown"],
            }
        )
        print(
            f"  最优: rsi<{best['rsi_oversold']:.0f} hold={best['max_hold']} "
            f"(训练夏普 {best['train_sharpe']:.2f}) | "
            f"测试 年化={best['test_annual']:.2%} "
            f"夏普={best['test_sharpe'] if best['test_sharpe'] is not None else 0:.2f} "
            f"回撤={best['test_dd']:.2%}"
        )
        print(
            f"  固定默认: 测试 年化={m_te_fix['annual_return']:.2%} "
            f"夏普={m_te_fix['sharpe'] if m_te_fix['sharpe'] is not None else 0:.2f}"
        )

    df = pd.DataFrame(rows)
    os.makedirs(args.output, exist_ok=True)
    df.to_csv(os.path.join(args.output, "folds.csv"), index=False)

    def summarize(col: str):
        s = df[col].dropna()
        return {
            "mean": float(s.mean()),
            "median": float(s.median()),
            "positive_ratio": float((s > 0).mean()),
            "min": float(s.min()),
            "max": float(s.max()),
        }

    summary = {
        "n_folds": len(df),
        "rolling_test_annual": summarize("test_annual"),
        "rolling_test_sharpe": summarize("test_sharpe"),
        "rolling_test_dd": summarize("test_dd"),
        "fixed_test_annual": summarize("fix_test_annual"),
        "fixed_test_sharpe": summarize("fix_test_sharpe"),
    }
    with open(os.path.join(args.output, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n=== 汇总（均值回归） ===")
    for key in ("rolling_test_annual", "fixed_test_annual"):
        s = summary[key]
        print(f"{key}: 均值 {s['mean']:.2%} / 中位数 {s['median']:.2%} / 盈利折占比 {s['positive_ratio']:.0%}")
    print(f"rolling_test_sharpe: 均值 {summary['rolling_test_sharpe']['mean']:.2f}")
    print(f"fixed_test_sharpe:   均值 {summary['fixed_test_sharpe']['mean']:.2f}")
    print(f"\n结果写入 {args.output}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
