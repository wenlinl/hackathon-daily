#!/usr/bin/env python3
"""短期反转因子滚动多折 walk-forward 验证。

每折：3 年训练段小网格（回看天数 × 持有只数）选优，6 个月测试段样本外评估。
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
from datetime import datetime, timedelta

import pandas as pd

from src import backtest, data, report, strategy_rev

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
    rvp: strategy_rev.ReversalParams,
    bp: backtest.BacktestParams,
    index_data: pd.DataFrame | None = None,
) -> dict:
    dfs_w = {c: df.loc[start:end] for c, df in dfs.items() if len(df.loc[start:end])}
    frames_w = {
        c: f.loc[start:end]
        for c, f in frames_all.items()
        if c in dfs_w and len(f.loc[start:end])
    }
    dates = sorted({d for f in frames_w.values() for d in f.index})
    targets = strategy_rev.select_rev_targets(frames_w, dates, rvp)
    res = backtest.run_backtest(dfs_w, frames_w, targets, bp, index_data)
    return report.compute_metrics(
        res["nav"]["nav"], res["trades"], res["total_turnover"], bp.initial_capital
    )


def main() -> int:
    p = argparse.ArgumentParser(description="短期反转因子滚动 walk-forward 验证")
    p.add_argument("--universe", default=os.path.join(HERE, "config", "universe_csi.json"))
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--train-years", type=int, default=3)
    p.add_argument("--test-months", type=int, default=6)
    p.add_argument("--step-months", type=int, default=6)
    p.add_argument("--rebalance", type=int, default=20, help="轮换周期（交易日）")
    p.add_argument("--vol-target", action="store_true", help="波动率目标仓位")
    p.add_argument("--target-vol", type=float, default=0.20)
    p.add_argument("--crash-filter", action="store_true", help="市场大跌过滤")
    p.add_argument("--crash-mom20", type=float, default=-0.05)
    p.add_argument("--vol-weight", action="store_true", help="波动率倒数加权")
    p.add_argument("--max-weight", type=float, default=0.0, help="单票仓位上限（0=不限）")
    p.add_argument("--max-industry", type=int, default=0, help="每行业最多持有数（0=不限）")
    p.add_argument("--min-amount", type=float, default=0.0, help="20 日均成交额下限（元，0=不限）")
    p.add_argument("--output", default=os.path.join(HERE, "output", "walkforward_rev"))
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

    meta = (
        data.fetch_stock_meta(os.path.join(HERE, "output", "cache_meta"))
        if args.max_industry > 0
        else {}
    )
    if not meta and args.max_industry > 0:
        print("  提示：股票元数据获取失败，行业分散/ST 过滤未生效")

    index_data = None
    if args.vol_target or args.crash_filter:
        idx_close = data.fetch_index(
            "000300", start, end, os.path.join(HERE, "output", "cache_index")
        )
        index_data = strategy_rev.build_index_filters(idx_close)

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

    grid = list(itertools.product([5, 10], [20, 30]))  # ret_days × bottom_n
    rows = []
    for fold in folds:
        print(f"\n=== 折叠 {fold['test_start']} → {fold['test_end']} ===")
        best = None
        for ret_days, bottom_n in grid:
            rvp = strategy_rev.ReversalParams(
                ret_days=ret_days,
                bottom_n=bottom_n,
                min_amount=args.min_amount,
                max_industry=args.max_industry,
                meta=meta,
            )
            bp = backtest.BacktestParams(
                exit_mode="hold",
                rebalance_days=args.rebalance,
                use_vol_target=args.vol_target,
                target_vol=args.target_vol,
                use_crash_filter=args.crash_filter,
                crash_mom20=args.crash_mom20,
                vol_weight=args.vol_weight,
                max_weight=args.max_weight,
            )
            frames_all = strategy_rev.build_rev_frames(dfs, rvp)
            m_tr = run_window(dfs, frames_all, fold["train_start"], fold["train_end"], rvp, bp, index_data)
            m_te = run_window(dfs, frames_all, fold["test_start"], fold["test_end"], rvp, bp, index_data)
            tr_s = m_tr["sharpe"] if m_tr["sharpe"] is not None else -99
            if best is None or tr_s > best["train_sharpe"]:
                best = {
                    "ret_days": ret_days,
                    "bottom_n": bottom_n,
                    "train_sharpe": tr_s,
                    "train_annual": m_tr["annual_return"],
                    "test_annual": m_te["annual_return"],
                    "test_sharpe": m_te["sharpe"],
                    "test_dd": m_te["max_drawdown"],
                    "test_trades": m_te["n_trades"],
                }

        rvp_fix = strategy_rev.ReversalParams(
            min_amount=args.min_amount,
            max_industry=args.max_industry,
            meta=meta,
        )
        bp_fix = backtest.BacktestParams(
            exit_mode="hold",
            rebalance_days=args.rebalance,
            use_vol_target=args.vol_target,
            target_vol=args.target_vol,
            use_crash_filter=args.crash_filter,
            crash_mom20=args.crash_mom20,
            vol_weight=args.vol_weight,
            max_weight=args.max_weight,
        )
        frames_fix = strategy_rev.build_rev_frames(dfs, rvp_fix)
        m_te_fix = run_window(
            dfs, frames_fix, fold["test_start"], fold["test_end"], rvp_fix, bp_fix, index_data
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
            f"  最优: ret={best['ret_days']}d n={best['bottom_n']} "
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

    print("\n=== 汇总（短期反转因子） ===")
    for key in ("rolling_test_annual", "fixed_test_annual"):
        s = summary[key]
        print(f"{key}: 均值 {s['mean']:.2%} / 中位数 {s['median']:.2%} / 盈利折占比 {s['positive_ratio']:.0%}")
    print(f"rolling_test_sharpe: 均值 {summary['rolling_test_sharpe']['mean']:.2f}")
    print(f"fixed_test_sharpe:   均值 {summary['fixed_test_sharpe']['mean']:.2f}")
    print(f"\n结果写入 {args.output}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
