# Feature Log

按版本倒序记录已完成功能 + 已知技术债。新功能合并后在对应版本下追加一行。

---

## 已完成

### v0.9.0 — Batch 3: 协议外延 + SDK 多语言(2026-05-26 PR 开,Phase 4 review pending,PR #53 / #54 / #55)
- B2.5 Step-up Protocol — `AdHocGrant` schema + Ed25519 签名 + `data/grants/<id>.json` 持久化(`_safe()` allowlist + post-resolve `is_relative_to` 边界校验,延续 Priv-1 经验)+ `POST /step-up`(rate limit `(principal_id, agent_id) ≤ 5/60s` + 三种 approval mode:`auto-deny` 默认 / `auto-approve` / `callback` 同步转发,callback 任意失败 → fail-closed)+ `GET /grants/{id}` 200/404/410 + MCP tool 12 `request_step_up`(HTTP-forward only,ADR-009)+ AP2 adapter `extensions.ad_hoc_grant_id` 扩展(verified + unexpired + literal-task-cover → 把 Charter `incompatible`/`needs_approval` 提升为 `allow`,任何 failure 留住原 verdict 不 fail-open);+3 typed errors。~1759 LOC + 56 个新测试。Grants **不**进 transparency log(ADR-013 future-work)(PR #53 / Issue #51)
- B1.2 `@charter/core` JS / TS SDK — verification-only 端口,12 个 source(1352 LOC,zod schema + canonical bytes + Ed25519 via `@noble/ed25519` v2 + aggregate + strict chain + lifecycle + JWK/kid + pin fingerprint + transparency log walk + SD-JWT path 1 privacy)+ 11 个 vitest(110 cases 全绿)+ 4 个 conformance vector 锚定与 Python 字节级一致;`tsup` 双格式(ESM + CJS + `.d.ts`),`tsc --noEmit` strict 干净。`signing.ts` 修了一个 leftover bug:`ensureEd25519Init()` 调用但从未定义(noble v2 自动用 WebCrypto 初始化,无需 init)(PR #54 / Issue #50)
- chore env-fixes — `tests/test_observability.py` `importorskip("opentelemetry")` → `importorskip("opentelemetry.sdk")`(api 包常被传递依赖装上但 SDK 不在,旧 guard 太宽松);`tests/adapters/test_postgres_{intent,proxy}.py` 加模块级 `importorskip("sqlglot")` 使 `[dev]`-only env 不再在 collection 阶段报错;`charter/inspector.py` 三处 `jinja2.Template.render` 显式 bind 到 `rendered: str` 解决预先存在的 mypy `no-any-return`。完整套件:6 failed + 10 errored + 2 collection errors → **427 passed / 12 skipped / 2 xfailed**;`mypy charter/` 32 文件 0 错误(PR #55)
- 累计新增 ~1759 (B2.5) + ~5297 (B1.2) + ~29 (chore) = **~7085 LOC**,Python 端 +56 测试,JS 端 +110 vitest cases
- 未做:Optional ADR-011 path 2 delegated grading endpoint(从 "Beyond v0.9 deferred" 提前的候选项,保持 Batch 3 聚焦未启动)

### v0.9.0 — Batch 2: 协议生态 + capability 演进(2026-05-23 → 2026-05-24 合并,PR #44-#49 + hotfix 5ebc6f3)
- A8 Postgres reference adapter — capability-boundary 模式 reference,fail-closed PG wire proxy,PRODUCT.md §5.6 + ADR-006 演进措辞(PR #48 / Issue #41,~700 LOC + 37 tests)
- B1.1 Conformance test suite — language-neutral 44 JSON vectors + SPEC.md + Python runner(完整)+ JS/Rust skeleton runner,CI gate(PR #47 / Issue #42)
- B1.3 Revocation propagation — Cache-Control middleware + `/transparency/revoked` NDJSON + RevocationAwareCache client SDK helper(PR #45 / Issue #38)
- B2.7 OTel observability — `charter/observability.py` no-op fallback + 6 个 span(charter.* 私有 namespace) + docs/observability.md(PR #46 / Issue #40)
- B3.8 Inspector Web UI — `/inspect` + HTMX/Alpine.js + URL allowlist + SSRF guard,QA 32/32 PoC clean(PR #44 / Issue #39)
- B3.10 Performance baseline — `benchmarks/` 5 文件 + pytest-benchmark + docs/performance.md + CI opt-in `[bench]` gate(PR #49 / Issue #43)
- OTel CI regression hotfix — `unused-ignore` 处理 cross-env mypy(commit 5ebc6f3)
- 累计 +133 个新测试(363 → 474+);测试 collect 涨到 439

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
| `docs/architecture.md` 是 single 545-行 forward-looking doc,每 PR 都改不同 section,Batch 2 5 个 PR 都改同文件触发 GitHub auto-resolve 失败 → 考虑拆分 `docs/architecture/{system-context,issuance,runtime,topology,trust,e2e}.md` 单独维护(中优先级) | v0.9 Batch 2 retro | 2026-05-24 |
| OTel CI regression(unused-ignore)说明 worker 在引入 optional-dep 模块时缺"两种 env 跑 mypy"验证。下轮 prompt 加:optional-dep 改动后必须 confirm `pip install -e '.[dev]'`(不含 optional)env 下 mypy strict 也通过 | v0.9 Batch 2 retro | 2026-05-24 |
| Worker self-cleanup:`ExitWorktree` 在 subagent context 不可用是 harness 限制(非 worker 失职),Tech Lead 每次 Batch 结束都得手动清 orphaned worktrees。考虑请求 harness 提供 "PR-create 后自动 worktree teardown" 钩子 | v0.9 Batch 2 retro | 2026-05-24 |
| Rebase Worker 是 Batch 2 新出现且表现良好的角色 — 应固化为 `worker-rebase.md` 模板,跟 worker-fix.md / worker-new.md 并列 | v0.9 Batch 2 retro | 2026-05-24 |
| ROADMAP.md 已知技术债:v0.8 + v0.9 还未打 git tag(v0.5/0.6/0.7 都有),可以在 Batch 3 启动前补上 `git tag v0.8.0` + `git tag v0.9-batch2-rc1` | v0.9 Batch 2 retro | 2026-05-24 |
| CHANGELOG.md 的 `[Unreleased]` 区段在 Batch 3 之前**只**记录了 B3.8 Inspector 一项 — Batch 1 + Batch 2 共 12 个 PR 的 Added 行从未补录。Batch 3 commit 同步加了 B2.5/B1.2/B3.8/PR #55,但 Batch 1+2 的回填仍是 pending,在打 v0.9 tag 前必须补齐 | v0.9 Batch 3 retro | 2026-05-26 |
| `optional-dependencies` 的 skip-guard 模式不统一:`test_postgres_*` / `test_observability` 现在用模块级 `importorskip`,但 `test_openai_agents` 走的是 fixture-内 skip,`test_web_bot_auth` 又是另一套。下轮考虑拉一个 `tests/optional_extras.py` 集中"哪种 extra 缺时 skip 哪些 modules" | v0.9 Batch 3 retro | 2026-05-26 |
| B1.2 ship 的 `js/` 子树需要补一个 README + npm publish workflow + GitHub Action 跑 vitest;现在它的 CI 完全没接入 `.github/workflows/ci.yml`(Python only)。`@charter/core` 没 CI 守护会很快走样 | v0.9 Batch 3 retro | 2026-05-26 |
| `conformance/runners/javascript/run.mjs` 仍是 skeleton(只跑一个 vector),B1.2 落地后应改写为 dispatch through `@charter/core`,真正覆盖所有 44 vector;否则 B1.1 "language-neutral conformance" 承诺只对 Python 兑现 | v0.9 Batch 3 retro | 2026-05-26 |
| Batch 3 期间发现 `signing.ts` 残留了一个 `ensureEd25519Init()` 调用但未定义 — 写测试时才被抓出来。说明 worker-new 流程里"无测试代码不算 done"应该升级成硬性 SOP(B1.2 的 source 是上一轮 worker 提交的,无 test 即合) | v0.9 Batch 3 retro | 2026-05-26 |
| ADR-011 path 2(delegated grading endpoint)从 "Beyond v0.9 deferred" 提前到 Batch 3 可选位,实际没启动。下次规划要决定:留在 v0.9 Batch 4(若有)/ 推到 v0.10 / 还是降级回 deferred | v0.9 Batch 3 retro | 2026-05-26 |
