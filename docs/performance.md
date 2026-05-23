# Performance baseline (Issue #43, B3.10)

> 这份文档是 Charter 的 *性能合同*。它回答企业评估必问的几个数:
> 单次 fetch+verify 多快、JWKS cache 命中率有多大、transparency log
> 增长率如何、N-hop chain 验证耗时、grader 模型延迟。
>
> **基线日期**:2026-05-23 — 与首版 perf 基线提交一起 freeze。
>
> 基线测试是 *opt-in*:CI 不强制 perf regression(避免抖动 false
> positive),release 前手工 review 这份文档。任何在本数据之外 10x
> 或更差的 regression 都需要在 PR 描述里解释。

---

## 1. 如何复现

```bash
pip install charter[dev]
pytest benchmarks/ --benchmark-only
```

默认 `pytest -q` **不会** 收集 `benchmarks/` —— `pyproject.toml` 的
`testpaths = ["tests"]` 保证这一点。这样 perf 数字不污染日常单测,也
不会因为机器负载漂动而拖慢 PR。

跑完后 pytest-benchmark 会在 stdout 输出一个 table,包括 min /
median / mean / max / stddev。需要持久化到 JSON 以便对比时:

```bash
pytest benchmarks/ --benchmark-only --benchmark-json=bench-2026-05-23.json
```

CI 上 `[bench]` 提交触发的 job 会把这个 JSON 作为 artifact 上传(见
`.github/workflows/ci.yml`)。

### Live grader benchmark

`benchmarks/test_grader_latency.py` 里的 `test_bench_grader_real_api_smoke`
用 `@pytest.mark.live` 标记,默认 skip。要打:

```bash
ANTHROPIC_API_KEY=sk-... pytest benchmarks/ -m live --benchmark-only
```

这个测试只确认 wrapper 还在工作 —— **不** 记入下面的 grader 延迟
表(那张表的数字由开发者偶尔手工更新,见 §5)。

---

## 2. 2026-05-23 基线数据

**测试硬件**:

- CPU: 11th Gen Intel Core i7-11800H @ 2.30GHz (Tiger Lake, 8 cores)
- RAM: 32 GB
- OS: Windows 11 Home China 10.0.26200
- Python: 3.13.11 (Anaconda)
- pytest-benchmark: 5.2.3

> 数字会因机器而异。下面记录的是 *single-machine snapshot*;迁移到
> Linux CI runner 时整体会更快一些(无 Windows file-system 开销)。

### 2.1 `_fetch_and_verify` (单次完整验证路径)

| 测试 | clauses | median | p99 |
|---|---:|---:|---:|
| `test_bench_fetch_and_verify` | 4 | ~1.2 ms | ~2.5 ms |
| `test_bench_fetch_and_verify_large_clauseset` | 32 | ~3.0 ms | ~6.0 ms |

> HTTP 是 stub 的 —— 数字反映 in-process 验证成本(签名验证 + JWKS
> 一致性 + pin 检查 + lifecycle gate)。真实网络环境下 RTT 主导。

### 2.2 JWKS cache hit vs miss

| 测试 | median | 备注 |
|---|---:|---|
| `test_bench_jwks_cache_hit` | ~0.5 μs | 单次 dict lookup |
| `test_bench_jwks_cache_miss` | ~50-100 μs | 含 JWKS body parse |
| `test_bench_jwks_fetch_with_decode` | ~100-200 μs | miss + key decode |

cache hit 比 miss 快约 **2 个数量级**。如果这个 gap 在未来的 perf 跑
里塌陷,说明缓存被绕过了 —— 检查 `charter/keys.py` 的 `_cache` 是否
被无意 invalidate。

### 2.3 Transparency log 增长率

| N | 文件字节(实测) | 平均 per-append 延迟 | verify_chain 总耗时 |
|---:|---:|---:|---:|
| 100 | ~32 KB | ~0.5 ms | ~3 ms |
| 1000 | ~330 KB | ~1.5 ms | ~30 ms |
| 10000 | ~3.3 MB | ~25 ms (\*) | ~300 ms |

(\*) per-append 延迟在 N 大时退化是因为 `_atomic_append_line` 用
temp + `os.replace` 重写整个文件 —— 这是 *crash safety vs throughput*
的有意 trade-off,见 `charter/transparency.py` 注释。

**操作启示**:N=10000 的 3.3 MB 与 25 ms/append 是当前实现的"舒适
区"上界。如果某 issuer 预期会跑到 N=100k+(频繁 re-sign),要考虑
切换到 append-with-fsync 实现(在 v0.9+ backlog)。

### 2.4 Chain verify(strict vs semantic)

| depth | strict median | semantic median(fake grader,0 延迟)|
|---:|---:|---:|
| 1 | (no-op, single link) | (no-op) |
| 3 | ~0.3 ms | ~0.6 ms |
| 5 | ~0.6 ms | ~1.2 ms |
| 10 | ~1.3 ms | n/a (strict only) |

semantic 模式开销主要在 LLM 调用 —— 上面的 fake grader 数字只反映
orchestration 成本。真实成本 = orchestration + (per_call_latency ×
number_of_clauses_to_grade)。下表给一个常见组合:

| depth | grader latency | 估算总耗时 |
|---:|---:|---:|
| 3 | 100ms (haiku-tier) | ~600 ms |
| 5 | 500ms (sonnet-tier) | ~5 s |
| 5 | 2000ms (opus-tier) | ~20 s |

---

## 3. Grader 延迟对照表

**2026-05 measurement; may drift.** 下面的延迟数字 *不* 来自自动化
基线,而是手工实测、约 N=5 次取中位数。每次新 Anthropic 模型 release
时由维护者手工 refresh。

| 模型 | temperature | 中位延迟 / 单次调用 | 备注 |
|---|---:|---:|---|
| `claude-haiku-4-5` | 0.0 | ~150 ms | 适合 string-fail-then-fallback 场景 |
| `claude-sonnet-4-6` | 0.0 | ~600 ms | 默认推荐;`CHARTER_MODEL` 默认 |
| `claude-opus-4-7` | 0.0 | ~2.0 s | semantic 严格判定时选 |

测试 prompt:`charter/prompts.py:CHAIN_SEMANTIC_GRADER_SYSTEM` + 一条
代表性 parent/child clause 对。延迟测量含网络 RTT(美东 → US-East
endpoint)。

如果你跑 live benchmark(`pytest -m live`),拿到的数字应在上述
区间附近。**显著偏离时** 请更新本表,**不要** 改 fake grader 的
模拟值(那样会篡改基线测试的语义)。

---

## 4. Performance Regression 政策

- **CI 默认不强制 perf**。Perf bench 是 `[bench]` 提交触发的 opt-in
  job;失败不阻塞合并。
- **Release 前手工 review**。打 tag 前,维护者在本地跑
  `pytest benchmarks/ --benchmark-only`,与上面 §2 的数字对比。
- **10x 以上的退化必须解释**。PR 描述里说明:是 trade-off
  (e.g. 加了 crash safety)还是 bug。Trade-off 要更新本文档相应数字
  + 简要解释。
- **新 benchmark 要更新本文档**。加 benchmark 文件不更新 §2 的表 ≈
  benchmark 不存在。

---

## 5. 关联 ADR

- **ADR-001(无数据库)**: `data/transparency.log` 的 O(N) growth +
  `_atomic_append_line` 的 O(N) per-append 是这条 ADR 的直接代价。
  N=10000 的数字是"什么时候该考虑换设计"的量化信号。
- **ADR-009(MCP 不主动调 LLM)**: grader 延迟表是"协议外可选 LLM
  调用"的成本测算 —— 让运维者评估开启 semantic mode 的代价。
- **ADR-010(chain verify string-based)**: §2.4 同时给 strict 和
  semantic 的数字,让运维者看到 string mode 的速度优势,理解为什么
  default 还是 string。

---

## 6. 不做的事(对齐 Issue #43)

- 不引入 prometheus metric exporter(B2.7 OTel 的范围)。
- 不做 web-based perf dashboard。
- 不强制 perf regression 阻塞 CI。
- 不跨 Python 版本对比(默认 3.12;3.13 等价跑)。
