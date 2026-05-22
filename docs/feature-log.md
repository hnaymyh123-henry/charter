# Feature Log

按版本倒序记录已完成功能 + 已知技术债。新功能合并后在对应版本下追加一行。

---

## 已完成

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
