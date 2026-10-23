# -*- coding: utf-8 -*-
"""离线回归测试：不依赖飞书网络，用内存假表验证核心业务逻辑。

运行：python tests/test_offline.py
"""
import os
import sys
import unittest
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg
import daily_snapshot
import position
import performance
import indicators as ind
import scoring as sc


def ms(d):
    return int(pd.Timestamp(d).timestamp() * 1000)


class FakeFS:
    """内存版飞书表，模拟 read/batch_*/upsert。"""

    def __init__(self, tables=None):
        self.tables = {k: list(v) for k, v in (tables or {}).items()}
        self._seq = 0

    def read(self, name):
        return [dict(r) for r in self.tables.get(name, [])]

    def _new_id(self):
        self._seq += 1
        return f"rec{self._seq:04d}"

    def batch_create(self, tid, rows):
        name = self._name(tid)
        for r in rows:
            row = dict(r)
            row["_id"] = self._new_id()
            self.tables.setdefault(name, []).append(row)
        return [r["_id"] for r in self.tables.get(name, [])]

    def batch_update(self, tid, updates):
        name = self._name(tid)
        idx = {r["_id"]: r for r in self.tables.get(name, [])}
        for u in updates:
            if u["record_id"] in idx:
                idx[u["record_id"]].update(u["fields"])

    def update(self, tid, rid, fields):
        self.batch_update(tid, [{"record_id": rid, "fields": fields}])

    def batch_delete(self, tid, ids):
        name = self._name(tid)
        self.tables[name] = [r for r in self.tables.get(name, []) if r["_id"] not in ids]

    def upsert(self, tid, key_field, rows, key_func=None):
        name = self._name(tid)
        existing = {key_func(r): r["_id"] for r in self.tables.get(name, [])}
        create, update = [], []
        for r in rows:
            k = key_func(r) if key_func else r.get(key_field)
            if k in existing:
                update.append({"record_id": existing[k], "fields": r})
            else:
                create.append(r)
        if create:
            self.batch_create(tid, create)
        if update:
            self.batch_update(tid, update)
        return len(create), len(update)

    def _name(self, tid):
        for k, v in cfg.TABLES.items():
            if v == tid:
                return k
        return tid


def make_daily(prices, start="2026-01-05", settle_offset=None):
    """用收盘价序列造一份日线（工作日）。"""
    dates = pd.bdate_range(start, periods=len(prices))
    return pd.DataFrame({
        "date": dates,
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [1000] * len(prices),
        "oi": [10000] * len(prices),
        "settle": settle_offset or prices,
    })


class TestIndicators(unittest.TestCase):
    def test_weekly_aggregation(self):
        daily = make_daily([100, 101, 102, 103, 104, 105, 106, 107, 108, 109])
        wk = ind.build_weekly(daily)
        self.assertEqual(wk.iloc[0]["open"], 100)
        self.assertEqual(wk.iloc[0]["close"], 104)
        self.assertEqual(wk.iloc[0]["high"], 104 * 1.01)
        self.assertEqual(wk.iloc[0]["volume"], 5000)
        self.assertEqual(wk.iloc[1]["open"], 105)

    def test_weekly_oi_is_last(self):
        daily = make_daily([100] * 5)
        daily["oi"] = [1, 2, 3, 4, 5]
        wk = ind.build_weekly(daily)
        self.assertEqual(wk.iloc[0]["oi"], 5)

    def test_score_range_and_color(self):
        n = 60
        up = make_daily([100 + i for i in range(n)])
        feat = ind.week_features(ind.build_weekly(up))
        s, col = sc.score(feat)
        self.assertGreaterEqual(s, -100)
        self.assertLessEqual(s, 100)
        self.assertIn(col, cfg.OPTIONS["power"])

    def test_power_color(self):
        f = dict(hist_up=True, hist_down=False, ema12_up=True, ema12_down=False)
        self.assertEqual(sc.power_color(f), "绿色")
        f = dict(hist_up=False, hist_down=True, ema12_up=False, ema12_down=True)
        self.assertEqual(sc.power_color(f), "红色")
        f = dict(hist_up=True, hist_down=False, ema12_up=False, ema12_down=True)
        self.assertEqual(sc.power_color(f), "蓝色")


class TestPosition(unittest.TestCase):
    def _rows(self, specs):
        out = []
        for i, s in enumerate(specs):
            row = {
                cfg.JOURNAL_FIELDS["date"]: ms(s["date"]),
                cfg.JOURNAL_FIELDS["name"]: s["name"],
                cfg.JOURNAL_FIELDS["tactic"]: s["tactic"],
                cfg.JOURNAL_FIELDS["direction"]: s["direction"],
                cfg.JOURNAL_FIELDS["contract"]: "C2701",
            }
            if s["tactic"] in cfg.CLOSE_TACTICS:
                row[cfg.JOURNAL_FIELDS["close_px"]] = s["px"]
                row[cfg.JOURNAL_FIELDS["close_qty"]] = s["qty"]
            else:
                row[cfg.JOURNAL_FIELDS["open_px"]] = s["px"]
                row[cfg.JOURNAL_FIELDS["open_qty"]] = s["qty"]
            out.append({**row, "_id": f"j{i}"})
        return out

    def test_weighted_avg_and_partial_close(self):
        logs = self._rows([
            dict(date="2026-10-01", name="玉米", tactic="开仓", direction="多", px=2200, qty=2),
            dict(date="2026-10-02", name="玉米", tactic="加仓", direction="多", px=2300, qty=2),
            dict(date="2026-10-03", name="玉米", tactic="出场", direction="多", px=2400, qty=1),
        ])
        fs = FakeFS({"journal": logs,
                     "monitor": [{cfg.MONITOR_FIELDS["name"]: "玉米", "_id": "m1",
                                  cfg.MONITOR_FIELDS["holding"]: "否"}],
                     "position": []})
        pos = position.sync(fs, dry_run=False)
        self.assertEqual(pos[("玉米", "多")]["qty"], 3)
        self.assertAlmostEqual(pos[("玉米", "多")]["avg_px"], 2250)
        held = fs.read("position")
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0][cfg.POS_FIELDS["qty"]], 3)
        self.assertEqual(fs.read("monitor")[0][cfg.MONITOR_FIELDS["holding"]], "是")

    def test_full_close_deletes_row(self):
        logs = self._rows([
            dict(date="2026-10-01", name="玉米", tactic="开仓", direction="多", px=2200, qty=2),
            dict(date="2026-10-02", name="玉米", tactic="止损出场", direction="多", px=2100, qty=2),
        ])
        fs = FakeFS({"journal": logs,
                     "monitor": [{cfg.MONITOR_FIELDS["name"]: "玉米", "_id": "m1",
                                  cfg.MONITOR_FIELDS["holding"]: "是"}],
                     "position": [{cfg.POS_FIELDS["name"]: "玉米", cfg.POS_FIELDS["direction"]: "多",
                                   cfg.POS_FIELDS["qty"]: 2, "_id": "p1"}]})
        position.sync(fs, dry_run=False)
        self.assertEqual(fs.read("position"), [])
        self.assertEqual(fs.read("monitor")[0][cfg.MONITOR_FIELDS["holding"]], "否")

    def test_missing_tactic_warns(self):
        logs = self._rows([
            dict(date="2026-10-01", name="玉米", tactic="", direction="多", px=2200, qty=2),
        ])
        fs = FakeFS({"journal": logs, "monitor": [], "position": []})
        with self.assertLogs("position", level="WARNING"):
            position.sync(fs, dry_run=False)


class TestSnapshot(unittest.TestCase):
    def _monitor(self):
        return [{cfg.MONITOR_FIELDS["name"]: "玉米", cfg.MONITOR_FIELDS["code"]: "C0",
                 cfg.MONITOR_FIELDS["multiplier"]: 10, cfg.MONITOR_FIELDS["margin_rate"]: 0.1,
                 "_id": "m1"}]

    def _patch_daily(self, df):
        daily_snapshot.akd.get_daily = lambda code, **kw: df

    def test_dual_path_open_position(self):
        # 持仓：昨收 2190，今结 2218，开仓均价 2200，2 手，乘数 10
        df = make_daily([2100, 2190, 2218], start="2026-10-20")
        self._patch_daily(df)
        fs = FakeFS({
            "monitor": self._monitor(),
            "journal": [{cfg.JOURNAL_FIELDS["date"]: ms("2026-10-21"),
                         cfg.JOURNAL_FIELDS["name"]: "玉米",
                         cfg.JOURNAL_FIELDS["direction"]: "多",
                         cfg.JOURNAL_FIELDS["tactic"]: "开仓",
                         cfg.JOURNAL_FIELDS["open_px"]: 2200,
                         cfg.JOURNAL_FIELDS["open_qty"]: 2,
                         cfg.JOURNAL_FIELDS["open_fee"]: 5,
                         "_id": "j1"}],
            "position": [{cfg.POS_FIELDS["name"]: "玉米", cfg.POS_FIELDS["direction"]: "多",
                          cfg.POS_FIELDS["qty"]: 2, cfg.POS_FIELDS["avg_px"]: 2200, "_id": "p1"}],
            # 昨日快照已包含浮亏：(2190-2200)*2*10 - 5 = -205
            "snapshot": [{cfg.SNAP_FIELDS["date"]: ms("2026-10-21"),
                          cfg.SNAP_FIELDS["equity"]: 19795.0, "_id": "s1"}],
        })
        row = daily_snapshot.run(fs, today=pd.Timestamp("2026-10-22"))
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[cfg.SNAP_FIELDS["tick_equity"]], 20355.0, places=2)
        self.assertAlmostEqual(row[cfg.SNAP_FIELDS["mtm_equity"]], 20355.0, places=2)
        self.assertEqual(row[cfg.SNAP_FIELDS["ok"]], "是")
        self.assertAlmostEqual(row[cfg.SNAP_FIELDS["margin"]], 4436.0, places=2)

    def test_missing_settle_aborts(self):
        df = make_daily([2100, 2190], start="2026-10-20")  # 没有今天
        self._patch_daily(df)
        fs = FakeFS({
            "monitor": self._monitor(),
            "journal": [],
            "position": [{cfg.POS_FIELDS["name"]: "玉米", cfg.POS_FIELDS["direction"]: "多",
                          cfg.POS_FIELDS["qty"]: 2, cfg.POS_FIELDS["avg_px"]: 2200, "_id": "p1"}],
            "snapshot": [],
        })
        self.assertIsNone(daily_snapshot.run(fs, today=pd.Timestamp("2026-10-22")))
        self.assertEqual(fs.read("snapshot"), [])

    def test_close_row_without_close_px_aborts(self):
        df = make_daily([2100, 2190, 2218], start="2026-10-20")
        self._patch_daily(df)
        fs = FakeFS({
            "monitor": self._monitor(),
            "journal": [{
                cfg.JOURNAL_FIELDS["date"]: ms("2026-10-22"),
                cfg.JOURNAL_FIELDS["name"]: "玉米",
                cfg.JOURNAL_FIELDS["direction"]: "多",
                cfg.JOURNAL_FIELDS["tactic"]: "出场",
                cfg.JOURNAL_FIELDS["close_qty"]: 2,
                "_id": "j1"},
            ],
            "position": [],
            "snapshot": [],
        })
        self.assertIsNone(daily_snapshot.run(fs, today=pd.Timestamp("2026-10-22")))

    def test_computed_pnl_ignores_broken_formula(self):
        """PNL_SOURCE=computed 时，平仓行缺开仓价也能算出正确盈亏。"""
        df = make_daily([2100, 2190, 2218], start="2026-10-20")
        self._patch_daily(df)
        old = cfg.PNL_SOURCE
        cfg.PNL_SOURCE = "computed"
        try:
            fs = FakeFS({
                "monitor": self._monitor(),
                "journal": [
                    {cfg.JOURNAL_FIELDS["date"]: ms("2026-10-21"),
                     cfg.JOURNAL_FIELDS["name"]: "玉米",
                     cfg.JOURNAL_FIELDS["direction"]: "多",
                     cfg.JOURNAL_FIELDS["tactic"]: "开仓",
                     cfg.JOURNAL_FIELDS["open_px"]: 2200,
                     cfg.JOURNAL_FIELDS["open_qty"]: 2,
                     cfg.JOURNAL_FIELDS["open_fee"]: 5,
                     "_id": "j1"},
                    {cfg.JOURNAL_FIELDS["date"]: ms("2026-10-22"),
                     cfg.JOURNAL_FIELDS["name"]: "玉米",
                     cfg.JOURNAL_FIELDS["direction"]: "多",
                     cfg.JOURNAL_FIELDS["tactic"]: "出场",
                     cfg.JOURNAL_FIELDS["close_px"]: 2218,
                     cfg.JOURNAL_FIELDS["close_qty"]: 2,
                     cfg.JOURNAL_FIELDS["close_fee"]: 5,
                     cfg.JOURNAL_FIELDS["pnl"]: 99999,   # 故意给错的公式结果
                     "_id": "j2"},
                ],
                "position": [],
                "snapshot": [{cfg.SNAP_FIELDS["date"]: ms("2026-10-21"),
                              cfg.SNAP_FIELDS["equity"]: 19795.0, "_id": "s1"}],
            })
            row = daily_snapshot.run(fs, today=pd.Timestamp("2026-10-22"))
            self.assertIsNotNone(row)
            # 毛盈亏 (2218-2200)*2*10=360，手续费 5+5=10 -> 20350
            self.assertAlmostEqual(row[cfg.SNAP_FIELDS["tick_equity"]], 20350.0, places=2)
        finally:
            cfg.PNL_SOURCE = old


class TestPerformance(unittest.TestCase):
    def test_period_selection(self):
        """2 月只算月；6 月算月+季；12 月算月+季+年。"""
        feb = performance._periods(pd.Timestamp("2027-02-28"))
        self.assertEqual([x[0] for x in feb], ["月"])
        jun = performance._periods(pd.Timestamp("2027-06-30"))
        self.assertEqual([x[0] for x in jun], ["月", "季"])
        self.assertEqual(jun[1][2], "2027Q2")
        dec = performance._periods(pd.Timestamp("2027-12-31"))
        self.assertEqual([x[0] for x in dec], ["月", "季", "年"])
        self.assertEqual(dec[2][2], "2027Y")

    def test_month_end_only(self):
        fs = FakeFS({"snapshot": [], "journal": []})
        self.assertEqual(performance.run(fs, today=pd.Timestamp("2027-02-20")), [])

    def test_metrics(self):
        snaps = []
        eqs = [20000, 20200, 19900, 20500, 20100]
        for i, eq in enumerate(eqs):
            snaps.append({cfg.SNAP_FIELDS["date"]: ms(f"2026-10-{i + 1:02d}"),
                          cfg.SNAP_FIELDS["equity"]: float(eq), "_id": f"s{i}"})
        fs = FakeFS({"snapshot": snaps, "journal": [
            {cfg.JOURNAL_FIELDS["date"]: ms("2026-10-02"), cfg.JOURNAL_FIELDS["tactic"]: "出场",
             cfg.JOURNAL_FIELDS["direction"]: "多", cfg.JOURNAL_FIELDS["pnl"]: 300, "_id": "j1"},
            {cfg.JOURNAL_FIELDS["date"]: ms("2026-10-03"), cfg.JOURNAL_FIELDS["tactic"]: "止损出场",
             cfg.JOURNAL_FIELDS["direction"]: "空", cfg.JOURNAL_FIELDS["pnl"]: -100, "_id": "j2"},
        ]})
        rows = performance.run(fs, today=pd.Timestamp("2026-10-31"), force=True)
        self.assertEqual(len(rows), 1)          # 10 月不是季末，只出月线
        m = rows[0]
        self.assertEqual(m[cfg.PERF_FIELDS["cycle_id"]], "2026-10")
        self.assertAlmostEqual(m[cfg.PERF_FIELDS["tot_ret"]], 20100 / 20000 - 1, places=4)
        self.assertAlmostEqual(m[cfg.PERF_FIELDS["max_dd"]], 20100 / 20500 - 1, places=4)
        self.assertEqual(m[cfg.PERF_FIELDS["trades"]], 2)
        self.assertAlmostEqual(m[cfg.PERF_FIELDS["win_rate"]], 0.5)
        self.assertAlmostEqual(m[cfg.PERF_FIELDS["pf"]], 3.0)
        self.assertEqual(m[cfg.PERF_FIELDS["consec_win"]], 1)


class TestJournalPnl(unittest.TestCase):
    def _row(self, **kw):
        rid = kw.pop("_id")
        day = kw.pop("_date", None) or kw.get(cfg.JOURNAL_FIELDS["date"])
        row = dict(kw)
        row["_id"] = rid
        row["_date"] = pd.Timestamp(day)
        return row

    def test_long_and_short(self):
        """多：玉米 1500→1480×5手；空：甲醇 3220→3175×2手（部分平仓）。"""
        F = cfg.JOURNAL_FIELDS
        rows = [
            self._row(_id="j1", **{F["date"]: "2026-10-15", F["name"]: "玉米", F["direction"]: "多",
                                   F["tactic"]: "开仓", F["open_px"]: 1500, F["open_qty"]: 5,
                                   F["open_fee"]: 6.6}),
            self._row(_id="j2", **{F["date"]: "2026-10-16", F["name"]: "玉米", F["direction"]: "多",
                                   F["tactic"]: "出场", F["close_px"]: 1480, F["close_qty"]: 5,
                                   F["close_fee"]: 6.6}),
            self._row(_id="j3", **{F["date"]: "2026-10-19", F["name"]: "甲醇", F["direction"]: "空",
                                   F["tactic"]: "开仓", F["open_px"]: 3220, F["open_qty"]: 4,
                                   F["open_fee"]: 14.168}),
            self._row(_id="j4", **{F["date"]: "2026-10-22", F["name"]: "甲醇", F["direction"]: "空",
                                   F["tactic"]: "出场", F["close_px"]: 3175, F["close_qty"]: 2,
                                   F["close_fee"]: 6.985}),
        ]
        pnl, warns = position.compute_journal_pnl(rows, {"玉米": 10, "甲醇": 10})
        self.assertEqual(warns, [])
        self.assertAlmostEqual(pnl["j2"], -1013.2, places=2)     # (1480-1500)*10*5-6.6-6.6
        self.assertAlmostEqual(pnl["j4"], 885.93, places=2)      # (3220-3175)*10*2-7.084-6.985
        self.assertNotIn("j1", pnl)                              # 开仓行不写
        self.assertNotIn("j3", pnl)

    def test_no_open_block(self):
        F = cfg.JOURNAL_FIELDS
        rows = [self._row(_id="j9", **{F["date"]: "2026-10-20", F["name"]: "玉米", F["direction"]: "多",
                                       F["tactic"]: "出场", F["close_px"]: 2200, F["close_qty"]: 1})]
        pnl, warns = position.compute_journal_pnl(rows, {"玉米": 10})
        self.assertEqual(pnl, {})
        self.assertEqual(len(warns), 1)


class TestHistory(unittest.TestCase):
    def test_routing(self):
        import history
        self.assertEqual(history.table_for("玉米"), "history")
        self.assertEqual(history.table_for("甲醇"), "history2")
        self.assertIsNone(history.table_for("不存在的品种"))

    def test_csv_roundtrip(self):
        import tempfile
        import history
        old = cfg.DATA_DIR
        cfg.DATA_DIR = tempfile.mkdtemp()
        try:
            rec = history.make_record("玉米", "C2701", 2218.0, -65, "蓝色", 1792760000000)
            history.append_csv({"history": [rec]})
            df = history.read_csv_all(cfg.DATA_DIR)
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["品种"], "玉米")
            self.assertEqual(int(df.iloc[0]["趋势评分"]), -65)
        finally:
            cfg.DATA_DIR = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
