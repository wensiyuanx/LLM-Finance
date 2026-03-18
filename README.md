# 富途量化交易机器人 - 完整操作手册 (Intraday 2-Hour 架构)

本文档覆盖**本地开发调试**、**回测调优**、**资产管理**以及**生产环境部署**的全部流程。当前系统专为**A股及港股**优化，全面支持盘中2H级别分析、T+1交易规则限制及整手交易分配。

---

## 第一部分：核心架构与策略说明

### 1. 核心架构
| 模块 | 技术 | 用途 |
|------|------|------|
| 数据采集 | Futu OpenAPI | 采集 A/港股 小时级别 K线 (K_60M)，强制前复权(QFQ)避免技术指标失真。 |
| 指标计算 | pandas-ta | MA、RSI、布林带、ATR、ADX、OBV。数据由 1H 平滑聚合为 2H。 |
| 资产组合 | PortfolioManager | 整手过滤(Board Lot)、防超买并发控制、管理 T+1 (A股) / T+0 (港股)。 |
| 数据库 | MySQL + SQLAlchemy | K线存档、信号审计、持仓快照(含可用数量)、并发读写钱包。 |
| 并发调度 | sched + ThreadPool | 极速协程拉取行情，对接每日早盘锁定结算与尾盘集合竞价。 |
| 回测引擎 | Backtrader | 提供策略的回测与参数拟合平台。 |

### 2. 量化策略体系 (双轨制)

系统当前内置了两套独立的交易逻辑，会根据监控标的的 `is_etf` 属性自动路由分发。在多标的同时触发买入信号时，系统采用 **标的打分机制 (RSI 偏离度优先)** 进行资金分配。

#### 2.1 标的打分机制与组合管理
为了解决资金有限时多个标的同时出现买点的问题，系统引入了打分机制：
- **得分计算**：只要触发买入信号，系统会基于当前的 RSI 超卖程度计算得分（`score = 100 - RSI_14`）。RSI 越低（跌幅越大、偏离度越高），得分越高。如果在 ETF 策略中直接跌破了布林带下轨，还会额外加 10 分。
- **资金分配**：在订单评估环节，系统会将所有买入信号按得分**降序排序**，优先将可用资金买入“超卖最严重、得分最高”的标的，直到现金耗尽。

#### 2.2 个股策略 (2H 级别多因子共振 - 右侧趋势)
适用于普通股票（如腾讯、阿里等），侧重于趋势确认和防骗线。
**买入条件**（需共振 ≥ 2 个，且无持仓）:
| 因子 | 条件 |
|------|------|
| 均线 (MA) | 5周期线上穿 20周期线 **且放量突破** |
| RSI | RSI < 35 (超卖反弹) **且当前价格 > 200周期均线 (趋势判断)** |
| 布林带 | 价格触及布林带下轨 1% 以内 **且非下跌趋势** |

**卖出条件**（需共振 ≥ 2 个，且在T+1限制下**可卖数量 > 0**）:
| 因子 | 条件 |
|------|------|
| 均线死叉 | 5周期线下穿 20周期线 |
| RSI 超买 | RSI > 70 **且非强趋势 (ADX ≤ 25)** |
| 布林带上轨 | 价格触及布林带上轨 1% 以内 **且非强势上涨** |

#### 2.3 宽基 ETF 策略 (左侧网格与均值回归)
专为 A股/港股宽基 ETF (如创业板 159915, 沪深300 510300) 打造，抛弃趋势跟踪，利用 ETF 不退市特性进行越跌越买的马丁格尔变种网格交易。
**买入条件** (无视大盘趋势，纯左侧建仓):
| 批次 | 条件 |
|------|------|
| 首仓建仓 (空仓) | RSI 极度超卖 (< 25) **或** 跌破布林带下轨 2% |
| 网格加仓 (被套) | 距离上一笔买入成本每下跌 3%，且 RSI < 35，即加仓下一批次 |
*(注：资金按 20% -> 20% -> 30% -> 30% 分4批倒金字塔建仓)*

**卖出条件** (触网或反弹即平):
| 场景 | 条件 |
|------|------|
| 整体止盈 | 整体持仓盈利达到 4%，全仓平仓落袋为安 |
| 超跌反弹止盈 | 价格触及布林带上轨且已盈利，全仓平仓 |
| 网格分批止盈 | (实盘/回测支持) 最后一批抄底资金反弹获利 3% 时，单独将该批次平仓做 T |

#### 2.4 ATR 盘中即时止赢/止损 (由券商条件单承接):
每次发单执行买入后，系统将直接挂载附带以下参数的**条件限价单(Stop Order)**：
| 规则 | 触发 |
|------|------|
| ATR 止损 | 触及均价 - 2 × ATR(14) 立即止损出局 |
| ATR 止盈 | 触及均价 + 3 × ATR(14) 让利润奔跑后卖出 |
| 极限风控 | 极震盈亏 ±15% 或 30% 保底兜底 |

---

## 第二部分：本地环境启动与开发调试指南

### 1. 初次安装 (First-time Setup)
```bash
# 1. 确保 Python 3.12 已安装
python3 --version

# 2. 创建并激活虚拟环境
python3 -m venv venv
source venv/bin/activate     # macOS / Linux
# .\venv\Scripts\activate    # Windows

# 3. 安装全部依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 用编辑器打开 .env 填写 MySQL 账号、密码和网关信息
```

### 2. 数据库初始化 (只需执行一次)
> ⚠️ **警告**：以下命令会清空并在本地重建所有表结构与模拟钱包。
```bash
# 执行数据库表结构迁移与初始资金录入 (默认A股1w, 港股2万)
python scripts/init_db.py
```

### 3. 启动 FutuOpenD (行情网关)
请确保本机已安装富途 `FutuOpenD` 并处于登录状态。程序通过该网关拉取真实盘口。
默认监听：`127.0.0.1:11111`。

### 4. 本地运行与调试 (Debug Mode)
如果你想在本地写代码、调指标，不需要干等调度器的时间，可以**随时单次触发扫描**：
```bash
# 单次运行扫描所有的股票池、计算指标、产出买卖打印日志
python main.py
```
*这会输出并发抓取进度、每只股票是否触发买卖信号、可用余额与T+1交易核算信息。*

### 6. 双引擎驱动架构 (定时任务 + 实时高频监控)

目前系统升级为“双引擎”架构，确保盘中风险可控，同时保持定时评估的稳定性。

#### 引擎一：主调度器 (每日守护进程)
当你调试无误准备挂机时，启动调度器即可：
```bash
python run_scheduler.py
```
**系统将按照以下严格的交易时间表全自动轮询：**
- **09:00** - T+1 限售股转解禁（A股可卖份额重置）
- **11:30** - A股、港股 上午收盘数据切片扫描
- **13:30** - 港股 下午盘中评估
- **14:50** - A股 尾盘准点执行扫描 (规避跳空，切入竞价)
- **15:50** - 港股 尾盘准点执行扫描

#### 引擎二：高频实时监控线程 (Websocket 订阅)
这是一个完全独立的实时监控脚本，像保安一样死死盯住您的持仓和关注列表，防范盘中闪崩。新开一个终端窗口运行：
```bash
python realtime_monitor.py
```
**核心功能：**
- **实时看板订阅**：利用 Futu API 的 `OpenQuoteContext` 建立长连接，订阅数据库中所有 `is_active=1` 的监控标的实时报价，并每秒将最新价格（`last_price`）更新到数据库的 `asset_monitor` 表中，供大屏或 SQL 实时查看。
- **高频止损/止盈拦截**：
  - 脚本在后台每秒接收富途推送，立即与数据库中的持仓均价 (`avg_cost`) 比对。
  - 一旦盘中价格发生闪崩或暴涨，触发了硬性止损（-8%）或硬性止盈（+15%），立即拦截并打印报警日志（触发卖出）。
- **动态刷新**：内置轮询机制（每 60 秒刷新一次），如果主程序 `main.py` 在尾盘新买入了股票，它会自动将其加入实时订阅列表中。

> ℹ️ **说明**：目前的执行依然是模拟打印日志，不会真实扣费。建议先同时开启这两个脚本跑几天，观察打分买入的优先级和实时止损的触发是否符合预期，以及高频监控是否能精准捕获盘中的价格异动。

---

## 第三部分：资产管理与监控标的

系统剥离出了强悍的 `PortfolioManager`，由它全权处理仓位风控。
不需要改动任何代码，直接修改数据库即可增减监控名单：

```sql
-- 增加 A股 ETF 跟踪标的 (注意 is_etf 标志位)
INSERT INTO asset_monitor (code, market_type, is_active, is_etf, user_id)
VALUES ('SZ.159915', 'A_SHARE', 1, 1, 1);

-- 增加 港股 个股 跟踪标的 (is_etf = 0)
INSERT INTO asset_monitor (code, market_type, is_active, is_etf, user_id)
VALUES ('HK.00700', 'HK_SHARE', 1, 0, 1);

-- 暂停监控某只股票
UPDATE asset_monitor SET is_active = 0 WHERE code = 'HK.00700';
```

---

## 第四部分：回测调试 (Backtesting Engine)

如果你修改了 `strategy/logic.py` 内的参数，极度建议跑一次回测以确认胜率。
系统配套了原生的 Backtrader 回测引擎核心，包含两套独立的回测脚本。

### 1. 运行个股回测 (趋势策略)
```bash
python scripts/run_backtest.py HK.00700 --days 550
```
- 修改 `scripts/backtest/backtrader_strategy.py` 中的 `params` 来调整均线、RSI 阈值或 ATR 止损倍数。

### 2. 运行 ETF 回测 (左侧网格策略)
```bash
PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python python scripts/run_etf_backtest.py SZ.159915 --days 550
```
- 修改 `scripts/backtest/etf_grid_strategy.py` 中的 `params` 调整建仓 RSI、网格加仓间距 (`grid_drop_pct`) 和止盈目标 (`take_profit_pct`)。
- 回测结束后，会在根目录生成包含交易买卖点标记、最大资金动用率等信息的 `etf_backtest_result_SZ.159915.png`。

---

## 第五部分：日常审计命令

量化系统在虚拟挂机时，你可以随时通过 SQL 查看表现：

### 1. 查看钱包与可卖持仓
```sql
-- 查看可用资金
SELECT user_id, market_type, balance, currency FROM user_wallets;

-- 查看实时持仓 (重点看 sellable_quantity 确认 A股是否解套)
SELECT code, market_type, quantity, sellable_quantity, avg_cost FROM holdings;
```

### 2. 追溯策略决策链
```sql
-- 查看所有发出过的 BUY/SELL 信号及其触发原因
SELECT code, action, reason, close_price, created_at FROM signal_records ORDER BY created_at DESC;
```

---

## 第六部分：生产服务器部署与实盘对接

### 1. Linux 服务器部署 (Ubuntu推荐)
如果你要在云端常驻运行：
```bash
# 1. 安装基础依赖
sudo apt update && sudo apt install mysql-server python3.12 python3.12-venv -y

# 2. 拉取项目与分配环境
git clone <your-repo> bot-prod
cd bot-prod
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/init_db.py

# 3. 部署 FutuOpenD
# 前往富途官网下载无界面 Linux 版 FutuOpenD，使用 supervisor 确保其 24 小时后台运行，提供 11111 接口

# 4. 后台常驻机器
nohup python run_scheduler.py > bot_production.log 2>&1 &

# 5. 随时查看日志
tail -f bot_production.log
```

### 2. 实盘对接警告
> ⚠️ **高风险操作：实盘下有损耗、滑点与佣金，请务必在 Paper Trading (纸面测试) 且胜率达标后开启。**

目前系统默认在沙盒模式运行（`simulate=True`）。如果要接入自己的富途账户提款：
1. 打开 `main.py`，找到 `OrderExecutor`，将 `simulate=True` 改为 `simulate=False`。
2. 打开 `engine/executor.py`，**解除发单接口注解** `trade_ctx.place_order`，开启真实接口调用。
3. **严重警告**：请务必同时实装第二阶段的“Stop-loss order (条件单)”，确保每次发送买入市价单时，附带挂上计算好的保护性止损止盈单！
