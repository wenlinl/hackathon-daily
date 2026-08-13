#!/usr/bin/env python3
"""Walk-forward 样本外验证。

训练段（2022-01-01 ~ 2024-12-31）做小规模参数网格搜索，选出训练段最优参数；
测试段（2025-01-01 起）完全用训练段选出的参数运行，检验是否过拟合。

同时报告「当前默认参数」（此前在全样本上调出）在训练段/测试段的表现作对照。

用法:
    python3 validate.py
    python3 validate.py --train-start 2022-01-01 --train-end 2024-12-31 --test-start 2025-01-01
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
from datetime import datetime

import pandas as pd

from src import backtest, data, report, sentiment, strategy

HERE = os.path.dirname(os.path.abspath(__file__))


def run_window(
    dfs: dict[str, pd.DataFrame],
    frames_all: dict[str, pd.DataFrame],
    start: str,
    end: str,
    sp: strategy.StrategyParams,
    bp: backtest.BacktestParams,
    senti: pd.DataFrame | None = None,
):
    """在指定日期窗口内跑一遍回测，返回 (指标, 结果)。"""
    dfs_w = {c: df.loc[start:end] for c, df in dfs.items() if len(df.loc[start:end])}
    frames_w = {
        c: f.loc[start:end] for c, f in frames_all.items() if c in dfs_w and len(f.loc[start:end])
    }
    dates = sorted({d for f in frames_w.values() for d in f.index})
    targets = strategy.select_targets(frames_w, dates, sp, senti)
    res = backtest.run_backtest(dfs_w, frames_w, targets, bp)
    metrics = report.compute_metrics(
        res["nav"]["nav"], res["trades"], res["total_turnover"], bp.initial_capital
    )
    return metrics, res


def config_label(sp: strategy.StrategyParams, bp: backtest.BacktestParams) -> str:
    return (
        f"breadth={sp.breadth_up20:.2f}/{sp.breadth_up60:.2f} "
        f"cross={sp.recent_cross_days} rsi={sp.rsi_min:.0f}-{sp.rsi_max:.0f} "
        f"trail={bp.trail_atr_mult:.1f} reb={bp.rebalance_days}"
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Walk-forward 样本外验证")
    p.add_argument("--universe", default=os.path.join(HERE, "config", "universe.json"))
    p.add_argument("--train-start", default="2022-01-01")
    p.add_argument("--train-end", default="2024-12-31")
    p.add_argument("--test-start", default="2025-01-01")
    p.add_argument("--test-end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--output", default=os.path.join(HERE, "output", "walkforward"))
    p.add_argument("--no-sentiment", action="store_true", help="跳过市场情绪特征对比")
    args = p.parse_args()

    codes = data.load_universe(args.universe)
    start = datetime.strptime(args.train_start, "%Y-%m-%d")
    end = datetime.strptime(args.test_end, "%Y-%m-%d")
    dfs = data.download_universe(
        codes, start, end,
        cache_dir=os.path.join(args.output, "..", "cache"),
        use_cache=True,
    )
    if not dfs:
        print("数据加载失败")
        return 1

    sentiment_data = None
    if not args.no_sentiment:
        print("加载市场情绪数据（板块强度 + 龙虎榜）…")
        cache_dir = os.path.join(args.output, "..", "sentiment_cache")
        codes = sentiment.fetch_board_codes()
        closes = sentiment.fetch_board_closes(codes, start, end, cache_dir)
        lhb = sentiment.fetch_lhb_net(start, end, cache_dir)
        sentiment_data = sentiment.build_sentiment(closes)
        sentiment_data = sentiment_data.join(lhb.rename("lhb_net"), how="left")

    def run_pair(
        sp: strategy.StrategyParams,
        bp: backtest.BacktestParams,
        senti: pd.DataFrame | None = None,
    ):
        frames_all = strategy.build_indicator_frames(dfs, sp)
        m_tr, _ = run_window(dfs, frames_all, args.train_start, args.train_end, sp, bp, senti)
        m_te, res_te = run_window(dfs, frames_all, args.test_start, args.test_end, sp, bp, senti)
        return m_tr, m_te, res_te

    rows = []

    # 1) 当前默认参数（此前在全样本上调出）作对照
    sp_def = strategy.StrategyParams()
    bp_def = backtest.BacktestParams()
    m_tr, m_te, res_def = run_pair(sp_def, bp_def)
    rows.append(
        {
            "label": "当前默认(训练段选优)",
            "kind": "reference",
            **{f"train_{k}": v for k, v in m_tr.items()},
            **{f"test_{k}": v for k, v in m_te.items()},
        }
    )
    print(f"对照(当前默认): 训练段 年化={m_tr['annual_return']:.2%} 夏普={m_tr['sharpe']:.2f} | "
          f"测试段 年化={m_te['annual_return']:.2%} 夏普={m_te['sharpe']:.2f} 回撤={m_te['max_drawdown']:.2%}")

    # 2) 训练段网格搜索
    best = None
    grid = list(
        itertools.product(
            [0.35, 0.50],  # breadth20
            [0.25],        # breadth60
            [10, 20],       # cross_window
            [2.0, 2.5],     # trail_atr
            [5, 10],        # rebalance_days
            [False, True],  # require_volume
        )
    )
    for b20, b60, cw, trail, reb, vol in grid:
        sp = strategy.StrategyParams(
            breadth_up20=b20, breadth_up60=b60, recent_cross_days=cw, require_volume=vol
        )
        bp = backtest.BacktestParams(trail_atr_mult=trail, rebalance_days=reb)
        m_tr, m_te, _ = run_pair(sp, bp)
        row = {
            "label": config_label(sp, bp) + (" vol=1.0" if vol else ""),
            "kind": "grid",
            **{f"train_{k}": v for k, v in m_tr.items()},
            **{f"test_{k}": v for k, v in m_te.items()},
        }
        rows.append(row)
        train_sharpe = m_tr["sharpe"] if m_tr["sharpe"] is not None else -99
        if best is None or train_sharpe > best["train_sharpe"]:
            best = row
            best["train_sharpe"] = train_sharpe
            best["sp"] = sp
            best["bp"] = bp

    print(f"\n训练段最优: {best['label']}")
    print(f"  训练段: 年化={best['train_annual_return']:.2%} 回撤={best['train_max_drawdown']:.2%} 夏普={best['train_sharpe']:.2f}")
    print(f"  测试段(样本外): 年化={best['test_annual_return']:.2%} 回撤={best['test_max_drawdown']:.2%} "
          f"夏普={best['test_sharpe']:.2f} 交易={best['test_n_trades']} 胜率={best['test_win_rate']:.1%}")

    # 3) 市场情绪特征对比（基于当前默认参数，测试段仍是样本外）
    if sentiment_data is not None:
        print("\n=== 市场情绪特征对比（当前默认参数 + 情绪过滤） ===")
        variants = [
            ("无情绪过滤", {}),
            ("板块广度≥0.50", {"require_board_breadth": True}),
            ("板块广度≥0.60", {"require_board_breadth": True, "board_breadth_min": 0.60}),
            ("龙虎榜净买>0", {"require_lhb_positive": True}),
            ("板块+龙虎榜", {"require_board_breadth": True, "require_lhb_positive": True}),
        ]
        for label, kw in variants:
            spv = strategy.StrategyParams(**kw)
            bpv = backtest.BacktestParams()
            m_tr, m_te, _ = run_pair(spv, bpv, sentiment_data)
            rows.append(
                {
                    "label": f"情绪-{label}",
                    "kind": "sentiment",
                    **{f"train_{k}": v for k, v in m_tr.items()},
                    **{f"test_{k}": v for k, v in m_te.items()},
                }
            )
            tr_s = m_tr["sharpe"] if m_tr["sharpe"] is not None else 0.0
            te_s = m_te["sharpe"] if m_te["sharpe"] is not None else 0.0
            print(f"{label:14s} 训练 年化={m_tr['annual_return']:.2%} 夏普={tr_s:.2f} | "
                  f"测试 年化={m_te['annual_return']:.2%} 夏普={te_s:.2f} 回撤={m_te['max_drawdown']:.2%}")

    # 4) 输出
    os.makedirs(args.output, exist_ok=True)
    cmp = pd.DataFrame(rows).drop(columns=["sp", "bp"], errors="ignore")
    cmp.to_csv(os.path.join(args.output, "comparison.csv"), index=False)
    best_cfg = {
        "strategy": vars(best["sp"]),
        "backtest": vars(best["bp"]),
        "train_metrics": {k: v for k, v in best.items() if k.startswith("train_") and k != "train_sharpe"},
        "test_metrics": {k: v for k, v in best.items() if k.startswith("test_")},
    }
    with open(os.path.join(args.output, "best_config.json"), "w", encoding="utf-8") as f:
        json.dump(best_cfg, f, ensure_ascii=False, indent=2, default=str)
    res_def["nav"].to_csv(os.path.join(args.output, "reference_test_nav.csv"))
    if len(res_def["trades"]):
        res_def["trades"].to_csv(os.path.join(args.output, "reference_test_trades.csv"), index=False)
    print(f"\n结果已写入 {args.output}/ (comparison.csv, best_config.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
