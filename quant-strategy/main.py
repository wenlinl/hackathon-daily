#!/usr/bin/env python3
"""量化策略骨架入口：数据下载 → 指标 → 信号 → 回测 → 报告。

用法:
    python3 main.py
    python3 main.py --start 2021-01-01 --end 2026-08-12 --top-n 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import pandas as pd

from src import backtest, data, report, sentiment, strategy, strategy_mr, strategy_rev

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="A股日线量化策略骨架")
    p.add_argument("--strategy", choices=["momentum", "meanrev", "reversal"], default="momentum",
                   help="策略类型：momentum=多指标动量 / meanrev=均值回归 / reversal=短期反转因子")
    p.add_argument(
        "--universe",
        default=os.path.join(HERE, "config", "universe_csi.json"),
        help="股票池配置（默认沪深300+中证500 全成分；快速演示可用 config/universe.json）",
    )
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--rebalance", type=int, default=5)
    p.add_argument("--stop-loss", type=float, default=0.10)
    p.add_argument("--trail-atr", type=float, default=2.5, help="ATR 跟踪止损倍数")
    p.add_argument("--max-hold", type=int, default=120)
    p.add_argument("--cross-window", type=int, default=10, help="MACD 金叉后多少天内算新鲜信号")
    p.add_argument("--rsi-min", type=float, default=35.0)
    p.add_argument("--rsi-max", type=float, default=75.0)
    p.add_argument("--breadth20", type=float, default=0.50, help="20 日动量多头比例阈值")
    p.add_argument("--breadth60", type=float, default=0.25, help="60 日动量多头比例阈值")
    p.add_argument("--require-volume", action="store_true", help="要求金叉时放量（量比≥阈值）")
    p.add_argument("--vol-ratio-min", type=float, default=1.0, help="量比阈值")
    p.add_argument("--vol-adj-mom", action="store_true", help="打分改用波动率调整后的动量（默认使用原始 20 日动量）")
    p.add_argument("--require-board-breadth", action="store_true", help="要求板块赚钱效应达标（情绪过滤）")
    p.add_argument("--board-breadth-min", type=float, default=0.50, help="板块 20 日动量向上占比阈值")
    p.add_argument("--board-mom-days", type=int, default=20, help="板块动量周期")
    p.add_argument("--require-lhb-positive", action="store_true", help="要求龙虎榜当日净买额为正（情绪过滤）")
    p.add_argument("--require-index-trend", action="store_true", help="要求沪深300 站上 200 日线（强趋势闸门）")
    p.add_argument("--index-ma", type=int, default=200, help="指数趋势均线周期")
    p.add_argument("--mr-stop", type=float, default=0.08, help="均值回归硬止损")
    p.add_argument("--mr-target", type=float, default=0.10, help="均值回归止盈")
    p.add_argument("--mr-exit-rsi", type=float, default=55.0, help="均值回归 RSI 出场阈值")
    p.add_argument("--mr-max-hold", type=int, default=10, help="均值回归最长持有天数")
    p.add_argument("--mr-rsi-oversold", type=float, default=30.0, help="均值回归超卖阈值")
    p.add_argument("--rev-days", type=int, default=5, help="反转因子回看天数")
    p.add_argument("--bottom-n", type=int, default=30, help="反转因子持有最弱股票数量")
    p.add_argument("--vol-target", action="store_true", help="波动率目标仓位（按指数 20 日波动缩放买入）")
    p.add_argument("--target-vol", type=float, default=0.20, help="目标年化波动率")
    p.add_argument("--crash-filter", action="store_true", help="市场大跌过滤（指数 20 日动量低于阈值时清仓避险）")
    p.add_argument("--crash-mom20", type=float, default=-0.05, help="大跌过滤阈值（20 日动量）")
    p.add_argument("--vol-weight", action="store_true", help="波动率倒数加权（默认关闭）")
    p.add_argument("--max-weight", type=float, default=0.0, help="单票仓位上限（0=不限）")
    p.add_argument("--max-industry", type=int, default=0, help="每行业最多持有数（0=不限）")
    p.add_argument("--min-amount", type=float, default=0.0, help="20 日均成交额下限（元，0=不限）")
    p.add_argument("--capital", type=float, default=1_000_000.0)
    p.add_argument("--output", default=os.path.join(HERE, "output"))
    p.add_argument("--no-cache", action="store_true", help="忽略本地缓存重新下载")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    codes = data.load_universe(args.universe)
    names = {c["code"]: c.get("name", c["code"]) for c in codes}
    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")

    print(f"股票池：{len(codes)} 只；区间：{args.start} ~ {args.end}")
    dfs = data.download_universe(
        codes, start, end,
        cache_dir=os.path.join(HERE, "output", "cache"),
        use_cache=not args.no_cache,
    )
    if not dfs:
        print("数据下载失败，请检查网络后重试。")
        return 1
    print(f"成功获取 {len(dfs)} 只股票行情")

    sentiment_data = None
    if args.require_board_breadth or args.require_lhb_positive or args.require_index_trend:
        print("加载市场情绪/趋势数据…")
        cache_dir = os.path.join(HERE, "output", "sentiment_cache")
        parts = []
        if args.require_board_breadth:
            codes = sentiment.fetch_board_codes()
            closes = sentiment.fetch_board_closes(codes, start, end, cache_dir)
            parts.append(sentiment.build_sentiment(closes, args.board_mom_days))
        if args.require_lhb_positive:
            lhb = sentiment.fetch_lhb_net(start, end, cache_dir)
            parts.append(lhb.rename("lhb_net"))
        if args.require_index_trend:
            idx_close = data.fetch_index(
                "000300", start, end, os.path.join(HERE, "output", "cache_index")
            )
            parts.append(idx_close.rename("index_close"))
        sentiment_data = pd.concat(parts, axis=1, join="outer") if parts else None
        if sentiment_data is not None and len(sentiment_data):
            last = sentiment_data.dropna().index[-1]
            print(f"  情绪/趋势数据覆盖至 {last:%Y-%m-%d}")

    if args.strategy == "meanrev":
        sp = strategy_mr.MeanReversionParams(
            rsi_oversold=args.mr_rsi_oversold,
            vol_ratio_min=args.vol_ratio_min if args.require_volume else 0.0,
            top_n=args.top_n,
        )
        frames = strategy_mr.build_mr_frames(dfs, sp)
        bp = backtest.BacktestParams(
            initial_capital=args.capital,
            exit_mode="mean_rev",
            mr_stop=args.mr_stop,
            mr_target=args.mr_target,
            mr_exit_rsi=args.mr_exit_rsi,
            mr_max_hold=args.mr_max_hold,
        )
    elif args.strategy == "reversal":
        meta = (
            data.fetch_stock_meta(os.path.join(HERE, "output", "cache_meta"))
            if args.max_industry > 0
            else {}
        )
        if not meta and args.max_industry > 0:
            print("  提示：股票元数据获取失败，行业分散/ST 过滤未生效")
        sp = strategy_rev.ReversalParams(
            ret_days=args.rev_days,
            bottom_n=args.bottom_n,
            min_amount=args.min_amount,
            max_industry=args.max_industry,
            meta=meta,
        )
        frames = strategy_rev.build_rev_frames(dfs, sp)
        bp = backtest.BacktestParams(
            initial_capital=args.capital,
            exit_mode="hold",
            rebalance_days=args.rebalance,
            use_vol_target=args.vol_target,
            target_vol=args.target_vol,
            use_crash_filter=args.crash_filter,
            crash_mom20=args.crash_mom20,
            vol_weight=args.vol_weight,
            max_weight=args.max_weight,
        )
    else:
        sp = strategy.StrategyParams(
            top_n=args.top_n,
            recent_cross_days=args.cross_window,
            rsi_min=args.rsi_min,
            rsi_max=args.rsi_max,
            breadth_up20=args.breadth20,
            breadth_up60=args.breadth60,
            require_volume=args.require_volume,
            vol_ratio_min=args.vol_ratio_min,
            use_vol_adj_mom=args.vol_adj_mom,
            require_board_breadth=args.require_board_breadth,
            board_breadth_min=args.board_breadth_min,
            board_mom_days=args.board_mom_days,
            require_lhb_positive=args.require_lhb_positive,
            require_index_trend=args.require_index_trend,
            index_ma=args.index_ma,
        )
        frames = strategy.build_indicator_frames(dfs, sp)
        bp = backtest.BacktestParams(
            initial_capital=args.capital,
            stop_loss=args.stop_loss,
            trail_atr_mult=args.trail_atr,
            max_hold_days=args.max_hold,
            rebalance_days=args.rebalance,
        )
    dates = sorted({d for f in frames.values() for d in f.index})
    if args.strategy == "meanrev":
        targets = strategy_mr.select_mr_targets(frames, dates, sp)
    elif args.strategy == "reversal":
        targets = strategy_rev.select_rev_targets(frames, dates, sp)
    else:
        targets = strategy.select_targets(frames, dates, sp, sentiment_data)

    active_days = sum(1 for d in dates if targets[d])
    print(f"有目标天数：{active_days}/{len(dates)} ({active_days / max(len(dates), 1):.1%})")
    index_data = None
    if args.strategy == "reversal" and (args.vol_target or args.crash_filter):
        idx_close = data.fetch_index(
            "000300", start, end, os.path.join(HERE, "output", "cache_index")
        )
        index_data = strategy_rev.build_index_filters(idx_close)
    result = backtest.run_backtest(dfs, frames, targets, bp, index_data)
    nav, trades = result["nav"], result["trades"]

    # 基准：沪深300
    benchmark = data.fetch_index("000300", start, end, os.path.join(HERE, "output", "cache_index"))

    metrics = report.compute_metrics(nav["nav"], trades, result["total_turnover"], args.capital)
    if args.strategy == "meanrev":
        signals = strategy_mr.latest_mr_snapshot(frames, names, dates[-1], sp)
    elif args.strategy == "reversal":
        signals = strategy_rev.latest_rev_snapshot(frames, names, dates[-1], sp)
    else:
        signals = strategy.latest_signal_snapshot(frames, names, dates[-1], sp)
    report.write_report(args.output, metrics, {"strategy": vars(sp), "backtest": vars(bp)}, nav, trades, signals)
    report.plot_equity_curve(nav["nav"], benchmark, os.path.join(args.output, "equity_curve.png"))

    pct = lambda x: "—" if x is None else f"{x:.2%}"
    sharpe_s = f"{metrics['sharpe']:.2f}" if metrics["sharpe"] is not None else "—"
    pf_s = f"{metrics['profit_factor']:.2f}" if metrics["profit_factor"] is not None else "—"
    print("\n=== 回测结果 ===")
    print(f"总收益率：{metrics['total_return']:.2%}   年化：{metrics['annual_return']:.2%}")
    print(f"最大回撤：{metrics['max_drawdown']:.2%}   夏普：{sharpe_s}")
    print(f"交易次数：{metrics['n_trades']}   胜率：{pct(metrics['win_rate'])}   盈亏比：{pf_s}")
    print(f"平均持有：{metrics['avg_hold_days'] if metrics['avg_hold_days'] is not None else '—'} 天   "
          f"换手率：{metrics['turnover_ratio']:.2f}")
    n_active = sum(bool(x) for x in (signals["signal"] if "signal" in signals else signals["in_pool"]))
    print(f"\n最新交易日 {dates[-1].date()} 候选信号 {n_active} 只（详见 latest_signals.csv）")
    print(f"\n产物目录：{args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
