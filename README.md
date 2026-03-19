# 富途量化交易机器人V1.0.0 - 完整操作手册

本文档覆盖**本地开发调试**、**回测调优**、**资产管理**以及**生产环境部署**的全部流程。当前系统专为**A股及港股**优化，全面支持盘中60分钟级别分析、T+1交易规则限制及整手交易分配。

**版本信息**：
- 最新更新：2026-03-19
- 核心特性：双轨制策略 + 实时风控 + 自动化调度
- 运行模式：模拟交易（可切换实盘）

**项目地址**：[LLM-Finance](https://github.com/your-repo/LLM-Finance)

---

## 📋 目录

- [第一部分：核心架构与策略说明](#第一部分核心架构与策略说明)
- [第二部分：本地环境启动与开发调试指南](#第二部分本地环境启动与开发调试指南)
- [第三部分：资产管理与监控标的](#第三部分资产管理与监控标的)
- [第四部分：回测调试](#第四部分回测调试)
- [第五部分：日常审计命令](#第五部分日常审计命令)
- [第六部分：生产服务器部署与实盘对接](#第六部分生产服务器部署与实盘对接)
- [第七部分：常见问题与故障排查](#第七部分常见问题与故障排查)
- [附录：技术架构详解](#附录技术架构详解)

---

## 第一部分：核心架构与策略说明

### 1. 核心架构
| 模块 | 技术 | 用途 |
|------|------|------|
| 数据采集 | Futu OpenAPI | 采集 A/港股 60分钟级别 K线 (K_60M)，强制前复权(QFQ)避免技术指标失真。 |
| 指标计算 | pandas-ta | MA、RSI、布林带、ATR、ADX、OBV。支持多时间框架分析。 |
| 资产组合 | PortfolioManager | 整手过滤(Board Lot)、防超买并发控制、管理 T+1 (A股) / T+0 (港股)。 |
| 数据库 | MySQL + SQLAlchemy | K线存档、信号审计、持仓快照(含可用数量)、并发读写钱包。 |
| 并发调度 | schedule + ThreadPool | 定时任务调度，对接每日早盘锁定结算与尾盘集合竞价。 |
| 实时监控 | Futu WebSocket | 高频实时报价订阅，盘中止损止盈风险控制。 |
| 回测引擎 | Backtrader | 提供策略的回测与参数拟合平台。 |

### 2. 量化策略体系 (双轨制)

系统当前内置了两套独立的交易逻辑，会根据监控标的的 `is_etf` 属性自动路由分发。在多标的同时触发买入信号时，系统采用 **标的打分机制 (RSI 偏离度优先)** 进行资金分配。

#### 2.1 标的打分机制与组合管理
为了解决资金有限时多个标的同时出现买点的问题，系统引入了打分机制：
- **得分计算**：只要触发买入信号，系统会基于当前的 RSI 超卖程度计算得分（`score = 100 - RSI_14`）。RSI 越低（跌幅越大、偏离度越高），得分越高。如果在 ETF 策略中直接跌破了布林带下轨，还会额外加 10 分。
- **资金分配**：在订单评估环节，系统会将所有买入信号按得分**降序排序**，优先将可用资金买入"超卖最严重、得分最高"的标的，直到现金耗尽。

#### 2.2 个股策略 (60分钟级别多因子共振 - 右侧趋势)
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

### 5. 双引擎驱动架构 (定时任务 + 实时高频监控)

目前系统升级为"双引擎"架构，确保盘中风险可控，同时保持定时评估的稳定性。

#### 🕐 引擎一：主调度器 (每日守护进程 - 已集成实时监控)

**重要更新**：实时监控功能已集成到调度器中，无需单独启动！

当你调试无误准备挂机时，启动调度器即可：
```bash
python run_scheduler.py
```

**系统将按照以下严格的交易时间表全自动轮询：**
- **09:00** - T+1 限售股转解禁（A股可卖份额重置）
- **11:30** - A股、港股 上午收盘数据切片扫描
- **14:00** - 港股 下午盘中评估
- **14:50** - A股 尾盘准点执行扫描 (规避跳空，切入竞价)
- **15:50** - 港股 尾盘准点执行扫描

**调度器特性**：
- ✅ 集成实时风控监控（无需额外进程）
- ✅ 自动处理A股T+1交易规则
- ✅ 支持多市场并行调度
- ✅ 日志持久化到 `bot.log`
- ✅ 优雅的异常处理和恢复机制

#### 🔄 引擎二：实时监控线程 (已集成到调度器)

**实时监控功能已集成到 `run_scheduler.py` 中**，作为后台守护线程运行。

**核心功能：**
- **实时看板订阅**：利用 Futu API 的 `OpenQuoteContext` 建立长连接，订阅数据库中所有 `is_active=1` 的监控标的实时报价，并每秒将最新价格（`last_price`）更新到数据库的 `asset_monitor` 表中。
- **高频止损/止盈拦截**：
  - 每秒接收富途推送，立即与数据库中的持仓均价 (`avg_cost`) 比对。
  - 触发硬性止损（-8%）或硬性止盈（+15%）时，立即记录日志。
- **动态刷新**：内置轮询机制（每 60 秒刷新一次），自动新增监控标的。

> ℹ️ **说明**：
> - 现在只需要运行 `python run_scheduler.py` 即可获得完整的调度+监控功能
> - 如需单独测试实时监控，仍可运行 `python realtime_monitor.py`
> - 目前的执行依然是模拟打印日志，不会真实扣费

#### 📊 运行模式对比

| 运行模式 | 命令 | 适用场景 | 功能 |
|---------|------|---------|------|
| **单次分析** | `python main.py` | 开发调试、测试 | 执行一次策略分析 |
| **完整调度** | `python run_scheduler.py` | 生产部署 | 定时调度 + 实时监控 |
| **独立监控** | `python realtime_monitor.py` | 测试监控功能 | 仅实时风控监控 |

> 💡 **推荐**：生产环境使用 `python run_scheduler.py`，一个命令搞定所有功能！

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

## 第四部分：回测调试

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

### 2. 系统管理 (systemd)
推荐使用 systemd 管理服务，创建 `/etc/systemd/system/quant-bot.service`：
```ini
[Unit]
Description=Quant Trading Bot
After=network.target mysql.service

[Service]
Type=simple
User=www
WorkingDirectory=/www/wwwroot/llm-transaction-futu
ExecStart=/usr/bin/python3 run_scheduler.py
Restart=always
RestartSec=10
Environment="PATH=/www/wwwroot/llm-transaction-futu/venv/bin"

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable quant-bot
sudo systemctl start quant-bot
sudo systemctl status quant-bot
```

### 3. 实盘对接警告
> ⚠️ **高风险操作：实盘下有损耗、滑点与佣金，请务必在 Paper Trading (纸面测试) 且胜率达标后开启。**

目前系统默认在沙盒模式运行（`simulate=True`）。如果要接入自己的富途账户提款：
1. 打开 `main.py`，找到 `OrderExecutor`，将 `simulate=True` 改为 `simulate=False`。
2. 打开 `engine/executor.py`，**解除发单接口注解** `trade_ctx.place_order`，开启真实接口调用。
3. **严重警告**：请务必同时实装第二阶段的"Stop-loss order (条件单)"，确保每次发送买入市价单时，附带挂上计算好的保护性止损止盈单！

---

## 第七部分：常见问题与故障排查

### 1. 环境变量问题
**问题**：`KeyError: 'HOME'`
```bash
# 解决方案：已在代码中自动修复，确保使用最新版本的 run_scheduler.py
# 如果仍然出现，手动设置环境变量：
export HOME=/www/wwwroot/llm-transaction-futu
python run_scheduler.py
```

### 2. 富途API连接失败
**问题**：`Connect fail: ECONNREFUSED`
```bash
# 检查富途OpenD是否运行
netstat -tuln | grep 11111

# 检查日志
tail -f bot.log | grep -i "futu\|connect"
```

### 3. 数据库连接问题
**问题**：`Can't connect to MySQL server`
```bash
# 检查数据库是否运行
sudo systemctl status mysql

# 检查.env配置
cat .env | grep DB_

# 测试数据库连接
mysql -h $DB_HOST -u $DB_USER -p$DB_PASSWORD $DB_NAME
```

### 4. 信号记录为空
**问题**：signal_records表没有BUY/SELL信号
```bash
# 这是正常现象！说明当前市场条件不符合交易要求
# 查看最近的信号记录
python -c "
from database.db import SessionLocal
from database.models import SignalRecord
db = SessionLocal()
signals = db.query(SignalRecord).order_by(SignalRecord.created_at.desc()).limit(10).all()
for s in signals:
    print(f'{s.created_at} | {s.code} | {s.action.name} | {s.reason}')
db.close()
"
```

### 5. 内存不足
**问题**：`MemoryError` 或系统变慢
```bash
# 查看内存使用
free -h

# 清理旧的日志文件
find . -name "*.log" -mtime +7 -delete

# 清理旧的图表文件
find output/charts/ -mtime +7 -delete
```

### 6. 权限问题
**问题**：`Permission denied`
```bash
# 修改文件权限
chmod +x run_scheduler.py
chmod +x main.py

# 修改目录权限
chown -R www:www /www/wwwroot/llm-transaction-futu
```

---

## 附录：技术架构详解

### A. 项目目录结构
```
LLM-Finance/
├── database/                    # 数据库模块
│   ├── db.py                   # 数据库连接配置
│   └── models.py               # SQLAlchemy数据模型
│
├── data/                       # 数据采集模块
│   └── futu_client.py          # 富途API客户端
│
├── engine/                     # 交易执行引擎
│   ├── executor.py             # 订单执行器
│   └── portfolio.py            # 组合管理器
│
├── strategy/                   # 交易策略模块
│   ├── indicators.py           # 技术指标计算
│   └── logic.py                # 策略逻辑（个股+ETF）
│
├── scripts/                    # 工具脚本
│   ├── init_db.py              # 数据库初始化
│   ├── run_backtest.py         # 个股回测
│   ├── run_etf_backtest.py     # ETF回测
│   ├── visualizer.py           # K线图表生成
│   └── backtest/               # 回测策略
│       ├── backtrader_strategy.py
│       └── etf_grid_strategy.py
│
├── output/                     # 输出目录
│   └── charts/                 # 生成的图表
│
├── main.py                     # 主程序入口
├── run_scheduler.py            # 调度器入口（集成实时监控）
├── realtime_monitor.py         # 实时监控入口（独立）
├── requirements.txt            # 依赖配置
├── .env                        # 环境变量
├── .gitignore                  # Git忽略文件
└── README.md                   # 项目文档
```

### B. 数据库表结构
```sql
-- 核心表
kline_data              # K线数据存储
signal_records          # 交易信号记录
trade_records           # 实际交易记录
user_wallets            # 用户钱包
holdings                # 持仓信息
asset_monitor           # 监控资产列表
```

### C. 关键配置参数
```python
# 位置：main.py
DATA_WINDOW_DAYS = 550        # 数据窗口天数
POSITION_SIZE_FRAC = 0.25     # 单次买入仓位比例

# 位置：strategy/logic.py
# 个股策略参数
RSI_OVERSOLD = 35             # RSI超卖阈值
RSI_OVERBOUGHT = 70           # RSI超买阈值
BOLL_TOUCH_PCT = 0.01         # 布林带触及阈值

# ETF策略参数
RSI_EXTREME_OVERSOLD = 25     # ETF极度超卖阈值
GRID_DROP_PCT = 0.03          # 网格加仓间距
TAKE_PROFIT_PCT = 0.04        # 整体止盈目标
MAX_TRANCHES = 4              # 最大建仓批次

# 实时监控参数
HARD_STOP_LOSS = -0.08        # 硬止损阈值
HARD_TAKE_PROFIT = 0.15       # 硬止盈阈值
```

### D. API接口说明
```python
# 富途OpenAPI主要接口
from futu import *

# 行情订阅
quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
quote_ctx.subscribe(codes, [SubType.QUOTE], subscribe_push=True)

# K线数据获取
ret, data = quote_ctx.get_history_kline(code, start=start, end=end, ktype=KLType.K_60M)

# 实时报价处理
class QuoteHandler(StockQuoteHandlerBase):
    def on_recv_rsp(self, rsp_pb):
        # 处理实时报价推送
        pass
```

---

## 📞 技术支持

如有问题或建议，请通过以下方式联系：
- GitHub Issues: [项目Issues页面](https://github.com/your-repo/LLM-Finance/issues)
- 邮件: 13113288579@163.com

---

## 📄 许可证

本项目采用 MIT 许可证 - 详见 LICENSE 文件

---

## 🙏 致谢

感谢以下开源项目：
- [Futu OpenAPI](https://openapi.futunn.com/) - 富途开放平台
- [pandas-ta](https://github.com/twopirllc/pandas-ta) - 技术分析库
- [Backtrader](https://www.backtrader.com/) - 回测框架
- [SQLAlchemy](https://www.sqlalchemy.org/) - ORM框架

---

**最后更新**: 2026-03-19 | **维护者**: NaSaSKY | **版本**: 1.0.0
