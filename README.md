# 三重滤网趋势交易系统 · 自动化

用飞书多维表格做前端、Python 做计算、GitHub Actions 做定时调度，实现四件事：

| 模块 | 内容 | 触发时间（北京时间） |
| --- | --- | --- |
| 日内数据更新 | 抓主力合约最新价 → 合成滚动周K → 五维评分 → 写监控表；每 15 分钟把快照写入两张历史表 + CSV | 交易日 09:00–15:00、21:00–23:00，每 5 分钟刷新行情 |
| 交易与持仓记录 | 读交易日志 → 算「盈亏数额」→ 更新持仓表 → 回写「是否持仓」 | 每日 18:10（也可手动） |
| 每日账户快照 | 逐笔对冲 / 逐日盯市 双路径算权益并校验 | 每日 18:10 |
| 绩效指标 | 每月末算当月；季末月加算当季；12 月再加算当年 | 每月最后一天 18:20 |
| 复盘图 | 读取仓库里的历史 CSV，生成「价格 + 趋势评分」双轴 HTML 图 | 手动运行 `plot.py` |

---

## 1. 目录结构

```
code/
├── config.py          # 凭证、常量、表 ID、字段名、品种映射、历史表拆分
├── feishu.py          # 飞书 Bitable 封装：token、分页、批量增改删、upsert
├── akdata.py          # 行情：实时快照 / 主力合约名 / 日线（结算价、持仓量）
├── indicators.py      # EMA/MACD/DMI/强力指标/HPI + 日线合成滚动周K
├── scoring.py         # 五维趋势评分 + 动力系统三色
├── position.py        # 交易日志 → 盈亏数额 / 持仓管理 / 是否持仓
├── daily_snapshot.py  # 双路径校验 + 结算价补查
├── performance.py     # 月/季/年绩效（按月末规则产出）
├── history.py         # 历史双表路由、CSV 归档、每周清理、补档
├── plot.py            # Plotly 双轴复盘图
├── main.py            # 入口：realtime / daily / monthly / position / export-history / diagnose
├── data/              # history.csv / history2.csv（提交进仓库）
├── tests/test_offline.py
├── requirements.txt
└── .github/workflows/ # realtime.yml / snapshot.yml / monthly.yml
```

---

## 2. 飞书应用权限

应用必须在**文档级**有编辑权限：

1. 多维表格 → 右上角「···」→「权限设置 / 高级权限」
2. 把应用 `cli_aa5b6fc5ef385d2a` 加为「可编辑」协作者
3. 若开启高级权限，需对**全部 7 张表**（含历史数据复盘2）逐表授权
4. 开发者后台确认 `bitable:app` 读写权限已发布

自检：`python main.py --mode diagnose`，7 张表都 `[OK]` 即可。

| 返回码 | 含义 |
| --- | --- |
| `1254302` | 该表没有给应用授权 |
| `91403` | 应用对整表只读 |
| `1254045` | 字段名和代码不一致 |

---

## 3. 本机运行

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

常用命令：

```bash
python main.py --mode diagnose                 # 自检
python main.py --mode realtime                 # 盯盘一次（行情 + 每 15 分钟历史）
python main.py --mode realtime --force-history # 强制写一次历史（调试用）
python main.py --mode position                 # 交易日志盈亏 + 持仓归集
python main.py --mode daily                    # 持仓 + 每日快照
python main.py --mode monthly --force          # 强制算绩效
python main.py --mode export-history           # 把飞书历史表补进 CSV
```

参数：`--dry-run` 只算不写、`--limit N` 只跑前 N 个品种、`--date YYYY-MM-DD` 补跑、`--force` 忽略月末判断。

---

## 4. 复盘图

```bash
python plot.py --list                                   # 看有哪些品种、时间范围
python plot.py --name 螺纹钢                             # 输出 charts/螺纹钢.html
python plot.py --name 螺纹钢 --start 2026-10-01 --end 2026-10-31
python plot.py --all                                    # 每个品种一张
```

- 左轴：当前价格，**按动力系统三色（红/绿/蓝）分段着色**
- 右轴：趋势评分，**紫色加粗虚线**（固定 -110~110）
- 横轴只显示有行情的交易时段：午休、收盘后、隔夜不占位置
- 左上角 1天/1周/1月/3月/全部 按钮，范围与周K聚合一致（夜盘算下一交易日，所以"1天"= 前一日夜盘 + 当日日盘）
- 图例在右上角，不与按钮重叠；鼠标拖拽可自由缩放
- 数据来自 `data/history.csv`、`data/history2.csv`（本地或仓库里的完整历史）

---

## 5. 凭证配置

凭证**不写死在代码里**：

1. 本地：把 `.env.example` 复制为 `.env`，填入 `FEISHU_APP_SECRET`（`.env` 已被 git 忽略）
2. GitHub Actions：在仓库 Secrets 里配置同名变量
3. 也可以直接用环境变量覆盖

```bash
# .env 示例
FEISHU_APP_ID=cli_aa5b6fc5ef385d2a
FEISHU_APP_SECRET=xxx
FEISHU_APP_TOKEN=RDX2bwQhfaYeRhsZAwUcW6VYned
```

## 6. GitHub Actions 部署

1. 新建仓库推代码（`.venv/`、`logs/`、`charts/`、`.env` 已忽略，`data/` 会入库）
2. **Settings → Secrets and variables → Actions** 添加：
   - `FEISHU_APP_ID`
   - `FEISHU_APP_SECRET`
   - `FEISHU_APP_TOKEN`
3. 工作流：
   - `realtime.yml`：`*/5 1-3,5-7,13-15 * * 1-5`（UTC），脚本内部按 15 分钟写历史，并把 `data/*.csv` 提交回仓库
   - `snapshot.yml`：`10 10 * * 1-5`（北京 18:10）
   - `monthly.yml`：`20 10 28-31 * *`（北京 18:20，脚本自己判断是否月末）
4. `realtime.yml` 需要 `permissions: contents: write`（已配置），否则 CSV 推不回去。

### 补算历史快照

补录了某天的交易后，手动运行「收盘快照」并填写 `date`（如 `2026-10-23`）即可重算那天的快照，同时把持仓表的结算价刷新：
`python main.py --mode daily --date 2026-10-23`

非交易日（周末/节假日）直接运行会打印"没有日线数据（非交易日），跳过快照"，这是正常的。

## 7. 飞书自动化：交易日志 → GitHub Actions

交易日志表里的「状态」字段：**已完成** = 待处理，**已更新** = 已处理。

1. 飞书多维表格 → 自动化 → 新建
2. 触发条件：**新增/修改的记录满足条件时**
   - 数据表：交易日志
   - 条件：**状态 等于 已完成**
3. 操作：**发送 HTTP 请求**
   ```
   URL:  https://api.github.com/repos/<owner>/<repo>/dispatches
   方法: POST
   请求头:
     Authorization: Bearer <fine-grained token，需 Contents: 读写>
     Content-Type: application/json
   请求体: {"event_type":"journal-updated"}
   响应体: 选「文本 / 不解析」——GitHub 成功时返回 204 空响应，选 JSON 会解析失败
   ```
4. 保存启用后，把一条日志状态改成「已完成」，Actions 会出现「交易日志更新」运行，约 40 秒后状态自动变「已更新」。

> 两个坑：
> - `workflow_dispatch` 接口需要 **Actions** 权限，`repository_dispatch` 需要 **Contents** 权限，别混用。
> - 飞书只根据"HTTP 请求是否完成"标记成功，**403/404 也会显示成功**，一定要去 GitHub Actions 确认有没有真的产生运行。
> - 响应体是 204 空内容，响应配置若选 JSON 会报「出参返回格式不正确」，改选文本即可。

### 已知限制

- GitHub 定时任务会延迟 5–15 分钟，5 分钟粒度只能"尽量"
- 私有仓库每月 2000 分钟额度，5 分钟一次约 2900 分钟/月，建议 public 或改 10 分钟
- 仓库连续 60 天无提交会自动停用 schedule

---

## 6. 关键实现

### 滚动周K（与同花顺对齐）

周一为周起点；开=周一开盘（含上周五夜盘）、高/低=本周极值、收=最新收盘、量=累加、OI=最后一根。

**夜盘归属**：国内期货的夜盘属于**下一个交易日**（周五 21:00 的夜盘算下周一）。同花顺的周K就是从周五夜盘开始画的，而日线数据要等下一个交易日收盘才有。所以盯盘时会把实时快照按 `tradedate` 补成一根"当日"日线，再合成周K——这样周五一收盘的夜盘会立刻进入下一周的K线，HPI/EMA 等指标与行情软件一致，不会停留在上一周。

无夜盘品种按自然周聚合；`build_weekly` 的 `is_partial` 标记本周未走完的K线。

### 五维评分

| 维度 | 判定 | 分值 |
| --- | --- | --- |
| EMA | EMA5 > EMA10 | ±10 |
| 动力系统 | MACD柱与12期EMA斜率同向：绿 +20 / 蓝 0 / 红 −20 | ±20 |
| 强力指标 | 13期EMA慢线 > 0 | ±20 |
| DMI | ADX<20 记0；ADX>20 且上升 ±30；ADX>20 且下降 ±15 | ±30 |
| HPI | >0 且升 +20；>0 且降 +10；<0 且升 −20；<0 且降 −10 | ±20 |

绝对分 >100 会打错误日志（不截断、不影响写入）。

### 交易日志「盈亏数额」

脚本计算并写入，开仓行保持为空：

- 多：`(平仓价 − 开仓均价) × 合约乘数 × 平仓数量 − 开仓手续费 − 平仓手续费`
- 空：`(开仓均价 − 平仓价) × 合约乘数 × 平仓数量 − 开仓手续费 − 平仓手续费`

开仓均价 = **持仓池的加权平均成本**（与持仓管理表的开仓均价完全一致）；开仓手续费按平仓数量占池中数量的比例分摊。

> 早期版本按「上一条平仓之后的开仓行」算均价，当一次平仓的数量大于这段时间新开的数量时（用到更早批次的老仓），会把盈亏算高。现已改为持仓池加权平均。

### 双路径校验

- 逐笔：`初始资金 + 已实现盈亏（毛） + 浮动盈亏 − 全部手续费`
- 盯市：`前一日权益 + 当日盯市盈亏 − 当日手续费`

当日盯市盈亏拆成四块：**昨仓留存 / 昨仓平掉**（基准=昨结算）、**今开留存 / 今开平掉**（基准=今开仓均价）。
这样"当天先加仓再平掉、平仓量大于昨仓"时不会重复计算。差额 ≤ 0.1 元记「是」。
首个快照没有昨结算，盯市路径直接以逐笔为基准，「当日盈亏」记 0。

### 时间戳

飞书日期字段存的是 UTC 毫秒。所有"当前时间"统一用 `feishu.now_ms()`（真实 epoch），
**不要**用 `pd.Timestamp.now().timestamp()`——pandas 把 naive 时间当 UTC，会多 8 小时。

### 历史双表与每周清理

- 品种按 `config.HISTORY_SPLIT` 拆到「历史数据复盘1 / 2」
- 每 15 分钟写一次（`HISTORY_INTERVAL_MIN`），同时追加 CSV
- 每周第一个到点的任务会删掉两张表里「本周一之前」的记录，飞书只留当周
- 完整历史在仓库的 `data/*.csv`

### 绩效周期

- 每月末：算当月
- 3/6/9/12 月末：加算当季
- 12 月末：加算当年

按「周期标识」upsert，同周期重复运行只更新不重复插行。

---

## 8. 本地操作手册

### 环境

| 项目 | 位置 |
|---|---|
| 项目目录 | `D:\futuretrading\code` |
| Python 环境 | `D:\futuretrading\code\.venv`（依赖已装） |
| 凭证 | `D:\futuretrading\code\.env` |
| 日志 | `D:\futuretrading\code\logs\run.log` |
| 复盘图输出 | `D:\futuretrading\code\charts\` |

两种调用方式：

```powershell
# A. 直接调用解释器（推荐，不用激活）
cd D:\futuretrading\code
.\.venv\Scripts\python.exe main.py --mode 模式名

# B. 先激活
cd D:\futuretrading\code
.\.venv\Scripts\Activate.ps1
python main.py --mode 模式名
```

### 模式一览

| 模式 | 作用 | 常用参数 |
|---|---|---|
| `diagnose` | 自检：token、7 张表权限、行情、评分 | — |
| `realtime` | 日内盯盘：更新监控表，到点写历史 + CSV | `--dry-run` `--limit N` `--force-history` |
| `position` | 交易日志 → 盈亏数额 / 持仓 / 是否持仓 | `--pending`（只处理「状态=已完成」） `--dry-run` |
| `daily` | 持仓归集 + 每日快照 + 持仓结算价 | `--date YYYY-MM-DD` `--no-sync` `--dry-run` |
| `monthly` | 月/季/年绩效（按月末规则） | `--force` `--date` |
| `export-history` | 把飞书历史表补进 CSV | — |
| `plot.py` | 双轴复盘图 | `--list` `--name` `--start` `--end` `--all` |

### 日常场景

| 场景 | 做法 |
|---|---|
| 每天盯盘 | 不用管，GitHub Actions 自动跑（09:00–11:30、13:30–15:00、21:00–23:00） |
| 想看当前评分 | `realtime --dry-run`，或直接看飞书监控表 |
| 录入一笔交易 | 飞书填完日志 → 状态改「已完成」→ 自动触发，约 40 秒 |
| 收盘后要快照 | 不用管，18:10 自动跑 |
| 补录了历史交易 | 飞书改状态触发；再 `daily --date 那天的日期` 重算 |
| 月末绩效 | 不用管，月末 18:20 自动跑；调试用 `monthly --force` |
| 看复盘图 | `git pull` → `plot.py --name 品种` → 浏览器打开 `charts\品种.html` |

> 本地跑 `realtime` 会往 `data/*.csv` 追加数据，之后 `git pull` 可能和 Actions 的提交冲突。本地只想看行情就用 `--dry-run`；跑完真写入的话，执行 `git checkout -- data` 丢弃本地 CSV 改动（飞书已写入）。

### 看复盘图

```powershell
cd D:\futuretrading\code
git pull                                              # 拉最新 CSV
.\.venv\Scripts\python.exe plot.py --list             # 看品种与时间范围
.\.venv\Scripts\python.exe plot.py --name 螺纹钢       # 生成 charts\螺纹钢.html
.\.venv\Scripts\python.exe plot.py --name 甲醇 --start 2026-10-22 --end 2026-10-31
start charts\螺纹钢.html                               # 打开
```

- 左轴价格按动力系统三色分段，右轴趋势评分
- 底部滑块选时间段，顶部 1天/1周/1月/3月/全部 快捷按钮
- 图例可点击隐藏某条线；右上角相机图标导出 PNG
- 也可以访问 GitHub Pages（见第 9 节）在手机上看最新图

### 排查

```powershell
Get-Content D:\futuretrading\code\logs\run.log -Tail 50    # 最近日志
```

| 现象 | 原因 |
|---|---|
| `1254302` | 该表没给应用授权 |
| `91403` | 应用对该表只读 |
| `1254045` | 字段名和代码不一致 |
| `没有日线数据（非交易日），跳过快照` | 周末/节假日，正常 |
| `历史未到点` | 距上次写历史不到 15 分钟，正常 |
| `校验=否` | 双路径差额 > 0.1，看日志里的"差额" |

### Git

```powershell
cd D:\futuretrading\code
git pull                 # 拉最新代码/数据
git status               # 看本地改动
git log --oneline -5
```

本地一般不需要 push：代码由开发者推，`data/*.csv` 由 Actions 自动提交。

## 9. GitHub Pages（手机看复盘图）

仓库里有一个「复盘图发布」工作流（`.github/workflows/pages.yml`），每个交易日 **15:10**（由外部定时器触发）把 28 个品种的图发布到 GitHub Pages。

首次使用需要手动开启一次：

1. 仓库 → **Settings → Pages**
2. Build and deployment → Source 选 **GitHub Actions**
3. 回到 Actions → 「复盘图发布」→ **Run workflow**

发布地址形如 `https://eleven-l-97.github.io/futuretrading/`，手机上打开就能选品种看图。

---

## 10. 指标参数在哪改

改完保存即可，下一次 `realtime` 运行就生效（GitHub Actions 会在下一次 5 分钟任务里用新参数）。

### 指标周期 —— `config.py` 顶部「指标参数」区域

| 参数 | 变量 | 当前值 | 含义 |
|---|---|---|---|
| DMI 求和周期 | `DMI_N` | 8 | DMP/DMM/TR 的 N 周期求和 |
| DMI 平均周期 | `DMI_M` | 3 | ADX = MA(DX, M) |
| MACD 快线 | `MACD_FAST` | 12 | |
| MACD 慢线 | `MACD_SLOW` | 26 | |
| MACD 信号线 | `MACD_SIGNAL` | 9 | |
| 动力系统斜率 EMA | `POWER_EMA_PERIOD` | 12 | 判断"向上/向下"的 EMA 周期 |
| 强力指标快线 | `STRONG_FAST` | 2 | |
| 强力指标慢线 | `STRONG_SLOW` | 13 | |
| HPI 每点价值 | `HPI_CF` | 100 | 白银类可改 50 |
| HPI 平滑周期 | `HPI_SPAN` | 14 | 同花顺为 EMA(K,14) |
| 周K起点 | `WEEK_START_WEEKDAY` | 0 | 0=周一 |

### 评分权重 —— `scoring.py` 顶部「评分权重」区域

| 参数 | 变量 | 当前值 | 含义 |
|---|---|---|---|
| EMA 分 | `W_EMA` | 10 | EMA5 > EMA10 +10，否则 −10 |
| 动力系统分 | `W_POWER` | 20 | 绿 +20 / 蓝 0 / 红 −20 |
| 强力指标分 | `W_STRONG` | 20 | 慢线 >0 +20，否则 −20 |
| DMI 阈值 | `W_ADX_TH` | 20 | ADX 低于该值不加分 |
| DMI 上升分 | `W_DMI_UP` | 30 | ADX 上升时 ±30 |
| DMI 下降分 | `W_DMI_DOWN` | 15 | ADX 下降时 ±15 |
| HPI 同向分 | `W_HPI_UP` | 20 | >0 且升 +20 / <0 且降 −20 |
| HPI 反向分 | `W_HPI_DOWN` | 10 | >0 且降 +10 / <0 且升 −10 |

### 核对单个品种

```powershell
.\.venv\Scripts\python.exe main.py --mode check --code V0
```

会打印该品种五个维度的判定依据、各自得分、合计，以及周线/日线 HPI 的最新值与方向，方便和同花顺等行情软件对照。

---

## 11. 外部定时器（Cloudflare Worker）

GitHub 自带的 `schedule` 延迟较严重（新仓库尤其明显，实测 20–40 分钟），所以把"到点"这件事交给 Cloudflare Worker，GitHub 只负责跑工作流。

### 原理

Worker 每分钟被 Cloudflare 唤醒一次，在内部判断北京时间：

| 时点（北京） | 触发的事件 | 对应工作流 |
|---|---|---|
| 09:00–11:30 / 13:30–15:00 / 21:00–23:00，每 5 分钟 | `realtime-tick` | 日内盯盘 |
| 每交易日 18:10 | `daily-snapshot` | 收盘快照 |
| 每交易日 15:10 | `pages-build` | 复盘图发布 |
| 每月 28–31 日 18:20 | `monthly-perf` | 绩效指标 |

触发方式：`POST https://api.github.com/repos/Eleven-L-97/futuretrading/dispatches`，body `{"event_type":"..."}`。
工作流里已删除 `schedule`，只保留 `repository_dispatch` 和手动触发，避免重复运行。

### 部署步骤（一次性）

```bash
# 1. 安装 wrangler（Node.js 已装的话直接跑）
npm i -g wrangler

# 2. 登录 Cloudflare（浏览器授权）
wrangler login

# 3. 进入项目目录
cd D:\futuretrading\code\cloudflare

# 4. 写入 GitHub token（fine-grained，只勾 Contents: Read and write）
wrangler secret put GH_TOKEN
# 粘贴 token 回车

# 5. 部署（会自动注册每分钟的 cron）
wrangler deploy
```

部署成功后会显示 Worker 地址和 cron 状态。

### 验证

```bash
cd D:\futuretrading\code\cloudflare
wrangler tail
```

等到交易时段的整 5 分钟，应看到 `realtime-tick -> 204`；18:10 看到 `daily-snapshot -> 204`。

### 说明

- Cloudflare 免费额度：每天 10 万次请求，每分钟一次一天 1440 次，完全够用
- token 存在 Cloudflare 的加密变量里，不写在代码中
- 想改时点，只改 `cloudflare/worker.mjs` 里的 `sessions` / `mins` 判断，再 `wrangler deploy`
- 临时停用：Cloudflare 控制台 → Workers → futures-cron → Triggers → 删除 cron
