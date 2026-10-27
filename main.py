# -*- coding: utf-8 -*-
"""程序入口。

用法：
    python main.py --mode realtime       # 日内盯盘：一次抓取全部品种并写回飞书（由定时任务每 5 分钟调用）
    python main.py --mode daily          # 18:00 收盘后：持仓归集 + 每日账户快照
    python main.py --mode monthly        # 月末：绩效指标（月/季/年）
    python main.py --mode all            # daily + monthly（月末批处理）
    python main.py --mode position       # 仅交易日志 -> 持仓归集
    python main.py --mode diagnose       # 自检：凭证、表权限、行情、字段解析

常用参数：
    --dry-run        只计算不写飞书
    --limit N        只处理前 N 个品种（调试用）
    --date YYYY-MM-DD 指定业务日期（补跑快照/绩效用）
    --force          忽略「月末」判断，强制计算绩效
"""
import argparse
import logging
import logging.handlers
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd

import akdata as akd
import config as cfg
import history as history_mod
import indicators as ind
import scoring as sc
from feishu import Feishu, FeishuError, as_ms, as_num, as_text, ms_to_day, now_ms, today as local_today
from position import sync as sync_positions
import daily_snapshot
import performance

M = cfg.MONITOR_FIELDS
H = cfg.HIST_FIELDS

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def setup_logging(verbose=False):
    os.makedirs(LOG_DIR, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, "run.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


logger = logging.getLogger("main")


def _week_start(ts):
    ts = pd.Timestamp(ts).normalize()
    return ts - pd.Timedelta(days=(ts.weekday() - cfg.WEEK_START_WEEKDAY) % 7)


def _daily_with_spot(daily, quote):
    """把快照所属交易日补成一根日线，让周K包含最新的夜盘/盘中时段。

    国内期货的夜盘属于**下一个交易日**：周五 21:00 的夜盘算下周一，
    同花顺的周K就是从周五夜盘开始画的。日线数据要等下一个交易日收盘才有，
    所以盘中/夜盘先用实时快照拼一根"当日"日线，保证周K与行情软件一致。
    """
    if daily is None or not quote:
        return daily
    td = quote.get("tradedate")
    if not td:
        return daily
    try:
        td = pd.Timestamp(td).normalize()
    except Exception:
        return daily
    last = daily["date"].max() if len(daily) else None
    if last is not None and td < last:
        return daily

    o, h, l, p = quote.get("open"), quote.get("high"), quote.get("low"), quote.get("price")
    if None in (o, h, l, p):
        return daily

    # 当天/当周那根日线还没收盘：用实时快照刷新它，避免用到滞后的收盘价
    if last is not None and td == last:
        i = daily.index[-1]
        daily.loc[i, "close"] = p
        if h is not None:
            daily.loc[i, "high"] = max(float(daily.loc[i, "high"] or h), h)
        if l is not None:
            daily.loc[i, "low"] = min(float(daily.loc[i, "low"] or l), l)
        if quote.get("volume"):
            daily.loc[i, "volume"] = quote["volume"]
        if quote.get("oi"):
            daily.loc[i, "oi"] = quote["oi"]
        return daily

    bar = pd.DataFrame([{
        "date": td, "open": o, "high": h, "low": l, "close": p,
        "volume": quote.get("volume") or 0, "oi": quote.get("oi") or 0,
        "settle": float("nan"),
    }])
    return pd.concat([daily, bar], ignore_index=True)


def realtime(fs, dry_run=False, limit=None, force_history=False):
    """日内一批：抓行情 -> 算分 -> 写监控表 -> 每 15 分钟追加历史（双表 + CSV）。"""
    rows = fs.read("monitor")
    if not rows:
        logger.warning("监控表为空")
        return 0
    if limit:
        rows = rows[:limit]

    def fetch(row):
        code = as_text(row.get(M["code"]))
        return row, (akd.get_quote(code) if code else None), (akd.get_daily(code) if code else None)

    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = list(pool.map(fetch, rows))

    ts_now = now_ms()
    today = local_today()
    updates, ok, fail = [], 0, 0
    buckets = {t: [] for t in cfg.HISTORY_SPLIT}

    due = force_history or history_mod.history_due(ts_now)
    if due and not dry_run:
        history_mod.clear_previous_weeks(fs)

    for row, quote, daily in fetched:
        name = as_text(row.get(M["name"]))
        code = as_text(row.get(M["code"]))
        if not code:
            logger.warning(f"{name} 无代码，跳过")
            fail += 1
            continue
        if not quote or quote.get("price") is None:
            logger.warning(f"{name}({code}) 取不到实时价，跳过")
            fail += 1
            continue

        fields = {M["price"]: float(quote["price"])}
        if quote.get("contract"):
            fields[M["contract"]] = quote["contract"]

        score_val, color = None, None
        daily = _daily_with_spot(daily, quote)
        if daily is not None:
            wk = ind.build_weekly(daily)
            feat = ind.week_features(wk)
            if feat:
                score_val, color = sc.score(feat)
                fields[M["score"]] = score_val
                fields[M["power"]] = color

        start_ms = as_ms(row.get(M["trend_start"]))
        if start_ms:
            weeks = (today - ms_to_day(start_ms)).days // 7
            if weeks >= 0:
                fields[M["trend_weeks"]] = int(weeks)

        fields[M["updated_at"]] = ts_now
        updates.append({"record_id": row["_id"], "fields": fields})

        if due and score_val is not None:
            mode = cfg.HISTORY_MODE
            changed = (as_text(row.get(M["score"])) != str(score_val)) or (as_text(row.get(M["power"])) != (color or ""))
            if mode == "always" or (mode == "change" and changed):
                tname = history_mod.table_for(name)
                if tname is None:
                    logger.warning(f"{name} 不在任何历史表品种清单内，跳过历史追加")
                else:
                    buckets[tname].append(history_mod.make_record(
                        name, quote.get("contract"), quote["price"], score_val, color, ts_now))
        ok += 1

    n_hist = sum(len(v) for v in buckets.values())
    logger.info(f"实时盯盘：成功 {ok} 个，失败 {fail} 个，待更新 {len(updates)} 条；"
                f"历史{'到点' if due else '未到点'}，本次写入 {n_hist} 条")
    if dry_run:
        for u in updates[:3]:
            logger.info(f"[dry-run] update {u}")
        for tname, recs in buckets.items():
            for r in recs[:2]:
                logger.info(f"[dry-run] {tname} {r}")
        return ok

    if updates:
        fs.batch_update(cfg.TABLES["monitor"], updates)

    if n_hist:
        # 先落 CSV（本地/仓库归档），再写飞书；飞书写失败不丢数据
        history_mod.append_csv(buckets)
        for tname, recs in buckets.items():
            if recs:
                try:
                    fs.batch_create(cfg.TABLES[tname], [history_mod.to_feishu_row(r) for r in recs])
                except FeishuError as e:
                    logger.error(f"{tname} 写入飞书失败：{e}（CSV 已保存）")
    return ok


def daily(fs, dry_run=False, day=None, do_sync=True):
    if do_sync:
        sync_positions(fs, dry_run=dry_run)
    return daily_snapshot.run(fs, dry_run=dry_run, today=day)


def monthly(fs, dry_run=False, day=None, force=False):
    return performance.run(fs, dry_run=dry_run, today=day, force=force)


def check(fs, code=None, limit=3):
    """打印指定品种的评分明细，便于与行情软件核对。"""
    rows = fs.read("monitor")
    if code:
        rows = [r for r in rows if as_text(r.get(M["code"])) == code]
        if not rows:
            print(f"监控表里没有代码 {code}")
            return
    else:
        rows = rows[:limit]

    for r in rows:
        name = as_text(r.get(M["name"]))
        cd = as_text(r.get(M["code"]))
        daily = akd.get_daily(cd)
        if daily is None:
            print(f"{name}({cd}) 取不到日线")
            continue
        daily = _daily_with_spot(daily, akd.get_quote(cd))
        wk = ind.build_weekly(daily)
        f = ind.week_features(wk)
        items, total = sc.explain(f)
        last = wk.iloc[-1]
        print(f"\n=== {name} {cd}  周K截止 {last['date_end'].date()}  "
              f"收盘 {last['close']}  {'(本周未完成)' if last['is_partial'] else ''} ===")
        for key, desc, val in items:
            print(f"  {key:6} {desc:58} {val:+d}")
        print(f"  合计   {total:+d}   {sc.power_color(f)}")

        hd = ind.hpi(daily)
        hpw = ind.hpi(wk)
        print(f"  参考：周线 HPI 最新 {f['hpi']:.0f}，上一周 {hpw.iloc[-2]:.0f} "
              f"({'上升' if f['hpi'] > hpw.iloc[-2] else '下降'})")
        print(f"        日线 HPI 最新 {hd.iloc[-1]:.0f}，上一日 {hd.iloc[-2]:.0f} "
              f"({'上升' if hd.iloc[-1] > hd.iloc[-2] else '下降'})")


def diagnose(fs):
    print("=" * 60)
    print("飞书连接自检")
    print("=" * 60)
    try:
        tok = fs._get_token()
        print(f"[OK] tenant_access_token: {tok[:12]}...")
    except Exception as e:
        print(f"[FAIL] 获取 token 失败：{e}")
        return

    for name, tid in cfg.TABLES.items():
        try:
            rows = fs.read_table(tid)
            sample = ""
            if rows:
                keys = [k for k in rows[0] if k != "_id"][:3]
                sample = " | ".join(f"{k}={rows[0][k]}" for k in keys)
            print(f"[OK]   {name:9} {tid}  {len(rows):4d} 条  {sample}")
        except FeishuError as e:
            print(f"[FAIL] {name:9} {tid}  code={e.code} {e.msg}")

    print("\n行情自检（前 3 个品种）")
    try:
        for row in fs.read("monitor")[:3]:
            code = as_text(row.get(cfg.MONITOR_FIELDS["code"]))
            q = akd.get_quote(code)
            d = akd.get_daily(code)
            wk = ind.build_weekly(d) if d is not None else None
            f_ = ind.week_features(wk) if wk is not None else None
            s, c = sc.score(f_) if f_ else (None, None)
            print(f"  {as_text(row.get(cfg.MONITOR_FIELDS['name'])):6} {code:5} "
                  f"price={q['price'] if q else None} contract={q.get('contract') if q else None} "
                  f"bars={0 if d is None else len(d)} weeks={0 if wk is None else len(wk)} "
                  f"score={s} color={c}")
    except Exception as e:
        print(f"[FAIL] 行情自检失败：{e}")

    print("\n提示：若某些表返回 1254302 / 403，请在多维表格「高级权限」中把应用加为可编辑协作者。")


def main():
    p = argparse.ArgumentParser(description="三重滤网交易系统自动化")
    p.add_argument("--mode", default="realtime",
                   choices=["realtime", "daily", "monthly", "all", "position",
                            "export-history", "check", "diagnose"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--date", default=None, help="业务日期 YYYY-MM-DD")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--force-history", action="store_true", help="忽略 15 分钟节流，立即写历史与 CSV")
    p.add_argument("--pending", action="store_true",
                   help="只处理「状态=已完成」的交易日志（飞书自动化触发时用）")
    p.add_argument("--code", default=None, help="--mode check 时指定品种代码，如 V0")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    setup_logging(args.verbose)
    day = pd.Timestamp(args.date).normalize() if args.date else None
    fs = Feishu()
    logger.info(f"启动 mode={args.mode} dry_run={args.dry_run} date={day.date() if day is not None else 'today'}")

    try:
        if args.mode == "realtime":
            realtime(fs, dry_run=args.dry_run, limit=args.limit, force_history=args.force_history)
        elif args.mode == "daily":
            daily(fs, dry_run=args.dry_run, day=day, do_sync=not args.no_sync)
        elif args.mode == "monthly":
            monthly(fs, dry_run=args.dry_run, day=day, force=args.force)
        elif args.mode == "position":
            sync_positions(fs, dry_run=args.dry_run, pending=args.pending)
        elif args.mode == "export-history":
            history_mod.export_from_feishu(fs)
        elif args.mode == "all":
            daily(fs, dry_run=args.dry_run, day=day, do_sync=not args.no_sync)
            monthly(fs, dry_run=args.dry_run, day=day, force=args.force)
        elif args.mode == "diagnose":
            diagnose(fs)
        elif args.mode == "check":
            check(fs, code=args.code)
    except FeishuError as e:
        logger.error(f"飞书接口错误：{e}")
        sys.exit(2)
    except Exception:
        logger.exception("运行失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
