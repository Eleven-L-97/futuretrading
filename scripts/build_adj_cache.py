# -*- coding: utf-8 -*-
"""构建前复权主连缓存（东方财富数据，本地跑）。

东方财富的接口在境外（GitHub Actions）会被断开，国内可以访问，
所以由本地定期刷新，把结果存成 data/adj/<品种>.csv 提交到仓库；
线上盯盘直接读缓存，再用新浪补当天缺失的日线。

用法：
    python scripts/build_adj_cache.py            # 全部品种
    python scripts/build_adj_cache.py 玉米 PVC    # 指定品种
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak
import config as cfg

ADJ_DIR = os.path.join(cfg.DATA_DIR, "adj")


def build(names=None):
    from feishu import Feishu, as_text
    fs = Feishu()
    all_names = [as_text(r.get(cfg.MONITOR_FIELDS["name"])) for r in fs.read("monitor")]
    target = names or all_names
    os.makedirs(ADJ_DIR, exist_ok=True)

    ok, fail = 0, []
    for name in target:
        symbol = cfg.EM_ALIAS.get(name, name) + "主连"
        try:
            df = ak.futures_hist_em(symbol=symbol, period="daily",
                                    start_date="20220101", end_date="20500101")
            if df is None or df.empty:
                raise ValueError("空数据")
            df = df.rename(columns={
                "时间": "date", "开盘": "open", "最高": "high", "最低": "low",
                "收盘": "close", "成交量": "volume", "持仓量": "oi",
            })
            df = df[["date", "open", "high", "low", "close", "volume", "oi"]].copy()
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            for c in ("open", "high", "low", "close", "volume", "oi"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["close"]).drop_duplicates("date").sort_values("date")
            path = os.path.join(ADJ_DIR, f"{name}.csv")
            df.to_csv(path, index=False, encoding="utf-8-sig")
            print(f"[OK] {name:6} {len(df):4d} 行  {df.iloc[-1]['date']}  {df.iloc[-1]['close']}")
            ok += 1
        except Exception as e:
            print(f"[FAIL] {name}: {type(e).__name__} {e}")
            fail.append(name)
    print(f"\n完成 {ok} 个，失败 {len(fail)} 个：{fail}")


if __name__ == "__main__":
    build(sys.argv[1:] or None)
