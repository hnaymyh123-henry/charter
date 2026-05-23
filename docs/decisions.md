# Architecture Decision Records (ADR)

> 项目核心架构决策日志。任何与下列决策冲突的 PR 必须先更新本文件 + PROJECT_CONTEXT.md。
> 完整的设计理由章节见 [`PRODUCT.md`](../PRODUCT.md) §5 "Design Rationale"。

---

## ADR-001 — 不使用数据库,纯文件系统持久化

- **决策**:`data/` 下平铺文件存储,不引入 SQLite / Postgres
- **决策时间**:v0.x(hackathon),持续到现在
- **背景**:Charter 是协议,目标是任何人能简单实现。引入数据库 = 提高所有实现者的工程门槛
- **影响**:任何想引入数据库的 PR 需要先走架构决策评审

---

## ADR-002 — 签名算法固定 Ed25519

- **决策**:`provenance.issuer_signature` 仅支持 Ed25519,前缀 `"ed25519:"`
- **决策时间**:v0.x
- **背景**:Ed25519 是 2026 年的事实标准 —— 快、小、无参数选择,签名 64 字节
- **影响**:v0.x 不接受其他算法 PR。若要扩展,需要先升级 schema 的 `alg` 字段(目前没有)

---

## ADR-003 — Canonical JSON for signing

- **决策**:签名前 `Charter` 序列化为 UTF-8 JSON,sorted keys,无空格,`issuer_signature` 和 `transparency_log_id` 字段置空
- **决策时间**:v0.x;v0.8 扩展 `transparency_log_id` 例外
- **理由**:`transparency_log_id` 是 sign 之后才知道的 seq,要从 canonical 排除掉避免鸡生蛋
- **影响**:任何新加字段**默认进入签名覆盖**,只有"必须在 sign 之后赋值"的字段才能 opt out
- **v0.9 扩展(ADR-011 path 1)**:Disclosure 明文(`data/disclosures/<charter>/<id>.json`)从不进入 canonical bytes —— 只 `Clause.private_fields[].disclosure_hash` 进入。这样签名只承诺 hash,不承诺明文。同时为向后兼容,`Clause.private_fields == None` 时整个字段从 payload 中删除,保证 pre-ADR-011 Charter 的 canonical 字节与原签名一致。

---

## ADR-004 — TYPE_TO_DECISION 是协议常量

- **决策**:从 clause type 到 local decision 的映射写死在 `charter.constants.TYPE_TO_DECISION`,LLM 不得自由生成 local decision
- **决策时间**:v0.5
- **完整映射**:见 PRODUCT.md §4.2
- **影响**:任何"让 LLM 直接产出 verdict"的 PR 违反该 ADR,会被拒

---

## ADR-005 — 聚合规则:`incompatible > needs_approval > allow`

- **决策**:三状态严格优先级,单调,确定性,3 行 Python 即可写出
- **决策时间**:v0.5
- **fallback**:无 clause hit / 全部 confidence < 0.5 / lifecycle 异常 → `needs_approval`(closed-world)
- **影响**:聚合逻辑禁止引入概率融合 / 加权平均 / 模型调用

---

## ADR-006 — Charter 是 Delegation Gate,不是 Capability Enforcement(v0.x)

- **决策**:协议描述为 *"对于配合的 calling agent,提供低成本可审计的委托决策"*。明确**不**承诺对恶意 calling agent 的保护
- **决策时间**:v0.x(PRODUCT.md §5.6)
- **演进**:**A8 Postgres reference adapter v0.9 SHIPPED**(`charter.adapters.postgres`)—— 在 reference 级别落地 capability-boundary 模式,把同一 `aggregate_verdict` 原语放到 resource 侧强制执行。这**不**改变协议本身的 voluntary 立场;协议仍然不强制任何 enforcement 层,reference adapter 只是给愿意做强制的部署方一个可移植模板(~600 LOC,fail-closed everywhere)
- **影响**:文档与协议声明必须保持一致 ——"voluntary protocol"措辞不可弱化。Reference adapter 的存在仅作为 pattern proof,不进入协议规范

---

## ADR-007 — Self-Attesting Charter(v0)→ JWKS + Pin + Transparency log(v0.8)

- **决策(v0)**:Charter 内嵌 `issuer_public_key`,HTTPS 兜底信任
- **决策(v0.8 升级)**:加 JWKS 交叉检查 + key fingerprint pinning + SHA-256 chained transparency log
- **决策时间**:v0.8(2026-05-19)
- **`_fetch_and_verify` 顺序**:signature → JWKS 交叉检查 → pin → lifecycle
- **影响**:任何信任路径改动必须保持上述顺序,改动需在 PR 描述里说明影响

---

## ADR-008 — 私钥加密(可选)

- **决策**:`CHARTER_KEY_PASSPHRASE` 环境变量启用 BestAvailableEncryption;未设置则明文 PEM + WARN log(保留开发 UX)
- **决策时间**:v0.6
- **影响**:legacy 明文密钥保持兼容,从 PEM header 自动识别

---

## ADR-009 — MCP 服务不主动调 LLM(除 propose_*)

- **决策**:MCP 服务 default 不调 LLM,grader LLM 在 calling agent 侧。例外:`propose_within_scope`(1 次)和 `propose_within_scope_verified`(最多 2N 次)
- **决策时间**:v0.6
- **背景**:让运维者不必信任 Charter 服务的 LLM
- **影响**:新 MCP tool 若需调 LLM,必须在 tool 描述里显式说明并 documented as exception

---

## ADR-010 — Charter Chain 在 v0.7 使用 string-based 子集验证

- **决策**:`verify_chain(child, parent)` 用文本相等 / 包含规则,不使用 LLM
- **决策时间**:v0.7
- **演进**:**A1 PLANNED** 升级为 LLM-based 语义子集判定,结果缓存到 `attenuation_proof` 字段以保持 determinism
- **影响**:任何 chain 验证逻辑改动必须保留 string-based 路径作为 fallback

---

## ADR-011 — 隐私层走分层方案,不一步到位

- **决策(2026-05-22 讨论收敛)**:
  - **path 1(SHIPPED v0.9,Issue #31)**:redaction + SD-JWT,只遮蔽 clause text 里的敏感值,clause 结构和 type 保持公开;caller 的 LLM 仍能判 hit
  - **path 2(留到 v1)**:delegated grading endpoint(server-side 跑 grading 只返回 verdict);需要给 protocol 加 optional "issuer 是 trusted grading oracle" 模式
  - **path 3(ZKP)**:留到 ZK + LLM 工程栈成熟,长期方向
- **背景**:直接套 SD-JWT 会让被遮蔽 clause 在 caller 那边等同"不存在",反而不安全
- **path 1 shipped 内容**:`Clause.private_fields: list[PrivateFieldRef] | None`、`Visibility.private_clauses` 字面量扩展为 `"not_supported_in_v0" | "redaction_v1"`、`charter/privacy.py` 4 个 helper(`redact_clause` / `verify_disclosure` / `match_redacted` / `Disclosure`)、`data/disclosures/<charter_id>/<disclosure_id>.json` 持久化、`GET /disclosures/{charter_id}/{disclosure_id}` bearer-token endpoint(`CHARTER_DISCLOSURE_TOKEN`)
- **影响**:`Clause.private_fields` 默认为 None;`_canonical_bytes` 在 None 时删除该 key 保证向后兼容;disclosure 明文永不进 canonical bytes;path 2/3 编排同 ADR 保留

---

## ADR-012 — Framework adapter 优先级:OpenAI / Anthropic SDK > LangGraph / CrewAI

- **决策(2026-05-22)**:adapter 路线只投 OpenAI Agents SDK(已 ship v0.7)和 Anthropic SDK / Claude Agent SDK(deferred,低优先);**不**投 LangGraph 和 CrewAI
- **背景**:用户判断 LangGraph / CrewAI 已过采用峰值,model-vendor SDK 才是当前生态主流
- **影响**:新 adapter PR 若是 LangGraph / CrewAI 方向需先得到 PM 重新评估
