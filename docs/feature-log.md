# Feature Log

按版本倒序记录已完成功能 + 已知技术债。新功能合并后在对应版本下追加一行。

---

## 已完成

### v0.9.0 — Batch 1: Production-readiness + 协议扩展(2026-05-22 → 2026-05-23 合并,PR #32-#37)
- A1 Chain 语义子集校验 (LLM-based `verify_chain_semantic`,MCP tool #11,新 `CharterChainGraderError`,PR #34 / Issue #26)
- A5 AP2 Mandate 集成 (`charter/adapters/ap2.py` + `AP2VerifyResult` schema + 端到端 demo,PR #32 / Issue #27)
- A6 Web Bot Auth signed-header adapter (自实现 RFC 9421 子集 + sign/verify/middleware,Ed25519-only,PR #33 / Issue #28)
- Priv-1 Redaction + SD-JWT selective disclosure (`Clause.private_fields` + `charter/privacy.py` + bearer-token `/disclosures/...` endpoint,PR #35 / Issue #31)
- B1.4 Adversarial test suite (5 类攻击 28 case + `FakeAnthropicClient` + `docs/threat-model.md` + CI step,PR #36 / Issue #29)
- B3.9 Cookbook 10 篇 (180-275 行/篇 + 13 可跑 example,PR #37 / Issue #30)
- 累计 +11615/-46 行,+104 个新测试(259 → 363+,xfail 2)
- ADR-003 path-1 disclosure 例外 + ADR-011 path-1 落地 + 协议不变量 #5 扩展支持 `redaction_v1`

### v0.8.0 — Trust model upgrade(2026-05-19 合并,PR #19-#25)
- JWKS endpoint `/.well-known/jwks.json`(PR #19)
- JWKS 客户端 + `_fetch_and_verify` 交叉检查(PR #20)
- Key-fingerprint pinning + `charter pins` CLI(PR #21)
- SHA-256 chained transparency log(PR #22)
- Transparency HTTP endpoints + `provenance.transparency_log_id`(PR #23)
- `charter audit verify/show` CLI + 版本号 0.1.0 → 0.8.0 + CHANGELOG(PR #24)
- v0.8 release merge(PR #25)

### v0.7.0 — Charter Chain + Adapter + Deploy(2026-05-19 合并,PR #11-#16)
- Charter Chain schema(`parent_charter_url`、`attenuation_proof`、`MatchedClause.source_charter_id`)
- `fetch_charter_chain` + `aggregate_verdict_chain` MCP tools
- 两跳 demo(`profiles/acme_corp.yaml` + `profiles/acme_assistant.yaml` + `scripts/demo_chain.py`)
- OpenAI Agents SDK adapter(`charter.adapters.openai_agents`)
- fly.io deploy workflow

### v0.6.0 — Protocol completion(2026-05-19 合并,PR #2-#9)
- `propose_within_scope` + `propose_within_scope_verified` MCP tools
- `charter revoke` + `charter renew` CLI
- Discovery(`resolve_charter_url` + `data/charters/index.json`)
- Structured logging(`charter._logging`)
- Encrypted private keys(`CHARTER_KEY_PASSPHRASE`)

### v0.5.0 — Project hygiene + protocol foundations(2026-05-19 合并,PR #1)
- 类型化异常层级
- TYPE_TO_DECISION + 聚合规则常量化
- GitHub Actions CI(`{py3.12, py3.13} × {ubuntu, macos, windows}`)
- Apache 2.0 license + Dockerfile + healthz
- 文档拆分(后被 v0.8 重新整合进 PRODUCT.md)

### v0(hackathon prototype)
- 原始 36 小时 demo;tag `v0-demo` 保留

---

## 已知技术债

| 描述 | 来源 | 登记时间 |
|---|---|---|
| `pyproject.toml` 已有 `[tool.bumpversion]` 但 v0.8 未打 git tag(v0.5/0.6/0.7 都有) | v0.8 release | 2026-05-22 |
| Transparency log 的磁盘增长率未测量(B3.10 会覆盖) | v0.8 release | 2026-05-22 |
| Discovery index 在并发 `save_charter` 下没有锁保护(单进程开发足够,部署到 fly.io 多实例时可能丢更新) | v0.6 release | 2026-05-22 |
| `_canonical_bytes` 对 `transparency_log_id` 的特殊处理目前用文字判断,改 schema 时容易忘 —— 应该改成 Pydantic 字段元数据驱动 | v0.8 release | 2026-05-22 |
| `propose_within_scope_verified` 的 temperature 序列(0.2 / 0.5 / 0.8)和 max_attempts=3 是硬编码,缺少可配置接口 | v0.6 release | 2026-05-22 |
| A1 语义 chain check 已 ship,但 PR #36 的 2 个 attenuation bypass xfail 还没人验证现在能否变 xpass —— 应该跑一次 A1 verifier 重新评估 xfail 状态 | v0.9 Batch 1 retro | 2026-05-23 |
| Worker Agent worktree 在 PR 创建成功后未自动释放,导致 fix worker 不得不在已有 worktree 工作。下轮 worker prompt 加 `ExitWorktree action=remove` 自释放 | v0.9 Batch 1 retro | 2026-05-23 |
| Fix worker 重派(token 过期场景)的 prompt 缺"先 git log/git status 校对上轮成果"强制 step —— PR #32 第一次成功 push 但 PR comment 失败,差点重做。下轮模板加 | v0.9 Batch 1 retro | 2026-05-23 |
| 安全相关模块的 worker prompt 缺"allowlist 优先 + 必 grep 所有调用方 + 必 pathlib boundary check"checklist。PR #35 三轮 QA 的根因 | v0.9 Batch 1 retro | 2026-05-23 |
