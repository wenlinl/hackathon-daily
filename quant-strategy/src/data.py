"""数据层：东方财富公开接口下载日 K 线（前复权），带本地缓存。"""

from __future__ import annotations

import json
import os
import pickle
import random
import threading
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}
KLINE_COLS = [
    "date", "open", "close", "high", "low",
    "volume", "amount", "amplitude", "pct", "change", "turnover",
]
KEEP_COLS = ["open", "close", "high", "low", "volume", "amount", "pct"]
REQUEST_GAP = 0.9
MAX_RETRIES = 3
MAX_PASSES = 3

_BS: dict = {"logged_in": False, "bs": None, "calls": 0}
_BS_LOCK = threading.Lock()
BS_RECONNECT_EVERY = 250


def to_secid(code: str) -> str:
    """股票代码转东方财富 secid（6 开头沪市，其余按深市处理）。"""
    code = str(code).strip().zfill(6)
    if code[0] in ("6", "9", "5"):
        return f"1.{code}"
    return f"0.{code}"


def _fetch_em(
    code: str, start: datetime, end: datetime, adjust: str = "qfq", secid: str | None = None
) -> pd.DataFrame:
    """东方财富日 K 线（主数据源）。"""
    params = {
        "secid": secid or to_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101,
        "fqt": 1 if adjust == "qfq" else 0,
        "beg": start.strftime("%Y%m%d"),
        "end": end.strftime("%Y%m%d"),
        "lmt": 1000000,
    }
    try:
        # 每次请求都用新连接：代理会掐断 keep-alive 长连接
        resp = requests.get(EASTMONEY_KLINE, params=params, headers=HEADERS, timeout=8)
        resp.raise_for_status()
        data = resp.json().get("data")
        if not data or not data.get("klines"):
            return _empty_frame()
        rows = [row.split(",") for row in data["klines"]]
        df = pd.DataFrame(rows, columns=KLINE_COLS)
        df = df[["date"] + KEEP_COLS]
        for col in KEEP_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date").sort_index()
    except Exception:
        return _empty_frame()
    return _empty_frame()


def _fetch_tencent(
    code: str, start: datetime, end: datetime, adjust: str = "qfq"
) -> pd.DataFrame:
    """腾讯日 K 线（自动回退数据源，前复权）。"""
    market = {"6": "sh", "9": "sh", "0": "sz", "3": "sz", "8": "bj", "4": "bj"}.get(
        code[0], "sh"
    )
    symbol = f"{market}{code}"
    params = {
        "param": f"{symbol},day,{start:%Y-%m-%d},{end:%Y-%m-%d},8000,{adjust}"
    }
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(TENCENT_KLINE, params=params, headers=HEADERS, timeout=6)
            resp.raise_for_status()
            data = resp.json().get("data", {}).get(symbol, {})
            rows = data.get("qfqday") or data.get("day") or []
            if not rows:
                return _empty_frame()
            rows = [r[:6] for r in rows]  # 部分股票行尾带多余字段，只取前 6 列
            df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
            for col in ["open", "close", "high", "low", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])
            df["amount"] = 0.0
            df["pct"] = 0.0
            return df[["date"] + KEEP_COLS].set_index("date").sort_index()
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(2 ** attempt, 5) + random.random())
    return _empty_frame()


def _baostock_login():
    with _BS_LOCK:
        if not _BS["logged_in"]:
            import baostock as bs

            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
            _BS["bs"] = bs
            _BS["logged_in"] = True


def _baostock_logout():
    with _BS_LOCK:
        if _BS["logged_in"]:
            try:
                _BS["bs"].logout()
            except Exception:
                pass
            _BS["logged_in"] = False
            _BS["calls"] = 0


def _fetch_baostock(code: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Baostock 日 K 线（前复权，批量历史下载专用数据源）。"""
    market = {"6": "sh", "9": "sh", "0": "sz", "3": "sz", "8": "bj", "4": "bj"}.get(
        code[0], "sh"
    )
    symbol = f"{market}.{code}"
    for attempt in range(3):
        try:
            _baostock_login()
            # 服务器会在约 500 次查询后断连，主动定期重连
            if _BS["calls"] >= BS_RECONNECT_EVERY:
                _baostock_logout()
                _baostock_login()
            _BS["calls"] += 1
            rs = _BS["bs"].query_history_k_data_plus(
                symbol,
                "date,open,high,low,close,volume,amount",
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
                frequency="d",
                adjustflag="2",
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rows:
                df = pd.DataFrame(
                    rows, columns=["date", "open", "high", "low", "close", "volume", "amount"]
                )
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df["date"] = pd.to_datetime(df["date"])
                df["pct"] = 0.0
                return df[["date"] + KEEP_COLS].set_index("date").sort_index()
        except Exception:
            pass
        # 连接异常或空结果：重新登录后重试
        _baostock_logout()
        time.sleep(2.0 + attempt)
    return _empty_frame()


def fetch_index(
    code: str,
    start: datetime,
    end: datetime,
    cache_dir: str | None = None,
) -> pd.Series:
    """下载指数日线收盘价（Baostock，如沪深300 sh.000300），返回收盘价序列。"""
    symbol = f"sh.{code}"
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, f"{code}.csv")
        if os.path.exists(cache_path):
            try:
                cached = pd.read_csv(cache_path, index_col=0, parse_dates=True)["close"]
                if (
                    len(cached)
                    and cached.index.max() + pd.Timedelta(days=7) >= pd.Timestamp(end)
                ):
                    return cached.loc[start:end]
            except Exception:
                pass
    try:
        _baostock_login()
        rs = _BS["bs"].query_history_k_data_plus(
            symbol,
            "date,close",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="3",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return pd.Series(dtype=float)
        df = pd.DataFrame(rows, columns=["date", "close"])
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["date"] = pd.to_datetime(df["date"])
        series = df.set_index("date")["close"].sort_index()
        if cache_dir:
            series.to_csv(cache_path, header=["close"])
        return series
    except Exception:
        return pd.Series(dtype=float)


def fetch_stock_meta(cache_dir: str | None = None) -> dict:
    """获取全部股票的行业分类 / ST 标记 / 上市日期（Baostock，本地缓存）。"""
    if cache_dir:
        cache_path = os.path.join(cache_dir, "stock_meta.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    try:
        _baostock_login()
        rs = _BS["bs"].query_stock_industry()
        industry: dict[str, str] = {}
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            industry[row[1].split(".")[-1]] = row[3]
        rs2 = _BS["bs"].query_stock_basic()
        meta: dict = {}
        while rs2.error_code == "0" and rs2.next():
            row = rs2.get_row_data()
            if row[4] != "1":  # 仅股票（type=1）
                continue
            code = row[0].split(".")[-1]
            meta[code] = {
                "industry": industry.get(code, ""),
                "st": "ST" in row[1],
                "ipo_date": row[2],
            }
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        print(f"  股票元数据 {len(meta)} 条（行业/ST/上市日期）")
        return meta
    except Exception:
        return {}


def fetch_kline(
    code: str,
    start: datetime,
    end: datetime,
    adjust: str = "qfq",
    secid: str | None = None,
    source: str = "auto",
) -> pd.DataFrame:
    """下载单只股票日 K 线：东方财富优先，失败自动回退腾讯。"""
    df = _empty_frame()
    if source in ("auto", "em"):
        df = _fetch_em(code, start, end, adjust, secid)
    if len(df) == 0 and source in ("auto", "tencent"):
        df = _fetch_tencent(code, start, end, adjust)
    if len(df) == 0 and source in ("auto", "baostock"):
        df = _fetch_baostock(code, start, end)
    return df


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=KEEP_COLS)


def load_universe(path: str) -> list[dict]:
    """读取股票池配置文件。"""
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg["codes"]


def _download_one(
    item: dict,
    start: datetime,
    end: datetime,
    cache_dir: str,
    use_cache: bool,
    start_s: str,
    end_s: str,
    source: str = "auto",
) -> tuple[str, pd.DataFrame] | None:
    code = item["code"]
    cache_path = os.path.join(cache_dir, f"{code}.csv")
    df = None
    if use_cache and os.path.exists(cache_path):
        try:
            cached = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            # 只要求终点覆盖：上市晚的股票起点天然晚于请求起点，缓存仍有效
            if (
                len(cached)
                and cached.index.max() + pd.Timedelta(days=7) >= pd.Timestamp(end_s)
            ):
                df = cached
        except Exception:
            df = None
    if df is None:
        df = fetch_kline(code, start, end, source=source)
        if len(df):
            df.to_csv(cache_path)
        time.sleep(REQUEST_GAP + random.random() * 0.4)
    if len(df):
        return code, df.loc[start_s:end_s]
    return None


def download_universe(
    codes: list[dict],
    start: datetime,
    end: datetime,
    cache_dir: str,
    use_cache: bool = True,
    workers: int = 8,
    source: str = "auto",
) -> dict[str, pd.DataFrame]:
    """并行下载整个股票池的行情，返回 {code: DataFrame}。"""
    os.makedirs(cache_dir, exist_ok=True)
    out: dict[str, pd.DataFrame] = {}
    start_s, end_s = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    pkl_path = os.path.join(os.path.dirname(cache_dir.rstrip(os.sep)), "cache_all.pkl")
    wanted = {item["code"] for item in codes}
    if use_cache and os.path.exists(pkl_path):
        try:
            with open(pkl_path, "rb") as f:
                all_dfs = pickle.load(f)
            out = {
                c: df.loc[start_s:end_s]
                for c, df in all_dfs.items()
                if c in wanted and len(df.loc[start_s:end_s])
            }
            if len(out) >= len(wanted) * 0.95:
                print(f"  从合并缓存加载 {len(out)} 只", flush=True)
                return out
        except Exception:
            pass
    pending = list(codes)
    for round_no in range(MAX_PASSES):
        if not pending:
            break
        failed: list[dict] = []
        done = 0
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _download_one, item, start, end, cache_dir, use_cache, start_s, end_s, source
                ): item
                for item in pending
            }
            for fut in as_completed(futures):
                item = futures[fut]
                try:
                    result = fut.result()
                except Exception:
                    result = None
                with lock:
                    if result is None:
                        failed.append(item)
                    else:
                        code, df = result
                        out[code] = df
                        done += 1
                        if done % 50 == 0:
                            print(f"  下载进度：{done}/{len(pending)} …", flush=True)
        if failed and round_no < MAX_PASSES - 1:
            print(f"  第 {round_no + 2} 轮重试剩余 {len(failed)} 只，等待 30 秒后继续…", flush=True)
            time.sleep(30)
        pending = failed
    for item in pending:
        name = item.get("name", item["code"])
        print(f"  跳过 {name}({item['code']})：多次下载仍无数据")
    if len(out):
        try:
            with open(pkl_path, "wb") as f:
                pickle.dump(out, f)
        except Exception:
            pass
    return out
