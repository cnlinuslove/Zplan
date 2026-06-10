# 企查查数据接入 Z-Plan — 调研与设计

> **状态：调研完成，待用户提供 API 凭证后进入 Phase 1 MVP。**
> 调研日期：2026-06-08

---

## 一、企查查开放平台概况

**平台地址：** https://openapi.qcc.com  
**认证方式：** HMAC-MD5 签名（Header: `Token` + `Timespan`，Query: `key`）  
**计费模式：** 预充值余额制（500/2000/5000/10000/50000 元档位），按调用次数扣费  
**数据规模：** 市场主体 3.65 亿+、司法诉讼 2.5 亿+、知识产权 2.1 亿+、新闻舆情 4300 万+

### 认证机制（已确认）

```
Headers:
  Token:    MD5(key + Timespan + SecretKey) → 32位大写十六进制字符串
  Timespan: 当前 Unix 时间戳（秒）

Query:
  key:      AppKey（在"我的接口"页面获取）
  keyword:  搜索关键字（企业名/统一社会信用代码/注册号）
```

---

## 二、可用 API 清单（19 个端点）

### 工商信息（核心）

| API ID | 名称 | 关键返回字段 | Z-Plan 价值 |
|--------|------|-------------|------------|
| **410** | 企业工商信息 | `Name`, `CreditCode`, `OperName`(法人), `RegistCapi`(注册资本), `Scope`(经营范围), `Status`(状态), `EconKind`(企业类型), `Address`, `IsOnStock`, `StockNumber`, `StockType`, `StartDate`, `CheckDate` | ★★★★★ 基础画像 |
| **735** | 企业工商详情 | **`Partners`**(股东: `StockName`, `StockPercent`, `InvestType`, `ShouldCapi`), **`Employees`**(主要人员), **`Branches`**(分支机构), **`ChangeRecords`**(变更记录), `ContactInfo`, `Industry`, `InsuredCount`(参保人数), `PersonScope`(人员规模), `TagList` | ★★★★★ 股东/实控人穿透 |
| **886** | 企业模糊搜索 | 企业名列表 | ★★★ 公司名→统一代码 |
| **213** | 企业年报信息 | 年报财务数据 | ★★★★ 财务基本面 |

### 风险核查（黑天鹅滤网）

| API ID | 名称 | Z-Plan 价值 |
|--------|------|------------|
| **887** | 裁判文书核查 | ★★★★ 选股负面滤网 |
| **740** | 失信核查 | ★★★★★ 直接排除 |
| **741** | 被执行人核查 | ★★★★★ 直接排除 |
| **742** | 限制高消费核查 | ★★★ 实控人风险 |
| **739** | 经营异常核查 | ★★★★ 基本面预警 |
| **748** | 严重违法核查 | ★★★★★ 直接排除 |

### 知识产权（科技含量验证）

| API ID | 名称 | Z-Plan 价值 |
|--------|------|------------|
| **231** | 商标查询 | ★★★ 消费品牌 |
| **514** | 专利查询 | ★★★★ 科技硬实力 |
| **233** | 著作权软著查询 | ★★★ 软件公司 |

### 其他

| API ID | 名称 | 说明 |
|--------|------|------|
| 271 | 税号开票信息 | 财务验证 |
| 255 | 资质证书 | 特许经营 |
| 515 | 备案网站查询 | 线上业务 |
| 2001 | 企业信息核验 | 批量验证 |
| 2003 | 客户身份识别 | KYC 场景 |
| 2006 | 综合风险排查 | 聚合多个风险维度 |

---

## 三、API 调用流程设计

### 3.1 调用链路

```
Z-Plan DB (zplan.db)
    │
    ├── stock_list.ts_code  ←→  企查查搜索接口 (/886)
    │        │                       │
    │        │              通过公司名反查 KeyNo
    │        │                       │
    │        └──── KeyNo ────────────┘
    │                     │
    │         企查查工商信息 (/410) + 详情 (/735)
    │                     │
    │              ┌──────┴──────┐
    │              │             │
    │         company_profile  company_shareholder
    │         company_risk     company_annual_report
    │              │             │
    │              └── 选股 LLM 上下文增强
    │
    └── 规则引擎滤网（风险排除）
```

### 3.2 关键问题：股票代码 → 企查查 KeyNo 映射

企查查的查询入口是**企业名称**或**统一社会信用代码**，不是股票代码。需要建立映射：

**方案 A（推荐）：** 利用 `stock_list.name` → 企查查搜索 (/886) → KeyNo
- 优点：已有数据即可，无需额外整理
- 缺点：全市场批量查询时请求量大（5000+ 次搜索），首次建立需要分批执行

**方案 B：** 利用 API 410 中的 `IsOnStock` / `StockNumber` 反向验证
- 优点：可直接确认该企业是否上市主体
- 缺点：需要先有 KeyNo

**实施建议：**
1. Phase 1 使用方案 A，先对 Top 300 选股池跑
2. Phase 2 建立全市场 `stock_list` ↔ `qcc_keyno` 映射表
3. 映射表持久化后，后续只需增量更新

### 3.3 频率控制

```python
# 企查查会员通常有 QPS 限制（猜测 1-5 QPS），保守设计：
QCC_RATE_LIMIT_SECONDS = 1.0        # 每秒最多 1 次
QCC_DAILY_LIMIT = 5000              # 日调用上限（根据套餐）
QCC_BATCH_SLEEP_SECONDS = 0.3       # 批次间间隔
```

---

## 四、数据库 Schema 设计

### 4.1 新增表

```sql
-- 企业基础画像（stock_list 的扩展维度）
CREATE TABLE company_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code VARCHAR(16) NOT NULL,              -- 关联 stock_list
    market VARCHAR(8) NOT NULL DEFAULT 'a',
    qcc_key_no VARCHAR(64),                    -- 企查查内部 KeyNo
    credit_code VARCHAR(32),                   -- 统一社会信用代码
    legal_person VARCHAR(64),                  -- 法定代表人
    registered_capital VARCHAR(32),            -- 注册资本
    registered_capital_num REAL,               -- 注册资本（数值，万元）
    company_type VARCHAR(64),                  -- 企业类型（股份有限公司等）
    business_scope TEXT,                       -- 经营范围
    address VARCHAR(256),                      -- 注册地址
    establish_date DATE,                       -- 成立日期
    status VARCHAR(32),                        -- 经营状态
    insured_count INTEGER,                     -- 参保人数
    person_scope VARCHAR(32),                  -- 人员规模
    industry_qcc VARCHAR(64),                  -- 企查查行业分类
    is_listed BOOLEAN DEFAULT 0,              -- 是否上市主体
    stock_number_qcc VARCHAR(16),             -- 企查查记录的股票代码（需与ts_code交叉验证）
    synced_at_utc DATETIME NOT NULL,
    UNIQUE(ts_code, market)
);

-- 股东/实控人信息
CREATE TABLE company_shareholder (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code VARCHAR(16) NOT NULL,
    market VARCHAR(8) NOT NULL DEFAULT 'a',
    shareholder_name VARCHAR(128),             -- 股东名称
    stock_type VARCHAR(32),                    -- 股东类型（自然人/企业/机构）
    stock_percent REAL,                        -- 持股比例 (%)
    subscribed_capital REAL,                   -- 认缴出资额（万元）
    paid_capital REAL,                         -- 实缴出资额（万元）
    invest_type VARCHAR(32),                   -- 投资类型
    is_actual_controller BOOLEAN DEFAULT 0,    -- 是否为实控人（需推断）
    is_controlling BOOLEAN DEFAULT 0,          -- 是否为控股股东（需推断）
    synced_at_utc DATETIME NOT NULL,
    UNIQUE(ts_code, market, shareholder_name)
);

-- 风险事件（可同时作为 financial_alerts 来源）
CREATE TABLE company_risk_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_code VARCHAR(16) NOT NULL,
    market VARCHAR(8) NOT NULL DEFAULT 'a',
    risk_type VARCHAR(32) NOT NULL,            -- dishonest/execution/court/abnormal/serious
    case_no VARCHAR(64),                       -- 案号
    title VARCHAR(256),                        -- 事件标题
    involved_amount REAL,                      -- 涉案金额（万元）
    event_date DATE,                           -- 事件日期
    court VARCHAR(128),                        -- 执行法院
    status VARCHAR(32),                        -- 状态
    extra_json TEXT,                           -- 原始返回
    synced_at_utc DATETIME NOT NULL,
    INDEX idx_company_risk_ts_type (ts_code, risk_type)
);
```

### 4.2 扩展现有表

`stock_list` 表扩展一个字段：
```sql
ALTER TABLE stock_list ADD COLUMN qcc_key_no VARCHAR(64);
-- 企查查 KeyNo 映射，null 表示尚未建立映射
```

---

## 五、代码实现路径

### 5.1 新文件

```
zplan-共享/src/zplan_shared/qcc_client.py   -- API 客户端（签名 + HTTP）
zplan-共享/src/zplan_shared/qcc_etl.py      -- ETL 脚本（回填 company_profile 等）
zplan-选股/config/qcc_filters.yaml          -- 选股滤网规则（风险排除条件）
```

### 5.2 qcc_client.py 核心设计

```python
"""企查查开放平台 API 客户端。"""
from __future__ import annotations
import hashlib, os, time, requests

class QccClient:
    def __init__(self, app_key=None, app_secret=None):
        self.app_key = app_key or os.getenv("QCC_APP_KEY")
        self.app_secret = app_secret or os.getenv("QCC_APP_SECRET")
        self.base_url = "https://api.qcc.com"

    def _sign(self) -> tuple[str, str]:
        timespan = str(int(time.time()))
        token = hashlib.md5(
            (self.app_key + timespan + self.app_secret).encode()
        ).hexdigest().upper()
        return token, timespan

    def _headers(self) -> dict:
        token, timespan = self._sign()
        return {"Token": token, "Timespan": timespan}

    def search_company(self, keyword: str) -> dict | None:
        """模糊搜索 → 获取 KeyNo"""
        ...

    def get_basic_info(self, keyword: str) -> dict | None:
        """API 410：企业工商信息"""
        ...

    def get_detail(self, search_key: str) -> dict | None:
        """API 735：企业工商详情（股东+人员+变更）"""
        ...

    def check_risk(self, keyword: str, risk_type: str) -> dict | None:
        """统一风险核查接口"""
        ...
```

### 5.3 qcc_etl.py 核心设计

按现有 `stock_meta_etl.py` 和 `stock_concept_etl.py` 的模式：

1. 遍历 `stock_list`（或指定 subset）
2. 用 `stock_list.name` → 企查查搜索 → 获取 KeyNo
3. 用 KeyNo → 获取基础信息 + 详情
4. Upsert 到 `company_profile` / `company_shareholder`
5. 可选：跑风险核查接口 → `company_risk_event`

### 5.4 选股策略集成

**规则引擎滤网（`zplan-选股`）：**
```yaml
# qcc_filters.yaml
risk_exclude:
  - risk_type: dishonest       # 失信
    action: EXCLUDE
    lookback_days: 365
  - risk_type: execution       # 被执行人
    action: SCORE_MULTIPLY
    factor: 0.5
  - risk_type: abnormal        # 经营异常
    action: EXCLUDE
    lookback_days: 180
```

**LLM 上下文增强（`llm_research.py`）：**
- 将 `company_profile` 的关键字段注入 `research_with_llm()` 的 prompt
- 经营范围 vs 概念标签交叉验证 → 减少"蹭概念"误判
- 实控人背景 → 国企/民企风格判断
- 参保人数/专利数 → 真实业务规模信号

---

## 六、需要你提供的

请在企查查开放平台完成以下操作并告知：

### 6.1 必须提供

| 信息 | 获取方式 | 说明 |
|------|---------|------|
| **AppKey** | https://openapi.qcc.com → 登录 → "我的接口" | 接口调用身份标识 |
| **AppSecret** | 同上 | 用于签名的密钥 |

### 6.2 建议确认

| 信息 | 重要性 | 影响 |
|------|--------|------|
| **会员套餐类型** | 高 | 决定每日 API 调用上限和可用接口范围 |
| **余额 / 调用次数** | 高 | 全市场 5000+ 股票首次回填约需 5000-10000 次调用 |
| **QPS 限制** | 中 | 影响首次回填耗时 |

### 6.3 获取步骤

1. 登录 https://openapi.qcc.com （用企查查会员账号）
2. 进入「我的接口」或「控制台」
3. 创建应用 → 获取 AppKey / AppSecret
4. 查看「已购接口」列表，确认哪些 API 已有权限
5. 查看接口定价页面：每个 API 的单次调用价格
6. （可选）在「API 测试」页面用企业名试用一次，看看返回数据

---

## 七、实施计划

### Phase 1 — MVP（半天）

**目标：** 单票查询打通，验证数据质量

- [ ] 创建 `qcc_client.py`（API 签名 + 3 个核心接口）
- [ ] 手工对 5 只持仓/关注股票跑一轮
- [ ] 对比企查查数据 vs 东财数据，确认补充价值
- [ ] 存入 `company_profile` / `company_shareholder` 表

### Phase 2 — 全市场回填（1-2 天）

**目标：** 选股池全覆盖

- [ ] `qcc_etl.py`：全市场（或 Top 300）股票 → KeyNo 映射
- [ ] 批量回填基础画像 + 股东信息
- [ ] 风险核查接口集成（至少失信 + 被执行）
- [ ] LLM 研报 prompt 注入企查查维度

### Phase 3 — 深度应用（2-3 天）

**目标：** 选股差异化

- [ ] 规则滤网：风险事件自动排除/降分
- [ ] 股东穿透分析：识别知名机构/国资背景
- [ ] 专利/知识产权评分因子
- [ ] 实控人变更预警 → 纳入 `financial_alerts`

---

## 八、风险与注意事项

1. **公司名匹配歧义：** 股票简称 vs 工商全称可能不精确（如"平安银行" vs "平安银行股份有限公司"）。API 886 的模糊搜索应能处理，但需要验证匹配率。

2. **上市主体 vs 运营主体：** 有些集团的上市主体和实际运营主体不同（如控股型上市公司），企查查的数据可能反映的是母公司而非运营子公司，需要留意 `StockNumber` 字段交叉验证。

3. **API 调用成本：** 全市场 5000 只股票 × 3 个核心接口 = 15000 次调用。如果单次 0.5 元，即约 7500 元。建议优先对选股池（Top 300）回填，成本约 450 元。

4. **数据新鲜度：** 企查查工商数据更新频率为 T+1 到 T+30 不等（取决于工商局公示节奏），不是实时数据，适合做中长线基本面而非短线信号。
