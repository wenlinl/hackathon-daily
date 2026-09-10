#!/usr/bin/env python3
"""全板块扫描：从"低位启动 + 资金异动"角度每天产出候选板块清单。

设计（详见 docs/sector-workflow.md）：
    全量板块快照 -> 初筛(涨幅/成交额/量比/60日涨幅) -> 资金流日线(位置/多周期涨幅/连续净流入) -> 四维打分 -> 候选榜
机器负责"广撒网 + 量化验证"，逻辑可持续性(S/A/B/C)留给人工判断。

用法:
    python3 sector_scan.py --dry-run            # 只采集并打印，不写历史、不写 Notion
    python3 sector_scan.py                      # 采集、写 scan_history.json，并写入 Notion(有 TOKEN 时)
    python3 sector_scan.py --min-score 5 --top 15
    python3 sector_scan.py --json               # 仅输出候选 JSON

依赖: 同目录 collect.py 的网络/格式化/Notion 工具。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests

from collect import (
    FALLBACK_DB_ID,
    HEADERS,
    NOTION_API,
    NOTION_DATABASE_ID,
    NOTION_TOKEN,
    BREADTH,
    INDICES,
    QUOTE_FIELDS,
    REQUEST_GAP,
    TZ,
    Breadth,
    Quote,
    _fmt,
    _fmt_pct,
    _fmt_yi,
    _labeled_block,
    _text_block,
    fetch_dt,
    fetch_zb,
    fetch_zt,
    notion_append_children,
    notion_create_page,
    notion_headers,
    query_database_rows,
)

# ---------------------------------------------------------------------------
# 可调参数（与 SOP 附录保持一致）
# ---------------------------------------------------------------------------

MIN_AMOUNT = 3e8          # 板块当日成交额下限（元），过滤缺乏流动性的小板块
MIN_SCORE = 4             # 四维总分门槛（位置/量价/资金/拥挤度 各 0-2，满分 8）
TOP_N = 20                # 报告展示的候选数量
POS_LOW = 0.25            # 距一年高点回撤 >=25% 记 2 分（低位）
POS_MID = 0.10            # 回撤 10%-25% 记 1 分
VP_PCT_RANGE = (0.5, 4.0)  # 温和上涨区间（%）
VP_VR_RANGE = (1.2, 2.5)   # 温和放量量比区间
CAP_CONSEC_DAYS = 3       # 主力连续净流入 >=3 天记 2 分
CROWD_R20_HOT = 0.25      # 20 日涨幅 >=25% 视为拥挤
CROWD_VR_HOT = 2.5        # 量比 >=2.5 视为局部过热
HISTORY_DAYS = 260        # 历史回看天数（一年以上）
LOCAL_GAP = 2.0           # 逐板块请求基础间隔（秒）
LOCAL_JITTER = 0.8        # 间隔随机抖动上限（秒），避免规律性请求触发限流
COOLDOWN_FAILS = 5        # 连续失败达到该值后冷却一次
COOLDOWN_SECS = 30        # 冷却时长（秒）
PRE_PCT_RANGE = (-1.5, 6.0)  # 预筛：当日涨幅区间（%），允许小幅回调日
PRE_VR_RANGE = (0.8, 3.0)  # 预筛：快照量比区间
PRE_R60_RANGE = (-0.45, 0.35)  # 预筛：快照 60 日涨幅区间（过滤长阴跌与高位）
PRE_R5_MIN = 0.5          # 预筛：当日下跌时，要求 5 日总体涨幅 >=0.5%
PRE_POOL_CAP = 60         # 进入逐板块历史请求的最大数量
LEADERS_TOP_N = 6         # 为前 N 个候选拉取板块内领涨个股
LEADER_COUNT = 5          # 每个板块展示的领涨个股数量
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
FLOW_DELAY_URL = "https://push2delay.eastmoney.com/api/qt/stock/fflow/daykline/get"
QUOTE_APIS = [
    "https://push2.eastmoney.com/api/qt",       # 实时入口
    "https://push2delay.eastmoney.com/api/qt",  # 延时入口（盘后数据完整，可作降级）
]
HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "scan_history.json"
)
CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "board_cache.json"
)

# 合成/统计型板块，不参与行业研究
DENY_NAMES = ("昨日", "涨停", "跌停", "连板", "炸板", "打板")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Board:
    code: str
    name: str
    kind: str = "概念"  # 行业 / 概念
    pct: float = 0.0
    amount: float = 0.0
    vr: float | None = None
    r60_snap: float | None = None  # 快照自带 60 日涨幅（预筛用）
    r5_snap: float | None = None   # 快照自带 5 日涨幅（预筛用）
    drawdown: float | None = None
    r5: float | None = None
    r20: float | None = None
    r60: float | None = None
    consec_up: int = 0
    main_flow: float | None = None
    main_pct: float | None = None  # 主力净占比（%）
    consec_inflow: int = 0
    pos_s: int = 0
    vp_s: int = 0
    cap_s: int = 0
    crowd_s: int = 0
    total: int = 0
    labels: list[str] = field(default_factory=list)
    excluded: str = ""  # 空=通过筛选


@dataclass
class Env:
    regime: str = "中性"
    cap: str = "中"
    detail: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class Report:
    date: str = ""
    env: Env = field(default_factory=Env)
    candidates: list[Board] = field(default_factory=list)
    passed: int = 0
    stage_counts: dict = field(default_factory=dict)
    excluded_examples: list[tuple[str, str, str]] = field(default_factory=list)
    excl_stats: dict = field(default_factory=dict)
    leaders: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    new_in: list[Board] = field(default_factory=list)
    still_in: list[Board] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    prev: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    hist_failed: int = 0


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _mean(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _num(v) -> float | None:
    """把接口返回的 '-' 等非数值统一转成 None。"""
    return float(v) if isinstance(v, (int, float)) else None


def _try_json_once(url: str, params: dict | None = None) -> dict | None:
    """单次快速请求（不重试），用于快速探测/降级，避免在被限流入口上耗时长退避。"""
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=8)
        if resp.status_code == 200 and resp.text.strip():
            return resp.json()
    except Exception:
        pass
    return None


def _parse_klines(raw: dict | None) -> list[dict]:
    rows = ((raw or {}).get("data")) or {}
    out: list[dict] = []
    for line in rows.get("klines") or []:
        p = line.split(",")
        if len(p) < 10:
            continue
        try:
            out.append(
                {
                    "date": p[0],
                    "open": float(p[1]),
                    "close": float(p[2]),
                    "high": float(p[3]),
                    "low": float(p[4]),
                    "vol": float(p[5]),
                    "amount": float(p[6]),
                    "pct": float(p[8]),
                }
            )
        except (ValueError, IndexError):
            continue
    return out


def _parse_history(raw: dict | None) -> list[tuple[str, float, float, float]]:
    """解析资金流日线：每行 = (日期, 主力净流入, 收盘价, 涨跌幅)。"""
    rows = ((raw or {}).get("data")) or {}
    out: list[tuple[str, float, float, float]] = []
    for line in rows.get("klines") or []:
        p = line.split(",")
        if len(p) < 13:
            continue
        try:
            out.append((p[0], float(p[1]), float(p[11]), float(p[12])))
        except (ValueError, IndexError):
            continue
    return out


def _secid_board(code: str) -> str:
    return f"90.{code}"


# ---------------------------------------------------------------------------
# 阶段 1：全量板块快照（行情 + 当日主力净流入）
# ---------------------------------------------------------------------------

def _fetch_board_snapshot(kind: str, fid: str, extra_fields: str) -> list[dict]:
    """拉取某类板块的全量快照；主入口缺页时用延时镜像补齐并合并去重。"""
    t = "2" if kind == "行业" else "3"
    merged: dict[str, dict] = {}
    total = 0
    for base in QUOTE_APIS:
        for pn in range(1, 13):
            diff: list = []
            for attempt in range(3):
                data = _try_json_once(
                    f"{base}/clist/get",
                    {
                        "pn": pn,
                        "pz": 100,
                        "po": 1,
                        "np": 1,
                        "fltt": 2,
                        "invt": 2,
                        "fid": fid,
                        "fs": f"m:90+t:{t}",
                        "fields": f"f12,f14,f3,f6,f10,f24,f109{extra_fields}",
                    },
                )
                d = (data or {}).get("data") or {}
                diff = d.get("diff") or []
                if isinstance(diff, dict):
                    diff = list(diff.values())
                if diff:
                    total = max(total, int(d.get("total") or 0))
                    break
                time.sleep(1.2 * (attempt + 1))
            for item in diff:
                code = str(item.get("f12") or "")
                if code:
                    merged[code] = item
            if len(merged) >= total:
                break
            time.sleep(0.3)
        if merged and len(merged) >= total:
            break
        time.sleep(1.0)
    # 与入口顺序无关，统一按当日涨幅降序（供后续截断使用）
    return sorted(
        merged.values(),
        key=lambda x: float(x.get("f3") or -999),
        reverse=True,
    )


def fetch_all_boards() -> dict[str, Board]:
    """行业+概念全量快照，并合并当日主力净流入。"""
    boards: dict[str, Board] = {}
    for kind in ("行业", "概念"):
        for d in _fetch_board_snapshot(kind, "f3", ""):
            code = str(d.get("f12") or "")
            name = str(d.get("f14") or "")
            if not code or not name:
                continue
            boards[code] = Board(
                code=code,
                name=name,
                kind=kind,
                pct=float(d.get("f3") or 0),
                amount=float(d.get("f6") or 0),
                vr=_num(d.get("f10")),
                r60_snap=_num(d.get("f24")),
                r5_snap=_num(d.get("f109")),
            )
        time.sleep(REQUEST_GAP)
        for d in _fetch_board_snapshot(kind, "f62", ",f62,f184"):
            code = str(d.get("f12") or "")
            b = boards.get(code)
            if b:
                b.main_flow = d.get("f62")
                b.main_pct = _num(d.get("f184"))
        time.sleep(REQUEST_GAP)
    return boards


def fetch_board_leaders(b: Board, count: int = LEADER_COUNT) -> list[tuple[str, float]]:
    """拉取板块内按当日涨幅排序的领涨个股。"""
    for base in QUOTE_APIS:
        data = _try_json_once(
            f"{base}/clist/get",
            {
                "pn": 1,
                "pz": count,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": f"b:{b.code}",
                "fields": "f12,f14,f3",
            },
        )
        diff = (data or {}).get("data") or {}
        diff = diff.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if diff:
            return [
                (str(d.get("f14") or ""), float(d.get("f3") or 0))
                for d in diff
                if d.get("f14")
            ]
    return []


# ---------------------------------------------------------------------------
# 阶段 2：资金流日线 -> 收盘价序列(位置/多周期涨幅) + 连续净流入
# ---------------------------------------------------------------------------

def load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def _merge_rows(cached: list[list], fresh: list[tuple]) -> list[tuple]:
    """按日期合并缓存与最新行情，去重并截断到 HISTORY_DAYS。"""
    by_date = {r[0]: r for r in [(x[0], x[1], x[2], x[3]) for x in cached if len(x) >= 4]}
    for row in fresh:
        by_date[row[0]] = row
    merged = sorted(by_date.values(), key=lambda x: x[0])
    return merged[-HISTORY_DAYS:]


def _apply_history(b: Board, rows: list[tuple]) -> None:
    if len(rows) < 6:
        return
    closes = [r[2] for r in rows]
    last = closes[-1]
    b.drawdown = last / max(closes[-250:]) - 1
    b.r5 = last / closes[-6] - 1 if len(closes) >= 6 else None
    b.r20 = last / closes[-21] - 1 if len(closes) >= 21 else None
    b.r60 = last / closes[-61] - 1 if len(closes) >= 61 else None
    b.consec_up = 0
    for r in reversed(rows):
        if r[3] > 0:
            b.consec_up += 1
        else:
            break
    b.consec_inflow = 0
    for r in reversed(rows):
        if r[1] > 0:
            b.consec_inflow += 1
        else:
            break
    if b.main_flow is None:
        b.main_flow = rows[-1][1]


def fetch_board_history(b: Board, cache: dict, trade_date: str) -> bool:
    """拉取资金流日线并更新板块指标；有当日缓存时只做增量更新。"""
    cached = cache.get(b.code)
    rows: list[tuple] = []
    if isinstance(cached, dict):
        raw_rows = cached.get("rows") or []
        rows = [(r[0], r[1], r[2], r[3]) for r in raw_rows if len(r) >= 4]
    lmt = HISTORY_DAYS
    if rows:
        try:
            last_date = datetime.strptime(rows[-1][0], "%Y-%m-%d").date()
            target = datetime.strptime(trade_date, "%Y-%m-%d").date()
            if 0 <= (target - last_date).days <= 20:
                lmt = 20  # 缓存较新：只补最近 20 根，覆盖周末与长假
        except (ValueError, IndexError):
            pass
    params = {
        "lmt": lmt,
        "klt": 101,
        "secid": _secid_board(b.code),
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
    }
    fresh: list = []
    for attempt in range(3):
        fresh = _parse_history(_try_json_once(FLOW_URL, params))
        if fresh:
            break
        time.sleep(1.5 * (attempt + 1))
    if not fresh:
        # 主入口被限流时降级到延时入口（仅返回当日一行，需配合缓存）
        fresh = _parse_history(_try_json_once(FLOW_DELAY_URL, params))
        if not fresh:
            time.sleep(2.0)
            fresh = _parse_history(_try_json_once(FLOW_DELAY_URL, params))
    if not fresh and not rows:
        return False
    merged = _merge_rows(rows, fresh)
    _apply_history(b, merged)
    if b.drawdown is None:
        return False
    cache[b.code] = {
        "rows": [[r[0], r[1], r[2], r[3]] for r in merged],
        "updated": merged[-1][0],
    }
    return True


# ---------------------------------------------------------------------------
# 打分与筛选
# ---------------------------------------------------------------------------

def score_position(b: Board) -> int:
    if b.drawdown is None:
        return 0
    if b.drawdown <= -POS_LOW:
        return 2
    if b.drawdown <= -POS_MID:
        return 1
    return 0


def score_volume_price(b: Board) -> int:
    pct, vr = b.pct, b.vr
    if vr is None:
        return 0
    if VP_PCT_RANGE[0] <= pct <= VP_PCT_RANGE[1] and VP_VR_RANGE[0] <= vr <= VP_VR_RANGE[1]:
        return 2
    if pct > 0 and 1.0 <= vr <= 3.0:
        return 1
    return 0


def score_capital(b: Board) -> int:
    flow = b.main_flow if b.main_flow is not None else 0
    if flow <= 0:
        return 0
    if b.consec_inflow >= CAP_CONSEC_DAYS:
        return 2
    return 1


def score_crowding(b: Board, candidate_days: int) -> int:
    r20 = b.r20 if b.r20 is not None else 0
    vr = b.vr if b.vr is not None else 0
    if candidate_days >= 3 or r20 >= CROWD_R20_HOT or (vr >= 3.5 and b.pct >= 5):
        return 0
    if r20 >= 0.10 or vr >= CROWD_VR_HOT:
        return 1
    return 2


def classify(b: Board) -> str:
    """返回排除原因；空字符串表示通过。"""
    if b.drawdown is None:
        return "无历史数据"
    if (b.r20 or 0) <= -0.15:
        return "下降趋势未扭转"
    # 短期趋势：当日/5日/20日 三者不能全为负（不苛求当天上涨）
    if b.pct <= 0 and (b.r5 or -99) <= 0 and (b.r20 or -99) <= 0:
        return "短期无上涨"
    if b.drawdown > -POS_MID and (b.r20 or 0) >= CROWD_R20_HOT and (b.vr or 0) >= 3.0:
        return "高位加速"
    if b.pct >= 7.0 and (b.vr or 0) >= CROWD_VR_HOT:
        return "单日过热"
    b.pos_s = score_position(b)
    b.vp_s = score_volume_price(b)
    if b.pos_s + b.vp_s < 2:
        return "形态未达标"
    return ""


# ---------------------------------------------------------------------------
# 大盘环境
# ---------------------------------------------------------------------------

def fetch_index_kline(secid: str, lmt: int = 40) -> list[dict]:
    for attempt in range(3):
        data = _try_json_once(
            KLINE_URL,
            {
                "secid": secid,
                "klt": 101,
                "fqt": 1,
                "lmt": lmt,
                "end": "20500101",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
        )
        bars = _parse_klines(data)
        if bars:
            return bars
        time.sleep(1.5 * (attempt + 1))
    return []


def fetch_trade_mood() -> tuple[str, int, int, int]:
    """返回 (交易日, 涨停, 跌停, 炸板)。周末/节假日自动回退最近交易日。"""
    today = datetime.now(TZ)
    for back in range(6):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        dc = d.strftime("%Y%m%d")
        zt_total, _, qdate = fetch_zt(dc)
        if zt_total or qdate:
            dt_total, _ = fetch_dt(dc)
            time.sleep(REQUEST_GAP)
            zb_total, _ = fetch_zb(dc)
            trade_date = (
                f"{qdate[:4]}-{qdate[4:6]}-{qdate[6:8]}" if qdate else d.strftime("%Y-%m-%d")
            )
            return trade_date, zt_total, dt_total, zb_total
    return today.strftime("%Y-%m-%d"), 0, 0, 0


def fetch_indices_fb() -> list[Quote]:
    """指数快照：实时入口失败时降级到延时镜像。"""
    secids = ",".join(s for s, _ in INDICES)
    name_map = {secid.split(".")[1]: name for secid, name in INDICES}
    for base in QUOTE_APIS:
        data = _try_json_once(
            f"{base}/ulist.np/get",
            {"fltt": 2, "invt": 2, "fields": QUOTE_FIELDS, "secids": secids},
        )
        diff = (data or {}).get("data") or {}
        diff = diff.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if diff:
            return [
                Quote(
                    code=str(d.get("f12") or ""),
                    name=name_map.get(str(d.get("f12") or ""), str(d.get("f14") or "")),
                    price=d.get("f2"),
                    pct=d.get("f3"),
                    change=d.get("f4"),
                    amount=d.get("f6"),
                    high=d.get("f15"),
                    low=d.get("f16"),
                    open_=d.get("f17"),
                )
                for d in diff
                if d.get("f12")
            ]
    return []


def fetch_breadth_fb() -> list[Breadth]:
    """涨跌家数：实时入口失败时降级到延时镜像。"""
    result: list[Breadth] = []
    for secid, label in BREADTH:
        d = None
        for base in QUOTE_APIS:
            data = _try_json_once(
                f"{base}/stock/get",
                {"secid": secid, "fields": "f14,f113,f114,f115"},
            )
            d = (data or {}).get("data")
            if d:
                break
        if not d:
            result.append(Breadth(label=label))
            continue
        result.append(
            Breadth(
                label=label,
                up=int(d.get("f113") or 0),
                down=int(d.get("f114") or 0),
                flat=int(d.get("f115") or 0),
            )
        )
        time.sleep(0.3)
    return result


def fetch_env() -> tuple[Env, str]:
    env = Env()
    quotes = fetch_indices_fb()
    sh = next((q for q in quotes if q.code == "000001"), None)
    sz = next((q for q in quotes if q.code == "399001"), None)

    sh_bars = fetch_index_kline("1.000001")
    time.sleep(REQUEST_GAP)
    sz_bars = fetch_index_kline("0.399001")
    time.sleep(REQUEST_GAP)
    if not sh_bars or not sz_bars:
        env.detail.append("指数历史K线获取失败（可能被限流），环境档位按中性处理")

    trend_up = None
    if len(sh_bars) >= 20:
        ma20 = _mean([x["close"] for x in sh_bars[-20:]])
        last_close = sh_bars[-1]["close"]
        if ma20:
            trend_up = last_close >= ma20
            r5_sh = last_close / sh_bars[-6]["close"] - 1
            dev = last_close / ma20 - 1
            env.detail.append(
                f"上证 {_fmt(last_close)}，5日 {_fmt_pct(r5_sh)}，20日线 {_fmt(ma20)}，"
                f"偏离 {_fmt_pct(dev)}（{'上方' if trend_up else '下方'}）"
            )
    if len(sz_bars) >= 20:
        ma20_sz = _mean([x["close"] for x in sz_bars[-20:]])
        if ma20_sz:
            last_sz = sz_bars[-1]["close"]
            r5_sz = last_sz / sz_bars[-6]["close"] - 1
            dev_sz = last_sz / ma20_sz - 1
            env.detail.append(
                f"深成指 {_fmt(last_sz)}，5日 {_fmt_pct(r5_sz)}，20日线 {_fmt(ma20_sz)}，"
                f"偏离 {_fmt_pct(dev_sz)}（{'上方' if dev_sz >= 0 else '下方'}）"
            )

    vol_ok = None
    today_total = None
    if sh and sz and sh.amount and sz.amount:
        today_total = sh.amount + sz.amount
    prev_totals: list[float] = []
    if len(sh_bars) >= 6 and len(sz_bars) >= 6:
        for i in range(-6, -1):
            prev_totals.append(sh_bars[i]["amount"] + sz_bars[i]["amount"])
    avg5 = _mean(prev_totals)
    if today_total and avg5:
        vol_ok = today_total >= avg5
        delta = today_total / avg5 - 1
        vol_word = "放量" if delta >= 0.05 else ("缩量" if delta <= -0.05 else "量能平稳")
        env.detail.append(
            f"两市成交 {_fmt_yi(today_total)}，较5日均量 {_fmt_pct(delta)}（{vol_word}）"
        )

    breadth = fetch_breadth_fb()
    up = sum(x.up for x in breadth)
    down = sum(x.down for x in breadth)
    if up or down:
        ratio = up / (up + down)
        mood_word = "普涨" if ratio >= 0.6 else ("普跌" if ratio <= 0.4 else "分化")
        env.detail.append(f"涨跌家数：上涨 {up} / 下跌 {down}（{mood_word}，{ratio*100:.1f}%上涨）")

    trade_date, zt, dt, zb = fetch_trade_mood()
    zb_ratio = zb / (zt + zb) if (zt + zb) > 0 else None
    zb_display = zb_ratio * 100 if zb_ratio is not None else None
    env.detail.append(f"涨停 {zt} / 跌停 {dt} / 炸板 {zb}（炸板率 {_fmt(zb_display, 1)}%）")

    bull = bool(trend_up and vol_ok and zt >= 60 and zb_ratio is not None and zb_ratio <= 0.30)
    bear = bool(
        trend_up is False
        and vol_ok is False
        and (zt <= 30 or (zb_ratio is not None and zb_ratio >= 0.40))
    )
    if bull:
        env.regime, env.cap = "积极", "高"
    elif bear:
        env.regime, env.cap = "防御", "低"
    else:
        env.regime, env.cap = "中性", "中"

    # 环境解读
    parts: list[str] = []
    if trend_up is None:
        parts.append("指数趋势数据缺失")
    else:
        parts.append("上证" + ("站上" if trend_up else "跌破") + "20日线")
    if vol_ok is None:
        parts.append("量能数据缺失")
    else:
        parts.append("两市成交" + ("高于" if vol_ok else "低于") + "5日均量")
    if zt >= 60:
        parts.append(f"涨停 {zt} 家、赚钱效应活跃")
    elif zt <= 30:
        parts.append(f"涨停仅 {zt} 家、情绪偏弱")
    else:
        parts.append(f"涨停 {zt} 家、赚钱效应一般")
    env.summary = (
        "，".join(parts)
        + f"。综合判定：{env.regime}环境，仓位上限参考：{env.cap}。"
    )
    return env, trade_date


# ---------------------------------------------------------------------------
# 历史对比
# ---------------------------------------------------------------------------

def load_history() -> tuple[list[dict], dict[str, int]]:
    """返回 (历史条目列表, {板块代码: 已连续入选天数})。"""
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return [], {}
    entries = data.get("entries", []) if isinstance(data, dict) else []
    days: dict[str, int] = {}
    for entry in entries[-10:]:
        for code in entry.get("codes", []):
            days[code] = days.get(code, 0) + 1
    return entries, days


def save_history(entries: list[dict], date: str, candidates: list[Board]) -> None:
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    entries = [e for e in entries if e.get("date") != date]
    entries.append(
        {
            "date": date,
            "codes": [b.code for b in candidates],
            "top": [
                {"code": b.code, "name": b.name, "kind": b.kind, "score": b.total, "pct": b.pct}
                for b in candidates[:TOP_N]
            ],
        }
    )
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump({"entries": entries[-60:]}, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------

def _fmt_signals(b: Board, is_new: bool) -> str:
    labels = list(b.labels)
    if is_new:
        labels.append("新上榜")
    return "·".join(labels) if labels else "--"


def _flow_str(v: float | None) -> str:
    if v is None:
        return "--"
    yi = v / 1e8
    return f"净流入{yi:+.1f}亿" if yi >= 0 else f"净流出{abs(yi):.1f}亿"


def _position_note(b: Board) -> str:
    if b.drawdown is None:
        return "位置数据缺失"
    dd = b.drawdown * 100
    if dd <= -25:
        return f"距一年高点 {dd:.1f}%，处于低位"
    if dd <= -10:
        return f"距一年高点 {dd:.1f}%，处于中低位"
    return f"距一年高点仅 {abs(dd):.1f}%，接近高位"


def _crowd_note(b: Board) -> str:
    r20 = (b.r20 or 0) * 100
    if r20 >= 25:
        return f"20日涨幅已达 {r20:.0f}%，热度较高"
    if r20 >= 10:
        return f"20日涨幅 {r20:.0f}%，开始受到关注"
    return f"20日涨幅仅 {r20:.1f}%，尚无人问津"


def _interp_text(b: Board) -> str:
    vp = f"今日{_fmt_pct(b.pct)}、量比{_fmt(b.vr)}，温和放量上攻" if b.vp_s == 2 else \
        f"今日{_fmt_pct(b.pct)}、量比{_fmt(b.vr)}，量价配合一般"
    pct_part = f"（占比 {b.main_pct:.1f}%）" if b.main_pct is not None else ""
    flow_part = _flow_str(b.main_flow)
    capital = f"主力{flow_part}{pct_part}，连续 {b.consec_inflow} 日净流入" if b.cap_s > 0 else \
        f"主力{flow_part}，资金面偏弱"
    verdict = "符合'低位温和启动'形态" if b.pos_s >= 1 and b.vp_s >= 1 else "信号偏弱、需进一步观察"
    return (
        f"{vp}；{_position_note(b)}；{capital}；{_crowd_note(b)}。"
        f"综合 {b.total} 分，{verdict}。重点确认：催化剂能否持续、利好能否落到利润。"
    )


def _detail_lines(i: int, b: Board, leaders: list[tuple[str, float]]) -> list[str]:
    pct_part = f"，占比 {b.main_pct:.1f}%" if b.main_pct is not None else ""
    lines = [
        f"{i}. {b.name}（{b.kind}） {b.total} 分 [{'·'.join(b.labels) or '--'}]",
        f"   走势：当日 {_fmt_pct(b.pct)}，5日 {_fmt_pct(b.r5)}，20日 {_fmt_pct(b.r20)}，"
        f"60日 {_fmt_pct(b.r60)}；距一年高点 {_fmt_pct(b.drawdown)}；量比 {_fmt(b.vr)}；"
        f"连续上涨 {b.consec_up} 日",
        f"   资金：主力 {_flow_str(b.main_flow)}{pct_part}，连续 {b.consec_inflow} 日净流入",
        f"   打分：位置{b.pos_s} + 量价{b.vp_s} + 资金{b.cap_s} + 拥挤度{b.crowd_s} = {b.total}",
        f"   解读：{_interp_text(b)}",
    ]
    if leaders:
        lines.append("   板块内领涨：" + "、".join(f"{n} {_fmt_pct(p)}" for n, p in leaders))
    return lines


def render_text(r: Report) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f"{r.date} 板块扫描报告（机器初筛 v1，门槛 ≥{MIN_SCORE}）")
    lines.append("=" * 78)

    lines.append(f"【一、大盘环境与仓位】{r.env.regime} | 仓位上限参考：{r.env.cap}")
    for d in r.env.detail:
        lines.append(f"- {d}")
    if r.env.summary:
        lines.append(f"- 解读：{r.env.summary}")

    lines.append("")
    lines.append("【二、筛选漏斗与排除统计】")
    stage = r.stage_counts
    lines.append(
        f"- 全量快照 {stage.get('total', 0)} 个板块 → 预筛达标 {stage.get('after_snapshot', 0)} → "
        f"历史校验通过 {stage.get('after_history', 0)} → 四维评分通过 {r.passed} 个"
    )
    if r.excl_stats:
        dist = "、".join(f"{k} {v}" for k, v in sorted(r.excl_stats.items(), key=lambda x: -x[1]))
        lines.append(f"- 历史阶段排除分布：{dist}")
    for name, kind, reason in r.excluded_examples[:5]:
        lines.append(f"- 例：{kind}·{name} → {reason}")

    lines.append("")
    lines.append(f"【三、候选榜总览】通过评分 {r.passed} 个，展示前 {min(len(r.candidates), TOP_N)} 个")
    lines.append(
        f"{'#':<3}{'板块':<10}{'类型':<4}{'当日':>7}{'5日':>7}{'20日':>7}"
        f"{'距高点':>7}{'量比':>6}{'主力':>9}{'连续':>4}{'分':>3} 信号"
    )
    for i, b in enumerate(r.candidates[:TOP_N], 1):
        flow = _fmt_yi(b.main_flow) if b.main_flow is not None else "--"
        lines.append(
            f"{i:<3}{b.name:<10}{b.kind:<4}{_fmt_pct(b.pct):>7}{_fmt_pct(b.r5):>7}"
            f"{_fmt_pct(b.r20):>7}{_fmt_pct(b.drawdown):>7}{_fmt(b.vr):>6}"
            f"{flow:>9}{b.consec_inflow:>4}{b.total:>3} {_fmt_signals(b, any(x is b for x in r.new_in))}"
        )
    if not r.candidates:
        lines.append("- 今日无板块通过评分门槛（可降低 --min-score 或检查数据源）")

    lines.append("")
    lines.append(f"【四、候选板块详解】（前 {min(len(r.candidates), LEADERS_TOP_N)} 个）")
    for i, b in enumerate(r.candidates[:LEADERS_TOP_N], 1):
        lines.extend(_detail_lines(i, b, r.leaders.get(b.code, [])))

    lines.append("")
    lines.append("【五、与昨日对比】")
    lines.append(
        f"- 新入选 {len(r.new_in)}："
        + ("、".join(f"{b.name}({b.total}分)" for b in r.new_in[:10]) or "无")
    )
    still_parts = []
    for b in r.still_in[:10]:
        prev = r.prev.get(b.code, {})
        ps = prev.get("score")
        if ps is None:
            still_parts.append(f"{b.name}({b.total}分)")
        else:
            still_parts.append(f"{b.name} {ps}→{b.total}分({b.total - ps:+d})")
    lines.append(f"- 持续候选 {len(r.still_in)}：" + ("、".join(still_parts) or "无"))
    lines.append(f"- 退出 {len(r.dropped)}：{('、'.join(r.dropped[:10]) or '无')}")

    lines.append("")
    lines.append("【六、待人工逻辑体检】")
    for i, b in enumerate(r.candidates[:10], 1):
        lines.append(
            f"{i}. {b.name}：催化剂是什么（政策/价格/供需）？能否持续？"
            f"利好能否落到利润？板块处于哪一阶段？"
        )

    lines.append("")
    lines.append("【七、数据与方法说明】")
    lines.append(
        f"- 评分：位置/量价/资金/拥挤度 各 0–2 分，满分 8，≥{MIN_SCORE} 入候选；"
        f"不含逻辑维度，融资余额与 ETF 份额未纳入(v2)。"
    )
    lines.append("- 口径：距一年高点回撤按收盘价序列计算；主力净流入为板块口径（元）。")
    lines.append("- 数据来源：东方财富公开接口。仅供研究参考，不构成投资建议。")
    for w in r.warnings:
        lines.append(f"- ⚠️ {w}")
    return "\n".join(lines)


def build_children(r: Report) -> list[dict]:
    c: list[dict] = []
    c.append(_text_block("paragraph", f"**{r.date} 板块扫描报告**（机器初筛 v1，门槛 ≥{MIN_SCORE}）"))

    c.append(_text_block("heading_2", "一、大盘环境与仓位"))
    c.append(_labeled_block("环境", f"{r.env.regime}，仓位上限参考：{r.env.cap}"))
    for d in r.env.detail:
        c.append(_text_block("bulleted_list_item", d))
    if r.env.summary:
        c.append(_labeled_block("解读", r.env.summary))

    c.append(_text_block("heading_2", "二、筛选漏斗与排除统计"))
    stage = r.stage_counts
    c.append(
        _labeled_block(
            "漏斗",
            f"全量快照 {stage.get('total', 0)} → 预筛达标 {stage.get('after_snapshot', 0)} → "
            f"历史校验通过 {stage.get('after_history', 0)} → 评分通过 {r.passed}",
        )
    )
    if r.excl_stats:
        c.append(
            _labeled_block(
                "排除分布",
                "、".join(f"{k} {v}" for k, v in sorted(r.excl_stats.items(), key=lambda x: -x[1])),
            )
        )
    for name, kind, reason in r.excluded_examples[:5]:
        c.append(_labeled_block(f"例：{kind}·{name}", reason))

    c.append(_text_block("heading_2", f"三、候选榜总览（{r.passed} 个通过）"))
    for i, b in enumerate(r.candidates[:TOP_N], 1):
        text = (
            f"{b.kind} 当日{_fmt_pct(b.pct)} 5日{_fmt_pct(b.r5)} 20日{_fmt_pct(b.r20)} "
            f"60日{_fmt_pct(b.r60)} 距高点{_fmt_pct(b.drawdown)} 量比{_fmt(b.vr)} "
            f"主力{_flow_str(b.main_flow)}(连续{b.consec_inflow}日) 分{b.total} "
            f"[{_fmt_signals(b, any(x is b for x in r.new_in))}]"
        )
        c.append(_labeled_block(f"{i}. {b.name}", text))
    if not r.candidates:
        c.append(_text_block("bulleted_list_item", "今日无板块通过评分门槛。"))

    c.append(_text_block("heading_2", f"四、候选板块详解（前 {min(len(r.candidates), LEADERS_TOP_N)} 个）"))
    for i, b in enumerate(r.candidates[:LEADERS_TOP_N], 1):
        for line in _detail_lines(i, b, r.leaders.get(b.code, [])):
            c.append(_text_block("paragraph", line))

    c.append(_text_block("heading_2", "五、与昨日对比"))
    c.append(
        _labeled_block(
            "新入选",
            "、".join(f"{b.name}({b.total}分)" for b in r.new_in[:10]) or "无",
        )
    )
    still_parts = []
    for b in r.still_in[:10]:
        prev = r.prev.get(b.code, {})
        ps = prev.get("score")
        still_parts.append(
            f"{b.name} {ps}→{b.total}分({b.total - ps:+d})" if ps is not None else f"{b.name}({b.total}分)"
        )
    c.append(_labeled_block("持续候选", "、".join(still_parts) or "无"))
    c.append(_labeled_block("退出", "、".join(r.dropped[:10]) or "无"))

    c.append(_text_block("heading_2", "六、待人工逻辑体检"))
    for i, b in enumerate(r.candidates[:10], 1):
        c.append(
            _text_block(
                "bulleted_list_item",
                f"{b.name}：催化剂是什么（政策/价格/供需）？能否持续？"
                f"利好能否落到利润？板块处于哪一阶段？",
            )
        )

    c.append(_text_block("heading_2", "七、数据与方法说明"))
    c.append(
        _text_block(
            "bulleted_list_item",
            f"评分：位置/量价/资金/拥挤度 各 0–2 分，满分 8，≥{MIN_SCORE} 入候选；"
            f"不含逻辑维度，融资余额与 ETF 份额未纳入(v2)。",
        )
    )
    c.append(_text_block("bulleted_list_item", "口径：距一年高点回撤按收盘价序列计算；主力净流入为板块口径。"))
    c.append(_text_block("bulleted_list_item", "⚠️ 数据仅供研究参考，不构成投资建议。"))
    for w in r.warnings:
        c.append(_text_block("bulleted_list_item", f"⚠️ {w}"))
    return c


def write_notion(r: Report) -> None:
    if not NOTION_TOKEN:
        print("[warn] 未设置 NOTION_TOKEN，跳过 Notion 写入", file=sys.stderr)
        return
    db_id = NOTION_DATABASE_ID or FALLBACK_DB_ID
    title = f"{r.date} 板块扫描报告"
    try:
        existing = query_database_rows(db_id)
    except RuntimeError as exc:
        print(f"[warn] 无法查询数据库，跳过写入: {exc}", file=sys.stderr)
        return

    found_id = None
    old_ids: list[str] = []
    for row in existing:
        props = row.get("properties", {})
        t = ""
        for p in props.values():
            if p.get("type") == "title":
                t = "".join(x.get("plain_text", "") for x in p.get("title", []))
        if t == title or t == f"{r.date} 板块扫描候选":
            found_id = row["id"]
        elif "板块扫描" in t:
            old_ids.append(row["id"])

    children = build_children(r)
    if found_id:
        # 同日已有记录：清空旧块后整体替换，保证保留收盘后的最新数据
        try:
            ids: list[str] = []
            cursor = None
            while True:
                params: dict = {"page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                resp = requests.get(
                    f"{NOTION_API}/blocks/{found_id}/children",
                    params=params,
                    headers=notion_headers(),
                    timeout=30,
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                ids.extend(b["id"] for b in data.get("results", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
            for bid in ids:
                requests.delete(f"{NOTION_API}/blocks/{bid}", headers=notion_headers(), timeout=30)
            notion_append_children(found_id, children)
            print(f"[ok] 已更新扫描报告: {title} @ {r.date}")
        except Exception as exc:
            print(f"[warn] 更新失败: {exc}", file=sys.stderr)
        return

    for pid in old_ids:
        try:
            resp = requests.patch(
                f"{NOTION_API}/pages/{pid}",
                headers=notion_headers(),
                json={"archived": True},
                timeout=30,
            )
            if resp.status_code == 200:
                print(f"[ok] 已归档旧扫描记录 {pid}")
        except Exception:
            pass

    properties = {
        "名称": {"title": [{"type": "text", "text": {"content": title}}]},
        "日期": {"date": {"start": r.date}},
    }
    try:
        page = notion_create_page(
            {"type": "database_id", "database_id": db_id}, properties, children=children[:90]
        )
        if len(children) > 90:
            notion_append_children(page["id"], children[90:])
        print(f"[ok] 已写入扫描报告: {title} @ {r.date}")
    except RuntimeError as exc:
        print(f"[warn] Notion 写入失败: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def scan(min_score: int, max_boards: int) -> tuple[Report, list[dict], dict]:
    r = Report()
    env, trade_date = fetch_env()
    r.env = env
    r.date = trade_date

    now = datetime.now(TZ)
    if now.hour < 15:
        r.warnings.append(f"当前 {now.strftime('%H:%M')}，盘中数据不完整，建议收盘后再运行。")

    print("[1/6] 拉取大盘环境…", flush=True)
    print("[2/6] 拉取全量板块快照…", flush=True)
    boards = fetch_all_boards()
    r.stage_counts["total"] = len(boards)

    # 首轮：用快照自带字段做硬过滤（涨幅/成交额/量比/60日涨幅/合成板块），
    # 大幅减少后续逐板块历史数据请求量。
    pool_a: list[Board] = []
    for b in boards.values():
        if not (PRE_PCT_RANGE[0] <= b.pct <= PRE_PCT_RANGE[1]):
            continue
        if b.amount < MIN_AMOUNT:
            continue
        if any(x in b.name for x in DENY_NAMES):
            continue
        # 当日不要求必须上涨：当日收跌时，看 5 日总体涨幅是否仍为正
        if b.pct <= 0 and (b.r5_snap is None or b.r5_snap < PRE_R5_MIN):
            continue
        if b.r60_snap is not None and not (
            PRE_R60_RANGE[0] <= b.r60_snap <= PRE_R60_RANGE[1]
        ):
            continue
        if b.vr is not None and not (PRE_VR_RANGE[0] <= b.vr <= PRE_VR_RANGE[1]):
            continue
        pool_a.append(b)
    raw_count = len(pool_a)
    if raw_count > max_boards:
        pool_a = pool_a[:max_boards]
        r.warnings.append(
            f"预筛后板块 {raw_count} 个超过上限 {max_boards}，已截断，建议收紧过滤参数。"
        )
    r.stage_counts["after_snapshot"] = len(pool_a)
    print(
        f"[info] 快照 {len(boards)} 个板块，预筛达标 {raw_count} 个，"
        f"进入历史校验 {len(pool_a)} 个",
        flush=True,
    )

    print("[3/6] 拉取板块历史与资金流（位置/量价/连续净流入）…", flush=True)
    cache = load_cache()
    pool_c: list[Board] = []
    fails = 0
    for i, b in enumerate(pool_a, 1):
        gap = LOCAL_GAP + random.uniform(0, LOCAL_JITTER)
        if not fetch_board_history(b, cache, trade_date):
            r.hist_failed += 1
            b.excluded = "无历史数据"
            r.excl_stats[b.excluded] = r.excl_stats.get(b.excluded, 0) + 1
            fails += 1
            if fails >= COOLDOWN_FAILS:
                print(f"[info] 连续 {fails} 次失败，冷却 {COOLDOWN_SECS}s 后继续…", flush=True)
                time.sleep(COOLDOWN_SECS)
                fails = 0
            if len(r.excluded_examples) < 6:
                r.excluded_examples.append((b.name, b.kind, b.excluded))
            time.sleep(gap)
            continue
        fails = 0
        reason = classify(b)
        if reason:
            b.excluded = reason
            r.excl_stats[reason] = r.excl_stats.get(reason, 0) + 1
            if reason not in ("形态未达标", "下降趋势未扭转", "短期无上涨") and len(r.excluded_examples) < 6:
                r.excluded_examples.append((b.name, b.kind, reason))
            time.sleep(gap)
            continue
        pool_c.append(b)
        if i % 20 == 0:
            print(f"[info] 历史进度 {i}/{len(pool_a)}，暂留 {len(pool_c)}", flush=True)
        time.sleep(gap)
    r.stage_counts["after_history"] = len(pool_c)

    print("[4/6] 四维打分与历史对比…", flush=True)
    entries, days = load_history()
    prev_codes: set[str] = set()
    if entries:
        prev_codes = set(entries[-1].get("codes", []))
        r.prev = {x.get("code"): x for x in entries[-1].get("top", []) if x.get("code")}

    for b in pool_c:
        b.pos_s = score_position(b)
        b.vp_s = score_volume_price(b)
        b.cap_s = score_capital(b)
        b.crowd_s = score_crowding(b, days.get(b.code, 0))
        b.total = b.pos_s + b.vp_s + b.cap_s + b.crowd_s
        if b.pos_s == 2:
            b.labels.append("低位")
        if b.vp_s == 2:
            b.labels.append("温和放量")
        if b.cap_s == 2:
            b.labels.append(f"主力连续{b.consec_inflow}日流入")
        if b.crowd_s == 2:
            b.labels.append("无人问津")

    passed = [b for b in pool_c if b.total >= min_score]
    passed.sort(key=lambda x: (-x.total, -x.pct, x.name))
    r.candidates = passed
    r.passed = len(passed)

    today_codes = {b.code for b in passed}
    r.new_in = [b for b in passed if b.code not in prev_codes]
    r.still_in = [b for b in passed if b.code in prev_codes]
    r.dropped = [r.prev.get(c, {}).get("name", c) for c in sorted(prev_codes - today_codes)]

    if r.hist_failed:
        r.warnings.append(f"{r.hist_failed} 个板块历史数据获取失败，已跳过。")

    for b in passed[:LEADERS_TOP_N]:
        r.leaders[b.code] = fetch_board_leaders(b)
        time.sleep(0.6)

    print("[5/6] 生成报告…", flush=True)
    r.stage_counts["passed"] = r.passed
    return r, entries, cache


def main() -> int:
    global TOP_N
    parser = argparse.ArgumentParser(description="全板块扫描：低位启动候选清单")
    parser.add_argument("--dry-run", action="store_true", help="只采集打印，不写历史、不写 Notion")
    parser.add_argument("--min-score", type=int, default=MIN_SCORE, help="候选总分门槛（默认 4）")
    parser.add_argument("--top", type=int, default=TOP_N, help="展示数量（默认 20）")
    parser.add_argument("--max-boards", type=int, default=PRE_POOL_CAP,
                        help="逐板块请求的最大数量（默认 100）")
    parser.add_argument("--json", action="store_true", help="仅输出候选 JSON")
    args = parser.parse_args()

    TOP_N = args.top
    r, entries, cache = scan(args.min_score, args.max_boards)

    if args.json:
        payload = {
            "date": r.date,
            "env": {"regime": r.env.regime, "cap": r.env.cap},
            "candidates": [
                {
                    "code": b.code,
                    "name": b.name,
                    "kind": b.kind,
                    "pct": b.pct,
                    "r5": b.r5,
                    "r20": b.r20,
                    "r60": b.r60,
                    "drawdown": b.drawdown,
                    "vr": b.vr,
                    "main_flow": b.main_flow,
                    "consec_inflow": b.consec_inflow,
                    "score": b.total,
                    "signals": b.labels,
                }
                for b in r.candidates[:TOP_N]
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print()
    print(render_text(r))
    print()

    if args.dry_run:
        print("[dry-run] 未写历史、未写缓存、未写 Notion")
        return 0
    save_history(entries, r.date, r.candidates)
    save_cache(cache)
    write_notion(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
