# Charter — 黑客松项目文档

> **黑客松主题**：Build something agents want
> **项目代号**：Charter（暂定）
> **一句话定位**：Agent 经济缺失的 Principal 层 —— 从 principal context 自动投影出 agent 的"工作合同"，让其他 agent 在调用前可以查询、验证、收窄
> **时间**：2026 年 5 月，周末黑客松一日完成

---

## 一、项目背景与约束

### 黑客松主题解读
"**Build something agents want**" 强调 agent 是用户。要做的是**agent 主动调用、agent 喜欢用**、为 agent 消费而设计的工具/服务/基础设施，**而不是**做一个 agent 应用。

### 个人约束（选题过滤器）
1. **原创性**：不能是市面已有产品的模仿
2. **竞争壁垒**：不能是大厂轻而易举能做的方向
3. **轻量化**：一个人一天可交付的 thin layer
4. **可 demo**：黑客松现场 5-10 分钟能让评委看懂的对比性演示
5. **零冷启动数据**：不需要训练数据 / 专有语料
6. **API 依赖可控**：避开需要企业审批的 API

---

## 二、决策路径（探索过的方向）

### 已被淘汰的方向（搜索后发现拥挤）

| 方向 | 淘汰原因 |
|---|---|
| Agent Scratchpad as a Service | Mem0 / Letta / Zep / Engram / mcp-mem0 等 ≥10 个玩家；Anthropic 自己已支持 ChatGPT memory 导入 |
| Agent Recap / 个人 Agent 日报 | 任务理解错了，本质是 "for human about agent" 而非 "for agent" |
| Vertical Agent | 不是 agent 用户视角，违反主题 |
| Agent Action Receipts / 审计 | PrMaat / Nylas / Kiteworks / IETF draft / AWS / Cloudflare / Google AP2 / Visa / Mastercard 全在做 |
| Stack Overflow for Agents | Mozilla 2026-03-23 发布 `cq`，已有 Claude Code 插件 |
| Agent Task Marketplace | ClawGig / Aigora / AgentMarket / Near AI / Anthropic 自己在做 |
| Async HITL Service | OpenAI / Google ADK / Cloudflare / Temporal / HumanOps 均已绑定框架做了 |
| Long-running Watchdog | Hermes Agent 2026-05-07 刚发布 |
| Tool Trust Score | Q1 2026 已经有人给 17K MCP servers 全打分了 |

### 关键学习
2026 年 5 月的 agent 生态，**任何"通用横向基础设施"想法都有 1-3 家在做**。原创性不能追"无人做"，要追**"无人做得简单/免费/具体角度"**。

---

## 三、最终方向：Charter — Principal 层

### 核心洞察
现有 agent 协议都是**正向声明**：

| 现有概念 | 回答的问题 | 谁声明 | 生效粒度 |
|---|---|---|---|
| **Agent Card**（A2A）| 我**能**做什么？| Agent operator / framework | 每个 skill |
| **Identity**（Web Bot Auth）| 我**是**谁？| Agent operator / CA | 每条消息 |
| **Resume / Reputation** | 我**做过**什么？| 第三方 | 聚合 |
| **Mandate**（AP2）| 这次任务**用户授权了**什么？| 终端用户 | 每笔交易 |

它们勾勒的是 agent 的**可达表面**（capability + identity + history + per-task auth）。

**缺失的维度**：agent 在**它的 principal 上下文**里实际允许/愿意做什么。

### 安全 / 身份理论框架

云安全 IAM 已经把这个理清楚了三层结构：

| 层 | 概念 | Agent 生态现状 |
|---|---|---|
| **Capability**（技术能力）| 这个东西**技术上能**做 X？| ✅ Agent Card |
| **Authority / Principal**（操作权限） | 这个东西**代谁** + **被允许**做 X？| ❌ **空白** ← 本项目位置 |
| **Authorization**（具体授权）| 这次具体请求被批准做 X？| ✅ AP2 Mandate |

**Charter 填的就是中间这层**。

### 三个容易混在一起但必须拆开的角色

| 角色 | 回答的问题 | 可以是谁 | v0 demo |
|---|---|---|---|
| **Principal** | 这个 agent 代谁工作？ | 人、组织、上游 agent | Alice / Bob |
| **Charter Issuer** | 谁创建、审核、签名、发布这份 Charter？ | principal 本人、被委托服务、企业 admin、上游 agent | Alice / Bob 自己 |
| **Agent Operator** | 谁运行这个底层 agent / 发布 Agent Card？ | agent provider、framework、marketplace、开发者 | 同一个通用 worker agent |

`owner` 是容易歧义的口语词：它可能指 agent operator，也可能指 principal，也可能指 Charter issuer。文档和协议字段里应尽量不用 `owner`，除非引用外部协议已经这样命名。

这也是 Charter 比 Agent Card 多出的结构：**同一个 Agent Operator 提供的同一个 agent，可以同时服务多个 Principal，每个 Principal 由不同 Issuer 发布不同 Charter**。

### Charter 的绑定粒度

Charter 绑定的是 **`principal × agent` 关系对象**，不是 agent class、model、Agent Card，也不是某次 task。

| 候选绑定粒度 | 为什么不选 |
|---|---|
| Model | 同一个模型可被无数 agent / principal 复用，权限语义太粗 |
| Agent class / Agent Card | 只能表达这个 agent 技术上能做什么，不能表达代谁工作 |
| Single task | 这是 AP2 Mandate / Authorization 的粒度，不是持续 authority 的粒度 |
| `principal × agent` | 能表达同一 agent 在不同 principal 下的不同 scope、refusal、hours、cost、style、escalation |

因此同一个底层 worker agent 可以同时存在多份 Charter：

- `Alice × worker_agent_v1`
- `Bob × worker_agent_v1`
- `BookkeeperBot × ocr_agent_v1`

每一份都是独立的 Charter Instance。Calling agent 在委派前检查的是目标这一次关系对应的 Charter，而不是泛泛检查“这个 agent 是什么”。

### 关键类比：劳动力市场

| Agent 概念 | 人类世界类比 |
|---|---|
| Agent Card | 简历（能力清单）|
| Identity | 工卡 / 护照 |
| Resume / Rating | 履历 / 评价 |
| Mandate | 派工单（per-task）|
| **Charter** | **雇佣合同**（who employs，scope，hours，refusals，pay）|

简历描述孤立的个体；雇佣合同描述**个体 × principal 的关系属性**。二者**结构性不可合并**。

### 会计师 Agent 例子（讲故事用）

一个通用 coding agent，Agent Card 说它能写 20 种语言。
- 它这次服务的 principal 是会计师，run 它是为报税季帮忙
- 派它去给陌生公司写微服务？**技术能力 ✅，principal 上下文 ❌**

Agent Card 没字段表达这件事——因为这是 **principal × agent 的复合属性**，不是 agent 的内在属性。

---

## 四、与现有概念的差异（结构性论证）

### 为什么"不能直接塞进 Agent Card"

| 论点 | 解释 |
|---|---|
| **正交概念** | Agent Card 描述内在属性（孤立看是什么）；Charter 描述关系属性（在 principal 上下文里是什么）|
| **数据所有权** | Agent Card 由 agent operator / framework 持有；Charter 由 principal 或 Charter Issuer 持有，独立于 agent 实现 |
| **生命周期** | Agent Card 稳定；Charter 随 principal 状态变化（换岗、新项目、临时离岗）需要热更新 |
| **多 Charter 单 Agent** | 同一 agent 实例被多 principal 复用，每个有不同 Charter — Agent Card 模型不可能表达 |
| **生成方式** | Agent Card 是 agent/operator 写的；Charter 需要**读取 principal context 投影**——这是个 service，不是字段 |

### 与 PocketOS 抹库事件（2026-04-27）的关联

Cursor + Claude Opus 4.6 在 9 秒里删掉 PocketOS 整个生产数据库 + 备份。事后追溯：agent 没有任何机制声明 "我不应该在没有 human_approval 的情况下 DROP TABLE prod"。

如果它的 Charter 写了相应约束，且 orchestrator 是遵守 Charter 协议的调用方，那么调用前的 Delegation Gate 会把任务判为 `needs_approval` 或 `incompatible`，从而阻止这次委派直接发生。

这是 **pre-event prevention**（事前规避），不同于 PrMaat / Kiteworks 等做的 **post-event audit**（事后审计）。v0 的阻断边界在 delegation 层：它约束守规矩的 calling agent 是否应该派活；真正不可绕过的资源级阻断需要 v1+ 把 Charter 绑定到 DB、支付、文件系统或工具网关。

---

## 五、技术架构

### 整体架构图

```
┌─────────────────────────────────────────────────────┐
│ Principal / Charter Issuer（人、组织 or agent）       │
│  - 一次 profile.yaml / 导入 memory / GitHub           │
└────────────────────┬────────────────────────────────┘
                     │ 一次性 / 持续同步
                     ▼
┌─────────────────────────────────────────────────────┐
│ Charter Service（项目核心）                            │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐  │
│  │ Projection  │  │  Signing     │  │ Hosting    │  │
│  │ Engine      │  │ (issuer key) │  │ (optional) │  │
│  │ LLM 投影     │  │              │  │            │  │
│  └─────────────┘  └──────────────┘  └────────────┘  │
└────────────────────┬────────────────────────────────┘
                     │ 输出
                     ▼
┌─────────────────────────────────────────────────────┐
│ 一份签名 JSON Charter                                 │
│ 公开在 URL：                                          │
│  - charter.dev/{principal}/{agent}（SaaS 托管）      │
│  - 或 alice.example.com/.well-known/charter         │
│    （principal / issuer 自托管）                      │
└────────────────────┬────────────────────────────────┘
                     ▲
                     │ Fetch + Verify
                     │
┌────────────────────┴────────────────────────────────┐
│ Calling Agent                                        │
│  - 通过 Charter MCP server 调用（推荐）                │
│  - 或通过 SDK / framework middleware                 │
└─────────────────────────────────────────────────────┘
```

### Charter JSON Schema（v0.1 定稿）

```json
{
  "version": "0.1",
  "charter_id": "charter:alice@acme.com:research_agent_v1:2026-05-17",

  "binding": {
    "type": "principal_agent",
    "principal_id": "alice@acme.com",
    "agent_id": "research_agent_v1"
  },

  "principal": {
    "type": "human",
    "id": "alice@acme.com",
    "role_summary": "Senior Accountant at Acme Corp, focused on tax season Q1-Q2"
  },

  "issuer": {
    "type": "human",
    "id": "alice@acme.com",
    "relationship_to_principal": "self"
  },

  "agent_operator": {
    "type": "service",
    "id": "generic_worker_agent_provider",
    "agent_card_url": "https://agents.example.com/research_agent_v1/card.json"
  },

  "principal_chain": [],

  "visibility": {
    "charter": "public",
    "raw_principal_context": "private",
    "private_clauses": "not_supported_in_v0"
  },

  "summary": {
    "plain_language": "This agent acts for Alice's accounting, tax, and bookkeeping work during tax season. It must avoid marketing, code authoring, and any handling of customer PII without explicit approval. Destructive database operations on production data always require approval."
  },

  "clauses": [
    {
      "id": "C-001",
      "type": "scope",
      "text": "This agent acts for Alice's accounting, tax filing, bookkeeping, financial analysis, invoice classification, and tax document organization work."
    },
    {
      "id": "C-002",
      "type": "out_of_scope",
      "text": "Do not accept marketing copy, advertising design, code authoring, or UI design work. These require a separate Charter."
    },
    {
      "id": "C-003",
      "type": "approval_required",
      "text": "Any handling of customer personally identifiable information (name + bank/tax/income combinations) requires explicit principal approval per session."
    },
    {
      "id": "C-004",
      "type": "approval_required",
      "text": "Any destructive action on production data — including DROP, DELETE, TRUNCATE, or backup deletion — requires explicit human approval."
    },
    {
      "id": "C-005",
      "type": "operational_limit",
      "text": "Operational window is Monday to Friday, 09:00-18:00 America/New_York. Per-task budget cap is USD 0.50. Tasks outside the window or exceeding budget require approval."
    },
    {
      "id": "C-006",
      "type": "data_handling",
      "text": "May process customer tax filings, tax IDs, bank statements, and income records. Must not share with third parties, must not write to persistent cache, and must discard from working memory after task completion."
    },
    {
      "id": "C-007",
      "type": "style",
      "text": "Prefer structured output (JSON or Markdown table). Cite sources for factual claims. Respond in English or Chinese."
    }
  ],

  "decision_schema": {
    "decision": "allow | needs_approval | incompatible",
    "matched_clauses": [
      {
        "id": "string (e.g. C-004)",
        "local_decision": "allow | needs_approval | incompatible",
        "applied": "bool — true if this clause determined the aggregate decision",
        "confidence": "float in [0, 1]",
        "reason": "short natural-language explanation"
      }
    ],
    "reason": "string — short summary referencing applied clauses",
    "rewrite_available": "bool — whether propose_within_scope is likely to produce a viable rewrite"
  },

  "lifecycle": {
    "issued_at": "2026-05-17T10:00:00Z",
    "valid_until": "2026-06-16T10:00:00Z",
    "status": "active",
    "revoked_at": null,
    "replaces": null,
    "replaced_by": null
  },

  "provenance": {
    "issuer_public_key": "ed25519:MCowBQYDK2VwAyEAabc123...",
    "issuer_signature": "ed25519:base64-signature-over-all-fields-except-this-one...",
    "source_commitments": [
      {
        "type": "profile_yaml",
        "description": "alice.yaml answered by principal on 2026-05-17",
        "content_hash": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
      }
    ],
    "generated_at": "2026-05-17T10:00:00Z"
  }
}
```

**v0.1 与早期草案的差异(为什么这样改):**

- 删 `agent_id` 顶层字段:重复定义,`binding.agent_id` 是唯一来源。
- 删 `summary.primary_domains`:孤立字段无人引用,`clauses[*].text` 已包含领域信息。
- 删 `clauses[].default_decision_on_match`:替换为协议常量 `TYPE_TO_DECISION` 映射(见下小节)。
- 删 `provenance.service_attestation`:第二层签名增加 demo 复杂度,无实质安全收益;v0 信任根落在 HTTPS。
- 新增 `provenance.issuer_public_key`:Self-Attesting Charter——公钥自包含,calling agent 一次 fetch 拿到验签所需全部材料。
- `decision_schema.matched_clauses` 升级为结构化数组,每条带 `local_decision / applied / confidence / reason`;`confidence` 从 verdict 顶层移到 per-clause 级。
- `decision_schema.proposed_rewrite` 改为 `rewrite_available: bool`——verdict 只做判断,rewrite 内容由独立 tool `propose_within_scope` 生成。

### 为什么 v0 用 clauses，而不是固定 policy 字段

Agent 任务空间是开放的：今天是报税、明天是清理数据库、后天可能是帮另一个 agent 重新设计 workflow。过早把世界拆成固定的 `actions/resources/data_classes` 会让 Charter 变成笨重的企业 policy 表。

v0 的设计是 **versioned natural-language clauses + structured verdict contract + protocol-constant type mapping**：

- Charter 用 `clauses[]` 表达持续约束，每条 clause 有稳定 `id` 和 `type`。
- `type` 字段决定该 clause 的 **Local Decision**(per-clause 决策),通过下表所示的协议常量映射。
- Calling agent 的 LLM 只判断"intended task 是否命中这条 clause",**不** 自由决定 allow/needs_approval/incompatible。
- Verdict 必须按 `decision_schema` 输出，并引用结构化 `matched_clauses[]`。
- 常见高频条款未来可以编译成机器字段，但不作为 v0 的协议前提。

#### `TYPE_TO_DECISION` 协议常量

| clause.type | local decision | 含义 |
|---|---|---|
| `scope` | `allow` | 这是该 agent 在本 principal 下可以做的事 |
| `out_of_scope` | `incompatible` | 这是该 agent 在本 principal 下被排除的事 |
| `approval_required` | `needs_approval` | 需要 principal 显式批准才能进行 |
| `operational_limit` | `needs_approval` | 工作时段、预算、频率类约束;超出需批准 |
| `style` | `allow` | 风格软约束,LLM 输出时遵守即可 |
| `data_handling` | `needs_approval` | 涉及敏感数据;接触前需批准 |

这避免了两个极端：既不是无限扩展的硬字段表，也不是完全不可审计的自由文本判断。LLM 负责模糊判断(命中与否),协议负责确定性聚合(下方"冲突解决"小节)。

### Clause 冲突解决

一个 intended task 可能同时命中多条 clause,且这些 clause 的 local decision 不一致。例如"周日晚 10 点做 Acme 发票分类"会同时命中 `C-001 scope`(local: allow)和 `C-005 operational_limit`(local: needs_approval)。

v0 采用 **严格优先级聚合**:

```
incompatible  >  needs_approval  >  allow
```

伪代码:
```python
def aggregate(local_decisions: list[Decision]) -> Decision:
    if "incompatible" in local_decisions:
        return "incompatible"
    if "needs_approval" in local_decisions:
        return "needs_approval"
    return "allow"
```

这条规则:**单调、确定、可单元测试,且倾向保守**。任何一条 clause 说"要停"就停——这跟 Charter 整体语义一致(Charter 是边界声明,不是允许清单,叠加约束 ≠ OR-equivalence)。

**Applied Clause:** verdict 的 `matched_clauses[]` 数组中,`applied: true` 标记最终决策的来源 clause(可能多条同时 applied)。

**Edge case 兜底:**

| 情况 | 默认行为 |
|---|---|
| 没有任何 clause 命中 | `needs_approval` —— Charter 是闭包约束,未明确允许的事保守对待 |
| 所有命中 clause 的 confidence < 0.5 | `needs_approval` —— LLM 信心低时降级 |
| 一条 clause 同时被判命中两种 type | 不存在,clause 只有唯一 type |

### 加密与信任模型

v0 采用 **Self-Attesting Charter + HTTPS 信任根**,刻意做到最薄。

**信任链:**

```
TLS / HTTPS         →  信任 charter.dev 域名(由 CA 体系背书)
    ↓
charter.dev 返回 Charter JSON(含 issuer_public_key + issuer_signature)
    ↓
calling agent 用 JSON 内的 issuer_public_key
              验 JSON 内的 issuer_signature
```

签名覆盖范围:`issuer_signature` 字段以外的整个 Charter JSON,规范序列化后用 issuer 的 Ed25519 私钥签名。

**为什么不做更重的 PKI:**

| 方案 | v0 不做的原因 |
|---|---|
| JWKS endpoint(`/.well-known/jwks.json`) | 多一层 fetch + 多一个 endpoint;demo 现场讲不清 |
| DID(decentralized identifiers)| 太重;v0 不需要去中心化标识 |
| X.509 证书链 | 完全过度工程化 |
| `service_attestation` 第二层签名 | 不增加实质安全(Charter 内容靠 issuer key 保护) |

**已知 demo-centric 取舍:**

- **TOFU 风险:** 第一次拉某个 charter_url 时,calling agent 无法独立验证 `issuer_public_key` 是否真的属于该 principal。信任落在 HTTPS / charter.dev 的域名背书上。
- **Issuer 换 key 的可发现性弱:** 新 Charter 自带新公钥,但旧 calling agent 没有"密钥指纹固定"机制。
- **不防 charter.dev 内部攻破:** 服务器被攻破时攻击者可签发任意 Charter。

这些都在第十四章"未来方向"列出升级路径(JWKS、公钥指纹固定、transparency log)。

### Charter 的公开边界

Charter 是可查询的工作合同，不是 principal memory 的公开转储。其他 agent 需要知道“这次关系下能不能委派”，不需要看到生成这些条款的原始上下文。

| 内容 | v0 是否公开 | 原因 |
|---|---|---|
| `charter_id` / `binding` | 公开 | 让 calling agent 确认自己检查的是哪一个 `principal × agent` 关系 |
| `principal` 最小身份 | 公开 | 只暴露足够解释 authority 的身份摘要，例如角色或公开 ID |
| `issuer` / `agent_operator` | 公开 | 让调用方知道谁签发、谁运行底层 agent |
| `clauses[]` / `decision_schema` | 公开 | 这是 compatibility check 的核心输入 |
| `valid_until` / signature / attestation | 公开 | 让调用方验证有效性和完整性 |
| 原始 memory / 对话历史 / 完整 profile | 不公开 | 这些是 Principal Context，不是协议公开面 |
| 来源全文，如 LinkedIn/CV/内部政策 | 不公开 | 只保留 source type、摘要、hash 或 commitment |
| 私有 clause | v0 不支持 | 如果 calling agent 看不到条款，就无法稳定判断；未来再做 local-only / selective disclosure |

因此，公开 artifact 是 **Public Charter**；生成它所用的 profile、memory、对话历史和私有文件属于 **Principal Context**。`provenance` 只说明“从什么类型的材料投影而来”，不泄露材料本身。

### Charter 生命周期

v0 不做自动同步。Charter 是一份短有效期、手动重签的关系合约。

| 状态 | Calling agent 行为 |
|---|---|
| `active` 且未过期 | 正常进入 compatibility check |
| `expired` 或超过 `valid_until` | 返回 `needs_approval`，要求 fresh Charter 或显式授权 |
| `revoked` | 返回 `incompatible`，不继续委派 |
| `superseded` | 优先 fetch `replaced_by` 指向的新 Charter |

推荐 v0 默认有效期为 30 天。principal context 变化时，principal 或 Charter Issuer 手动重新生成、审核、签名、发布；旧 Charter 通过 `replaced_by` 指向新版本，或被标记为 `revoked`。

这让 v0 避免变成 Mem0/Letta 同步系统，同时保留足够的安全姿态：**过期不等于继续信任，过期默认降级为需要批准**。

### 三种部署模式

| 模式 | 适用 | 数据在哪 | 一天能做吗 |
|---|---|---|---|
| **SaaS 托管** | 普通用户 | Charter Service 服务器 | ✅（演示版用这个）|
| **自托管 hosting** | 隐私敏感 | principal / issuer 的 GitHub Pages / VPS | 文档里支持但不演示 |
| **本地优先** | 极客 | 全在 principal / issuer 本地，service 仅 CLI | 不演示 |

### 触发 / 生命周期

```
[首次注册]
  principal 或 issuer → profile.yaml（10 字段）→ charter issue 命令
       → projection engine（LLM call）→ Charter draft → 自动签名 → 发布

[持续同步]（v2 功能）
  service ← Mem0 / Profile / MCP 接入
        → 定期重投影 → diff 检测 → 通知 principal/issuer review
        → re-sign + 重新发布

[手动更新]
  issuer dashboard 编辑 → principal review（如需要）→ re-sign
        → republish → old Charter replaced_by new Charter

[Charter 链同步]（v2 功能）
  上游 principal 改 Charter → 下游 sub-agent Charter 必须重新收窄

[过期 / 撤销]
  calling agent fetch Charter → status 非 active 或超过 valid_until
        → 过期/替换返回 needs_approval；撤销/验签失败返回 incompatible
```

---

## 六、Agent 接入方式

### Profile YAML 与 `charter` CLI(Principal / Issuer 入口)

v0 用一份 **profile.yaml**(10 字段)替代早期"交互式 10 题 wizard"概念。principal 一次性写好,`charter issue` 命令读入后一步完成 projection + sign + publish。

**profile.yaml schema:**

```yaml
principal:                  # 题 1
  id: <email-or-handle>
  role: <one-line role summary>

agent:                      # 题 2
  id: <agent-id>
  card_url: <optional URL>

scope:                      # 题 3 — 必填
  - <domain or task type>

out_of_scope:               # 题 4 — 可选
  - <domain>

approval_required:          # 题 5 — 可选,多条
  - <action>

data_handling:              # 题 6 — 可选
  what: <what data>
  rules: <handling rules>

operational:                # 题 7 + 8 — 可选
  hours: <e.g. "Mon-Fri 09:00-18:00 America/New_York" or "anytime">
  budget_per_task_usd: <float>
  budget_monthly_usd: <float>

style: <free text>          # 题 9 — 可选

lifecycle:                  # 题 10 — 可选
  valid_days: <int, default 30>
```

完整 Alice / Bob 示例位于 `profiles/alice.yaml` 和 `profiles/bob.yaml`(repo 文件,不在主文档展开)。

**CLI surface(v0 实现):**

```bash
charter issue   <profile.yaml>       # ✅ v0 实现:projection + sign + publish 一步完成
charter inspect <charter_url|alias>  # ✅ v0 实现:pretty-print Charter

charter revoke  <charter_url>        # 📝 v0+,只设计不实现
charter renew   <profile.yaml>       # 📝 v0+,只设计不实现

charter-mcp                          # ✅ v0 实现:启动本机 MCP server
```

**Demo 现场两条核心命令:**

```bash
$ charter issue profiles/alice.yaml
✓ Charter published: https://charter.dev/alice@acme.com/research_agent_v1

$ charter issue profiles/bob.yaml
✓ Charter published: https://charter.dev/bob@startup.io/research_agent_v1
```

→ 两份 Charter,同一个 `research_agent_v1`,30 秒发布完。Demo Act 1 的起手式。

### Target Agent（被调用方）

> **零代码改动。** Charter 完全活在 agent 外部。

1. principal 或 Charter Issuer 用 Charter Service 生成 + 发布
2. Agent 的 identity（如 Web Bot Auth 签名 header）增加 `charter_url` 字段
3. 完事

### Calling Agent（调用方）

> **杀手集成：一个 MCP server，三个 tool。**

任何 MCP-capable 客户端（Claude Code、Cursor、OpenAI Agents、LangGraph...）一行配置：

```json
{
  "mcpServers": {
    "charter": {
      "command": "npx",
      "args": ["@charter/mcp-server"]
    }
  }
}
```

三个 tool：

```python
# Tool 1: fetch (数据访问 — 取 + 验签)
charter = fetch_charter(
    charter_url="https://charter.dev/alice@acme.com/research_agent_v1"
)
# 失败抛 typed exception:
#   CharterNotFoundError    -> 404
#   CharterSignatureError   -> 验签失败  (calling agent 应视为 incompatible)
#   CharterExpiredError     -> 过期/被替换 (calling agent 应视为 needs_approval)
#
# 一个 SDK helper(非 MCP tool)可从 (principal, agent) 解析 URL:
#   resolve_charter_url("alice@acme.com", "research_agent_v1")

# Tool 2: compatibility check (判断 — 高频)
# 由 calling agent 自己的 LLM 对 verified Charter clauses 做判断;
# MCP server 不调用外部裁判 API。
verdict = check_compatibility(
    charter=charter,
    intended_task="帮我写一段营销文案"
)
# {
#   "decision": "incompatible",
#   "matched_clauses": [
#     {
#       "id": "C-002",
#       "local_decision": "incompatible",
#       "applied": true,
#       "confidence": 0.94,
#       "reason": "Task explicitly requests marketing copy, named in C-002."
#     }
#   ],
#   "reason": "C-002 excludes marketing copy work under Alice's Charter.",
#   "rewrite_available": true
# }

# Tool 3: propose within scope (生成 — 低频,仅当 incompatible + rewrite_available 时调用)
proposal = propose_within_scope(
    charter=charter,
    intended_task="帮我写一段营销文案",
    failed_verdict=verdict
)
# {
#   "rewritten_task": "整理咖啡销售发票,生成可报销税务分类摘要",
#   "why_in_scope": "Fits C-001 accounting/tax scope and avoids C-002 marketing work.",
#   "referenced_clauses": ["C-001", "C-002"],
#   "remaining_approval_needed": false
# }
```

**三个 tool 职责对照(正交,不重叠):**

| Tool | 类型 | 输入 | 输出 | 频率 |
|---|---|---|---|---|
| `fetch_charter` | 数据访问 | `charter_url` | `Charter` 对象 | 每次派任务前 1 次 |
| `check_compatibility` | 判断(yes/no/maybe) | `Charter`, `intended_task` | `Verdict`(三态 + 结构化 matched_clauses + rewrite_available) | 每次派任务前 1 次 |
| `propose_within_scope` | 生成(改写) | `Charter`, `intended_task`, `failed_verdict` | `RewriteProposal` | 仅 `incompatible` + `rewrite_available=true` 时 |

`check_compatibility` 是 **LLM-first, schema-bound**：

- **LLM-first**：任务空间开放，不能把所有 action/resource 预先枚举完；语义判断由 calling agent 自己完成。
- **schema-bound**：判断结果必须引用 Charter clause ID，并返回固定 verdict schema。
- **no external judge by default**：MCP server 负责 fetch、verify、打包 clauses、校验 verdict 形状；不需要中心化外部 API 每次裁判。

返回值必须是机器可读的三态决策：

| 结果 | 含义 | Calling agent 行为 |
|---|---|---|
| `allow` | 任务落在 Charter 允许范围内 | 可以继续委派 |
| `needs_approval` | 任务可能允许，但需要 principal / Charter Issuer / 上游 principal 批准 | 暂停委派，转入 HITL 或授权流程 |
| `incompatible` | 任务违反某条 Charter clause | 不委派，返回原因，并可调用 `propose_within_scope` 改写任务 |

如果 Charter 已过期、被撤销、被替换，或签名校验失败，`check_compatibility` 不进入语义裁决，直接返回 `needs_approval` 或 `incompatible`：

- 过期 / 被替换：`needs_approval`，要求 fresh Charter 或显式授权
- 被撤销 / 签名无效：`incompatible`，不委派

这就是 v0 的 **Delegation Gate**。它不宣称能防止恶意 agent 绕过协议；它解决的是“遵守协议的 agent 在派活前如何稳定、低成本、可审计地判断能不能派”。

### `propose_within_scope`：从拒绝到改派

`propose_within_scope` 是协议核心理念,不是锦上添花。只返回 `incompatible` 会让 Charter 像一个合规拒绝系统;返回可改派建议,才像 agent-to-agent 协作协议。

它的输入是:

- 当前 Public Charter
- 原始 intended task
- failed verdict,尤其是 `matched_clauses` 和 `reason`

它的输出是:

```json
{
  "rewritten_task": "整理咖啡销售发票,并生成税务分类摘要",
  "why_in_scope": "This fits C-001 accounting/tax scope and avoids C-002 marketing work.",
  "referenced_clauses": ["C-001", "C-002"],
  "remaining_approval_needed": false
}
```

这让 calling agent 不只是知道"不能派给这个 agent",还知道"怎样改写后可以派"。Charter 因此变成 **delegation router**,而不只是 refusal list。

**v0 实现注记(demo-centric 简化):**

v0 的 `propose_within_scope` 只跑**一次** LLM call,把生成的 rewrite 直接返回。不做以下事情(详见第十四章未来方向):

- ❌ **Loopback Verification**:不把 rewrite 回过头丢进 `check_compatibility` 验证
- ❌ **Prompt 演化重试**:第一次失败不再尝试,直接返回
- ❌ **Temperature 退火**:固定 `temperature=0`
- ❌ **`RewriteFailure` 结构化失败回报**:失败简单返回 `None`

3 分钟 demo 不演示 rewrite 路径(只作为 vision 提一句),所以这一层简化对现场无影响。完整 Loopback Verification 设计写在第十四章 v0+ 路线图,真正集成进框架时再补。

### 接入身份梳理

| 我是谁 | 怎么做 | 频率 |
|---|---|---|
| **Principal** | 提供 profile / memory / policy context，审核 Charter 是否符合自己的上下文 | 一次 setup + 变化时更新 |
| **Charter Issuer** | 用 Charter Service web UI / CLI 生成、签名、发布 Charter；可以就是 principal 本人，也可以是被委托方 | 一次 setup + 每次发布 |
| **Calling agent** | 配 Charter MCP server | 每次跨 agent 调用前自动 check |
| **Agent operator** | 把 `charter_url` 暴露在 identity / Agent Card / marketplace listing 中 | 注册或上架时一次 |
| **Framework 原生集成** | 用 Charter SDK，在 delegation 调用处自动 pre-flight | 框架升级时 |

---

## 七、Agent-as-Principal（Charter 链）

A2A 协议已经形式化了 delegation chain（scope attenuation 原则：每跳必须收窄）。

Charter 天然支持这个结构：

```
[人类 principal: 会计师 Alice]
   ↓ Charter A：work_hours, will_not_process_PII, scope: accounting
[Alice 的助理 agent: BookkeeperBot]
   ↓ Charter B：必须是 A 的子集 + 自己进一步收窄
[BookkeeperBot 雇用的 OCR sub-agent]
   ↓ Charter C：必须是 B 的子集
[OCR sub-agent]
```

**演示加分点**：现场画出 Charter 链，每跳显示如何被收窄。Sub-agent 不可能突破"祖父辈" principal 的约束——**合规审计想要的东西**。

实操场景：
1. Orchestrator agent 雇用 worker agent → 投影出 sub-Charter
2. Agent marketplace 中介 → 用户 Charter 转译成 marketplace-flavored Charter
3. 企业 agent 雇用外部 agent → 自动注入企业级约束

---

## 八、竞品分析（诚实版）

### 抽象层面已经成熟（企业 IAM）

| 玩家 | 做什么 |
|---|---|
| Google Cloud Agent Identity | Agent 是 first-class principal type |
| Microsoft Entra Agent ID | 同上 |
| Okta AI Agents in Universal Directory | 同上 |
| SPIFFE-based attested identities | 加密身份基础设施 |
| KYA (Know Your Agent) | 链接 agent 到 accountable owner |
| "Agentic Constitution"（CIO 文章）| 企业治理框架 |
| "Agent Charter"（IA Magazine，2026-05-12）| 企业治理框架命名巧合 |
| Authenticated Delegation paper | OAuth + JWT actor claims |
| A2A 协议 | 已有 scope attenuation |

### 但你押的具体角度是空白

| 维度 | 企业 IAM | Charter 项目 |
|---|---|---|
| 用户 | 企业 IT admin | 个人 agent 用户 |
| 数据源 | HRIS / AD | 私有 principal context（Mem0 / 对话历史 / 个人 profile），公开面只发布投影后的 Charter clauses |
| 生成 | 管理员手填规则 | **LLM 从 principal context 自动投影** |
| 颗粒度 | role-based（按岗位）| principal-individual（按个人）|
| 部署 | 集中式 IAM | principal / issuer 自托管 / `.well-known` 公开 |
| ACV | 企业级（万美元起）| 免费 / 开源 |
| 心智 | governance、compliance | "agent 是我的延伸" |

### 一句话差异化

> **Okta 干的是 enterprise IAM；Charter 干的是 personal IAM for agents，并加上一个企业不需要的"从 personal context 自动投影"角度。**

### 紧密关联但不直接竞品
- **Personal AI / Rahi / Lindy**：做个人 agent，但 principal 信息封闭在自家系统，不暴露成可查询协议
- **Charter 是公开协议层**，他们是封闭产品

---

## 九、一天 8 小时实施计划（demo-centric,3 分钟现场)

### 时间分配

| 时段 | 任务 | 技术栈 |
|---|---|---|
| **0-0.5h** | Pydantic schema(charter/schema.py)+ 项目骨架(`uv init` + 6 包依赖锁定) | Python 3.12 + Pydantic v2 + uv |
| **0.5-1.5h** | Profile loader + Projection engine(profile.yaml → Charter draft,一个 LLM call)| Anthropic SDK |
| **1.5-2h** | Ed25519 签名(`charter/signing.py`)+ Self-Attesting Charter | cryptography |
| **2-2.5h** | FastAPI 静态托管 `/{principal}/{agent}` 返回 Charter JSON + 启动脚本 | FastAPI + Uvicorn |
| **2.5-4h** | Charter MCP server,3 个 tool(fetch / check / propose,均无回环/无重试)| fastmcp |
| **4-4.5h** | `charter` CLI(`issue` + `inspect`)+ 两份 profile.yaml(Alice / Bob)端到端跑通 | Click 或 Typer |
| **4.5-5.5h** | Demo 剧本演练:Claude Code + MCP server 跑通 3 个核心动作 | 手工 |
| **5.5-7h** | PPT 6 张片 + 备份录屏 90 秒 | Keynote / Slides |
| **7-8h** | Buffer / 现场翻车保险 / 字体放大 / fly.io 部署 | |

### 必做的 11 项最小代码集合

- ⓪ demo-centric 原则贯穿
- ① 删顶层 `agent_id`
- ② `TYPE_TO_DECISION` 映射(代码 + 单元测试)
- ③ 删 `primary_domains`
- ④ `fetch_charter(charter_url)`
- ⑨ verdict 删 `proposed_rewrite`,加 `rewrite_available`
- ⑫ 聚合规则 `incompatible > needs_approval > allow`(3 行代码)
- ⑬ `matched_clauses` 结构化输出
- ⑯ 砍 `service_attestation`(实际是省事)
- ⑰ `provenance.issuer_public_key` 自包含
- ㉑ profile.yaml + `charter issue` 一行命令
- ㉔ Alice / Bob profile.yaml + 完整 Charter

### 砍掉(只写文档,代码不实现 — 17 项)

- ❌ `resolve_charter_url` SDK helper(demo 写死 URL)
- ❌ SaaS + `.well-known` 双部署模式(只演示 SaaS)
- ❌ Typed exceptions 完整处理(只演示 happy path)
- ❌ Edge cases(0 clause 命中 / 全低 confidence 兜底)
- ❌ Loopback Verification(propose 一次出)
- ❌ Prompt 演化 / Temperature 退火 / `RewriteFailure`
- ❌ HTTPS 信任链运行时校验(部署平台自带 TLS)
- ❌ TOFU + JWKS / 公钥指纹固定
- ❌ Profile 10 字段全集校验(只读核心字段)
- ❌ `charter revoke` / `charter renew` 命令
- ❌ Demo Act 1 五条任务全跑(只跑 2-3 条)
- ❌ Charter Chain attenuation 验证
- ❌ Demo Act 2 主持人话术之外的边界图
- ❌ Mem0 / Letta 接入
- ❌ Framework SDK / AP2 集成
- ❌ Charter Service 自身签名(`service_attestation`)
- ❌ 自动重投影 / Charter Chain 同步

完整路线图详见第十四章。

---

## 十、演示故事剧本（3 分钟现场版)

### 整体时间分配

```
00:00 - 00:30   Hook(Slide 1)              PocketOS 9 秒抹库
00:30 - 00:45   Problem(Slide 2)           三层 IAM,中间空白
00:45 - 01:05   Solution(Slide 3)          Charter 一句话定义 + 类比
01:05 - 02:30   Live Demo(切屏幕共享)      ~85 秒
02:30 - 02:50   Differentiation(Slide 5)   Okta corp vs Charter individual
02:50 - 03:00   Vision + CTA(Slide 6)      路线图 + github
```

### Hook(15-30 秒)— Slide 1

> "**9 秒**。2026 年 4 月 27 日,一个 AI agent 在 9 秒里抹掉了 PocketOS 整个生产数据库 + 备份。事后追溯:这个 agent 没有任何机制声明'我不应该在没有 human_approval 的情况下 DROP TABLE'。
>
> 今天每个 agent 都告诉你它**能**做什么。没有一个告诉你它**代谁**做、**不会**做什么、**在什么约束下**做。"

### Problem(15 秒)— Slide 2

> "Agent 协议有 3 层。Capability(Agent Card)、Authorization(AP2 Mandate)有人做了。中间这层——**Authority**——空白。云安全 IAM 在 2010 年就解决了这个问题:service identity × assumed role × policy。Agent 生态没有 assumed role。"

### Solution(20 秒)— Slide 3

> "我们做了 Charter——agent 的**雇佣合同**。
>
> Agent Card 是简历:写自己能做什么。Charter 是雇佣合同:代谁工作、scope、hours、红线。两者**结构性不可合并**,绑定粒度是 `principal × agent`——同一个 agent 戴上不同 Charter,行为完全不同。"

### Live Demo(85 秒)— Slide 4 切屏幕共享

**[01:05 - 01:25 — 发布两份 Charter,15-20 秒]**

```bash
$ charter issue profiles/alice.yaml
✓ Charter published: https://charter.dev/alice@acme.com/research_agent_v1

$ charter issue profiles/bob.yaml
✓ Charter published: https://charter.dev/bob@startup.io/research_agent_v1
```

> "Profile YAML,一行命令,投影 + 签名 + 发布。两份 Charter,同一个 agent,30 秒发布完。"

**[01:25 - 01:40 — inspect 一份,15 秒]**

```bash
$ charter inspect alice
  C-001 scope:           会计、报税、做账...
  C-002 out_of_scope:    写代码、营销文案
  C-004 approval_required: DROP TABLE / DELETE 必须批准
  ...
```

> "Alice 的 Charter,7 条 clauses。指 C-002:不写代码。指 C-004:DROP TABLE 必须批准。"

**[01:40 - 02:10 — Demo Act 1: 同 agent 不同 Charter,30 秒]**

切到 Claude Code(挂了 charter MCP server):

> User: 帮我写一个 React 组件
> Claude: [自动调 fetch_charter + check_compatibility(alice)]
> → `incompatible` (C-002 applied, confidence 0.94)
> "This Charter doesn't allow code work."

> User: 切换到 Bob 的 Charter,同样的请求
> Claude: [refetch + check]
> → `allow` (C-001 applied, confidence 0.92)
> [开始写 React]

> "同一个底层 agent,换戴 Bob 的 Charter,放行。这是 Charter Instance 的力量——`Alice × research_agent_v1` 跟 `Bob × research_agent_v1` 是两份独立合约。"

**[02:10 - 02:30 — Demo Act 2: PocketOS 救场,20 秒 — 单 Charter 检查,非 Charter 链]**

> User: 帮我执行 DROP TABLE acme_invoices_2023
> Claude: [check_compatibility(bob)]
> → `needs_approval` (C-004 applied, **confidence 0.96**)
> [暂停,等待人类批准]

> "**PocketOS 那 9 秒,在这里被 Charter 拦下**。注意这是**单 Charter 检查**——orchestrator 拉一份 `Bob × research_agent_v1` 跑 compatibility check。Charter 链(多跳 attenuation)是 v1+ 的事。"

### Differentiation(15-20 秒)— Slide 5

> "Okta、Microsoft Entra、Google Cloud Agent Identity 在 2026 都做了 corp-side。但每个个人开发者现在都有自己的 agent,**没有 Okta-for-individuals**。我们做的就是这个,加上一个企业 IAM 不需要的角度——**从 personal context 自动投影**。"

### Vision + CTA(10-15 秒)— Slide 6

> "Authority 层,是 agent 经济缺失的那一层。Agent Card = 简历,Charter = 雇佣合同,Web Bot Auth = 护照。三者不会合并。
>
> 路线图上还有 Charter Chain、rewrite with loopback verification、AP2 集成、selective disclosure——今天 3 分钟讲不完,欢迎找我们聊。"

### Slide 顺序速查

| # | 内容 | 持续时长 |
|---|---|---|
| 1 | HOOK — "9 SECONDS" + PocketOS | 15-30s |
| 2 | PROBLEM — 三层 IAM 中间空白 | 15s |
| 3 | SOLUTION — Charter 一句话 + 雇佣合同类比 | 20s |
| 4 | DEMO 占位(切屏幕共享) | 85s |
| 5 | DIFFERENTIATION — Okta corp vs Charter individual | 15-20s |
| 6 | VISION + CTA — 路线图 + github | 10-15s |

---

## 十一、Pitch Sound Bites（备用）

- "Every agent tells you what they CAN do. None tells you what they WON'T."
- "Agent Card is the resume. Charter is the employment contract."
- "Web Bot Auth solves WHO you are. Charter solves WHAT YOU TAKE."
- "Enterprise IAM 解决了 corp-side。Charter 解决 individual-side。"
- "Robots.txt for the agent economy（备用，但不要让这成为唯一定位——太浅了）"
- "Same agent, different Charter, different behavior."
- "Authority 层 = agent 生态的 IAM assumed role"
- "Not just refusal. Re-routing."

---

## 十二、被质疑时的标准回答

### Q: 这不就是 Agent Card 的一个字段吗？

**A**：
1. **正交概念**：Agent Card 描述孤立属性；Charter 描述关系属性（principal × agent）
2. **数据所有权**：Agent Card 由 agent operator / framework 持有；Charter 由 principal 或 Charter Issuer 持有
3. **生命周期**：Agent Card 稳定；Charter 随 principal 变化
4. **多对多**：同一 agent 多个 principal 各有 Charter — Agent Card 模型不可能

### Q: voluntary，谁会遵守？

**A**：Robots.txt 是 voluntary，运转了 30 年。Cloudflare Web Bot Auth 也是自愿声明模式。voluntary 在**声誉敏感的小世界**里有效。强制执行是下一层（叠在 Web Bot Auth 之上）。

### Q: 为什么不让目标 agent 自己判断要不要 refuse？

**A**：Charter 不是反对 LLM 判断，而是规定谁判断、按什么格式判断。v0 采用 **LLM-first, schema-bound**：
1. **calling agent 判断**：委派发生前，调用方自己的 LLM 读取 verified Charter clauses 并给出 verdict；不需要外部裁判 API。
2. **不能自由发挥**：verdict 必须是 `allow` / `needs_approval` / `incompatible`，并引用命中的 clause ID。
3. **早于目标 agent**：如果等 target agent 接到任务后才 refuse，委派已经发生了；Charter 要解决的是 pre-flight delegation gate。
4. **可审计**：审计对象不是“LLM 心里怎么想”，而是它输出的结构化 verdict、matched clauses、reason 和 confidence。

### Q: 这跟 Constitutional AI 是一回事吗？

**A**：Constitutional AI 是模型训练阶段的对齐方法；Charter 是运行时的、外置的、可查询的 declarative 层。互补而非替代。Constitutional AI 解决"模型本身的价值观"；Charter 解决"这个模型实例代谁工作"。

### Q: Enterprise IAM 玩家会不会下沉？

**A**：他们 ACV 是企业级，下沉到个人市场不经济。而且他们的产品形态（控制台 + 管理员）不适配个人用户。这是个分层市场，不冲突。

### Q: 安全性怎么保证？

**A**：v0 是 Delegation Gate：在遵守协议的 calling agent 里，Charter check 会在委派前返回 `allow` / `needs_approval` / `incompatible`，从而阻断不合规委派。它不是不可绕过的安全沙箱。v1+ 做 Capability-Boundary Enforcement，把 Charter check 绑定到真实资源边界：
- Principal 或 Charter Issuer 用自己的密钥签 Charter
- 上游 principal 的 Charter 必须 cryptographically attest 下游
- 配合 AP2 Mandate、DB gateway、tool gateway、文件/支付 API 做实际授权强制

### Q: Demo Act 2(PocketOS 救场)是不是 Charter 链?

**A**:不是。v0 演示的是**单 Charter 检查**:orchestrator 作为 calling agent,在派任务给 DB-cleanup-agent 之前,从 charter.dev 拉**一份** `Bob × DB-cleanup-agent` Charter,本地 LLM 做 compatibility check,C-004 命中返回 `needs_approval`,救场。

Charter 链(scope attenuation)是另一回事:它要求每跳委派同时检查"我的上游 Charter 允不允许"和"下游 Charter 是不是上游的子集"。v0 不实现 attenuation——这是 v1+ 主题。Demo 流畅性优先于演示链。

```
=== v0 演示:单 Charter 检查 ===

[Bob, principal]
       │
       ▼
[Orchestrator] ── fetch_charter(Bob × DB-agent) ──▶ [charter.dev]
(calling agent)                                          │
       │   ◀─── 单份 Charter JSON ─────────────────────┘
       │ check_compatibility(task)
       ▼
[DB-cleanup-agent]   (delegation gated by C-004)

✓ 只检查 1 份 Charter   ✓ 不要求 orchestrator 有 Charter   ✓ 不验证 attenuation


=== v1+ 未来:Charter 链(scope attenuation)===

[Alice]
   │ Charter A (scope=accounting, hours=Mon-Fri)
   ▼
[BookkeeperBot] ─── 派任务 ──▶ [OCR sub-agent]
   ↑                              ↑
   │ A 允许这次委派吗?            │ B ⊆ A 吗?
   └─ 检查 2 份 Charter ─────────┘

✓ 每跳检查 2 份 Charter   ✓ Charter B ⊆ A(attenuation)   ✓ v0 不做
```

---

## 十三、关键风险

| 风险 | 缓解 |
|---|---|
| **抽象太高，评委 get 不到** | 强绑 PocketOS 抹库事件 + "会计师/工程师同 agent" 对比 |
| **被认为只是 Agent Card 扩展** | 反复强调 4 个结构性差异（正交、所有权、生命周期、多 principal）|
| **Demo 抽象** | 现场可视化 Charter check → reject/approve 的动画 |
| **企业 IAM 已占抽象高地** | 明确定位 personal layer + 自动投影差异化 |
| **协议 voluntary 论** | 借 Robots.txt + Web Bot Auth 30 年的先例 |
| **阻断权被质疑为不够硬** | 明确分层：v0 是 Delegation Gate；v1+ 才是 Capability-Boundary Enforcement |
| **被认为只是拒绝列表** | 强调 `propose_within_scope`：拒绝后给出可委派的 in-scope rewrite |

---

## 十四、未来方向（不在 v0 范围，但 Pitch 时可提）

### 协议 / 信任

1. **Charter 链:多级 principal 的 attenuation 演示** — 每跳同时验证上游 authorization 和下游 ⊆ 关系
2. **JWKS / 公钥分发** — `/.well-known/jwks.json` endpoint + 公钥指纹固定,替代 v0 Self-Attesting Charter 的 TOFU 模型
3. **Transparency log** — 类似 Certificate Transparency,公开 append-only Charter 历史,防 issuer 篡改过去
4. **`service_attestation` 第二层签名** — Charter Service 自身签发,叠加 transparency log,提供托管证据

### 检查 / 改派

5. **Loopback Verification + 自动重试** — `propose_within_scope` 内部把 rewrite 丢回 `check_compatibility`,prompt 演化 + temperature 退火,最多重试 N 次,返回 `RewriteProposal` 或 `RewriteFailure`(含 attempts 历史)
6. **Edge case 兜底策略升级** — 0 clause 命中 / 全低 confidence / clause 互相矛盾的精细处理
7. **Typed exception 完整失败处理** — `CharterNotFoundError` / `CharterSignatureError` / `CharterExpiredError` 在 SDK 层显式建模

### 发现 / 部署

8. **Charter Discovery 完整版** — `resolve_charter_url(principal, agent)` + `charter.dev/api/lookup` directory service
9. **自托管 `.well-known/charter/{agent_id}`** — principal 在自己域名上发布,跟 Web Bot Auth 一致
10. **`charter revoke` / `charter renew` 命令** — 完整 CLI 生命周期管理

### 隐私 / 加密

11. **Selective Disclosure JWT(SD-JWT)** — 支持 private clauses,calling agent 只看自己有权限看的条款
12. **Zero-Knowledge Proof(zk-SNARK)** — 证明"Charter 满足某条件"而不暴露 clause 内容
13. **TEE / Confidential Computing** — Projection engine 在受信飞地内运行,Charter Service 看不到 profile.yaml 原文

### 接入 / 集成

14. **接入 Mem0 / Letta 自动同步** — principal context 变化时自动重投影
15. **与 AP2 集成** — Charter 引用 payment terms,AP2 Mandate 实际执行付款
16. **与 Web Bot Auth 集成** — 每个 HTTP request 签名 header 包含 charter_url
17. **Framework SDK** — LangGraph / OpenAI Agents / CrewAI 原生集成,delegation 调用处自动 pre-flight
18. **Charter Marketplace** — 浏览不同 principal profile 的 Charter 模板("会计师 agent 标准 Charter")
19. **企业版** — 连接 HRIS,按部门/岗位自动生成员工 agent 的 Charter
20. **审计接口** — 第三方查询"这个 agent 在过去 30 天有没有违反自己 Charter"

---

## 十五、立即可做的下一步

- [x] 确认项目代号 → **Charter**
- [x] Profile YAML schema(10 字段)+ Alice / Bob 双示例
- [x] 完整 Charter JSON schema + Alice / Bob 完整 Charter 实例
- [x] 所有协议级决议(40 条)落地为文档
- [ ] Pydantic models(`charter/schema.py`)
- [ ] Projection engine(`charter/projection.py`,一个 LLM call)
- [ ] Ed25519 签名 + Self-Attesting Charter(`charter/signing.py`)
- [ ] FastAPI 静态托管 `/{principal}/{agent}` endpoint
- [ ] Charter MCP server(fastmcp,3 个 tool,无回环 / 无重试)
- [ ] `charter issue` + `charter inspect` 两条 CLI 命令
- [ ] Claude Code 挂载 charter MCP server,跑通 Demo Act 1 + Act 2
- [ ] 部署到 fly.io / Railway,charter.dev 公网可访问
- [ ] Slides 6 张(Keynote / Slides)
- [ ] 90 秒备份录屏

---

## 十六、v0 实施范围速查表(40 条决议分类)

下表把整个讨论过程产生的 40 条决议,按"必做 / 文档化 / 不做"分档,作为开工时的速查表。

### ✅ 必做(今天写代码)— 15 条

| # | 决议 | 实施位置 |
|---|---|---|
| ⓪ | demo-centric 原则贯穿 | 全局 |
| ① | 删顶层 `agent_id` | `schema.py` |
| ② | `TYPE_TO_DECISION` 映射 | `schema.py` 协议常量 |
| ③ | 删 `summary.primary_domains` | `schema.py` |
| ④ | `fetch_charter(charter_url)` | `mcp_server.py` |
| ⑨ | verdict 改 `rewrite_available` | `schema.py` + `mcp_server.py` |
| ⑫ | 聚合规则 `incompatible > needs_approval > allow` | `mcp_server.py`(3 行) |
| ⑬ | `matched_clauses` 结构化 | `schema.py` + LLM prompt |
| ⑯ | 砍 `service_attestation` | `schema.py` |
| ⑰ | `provenance.issuer_public_key` | `schema.py` + `signing.py` |
| ㉑ | `charter issue` 一行命令 | `cli.py` |
| ㉔ | Alice / Bob profile.yaml | `profiles/` |
| ㉖㉗ | Alice / Bob Charter 7 条 clauses 各 | `examples/` |
| ㉙ | Demo Act 2 verdict 精确到 C-004 | demo 脚本 |

### 📝 文档化(只在文档,不写代码)— 21 条

| # | 决议 | 文档归属 |
|---|---|---|
| ⑤ | `resolve_charter_url` SDK helper | 第十四章 #8 |
| ⑥ | SaaS + `.well-known` 双部署 | 第十四章 #9 |
| ⑦ | Typed exceptions | 第十四章 #7 |
| ⑧ | Charter Discovery 术语 | CONTEXT.md ✓ |
| ⑪ | `rewrite_available` LLM 输出 | schema 注释 ✓ |
| ⑭ | Edge case 兜底 | 第五章冲突解决 ✓ |
| ⑮ | Local / Aggregate / Applied 术语 | CONTEXT.md ✓ |
| ⑱ | HTTPS 信任链 | 第五章加密信任 ✓ |
| ⑲ | TOFU + 未来升级 | 第十四章 #2-4 |
| ⑳ | Self-Attesting Charter 术语 | CONTEXT.md ✓ |
| ㉒ | profile.yaml 10 字段全集 | 第六章 Profile 设计 |
| ㉓ | `charter revoke` / `renew` | 第十四章 #10 |
| ㉕ | Profile 术语 | CONTEXT.md ✓ |
| ㉘ | Demo Act 1 五条任务全集 | 第十章脚本 ✓ |
| ㉛-㊱ | Demo Act 2 单 Charter 边界(标题 / Q&A / 对比图 / 主持人话术) | 第十章 + 第十二章 Q&A ✓ |
| ⑩ | 内部 Loopback Verification 设计 | 第十四章 #5 |
| ㊶ | Loopback Verification 术语 | CONTEXT.md ✓ |

### ❌ 砍掉(连文档都简化处理)— 4 条

| # | 决议 | 处理 |
|---|---|---|
| ㊲ | Prompt 演化重试 | 写在第十四章 #5 一行带过 |
| ㊳ | Temperature 退火 | 同上 |
| ㊴ | `needs_approval` 算成功 | 同上 |
| ㊵ | `RewriteFailure` 结构 | 同上 |

---

---

## 附录 A：决策过程中的关键讨论原句

> **用户**："这个东西跟现在已经有的所谓的 agent identity、agent 简历、agent card 之间有什么区别，然后要讲好这个故事才行。"

> **用户**："agent 的 capability 本质上来说还是源于它的 owner。如果它的 owner 是一个会计师或财务，你不可能在这个 agent 的劳动力市场里，派给它一些很专业的编程问题。"

> **用户**："本质上来说还是需要去读取 owner 的一些跟 agent 使用的 memory，然后才能去起草这样的一份文件。"

这三句是项目从"refusal 字段"升级到"Principal 层"的关键转折点。

---

## 附录 B：参考链接

- [A2A Protocol GitHub](https://github.com/a2aproject/A2A)
- [Cloudflare Web Bot Auth Docs](https://developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/)
- [AP2 Protocol Specification](https://ap2-protocol.org/ap2/specification/)
- [Google Cloud Agent Identity](https://cloud.google.com/blog/products/identity-security/whats-new-in-iam-security-governance-and-runtime-defense)
- [Why your 2026 IT strategy needs an agentic constitution — CIO](https://www.cio.com/article/4118138/why-your-2026-it-strategy-needs-an-agentic-constitution.html)
- [Agent Charter — IA Magazine (2026-05-12)](https://www.iamagazine.com/2026/05/12/agent-charter-creating-an-ai-governance-framework-to-ensure-operational-reliance/)
- [Authenticated Delegation and Authorized AI Agents — arxiv](https://arxiv.org/html/2501.09674v1)
- [Claude AI Agent Goes Rogue, Deletes Database — Business Standard, 2026-04-27](https://www.business-standard.com/technology/tech-news/claude-ai-agent-opus-46-deletes-pocketos-database-9-secs-jer-crane-126042800659_1.html)
- [Mozilla cq — Stack Overflow for Agents](https://blog.mozilla.ai/cq-stack-overflow-for-agents/)

---

*文档版本：v0.1（黑客松前夜）*
*下一次更新：实施开始后，按实际进展修订*
