# Tech Stack

## 语言 & 运行时
- **Python 3.12 / 3.13**(`requires-python = ">=3.12"`)
- 单语言项目;计划新增 `charter-js`(npm,TypeScript)作为独立仓库 —— PLANNED B1.2,blocked by B1.1。

## 框架
- **FastAPI ≥ 0.115** + **uvicorn ≥ 0.30** —— HTTP 服务(charter.server)
- **MCP ≥ 1.0** —— 暴露 10 个 MCP tools(charter.mcp_server)
- **Anthropic SDK ≥ 0.40** —— `propose_within_scope` / `propose_within_scope_verified` 的 LLM 调用
- **Pydantic ≥ 2.0** —— schema 定义(charter.schema)
- **Click ≥ 8.1** —— CLI(`charter issue / inspect / revoke / renew / pins / audit`)

## 加密 / 安全
- **cryptography ≥ 43.0** —— Ed25519 签名,BestAvailableEncryption 加密私钥
- 协议层固定 **Ed25519 only**(PRODUCT.md §4.5),v0.x 不引入其他签名算法

## 持久化
- **无数据库** —— 全部文件系统:`data/charters/`、`data/keys/`、`data/pins.json`、`data/transparency.log`、`data/charters/index.json`
- 不引入数据库是协议轻量化的有意选择,任何 PR 想引入 SQLite / Postgres 都需走架构决策

## 测试
- **pytest ≥ 8.0** + **pytest-asyncio ≥ 0.23**
- 259 个 test 用例,19 个 test 文件
- `asyncio_mode = "auto"`

## Lint / Type
- **ruff ≥ 0.6**(lint + format),规则集 `E,F,W,I,B,UP,C4,SIM,RET`,line-length 100
- **mypy ≥ 1.10 strict mode**(`disallow_untyped_decorators = false`),仅扫描 `charter/`

## CI
- GitHub Actions:`.github/workflows/ci.yml`,矩阵 `{py3.12, py3.13} × {ubuntu, macos, windows}`
- Deploy:`.github/workflows/deploy.yml` → fly.io,gated on `vars.DEPLOY_ENABLED == 'true'`

## 关键依赖版本

| 依赖 | 最低版本 | 用途 |
|---|---|---|
| pydantic | 2.0 | schema |
| fastapi | 0.115 | HTTP |
| uvicorn[standard] | 0.30 | ASGI 服务器 |
| cryptography | 43.0 | Ed25519 + 私钥加密 |
| anthropic | 0.40 | LLM 调用 |
| mcp | 1.0 | MCP tool surface |
| pyyaml | 6.0 | Profile YAML 解析 |
| click | 8.1 | CLI |
| python-dotenv | 1.0 | env 加载 |
| httpx | 0.27 | 客户端 HTTP(JWKS / 链 / discovery) |

## 计划新增依赖(按 task)

| Task | 计划新增依赖 | 用途 |
|---|---|---|
| #4 A6 Web Bot Auth | `http-message-signatures`(或自实现 RFC 9421) | HTTP message signing |
| #5 A8 Postgres adapter | `sqlglot`、`asyncpg` | SQL 解析 + 异步 PG 客户端(reference adapter 独立模块) |
| #6 Priv-1 | `sd-jwt`(评估中) | SD-JWT selective disclosure |
| #14 B2.7 OTel | `opentelemetry-api`、`opentelemetry-sdk`(可选依赖) | 可观察性 |
| #15 B3.10 perf | `pytest-benchmark` | dev 依赖 |

**新增依赖原则**:核心 `charter/` 模块不引入重依赖;adapter / 实验性模块作为可选依赖(`[project.optional-dependencies]`)。
