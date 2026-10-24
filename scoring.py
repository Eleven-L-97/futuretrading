# -*- coding: utf-8 -*-
"""五维度趋势评分 + 动力系统三色。

要调权重/阈值，改下面这一块常量即可（都在本文件顶部）。

维度与分值（理论范围 -100 ~ +100）：
1. EMA5 vs EMA10                 ±W_EMA
2. 动力系统（MACD柱 + EMA斜率）   绿 +W_POWER / 蓝 0 / 红 -W_POWER
3. 强力指标 13 期慢线             >0 +W_STRONG / <0 -W_STRONG
4. DMI                          ADX<W_ADX_TH 记 0；ADX>阈值且上升 ±W_DMI_UP；下降 ±W_DMI_DOWN
5. HPI                          >0 且升 +W_HPI_UP；>0 且降 +W_HPI_DOWN；
                                <0 且升 -W_HPI_UP；<0 且降 -W_HPI_DOWN
"""
import logging

logger = logging.getLogger(__name__)

# ============ 评分权重（要调分数就改这里） ============
W_EMA = 10            # EMA5 vs EMA10
W_POWER = 20          # 动力系统 绿/红
W_STRONG = 20         # 强力指标慢线方向
W_ADX_TH = 20         # ADX 阈值：低于该值 DMI 不加分
W_DMI_UP = 30         # ADX 上升时的 DMI 分值
W_DMI_DOWN = 15       # ADX 下降时的 DMI 分值
W_HPI_UP = 20         # HPI 与价格同向（>0 升 / <0 降）
W_HPI_DOWN = 10       # HPI 与价格反向（>0 降 / <0 升）


def power_color(f):
    """动力系统三色：MACD 柱与 EMA 斜率同向。"""
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
    s += W_EMA if f["ema5"] > f["ema10"] else -W_EMA

    # 2. 动力系统
    col = power_color(f)
    s += {"绿色": W_POWER, "蓝色": 0, "红色": -W_POWER}[col]

    # 3. 强力指标慢线
    s += W_STRONG if f["slow"] > 0 else -W_STRONG

    # 4. DMI
    if f["adx"] < W_ADX_TH:
        pass
    elif f["adx_up"]:
        s += W_DMI_UP if f["pdi"] > f["ndi"] else -W_DMI_UP
    else:
        s += W_DMI_DOWN if f["pdi"] > f["ndi"] else -W_DMI_DOWN

    # 5. HPI
    if f["hpi"] > 0:
        s += W_HPI_UP if f["hpi_up"] else W_HPI_DOWN
    else:
        s += -W_HPI_UP if f["hpi_up"] else -W_HPI_DOWN

    if abs(s) > 100:
        logger.error(f"评分越界 {s}（理论范围 ±100），请检查维度是否重复计算")
    return int(s), col


def explain(f):
    """返回各维度得分明细，供 --mode check 核对。"""
    if f is None:
        return [], 0
    items = []

    e = W_EMA if f["ema5"] > f["ema10"] else -W_EMA
    items.append(("EMA", f"EMA5={f['ema5']:.2f} / EMA10={f['ema10']:.2f}", e))

    col = power_color(f)
    items.append(("动力系统", f"{col}（柱{'升' if f['hist_up'] else '降'}，EMA12{'升' if f['ema12_up'] else '降'}）",
                  {"绿色": W_POWER, "蓝色": 0, "红色": -W_POWER}[col]))

    items.append(("强力指标", f"13期慢线={f['slow']:.0f}", W_STRONG if f["slow"] > 0 else -W_STRONG))

    if f["adx"] < W_ADX_TH:
        d = 0
        desc = f"ADX={f['adx']:.2f} < {W_ADX_TH}，不加分"
    elif f["adx_up"]:
        d = W_DMI_UP if f["pdi"] > f["ndi"] else -W_DMI_UP
        desc = f"ADX={f['adx']:.2f} 上升，+DI={f['pdi']:.2f} / -DI={f['ndi']:.2f}"
    else:
        d = W_DMI_DOWN if f["pdi"] > f["ndi"] else -W_DMI_DOWN
        desc = f"ADX={f['adx']:.2f} 下降，+DI={f['pdi']:.2f} / -DI={f['ndi']:.2f}"
    items.append(("DMI", desc, d))

    if f["hpi"] > 0:
        h = W_HPI_UP if f["hpi_up"] else W_HPI_DOWN
        desc = f"HPI>0（{f['hpi']:.0f}）且{'上升' if f['hpi_up'] else '下降'}"
    else:
        h = -W_HPI_UP if f["hpi_up"] else -W_HPI_DOWN
        desc = f"HPI<0（{f['hpi']:.0f}）且{'上升' if f['hpi_up'] else '下降'}"
    items.append(("HPI", desc, h))

    return items, sum(x[2] for x in items)
