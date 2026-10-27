# -*- coding: utf-8 -*-
"""单品种「价格 + 趋势评分」双轴复盘图（Plotly）。

数据来源：data/history.csv、data/history2.csv（由盘中任务写入并提交到仓库）。

显示规则：
- 横轴只显示有行情的交易时段（午休、收盘后、隔夜都不占位置）
- 价格线按动力系统三色分段；趋势评分为紫色加粗虚线
- 1天 / 1周 / 1月 / 3月 / 全部 按钮的时间范围与周K聚合规则一致：
  夜盘（21:00 起）算作下一个交易日，所以"1天"= 前一日夜盘 + 当日日盘

用法：
    python plot.py --list
    python plot.py --name 螺纹钢
    python plot.py --name 螺纹钢 --start 2026-10-01 --end 2026-10-31
    python plot.py --all
"""
import argparse
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import config as cfg
import history

POWER_COLORS = cfg.POWER_COLORS
SCORE_COLOR = "#8e44ad"


def load(data_dir=None):
    return history.read_csv_all(data_dir)


def _trading_day(ts: pd.Series) -> pd.Series:
    """21:00 之后属于下一个交易日。"""
    add = (ts.dt.hour >= 21).astype(int)
    return (ts + pd.to_timedelta(add, unit="D")).dt.normalize()


def build_figure(df, name, start=None, end=None):
    d = df[df["品种"] == name].copy()
    d["时间"] = pd.to_datetime(d["时间"])
    if start:
        d = d[d["时间"] >= pd.Timestamp(start)]
    if end:
        d = d[d["时间"] <= pd.Timestamp(end) + pd.Timedelta(days=1)]
    d = d.sort_values("时间").reset_index(drop=True)
    if d.empty:
        raise SystemExit(f"{name} 在指定区间没有历史数据")

    d["动力系统"] = d["动力系统"].fillna("蓝色")
    d["td"] = _trading_day(d["时间"])
    x = d["时间"].dt.strftime("%m-%d %H:%M").tolist()

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    # ---- 价格线：按动力系统三色分段 ----
    shown = set()
    start_i = 0
    for i in range(1, len(d) + 1):
        if i == len(d) or d.loc[i, "动力系统"] != d.loc[start_i, "动力系统"]:
            lo = max(start_i - 1, 0)
            seg = d.iloc[lo:i]
            color_name = d.loc[start_i, "动力系统"]
            color = POWER_COLORS.get(color_name, "#7f8c8d")
            fig.add_trace(
                go.Scatter(
                    x=x[lo:i], y=seg["价格"], mode="lines",
                    line=dict(color=color, width=2),
                    name=color_name,
                    legendgroup=color_name,
                    showlegend=color_name not in shown,
                    hovertemplate="%{x}<br>价格 %{y}<extra></extra>",
                ),
                secondary_y=False,
            )
            shown.add(color_name)
            start_i = i

    # ---- 趋势评分：右轴，紫色加粗虚线 ----
    fig.add_trace(
        go.Scatter(
            x=x, y=d["趋势评分"], mode="lines",
            line=dict(color=SCORE_COLOR, width=2.5, dash="dash"),
            name="趋势评分",
            hovertemplate="%{x}<br>评分 %{y}<extra></extra>",
        ),
        secondary_y=True,
    )
    fig.add_hline(y=0, line_width=0.5, line_dash="dot", line_color="#cccccc", secondary_y=True)

    # ---- 时间范围按钮：与交易日/周对齐 ----
    td = d["td"]
    last_td = td.iloc[-1]

    def first_idx(mask):
        idx = np.where(mask.to_numpy())[0]
        return int(idx[0]) if len(idx) else 0

    week_start = last_td - pd.Timedelta(days=last_td.weekday())
    ranges = [
        ("1天", first_idx(td == last_td)),
        ("1周", first_idx(td >= week_start)),
        ("1月", first_idx(td >= last_td - pd.Timedelta(days=30))),
        ("3月", first_idx(td >= last_td - pd.Timedelta(days=90))),
        ("全部", 0),
    ]
    buttons = [
        dict(label=label, method="relayout",
             args=[{"xaxis.range": [x[i0], x[-1]]}])
        for label, i0 in ranges
    ]

    title = f"{name} 价格与趋势评分（{d['时间'].min():%Y-%m-%d} ~ {d['时间'].max():%Y-%m-%d}）"
    fig.update_layout(
        title=dict(text=title, x=0.5),
        height=640,
        margin=dict(l=60, r=60, t=110, b=40),
        hovermode="x unified",
        template="plotly_white",
        updatemenus=[dict(
            type="buttons", direction="right",
            x=0, y=1.16, xanchor="left", yanchor="top",
            pad=dict(r=4, t=2), bgcolor="#ffffff", bordercolor="#dddddd",
            buttons=buttons,
        )],
        legend=dict(orientation="h", x=1, y=1.02, xanchor="right", yanchor="bottom",
                    bgcolor="rgba(255,255,255,0.6)"),
    )
    fig.update_yaxes(title_text="当前价格", secondary_y=False)
    fig.update_yaxes(title_text="趋势评分", secondary_y=True, range=[-110, 110])
    fig.update_xaxes(type="category", nticks=10, tickangle=-30,
                     showspikes=True, spikemode="across", spikethickness=1)
    if start or end:
        fig.update_xaxes(range=[x[0], x[-1]])
    return fig


def main():
    p = argparse.ArgumentParser(description="单品种价格+评分双轴复盘图")
    p.add_argument("--name", help="品种名，如 螺纹钢")
    p.add_argument("--start", help="开始日期 YYYY-MM-DD")
    p.add_argument("--end", help="结束日期 YYYY-MM-DD")
    p.add_argument("--out", help="输出 HTML 路径")
    p.add_argument("--data", help="CSV 目录，默认 ./data")
    p.add_argument("--all", action="store_true", help="为所有品种各生成一张")
    p.add_argument("--list", action="store_true", help="列出 CSV 里可用的品种")
    args = p.parse_args()

    df = load(args.data)
    if df.empty:
        raise SystemExit(f"没有历史数据，请确认 {args.data or cfg.DATA_DIR} 下有 history.csv / history2.csv")

    names = sorted(df["品种"].dropna().unique().tolist())
    if args.list:
        print("可用品种：", "、".join(names))
        print(f"时间范围：{df['时间'].min()} ~ {df['时间'].max()}，共 {len(df)} 条")
        return

    targets = names if args.all else ([args.name] if args.name else [])
    if not targets:
        p.error("请用 --name 指定品种，或 --all 生成全部；--list 查看可用品种")

    outdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "charts")
    os.makedirs(outdir, exist_ok=True)
    for name in targets:
        if name not in names:
            print(f"[跳过] CSV 里没有 {name}")
            continue
        fig = build_figure(df, name, args.start, args.end)
        out = args.out if (args.out and not args.all) else os.path.join(outdir, f"{name}.html")
        fig.write_html(out, include_plotlyjs=True, full_html=True)
        print(f"[OK] {name} -> {out}")


if __name__ == "__main__":
    main()
