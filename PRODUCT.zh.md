# Charter（中文版）

> **Agent 经济里的 Authority 层。** 一份签名过的、可查询的"工作合同"
> ——告诉调用方:在某个 principal 上下文下,这个底层 agent 被允许做
> 什么、愿意做什么、必须拒绝什么。**派任务之前就能查清楚。**

这是 Charter 项目的中文产品文档,内容跟英文版 [`PRODUCT.md`](PRODUCT.md)
保持同步。装机说明看 [`README.md`](README.md);术语定义看
[`CONTEXT.md`](CONTEXT.md);具体迭代计划看
[`ROADMAP.md`](ROADMAP.md)。

> **语言版本**:英文版是 [`PRODUCT.md`](PRODUCT.md),作为对外的权威版本。
> 本中文版供项目作者自查使用,翻译可能滞后 1-2 个版本;以英文版为准。

---

## 目录

- [1. Charter 是什么](#1-charter-是什么)
- [2. 为什么需要它](#2-为什么需要它)
- [3. 三个角色](#3-三个角色)
- [4. 协议本身](#4-协议本身)
  - [4.1 Charter JSON Schema](#41-charter-json-schema)
  - [4.2 Clause 类型与 Local Decision](#42-clause-类型与-local-decision)
  - [4.3 聚合规则](#43-聚合规则)
  - [4.4 生命周期](#44-生命周期)
  - [4.5 Self-Attesting 签名模型](#45-self-attesting-签名模型)
  - [4.6 Public Charter vs. Principal Context](#46-public-charter-vs-principal-context)
  - [4.7 MCP 工具表](#47-mcp-工具表)
  - [4.8 Charter Discovery](#48-charter-discovery)
  - [4.9 Charter Chain 衰减](#49-charter-chain-衰减)
- [5. 设计 Rationale](#5-设计-rationale)
- [6. 反目标:Charter 不是什么](#6-反目标charter-不是什么)
- [7. 当前进展](#7-当前进展)
  - [7.1 v0.5 — Project Hygiene](#71-v05--project-hygiene)
  - [7.2 v0.6 — 协议补全](#72-v06--协议补全)
  - [7.3 v0.7 — Charter Chain + 框架适配 + 公网部署](#73-v07--charter-chain--框架适配--公网部署)
- [8. Roadmap](#8-roadmap)
  - [8.1 v0.8 — 信任模型升级(规划中)](#81-v08--信任模型升级规划中)
  - [8.2 v0.8 之外(延后 backlog)](#82-v08-之外延后-backlog)
- [9. 参考链接](#9-参考链接)

---

## 1. Charter 是什么

Charter 是一份签名 JSON 合同,声明:**"这个具体的 agent,代表这个具体的
principal 工作时,被允许做这些事、拒绝那些事,并且持续受到这些约束。"**

它填补了现有 agent 协议里**结构性空白**的中间一层:

| 层 | 回答的问题 | 现有协议 |
|---|---|---|
| **Capability(能力)** | 这个 agent 技术上能做什么? | Agent Card(A2A) |
| **Authority(授权)** | 这个 agent 代谁工作,被允许做什么? | **Charter** |
| **Authorization(具体批准)** | 这次具体任务被批准了吗? | AP2 Mandate |

没有 Authority 这一层,agent 生态只有"能力"和"单次授权",中间没有
"持续的关系性约束"。结果是:调用方要么盲目信任目标 agent(不安全)、
要么每件事都问用户(不可用)。

**Charter 的绑定粒度是 `principal × agent` 关系对。** 同一个底层
agent,服务 Alice 和 Bob 时是两份独立 Charter——scope、out_of_scope、
工时、预算、上报方式都可以完全不同。调用方派任务前,根据当前要派的
那个关系对去取对应的 Charter,跑一遍 Compatibility Check 再决定派不
派。

---

## 2. 为什么需要它

云端 IAM 十年前就把同样的三层关系理清楚了。Agent 生态正在重建栈的
最下面(Agent Card)和最上面(AP2 Mandate),中间这层却始终空着。

具体的失败模式:一个技术上能 `DROP TABLE production` 的 agent,**没有
任何协议级的方式去声明** "……但我代表 Alice 这个会计师工作的时候不
能这么做" —— 这条约束如果存在,只能埋在 agent operator 的运行时里,
对调用方不可见、不可查、不可被第三方独立验证。

Charter 就是这条"可签名、可拉取、可被第三方验证"的声明。

为什么不能把它塞进 Agent Card?为什么不是 Constitutional AI 能解决
的?为什么自愿协议也能 work?完整的结构性论证在
[§5 设计 Rationale](#5-设计-rationale)。

---

## 3. 三个角色

| 角色 | 定义 |
|---|---|
| **Principal(委托方)** | 这个 agent 替谁工作。可以是人、组织、或者上游 agent。提供决定 scope / refusal / 工时 / 上报方式的上下文。 |
| **Charter Issuer(签发方)** | 创建、审核、签名、发布 Charter 的人/服务。可以是 principal 本人,也可以是被委托的服务,或企业管理员,或上游 agent。 |
| **Agent Operator(运行方)** | 跑底层 agent 实现、发布能力元数据(Agent Card)的人。同一个 agent 可以同时被多个 principal 用,每个对应一份 Charter。 |

非正式术语 **"owner"** 故意不用——它会把上面这三个角色混在一起,
而 Charter 协议需要它们保持分离。

---

## 4. 协议本身

这一节是给实现者看的。要做一份兼容 Charter 协议的实现,这里写的就是
全部内容。

### 4.1 Charter JSON Schema

一份 Public Charter 是一个单独的 JSON 文档:

```json
{
  "version": "0.1",
  "charter_id": "charter:<principal_id>:<agent_id>:<issued_date>",

  "binding": {
    "type": "principal_agent",
    "principal_id": "string",
    "agent_id": "string"
  },

  "principal": {
    "type": "human | organization | agent",
    "id": "string",
    "role_summary": "string"
  },

  "issuer": {
    "type": "human | organization | service | agent",
    "id": "string",
    "relationship_to_principal": "self | delegated | admin | upstream"
  },

  "agent_operator": {
    "type": "service | individual",
    "id": "string",
    "agent_card_url": "string | null"
  },

  "principal_chain": [],

  "visibility": {
    "charter": "public",
    "raw_principal_context": "private",
    "private_clauses": "not_supported_in_v0"
  },

  "summary": {
    "plain_language": "string"
  },

  "clauses": [
    {
      "id": "string (例如 C-001)",
      "type": "scope | out_of_scope | approval_required | operational_limit | style | data_handling",
      "text": "string"
    }
  ],

  "decision_schema": {
    "decision": "allow | needs_approval | incompatible",
    "matched_clauses": [
      {
        "id": "string",
        "local_decision": "allow | needs_approval | incompatible",
        "applied": "bool",
        "confidence": "float in [0, 1]",
        "reason": "string",
        "source_charter_id": "string | null"
      }
    ],
    "reason": "string",
    "rewrite_available": "bool"
  },

  "lifecycle": {
    "issued_at": "ISO-8601 时间戳",
    "valid_until": "ISO-8601 时间戳",
    "status": "active | expired | revoked | superseded",
    "revoked_at": "ISO-8601 时间戳 | null",
    "replaces": "charter_id | null",
    "replaced_by": "charter_id | null"
  },

  "provenance": {
    "issuer_public_key": "ed25519:<base64-DER-公钥>",
    "issuer_signature": "ed25519:<base64-签名>",
    "source_commitments": [
      {
        "type": "string (例如 profile_yaml)",
        "description": "string",
        "content_hash": "sha256:<hex>"
      }
    ],
    "generated_at": "ISO-8601 时间戳"
  },

  "parent_charter_url": "string | null",
  "attenuation_proof": {
    "parent_charter_id": "string",
    "attenuates": { "<child_clause_id>": ["<parent_clause_id>", ...] }
  }
}
```

**字段说明:**

- `charter_id` 是唯一规范标识,每个 Charter 实例独有。
- `binding.agent_id` 是绑定 agent 的唯一权威字段。**没有顶层 `agent_id`**。
- `principal_chain` 是为 Charter Chain 保留的字段;v0.1 永远是空数组。
- `visibility.private_clauses` 必须是 `"not_supported_in_v0"`。Selective disclosure 是后续版本的事。
- `summary.plain_language` 只是给人看的说明,不参与决策聚合。
- `decision_schema` 是描述 verdict **形状**的元数据,不是 verdict 本身。
- `provenance.source_commitments[].content_hash` 是对源材料(通常是 Profile YAML)的 opaque commitment。**原始源材料绝不发布在 Public Charter 里。**
- `parent_charter_url` + `attenuation_proof` 只在 Charter Chain 的子节点上有值(v0.7+);根 Charter 两个字段都是 `null`。
- `matched_clauses` 里的 `source_charter_id` 只由 `aggregate_verdict_chain`(v0.7+)填充,让调用方看清是链上哪一份 Charter 触发了最终决策。单 Charter 聚合时这个字段是 `null`。

### 4.2 Clause 类型与 Local Decision

每条 clause 都有一个 `type`,取值来自一个**闭合的小集合**。协议把
"clause type → local decision(本条 clause 对本次任务的贡献)"
这一映射写死成一个常量:

```python
TYPE_TO_DECISION = {
    "scope":             "allow",
    "out_of_scope":      "incompatible",
    "approval_required": "needs_approval",
    "operational_limit": "needs_approval",
    "style":             "allow",
    "data_handling":     "needs_approval",
}
```

| `clause.type` | local decision | 含义 |
|---|---|---|
| `scope` | `allow` | 本 principal 下,这个 agent 被正式 charter 去做的事 |
| `out_of_scope` | `incompatible` | 本 principal 下,这个 agent 被明确排除的事 |
| `approval_required` | `needs_approval` | 允许做,但每次/每会话需要 principal 显式批准 |
| `operational_limit` | `needs_approval` | 工时 / 预算 / 频率 / 地理等约束;越界需批准 |
| `style` | `allow` | 风格软约束(格式、语言、是否引用来源) |
| `data_handling` | `needs_approval` | 敏感数据类别;接触前需批准 |

**LLM 负责模糊判断**("这条 clause 是否被这次任务命中?");
**协议负责确定性判断**("已知命中,verdict 是什么?")。
实现**不能**让 LLM 自由生成 local decision——它只能由
`clause.type` 机械映射得到。

### 4.3 聚合规则

Compatibility Check 的 verdict,是把所有被 LLM 标为命中(且
`confidence >= 0.5`)的 clause 的 local decision 聚合得到。

严格优先级:

```
incompatible  >  needs_approval  >  allow
```

```python
def aggregate(local_decisions: list[str]) -> str:
    if "incompatible" in local_decisions:
        return "incompatible"
    if "needs_approval" in local_decisions:
        return "needs_approval"
    return "allow"
```

单次扫描、确定性、单调、可单元测试。**任何一条 clause 说"停"就停**
——Charter 是边界声明,不是 allow-list。叠加约束语义上是 AND,不是 OR。

**Fallback(兜底)规则:**

| 情况 | 聚合决策 |
|---|---|
| 没有任何 clause 命中 | `needs_approval`(保守默认 —— Charter 是闭世界约束) |
| 所有命中 clause 的 confidence 都 < 0.5 | `needs_approval`(LLM 信心不够,降级) |
| 生命周期是 `expired` 或 `superseded` | `needs_approval`,要求换新 Charter |
| 生命周期是 `revoked` 或签名验证失败 | `incompatible`,不要派 |

**Applied Clause**:verdict 的 `matched_clauses` 数组里,每条都带
`applied: true | false` 标记。一条 clause 是 applied,当它的
local decision 跟最终聚合决策一致。可以同时有多条 applied=true。
这让 verdict **直接可审计**——调用方能追溯是哪条 clause 导致了
哪个结果。

### 4.4 生命周期

| 状态 | 调用方应该怎么做 |
|---|---|
| `active` 且当前时间 `<= valid_until` | 正常跑 Compatibility Check |
| `expired` 或当前时间 `> valid_until` | 返回 `needs_approval`,要求换新 Charter |
| `revoked` | 返回 `incompatible`,不要派 |
| `superseded` | 去取 `lifecycle.replaced_by` 指向的 Charter,重新跑检查 |

v0 用**手动重签 + 短有效期**(默认 30 天)。没有自动重投影。
Principal context 变了,签发方要重新生成、审核、签名、发布。旧
Charter 要么标记成 `superseded`(`replaced_by` 指向新的
`charter_id`),要么 `revoked`。

服务器**可以**返回 expired Charter,只要 `lifecycle.status` 和
`valid_until` 字段如实反映状态。降级由调用方的 gate 来执行。

### 4.5 Self-Attesting 签名模型

v0 用 **Self-Attesting Charter**:Charter 自己的
`provenance.issuer_public_key` 字段就携带验签需要的公钥。调用方一次
HTTPS GET 拿到 Charter,本地验签完成,**不需要再 fetch JWKS、不需要
DID 解析、不需要外部 PKI**。

**信任链:**

```
HTTPS(TLS / CA 体系)
    ↓ 信任 Charter 所在域名
服务器返回 Charter JSON,内含 issuer_public_key + issuer_signature
    ↓
调用方用 issuer_public_key 验证 issuer_signature
```

**签名覆盖范围**:`issuer_signature` 字段以外的整个 Charter JSON。
实现流程:

1. 构造 Charter,`issuer_signature` 字段留空(或不存在)。
2. 规范化:UTF-8 JSON,key 排序,数字格式稳定。
3. 用签发方的 Ed25519 私钥签字节。
4. 把签名结果写回 `provenance.issuer_signature`,格式
   `ed25519:<base64>`。

**算法**:v0 只支持 Ed25519。

**静态私钥加密**(v0.6+):设了 `CHARTER_KEY_PASSPHRASE` 就用
`BestAvailableEncryption` 加密;没设就明文 PEM + 每次写时一条响亮
的 WARN 日志。v0 时代的明文 key 仍然能加载(向后兼容)。

**v0 信任模型的已知短板**(v0.8 会补,见 [§8.1](#81-v08--信任模型升级规划中)):

- **首次拉取的 TOFU 问题**:调用方第一次见到一个 `charter_url`,
  没法独立验证内嵌的公钥真的属于声明的 principal。信任落在 HTTPS 上。
- **密钥轮换发现机制弱**:换了新公钥,新 Charter 自带新公钥,
  旧调用方没有"指纹固定"机制能感知到换了。
- **不防服务器被攻破**:服务器被入侵,攻击者可以签发任意 Charter。

### 4.6 Public Charter vs. Principal Context

Charter 是**公开的工作合同**。生成它所用的素材**不是**。

| 内容 | 是否公开 | 原因 |
|---|---|---|
| `charter_id`, `binding` | 是 | 让调用方确认查的是哪一份关系合同 |
| `principal` 最小身份 | 是 | 足够解释 authority 的身份摘要 |
| `issuer`, `agent_operator` | 是 | 让调用方知道谁签的、谁运行底层 agent |
| `clauses[]`, `decision_schema` | 是 | Compatibility Check 的核心输入 |
| `lifecycle`、签名、公钥 | 是 | 验证有效性和完整性必需 |
| `provenance.source_commitments[]` (type + hash + 说明) | 是 | 证明来源 *存在* 但不暴露内容 |
| 原始 memory / 对话历史 / 完整 profile | **否** | Principal Context — 绝不放到公开 artifact 上 |
| 原始源文件(CV / 公司内部政策 / 等等) | **否** | 只公开 commitment(type + hash + summary) |
| 私有 clause | **v0 不支持** | 调用方读不到的 clause,没法稳定判断。Selective disclosure 延后 |

Profile YAML 被当作 Principal Context。**只有 SHA-256 commitment**
出现在 `provenance.source_commitments` 里,**原始 YAML 不会持久化**
或者作为 Public Charter 的附属一起发布。

### 4.7 MCP 工具表

Charter MCP 服务暴露一个小而正交的工具集合。本仓库的参考实现(到
v0.7)一共有 **10 个工具**:

| # | 工具 | 内含 LLM 调用 | 用途 |
|---|---|---|---|
| 1 | `fetch_charter(charter_url)` | 0 | 拉 + 验签,返回 Charter + 协议提示 |
| 2 | `aggregate_verdict(charter, hits)` | 0 | 把 per-clause 判断聚合成 Verdict |
| 3 | `delegate_task(principal, agent, task)` | 0 | 调用方 → 把任务信封写进 inbox |
| 4 | `check_inbox()` | 0 | worker agent → 读最新待处理任务 |
| 5 | `send_result(task_id, verdict, ...)` | 0 | worker agent → 把回复写进 outbox |
| 6 | `read_outbox()` | 0 | 调用方 → 读 worker 的回复 |
| 7 | `propose_within_scope(url, task, failed_verdict)` | 1 | 一次性改写 incompatible 的任务 |
| 8 | `propose_within_scope_verified(url, task, failed_verdict, max_attempts=3)` | 最多 2N | Loopback 验证版改写:温度退火 + 反馈重试 |
| 9 | `fetch_charter_chain(charter_url, max_depth=5)` | 0 | 沿 `parent_charter_url` 走到根,逐跳验签 + 验证 attenuation。返回根在前 |
| 10 | `aggregate_verdict_chain(chain, hits_per_charter)` | 0 | 跨链聚合所有命中 clause。最严格的那个 Charter 胜出 |

**设计原则**:MCP 服务**默认不调用 LLM**。调用方自己的 LLM 做模糊
的 clause-hit 判断;服务器做确定性聚合。**例外**只有
`propose_within_scope`(一次 LLM 调用)和
`propose_within_scope_verified`(最多 2N 次 LLM 调用 —— 全套工具里
唯一会做多次 LLM 调用的,文档里明确标出)。

**Typed errors**(由 `_fetch_and_verify` 抛出,经 MCP 层透出):

| 异常 | 何时抛 | 调用方该怎么响应 |
|---|---|---|
| `CharterNotFoundError` | HTTP 404 或无法访问 | 保守上报 |
| `CharterSchemaError` | body 不是合法的 Charter | 拒用 + 报告 |
| `CharterSignatureError` | 签名验证失败 | `incompatible`,不要派 |
| `CharterRevokedError` | `lifecycle.status == "revoked"` | `incompatible` |
| `CharterExpiredError` | `lifecycle.status in {"expired", "superseded"}` | `needs_approval` |

### 4.8 Charter Discovery

两种互补的 URL 形式;v0.5+ 同时支持。

**SaaS 托管(默认):**

```
{base}/{principal_id}/{agent_id}
```

例:`https://charter.dev/alice@acme.com/research_agent_v1`。可选的
`{base}/api/lookup?principal_id=...&agent_id=...` 端点返回标准
`charter_url`,支持自定义路径布局的部署。

**自托管 `.well-known`(沿用 Web Bot Auth 模式):**

```
https://{principal_domain}/.well-known/charter/{agent_id}
```

例:`https://alice.example.com/.well-known/charter/research_agent_v1`。
Principal 在自己的域名下发布 Charter。`principal_id` 由 host 隐含,
URL 路径里不出现。参考实现里靠 `CHARTER_SELF_HOSTED_PRINCIPAL`
环境变量开启。

**SDK helper:**

```python
from charter.discovery import resolve_charter_url

url = resolve_charter_url("alice@acme.com", "research_agent_v1")
```

先查本地 `data/charters/index.json`(每次 `save_charter` 自动维护);
查不到回退到 `{CHARTER_URL_BASE}/...` 拼接。`strict=True` 会抛
`CharterNotFoundError` 而不是回退。

### 4.9 Charter Chain 衰减

Charter Chain 是一串 Charter,每个子节点都声称是父节点的**严格子集**
——也就是 **agent-as-principal**(agent 自己也作为 principal)
的场景。公司给助理 agent 发一份宽 Charter;助理 agent 作为 principal,
再给它委派出去的研究 agent 发一份**更窄**的 Charter。

**Schema**:子 Charter 带 `parent_charter_url` + 可选的
`attenuation_proof.parent_charter_id`。根 Charter 两个字段都是 `null`。

**验证规则(v0.7,基于字符串 —— 语义子集判断 v0.8+):**

1. 父节点每条 `out_of_scope` 都要被子节点的某条 `out_of_scope` 覆盖
   (文本相等 OR 子节点文本是父文本的超串)。子节点可以**加**新的
   排除项。
2. `approval_required` 同样规则。
3. 子节点每条 `scope` 必须精确匹配父节点的某条 `scope` 文本。
   子节点可以**少**几条 scope —— 这正是 attenuation 的全部意义。
4. `attenuation_proof.parent_charter_id`(如果有)必须跟父
   `charter_id` 一致。

**MCP 工具:**

- `fetch_charter_chain` 沿 `parent_charter_url` 走到根,每跳验签 +
  生命周期 + attenuation。带循环检测(对 `charter_id` 维护一个
  seen-set),带深度上限(默认 5),返回的链是**根在前**。
- `aggregate_verdict_chain` 把同一套优先级规则套到链上**所有 Charter
  的所有命中 clause**。最严格的那一份 Charter 胜出。每条
  `matched_clauses` 都带 `source_charter_id`,调用方能看清是哪一
  份触发的。

**链强制的属性**:**所有限制的并集**,不是只看父。子节点禁止的、
父节点没禁的任务,在链上仍然被拦下 —— 这才是 attenuation 真正的
意义。

---

## 5. 设计 Rationale

这一节解释**为什么**。Spec 是规范的(normative);这一节是解释性
(explanatory)。第一次读如果只关心实现,可以跳。

### 5.1 为什么 Authority 这一层是关键空白

现有 agent 协议都是**正向声明**,各自填了 agent 表面的一个轴:

| 现有 | 回答什么 | 谁声明 | 粒度 |
|---|---|---|---|
| Agent Card(A2A) | 我能做什么? | Agent operator / framework | 每个 skill |
| Identity(Web Bot Auth) | 我是谁? | Agent operator / CA | 每条消息 |
| Resume / Reputation | 我做过什么? | 第三方 | 聚合 |
| Mandate(AP2) | 这次任务用户授权了什么? | 终端用户 | 每笔交易 |

叠起来:能力 + 身份 + 历史 + 单次授权。**漏掉的是**:在当前 principal
上下文里,agent **持续被允许或愿意做什么**。云端 IAM 十年前就把这
三层理清了 —— Charter 填的就是 Authority 中间这层。

### 5.2 为什么绑定粒度是 `principal × agent`

| 候选粒度 | 为什么不选 |
|---|---|
| **Model** | 同一模型服务无数 agent 和 principal;在这粒度上权限语义没用。 |
| **Agent class / Agent Card** | 只表达 agent 技术上能做什么 —— 没法表达"代谁工作"。Agent Card 是内在属性;Charter 是关系属性。 |
| **Single task** | 这是 AP2 Mandate 的粒度。Charter 要持续比单次任务久。 |
| **`principal × agent`** | 能表达同一 agent 在不同 principal 下不同的 scope / 拒绝 / 工时 / 预算 / 风格 / 上报。 |

这个粒度还带出一个关键性质:**同一个底层 agent 可以同时持有多份
Charter** —— `alice × research_agent_v1`、`bob × research_agent_v1`、
`bookkeeper × ocr_agent_v1` —— 每份都是独立的 Charter Instance。
调用方永远拿对应于"它当前要派活给谁、代表谁"那一份去评估。

### 5.3 为什么用版本化自然语言 clause,不用固定 policy 字段

有 IAM 背景的人很容易想做"固定 taxonomy":`actions` /
`resources` / `data_classes` 等等。v0 拒绝这条路。

Agent 任务空间是**开放的** —— 今天报税,明天清数据库,后天帮另一个
agent 重做 workflow。预先枚举的 taxonomy 不是变成谁也不填的企业策略
表,就是把没预想到的任务类型锁在门外。

v0 选**版本化自然语言 clause + 结构化 verdict 契约 + 协议常量 type
映射**:

- 每条 clause 有稳定 `id` + 从小闭集合里取的 `type`。
- Clause `text` 是自然语言 —— 跟底层 LM 一样有表达力。
- `TYPE_TO_DECISION` 把机械贡献写死,LLM 永远不能自由发挥决定
  `allow / needs_approval / incompatible` —— 它只判命中。

未来如果想把高频 clause 编译成机器字段,这个设计**向前兼容**,
没有被排除。

### 5.4 为什么 LLM-first + schema-bound

Compatibility Check 故意拆成两步:

- **LLM-first**:输入("这条开放式任务命中这条 clause 吗?")正好
  是语言模型擅长、规则系统不擅长的模糊语义判断。
- **Schema-bound**:输出必须**确定 / 可审计 / 可组合**。LLM 永远不
  返回自由形式的 verdict —— 它返回 per-clause 命中 + 信心,
  协议侧聚合器产出 verdict。

两个直接后果:

1. **调用方自己的 LLM 当裁判。** 没有中心化裁判 API。Operator 不需要
   信任外部服务来决定 agent 能做什么 —— 他们只要信任确定性聚合器和
   已签名的 Charter 内容。
2. **聚合器可单元测试。** `incompatible > needs_approval > allow`
   就三行代码。有意思的逻辑全部进了 clause 文本(签名过、可审查)和
   LLM prompt(可观测)。

未来想换更强的裁判(比如微调过的分类器)也可以,**不动协议表面**。

### 5.5 为什么 Self-Attesting + HTTPS 是 v0 的信任根

v0 出最薄的、还能挡住 casual tampering 的信任模型。Charter 内置
自己的公钥,调用方一次 HTTPS GET 拿到,本地验签完成。

更重的替代方案都被明确**延后**了:

| 替代方案 | 为什么不做 |
|---|---|
| JWKS 端点(`/.well-known/jwks.json`) | 多一次 fetch,多一个 endpoint 要解释。 |
| DID(分布式标识) | 协议还没需要去中心化标识,基础设施太重。 |
| X.509 证书链 | 跟 v0 实际威胁面比,工程量过度。 |
| `service_attestation` 第二层签名 | 不能防"签发方密钥被泄"这个真威胁,只增加复杂度。 |

诚实的局限(TOFU、轮换发现弱、不防 host 失陷)写在
[§4.5](#45-self-attesting-签名模型),v0.8 来补。

### 5.6 为什么 Charter 是**自愿协议**

Charter 是 **Delegation Gate(派活闸门)**,**不是 Capability-Boundary
Enforcement(能力边界强制)**:

| 层 | 真正能拦什么 | 谁必须配合 |
|---|---|---|
| **Delegation Gate**(v0) | 守规矩调用方的派活决策 | 调用方 |
| **Capability-Boundary Enforcement**(延后) | 实际资源操作(数据库写、转账、删文件) | 资源网关 |

v0 故意是自愿的:

1. **先例**:robots.txt 自愿了 30 年,跑通了可索引互联网。Cloudflare
   Web Bot Auth 也是自愿。自愿协议在**声誉敏感的生态**里是 work 的。
2. **迭代速度**:资源级强制要求跟每个网关(数据库、支付、文件系统、
   工具运行时)整合。那是多季度工程。Delegation Gate 才几百行代码。
3. **兼容面**:自愿协议可以**渐进采纳**。Capability-boundary 系统
   必须挂在每一个操作前面,否则等于零。

Charter v0 的诚实主张:**"对遵守协议的调用方,这里有一种稳定、低
成本、可审计的派活前决策方式。"** 不强行声称能挡恶意 agent。

### 5.7 为什么 `propose_within_scope` 是协议的一部分

一个只会返回 `allow / needs_approval / incompatible` 的 Charter,
本质上是个**合规拒绝系统**。有用,但是死的:调用方要么放弃、要么
反复撞门。

`propose_within_scope` 把协议从"拒绝列表"变成**派活路由器**。
verdict 是 `incompatible` 时,调用方可以反问:"在这份 Charter 下,
我**改派**成什么任务是合法的?" —— 拿回一份**基于 Charter 的改写**,
还附带触发 / 避开的 clause 列表。

两个后果:

1. **协议地位从 gate 升级到 coordinator。** 单纯拒绝告诉调用方"不
   是这里"。拒绝 + 改写告诉调用方"不是这件,但你可以做那件"。
   这是**权限系统**和 **agent 市场协议**的差别。
2. **倒逼 clause 设计变好。** 能产出有用改写的 clause 通常是具体的
   (`accounting / tax / bookkeeping`),而不是模糊的(`work-related
   tasks`)。改写这条路径变成 clause 质量的强迫机制。

### 5.8 为什么 Charter Chain 衰减放到 v0.7,不是 v0

双跳链是天然的下一个 demo:Alice 给 BookkeeperBot 签 Charter,
BookkeeperBot 再给它雇的 OCR 子 agent 签一份**收紧的** Charter。
每一跳必须是上一跳的子集。

v0 → v0.7 延后的三个原因:

1. **单 Charter 检查是承重的根。** 链是单 Charter 检查的组合;
   单 Charter 都没稳定,演示链没意义。
2. **链的子集语义需要 v0 没有的 clause-level 支持。** 干净的链检查
   要么用文本子集规则(粗糙),要么用语义子集推理(难得多)。v0 出
   clause 结构,v0.7 在上面加链逻辑,语义子集是 v0.8+。
3. **最常见的单体场景是一个人 × 一个 agent。** Charter Chain 是
   多 agent 场景。v0 的全部目的就是先让简单场景落地。

v0.7 出的是字符串版的链验证。语义子集在延后 backlog 上。

---

## 6. 反目标:Charter **不是**什么

Charter 经常被误以为是别的东西。澄清一下:

**不是 Agent Card 的扩展。** Agent Card 描述孤立的 agent。Charter
描述关系。不同对象、不同数据所有者、不同生命周期、不同基数。
在 Agent Card 里放一个 `charter_url` 引用是 OK 的;合并两个
artifact 不是。

**不是 Constitutional AI 或对齐训练的替代品。** Constitutional AI
是训练阶段塑造模型行为。Charter 是运行时、外置、可查询的声明层,
针对**某个具体的 principal-agent 关系**。两者**互补**——
Constitutional AI 说模型不论在什么场景里都不会做什么;Charter 说
**这次部署的这个模型,代表这个 principal 工作时**应该做什么。

**不是企业 IAM 产品。** Okta / Microsoft Entra / Google Cloud Agent
Identity 服务企业 IT 管理员,做自上而下的 RBAC。数据源是 HRIS 和 AD,
产品形态是管理控制台。**Charter 面向个人 principal**(或者小组织),
数据源是 principal 自己的上下文,clause 是用 LLM 投影出来的,
没有控制台。市场不重叠;两边对的设计选择对换都错。

**不是对恶意 agent 的保证。** 恶意调用方可以完全无视协议。v0 诚实
承认这一点,把声明范围圈在**配合协议的 agent 之间**。硬性强制要
靠 Capability-Boundary Enforcement,在延后 backlog。

**不是 principal 数据的公开转储。** Public Charter 是工作合同,
不是 memory dump。`provenance.source_commitments` 里全是 commitment
(type + summary + hash)。原始 Profile YAML、memory、对话历史、
源文档都**保持私有**。读到 Charter 的人能判断"派不派",但读不到
"principal 当初是怎么表述偏好的"。

---

## 7. 当前进展

到目前为止,`main` 上已经发了三个版本。每个都有打 tag 的 GitHub
Release,每个对应一个已关闭的 milestone 和一个合入的 PR。

**总体数据(到 v0.7):**

- `charter/` 包下 **18 个模块**
- **10 个 MCP 工具**
- **4 个 CLI 命令**(`issue` / `inspect` / `revoke` / `renew`)
- **158 个测试**,CI 6/6 全绿(`{py3.12, py3.13} × {ubuntu, macos, windows}`)
- `ruff` 干净,`mypy --strict` 干净

### 7.1 v0.5 — Project Hygiene

[Release notes](https://github.com/hnaymyh123-henry/charter/releases/tag/v0.5.0) · [PR #1](https://github.com/hnaymyh123-henry/charter/pull/1)

把黑客松原型升级成"陌生人能 clone、能 review、能贡献"的真项目。

- **Apache 2.0 license** + 完整项目元数据
- **Typed exception 层级**(`charter.errors`):`CharterError` +
  `CharterNotFoundError` / `CharterSchemaError` /
  `CharterSignatureError` / `CharterExpiredError` /
  `CharterRevokedError`。替换掉
  `ValueError("CharterNotFoundError: ...")` 这种前缀字符串
- **`/healthz`** 存活探针
- **`/.well-known/charter/{agent_id}`** 自托管路由,由
  `CHARTER_SELF_HOSTED_PRINCIPAL` 开启
- **CI** 跑 `{py3.12, py3.13} × {ubuntu, macos, windows}`,跑
  `ruff check` + `ruff format --check` + `mypy --strict` + `pytest`
- **`Dockerfile`**(多阶段、非 root 用户、`/data` volume、
  `HEALTHCHECK`)
- **`fly.toml`** 模板 + `.dockerignore`
- **文档拆分**:黑客松文档 → `docs/spec.md` + `docs/design.md` +
  `docs/legacy/hackathon-design.md`。(后来这两份在本次 PR 里又
  合并成本文件了。)
- **35 个测试**(原来 14)

### 7.2 v0.6 — 协议补全

[Release notes](https://github.com/hnaymyh123-henry/charter/releases/tag/v0.6.0) · [PR #9](https://github.com/hnaymyh123-henry/charter/pull/9)

把 v0 设计了但没做的功能补齐,让实现的协议表面对得上规范。

- **`propose_within_scope` MCP 工具** —— 单次 LLM 改写
- **`propose_within_scope_verified`** —— 在上面套 loopback 验证:
  最多 3 次尝试,温度退火 0.2 → 0.5 → 0.8,每次重试 prompt 收到
  上次的失败原因。成功返回 `RewriteProposal`,耗尽返回
  `RewriteFailure(attempts=...)`(带完整历史)
- **`charter revoke`** CLI —— 翻转 status、重签,撤销动作本身也带
  签名。之后 `fetch_charter` 抛 `CharterRevokedError`
- **`charter renew`** CLI —— 不调 LLM。clauses + summary 一字不改,
  换 `charter_id` 和有效期。旧 Charter 进
  `data/charters/archive/`,状态 `superseded`
- **Charter Discovery** —— `resolve_charter_url(principal, agent)`
  SDK helper + `data/charters/index.json` 索引文件(`save_charter`
  自动维护)
- **结构化日志**(`charter/_logging.py`)—— human + JSON 双格式。
  `CHARTER_LOG_FORMAT` 环境变量切换。每次 fetch 结果 + 每次 CLI
  命令都打一条日志,带 `charter_id` / `principal_id` /
  `agent_id` / `outcome`
- **静态私钥加密** —— `CHARTER_KEY_PASSPHRASE` 开启
  `BestAvailableEncryption`。没设就明文 PEM + 每次写时一条 WARN
  日志。v0 时代的明文 key 仍然能加载(向后兼容)
- **103 个测试**(原来 35)

### 7.3 v0.7 — Charter Chain + 框架适配 + 公网部署

[Release notes](https://github.com/hnaymyh123-henry/charter/releases/tag/v0.7.0) · [PR #16](https://github.com/hnaymyh123-henry/charter/pull/16)

Charter Chain attenuation + 第一个框架适配器 + 真正能上线的部署。

- **Chain schema** —— `Charter.parent_charter_url` +
  `Charter.attenuation_proof` + `MatchedClause.source_charter_id`
- **`verify_chain(child, parent)`** —— 字符串版子集检查
  (保守 / 确定性 / 零 LLM 成本)
- **`fetch_charter_chain`** MCP 工具 —— 沿 `parent_charter_url`
  走链,每跳验签 + 生命周期 + attenuation。带循环检测、深度上限、
  返回根在前
- **`aggregate_verdict_chain`** MCP 工具 —— 跨链套优先级规则,
  最严格胜出。每条 `matched_clauses` 带 `source_charter_id`
- **双跳 demo** —— `profiles/acme_corp.yaml` +
  `profiles/acme_assistant.yaml` + `scripts/demo_chain.py`。
  关键场景:"导出客户 PII 到 CSV"被**子 Charter 拦下而父 Charter
  没禁** —— 证明链强制的是**限制并集**,不是只看父
- **OpenAI Agents SDK 适配器** —— `charter.adapters.openai_agents`,
  `charter_preflight(charter_url, task)` + `@charter_gated(charter_url)`
  装饰器。**不强依赖** `openai-agents`,
  `pip install -e '.[openai_agents]'` 才装。Grader 注入让用户可以
  把所有 LLM 流量都留在同一家
- **fly.io 部署 workflow** —— `.github/workflows/deploy.yml`,
  push 到 `main` 自动部署,`/healthz` smoke check。被
  `vars.DEPLOY_ENABLED == 'true'` 开关 gate 住
- **158 个测试**(原来 103,新增 51)

---

## 8. Roadmap

`v0.5 / v0.6 / v0.7` 的详细迭代计划仍在 [`ROADMAP.md`](ROADMAP.md)
里。下面是面向未来的部分。

### 8.1 v0.8 — 信任模型升级(规划中)

[规划 issue #17](https://github.com/hnaymyh123-henry/charter/issues/17)
· [Milestone v0.8](https://github.com/hnaymyh123-henry/charter/milestone/4)

把 v0 的 TOFU 信任模型替换掉。三块联动:

1. **JWKS 端点** —— 签发方在 `/.well-known/jwks.json` 公开自己的公钥
   集。调用方每个签发方拉一次,按 `kid` pin。Charter 新增
   `provenance.issuer_kid` 字段
2. **公钥指纹固定** —— `data/pins.json` 记录
   `principal_id → key_fingerprint`,首次 fetch 写入;之后每次都对
   比内嵌公钥的指纹是否匹配。不匹配抛新的
   `CharterPinMismatchError`。手动重置走 `charter pins reset
   <principal>`
3. **透明日志(append-only)** —— 每份签发的 Charter 进
   `data/transparency.log`,带 SHA-256 链。`charter audit verify`
   走一遍日志确认连续;`GET /transparency/log` 和
   `GET /transparency/proof/<charter_id>` 给第三方审计用。
   Charter 新增 `provenance.transparency_log_id` 携带日志位置

附带几个小事(顺手):**把 `pyproject.toml` 的版本从 `0.1.0` 升到
`0.7.0`** —— 之前一直没升,**加 `charter audit` CLI 命名空间**。

**当前状态**:scope 草稿在 issue #17,等你 ✅/❌ 各条之后才开
具体 issue 和 PR。milestone 现在还是空的,没成本。

### 8.2 v0.8 之外(延后 backlog)

记下来防止忘,但不在近期路线上。

- **Charter Chain 的语义子集判断。** v0.7 是字符串版。语义版用
  LLM 来判断子节点 clause 是否是父节点的更严格表述。v0.8+ 的事
- **隐私**:Selective Disclosure JWT(SD-JWT)实现私有 clause;
  zero-knowledge proof —— 证明"Charter 满足 X"但不暴露 X
- **更多框架适配器**:v0.7 出了 OpenAI Agents。LangGraph 和 CrewAI
  在 backlog
- **集成**:Mem0/Letta 自动重投影(principal context 变了自动重生
  Charter);AP2 支付条款引用;Web Bot Auth 签名 header 携带
  `charter_url`
- **企业**:接 HRIS,按角色自动签发 Charter;审计接口 ——
  "这个 agent 过去 30 天里有没有违反过自己 Charter?"
- **Capability-Boundary Enforcement**:把 Charter check 绑到真实资源
  网关(数据库、支付、文件系统、工具运行时),让恶意调用方无法绕过。
  v1+ 主线
- **Charter 市场 / 模板**:按角色浏览的 profile 模板("标准会计师
  agent Charter")

---

## 9. 参考链接

- [`README.md`](README.md) —— 装机、配置、运行
- [`PRODUCT.md`](PRODUCT.md) —— 英文版本(canonical),本文的对应版
- [`CONTEXT.md`](CONTEXT.md) —— 术语表
- [`ROADMAP.md`](ROADMAP.md) —— v0.5 / v0.6 / v0.7 详细工作项分解
- [`AGENTS.md`](AGENTS.md) —— worker agent 协议行为规范(5 步
  Compatibility Check 循环)
- [`docs/legacy/hackathon-design.md`](docs/legacy/hackathon-design.md)
  —— 原黑客松文档,留作历史参考
- [GitHub Releases](https://github.com/hnaymyh123-henry/charter/releases) —— `v0.5.0` / `v0.6.0` / `v0.7.0`
- [当前开放的 Milestone](https://github.com/hnaymyh123-henry/charter/milestones?state=open) —— v0.8(规划中)
