# Polymarket 自适应跟单机器人

基于钱包跟踪和自适应策略的 Polymarket 自动化交易机器人。支持智能钱包发现、跟单开仓/平仓、风险管理、熔断保护。

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                        TradingBot                           │
├──────────────┬──────────────┬──────────────┬────────────────┤
│  钱包发现     │  交易执行     │  风险管理     │  监控与安全    │
│              │              │              │                │
│ WalletScanner│ CopyExecutor │ RiskManager  │ CircuitBreaker │
│ QualityScorer│ PolyClient   │ OrderValidator│ EmergencyStop  │
│ MMDetector   │ Strategies   │ SlippageGuard│ TelegramAlert  │
│ RedFlagDetect│ WalletMonitor│ BalanceMonitor│ HeartbeatMon   │
└──────────────┴──────────────┴──────────────┴────────────────┘
```

## 功能特性

### 跟单系统
- **智能钱包发现** — 4 个数据源自动发现: 排行榜 / CLOB 市场活跃 / Polygonscan / 手动种子
- **5 维质量评分** — 胜率 (20%) + 盈亏比 (25%) + 稳定性 (20%) + 风控 (15%) + 专业度 (20%)
- **做市商检测** — 6 种模式识别 (高频/短持仓/均衡胜率/低利润/双向/连续报价)
- **开仓跟单** — 根据钱包评分智能计算跟单金额
- **平仓跟单** — 目标平仓时实时跟随 + 定期同步检测遗漏
- **幂等执行** — SQLite 持久化，重复信号不会重复开仓

### 策略
- **自适应策略管理** — 根据市场状态动态选择策略
- **Endgame Sweeper** — 高概率收割 (95%+ 概率, ≤7 天结算)

### 风险管理
- **仓位控制** — 单笔上限、并发上限、总敞口上限
- **止损止盈** — 自动止损 15%、止盈 25%
- **熔断机制** — 日亏损超限自动停止交易
- **滑点保护** — 订单执行价格偏差检测

### 安全特性
- **紧急停止** — 创建 EMERGENCY_STOP 文件立即平仓停机
- **强制清仓** — 一键关闭所有持仓
- **L2 认证** — 通过 py-clob-client 自动派生 API 密钥 (EIP-712 签名)
- **Gnosis Safe** — 支持浏览器钱包 Proxy 合约 (signature_type=2)
- **连接重试** — Polymarket API 连接失败自动重试 3 次 (指数退避 5s/10s/20s)

### 监控
- **Telegram 通知** — 交易提醒 (含源钱包 Polygonscan 链接)、异常告警、定期报告
- **WebSocket 实时推送** — 市场数据和交易事件 (可选, 留空自动回退轮询)
- **结构化日志** — JSON 格式生产级日志

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

核心依赖: `py-clob-client>=0.11.0` / `web3>=6.0.0` / `eth_account>=0.10.0` / `aiohttp>=3.9.0`

### 2. 配置 .env

```bash
cp .env.example .env
```

**必填项:**

```bash
# Polygon RPC 节点
POLYGON_RPC_URL=https://polygon-rpc.com

# 钱包私钥 (用于 L2 API 认证)
PRIVATE_KEY=your_private_key

# MetaMask EOA 地址 (用于签名)
WALLET_ADDRESS=0xYourWalletAddress

# Proxy 地址 (资金存放位置)
# 获取: polymarket.com → 头像 → Settings → Wallet Address
FUNDER_ADDRESS=0xYourProxyAddress

# Polygonscan API Key (钱包扫描用)
POLYGONSCAN_API_KEY=your_api_key
```

**可选 — 手动指定跟单目标:**

```bash
# 已知优质钱包 (逗号分隔, 与自动发现并行)
SEED_WALLETS=0xabc...,0xdef...

# 或直接指定目标钱包 (跳过评分)
TARGET_WALLETS=0x123...,0x456...
```

### 3. 运行

```bash
# 模拟模式 (首次运行推荐)
python main.py

# 实盘模式
# 修改 .env: DRY_RUN=false
python main.py
```

## 配置参考

### 跟单配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `COPY_TRADING_ENABLED` | `true` | 启用跟单 |
| `COPY_AUTO_DISCOVER` | `true` | 自动发现高质量钱包 |
| `COPY_TRADING_MODE` | `smart` | 模式: smart / fixed / proportional / full |
| `COPY_FIXED_AMOUNT` | `10` | 固定/基准金额 (USD) |
| `COPY_PROPORTIONAL_RATIO` | `0.1` | 比例跟单 (10%) |
| `COPY_MAX_AMOUNT` | `50` | 单笔最大 (USD) |
| `COPY_MIN_AMOUNT` | `5` | 单笔最小 (USD) |
| `COPY_DELAY_SECONDS` | `1.0` | 跟单延迟 (秒) |
| `COPY_MAX_WALLETS` | `10` | 最大跟单钱包数 |
| `COPY_FOLLOW_CLOSE` | `true` | 跟平仓 (关键!) |
| `COPY_CLOSE_ON_TARGET_CLOSE` | `true` | 目标平仓时自动跟随平仓 |
| `COPY_POSITION_SYNC_INTERVAL` | `300` | 持仓同步间隔 (秒) |
| `SEED_WALLETS` | | 种子钱包 (逗号分隔) |
| `TARGET_WALLETS` | | 目标钱包 (逗号分隔) |

### 钱包质量门槛

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `WALLET_MIN_TRADES` | `20` | 最小交易次数 |
| `WALLET_MIN_WIN_RATE` | `0.55` | 最小胜率 (55%) |
| `WALLET_MIN_PROFIT_FACTOR` | `1.2` | 最小盈亏比 |
| `WALLET_MIN_QUALITY_SCORE` | `70` | 最小评分 (0-100) |

### 钱包质量等级

| 等级 | 分数 | 描述 | 跟单倍数 |
|------|------|------|----------|
| Elite | 9.0-10.0 | 顶级交易者 | 2.0x |
| Expert | 7.0-8.9 | 专家级 | 1.5x |
| Good | 5.0-6.9 | 良好 | 1.0x |
| Poor | <5.0 | 不跟单 | 排除 |

### 风险管理

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `MAX_DAILY_LOSS` | `100` | 日最大亏损 (USD) |
| `MAX_POSITION_SIZE` | `50` | 单笔上限 (USD) |
| `MAX_POSITION_PCT` | `0.03` | 仓位占比上限 (3%) |
| `MAX_CONCURRENT_POSITIONS` | `5` | 最大并发仓位 |
| `MAX_TOTAL_EXPOSURE` | `200` | 总敞口上限 (USD) |

### Endgame Sweeper

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `ENDGAME_MIN_PROB` | `0.95` | 最小入场概率 |
| `ENDGAME_MAX_DAYS` | `7` | 最大距结算天数 |
| `ENDGAME_MIN_LIQUIDITY` | `10000` | 最小日流动性 (USD) |

### WebSocket 配置

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `WEBSOCKET_ENABLED` | `true` | 启用 WebSocket 实时推送 |
| `POLYMARKET_WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Polymarket WebSocket |
| `WALLET_MONITOR_WS_URL` | _(空)_ | 钱包监控 WSS (Polygon RPC, 如 Alchemy WSS; 留空回退轮询) |
| `WS_RECONNECT_INTERVAL` | `5` | 重连间隔 (秒) |
| `WS_MAX_RECONNECT` | `10` | 最大重连次数 |

### Telegram 通知

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `TELEGRAM_BOT_TOKEN` | _(空)_ | Bot Token (通过 @BotFather 创建) |
| `TELEGRAM_CHAT_ID` | _(空)_ | Chat ID (通过 @userinfobot 获取) |

> 配置 Telegram 后，交易通知会包含源钱包的完整地址和 Polygonscan 链接，方便手动验证。

## 项目结构

```
├── main.py                    # 主入口, TradingBot 编排器
├── config/
│   └── settings.py            # 配置管理 (环境变量 → 数据类)
├── core/
│   ├── copy_executor.py       # 跟单执行 (开仓/平仓/持仓同步)
│   ├── risk_manager.py        # 风险管理 (仓位/止损止盈/敞口)
│   ├── circuit_breaker.py     # 熔断机制
│   ├── wallet_scanner.py      # 钱包发现 (4源: 排行榜/CLOB/Polygonscan/种子)
│   ├── wallet_monitor.py      # 钱包交易监控 (Polling/WebSocket)
│   ├── wallet_quality_scorer.py # 5维质量评分
│   ├── market_maker_detector.py # 做市商识别
│   ├── red_flag_detector.py   # 警告信号检测
│   └── websocket_manager.py   # WebSocket 连接管理
├── services/
│   ├── polymarket_client.py   # Polymarket API 客户端 (CLOB/Data/Gamma)
│   └── telegram_service.py    # Telegram 通知
├── strategies/
│   ├── adaptive.py            # 自适应策略管理
│   ├── base.py                # 策略基类
│   └── endgame.py             # Endgame Sweeper 策略
├── utils/
│   ├── emergency_stop.py      # 紧急停止机制
│   ├── slippage_protection.py # 滑点保护
│   ├── trade_persistence.py   # 交易持久化 (SQLite)
│   ├── validation.py          # 订单验证
│   ├── monitoring.py          # 监控服务
│   ├── structured_logging.py  # 结构化日志
│   └── ...                    # retry, gas_nonce, financial 等
├── data/                      # 运行时数据 (trades.db)
├── logs/                      # 日志文件
└── tests/                     # 测试用例
```

## 紧急停止

创建 `EMERGENCY_STOP` 文件可触发紧急停止并强制平仓：

```bash
# 触发停止
echo "紧急停止原因" > EMERGENCY_STOP

# 恢复运行
rm EMERGENCY_STOP
```

## API 依赖

| API | 用途 | 认证 |
|-----|------|------|
| CLOB (`clob.polymarket.com`) | 交易执行、订单簿 | L2 HMAC (自动派生) |
| Data (`data-api.polymarket.com`) | 用户交易/持仓/排行榜 | 无 |
| Gamma (`gamma-api.polymarket.com`) | 市场元数据 | 无 |
| Polygonscan (`api.polygonscan.com`) | 钱包链上活动 | API Key |

## 风险提示

**核心风险:**
- 可能损失全部资金，过去收益不代表未来表现
- 市场流动性不足可能导致无法及时平仓
- 网络故障或 API 中断可能导致交易失败
- 被跟单钱包策略变化或失误
- 智能合约漏洞或软件 bug

**建议:**
- 首次实盘使用 < $100 测试
- 单笔仓位不超过总资金 3%
- 持续监控运行状态
- **不确定风险时不要使用实盘模式**

---

MIT License
