# Polymarket 自适应跟单机器人

基于策略信号自动执行交易，支持风险管理和熔断机制的 Python 交易机器人。

## 功能特性

- **自适应策略** - 根据市场状态自动选择最优策略
- **Endgame Sweeper** - 高概率收割 (95%+ 概率)
- **钱包跟单** - 开仓/平仓/持仓同步，完整跟单闭环
- **L2 API 认证** - 支持 EOA、Poly Proxy、Gnosis Safe 模式
- **Gnosis Safe 支持** - 兼容浏览器钱包 Proxy 合约
- **风险管理** - 止损止盈、仓位控制、熔断保护
- **Telegram 通知** - 交易提醒和状态报告
- **交易持久化** - SQLite 存储，幂等性保证

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 `.env`

```bash
cp .env.example .env
# 编辑 .env 文件
```

### 3. 必需配置项

```bash
# 钱包私钥（用于 API 认证）
PRIVATE_KEY=your_private_key

# MetaMask EOA 地址（用于签名）
WALLET_ADDRESS=0xYourWalletAddress

# Proxy 地址（资金存放位置，从 polymarket.com/settings 获取）
FUNDER_ADDRESS=0xYourProxyAddress

# Polygonscan API Key（跟单功能必需）
POLYGONSCAN_API_KEY=your_api_key
```

**如何获取 Proxy 地址**：
1. 登录 https://polymarket.com
2. 点击右上角头像 → Settings
3. 找到 "Wallet Address"

### 4. 运行

```bash
# 模拟模式（推荐首次运行）
python main.py

# 实盘模式（修改 .env 中 DRY_RUN=false）
python main.py
```

## 配置说明

### 风险管理

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 每日最大损失 | $100 | 达到后触发熔断 |
| 单笔最大仓位 | $50 | 单笔交易上限 |
| 最大并发仓位 | 5 | 同时持仓数量 |
| 默认止损 | 15% | 价格反向移动 |
| 默认止盈 | 25% | 价格顺向移动 |

### 跟单配置

| 配置项 | 默认值 | 说明 |
|--------|---------|------|
| `COPY_TRADING_ENABLED` | `true` | 是否启用跟单 |
| `COPY_AUTO_DISCOVER` | `true` | 自动发现高质量钱包 |
| `COPY_TRADING_MODE` | `smart` | 模式：smart/fixed/proportional/full |
| `COPY_FIXED_AMOUNT` | `10` | 固定金额跟单 (USD) |
| `COPY_PROPORTIONAL_RATIO` | `0.1` | 比例跟单 (0.1=10%) |
| `COPY_MAX_AMOUNT` | `50` | 单笔最大跟单 (USD) |
| `COPY_FOLLOW_CLOSE` | `true` | 跟平仓（关键！） |
| `COPY_POSITION_SYNC_INTERVAL` | `300` | 持仓同步间隔（秒） |

### 平仓跟单

避免持仓失控的关键功能：
- **实时跟平仓** - 目标平仓时立即跟随
- **定期同步** - 每 5 分钟检查目标持仓
- **遗漏检测** - 发现目标已平但我们还持有，自动平仓

### 钱包质量

| 等级 | 分数 | 跟单倍数 |
|------|------|----------|
| Elite | 9.0-10.0 | 2.0x |
| Expert | 7.0-8.9 | 1.5x |
| Good | 5.0-6.9 | 1.0x |
| Poor | <5.0 | 排除 |

## 安全注意事项

1. **私钥安全** - 永远不要在日志或代码中暴露私钥
2. **模拟测试** - 首次运行务必使用 `DRY_RUN=true`
3. **小额测试** - 实盘先使用小额资金测试
4. **监控日志** - 定期检查交易日志和异常

## 紧急停止

创建 `EMERGENCY_STOP` 文件可触发紧急停止并强制平仓：

```bash
# 触发停止
echo "紧急停止原因" > EMERGENCY_STOP

# 恢复运行
rm EMERGENCY_STOP
```

## ⚠️ 交易风险提示

**核心风险**：
- 可能损失全部资金，过去收益不代表未来表现
- 市场流动性不足可能导致无法及时平仓
- 网络故障或 API 中断可能导致交易失败
- 被跟单钱包策略改变或失误
- 智能合约漏洞或软件 bug

**免责声明**：
- 本软件不构成投资建议，不保证盈利
- 开发者不对任何损失承担责任
- 仅使用可承受损失的资金进行交易

**建议**：
- 首次实盘使用 < $100 测试
- 单笔仓位不超过总资金的 3%
- 持续监控机器人运行状态

**如果不确定任何风险，请不要使用本软件进行实盘交易。**

---

MIT License

## License

MIT License
