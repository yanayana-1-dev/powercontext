# 赛题八实现说明：Embedding 并发请求合并与向量缓存

本文说明 `memory-02-embedding-batches`（Embedding 并发请求合并与向量缓存）在 PowerContext 中的实现思路与验证方式。

## 1. 题目与基线

- 赛题编号：`memory-02-embedding-batches`
- 冻结版本：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 开发分支：`zzd`
- 目标：同一事件循环内共享一个模型包装器，合并并发调用与执行中的相同文本、缓存成功向量，并保持原始顺序、usage、取消隔离和局部失败语义；真实 Memory 写入与向量搜索通过包装器完成。

## 2. 新增接口

`powercontext.builtin.inference.coalescing.CoalescingEmbeddingModel`：

```python
model = CoalescingEmbeddingModel(
    base_model,
    max_batch_size=64,
    coalesce_ms=5.0,
    cache_size=1024,
)
result = await model.embed(texts)
```

- `profile` 直接暴露底层模型 profile。
- `max_batch_size` 是每次底层请求的去重文本上限。
- `coalesce_ms` 为合并窗口，支持 0。
- `cache_size` 为成功向量 LRU 容量，0 表示关闭跨调用缓存。
- 参数非法（非正整数、负值、NaN/Infinity 等）抛 `ValueError`。

## 3. 实现结构

- **按文本共享**：wrapper 维护 `text -> _Entry(future, batch_id, usage)`；同一文本无论在同一调用、后续调用还是执行中的批次里，都等待同一个 future。
- **合并窗口**：新文本进入唯一队列并调度一次 flush；`coalesce_ms > 0` 时等待窗口，`0` 时只让出一次事件循环，使同一轮并发调用先汇合。
- **批次**：flush 每次从队列取出最多 `max_batch_size` 个唯一文本，创建一个独立底层请求；剩余文本再调度下一批。每个文本只进入一个批次。
- **调用内去重与顺序**：调用先对输入去重，再按原输入（含重复位置）从文本→向量映射组装 `vectors`。
- **LRU 缓存**：只缓存成功向量；命中时 `move_to_end`；超过 `cache_size` 淘汰最久未使用。`cache_size=0` 不缓存，但不影响执行中共享。
- **usage**：每个调用记录自己等待过的不同 `batch_id`，每个批次只汇总一次；把 provider 的 `requests` 与 token 数相加；纯缓存命中返回 `requests=0`、token 为 `None`。
- **失败**：底层异常原样设置到该批次每个等待者，失败结果不缓存，条目被移除以便重试；其他批次任务独立继续。
- **向量数量**：底层 `vectors` 数量与批次文本数不一致时抛 `ValueError`，并同样传播给等待者。
- **取消隔离**：调用方通过 `asyncio.shield(entry.future)` 等待；取消调用只取消它自己的等待，不取消共享 future，也不取消独立创建的批次任务。

## 4. 原生集成

`open_builtin_contexts(..., embedding_model=model)` 可直接接收包装器。Memory 写入的索引 embedding 与向量搜索的 query embedding 共用该 wrapper：

- 6 个 scope 并发写入同一组 24 条文本、`max_batch_size=6`、`coalesce_ms=0`、`cache_size=48` 时，24 个唯一文本恰好形成 4 个底层批次，每条文本只发送一次；
- 随后 6 次相同文本的向量查询全部缓存命中，不新增底层请求；
- 向量搜索仍能命中对应正文，entry/citation 身份不变。

## 5. 验证

官方公开自测：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src;D:\workspace\_comp\powercontext-2026-competition\competition"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
python -m pytest -q -c pytest.ini --confcutdir=. challenges/memory-02-embedding-batches/acceptance
```

结果：**8 passed**。覆盖并发去重与顺序、batch 上限、空输入、LRU、缓存关闭、失败重试、取消隔离、usage、向量数量校验、原生向量索引，以及 6 scope / 24 文本 / 4 次底层请求的预算。

产品侧测试：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src"
python -m pytest -q tests/test_coalescing.py -p no:cacheprovider
```

结果：**7 passed**。

## 6. 设计决策

- **future 共享而不是缓存请求对象**：同一文本的等待者直接共享 future，完成即从映射移除；是否长期复用由 LRU 缓存决定，执行中共享与跨调用缓存解耦。
- **独立批次任务**：批次由 wrapper 自己创建 task，调用方只等待 per-text future；取消调用不会传播到共享工作。
- **每次 flush 只发一个批次**：实现简单、严格保证批次上限，并让后续文本自然进入下一窗口；总请求数仍由唯一文本数 / `max_batch_size` 决定。
- **usage 按批次去重**：一个调用引用同一批次的多个文本只计一次；不同批次可分别计入多个调用。

## 7. 边界清单

| 场景 | 行为 |
| --- | --- |
| 空输入 | 不调用底层，`requests=0` |
| 同一调用重复文本 | 只请求一次，按原位置返回重复向量 |
| 并发调用相同文本 | 共享同一 future/批次 |
| 晚到调用遇到执行中批次 | 加入等待，不新增请求 |
| `cache_size=0` | 不跨调用缓存，执行中仍共享 |
| LRU 命中/淘汰 | 命中刷新顺序，容量有界 |
| 底层失败 | 异常传播给该批等待者，不缓存，可重试 |
| 向量数量不一致 | `ValueError` |
| 取消一个等待者 | 共享工作与其他等待者不受影响 |
| 多批次调用 usage | 每个不同批次计一次并累加 |
| 纯缓存命中 | `requests=0`，token 为 `None` |
