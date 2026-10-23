# -*- coding: utf-8 -*-
"""技术指标与周K合成。

周K合成规则（对应需求文档）：
- 周起点：周一
- 本周开 = 本周第一根日线的开盘；高/低 = 本周极值；收 = 最新一根日线收盘
- 本周量 = 日线成交量累加；本周持仓量 = 最后一根日线持仓量（时点数，不累加）
- 本周尚未结束时，最后一根即为「未完成周」，用最新价参与计算（滚动周K）
"""
import numpy as np
import pandas as pd

import config as cfg

WEEK_START = cfg.WEEK_START_WEEKDAY   # 0=周一


# ---------------------------------------------------------------- 基础指标
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast=12, slow=26, sig=9):
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, sig)
    hist = (dif - dea) * 2          # 国内常用 ×2，只用于判断方向/斜率
    return dif, dea, hist


def dmi(df: pd.DataFrame, n=14):
    """DMI：+DI / -DI / ADX。"""
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff().fillna(0)
    dn = (-l.diff()).fillna(0)
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(n, min_periods=1).mean().replace(0, np.nan)
    pdi = 100 * plus.rolling(n, min_periods=1).mean() / atr
    ndi = 100 * minus.rolling(n, min_periods=1).mean() / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    adx = ema(dx.fillna(0), n)
    return pdi.fillna(0), ndi.fillna(0), adx.fillna(0)


def power_indicator(close: pd.Series, volume: pd.Series):
    """强力指标 = (收盘 - 前收) × 成交量；快线 2 期 EMA，慢线 13 期 EMA。"""
    pi = close.diff().fillna(0) * volume.fillna(0)
    return pi, ema(pi, 2), ema(pi, 13)


def hpi(df: pd.DataFrame, cf=None, s=None):
    """HPI（赫里克强力指数）周线版。

    K = cf × Volume × (M - M_prev) × (1 + OI_Ratio)   （M 下跌时 OI_Ratio 取负）
    HPI = HPI_prev + s × (K - HPI_prev)
    """
    cf = cfg.HPI_CF if cf is None else cf
    s = cfg.HPI_S if s is None else s
    m = (df["high"] + df["low"]) / 2
    dm = m.diff().fillna(0)
    oi = df["oi"].ffill()
    oi_chg = oi.diff().fillna(0).abs()
    denom = pd.concat([oi, oi.shift(1)], axis=1).max(axis=1).replace(0, np.nan)
    oi_ratio = (2 * oi_chg / denom).fillna(0).clip(upper=2)
    sign = np.where(dm >= 0, 1, -1)
    K = cf * df["volume"].fillna(0) * dm * (1 + oi_ratio * sign)
    v = K.to_numpy(dtype=float)
    out = np.zeros(len(v))
    if len(v):
        out[0] = v[0]
        for i in range(1, len(v)):
            out[i] = out[i - 1] + s * (v[i] - out[i - 1])
    return pd.Series(out, index=df.index)


# ---------------------------------------------------------------- 周K合成
def build_weekly(daily: pd.DataFrame, asof_date=None):
    """日线 -> 滚动周K。

    返回列：week_start, date_start, date_end, open, high, low, close, volume, oi,
            n_days, is_partial
    """
    if daily is None or len(daily) == 0:
        return None
    df = daily.copy()
    if asof_date is not None:
        df = df[df["date"] <= pd.Timestamp(asof_date)]
    if df.empty:
        return None
    df = df.sort_values("date")
    # 按「周起点」分组，避免 pandas Grouper 对 object 类型日期报错
    weekday = df["date"].dt.weekday
    offset = (weekday - WEEK_START) % 7
    df["week_start"] = (df["date"] - pd.to_timedelta(offset, unit="D")).dt.normalize()

    wk = df.groupby("week_start", as_index=False).agg(
        date_start=("date", "first"),
        date_end=("date", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        oi=("oi", "last"),
        n_days=("date", "size"),
    )
    wk = wk.sort_values("date_end").reset_index(drop=True)
    # 最后一根若覆盖到今天所在周，则标记为未完成周
    wk["is_partial"] = False
    if len(wk):
        last_week_start = wk.iloc[-1]["week_start"]
        today = pd.Timestamp.now().normalize()
        this_week_start = today - pd.Timedelta(days=(today.weekday() - WEEK_START) % 7)
        wk.loc[wk.index[-1], "is_partial"] = bool(last_week_start >= this_week_start)
    return wk


# ---------------------------------------------------------------- 打分特征
def week_features(wk: pd.DataFrame, idx=-1):
    """提取打分所需特征（默认最后一根周K）。"""
    if wk is None or len(wk) == 0:
        return None
    c, v = wk["close"], wk["volume"]
    e5, e10, e12 = ema(c, 5), ema(c, 10), ema(c, 12)
    _, _, hist = macd(c)
    pdi, ndi, adx = dmi(wk)
    _, _, slow = power_indicator(c, v)
    hp = hpi(wk)

    def cur(s):
        return float(s.iloc[idx])

    def up(s):
        return bool(len(s) >= 2 and float(s.iloc[idx]) > float(s.iloc[idx - 1]))

    def down(s):
        return bool(len(s) >= 2 and float(s.iloc[idx]) < float(s.iloc[idx - 1]))

    return {
        "ema5": cur(e5), "ema10": cur(e10), "ema12": cur(e12),
        "df": cur(ema(c, 12) - ema(c, 26)),
        "hist": cur(hist), "pdi": cur(pdi), "ndi": cur(ndi), "adx": cur(adx),
        "slow": cur(slow), "hpi": cur(hp),
        "hist_up": up(hist), "hist_down": down(hist),
        "ema12_up": up(e12), "ema12_down": down(e12),
        "adx_up": up(adx), "adx_down": down(adx),
        "hpi_up": up(hp), "hpi_down": down(hp),
    }
