# -*- coding: utf-8 -*-
"""换月检查：主力合约变了就自动调整前复权缓存。

背景：评分用的是 data/adj/<品种>.csv（东财前复权主连，本地构建）。
东财接口在境外被断开，所以换月后不能重新拉取；这里用新浪的「单合约日线」
自己算价差，把历史部分平移，再补上新合约的日线，保证序列连续。

流程：
1. 读监控表 + 实时快照，拿到每个品种当前的主力合约
2. 和 data/contracts.json 里记录的合约比较
3. 对换月的品种：
   - 拉新旧两个合约的日线
   - 找换月日 D（新合约持仓量首次超过旧合约且之后不再反转）
   - gap = 新合约收盘(D) - 旧合约收盘(D)
   - 缓存中 D 之前的所有行 +gap（前复权平移）
   - 补上 D 之前的旧合约行、D 之后的新合约行
4. 写回缓存和 contracts.json

用法：python scripts/roll_check.py [--dry-run]
"""
import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import akshare as ak

import akdata as akd
import config as cfg
from feishu import Feishu, as_text

ADJ_DIR = os.path.join(cfg.DATA_DIR, "adj")
CONTRACT_FILE = os.path.join(cfg.DATA_DIR, "contracts.json")
COLS = ["date", "open", "high", "low", "close", "volume", "oi"]


def sina_contract(code):
    """新浪单合约日线 -> 统一列名。"""
    raw = ak.futures_zh_daily_sina(symbol=code)
    if raw is None or raw.empty:
        raise ValueError(f"{code} 无数据")
    df = raw.rename(columns={
        "date": "date", "open": "open", "high": "high", "low": "low",
        "close": "close", "volume": "volume", "hold": "oi",
    })
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume", "oi"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[COLS].dropna(subset=["close"]).sort_values("date").reset_index(drop=True)


def find_switch(old_df, new_df):
    """换月日：旧合约最后一次持仓量占优的次日（之后新合约一直占优）。"""
    m = old_df[["date", "oi"]].merge(new_df[["date", "oi"]], on="date", suffixes=("_old", "_new"))
    m = m.sort_values("date").reset_index(drop=True)
    last_old_lead = None
    for i in range(len(m)):
        if m.loc[i, "oi_old"] > m.loc[i, "oi_new"]:
            last_old_lead = i
    if last_old_lead is not None and last_old_lead + 1 < len(m):
        return m.loc[last_old_lead + 1, "date"]
    # 旧合约从未占优（例如缓存起始太晚）：退回第一次新 > 旧
    for i in range(len(m)):
        if m.loc[i, "oi_new"] > m.loc[i, "oi_old"]:
            return m.loc[i, "date"]
    return None


def adjust_cache(name, old_code, new_code, dry_run=False):
    path = os.path.join(ADJ_DIR, f"{name}.csv")
    if not os.path.exists(path):
        raise ValueError(f"缺少缓存 {path}")
    cache = pd.read_csv(path, encoding="utf-8-sig")
    cache["date"] = pd.to_datetime(cache["date"])

    old_df = sina_contract(old_code)
    new_df = sina_contract(new_code)
    switch = find_switch(old_df, new_df)
    if switch is None:
        raise ValueError(f"{old_code}/{new_code} 找不到换月日")

    old_row = old_df[old_df["date"] == switch]
    new_row = new_df[new_df["date"] == switch]
    if old_row.empty or new_row.empty:
        raise ValueError(f"换月日 {switch.date()} 两个合约没有同日数据")
    gap = float(new_row["close"].iloc[0]) - float(old_row["close"].iloc[0])

    # 历史部分（换月日之前）整体平移
    hist = cache[cache["date"] < switch].copy()
    hist[["open", "high", "low", "close"]] = hist[["open", "high", "low", "close"]] + gap

    # 拼接：平移后的历史 + 旧合约(换月日之前、缓存没覆盖到的) + 新合约(换月日起)
    fill_old = old_df[(old_df["date"] >= cache["date"].max()) & (old_df["date"] < switch)][COLS].copy()
    fill_old[["open", "high", "low", "close"]] += gap
    tail_new = new_df[new_df["date"] >= switch][COLS].copy()

    out = pd.concat([hist[COLS], fill_old, tail_new], ignore_index=True)
    out = out.drop_duplicates("date").sort_values("date").reset_index(drop=True)

    # 连续性检查：换月日不应再有跳空
    prev = out[out["date"] < switch]["close"].tail(1)
    cur = out[out["date"] == switch]["close"]
    if len(prev) and len(cur):
        jump = abs(float(cur.iloc[0]) - float(prev.iloc[0])) / max(float(prev.iloc[0]), 1e-9)
        if jump > 0.03:
            raise ValueError(f"调整后仍有 {jump:.1%} 跳空，放弃写入")

    if not dry_run:
        out["date"] = out["date"].dt.strftime("%Y-%m-%d")
        out.to_csv(path, index=False, encoding="utf-8-sig")
    return switch, gap, len(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    fs = Feishu()
    rows = fs.read("monitor")
    old_map = {}
    if os.path.exists(CONTRACT_FILE):
        with open(CONTRACT_FILE, encoding="utf-8") as f:
            old_map = json.load(f)

    changed, errors = [], []
    for r in rows:
        name = as_text(r.get(cfg.MONITOR_FIELDS["name"]))
        code = as_text(r.get(cfg.MONITOR_FIELDS["code"]))
        if not name or not code:
            continue
        quote = akd.get_quote(code)
        cur = (quote or {}).get("contract")
        if not cur:
            continue
        prev = old_map.get(name)
        if prev and prev != cur:
            try:
                switch, gap, n = adjust_cache(name, prev, cur, dry_run=args.dry_run)
                changed.append((name, prev, cur, switch.date(), round(gap, 1), n))
                old_map[name] = cur
            except Exception as e:
                errors.append((name, prev, cur, str(e)))
        else:
            old_map[name] = cur

    if not args.dry_run:
        with open(CONTRACT_FILE, "w", encoding="utf-8") as f:
            json.dump(old_map, f, ensure_ascii=False, indent=2)

    print(f"检查 {len(old_map)} 个品种")
    for item in changed:
        print(f"[换月] {item[0]}: {item[1]} -> {item[2]}  换月日 {item[3]}  价差 {item[4]}  共 {item[5]} 行")
    for item in errors:
        print(f"[失败] {item[0]}: {item[1]} -> {item[2]}  {item[3]}")
    print(f"完成：换月 {len(changed)} 个，失败 {len(errors)} 个")


if __name__ == "__main__":
    main()
