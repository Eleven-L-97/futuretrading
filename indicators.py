# -*- coding: utf-8 -*-
"""技术指标与周K合成。

周K合成规则（对应需求文档）：
- 周起点：周一
- 本周开 = 本周第一根日线的开盘；高/低 = 本周极值；收 = 最新一根日线收盘
- 本周量 = 日线成交量累加；本周持仓量 = 最后一根日线持仓量（时点数，不累加）
- 本周尚未结束时，最后一根即为「未完成周」，用最新价参与计算（滚动周K）

所有周期参数都在 config.py 的「指标参数」区域。
"""
import numpy as np
import pandas as pd

import config as cfg

WEEK_START = cfg.WEEK_START_WEEKDAY   # 0=周一


# ---------------------------------------------------------------- 基础指标
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast=None, slow=None, sig=None):
    fast = cfg.MACD_FAST if fast is None else fast
    slow = cfg.MACD_SLOW if slow is None else slow
    sig = cfg.MACD_SIGNAL if sig is None else sig
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, sig)
    hist = (dif - dea) * 2          # 国内常用 ×2，只用于判断方向/斜率
    return dif, dea, hist


def dmi(df: pd.DataFrame, n=None, m=None):
    """DMI：+DI / -DI / ADX。

    与同花顺公式一致：
      TR1 = SUM(MAX(MAX(H-L, ABS(H-REF(C,1))), ABS(L-REF(C,1))), N)
      DMP = SUM(IF(HD>0 AND HD>LD, HD, 0), N)     HD = H - REF(H,1)
      DMM = SUM(IF(LD>0 AND LD>HD, LD, 0), N)     LD = REF(L,1) - L
      DI1 = DMP*100/TR1 ; DI2 = DMM*100/TR1
      ADX = MA(ABS(DI2-DI1)/(DI2+DI1)*100, M)
    """
    n = cfg.DMI_N if n is None else n
    m = cfg.DMI_M if m is None else m

    h, l, c = df["high"], df["low"], df["close"]
    hd = h.diff()
    ld = -l.diff()                                    # REF(L,1) - L
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)

    tr_sum = tr.rolling(n, min_periods=1).sum().replace(0, np.nan)
    dmp = pd.Series(np.where((hd > 0) & (hd > ld), hd, 0.0), index=df.index)
    dmm = pd.Series(np.where((ld > 0) & (ld > hd), ld, 0.0), index=df.index)
    dmp_sum = dmp.rolling(n, min_periods=1).sum()
    dmm_sum = dmm.rolling(n, min_periods=1).sum()

    pdi = 100 * dmp_sum / tr_sum
    ndi = 100 * dmm_sum / tr_sum
    dx = 100 * (ndi - pdi).abs() / (ndi + pdi).replace(0, np.nan)
    adx = dx.fillna(0).rolling(m, min_periods=1).mean()
    return pdi.fillna(0), ndi.fillna(0), adx.fillna(0)


def power_indicator(close: pd.Series, volume: pd.Series):
    """强力指标 = (收盘 - 前收) × 成交量；快线 2 期 EMA，慢线 13 期 EMA。"""
    pi = close.diff().fillna(0) * volume.fillna(0)
    return pi, ema(pi, cfg.STRONG_FAST), ema(pi, cfg.STRONG_SLOW)


def hpi(df: pd.DataFrame, cf=None, span=None):
    """赫里克回报指数 HPI（与同花顺公式一致，用 EMA 平滑）。

    K = cf × Volume × (M - M_prev) × (1 ± OI_Ratio)
    HPI = EMA(K, span)

    OI_Ratio = 2 × |OI - OI_prev| / max(OI, OI_prev)；M 下跌时取负号。
    """
    cf = cfg.HPI_CF if cf is None else cf
    span = cfg.HPI_SPAN if span is None else span

    m = (df["high"] + df["low"]) / 2
    dm = m.diff().fillna(0)
    oi = df["oi"].astype(float)
    oi_use = oi.where(oi > 0, oi.shift(1))
    oi_prev = oi_use.shift(1)
    denom = pd.concat([oi_use, oi_prev], axis=1).max(axis=1).replace(0, np.nan)
    oi_ratio = (2 * (oi_use - oi_prev).abs() / denom).fillna(0)
    sign = np.where(dm > 0, 1, -1)
    k = cf * df["volume"] * dm * (1 + oi_ratio * sign)
    return ema(k.fillna(0), span)


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
    e5, e10 = ema(c, 5), ema(c, 10)
    e12 = ema(c, cfg.POWER_EMA_PERIOD)
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
