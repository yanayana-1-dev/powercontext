# 赛题五实现说明：HTTP 重排服务接入与跨批次排序

本文说明 `retrieval-01-reranking`（HTTP 重排服务接入与跨批次排序）在 PowerContext 中的实现思路与验证方式。

## 1. 题目与基线

- 赛题编号：`retrieval-01-reranking`
- 冻结版本：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 开发分支：`zzd`
- 目标：实现 `powercontext.builtin.artifacts.memory.http_reranker.HTTPMemoryReranker`，满足现有 `MemoryReranker` 契约，通过 `memory_reranker` 接入 Runtime 的真实 Memory 搜索。

## 2. 新增内容

| 文件 | 作用 |
| --- | --- |
| `src/powercontext/builtin/artifacts/memory/http_reranker.py` | `HTTPMemoryReranker` 与请求/响应校验 |
| `tests/test_http_reranker.py` | 分批、全局排序、schema、错误传播、usage 与原生集成回归测试 |
| `docs/zzd/retrieval-01-reranking.md` | 本文 |

基线源码零改动；复用 `MemoryReranker` 协议、`MemoryRerankDecision`、`InferenceUsage` 与 Runtime 的 `memory_reranker` 装配。

## 3. 接口与协议

```python
HTTPMemoryReranker(
    endpoint: str,
    *,
    client: httpx.AsyncClient,
    batch_size: int = 32,
    policy_id: str = "http-cross-encoder-v1",
)
```

`async rerank(query, candidates, limit)` 返回 `MemoryRerankDecision`。

请求（每批）：

```json
{"query": "...", "candidates": [{"rank": 1, "text": "..."}]}
```

响应：

```json
{"scores": [{"rank": 1, "score": 0.75}]}
```

- `rank` 始终是候选在**完整输入池**中的一基序号，分批后不重新编号。
- 请求只包含 `query`、`candidates`，每个 candidate 恰好包含 `rank` 和 `text`。
- 响应顶层恰好包含 `scores`；每条 score 恰好包含 `rank` 和 `score`。

## 4. 实现行为

1. **参数校验**：候选为空、`batch_size <= 0`、`limit` 不在 `[1, len(candidates)]` 内，均抛 `ValueError`。
2. **分批**：按原顺序每 `batch_size` 个候选一批；每批使用全局 rank，正常完成时请求数恰为 `ceil(len(candidates) / batch_size)`。
3. **响应校验**：支持乱序返回；每批必须恰好覆盖本批全部 rank 一次。缺失、重复、跨批 rank、顶层或记录级额外字段、非整数 rank、非数值/NaN/Infinity score 都抛 `ValueError`。
4. **全局选择**：合并所有批次的分数后统一排序：score 降序，平分按原始全局 rank 升序；取前 `limit` 个 rank。各批不会分别截断。
5. **身份保持**：只返回原候选的 rank，Memory 搜索据此投影回原 `MemoryHit`，正文、entry ID、entry version ID 不变。
6. **错误传播**：非 2xx 通过 `response.raise_for_status()` 保留 `httpx.HTTPStatusError`；网络错误原样传播；非法 JSON/schema 转 `ValueError`。
7. **usage**：`InferenceUsage(requests=<实际 HTTP 请求数>)`，不使用估算值。

## 5. 原生 Memory 集成

Runtime 已支持 `open_builtin_contexts(..., memory_reranker=...)` / `memory_reranker` 配置。`MemoryService.search` 会把 coarse pool（默认 30，可配置到 100）交给重排器，再用 `selected_ranks` 投影回原 hit。因此本实现无需修改 Runtime：

- `MemoryRerankTrace.candidate_hits` 保留完整候选池；
- `selected_ranks` 是全局排序后的原始 rank；
- 最终 `hits` 与 `candidate_hits` 中的对应条目版本完全一致；
- `result.rerank.usage.requests` 与实际批次数一致。

## 6. 验证

官方公开自测：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src;D:\workspace\_comp\powercontext-2026-competition\competition"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
python -m pytest -q -c pytest.ini --confcutdir=. challenges/retrieval-01-reranking/acceptance
```

结果：**10 passed**，覆盖分批上限、全局排序与平分、乱序响应、缺失/重复/跨批 rank、额外字段、NaN/Infinity、参数边界、网络错误传播，以及真实 Memory 搜索中的身份保持。

产品侧测试：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src"
python -m pytest -q tests/test_http_reranker.py -p no:cacheprovider
```

结果：**17 passed**。

## 7. 质量输入说明

正式评测的 SciFact 质量门槛（173 篇摘要、20 条 claim、384 维离线向量、`batch_size=7`、top 10）由离线 HTTP 服务提供分数。客户端只负责：

1. 以 7 为批上限发送全部 173 个候选（25 次请求，rank 1–173 各一次）；
2. 校验每批响应并合并；
3. 按服务分数全局稳定排序，取前 10。

因此返回顺序与服务分数决定的全局顺序一致，Recall@10 / MRR@10 与服务的评分质量一致，不受批次划分或批次发送顺序影响。

## 8. 边界清单

| 场景 | 行为 |
| --- | --- |
| 空候选 / `batch_size <= 0` / 非法 limit | `ValueError` |
| 响应乱序 | 接受，按 rank 映射 |
| 缺失、重复、跨批 rank | `ValueError` |
| 顶层或 score 记录额外字段 | `ValueError` |
| rank 非整数 / score 非数值 / NaN / Infinity | `ValueError` |
| 非 2xx | `httpx.HTTPStatusError` |
| 网络错误 | 原样传播 httpx 异常 |
| 非法 JSON | `ValueError` |
| 平分 | 原始全局 rank 升序 |
| 后续批次高分 | 参与同一次全局选择，可进入最终结果 |
| usage | 实际请求数 `ceil(n / batch_size)` |
