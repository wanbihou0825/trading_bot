# Polymarket 自适应跟单机器人

基于策略信号自动执行交易，支持风险管理和熔断机制的 Python 交易机器人。

## 功能特性

### 核心功能

- **自适应策略选择** - 根据市场状态自动选择最优策略
- **Endgame Sweeper** - 高概率收割策略 (95%+ 概率)
- **钱包跟单** - 实时跟踪目标钱包交易
- **WebSocket 支持** - 实时市场数据推送
- **风险管理** - 止损止盈、仓位控制、熔断保护
- **Telegram 通知** - 交易提醒和状态报告

### 钱包质量评估

- **质量评分** - 评估目标钱包的交易质量
- **做市商检测** - 识别并过滤做市商行为
- **警告检测** - 9种风险信号检测

### 跟单模式

| 模式 | 说明 |
|------|------|
| `smart` | 根据钱包质量智能调整金额 |
| `fixed` | 固定金额跟单 |
| `proportional` | 按比例跟单 |
| `full` | 全额跟单 |

## 快速开始

### 安装

```bash
# 克隆项目
git clone https://github.com/yourusername/polymarket-copy-bot.git
cd polymarket-copy-bot

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
nano .env
```

### 必需配置

- `PRIVATE_KEY` - 钱包私钥
- `WALLET_ADDRESS` - 钱包地址
- `POLYGON_RPC_URL` - Polygon RPC端点
- `POLYGONSCAN_API_KEY` - Polygonscan API密钥 (跟单功能)

### 运行

```bash
# 模拟模式 (推荐首次运行)
python main.py --dry-run

# 实盘模式
python main.py
```

## 策略说明

### Endgame Sweeper

专注于接近结算的高概率市场：
- 概率 >= 95%
- 距离结算 <= 7天
- 日流动性 >= $10,000
- 年化收益 >= 20%

### 自适应策略

根据市场状态自动选择：
- 接近结算 → Endgame Sweeper
- 高价差 → 做市策略
- 趋势市场 → 动量追踪

## 风险管理

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 每日最大损失 | $100 | 达到后触发熔断 |
| 单笔最大仓位 | $50 | 单笔交易上限 |
| 最大并发仓位 | 5 | 同时持仓数量 |
| 默认止损 | 15% | 价格反向移动 |
| 默认止盈 | 25% | 价格顺向移动 |

## 跟单配置

### 添加目标钱包

在 `.env` 文件中配置：

```env
# 目标钱包列表 (逗号分隔)
TARGET_WALLETS=0xWallet1,0xWallet2,0xWallet3

# 跟单模式
COPY_TRADING_MODE=smart

# 最大跟单金额
COPY_MAX_AMOUNT=50
```

### 钱包质量等级

| 等级 | 分数范围 | 最大配置比例 |
|------|----------|-------------|
| Elite | 9.0-10.0 | 15% |
| Expert | 7.0-8.9 | 10% |
| Good | 5.0-6.9 | 7% |
| Poor | <5.0 | 排除 |

## 目录结构

```
polymarket-copy-bot-new/
├── config/               # 配置管理
│   └── settings.py       # 主配置
├── core/                 # 核心功能
│   ├── exceptions.py     # 异常定义
│   ├── circuit_breaker.py # 熔断器
│   ├── risk_manager.py   # 风险管理
│   ├── wallet_quality_scorer.py  # 钱包评分
│   ├── market_maker_detector.py  # 做市商检测
│   ├── red_flag_detector.py      # 警告检测
│   ├── wallet_monitor.py         # 钱包监控
│   ├── websocket_manager.py      # WebSocket
│   └── copy_executor.py          # 跟单执行
├── strategies/           # 交易策略
│   ├── base.py           # 策略基类
│   ├── endgame.py        # Endgame策略
│   └── adaptive.py       # 自适应管理器
├── services/             # 外部服务
│   ├── polymarket_client.py  # API客户端
│   └── telegram_service.py   # 通知服务
├── utils/                # 工具函数
│   ├── logger.py         # 日志系统
│   ├── validation.py     # 验证工具
│   └── financial.py      # 财务计算
├── main.py               # 主入口
├── requirements.txt      # 依赖列表
└── README.md             # 说明文档
```

## WebSocket 支持

支持实时市场数据订阅：

- `subscribe_market()` - 市场更新
- `subscribe_orderbook()` - 订单簿更新
- `subscribe_trades()` - 交易流
- `subscribe_user_orders()` - 用户订单

## 安全注意事项

1. **私钥安全** - 永远不要在日志或代码中暴露私钥
2. **模拟测试** - 首次运行务必使用 `--dry-run` 模式
3. **小额测试** - 实盘先使用小额资金测试
4. **监控日志** - 定期检查交易日志和异常

## 依赖项

- Python 3.10+
- web3 >= 6.0.0
- aiohttp >= 3.9.0
- pydantic >= 2.0.0
- python-dotenv >= 1.0.0

## License

MIT License
