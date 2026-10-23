# -*- coding: utf-8 -*-
"""全局配置：凭证、常量、表 ID、字段名、品种映射。

凭证读取顺序：环境变量 > 同目录 .env 文件 > 空。
本地开发把凭证写在 .env（已在 .gitignore 中，不会提交）；
GitHub Actions 用仓库 Secrets 注入环境变量。
"""
import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_dotenv(path=None):
    """极简 .env 解析（KEY=VALUE，支持 # 注释）。"""
    path = path or os.path.join(_BASE_DIR, ".env")
    values = {}
    if not os.path.exists(path):
        return values
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip().strip('"').strip("'")
    except Exception:
        pass
    return values


_DOTENV = _read_dotenv()


def _cred(key, default=""):
    return os.getenv(key) or _DOTENV.get(key) or default


# ============ 飞书应用凭证 ============
APP_ID = _cred("FEISHU_APP_ID", "cli_aa5b6fc5ef385d2a")
APP_SECRET = _cred("FEISHU_APP_SECRET", "")
APP_TOKEN = _cred("FEISHU_APP_TOKEN", "RDX2bwQhfaYeRhsZAwUcW6VYned")

# ============ 可调常量（后续要改就改这里） ============
INITIAL_EQUITY = 20000.0      # 初始本金，增资到10万时改成 100000.0
TOLERANCE = 0.1               # 双路径校验容差（元）
RISK_FREE_ANNUAL = 0.025      # 无风险利率
TRADING_DAYS = 252            # 夏普年化系数
HPI_CF = 100.0                # HPI 每点价值（赫里克默认100；白银类品种可改为50）
HPI_S = 0.133                 # HPI 平滑因子
WEEK_START_WEEKDAY = 0        # 周K起点：0=周一
LOCAL_TZ = os.getenv("TRADING_TZ", "Asia/Shanghai")   # 业务时区（飞书日期按此时区解析）
DAILY_BARS = 800              # 每次取多少根日线合成周K（约3年，够EMA26/HPI预热）

# ============ 历史数据复盘 ============
# 写入节奏：每 HISTORY_INTERVAL_MIN 分钟一次（实时行情仍按 5 分钟刷新）
HISTORY_INTERVAL_MIN = int(os.getenv("HISTORY_INTERVAL_MIN", "15"))
# always=到点就写；change=到点且评分/颜色变化才写；off=不写
HISTORY_MODE = os.getenv("HISTORY_MODE", "always")
# 两张历史表按品种拆分，字段完全一致
HISTORY_SPLIT = {
    "history": ["玉米", "玉米淀粉", "工业硅", "豆粕", "苯乙烯", "聚丙烯",
                "乙二醇", "塑料", "PVC", "纯苯", "棕榈油", "铁矿石", "焦煤", "焦炭"],
    "history2": ["菜籽粕", "菜籽油", "玻璃", "螺纹钢", "甲醇", "纯碱", "PTA",
                 "短纤", "氧化铝", "白糖", "花生", "瓶片", "对二甲苯", "尿素"],
}
DATA_DIR = os.getenv("DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ============ 交易日志 ============
# 「盈亏数额」由脚本计算并写入（开仓行留空）：
#   多：(平仓价 - 开仓均价) × 合约乘数 × 平仓数量 - 开仓手续费 - 平仓手续费
#   空：(开仓均价 - 平仓价) × 合约乘数 × 平仓数量 - 开仓手续费 - 平仓手续费
# 开仓均价 = 上一条平仓记录之后、本条平仓记录之前的所有开仓行加权平均价
JOURNAL_PNL_INCLUDES_FEE = True
# 已实现盈亏取数口径：computed=脚本按开平价算（推荐）；journal=累加「盈亏数额」
PNL_SOURCE = os.getenv("PNL_SOURCE", "computed")
# 交易日志缺关键字段时是否中止每日快照
STRICT_JOURNAL = True
# 开平仓判定：交易日志「战术」字段命中这些值即视为平仓，其余非空即开仓
CLOSE_TACTICS = {"出场", "止损出场", "止盈出场"}

# ============ 表 ID ============
TABLES = {
    "monitor":  "tbl57sG7koK18937",   # 实时盯盘数据
    "history":  "tblRnWFiCGkn0Pcy",   # 历史数据复盘1
    "history2": "tblDlxbPArgCFFPR",   # 历史数据复盘2
    "journal":  "tblFEUuXYvm5RfG2",   # 交易日志
    "position": "tbl03U9IOrA88uAj",   # 持仓管理
    "snapshot": "tblFk7Q9qPgiHgrx",   # 每日账户快照
    "perf":     "tbl9q7VsknVwsoRV",   # 绩效指标
}

# ============ 字段名（与飞书表头一字不差） ============
MONITOR_FIELDS = {
    "name": "期货品种", "code": "代码", "exchange": "上市交易所",
    "multiplier": "合约乘数", "fee_per_lot": "手续费（元/手）",
    "fee_ratio": "手续费（成交金额比例）", "margin_rate": "保证金",
    "price": "当前价格", "min_tick": "最小变动单位",
    "night_session": "交易时间（有无夜盘）",
    "contract": "合约名称", "week_trend": "周趋势",
    "score": "趋势评分", "trend_start": "趋势开始时间",
    "trend_weeks": "趋势持续时间（周）", "pattern": "趋势图形态",
    "power": "动力系统", "holding": "是否持仓",
    "strategy": "战略", "action": "行动计划",
    "feasible": "可行性评分", "updated_at": "最后更新时间",
}
HIST_FIELDS = {
    "time": "记录时间", "name": "期货品种", "contract": "合约名称",
    "score": "趋势评分", "power": "动力系统", "price": "当前价格",
}
JOURNAL_FIELDS = {
    "date": "日期", "weekday": "周几", "name": "期货品种",
    "tactic": "战术", "direction": "方向", "contract": "合约名称",
    "open_px": "开仓价", "open_qty": "开仓数量", "open_fee": "开仓手续费",
    "close_px": "平仓价", "close_qty": "平仓数量", "close_fee": "平仓手续费",
    "pnl": "盈亏数额", "pnl_ratio": "盈亏比例", "updated_at": "最后更新时间",
}
POS_FIELDS = {
    "name": "期货品种", "contract": "合约名称", "qty": "持仓数量",
    "direction": "方向", "avg_px": "开仓均价",
    "sl": "止损点", "tp": "止盈点", "settle": "结算价",
    "risk_ratio": "风险比例", "fund_ratio": "资金占用比例",
    "updated_at": "最后更新时间",
}
SNAP_FIELDS = {
    "date": "快照日期", "equity": "账户价值", "available": "可用资金",
    "margin": "保证金", "usage": "资金使用率", "daily_pnl": "当日盈亏",
    "daily_ret": "当日收益率", "cum_pnl": "累计盈亏", "cum_fee": "累计手续费",
    "settle": "结算价", "mtm_equity": "盯市账户价值",
    "tick_equity": "逐笔账户价值", "ok": "校验是否通过",
    "updated_at": "最后更新时间",
}
PERF_FIELDS = {
    "cycle_id": "周期标识", "period": "统计周期", "start_date": "周期起始日",
    "end_date": "周期截止日", "ann_ret": "年化收益率", "tot_ret": "总收益率",
    "max_dd": "最大回撤", "max_dd_days": "最长回撤修复天数",
    "calmar": "卡玛比率", "sharpe": "夏普比率", "trades": "总交易笔数",
    "win_rate": "胜率", "pf": "盈亏因子", "avg_rr": "平均盈亏比",
    "big_win": "单笔最大盈利", "big_loss": "单笔最大亏损",
    "consec_win": "连续盈利次数", "consec_loss": "连续亏损次数",
    "long_contrib": "多头贡献利润占比", "short_contrib": "空头贡献利润占比",
    "remark": "备注",
}

# 单选选项（写入时必须与表内完全一致）
OPTIONS = {
    "power": ["红色", "绿色", "蓝色"],
    "holding": ["是", "否"],
    "ok": ["是", "否"],
    "period": ["月", "季", "年"],
    "trend": ["上升", "下降", "震荡"],
    "history_name": ["棕榈油", "纯苯", "铁矿石", "菜籽油", "焦炭", "焦煤", "PVC", "塑料",
                     "聚丙烯", "乙二醇", "苯乙烯", "尿素", "对二甲苯", "瓶片", "花生", "白糖",
                     "豆粕", "玉米", "玉米淀粉", "菜籽粕", "玻璃", "螺纹钢", "甲醇", "纯碱",
                     "PTA", "短纤", "氧化铝", "工业硅"],
}

# 动力系统三色（绘图用）
POWER_COLORS = {"红色": "#e74c3c", "绿色": "#27ae60", "蓝色": "#2980b9"}

# 主力连续代码 -> 新浪行情节点（用于取当前主力合约名与实时报价）
# 来源：ak.futures_symbol_mark()；仅覆盖本系统监控的品种
FUTURES_MARK = {
    "C0": "hym_qh", "CS0": "ymdf_qh", "RM0": "czp_qh", "FG0": "bl_qh",
    "RB0": "lwg_qh", "MA0": "zc_qh", "SA0": "cj_qh", "TA0": "pta_qh",
    "PF0": "pf_qh", "AO0": "ao_qh", "M0": "dp_qh", "SR0": "bst_qh",
    "PK0": "pk_qh", "PR0": "pr_qh", "PX0": "px_qh", "UR0": "ns_qh",
    "OI0": "czy_qh", "EB0": "byx_qh", "EG0": "yec_qh", "PP0": "jbx_qh",
    "L0": "lldpe_qh", "V0": "pvc_qh", "BZ0": "bz_qh", "P0": "zly_qh",
    "I0": "tks_qh", "JM0": "jm_qh", "J0": "jt_qh", "SI0": "si_qh",
}
