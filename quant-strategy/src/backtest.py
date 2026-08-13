"""回测引擎 v2：趋势出场 + ATR 跟踪止损，替代定时调仓卖出。

出场逻辑（每日收盘检查）：
1. hard_stop     跌破买入价 × (1 - stop_loss) 硬止损兜底；
2. trailing_stop 收盘跌破「持仓以来最高价 - trail_atr_mult × ATR」，快速回撤保护；
3. ma_cross      EMA20 下穿 EMA60 才确认趋势破位（避免单日假跌破洗仓）；
4. max_hold      最长持有兜底。

买入逻辑：每 rebalance_days 个交易日根据信号补仓新候选；不再定时强卖。
市场择时转弱（当日无目标）时整体清仓，规避系统性风险。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class BacktestParams:
    initial_capital: float = 1_000_000.0
    commission: float = 0.00025
    min_commission: float = 5.0
    stamp_duty: float = 0.0005
    slippage: float = 0.0005
    stop_loss: float = 0.10
    trail_atr_mult: float = 2.5
    max_hold_days: int = 120
    rebalance_days: int = 5
    lot_size: int = 100
    liquidate_on_regime: bool = True
    exit_on_signal_loss: bool = True
    exit_mode: str = "trend"  # trend=动量趋势 / mean_rev=均值回归 / hold=纯轮动（因子策略）
    mr_stop: float = 0.08
    mr_target: float = 0.10
    mr_exit_rsi: float = 55.0
    mr_max_hold: int = 10
    use_vol_target: bool = False
    target_vol: float = 0.20
    use_crash_filter: bool = False
    crash_mom20: float = -0.05
    vol_weight: bool = False
    max_weight: float = 0.0


@dataclass
class Position:
    code: str
    shares: int
    fill_price: float
    buy_date: pd.Timestamp
    buy_cost: float
    highest_close: float = 0.0


def run_backtest(
    dfs: dict[str, pd.DataFrame],
    frames: dict[str, pd.DataFrame],
    targets: dict[pd.Timestamp, list[str]],
    params: BacktestParams,
    index_data: pd.DataFrame | None = None,
) -> dict:
    """运行回测，返回净值表、交易记录和统计信息。"""
    dates = sorted({d for df in dfs.values() for d in df.index})
    date_idx = {d: i for i, d in enumerate(dates)}

    cash = params.initial_capital
    positions: dict[str, Position] = {}
    trades: list[dict] = []
    nav_rows: list[dict] = []

    pending_buys: list[str] = []
    pending_weights: list[float] = []
    pending_exits: list[tuple[str, str]] = []
    pending_scale = 1.0

    def buy_at_open(code: str, day: pd.Timestamp, weight: float = 1.0):
        nonlocal cash
        if code in positions or day not in dfs[code].index:
            return
        px = float(dfs[code].loc[day, "open"])
        if pd.isna(px) or px <= 0:
            return
        fill = px * (1 + params.slippage)
        total_w = sum(pending_weights) if pending_weights else 1.0
        budget = cash * pending_scale * weight / max(total_w, 1e-9)
        if params.max_weight > 0:
            equity = cash + sum(
                pos.shares * float(dfs[pos.code].loc[day, "open"])
                for pos in positions.values()
                if day in dfs[pos.code].index
            )
            budget = min(budget, params.max_weight * equity)
        shares = int(budget // (fill * params.lot_size)) * params.lot_size
        if shares <= 0:
            return
        turnover = shares * fill
        commission = max(turnover * params.commission, params.min_commission)
        cost = turnover + commission
        if cost > cash:
            return
        cash -= cost
        positions[code] = Position(code, shares, fill, day, cost, highest_close=fill)

    def sell_at(code: str, price: float, day: pd.Timestamp, reason: str):
        nonlocal cash
        pos = positions.get(code)
        if pos is None:
            return
        fill = price * (1 - params.slippage)
        turnover = pos.shares * fill
        commission = max(turnover * params.commission, params.min_commission)
        stamp = turnover * params.stamp_duty
        proceeds = turnover - commission - stamp
        cash += proceeds
        pnl = proceeds - pos.buy_cost
        trades.append(
            {
                "code": code,
                "buy_date": pos.buy_date.strftime("%Y-%m-%d"),
                "buy_price": round(pos.fill_price, 3),
                "sell_date": day.strftime("%Y-%m-%d"),
                "sell_price": round(fill, 3),
                "shares": pos.shares,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / pos.buy_cost, 4),
                "hold_days": date_idx[day] - date_idx[pos.buy_date],
                "reason": reason,
            }
        )
        del positions[code]

    for i, day in enumerate(dates):
        # 1) 开盘：先执行昨日计划的清仓，再补仓
        for code, reason in pending_exits:
            if code in positions and positions[code].buy_date < day and day in dfs[code].index:
                sell_at(code, float(dfs[code].loc[day, "open"]), day, reason)
        pending_exits = []
        for code, weight in zip(pending_buys, pending_weights):
            buy_at_open(code, day, weight)
        pending_buys = []
        pending_weights = []
        pending_scale = 1.0  # 缩放系数只对下一次调仓生效，执行后复位

        # 2) 收盘：趋势 / 风险出场（T+1，当日买入不卖出）
        for code in list(positions):
            pos = positions[code]
            if pos.buy_date == day or day not in dfs[code].index:
                continue
            close = float(dfs[code].loc[day, "close"])
            if pd.isna(close):
                continue
            pos.highest_close = max(pos.highest_close, close)
            f = frames.get(code)
            hold = date_idx[day] - date_idx[pos.buy_date]
            reason = None
            if params.exit_mode == "mean_rev":
                rsi = (
                    float(f.loc[day, "rsi"])
                    if f is not None and day in f.index
                    else float("nan")
                )
                bb_mid = (
                    float(f.loc[day, "bb_mid"])
                    if f is not None and day in f.index
                    else float("nan")
                )
                if close <= pos.fill_price * (1 - params.mr_stop):
                    reason = "mr_stop"
                elif close >= pos.fill_price * (1 + params.mr_target):
                    reason = "mr_target"
                elif (not pd.isna(rsi) and rsi >= params.mr_exit_rsi) or (
                    not pd.isna(bb_mid) and close >= bb_mid
                ):
                    reason = "mr_signal"
                elif hold >= params.mr_max_hold:
                    reason = "mr_time"
            elif params.exit_mode == "hold":
                reason = None  # 纯轮动：无单边出场，靠定期换仓
            else:
                ema20 = (
                    float(f.loc[day, "ema_fast"])
                    if f is not None and day in f.index
                    else float("nan")
                )
                ema60 = (
                    float(f.loc[day, "ema_slow"])
                    if f is not None and day in f.index
                    else float("nan")
                )
                atr_val = (
                    float(f.loc[day, "atr"])
                    if f is not None and day in f.index
                    else float("nan")
                )
                if close <= pos.fill_price * (1 - params.stop_loss):
                    reason = "hard_stop"
                elif (
                    not pd.isna(atr_val)
                    and atr_val > 0
                    and close < pos.highest_close - params.trail_atr_mult * atr_val
                ):
                    reason = "trailing_stop"
                elif not pd.isna(ema20) and not pd.isna(ema60) and ema20 < ema60:
                    reason = "ma_cross"
                elif hold >= params.max_hold_days:
                    reason = "max_hold"
            if reason:
                sell_at(code, close, day, reason)

        # 3) 收盘后规划次日操作
        if params.exit_mode == "hold":
            # 因子轮动：定期全量换仓
            if (i % params.rebalance_days == 0) and i < len(dates) - 1:
                target = targets.get(day, [])
                scale, ok_buy = 1.0, True
                if index_data is not None and day in index_data.index:
                    row = index_data.loc[day]
                    if params.use_crash_filter:
                        mom20 = row.get("mom20", float("nan"))
                        ok_buy = not pd.isna(mom20) and float(mom20) >= params.crash_mom20
                    if params.use_vol_target:
                        vol = row.get("vol20", float("nan"))
                        if not pd.isna(vol) and float(vol) > 0:
                            scale = min(1.0, params.target_vol / float(vol))
                if ok_buy:
                    pending_exits = [
                        (c, "rebalance") for c in positions if c not in set(target)
                    ]
                    pending_buys = [c for c in target if c not in positions]
                    pending_scale = scale
                    if params.vol_weight:
                        ws = []
                        for c in pending_buys:
                            f = frames.get(c)
                            v = (
                                float(f.loc[day, "vol20"])
                                if f is not None and day in f.index
                                else float("nan")
                            )
                            ws.append(1.0 / v if not pd.isna(v) and v > 0 else 1.0)
                        pending_weights = ws
                    else:
                        pending_weights = [1.0] * len(pending_buys)
                else:
                    # 市场大跌：清仓避险
                    pending_exits = [(c, "market_exit") for c in positions]
                    pending_buys = []
                    pending_weights = []
                    pending_scale = 1.0
        elif params.exit_mode == "mean_rev":
            # 均值回归：事件驱动，每天检查新信号
            target = targets.get(day, [])
            pending_buys = [c for c in target if c not in positions]
            pending_weights = [1.0] * len(pending_buys)
        elif (i % params.rebalance_days == 0) and i < len(dates) - 1:
            target = targets.get(day, [])
            if not target and params.liquidate_on_regime:
                pending_exits = [(c, "market_exit") for c in positions]
            else:
                # 信号失效才卖：持仓不再满足选股条件时离场，不强制定时调仓
                if params.exit_on_signal_loss:
                    for code in positions:
                        f = frames.get(code)
                        if f is not None and day in f.index and not bool(f.loc[day, "in_pool"]):
                            pending_exits.append((code, "signal_loss"))
                pending_buys = [c for c in target if c not in positions]
                pending_weights = [1.0] * len(pending_buys)

        # 4) 当日净值
        mv = 0.0
        for code, pos in positions.items():
            if day in dfs[code].index:
                close = float(dfs[code].loc[day, "close"])
                if not pd.isna(close):
                    mv += pos.shares * close
        nav = cash + mv
        nav_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "cash": round(cash, 2),
                "market_value": round(mv, 2),
                "nav": round(nav, 2),
            }
        )

    nav_df = pd.DataFrame(nav_rows).set_index("date")
    nav_df.index = pd.to_datetime(nav_df.index)
    trades_df = pd.DataFrame(trades)
    total_turnover = sum(
        (t["shares"] * t["buy_price"]) + (t["shares"] * t["sell_price"]) for t in trades
    )
    return {
        "nav": nav_df,
        "trades": trades_df,
        "total_turnover": total_turnover,
        "n_trades": len(trades),
        "final_cash": cash,
    }
