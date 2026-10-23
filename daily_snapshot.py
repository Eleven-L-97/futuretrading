# -*- coding: utf-8 -*-
"""模块三：每日账户快照（18:00 运行）。

两条独立路径计算账户价值并互相校验：
A. 逐笔对冲：初始资金 + 已实现盈亏 + 浮动盈亏
B. 逐日盯市：前一日账户价值 + 当日盯市盈亏 - 当日手续费

当日盯市盈亏按持仓拆分：
- 昨日已持有数量： (今结算 - 昨结算) × 乘数 × 手数
- 今日新开数量：     (今结算 - 今开仓价) × 乘数 × 手数
- 今日平掉数量：     (今平仓价 - 昨结算或今开仓价) × 乘数 × 手数
空头方向取负。

若任一持仓品种取不到当日结算价 -> 记日志并放弃本次写入。
"""
import logging

import pandas as pd

import akdata as akd
import config as cfg
from feishu import as_ms, as_num, as_text, day_to_ms, ms_to_day, now_ms, today as local_today
from position import journal_rows, realized_gross, replay, today_flows

logger = logging.getLogger(__name__)

S = cfg.SNAP_FIELDS
P = cfg.POS_FIELDS
M = cfg.MONITOR_FIELDS
J = cfg.JOURNAL_FIELDS

DIR_SIGN = {"多": 1, "空": -1}
EPS = 1e-9


def _snap_date(row):
    ms = as_ms(row.get(S["date"]))
    return ms_to_day(ms) if ms is not None else None


def _day_ms(ts):
    return day_to_ms(ts)


def _fee(row, prefix, qty, px, mult, monitor):
    """取手续费公式结果；为空则按「元/手 + 成交额比例」估算。"""
    v = as_num(row.get(J[f"{prefix}_fee"]))
    if v is not None:
        return float(v)
    if not monitor:
        return 0.0
    per_lot = as_num(monitor.get(M["fee_per_lot"]), 0) or 0
    ratio = as_num(monitor.get(M["fee_ratio"]), 0) or 0
    return qty * per_lot + abs(px * mult * qty) * ratio


def validate_journal(rows, require_open_px=None):
    """检查交易日志能否支撑正确的盈亏/手续费计算。

    飞书的「盈亏数额」是公式字段，平仓行如果没填开仓价，公式会按 0 计算，
    得出离谱的盈亏（实测出现过 +89194 这种数）。
    仅当 PNL_SOURCE=journal 时强制要求平仓行填开仓价。
    """
    if require_open_px is None:
        require_open_px = cfg.PNL_SOURCE == "journal"
    errs = []
    for r in rows:
        tactic = as_text(r.get(J["tactic"]))
        if not tactic:
            continue
        name = as_text(r.get(J["name"]))
        day = r["_date"].date()
        qty_o = as_num(r.get(J["open_qty"]), 0) or 0
        px_o = as_num(r.get(J["open_px"]), 0) or 0
        qty_c = as_num(r.get(J["close_qty"]), 0) or 0
        px_c = as_num(r.get(J["close_px"]), 0) or 0
        if tactic in cfg.CLOSE_TACTICS:
            if qty_c <= 0:
                errs.append(f"{name} {day} 平仓行缺少「平仓数量」")
            if px_c <= 0:
                errs.append(f"{name} {day} 平仓行缺少「平仓价」")
            if require_open_px and px_o <= 0:
                errs.append(f"{name} {day} 平仓行缺少「开仓价」（盈亏数额公式会按 0 计算）")
        else:
            if qty_o <= 0:
                errs.append(f"{name} {day} 开仓行缺少「开仓数量」")
            if px_o <= 0:
                errs.append(f"{name} {day} 开仓行缺少「开仓价」")
    return errs


def run(fs, dry_run=False, today=None):
    today = pd.Timestamp(today or local_today()).normalize()
    monitors = {as_text(r.get(M["name"])): r for r in fs.read("monitor")}
    positions = fs.read("position")
    snaps = fs.read("snapshot")
    rows = journal_rows(fs)
    # 补跑历史日期时，不能把之后发生的交易算进来
    rows = [r for r in rows if r["_date"] <= today]

    # ---------- 交易日志完整性检查 ----------
    errs = validate_journal(rows)
    if errs:
        for e in errs:
            logger.error(f"交易日志异常：{e}")
        if cfg.STRICT_JOURNAL:
            logger.error(f"共 {len(errs)} 处异常，本次快照中止（不会写入错误数据）。"
                         f"修好后重跑，或临时把 config.STRICT_JOURNAL 设为 False。")
            return None

    # ---------- 前一日快照 ----------
    hist = [(d, r) for d, r in ((_snap_date(r), r) for r in snaps) if d is not None and d < today]
    hist.sort(key=lambda x: x[0])
    prev_snap = hist[-1][1] if hist else None
    prev_day = hist[-1][0] if hist else None
    if prev_snap is None:
        logger.warning("未找到前一日快照，本次按初始资金计算（首次运行可忽略；补跑请先补齐历史快照）")
        prev_equity = cfg.INITIAL_EQUITY
    else:
        prev_equity = as_num(prev_snap.get(S["equity"]), cfg.INITIAL_EQUITY)

    # ---------- 已实现盈亏 / 手续费 ----------
    realized = 0.0
    total_fee = 0.0
    today_fee = 0.0
    journal_pnl = 0.0
    for r in rows:
        f_open = as_num(r.get(J["open_fee"]), 0) or 0
        f_close = as_num(r.get(J["close_fee"]), 0) or 0
        total_fee += f_open + f_close
        if r["_date"] == today:
            today_fee += f_open + f_close
        if as_text(r.get(J["tactic"])) in cfg.CLOSE_TACTICS:
            journal_pnl += as_num(r.get(J["pnl"]), 0) or 0

    mult_of = {name: (as_num(m.get(M["multiplier"]), 1) or 1) for name, m in monitors.items()}
    gross, open_fee_closed, close_fees = realized_gross(rows, mult_of)
    net_closed = gross - open_fee_closed - close_fees

    if cfg.PNL_SOURCE == "journal":
        realized = journal_pnl
    else:
        realized = gross
        if abs(journal_pnl - net_closed) > 1:
            logger.warning(
                f"飞书「盈亏数额」合计 {journal_pnl:.2f}，脚本按开平价计算 {net_closed:.2f}，"
                f"差 {journal_pnl - net_closed:.2f}。"
                f"快照采用按价格计算的结果；如需以表格公式为准，把 config.PNL_SOURCE 改成 journal。")

    # ---------- 结算价 ----------
    # 当前持仓必须取到今结算；当日有平仓的品种也要取（算 (平仓价 - 昨结算)）
    cur_names = {as_text(p.get(P["name"])) for p in positions
                 if (as_num(p.get(P["qty"]), 0) or 0) > 0}
    need_names = cur_names | {as_text(r.get(J["name"])) for r in rows}
    settle_map, prev_settle_map, missing = {}, {}, []
    for name in need_names:
        if not name:
            continue
        code = as_text((monitors.get(name) or {}).get(M["code"]))
        df = akd.get_daily(code) if code else None
        st = akd.latest_settle(df, today)
        ps = akd.prev_settle(df, today)
        if st is None:
            if name in cur_names:
                missing.append(f"{name}({code})")
            continue
        settle_map[name] = st
        if ps is not None:
            prev_settle_map[name] = ps
    if missing:
        logger.error(f"以下持仓品种取不到 {today.date()} 结算价，本次快照跳过：{missing}")
        return None

    # ---------- A. 逐笔路径 ----------
    float_pnl = 0.0
    margin_used = 0.0
    open_fee_open_pos = 0.0
    held = []
    for p in positions:
        name = as_text(p.get(P["name"]))
        qty = as_num(p.get(P["qty"]), 0) or 0
        if qty <= 0:
            continue
        avg = as_num(p.get(P["avg_px"]), 0) or 0
        direction = as_text(p.get(P["direction"]))
        sign = DIR_SIGN.get(direction, 1)
        mrow = monitors.get(name) or {}
        mult = as_num(mrow.get(M["multiplier"]), 1) or 1
        mg_rate = as_num(mrow.get(M["margin_rate"]), 0) or 0
        settle = settle_map[name]
        float_pnl += qty * (settle - avg) * mult * sign
        margin_used += qty * settle * mult * mg_rate
        held.append((name, direction, qty, avg, mult, sign, settle))

    # 未平仓头寸的开仓手续费（仅 journal 口径需要：盈亏数额已含手续费但未含未平仓的开仓费）
    if cfg.PNL_SOURCE == "journal" and cfg.JOURNAL_PNL_INCLUDES_FEE and positions:
        open_keys = {(as_text(p.get(P["name"])), as_text(p.get(P["direction"]))) for p in positions
                     if (as_num(p.get(P["qty"]), 0) or 0) > 0}
        for r in rows:
            if as_text(r.get(J["tactic"])) not in cfg.CLOSE_TACTICS:
                key = (as_text(r.get(J["name"])), as_text(r.get(J["direction"])))
                if key in open_keys:
                    open_fee_open_pos += as_num(r.get(J["open_fee"]), 0) or 0

    if cfg.PNL_SOURCE == "journal":
        tick_equity = cfg.INITIAL_EQUITY + realized + float_pnl - open_fee_open_pos
    else:
        # 按价格口径：毛盈亏 - 全部手续费
        tick_equity = cfg.INITIAL_EQUITY + realized + float_pnl - total_fee

    # ---------- B. 盯市路径 ----------
    qty_y = replay(rows, until=prev_day) if prev_day is not None else {}
    flows = today_flows(rows, today)
    mtm_pnl = 0.0
    keys = set(qty_y) | {(as_text(p.get(P["name"])), as_text(p.get(P["direction"])))
                         for p in positions} | set(flows)
    for key in keys:
        name, direction = key
        sign = DIR_SIGN.get(direction, 1)
        mrow = monitors.get(name) or {}
        mult = as_num(mrow.get(M["multiplier"]), 1) or 1
        settle = settle_map.get(name)
        qty_yes = (qty_y.get(key) or {}).get("qty", 0.0)
        qty_now = sum((as_num(p.get(P["qty"]), 0) or 0) for p in positions
                      if as_text(p.get(P["name"])) == name and as_text(p.get(P["direction"])) == direction)
        if qty_now > 0 and settle is None:
            continue        # 已在上面记入 missing 并中止，这里只是防御
        flow = flows.get(key) or {"add_qty": 0.0, "add_px": 0.0, "close_qty": 0.0, "close_px": 0.0}
        had_before = qty_yes > EPS
        base = prev_settle_map.get(name) if had_before else flow["add_px"]
        if base is None:
            base = settle
        # 余下数量：昨结算 -> 今结算
        if qty_now > 0:
            mtm_pnl += qty_now * (settle - base) * mult * sign
        # 今日新开（且昨日已有底仓）：开仓价 -> 今结算
        if had_before and flow["add_qty"] > EPS:
            mtm_pnl += flow["add_qty"] * (settle - flow["add_px"]) * mult * sign
        # 今日平掉：基准 -> 平仓价
        if flow["close_qty"] > EPS:
            mtm_pnl += flow["close_qty"] * (flow["close_px"] - base) * mult * sign

    if prev_day is None:
        # 首个快照没有「昨结算」可对齐，盯市路径直接以逐笔路径为基准
        logger.warning("首次快照（无历史快照）：盯市路径以逐笔路径为基准，"
                       "「当日盈亏」为建仓至今的累计盈亏")
        mtm_equity = tick_equity
    else:
        mtm_equity = prev_equity + mtm_pnl - today_fee
    diff = abs(tick_equity - mtm_equity)
    ok = diff <= cfg.TOLERANCE

    # ---------- 其余指标 ----------
    equity = mtm_equity
    margin_used = 0.0
    for p in positions:
        qty = as_num(p.get(P["qty"]), 0) or 0
        if qty <= 0:
            continue
        name = as_text(p.get(P["name"]))
        mrow = monitors.get(name) or {}
        mult = as_num(mrow.get(M["multiplier"]), 1) or 1
        mg_rate = as_num(mrow.get(M["margin_rate"]), 0) or 0
        margin_used += qty * settle_map[name] * mult * mg_rate

    daily_pnl = equity - prev_equity
    if prev_day is None:
        # 首个快照没有「昨日」，当日盈亏记为 0，累计盈亏仍反映建仓至今
        daily_pnl = 0.0
    row = {
        S["date"]: _day_ms(today),
        S["equity"]: round(equity, 2),
        S["available"]: round(equity - margin_used, 2),
        S["margin"]: round(margin_used, 2),
        S["usage"]: round(margin_used / equity, 4) if equity else 0.0,
        S["daily_pnl"]: round(daily_pnl, 2),
        S["daily_ret"]: round(daily_pnl / prev_equity, 4) if prev_equity else 0.0,
        S["cum_pnl"]: round(equity - cfg.INITIAL_EQUITY, 2),
        S["cum_fee"]: round(total_fee, 2),
        S["settle"]: (lambda v: round(v, 4) if v is not None else None)(_representative_settle(held, settle_map)),
        S["mtm_equity"]: round(mtm_equity, 2),
        S["tick_equity"]: round(tick_equity, 2),
        S["ok"]: "是" if ok else "否",
        S["updated_at"]: now_ms(),
    }

    logger.info(f"快照 {today.date()}：盯市={mtm_equity:.2f} 逐笔={tick_equity:.2f} "
                f"差额={diff:.2f} 校验={'通过' if ok else '不通过'} 持仓={[h[0] for h in held]}")
    if not ok:
        logger.warning(f"双路径校验未通过：盯市 {mtm_equity:.2f} vs 逐笔 {tick_equity:.2f}，"
                       f"差 {diff:.2f}。请核对交易日志手续费/盈亏公式。")

    if dry_run:
        logger.info(f"[dry-run] 将写入快照：{row}")
        return row

    fs.upsert(cfg.TABLES["snapshot"], S["date"], [row],
              key_func=lambda r: str(_snap_date(r).date()) if _snap_date(r) is not None else "")
    _backfill_settles(fs, monitors, today, rows)
    return row


def _representative_settle(held, settle_map):
    """快照表只有一列「结算价」：单品种直接写，多品种按保证金加权。"""
    if len(held) == 1:
        return settle_map[held[0][0]]
    total = sum(h[2] * h[6] * h[4] for h in held)
    if total <= 0:
        return settle_map[held[0][0]] if held else None
    return sum(h[2] * h[6] * h[4] * settle_map[h[0]] for h in held) / total


def snapshots_missing(snaps, today):
    """没有任何早于今天的快照，且今天不是第一天 -> 视为历史缺失。"""
    dates = [d for d in (_snap_date(r) for r in snaps) if d is not None and d < today]
    return len(snaps) > 0 and not dates


def _backfill_settles(fs, monitors, today, rows):
    """回填最近 5 个交易日内、快照表里缺失的「结算价」。

    按当日实际持仓回放，取持仓品种结算价的均值（单品种即该品种结算价）。
    """
    try:
        snaps = fs.read("snapshot")
    except Exception as e:
        logger.warning(f"回填结算价时读取快照失败：{e}")
        return
    missing = []
    for r in snaps:
        d = _snap_date(r)
        if d is not None and d < today and as_num(r.get(S["settle"])) is None:
            missing.append((d, r))
    missing.sort(key=lambda x: x[0])
    for d, r in missing[-5:]:
        held = replay(rows, until=d)
        values = []
        for name, _direction in held:
            mrow = monitors.get(name) or {}
            st = akd.latest_settle(akd.get_daily(as_text(mrow.get(M["code"]))), d)
            if st is not None:
                values.append(st)
        if values:
            fs.update(cfg.TABLES["snapshot"], r["_id"], {S["settle"]: round(sum(values) / len(values), 4)})
