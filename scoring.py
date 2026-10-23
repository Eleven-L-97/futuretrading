# -*- coding: utf-8 -*-
"""五维度趋势评分 + 动力系统三色。

维度与分值（理论范围 -100 ~ +100）：
1. EMA5 vs EMA10                 ±10
2. 动力系统（MACD柱 + EMA12斜率） 绿 +20 / 蓝 0 / 红 -20
3. 强力指标 13 期慢线             >0 +20 / <0 -20
4. DMI                          ADX<20 记 0；ADX>20 且上升 ±30；ADX>20 且下降 ±15
5. HPI                          >0 且升 +20；>0 且降 +10；<0 且升 -20；<0 且降 -10
"""
import logging

logger = logging.getLogger(__name__)


def power_color(f):
    """动力系统三色：MACD 柱与 12 期 EMA 斜率同向。"""
    if f["hist_up"] and f["ema12_up"]:
        return "绿色"
    if f["hist_down"] and f["ema12_down"]:
        return "红色"
    return "蓝色"


def score(f):
    """返回 (总分, 颜色)。"""
    if f is None:
        return 0, "蓝色"

    s = 0
    # 1. EMA5 vs EMA10
    s += 10 if f["ema5"] > f["ema10"] else -10

    # 2. 动力系统
    col = power_color(f)
    s += {"绿色": 20, "蓝色": 0, "红色": -20}[col]

    # 3. 强力指标慢线
    s += 20 if f["slow"] > 0 else -20

    # 4. DMI
    if f["adx"] < 20:
        pass
    elif f["adx_up"]:
        s += 30 if f["pdi"] > f["ndi"] else -30
    else:
        s += 15 if f["pdi"] > f["ndi"] else -15

    # 5. HPI
    if f["hpi"] > 0:
        s += 20 if f["hpi_up"] else 10
    else:
        s += -20 if f["hpi_up"] else -10

    if abs(s) > 100:
        logger.error(f"评分越界 {s}（理论范围 ±100），请检查维度是否重复计算")
    return int(s), col
