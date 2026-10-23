# -*- coding: utf-8 -*-
"""历史数据复盘：双表路由、CSV 归档、每周清理。

- 两张飞书表按品种拆分（config.HISTORY_SPLIT）
- 每 15 分钟写入一次（config.HISTORY_INTERVAL_MIN）
- 同时追加到 data/history.csv / data/history2.csv，由 GitHub Actions 提交进仓库
- 飞书里只保留「当周」数据，新的一周第一次运行时会清掉上周记录
"""
import csv
import logging
import os
from datetime import datetime

import pandas as pd

import config as cfg
from feishu import as_text, day_to_ms, ms_to_str, now_ms

logger = logging.getLogger(__name__)

H = cfg.HIST_FIELDS
CSV_HEADER = ["时间", "品种", "合约", "价格", "趋势评分", "动力系统"]


def table_for(name):
    """按品种名找到历史表（history / history2）。"""
    for tname, names in cfg.HISTORY_SPLIT.items():
        if name in names:
            return tname
    return None


def history_due(ts_ms=None):
    """距上一次写入是否已满 HISTORY_INTERVAL_MIN 分钟。

    以 CSV 里最后一条记录为准，而不是看"当前分钟数"——GitHub 的 cron 经常延迟
    5–15 分钟，按分钟对齐会漏掉窗口。第一次运行为空时直接写。
    """
    ts_ms = ts_ms or now_ms()
    last = last_written_ms("history") or last_written_ms("history2")
    if last is None:
        return True
    interval_ms = max(int(cfg.HISTORY_INTERVAL_MIN), 1) * 60 * 1000
    return (ts_ms - last) >= interval_ms - 60 * 1000     # 留 1 分钟容差


def csv_path(table_name):
    return os.path.join(cfg.DATA_DIR, f"{table_name}.csv")


def append_csv(records):
    """按表把记录追加到 CSV。records: {table_name: [dict]}"""
    os.makedirs(cfg.DATA_DIR, exist_ok=True)
    for tname, recs in records.items():
        if not recs:
            continue
        path = csv_path(tname)
        new_file = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(CSV_HEADER)
            for r in recs:
                w.writerow([r["time_str"], r["name"], r["contract"],
                            r["price"], r["score"], r["power"]])
        logger.info(f"CSV 追加 {len(recs)} 条 -> {path}")


def to_feishu_row(rec):
    return {
        H["time"]: rec["time_ms"],
        H["name"]: rec["name"],
        H["contract"]: rec["contract"] or "",
        H["score"]: rec["score"],
        H["power"]: rec["power"],
        H["price"]: rec["price"],
    }


def make_record(name, contract, price, score, power, time_ms):
    return {
        "time_ms": int(time_ms),
        "time_str": ms_to_str(time_ms),
        "name": name,
        "contract": contract or "",
        "price": float(price),
        "score": int(score),
        "power": power,
    }


def _week_start_ms():
    day = pd.Timestamp.now(tz=cfg.LOCAL_TZ).tz_localize(None).normalize()
    monday = day - pd.Timedelta(days=day.weekday())
    return day_to_ms(monday)


def clear_previous_weeks(fs):
    """删除两张历史表中「本周一之前」的记录。"""
    ws = _week_start_ms()
    for tname in cfg.HISTORY_SPLIT:
        tid = cfg.TABLES[tname]
        flt = {"conjunction": "and", "conditions": [
            {"field_name": H["time"], "operator": "isLess", "value": ["ExactDate", str(ws)]}]}
        try:
            if fs.count(tid, flt) == 0:
                continue
            ids = fs.find_ids(tid, flt)
            if ids:
                fs.batch_delete(tid, ids)
                logger.info(f"{tname} 清理上周历史记录 {len(ids)} 条")
        except Exception as e:
            logger.warning(f"{tname} 清理上周记录失败：{e}")


def export_from_feishu(fs):
    """把飞书两张历史表的现有记录补进 CSV（一次性迁移/补档用，按 时间+品种 去重）。"""
    existing = set()
    for tname in cfg.HISTORY_SPLIT:
        path = csv_path(tname)
        if os.path.exists(path):
            try:
                df = pd.read_csv(path, encoding="utf-8-sig")
                existing |= set(zip(df["时间"].astype(str), df["品种"].astype(str)))
            except Exception:
                pass

    total = 0
    for tname in cfg.HISTORY_SPLIT:
        recs = []
        for r in fs.read(tname):
            ms = r.get(H["time"])
            name = as_text(r.get(H["name"]))
            if not ms or not name:
                continue
            key = (ms_to_str(ms), name)
            if key in existing:
                continue
            recs.append(make_record(name, as_text(r.get(H["contract"])),
                                    r.get(H["price"]) or 0, r.get(H["score"]) or 0,
                                    as_text(r.get(H["power"])) or "蓝色", ms))
            existing.add(key)
        recs.sort(key=lambda x: x["time_ms"])
        if recs:
            append_csv({tname: recs})
            total += len(recs)
    logger.info(f"历史 CSV 补档完成，新增 {total} 条")
    return total


def last_written_ms(table_name="history"):
    """CSV 里最后一条记录的时间（毫秒），用于同一 15 分钟窗口去重。"""
    path = csv_path(table_name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4000))
            lines = f.read().decode("utf-8-sig", "replace").strip().splitlines()
        if len(lines) < 2:
            return None
        ts = pd.Timestamp(lines[-1].split(",")[0])
        return int(ts.tz_localize(cfg.LOCAL_TZ).timestamp() * 1000)
    except Exception:
        return None


def already_written(ts_ms):
    """当前 15 分钟窗口是否已经写过（防 cron 延迟导致同一窗口写两次）。"""
    last = last_written_ms("history") or last_written_ms("history2")
    if last is None:
        return False
    bucket = max(int(cfg.HISTORY_INTERVAL_MIN), 1) * 60 * 1000
    return last // bucket == int(ts_ms) // bucket


def read_csv_all(data_dir=None):
    """读取两个 CSV 合并成一个 DataFrame（绘图用）。"""
    data_dir = data_dir or cfg.DATA_DIR
    frames = []
    for tname in cfg.HISTORY_SPLIT:
        path = os.path.join(data_dir, f"{tname}.csv")
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, encoding="utf-8-sig")
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=CSV_HEADER)
    df = pd.concat(frames, ignore_index=True)
    df["时间"] = pd.to_datetime(df["时间"], errors="coerce")
    df = df.dropna(subset=["时间"]).sort_values("时间").reset_index(drop=True)
    return df
