# Style Guide

## 命名规范

- Python: `snake_case` 变量 / 函数 / 模块,`PascalCase` 类名,`SCREAMING_SNAKE_CASE` 常量
- 测试函数:`test_<被测对象>_<行为描述>`,如 `test_aggregate_verdict_incompatible_dominates`
- Fixture:`@pytest.fixture` 函数名跟用法一致,如 `signed_charter`
- 私有模块以单下划线起首(`charter/_logging.py`)

## 目录结构

```
charter/
  __init__.py
  schema.py            # 全部 Pydantic 模型
  constants.py         # TYPE_TO_DECISION 等协议常量
  errors.py            # 类型化异常层级(CharterError 起点)
  signing.py           # canonical bytes + Ed25519 sign / verify
  storage.py           # 文件读写 + 私钥加密
  chain.py             # Charter Chain verification
  discovery.py         # resolve_charter_url + index 维护
  loopback.py          # propose_within_scope_verified
  propose.py           # propose_within_scope 单次
  pins.py              # key fingerprint pinning
  keys.py              # JWKS 客户端 + cache
  transparency.py      # SHA-256 chained log
  projection.py        # Profile YAML → clauses(LLM 投射)
  prompts.py           # grader / projector 的 prompt 模板
  server.py            # FastAPI 路由(REST endpoints)
  mcp_server.py        # MCP tool 注册 + 实现
  cli.py               # Click 命令行入口
  _logging.py          # structlog / stdlib JSON formatter
  adapters/
    __init__.py
    openai_agents.py   # framework adapter(SHIPPED v0.7)

tests/
  test_<module>.py     # 每个 charter/ 模块对应一份测试文件
  adversarial/         # 攻击向量测试(PLANNED B1.4)
  conftest.py          # 公用 fixture(若需要)
```

**新增 adapter** 必须放到 `charter/adapters/` 下;新增 capability 适配器(如 Postgres proxy)若代码量 > 500 LOC,使用独立子包(`charter/adapters/postgres/`)。

## 错误处理

- 全部用类型化异常,不要 `raise ValueError("CharterXxxError: ...")`(v0.5 已清理过)
- 异常都继承自 `charter.errors.CharterError`
- FastAPI 路由层抛 `HTTPException`,业务层抛 `CharterError`,在 `_fetch_and_verify` 边界做转换
- 不要静默吞异常;低层错误必须冒泡或被上层显式翻译

## 注释 / Docstring

- **不写 what,只写 why**。signature 说明 what,符号命名说明 what
- **不引用任务编号 / PR 号 / issue 号**(那些放在 commit message 和 PR 描述里)
- 公开 API 加一行 docstring 说明用途,内部 helper 默认不写

## Lint / Format

- `ruff check` + `ruff format --check` 必须 pass
- `mypy --strict charter/` 必须 pass(Worker Agent 在自检阶段必跑)
- line-length 100
- import 顺序由 ruff 自动管理(isort 规则集 `I`)

## 测试

- 每个 charter/ 模块对应 `tests/test_<module>.py`
- 涉及 LLM 调用的测试必须 mock Anthropic(用 `respx` 拦截 httpx,或注入 client double)
- 网络访问的测试要么 mock,要么 mark `@pytest.mark.live` 并默认 skip
- 新增功能的 PR 必须带相应测试;coverage 不强制 100%,但关键路径必须覆盖

## Commit / PR 风格

- Commit message 格式:`<type>(<scope>): <subject>`
  - type:`feat` / `fix` / `docs` / `refactor` / `test` / `chore`
  - scope:`schema` / `signing` / `server` / `cli` / `mcp` / `adapters` / `transparency` / ...
  - 例:`feat(transparency): SHA-256-chained append-only log of signed Charters`
- PR 标题跟主 commit message 一致;PR body 包含 Summary + Test plan
- 不在 commit message 里加 emoji(项目根 CLAUDE.md 规则)

## Schema 演进

- 新加字段:**默认进入签名 canonical bytes**;若不应进,需在 `_canonical_bytes` 显式加 exclude 并在 ADR 记录理由
- 已发布字段不得改语义;需要变化时加新字段,旧字段标 deprecated
- `Charter` 的版本号在 `version` 字段,改动 schema 需要 bump
