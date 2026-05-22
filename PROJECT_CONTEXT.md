# PROJECT_CONTEXT.md — Charter

> 主索引。Worker Agent 在每轮启动时读取此文件恢复上下文,然后按需读取下方子文档。
> 详细内容分散在 `docs/` 和顶层产品文档中。

---

## 仓库信息

- **仓库地址**:https://github.com/hnaymyh123-henry/charter
- **主分支**:main
- **当前版本**:0.8.0(已合并 PR #25,未打 tag)
- **License**:Apache-2.0
- **创建时间**:2026 年初(hackathon 起源)

---

## 项目使命(一句话)

**The Authority layer for the agent economy** —— 在 Agent Card(Capability)和 AP2 Mandate(Authorization)之间填补关于"持续授权"的中间层,以可签名、可查询、可审计的方式表达 *principal × agent 关系*。

详见 [`PRODUCT.md`](PRODUCT.md)。

---

## 子文档目录

| 文件 | 内容 | 更新时机 |
|---|---|---|
| [`PRODUCT.md`](PRODUCT.md) | 协议规范 + 设计理由 + 当前能力清单 | 协议层变化时 |
| [`ROADMAP.md`](ROADMAP.md) | 历史迭代规划(v0.5 → v0.8) | 新版本规划时 |
| [`CHANGELOG.md`](CHANGELOG.md) | 版本变更日志 | 每次 release |
| [`CONTEXT.md`](CONTEXT.md) | 术语表(principal / issuer / charter / verdict / ...) | 术语变化时 |
| [`AGENTS.md`](AGENTS.md) | Worker agent 协议端期望行为(5 步 Compatibility Check 循环) | 协议变化时 |
| [`docs/architecture.md`](docs/architecture.md) | 业务流 / 信息流 / 传输架构图(forward-looking,5 张 Mermaid + 1 个端到端 walkthrough) | 架构决策时立即更新 |
| [`docs/tech-stack.md`](docs/tech-stack.md) | 语言、框架、关键依赖版本 | 技术选型变化时 |
| [`docs/decisions.md`](docs/decisions.md) | 架构决策记录(ADR),含信任模型、签名、聚合规则、隐私层设计 | 新决策时立即追加 |
| [`docs/style-guide.md`](docs/style-guide.md) | 命名规范、目录结构、错误处理、注释约定 | 约定变化时 |
| [`docs/feature-log.md`](docs/feature-log.md) | 已完成功能列表(PR 号 + 合并日期),底部含已知技术债 | 每轮 Phase 5 更新 |

---

## 当前状态(本节频繁更新)

- **最后更新**:2026-05-22
- **当前迭代目标**:**v0.9 — Production-readiness + 生态扩散**。围绕 14 个 task 分 3 批推进:
  - **Batch 1(并行 6 个)**: #9 Cookbook、#7 Adversarial test、#2 Chain 语义子集、#3 AP2、#4 Web Bot Auth、#6 隐私层 path 1
  - **Batch 2(部分串行 6 个)**: #8 Revocation propagation → #12 Inspector UI → #14 OTel(server.py 串行);并行 #5 Postgres adapter、#10 Conformance suite、#15 Perf baseline
  - **Batch 3(blocked 2 个)**: #11 JS SDK(等 #10)、#13 Step-up(等 #3)
- **开放 PR**:无(main 干净)
- **已知技术债**:见 [`docs/feature-log.md`](docs/feature-log.md) 底部

---

## 协议关键不变量(改动时必读)

任何改动 schema / signing / aggregation 的 PR 都必须保持这些不变量,否则破坏向后兼容。

1. **签名覆盖**:`provenance.issuer_signature` 和 `provenance.transparency_log_id` **不**进入 canonical bytes;其他字段全部进入。
2. **TYPE_TO_DECISION** 是协议常量(PRODUCT.md §4.2),不得在代码里被覆盖。
3. **聚合规则**:`incompatible > needs_approval > allow`,所有路径单调。
4. **`Charter.binding.agent_id` 是 agent_id 的唯一真相**,不要在顶层加重复字段。
5. **`visibility.private_clauses`** 接受 `"not_supported_in_v0"`(默认,向后兼容)或 `"redaction_v1"`(ADR-011 path 1,v0.9 ship)。后续 path 字面量需在同一 ADR 下扩展;不得在 schema 默认值上引入未在 ADR 登记的字符串。
6. **`_fetch_and_verify` 顺序**:signature → JWKS 交叉检查 → pin → lifecycle。任何重排序需要在 PR 描述里说明影响。
