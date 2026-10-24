# -*- coding: utf-8 -*-
"""行情数据封装（akshare / 新浪期货接口）。

对外提供：
- get_quote(code)   实时快照：最新价、当前主力合约名、昨结算、持仓量
- get_daily(code)   主力连续日线：date/open/high/low/close/volume/oi/settle
- get_settle(code, date) 指定交易日结算价

数据源实测（2026-10）：
- ak.futures_main_sina("V0") 可用，列名：日期/开盘价/最高价/最低价/收盘价/成交量/持仓量/动态结算价
- ak.futures_zh_spot("V0", market="CF") 可用，返回英文列 current_price / last_settle_price
- 主力合约名需查新浪 Market_Center 节点（见 config.FUTURES_MARK）
"""
import json
import logging
import time
import urllib.request

import numpy as np
import pandas as pd

import config as cfg

logger = logging.getLogger(__name__)

SINA_MARKET_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   "Market_Center.getHQFuturesData")

_DAILY_CACHE = {}   # code -> (ts, DataFrame)
_QUOTE_CACHE = {}   # code -> (ts, dict)
_CACHE_TTL = 90     # 秒


def _cached(store, key):
    hit = store.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def _sina_market(node):
    """新浪期货品种节点行情（返回该品种全部合约）。"""
    url = f"{SINA_MARKET_URL}?page=1&sort=position&asc=0&node={node}&base=futures"
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0",
    })
    raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", "replace")
    return json.loads(raw) if raw.strip() else []


def get_quote(code, with_contract=True):
    """实时快照。

    返回 dict：price / settle / prev_settle / oi / volume / name / contract / time
    取不到返回 None。
    """
    if not code:
        return None
    cached = _cached(_QUOTE_CACHE, code)
    if cached is not None:
        return cached

    node = cfg.FUTURES_MARK.get(code)
    out = None
    if node and with_contract:
        try:
            rows = _sina_market(node)
            if rows:
                df = pd.DataFrame(rows)
                for col in ("trade", "settlement", "presettlement", "position", "volume", "open", "high", "low"):
                    df[col] = pd.to_numeric(df.get(col), errors="coerce")
                cont = df[df["symbol"] == code]
                base = cont.iloc[0] if len(cont) else df.iloc[0]
                # 主力合约 = 非连续合约中持仓量最大者
                real = df[df["symbol"] != code].sort_values("position", ascending=False)
                contract = str(real.iloc[0]["symbol"]) if len(real) else None
                out = {
                    "price": float(base["trade"]) if pd.notna(base["trade"]) else None,
                    "settle": float(base["settlement"]) if pd.notna(base["settlement"]) else None,
                    "prev_settle": float(base["presettlement"]) if pd.notna(base["presettlement"]) else None,
                    "oi": float(base["position"]) if pd.notna(base["position"]) else None,
                    "volume": float(base["volume"]) if pd.notna(base["volume"]) else None,
                    "open": float(base["open"]) if pd.notna(base["open"]) else None,
                    "high": float(base["high"]) if pd.notna(base["high"]) else None,
                    "low": float(base["low"]) if pd.notna(base["low"]) else None,
                    "tradedate": str(base.get("tradedate", "")),
                    "name": str(base["name"]),
                    "contract": contract,
                    "time": str(base.get("ticktime", "")),
                    "source": "sina_market",
                }
        except Exception as e:
            logger.warning(f"{code} 新浪节点行情失败({node}): {e}")

    if out is None or out.get("price") is None:
        # 降级：akshare 实时快照（只给价格，给不出合约名）
        try:
            import akshare as ak
            df = ak.futures_zh_spot(symbol=code, market="CF", adjust="0")
            row = df.iloc[0]
            out = {
                "price": float(row["current_price"]),
                "settle": None,
                "prev_settle": float(row["last_settle_price"]),
                "oi": float(row["hold"]),
                "volume": float(row["volume"]),
                "name": str(row["symbol"]),
                "contract": (out or {}).get("contract"),
                "time": str(row.get("time", "")),
                "source": "akshare_spot",
            }
        except Exception as e:
            logger.warning(f"{code} 实时快照失败: {e}")
            return None

    _QUOTE_CACHE[code] = (time.time(), out)
    return out


_COLMAP = {
    "日期": "date", "开盘价": "open", "最高价": "high", "最低价": "low",
    "收盘价": "close", "成交量": "volume", "持仓量": "oi",
    "动态结算价": "settle", "结算价": "settle",
}


def get_daily(code, bars=None, tries=3):
    """主力连续日线（含结算价/持仓量）。失败返回 None。"""
    if not code:
        return None
    cached = _cached(_DAILY_CACHE, code)
    if cached is not None:
        return cached
    bars = bars or cfg.DAILY_BARS

    import akshare as ak
    df = None
    for i in range(tries):
        try:
            raw = ak.futures_main_sina(symbol=code)
            if raw is None or raw.empty:
                raise ValueError("返回空数据")
            df = raw.rename(columns=_COLMAP)
            keep = [c for c in ("date", "open", "high", "low", "close", "volume", "oi", "settle") if c in df.columns]
            df = df[keep].copy()
            if "date" not in df.columns or "close" not in df.columns:
                raise ValueError(f"缺少必要列，实际列：{list(raw.columns)}")
            df["date"] = pd.to_datetime(df["date"])
            df = (df.dropna(subset=["close"])
                    .sort_values("date")
                    .drop_duplicates("date")
                    .reset_index(drop=True))
            # 结算价为 0 视为缺失（新浪老数据为 0）
            if "settle" in df.columns:
                df.loc[df["settle"] <= 0, "settle"] = np.nan
            break
        except Exception as e:
            logger.warning(f"{code} 日线第{i + 1}次获取失败: {e}")
            df = None
            time.sleep(2 + i)
    if df is None or df.empty:
        return None

    if len(df) > bars:
        df = df.tail(bars).reset_index(drop=True)
    _DAILY_CACHE[code] = (time.time(), df)
    return df


def latest_settle(df, target_date=None):
    """取 target_date 当日结算价；没有则返回最近一个有结算价的交易日。"""
    if df is None or df.empty or "settle" not in df.columns:
        return None
    sub = df.dropna(subset=["settle"])
    if sub.empty:
        return None
    if target_date is not None:
        day = pd.Timestamp(target_date).normalize()
        hit = sub[sub["date"].dt.normalize() == day]
        if len(hit):
            return float(hit.iloc[-1]["settle"])
        return None
    return float(sub.iloc[-1]["settle"])


def prev_settle(df, before_date):
    """取 before_date 之前（不含当日）最近一个交易日的结算价。"""
    if df is None or df.empty or "settle" not in df.columns:
        return None
    day = pd.Timestamp(before_date).normalize()
    sub = df[(df["date"].dt.normalize() < day)].dropna(subset=["settle"])
    if sub.empty:
        return None
    return float(sub.iloc[-1]["settle"])


def clear_cache():
    _DAILY_CACHE.clear()
    _QUOTE_CACHE.clear()
