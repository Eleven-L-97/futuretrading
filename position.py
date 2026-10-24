# -*- coding: utf-8 -*-
"""模块二：交易日志 -> 持仓管理 -> 回写「是否持仓」。

规则（对应需求文档）：
- 开平仓由「战术」字段判断：出场 / 止损出场 / 止盈出场 = 平仓，其余非空 = 开仓
- 战术为空 -> 记录警告并跳过该行（提示补全）
- 开仓：同品种同方向已有持仓 -> 数量相加、均价加权；否则新增
- 平仓：平仓量 < 持仓量 -> 数量扣减、均价不变；相等 -> 删除该行；大于 -> 警告并按全平处理
- 交易日志只读，不修改、不删除
"""
import logging
from datetime import date as _date

import pandas as pd

import config as cfg
from feishu import as_ms, as_num, as_text, ms_to_day, now_ms

logger = logging.getLogger(__name__)

F = cfg.JOURNAL_FIELDS
P = cfg.POS_FIELDS
M = cfg.MONITOR_FIELDS

EPS = 1e-9


def norm_direction(v):
    s = as_text(v)
    if s in ("多", "做多", "买入", "买", "long", "Long"):
        return "多"
    if s in ("空", "做空", "卖出", "卖", "short", "Short"):
        return "空"
    return s or None


def row_date(row):
    ms = as_ms(row.get(F["date"]))
    if ms is None:
        return None
    return ms_to_day(ms)


def journal_rows(fs):
    """读取交易日志并做基础清洗，按日期升序。"""
    logs = fs.read("journal")
    rows = []
    for r in logs:
        name = as_text(r.get(F["name"]))
        if not name:
            continue
        d = row_date(r)
        if d is None:
            logger.warning(f"交易日志缺少日期，已跳过：{name}")
            continue
        r["_date"] = d
        rows.append(r)
    rows.sort(key=lambda x: x["_date"])
    return rows


def replay(rows, until=None):
    """按日期回放交易日志，返回 {(品种, 方向): {qty, avg_px, contract}}。

    只读计算，不关心战术缺失行（缺战术的会在 sync 中告警）。
    """
    pos = {}
    for r in rows:
        d = r["_date"]
        if until is not None and d > until:
            break
        tactic = as_text(r.get(F["tactic"]))
        name = as_text(r.get(F["name"]))
        direction = norm_direction(r.get(F["direction"]))
        if not tactic or not direction:
            continue
        is_close = tactic in cfg.CLOSE_TACTICS
        key = (name, direction)
        if not is_close:
            qty = as_num(r.get(F["open_qty"]), 0) or 0
            px = as_num(r.get(F["open_px"]), 0) or 0
            if qty <= 0 or px <= 0:
                continue
            if key in pos:
                p = pos[key]
                new_qty = p["qty"] + qty
                p["avg_px"] = (p["qty"] * p["avg_px"] + qty * px) / new_qty
                p["qty"] = new_qty
                p["contract"] = as_text(r.get(F["contract"])) or p["contract"]
            else:
                pos[key] = {"qty": qty, "avg_px": px, "contract": as_text(r.get(F["contract"]))}
        else:
            qty = as_num(r.get(F["close_qty"]), 0) or 0
            if qty <= 0 or key not in pos:
                continue
            if qty < pos[key]["qty"] - EPS:
                pos[key]["qty"] -= qty
            else:
                del pos[key]
    return pos


def realized_gross(rows, mult_of):
    """按加权平均成本回放，算出已平仓的**毛盈亏**（不含手续费）。

    返回 (毛盈亏, 已平仓部分分摊的开仓手续费, 平仓手续费合计)。

    这样算而不直接用飞书「盈亏数额」公式，是因为公式字段只看本行：
    - 平仓行不填开仓价时，公式会按 0 计算，得出离谱结果；
    - 平仓行的「开仓手续费」通常算不出来（开仓数量为空），导致开仓费漏计。
    """
    pos = {}
    gross = 0.0
    open_fee_closed = 0.0
    close_fee = 0.0
    for r in rows:
        tactic = as_text(r.get(F["tactic"]))
        name = as_text(r.get(F["name"]))
        direction = norm_direction(r.get(F["direction"]))
        if not tactic or not direction:
            continue
        key = (name, direction)
        mult = mult_of.get(name, 1) or 1
        sign = 1 if direction == "多" else -1
        if tactic in cfg.CLOSE_TACTICS:
            qty = as_num(r.get(F["close_qty"]), 0) or 0
            px = as_num(r.get(F["close_px"]), 0) or 0
            close_fee += as_num(r.get(F["close_fee"]), 0) or 0
            if key not in pos or qty <= 0 or px <= 0:
                continue
            held = pos[key]
            q = min(qty, held["qty"])
            gross += (px - held["avg_px"]) * q * mult * sign
            # 按平仓比例分摊该头寸剩余的开仓手续费
            if held["qty"] > EPS and held["fee"] > 0:
                share = held["fee"] * q / held["qty"]
                open_fee_closed += share
                held["fee"] -= share
            if q < held["qty"] - EPS:
                held["qty"] -= q
            else:
                del pos[key]
        else:
            qty = as_num(r.get(F["open_qty"]), 0) or 0
            px = as_num(r.get(F["open_px"]), 0) or 0
            fee = as_num(r.get(F["open_fee"]), 0) or 0
            if qty <= 0 or px <= 0:
                continue
            if key in pos:
                p = pos[key]
                new_qty = p["qty"] + qty
                p["avg_px"] = (p["qty"] * p["avg_px"] + qty * px) / new_qty
                p["qty"] = new_qty
                p["fee"] += fee
            else:
                pos[key] = {"qty": qty, "avg_px": px, "fee": fee}
    return gross, open_fee_closed, close_fee


def today_flows(rows, day):
    """统计某一天的开平仓流水：{(品种,方向): {add_qty, add_px, close_qty, close_px}}"""
    flows = {}
    for r in rows:
        if r["_date"] != day:
            continue
        tactic = as_text(r.get(F["tactic"]))
        direction = norm_direction(r.get(F["direction"]))
        if not tactic or not direction:
            continue
        key = (as_text(r.get(F["name"])), direction)
        f = flows.setdefault(key, {"add_qty": 0.0, "add_px": 0.0, "close_qty": 0.0, "close_px": 0.0})
        if tactic in cfg.CLOSE_TACTICS:
            q = as_num(r.get(F["close_qty"]), 0) or 0
            p = as_num(r.get(F["close_px"]), 0) or 0
            f["close_qty"] += q
            f["close_px"] += q * p
        else:
            q = as_num(r.get(F["open_qty"]), 0) or 0
            p = as_num(r.get(F["open_px"]), 0) or 0
            f["add_qty"] += q
            f["add_px"] += q * p
    for f in flows.values():
        f["add_px"] = f["add_px"] / f["add_qty"] if f["add_qty"] else 0.0
        f["close_px"] = f["close_px"] / f["close_qty"] if f["close_qty"] else 0.0
    return flows


def compute_journal_pnl(rows, mult_of):
    """计算每条平仓记录的「盈亏数额」，与持仓管理的加权平均成本保持一致。

    规则：
    - 维护每个 (品种, 方向) 的持仓池：数量、加权均价、剩余开仓手续费。
    - 平仓：用**整个持仓池**的加权均价作为成本（而不是只看「上一次平仓之后」的开仓行）。
    - 多：(平仓价 - 均价) × 乘数 × 平仓数量 - 开仓手续费分摊 - 平仓手续费
      空：(均价 - 平仓价) × 乘数 × 平仓数量 - 开仓手续费分摊 - 平仓手续费
    - 开仓手续费按平仓数量占池中数量的比例分摊。

    为什么不用「两次平仓之间的开仓行」：当一次平仓的数量大于这段时间新开的数量时，
    会用到更早批次的老仓，那批老仓的成本价不一样，只按新开仓行算会把盈亏算高/算低。
    """
    pools = {}   # key -> {"qty": 剩余数量, "amount": 剩余成本金额, "fee": 剩余开仓手续费}
    out = {}
    warns = []
    for r in rows:
        tactic = as_text(r.get(F["tactic"]))
        name = as_text(r.get(F["name"]))
        direction = norm_direction(r.get(F["direction"]))
        if not tactic or not name or not direction:
            continue
        key = (name, direction)
        if tactic in cfg.CLOSE_TACTICS:
            qty = as_num(r.get(F["close_qty"]), 0) or 0
            px = as_num(r.get(F["close_px"]), 0) or 0
            fee_close = as_num(r.get(F["close_fee"]), 0) or 0
            pool = pools.get(key)
            if not pool or pool["qty"] <= EPS:
                warns.append(f"{r['_date'].date()} {name} {direction} 平仓行找不到对应开仓行，盈亏数额未计算")
                continue
            if qty <= 0 or px <= 0:
                warns.append(f"{r['_date'].date()} {name} {direction} 平仓行缺平仓价/数量，盈亏数额未计算")
                continue
            held_qty = pool["qty"]
            q = min(qty, held_qty)
            if qty > held_qty + EPS:
                warns.append(f"{r['_date'].date()} {name} {direction} 平仓 {qty} 手大于持仓 {held_qty} 手，按 {held_qty} 手计算")
            avg = pool["amount"] / held_qty
            fee_open = pool["fee"] * q / held_qty
            mult = mult_of.get(name, 1) or 1
            sign = 1 if direction == "多" else -1
            out[r["_id"]] = round((px - avg) * mult * q * sign - fee_open - fee_close, 2)
            # 回写持仓池
            pool["qty"] -= q
            pool["amount"] -= avg * q
            pool["fee"] -= fee_open
            if pool["qty"] <= EPS:
                pools.pop(key, None)
        else:
            qty = as_num(r.get(F["open_qty"]), 0) or 0
            px = as_num(r.get(F["open_px"]), 0) or 0
            fee = as_num(r.get(F["open_fee"]), 0) or 0
            if qty > 0 and px > 0:
                pool = pools.setdefault(key, {"qty": 0.0, "amount": 0.0, "fee": 0.0})
                pool["qty"] += qty
                pool["amount"] += qty * px
                pool["fee"] += fee
    return out, warns


def sync(fs, dry_run=False, pending=False):
    """执行持仓归集并回写表格。返回当前持仓字典。

    pending=True 时只处理「状态=已完成」的记录，处理完把状态改成「已更新」，
    供飞书自动化触发时使用；没有待处理记录则直接返回，避免自动化循环触发。
    """
    rows = journal_rows(fs)
    if not rows:
        logger.info("交易日志为空：按「无持仓」处理，清空持仓表并回写「是否持仓=否」")

    status_key = F.get("status")
    pending_rows = [r for r in rows if status_key and as_text(r.get(status_key)) == "已完成"]
    if pending and not pending_rows:
        logger.info("没有「状态=已完成」的交易日志，跳过本次处理（防止自动化循环触发）")
        return {}

    # 提示缺战术字段的行
    for r in rows:
        if not as_text(r.get(F["tactic"])):
            logger.warning(f"交易日志缺少战术字段，请补全：{r['_date'].date()} "
                           f"{as_text(r.get(F['name']))} {as_text(r.get(F['direction']))}")

    pos = replay(rows)
    ts_now = now_ms()

    # ---------- 计算并写入交易日志「盈亏数额」（开仓行留空） ----------
    monitor = fs.read("monitor")
    mult_of = {as_text(m.get(M["name"])): (as_num(m.get(M["multiplier"]), 1) or 1) for m in monitor}
    pnl_map, pnl_warns = compute_journal_pnl(rows, mult_of)
    for w in pnl_warns:
        logger.warning(w)
    journal_updates = [{"record_id": rid, "fields": {F["pnl"]: val}} for rid, val in pnl_map.items()]
    journal_clears = [
        {"record_id": r["_id"], "fields": {F["pnl"]: None}}
        for r in rows
        if as_text(r.get(F["tactic"])) not in cfg.CLOSE_TACTICS
        and r.get(F["pnl"]) not in (None, "")
    ]
    logger.info(f"交易日志盈亏：计算 {len(journal_updates)} 条平仓行，清空 {len(journal_clears)} 条开仓行")

    pos_rows = fs.read("position")
    existing, stale = {}, []
    for r in pos_rows:
        name = as_text(r.get(P["name"]))
        d = norm_direction(r.get(P["direction"]))
        key = (name, d)
        if not name or not d:
            stale.append(r["_id"])          # 空模板行，直接清理
        elif key in existing:
            stale.append(existing[key])     # 重复行，保留最后一条
            existing[key] = r["_id"]
        else:
            existing[key] = r["_id"]

    to_create, to_update = [], []
    for (name, direction), p in pos.items():
        fields = {
            P["name"]: name,
            P["direction"]: direction,
            P["qty"]: p["qty"],
            P["avg_px"]: round(p["avg_px"], 4),
            P["updated_at"]: ts_now,
        }
        if p.get("contract"):
            fields[P["contract"]] = p["contract"]
        rid = existing.get((name, direction))
        if rid:
            to_update.append({"record_id": rid, "fields": fields})
        else:
            to_create.append(fields)

    stale += [rid for key, rid in existing.items() if key not in pos]

    holdings = {k[0] for k in pos}
    hold_updates = []
    for m in monitor:
        name = as_text(m.get(M["name"]))
        want = "是" if name in holdings else "否"
        if as_text(m.get(M["holding"])) != want:
            hold_updates.append({"record_id": m["_id"],
                                 "fields": {M["holding"]: want, M["updated_at"]: ts_now}})

    logger.info(f"持仓归集：在仓 {len(pos)} 个（{sorted(holdings)}），"
                f"新增 {len(to_create)}，更新 {len(to_update)}，清仓删除 {len(stale)}，"
                f"待标记已更新 {len(pending_rows)}")

    if dry_run:
        logger.info("[dry-run] 不写入飞书")
        for rid, val in list(pnl_map.items())[:5]:
            logger.info(f"[dry-run] 盈亏数额 {rid} = {val}")
        return pos

    if journal_updates:
        fs.batch_update(cfg.TABLES["journal"], journal_updates)
    if journal_clears:
        fs.batch_update(cfg.TABLES["journal"], journal_clears)
    if status_key and pending_rows:
        fs.batch_update(cfg.TABLES["journal"],
                        [{"record_id": r["_id"], "fields": {status_key: "已更新"}}
                         for r in pending_rows])
    if to_create:
        fs.batch_create(cfg.TABLES["position"], to_create)
    if to_update:
        fs.batch_update(cfg.TABLES["position"], to_update)
    if stale:
        fs.batch_delete(cfg.TABLES["position"], stale)
    if hold_updates:
        fs.batch_update(cfg.TABLES["monitor"], hold_updates)
    return pos
