# -*- coding: utf-8 -*-
"""模块三：每日账户快照（18:00 后运行）。

两条独立路径计算账户价值并互相校验：
A. 逐笔对冲：初始资金 + 已实现盈亏（毛） + 浮动盈亏 − 全部手续费
B. 逐日盯市：前一日账户价值 + 当日盯市盈亏 − 当日手续费

持仓来源：**按交易日志回放**（replay），而不是读持仓管理表。
这样补算历史日期、或交易日志晚录入，都不会算错。

结算价：全部通过 akshare 按品种取「当日结算价 / 昨日结算价」，
不再从快照表读「结算价」字段（该字段已删除）。
若某持仓品种当日结算价还没出来：本次不计算相关指标，只建一条只有日期的占位记录，
第二个交易日运行时会自动补算并回写。

每日成功计算后，会把持仓管理表里在仓品种的「结算价」更新为最新结算价。
"""
import logging

import pandas as pd

import akdata as akd
import config as cfg
from feishu import as_num, as_text, day_to_ms, ms_to_day, now_ms, today as local_today
from position import journal_rows, realized_gross, replay, today_flows

logger = logging.getLogger(__name__)

S = cfg.SNAP_FIELDS
P = cfg.POS_FIELDS
M = cfg.MONITOR_FIELDS
J = cfg.JOURNAL_FIELDS

DIR_SIGN = {"多": 1, "空": -1}
EPS = 1e-9


def _snap_date(row):
    ms = row.get(S["date"])
    if isinstance(ms, (int, float)):
        return ms_to_day(ms)
    return None


def validate_journal(rows, require_open_px=None):
    """检查交易日志关键字段是否齐全（平仓行缺价/数量会导致盈亏算不出来）。"""
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
                errs.append(f"{name} {day} 平仓行缺少「开仓价」")
        else:
            if qty_o <= 0:
                errs.append(f"{name} {day} 开仓行缺少「开仓数量」")
            if px_o <= 0:
                errs.append(f"{name} {day} 开仓行缺少「开仓价」")
    return errs


def run(fs, dry_run=False, today=None):
    day = pd.Timestamp(today or local_today()).normalize()

    # 先补算之前因结算价未出而空着的快照
    if not dry_run:
        for d in _pending_dates(fs, day):
            logger.info(f"补算 {d.date()} 的快照")
            _compute(fs, d, dry_run, is_today=False)

    return _compute(fs, day, dry_run, is_today=True)


def _pending_dates(fs, day, max_days=5):
    """最近几天里「有记录但账户价值为空」的日期。"""
    out = []
    for r in fs.read("snapshot"):
        d = _snap_date(r)
        if d is not None and d < day and as_num(r.get(S["equity"])) is None:
            out.append(d)
    return sorted(set(out))[-max_days:]


def _settles(rows, holdings, monitors, day):
    """取当日/昨结算价。返回 (今结 map, 昨结 map, 缺失品种列表)。"""
    names = {name for (name, _d) in holdings}
    flow_names = {name for (name, _d) in today_flows(rows, day)}
    need = names | flow_names
    settle_today, prev_settle_map, missing = {}, {}, []
    has_bar_today = False
    for name in need:
        code = as_text((monitors.get(name) or {}).get(M["code"]))
        df = akd.get_daily(code) if code else None
        if df is not None and len(df) and (df["date"].dt.normalize() == day).any():
            has_bar_today = True
        st = akd.latest_settle(df, day)
        ps = akd.prev_settle(df, day)
        if st is not None:
            settle_today[name] = st
        elif name in names:
            missing.append(f"{name}({code})")
        if ps is not None:
            prev_settle_map[name] = ps

    if not need:      # 空仓：用前几个品种判断是不是交易日
        for name in list(monitors)[:3]:
            code = as_text((monitors.get(name) or {}).get(M["code"]))
            df = akd.get_daily(code) if code else None
            if df is not None and len(df) and (df["date"].dt.normalize() == day).any():
                has_bar_today = True
                break

    return settle_today, prev_settle_map, missing, has_bar_today


def _compute(fs, day, dry_run=False, is_today=False):
    monitors = {as_text(r.get(M["name"])): r for r in fs.read("monitor")}
    snaps = fs.read("snapshot")
    rows = [r for r in journal_rows(fs) if r["_date"] <= day]

    errs = validate_journal(rows)
    if errs:
        for e in errs:
            logger.error(f"交易日志异常：{e}")
        if cfg.STRICT_JOURNAL:
            logger.error(f"共 {len(errs)} 处异常，本次快照中止（不会写入错误数据）")
            return None

    holdings = replay(rows, until=day)
    settle_today, prev_settle_map, missing, has_bar_today = _settles(rows, holdings, monitors, day)

    if not has_bar_today:
        logger.info(f"{day.date()} 没有日线数据（非交易日），跳过快照")
        return None

    if missing:
        logger.error(f"{day.date()} 以下持仓品种取不到当日结算价，本次不计算：{missing}；"
                     f"次日运行会自动补算")
        if not dry_run:
            _write_placeholder(fs, day)
        return None

    # ---------- 前一日快照 ----------
    hist = [(d, r) for d, r in ((_snap_date(r), r) for r in snaps) if d is not None and d < day]
    hist.sort(key=lambda x: x[0])
    prev_day = hist[-1][0] if hist else None
    prev_equity = as_num(hist[-1][1].get(S["equity"]), cfg.INITIAL_EQUITY) if hist else cfg.INITIAL_EQUITY
    if not hist:
        logger.warning("未找到前一日快照，本次按初始资金计算（首次运行可忽略）")

    # ---------- 盈亏与费用（只算到 day 为止） ----------
    total_fee = today_fee = journal_pnl = 0.0
    for r in rows:
        f_open = as_num(r.get(J["open_fee"]), 0) or 0
        f_close = as_num(r.get(J["close_fee"]), 0) or 0
        total_fee += f_open + f_close
        if r["_date"] == day:
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
                f"差 {journal_pnl - net_closed:.2f}；快照按价格计算。")

    # ---------- A. 逐笔路径 ----------
    float_pnl = 0.0
    margin_used = 0.0
    held = []
    for (name, direction), p in holdings.items():
        qty = p["qty"]
        if qty <= EPS:
            continue
        mrow = monitors.get(name) or {}
        mult = as_num(mrow.get(M["multiplier"]), 1) or 1
        mg_rate = as_num(mrow.get(M["margin_rate"]), 0) or 0
        settle = settle_today[name]
        sign = DIR_SIGN.get(direction, 1)
        float_pnl += qty * (settle - p["avg_px"]) * mult * sign
        margin_used += qty * settle * mult * mg_rate
        held.append((name, direction, qty, p["avg_px"], mult, sign, settle))

    if cfg.PNL_SOURCE == "journal":
        # 「盈亏数额」已含手续费，只需补扣未平仓头寸的开仓费
        open_fee_open_pos = 0.0
        for r in rows:
            if as_text(r.get(J["tactic"])) not in cfg.CLOSE_TACTICS:
                key = (as_text(r.get(J["name"])), as_text(r.get(J["direction"])))
                if key in holdings:
                    open_fee_open_pos += as_num(r.get(J["open_fee"]), 0) or 0
        tick_equity = cfg.INITIAL_EQUITY + realized + float_pnl - open_fee_open_pos
    else:
        tick_equity = cfg.INITIAL_EQUITY + realized + float_pnl - total_fee

    # ---------- B. 盯市路径 ----------
    # 把当天的持仓拆成四块，各自从自己的成本基准起算：
    #   昨仓留存、昨仓平掉（基准=昨结算）；今开留存、今开平掉（基准=今开仓价）
    # 这样「平仓量 > 昨仓量」（当天先加仓再减仓）时不会重复计算。
    qty_y = replay(rows, until=prev_day) if prev_day is not None else {}
    flows = today_flows(rows, day)
    mtm_pnl = 0.0
    keys = set(qty_y) | set(holdings) | set(flows)
    for key in keys:
        name, direction = key
        sign = DIR_SIGN.get(direction, 1)
        mrow = monitors.get(name) or {}
        mult = as_num(mrow.get(M["multiplier"]), 1) or 1
        settle = settle_today.get(name)

        q0 = (qty_y.get(key) or {}).get("qty", 0.0)
        q1 = holdings.get(key, {}).get("qty", 0.0)
        flow = flows.get(key) or {"add_qty": 0.0, "add_px": 0.0, "close_qty": 0.0, "close_px": 0.0}
        add_qty, add_px = flow["add_qty"], flow["add_px"]
        close_qty, close_px = flow["close_qty"], flow["close_px"]
        if q1 > EPS and settle is None:
            logger.warning(f"{name} 缺少今结算价，盯市路径跳过该品种")
            continue

        old_closed = min(close_qty, q0)
        old_remain = max(q0 - old_closed, 0.0)
        new_closed = max(close_qty - q0, 0.0)
        new_remain = max(q1 - old_remain, 0.0)

        # 昨仓：基准 = 昨结算
        if old_remain > EPS or old_closed > EPS:
            base = prev_settle_map.get(name)
            if base is None:
                base = settle if settle is not None else add_px
            if old_remain > EPS and settle is not None:
                mtm_pnl += old_remain * (settle - base) * mult * sign
            if old_closed > EPS:
                mtm_pnl += old_closed * (close_px - base) * mult * sign
        # 今仓：基准 = 今开仓均价
        if new_remain > EPS and settle is not None:
            mtm_pnl += new_remain * (settle - add_px) * mult * sign
        if new_closed > EPS:
            mtm_pnl += new_closed * (close_px - add_px) * mult * sign

    if prev_day is None:
        logger.warning("首次快照（无历史快照）：盯市路径以逐笔路径为基准，「当日盈亏」记 0")
        mtm_equity = tick_equity
    else:
        mtm_equity = prev_equity + mtm_pnl - today_fee

    diff = abs(tick_equity - mtm_equity)
    ok = diff <= cfg.TOLERANCE
    equity = mtm_equity
    daily_pnl = 0.0 if prev_day is None else equity - prev_equity

    row = {
        S["date"]: day_to_ms(day),
        S["equity"]: round(equity, 2),
        S["available"]: round(equity - margin_used, 2),
        S["margin"]: round(margin_used, 2),
        S["usage"]: round(margin_used / equity, 4) if equity else 0.0,
        S["daily_pnl"]: round(daily_pnl, 2),
        S["daily_ret"]: round(daily_pnl / prev_equity, 4) if prev_equity else 0.0,
        S["cum_pnl"]: round(equity - cfg.INITIAL_EQUITY, 2),
        S["cum_fee"]: round(total_fee, 2),
        S["mtm_equity"]: round(mtm_equity, 2),
        S["tick_equity"]: round(tick_equity, 2),
        S["ok"]: "是" if ok else "否",
        S["updated_at"]: now_ms(),
    }

    logger.info(f"快照 {day.date()}：盯市={mtm_equity:.2f} 逐笔={tick_equity:.2f} "
                f"差额={diff:.2f} 校验={'通过' if ok else '不通过'} "
                f"持仓={[h[0] for h in held]}")
    if not ok:
        logger.warning(f"双路径校验未通过：盯市 {mtm_equity:.2f} vs 逐笔 {tick_equity:.2f}，差 {diff:.2f}")

    if dry_run:
        logger.info(f"[dry-run] 将写入快照：{row}")
        return row

    fs.upsert(cfg.TABLES["snapshot"], S["date"], [row],
              key_func=lambda r: str(_snap_date(r).date()) if _snap_date(r) is not None else "")

    if is_today:
        _update_position_settles(fs, holdings, monitors)
    return row


def _write_placeholder(fs, day):
    """结算价未出：建一条只有日期的占位记录，次日补算。"""
    row = {S["date"]: day_to_ms(day), S["updated_at"]: now_ms()}
    fs.upsert(cfg.TABLES["snapshot"], S["date"], [row],
              key_func=lambda r: str(_snap_date(r).date()) if _snap_date(r) is not None else "")


def _update_position_settles(fs, holdings, monitors):
    """把持仓管理表里在仓品种的「结算价」更新为最新可得的结算价。"""
    try:
        pos_rows = fs.read("position")
    except Exception as e:
        logger.warning(f"读取持仓表失败，跳过结算价更新：{e}")
        return
    if not pos_rows:
        return
    by_name = {}
    for r in pos_rows:
        by_name.setdefault(as_text(r.get(P["name"])), []).append(r)

    updates = []
    for name in {n for (n, _d) in holdings}:
        code = as_text((monitors.get(name) or {}).get(M["code"]))
        df = akd.get_daily(code) if code else None
        st = akd.latest_settle(df)          # 最新可得的结算价
        if st is None:
            continue
        for r in by_name.get(name, []):
            updates.append({"record_id": r["_id"],
                            "fields": {P["settle"]: round(st, 4), P["updated_at"]: now_ms()}})
    if updates:
        fs.batch_update(cfg.TABLES["position"], updates)
        logger.info(f"持仓结算价已更新 {len(updates)} 条")
