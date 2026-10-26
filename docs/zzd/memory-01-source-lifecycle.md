# 赛题九实现说明：来源撤回后的记忆停用与恢复

本文说明 `memory-01-source-lifecycle`（来源撤回后的记忆停用与恢复）在 PowerContext 中的实现思路与验证方式。

## 1. 题目与基线

- 赛题编号：`memory-01-source-lifecycle`
- 冻结版本：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 开发分支：`zzd`
- 目标：调用方显式注册 Source/Memory/Topic Memory 的证据依赖 DAG；Source 撤回或恢复时按各层 `all`/`any` 策略传播，使原生 Memory 列表/搜索/Context 与 Topic Memory 搜索/Context 同步变化，同时保留历史引用供审计。

## 2. 新增接口

`powercontext.builtin.runtime.source_lifecycle` 导出：

- `SourceLifecycleError(ValueError)`
- `SourceLifecycleEvent`：`extra="forbid"`、`frozen=True`、`strict=True`；`node_id`/`reason` 非空且不能只有空白，`revision`/`sequence` 为正整数，`available` 为严格 bool。
- `SourceLifecycleApplication` / `SourceLifecycleService`

Runtime 新增 `runtime.lifecycle.for_scope(scope_id)`：

```python
service = runtime.lifecycle.for_scope(scope_id)
await service.register_source("source-a", source_ref, revision=3)
await service.register_memory("memory-a", citation, parents=("source-a",), policy="all")
await service.register_topic("topic-a", artifact_ref, parents=("memory-a",), policy="any")
active_ids = await service.apply(event)
reference = await service.resolve("memory-a", purpose="generation")
audit_reference = await service.resolve("memory-a", purpose="audit")
```

`apply()` 返回按节点编号排序的可用节点列表。

## 3. 图与持久化

新增 `pc_source_lifecycle` 表（`SHARED_TABLES`，随 SQLite/其他 profile 的 `create_all` 自动创建）：

- `scope_id` + `node_id` 主键，`kind` 为 `source`/`memory`/`topic`。
- `reference`、`parents` 为 JSON 文本；`policy`、`revision`、`available`、`sequence`、`last_available`、`last_reason`、`ordinal`。
- `artifact_id`/`artifact_revision` 为 Topic 去冗余字段，供搜索过滤。
- 每个 scope 的 `ordinal` 唯一，注册顺序即拓扑顺序（父节点必须先注册）。

注册：

- 节点编号 scope 内唯一；相同绑定幂等，不同绑定抛 `SourceLifecycleError`。
- Source 校验 `SourceRef` 可读；Memory 校验 `MemoryCitation` 的精确修订与 entry version；Topic 校验 `ArtifactRef` 为 `topic-memory` 且可精确读取。
- 派生节点至少一个父节点、父节点必须已注册、不得重复、不得自环；父节点按 `ordinal` 保证 DAG。
- 注册时立即按父节点当前可用性计算状态，不默认可用。

## 4. 事件与传播

事件只作用于 Source：

- `revision` 必须精确匹配该 Source 注册时的生命周期版本。
- `sequence` 单调递增：低序号报错；同序号且 `available`/`reason` 完全相同为幂等重放；同序号不同内容报错。
- 撤回/恢复都重新按 `all`/`any` 计算所有后代状态，并在同一事务内更新整个 scope 的节点状态。
- 幂等重放不修改事件行，但仍会重放原生状态同步；因此成功重放不会新增 Memory 修订，而中断后的重试可以补齐未完成的传播。

## 5. 原生集成

### Memory

- 对每个 Memory 节点读取当前 Memory head，按 citation 的 `entry_id`/`entry_version_id` 定位 manifest entry。
- 目标为 `active` 且当前非活跃时调用 `MemoryService.reactivate`；目标为 `inactive` 且当前活跃时调用 `MemoryService.forget`。
- 只改状态、不改正文；`memory.get(citation)` 仍可读取原始修订，`resolve(..., purpose="audit")` 始终返回原始 `MemoryCitation`。
- 状态已符合目标时跳过写入，因此重放和重复传播不会产生新修订。

### Topic Memory

- 不删除 Topic Artifact、publication 或 active 投影，避免触发原生存储完整性约束和破坏混合检索索引。
- 在 `TopicMemoryRepository.search` 融合后按 `pc_source_lifecycle` 中 `kind='topic' and available=0` 的 `(artifact_id, revision)` 过滤命中；恢复后过滤解除。
- 因此 `runtime.topic_memory.search`、Context 的 topic-memory section 会同步反映状态，而 `records.get_artifact_revision("topic-memory", ...)` 始终可精确读取原始 ArtifactRef。

## 6. 失败、重放与重启

- 事件行与节点状态先提交，再执行原生 Memory 状态同步；原生写入失败时 `apply` 抛错，但生命周期状态已持久化。
- 同一事件重试（同序号同内容）走幂等分支，并重新对全部 Memory 节点做状态对齐：已完成的跳过，未完成的补齐。
- 成功事件重放比较 manifest 当前状态，全部已符合则不写 Memory，修订号不变。
- 所有图、事件和状态在 `pc_source_lifecycle` 中持久化；重启后 `for_scope` 重新加载，`resolve` 与原生列表/搜索/Context 保持一致。
- 每个 scope 独立；不同 scope 中相同 `node_id` 互不影响。

## 7. 验证

官方公开自测：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src;D:\workspace\_comp\powercontext-2026-competition\competition"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
$env:PYTHONUTF8="1"   # 仅 Windows 本地读取 UTF-8 fixture 需要
python -m pytest -q -c pytest.ini --confcutdir=. challenges/memory-01-source-lifecycle/acceptance
```

结果：**4 passed**。覆盖 20 节点多层 `all`/`any`、撤回/恢复、原生 Memory 列表/搜索/Context、Topic 搜索/Context、精确历史引用、重启、事件版本/序号、注册冲突、撤回后注册的派生节点、共享 Topic 与独立事实。

产品侧测试：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src"
python -m pytest -q tests/test_source_lifecycle.py -p no:cacheprovider
```

结果：**5 passed**，覆盖事件字段严格校验、DAG 传播与 generation/audit、注册非法图/引用、事件重放无新修订、scope 隔离与重启。

另外手工验证了：

- 同一事件成功重放后 Memory `memory_ref` 不变；
- 用 SQLite trigger 中断 Memory 写入后，同一事件重试可完成传播；
- 重启后状态、audit 引用与 generation 拒绝一致。

## 8. 设计决策

- **显式 DAG + 拓扑序号**：不解析自然语言依赖；父节点先注册，`ordinal` 即拓扑顺序，传播一次线性扫描完成。
- **事件先持久化、原生后同步**：使中断后的同一事件重试成为恢复入口，而不是要求新的序号。
- **幂等重放仍对账**：重放不写事件，但重新检查所有 Memory 节点状态，既保证“无新修订”，又保证“失败重试可补齐”。
- **Topic 采用读侧过滤**：不改原生 Topic 投影，避免破坏 publication/active/chunks 的存储不变量与向量索引；搜索与 Context 的可观察行为满足撤回/恢复要求。

## 9. 边界清单

| 场景 | 行为 |
| --- | --- |
| 相同注册 | 幂等，无重复节点 |
| 不同绑定注册同编号 | `SourceLifecycleError` |
| 缺失父节点/重复父节点/自环/空父节点 | `SourceLifecycleError` |
| Source 引用不存在 | `SourceLifecycleError` |
| Memory 引用不存在/entry 不匹配 | `SourceLifecycleError` |
| Topic 引用不存在/非 topic-memory | `SourceLifecycleError` |
| 未知 Source 事件 | `SourceLifecycleError` |
| revision 不匹配 | `SourceLifecycleError` |
| 低 sequence / 同 sequence 不同内容 | `SourceLifecycleError` |
| 同 sequence 完全相同 | 幂等，重放对齐 |
| 撤回/恢复 | 按 all/any 传播到全部后代 |
| Memory generation | 可用返回原始 citation，不可用报错 |
| Memory audit | 始终返回原始 citation |
| Topic generation | 可用返回原始 ArtifactRef，不可用报错 |
| Topic audit | 始终返回原始 ArtifactRef |
| 重启 | 图、事件、状态、原生可见性一致 |
| 不同 scope 同编号 | 互不影响 |
| 成功事件重放 | 不新增 Memory 修订 |
| 传播中断后重试 | 完成剩余 Memory 状态写入 |
