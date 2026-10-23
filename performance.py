# -*- coding: utf-8 -*-
"""模块四：绩效指标（月 / 季 / 年）。

数据来源：每日账户快照（权益曲线） + 交易日志（逐笔盈亏）。
每次运行产出「月」「季」「年」三条记录，按「周期标识」upsert。
"""
import logging

import numpy as np
import pandas as pd

import config as cfg
from feishu import as_ms, as_num, as_text, day_to_ms, ms_to_day, today as local_today

logger = logging.getLogger(__name__)

S = cfg.SNAP_FIELDS
F = cfg.PERF_FIELDS
J = cfg.JOURNAL_FIELDS

RF_DAILY = (1 + cfg.RISK_FREE_ANNUAL) ** (1 / cfg.TRADING_DAYS) - 1


def _snap_date(row):
    ms = as_ms(row.get(S["date"]))
    return ms_to_day(ms) if ms is not None else None


def _periods(now):
    """按需求：每月算当月；季末月（3/6/9/12）加算当季；12 月再加算当年。"""
    q = (now.month - 1) // 3 + 1
    out = [("月", now.replace(day=1).normalize(), f"{now.year}-{now.month:02d}")]
    if now.month in (3, 6, 9, 12):
        out.append(("季", pd.Timestamp(year=now.year, month=(q - 1) * 3 + 1, day=1), f"{now.year}Q{q}"))
    if now.month == 12:
        out.append(("年", pd.Timestamp(year=now.year, month=1, day=1), f"{now.year}Y"))
    return out


def run(fs, dry_run=False, today=None, force=False):
    now = pd.Timestamp(today or local_today()).normalize()
    if not force and not _is_period_end(now):
        logger.info(f"{now.date()} 不是月末，跳过绩效计算；如需强制运行加 --force")
        return []

    snaps = fs.read("snapshot")
    journals = fs.read("journal")

    series = []
    for r in snaps:
        d = _snap_date(r)
        eq = as_num(r.get(S["equity"]))
        if d is not None and eq is not None:
            series.append((d, eq))
    series.sort(key=lambda x: x[0])
    if not series:
        logger.warning("无快照数据，绩效计算跳过")
        return []

    trades = []
    for r in journals:
        ms = as_ms(r.get(J["date"]))
        pnl = as_num(r.get(J["pnl"]))
        tactic = as_text(r.get(J["tactic"]))
        if ms is None or pnl is None or tactic not in cfg.CLOSE_TACTICS:
            continue
        trades.append({
            "date": ms_to_day(ms),
            "pnl": pnl,
            "direction": as_text(r.get(J["direction"])),
        })
    trades.sort(key=lambda x: x["date"])

    rows = []
    for pname, start, cid in _periods(now):
        row = _one_period(fs, pname, start, now, cid, series, trades)
        if row:
            rows.append(row)

    if not rows:
        logger.info("本期数据不足，未产出绩效记录")
        return []

    if dry_run:
        for r in rows:
            logger.info(f"[dry-run] {r[F['cycle_id']]}: {r}")
        return rows

    fs.upsert(cfg.TABLES["perf"], F["cycle_id"], rows)
    logger.info(f"绩效更新完成：{[r[F['cycle_id']] for r in rows]}")
    return rows


def _is_period_end(now):
    """月末（含最后一个自然日）判定：下一天进入新的月份即视为月末。"""
    return (now + pd.Timedelta(days=1)).month != now.month


def _one_period(fs, pname, start, end, cid, series, trades):
    # 期初权益：周期开始前最后一个快照；没有则用初始资金
    before = [eq for d, eq in series if d < start]
    start_eq = before[-1] if before else cfg.INITIAL_EQUITY
    inside = [(d, eq) for d, eq in series if start <= d <= end]
    if not inside:
        logger.info(f"{cid} 无快照，跳过")
        return None
    end_eq = inside[-1][1]

    dates = [start] + [d for d, _ in inside]
    eqs = np.array([start_eq] + [eq for _, eq in inside], dtype=float)

    days = max((end - start).days, 1)
    tot = end_eq / start_eq - 1 if start_eq else 0.0
    ann = (end_eq / start_eq) ** (365 / days) - 1 if start_eq > 0 else 0.0

    peak = np.maximum.accumulate(eqs)
    dd = eqs / peak - 1
    maxdd = float(dd.min())

    # 最长回撤修复天数（自然日）
    max_fix, best_remark = 0, ""
    peak_i = 0
    for i in range(1, len(eqs)):
        if eqs[i] >= eqs[peak_i]:
            if i - peak_i > 1:
                span = (dates[i] - dates[peak_i]).days
                max_fix = max(max_fix, span)
            peak_i = i
    if eqs[-1] < eqs[peak_i]:
        span = (dates[-1] - dates[peak_i]).days
        if span >= max_fix:
            max_fix = span
            best_remark = "未修复"

    rets = np.diff(eqs) / eqs[:-1]
    rets = rets[np.isfinite(rets)]
    sharpe = ((rets.mean() - RF_DAILY) / rets.std(ddof=1) * np.sqrt(cfg.TRADING_DAYS)
              if len(rets) > 1 and rets.std(ddof=1) > 0 else 0.0)
    calmar = ann / abs(maxdd) if maxdd else 0.0

    tlog = [t for t in trades if start <= t["date"] <= end]
    pnls = [t["pnl"] for t in tlog]
    n = len(pnls)
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    wr = len(wins) / n if n else 0.0
    gp = sum(wins)
    gl = abs(sum(losses))
    pf = gp / gl if gl else (9999.0 if gp > 0 else 0.0)
    aw = gp / len(wins) if wins else 0.0
    al = gl / len(losses) if losses else 0.0
    avgrr = aw / al if al else 0.0
    bw = max(pnls) if pnls else 0.0
    bl = abs(min(pnls)) if pnls else 0.0

    def consec(sign):
        best = cur = 0
        for x in pnls:
            if (x > 0 and sign == 1) or (x < 0 and sign == -1):
                cur += 1
                best = max(best, cur)
            else:
                cur = 0
        return best

    lp = sum(t["pnl"] for t in tlog if t["direction"] == "多")
    sp = sum(t["pnl"] for t in tlog if t["direction"] == "空")
    base = abs(lp) + abs(sp)
    lc = lp / base if base else 0.0
    sc = sp / base if base else 0.0
    remark = best_remark
    if base < 1e-9 and n:
        lc = sc = 0.0
        remark = (remark + " 多空净盈利≈0，占比无意义").strip()

    return {
        F["cycle_id"]: cid,
        F["period"]: pname,
        F["start_date"]: day_to_ms(start),
        F["end_date"]: day_to_ms(end),
        F["ann_ret"]: round(float(ann), 4),
        F["tot_ret"]: round(float(tot), 4),
        F["max_dd"]: round(maxdd, 4),
        F["max_dd_days"]: int(max_fix),
        F["calmar"]: round(float(calmar), 4),
        F["sharpe"]: round(float(sharpe), 4),
        F["trades"]: n,
        F["win_rate"]: round(wr, 4),
        F["pf"]: round(float(min(pf, 9999)), 4),
        F["avg_rr"]: round(avgrr, 4),
        F["big_win"]: round(float(bw), 2),
        F["big_loss"]: round(float(bl), 2),
        F["consec_win"]: consec(1),
        F["consec_loss"]: consec(-1),
        F["long_contrib"]: round(lc, 4),
        F["short_contrib"]: round(sc, 4),
        F["remark"]: remark,
    }
