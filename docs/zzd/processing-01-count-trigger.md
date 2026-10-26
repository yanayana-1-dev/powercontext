# 赛题七实现说明：按新增来源条目数触发记忆处理

本文说明 `processing-01-count-trigger`（按新增来源条目数触发记忆处理）在 PowerContext 中的实现思路与验证方式。

## 1. 题目与基线

- 赛题编号：`processing-01-count-trigger`
- 冻结版本：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 开发分支：`zzd`
- 目标：当某个 scope 的 Memory / Topic Memory 待处理 Source 条目数达到配置门槛时，自动启动一次原有处理调用，无需显式 flush 或等待时间条件；计数、窗口、重启与失败重试均复用既有持久化语义。

## 2. 新增配置

`RuntimeConfig` 新增两个字段，默认 `None`（关闭数量触发）：

```python
memory_source_count_threshold: int | None = None
topic_memory_source_count_threshold: int | None = None
```

`mode="before"` 校验拒绝布尔值、字符串、浮点数、0 与负数，只接受严格正整数或 `None`。`BuiltinConfig`/`RuntimeConfig` 的既有别名与校验规则不受影响。

## 3. 计数语义

对每个 **scope × family** 独立计算：

```text
待处理数 = COUNT(pc_sources WHERE journal_position > 该 family 成功游标)
```

- 首次处理游标为 0，统计该 scope 的全部 journal 条目。
- 计数基于 `pc_sources` 的**持久化 journal 身份**，不看正文长度、token 或生成结果数。
- 同一身份重复 capture 不会新增 row/position，因此不增加待处理数。
- 已有 Source 的 receipt 信任升级只修改现有 row，不增加计数。
- 仅用于 lineage 的服务端 Source 同样占一个 journal row，参与计数，但仍按原有规则从生成证据中排除。
- 成功后 family 游标前移，已消费的历史条目不会再计入门槛；不同 scope 与 Memory/Topic Memory 的游标互不替代。

## 4. 触发与消费

`ArtifactProcessingBinding` 新增 `automatic_source_count_threshold`，由 `composition` 从 RuntimeConfig 注入到 `memory` 与 `topic-memory` binding。

Supervisor 在每个 family 的周期内执行独立的 count discovery：

1. 按 `scope_id` keyset 分页读取 `pc_source_journal_heads`。
2. 对每个 scope 读取对应 binding 的 `SourceCursor`，统计游标之后的 `pc_sources` 行数。
3. 待处理数达到门槛、且没有已接受未完成的请求（`requested_generation <= handled_generation`）时，通过既有 `ArtifactProcessingIntentRepository.request` 接受一次调用，加入 supervisor ready 队列。
4. Worker 仍按既有 `source_window_limit` / `topic_memory_source_window_limit` 读取，不改变单次处理规模。
5. Memory 成功后按新 cursor 重新判断剩余数量：仍达到门槛就再接受一次，低于门槛则等待；Topic Memory 的已接受 wave 继续按原有分窗行为完成，不把门槛施加到后续窗口。

触发时机：

- 源码 capture 成功后主动 `wake` supervisor；同时配置门槛时 supervisor 以 1 秒周期兜底轮询，覆盖 `create_artifact` 产生的 lineage Source 等非 capture 路径。
- 数量条件与既有时间条件（`*_schedule_seconds` / cron）是 OR：任一满足即可启动；门槛为 `None` 时完全保持原有时间行为。
- 显式 `memory.for_scope(...).flush()` / `topic_memory.for_scope(...).flush()` 继续走既有 requested intent，绕过数量门槛。

## 5. 重启与失败

- 不足门槛的输入只停留在 journal/pending 中；重启后 count discovery 重新按 cursor 计算，不会提前处理。
- 已达到门槛但尚未接受：重启后 supervisor 首次 cycle 即接受并执行。
- 已接受但失败：requested > handled 的 intent 保留；既有 requested discovery 与 retry 机制继续重试，失败不推进成功 cursor。
- 成功后 cursor 前移，历史条目不会重新触发。
- 计数按 scope/family 独立，Memory 与 Topic Memory 的 cursor、门槛、working set 互不影响。

## 6. 验证

官方公开自测：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src;D:\workspace\_comp\powercontext-2026-competition\competition"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
$env:PYTHONUTF8="1"   # 仅 Windows 本地读取 UTF-8 fixture 需要；Linux 默认 UTF-8
python -m pytest -q -c pytest.ini --confcutdir=. challenges/processing-01-count-trigger/acceptance
```

产品侧测试：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src"
python -m pytest -q tests/test_count_trigger.py -p no:cacheprovider
```

结果：**15 passed**，覆盖默认关闭、严格正整数、拒绝布尔/浮点/字符串/零/负数，以及 binding 层门槛校验。

> 本地 Windows 的 multiprocessing `spawn` 启动原生 worker 明显慢于评测 Linux 环境；公开用例为 worker 配置了 30 秒超时。逻辑验证时使用独立测试副本把该超时与等待窗口放大，确认了 Memory 单窗/重复 capture、Topic wave、scope/family 隔离、时间 OR、显式 flush、lineage 计数、重启与失败重试等行为；提交的源码未改动测试配置。
>
> 官方原配置下本机以 `PYTHONUTF8=1` 运行，前两个用例（配置校验、Memory 门槛/单窗/重启）通过；Topic 用例因本机 spawn 启动超过 30 秒 worker 超时而中止，扩展超时后同一逻辑可完整通过。评测目标为 Linux x86_64，worker 启动远快于该超时。

## 7. 设计决策

- **复用 intents/admission**：数量触发只负责“决定何时接受一次调用”，执行、窗口、重试、cursor 与引用读取全部沿用既有 Worker，避免另建队列或 checkpoint。
- **按 journal 行计数**：`pc_sources` 的 position 是持久化身份；重复 capture 与 receipt 升级不新增行，天然满足题面。
- **level re-evaluation**：每次成功后都按新 cursor 重新计数，因此积压足够时可以连续按窗口消费，低于门槛即静默等待。
- **wake + 1s 兜底轮询**：capture 路径即时唤醒；非 capture 的 lineage 写入与重启场景由周期扫描覆盖，不依赖额外的持久化触发器。
- **不修改公开 flush/时间调度**：数量条件作为额外的 admission 通道，既有行为保持兼容。

## 8. 边界清单

| 场景 | 行为 |
| --- | --- |
| 门槛为 `None` | 保持原有时间/flush 行为 |
| 待处理数 < 门槛 | 不启动，静默等待 |
| 重复 capture / receipt 升级 | 不增加计数 |
| lineage-only Source | 计入 journal 数量，但不进入生成证据 |
| Memory 单窗后剩余 ≥ 门槛 | 重新接受下一次调用 |
| Memory 单窗后剩余 < 门槛 | 等待新增输入、时间条件或 flush |
| Topic 已接受 wave | 按原分窗完成，不重新施加门槛 |
| 重启时不足门槛 | 不处理 |
| 重启时达到门槛/已接受未成功 | 恢复处理，失败不丢输入 |
| 多 scope / 多 family | cursor、门槛、状态彼此独立 |
