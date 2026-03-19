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
│   ├── wallet_scanner.py         # 钱包扫描器
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
│   ├── financial.py      # 财务计算
│   ├── retry.py          # 重试机制 (tenacity)
│   ├── emergency_stop.py # 紧急停止+强制平仓
│   ├── structured_logging.py  # 结构化日志
│   ├── monitoring.py     # 监控告警
│   ├── multi_provider.py # 多RPC/WS failover
│   ├── gas_nonce.py      # Nonce管理+Gas优化
│   └── slippage_protection.py  # Slippage保护
├── tests/                # 测试套件
│   ├── conftest.py       # pytest配置+fixtures
│   ├── test_risk_manager.py    # 风险管理测试
│   ├── test_polymarket_client.py  # 客户端测试
│   ├── test_copy_executor.py  # 跟单执行测试
│   └── test_integration.py    # 集成测试
├── logs/                 # 日志目录 (运行时生成)
│   ├── bot.log           # 主日志
│   ├── trades.log        # 交易流水
│   └── audit.log         # 审计日志
├── main.py               # 主入口
├── pytest.ini            # 测试配置
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

## ⚠️ 交易风险提示

**使用本软件前，请务必仔细阅读以下风险提示：**

### 核心风险声明

1. **资金损失风险**
   - 所有交易均存在资金损失风险，您可能损失全部投入资金
   - 过去的收益不代表未来表现
   - 本软件不保证盈利，亏损风险由用户自行承担

2. **市场风险**
   - 预测市场具有高度不确定性
   - 事件结果可能与预期完全相反
   - 市场流动性不足可能导致无法及时平仓
   - 价格剧烈波动可能导致止损失效

3. **技术风险**
   - 网络故障可能导致订单无法及时执行
   - API服务中断可能错过交易机会
   - 智能合约漏洞可能导致资金损失
   - 软件bug可能导致非预期交易行为

4. **跟单风险**
   - 被跟单钱包可能改变策略或出现失误
   - 做市商行为可能被误判为普通交易
   - 跟单延迟可能导致成交价格偏离
   - 钱包地址可能被替换或欺骗

5. **系统性风险**
   - 区块链网络拥堵导致交易延迟
   - RPC节点故障影响交易执行
   - 交易所暂停服务或下架市场
   - 监管政策变化影响交易合法性

### 本软件限制

- **不构成投资建议** - 本软件仅为工具，所有交易决策由用户自主做出
- **不承担损失责任** - 因使用本软件产生的任何损失，开发者不承担责任
- **无收益保证** - 本软件不承诺任何收益率或盈利保证
- **测试不充分** - 虽然有单元测试，但无法覆盖所有极端情况

### 建议措施

| 措施 | 说明 |
|------|------|
| **小额起步** | 首次实盘建议使用 < $100 测试 |
| **设置止损** | 每笔交易都设置合理的止损价格 |
| **控制仓位** | 单笔仓位不超过总资金的 3% |
| **分散风险** | 不要把所有资金投入单一市场 |
| **持续监控** | 定期检查机器人运行状态和持仓 |
| **紧急预案** | 了解紧急停止文件的使用方法 |
| **资金管理** | 只使用可承受损失的资金 |

### 紧急停止

创建 `EMERGENCY_STOP` 文件可触发紧急停止：

```bash
# 紧急停止（会强制平仓所有持仓）
echo "紧急停止原因" > EMERGENCY_STOP

# 恢复运行
rm EMERGENCY_STOP
```

### 免责声明

本软件按"原样"提供，不附带任何明示或暗示的保证。在任何情况下，开发者或贡献者均不对任何直接、间接、偶然、特殊、惩罚性或后果性损害承担责任，包括但不限于：

- 资金损失
- 利润损失
- 业务中断
- 数据丢失
- 系统故障

使用本软件即表示您已充分理解并接受上述所有风险。

---

**如果不确定任何风险，请不要使用本软件进行实盘交易。**

## License

MIT License
