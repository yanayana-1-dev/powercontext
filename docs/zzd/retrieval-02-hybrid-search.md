# 赛题十一实现说明：seekdb 混合检索的单 SQL 候选查询

本文说明 `retrieval-02-hybrid-search`（seekdb 混合检索的单 SQL 候选查询）在 PowerContext 中的实现思路与验证方式。

## 1. 题目与基线

- 赛题编号：`retrieval-02-hybrid-search`
- 冻结版本：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 开发分支：`zzd`
- 目标：保持 FTS-only、vector-only 行为不变，把 seekdb/OceanBase 的 hybrid 候选检索从“两次 SQL + 应用层拼接”改为**恰好一条原生 statement**，同时保持 scope/artifact/tag 过滤、当前 head、增量投影、状态变化、引用身份和逐查询检索质量。

## 2. 实现结构

### 统一候选 SQL

`OceanBaseMemoryVectorIndex` 新增 `search_hybrid()`，使用一条带 `UNION ALL` 的 statement：

```sql
(SELECT 'fts' AS channel,
        h.memory_artifact_id, h.head_revision, h.entry_id, h.entry_version_id,
        v.text, NULL AS distance
 FROM pc_memory_entry_heads AS h
 JOIN pc_memory_entry_versions AS v
   ON v.scope_id = h.scope_id
  AND v.memory_artifact_id = h.memory_artifact_id
  AND v.entry_version_id = h.entry_version_id
 WHERE h.scope_id = :scope_id
   AND h.memory_artifact_id IN :memory_artifact_ids
   AND MATCH(h.searchable_text) AGAINST(:query)
 ORDER BY MATCH(h.searchable_text) AGAINST(:query) DESC,
          h.memory_artifact_id, h.entry_id, h.entry_version_id
 LIMIT :candidate_limit)
UNION ALL
(SELECT 'vector' AS channel,
        m.memory_artifact_id, m.head_revision, m.entry_id, m.entry_version_id,
        v.text, l2_distance(m.embedding, :query_vector) AS distance
 FROM pc_memory_vector_entries AS m
 JOIN pc_memory_entry_versions AS v
   ON v.scope_id = m.scope_id
  AND v.memory_artifact_id = m.memory_artifact_id
  AND v.entry_version_id = m.entry_version_id
 WHERE m.scope_id = :scope_id
   AND m.memory_artifact_id IN :memory_artifact_ids
 ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE
 LIMIT :candidate_limit)
```

- 每个分支保留原有的 scope、Memory artifact、tag filter 与 `candidate_limit`。
- FTS 分支沿用 `pc_memory_entry_heads` 的 FULLTEXT 索引、原排序与候选上限；vector 分支沿用 `pc_memory_vector_entries` 的 HNSW `l2_distance(...) APPROXIMATE`、原排序与候选上限。
- 结果按 `channel` 拆回 `MemorySearchChannels.fts` / `.vector`，再由原有融合逻辑处理；因此候选集合、通道标识、融合输入与冻结双查询路径逐项一致。
- Tag filter 使用参数化 `memory_tag_sql("h")` / `memory_tag_sql("m")`；带 tag 的 vector 分支去掉 `APPROXIMATE`，与既有 tagged vector SQL 保持一致。

### Composite 调度

`CompositeMemoryIndex.search` 在 `mode == "hybrid"` 且同时具备 FTS/vector capability 时，优先调用实现了 `search_hybrid` 的索引；其他后端（如 SQLite）没有该方法，继续使用原有两次查询路径。FTS-only / vector-only 的路径完全不变。

### 当前 head 与状态

`search_hybrid` 复用 `vector_complete`：

- 校验请求 Memory head 仍是当前 head；历史 head 抛 `CapabilityNotSupportedError("head")`。
- 校验 entry heads 与 vector projection 的 entry/version/content hash 一致，且 embedding profile hash 正确。
- 返回的 `MemoryChannelHit.memory_ref` 由 `memory_channel_hits` 使用**请求中已验证的当前 head**，不读取旧投影的 `head_revision`；未修改条目可以保留旧 projection 的 revision。
- 正文来自 `pc_memory_entry_versions` 的精确 entry version；去重/融合仍由 MemoryService 完成。

## 3. 兼容性

- `mode="fts"`：`OceanBaseMemoryFTSIndex.search` 不变。
- `mode="vector"`：`OceanBaseMemoryVectorIndex.search` 不变。
- `mode="hybrid"` + 无 seekdb/OceanBase 扩展：Composite 回退旧路径。
- 索引写入、初始化、hydrate、vector_complete 均不变。
- 过滤、tag、limit、head 校验语义与旧路径一致。

## 4. 验证

### seekdb 原生公开自测

本机 Windows 无 `pylibseekdb`，使用 WSL Ubuntu x86_64 + 已安装的固定依赖运行真实嵌入 seekdb：

```bash
cd /mnt/d/workspace/_comp/powercontext-2026-competition/competition
PYTHONPATH=/mnt/d/workspace/powercontext-zzd/src:/mnt/d/workspace/_comp/powercontext-2026-competition/competition \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONUTF8=1 \
python3 -m pytest -q -c pytest.ini --confcutdir=. \
  challenges/retrieval-02-hybrid-search/acceptance
```

结果：**1 passed**。该公开测试覆盖：

- hybrid 候选检索恰好一条 statement，且 statement 同时含 `pc_memory_entry_heads`、`pc_memory_vector_entries`、`AGAINST`、`l2_distance`；
- `EXPLAIN` 同时出现 `TEXT RETRIEVAL SCAN` 与 `VECTOR INDEX`；
- 结果命中 alpha、`matched_by=("fts","vector")`、引用当前 revised head；
- 重复查询身份稳定；
- 历史 head 被 `CapabilityNotSupportedError` 拒绝；
- 不同 scope 隔离；
- tag filter；
- 正文修订、停用、恢复后的当前版本与正文一致；
- 未修改条目仍可检索；
- FTS-only / vector-only 行为正常。

### 其他验证

- 官方其余已实现题目公开自测（Windows，不含 count 本地超时项）：**77 passed**。
- 产品回归测试：**98 passed**。
- `ruff check` / `ruff format --check`：通过。

质量方面，统一 SQL 的两个分支分别复刻冻结 FTS / vector 的 WHERE、ORDER BY、LIMIT 与候选上限，再交给未修改的 fusion；因此同库、同 scope/head/query/embedding 下的候选通道与旧双查询路径一致，Recall@10/MRR@10 不会因合并而改变。`probe`/延迟门槛需在正式 seekdb 语料上测量；本实现把两次往返降为一次候选 statement，不会增加查询次数。

## 5. 设计决策

- **UNION ALL 而非应用层拼接**：两个有序子查询各自保留 `ORDER BY ... LIMIT`，一条 statement 返回两路候选，满足“恰好一条候选 SQL”。
- **在 vector 索引类上实现 hybrid**：vector 索引已持有 embedding profile、vector SQL 和完整性校验；hybrid 只在 `CompositeMemoryIndex` 调度层组合，不引入新的数据库抽象。
- **复用 `vector_complete`**：历史 head、投影完整性和 embedding profile 校验沿用既有契约，避免另写一套易漂移的校验。
- **保留 Fusion**：不改变 MemoryService 的融合/排序/引用逻辑，只改变候选获取方式，降低质量与身份回归风险。
- **其他后端回退**：SQLite 等没有原生单 SQL hybrid 的后端保持旧两次查询，避免为本题破坏平台无关路径。

## 6. 边界清单

| 场景 | 行为 |
| --- | --- |
| hybrid 候选查询 | 恰好一条 statement，含 FTS + vector 两路 |
| EXPLAIN | `TEXT RETRIEVAL SCAN` + `VECTOR INDEX` |
| scope/artifact/tag/limit | 两分支均在数据库内过滤 |
| 当前 head | 请求 ref 为准；历史 head 拒绝 |
| 增量投影旧 revision | 不因此排除仍活跃条目 |
| 正文修订/停用/恢复 | 返回当前有效 entry version 与正文 |
| 重复查询 | 身份与顺序稳定 |
| 同 entry 双通道 | 融合后 matched_by 含 fts/vector，不重复身份 |
| FTS-only / vector-only | 原行为不变 |
| SQLite 等非 seekdb | Composite 回退原路径 |
| 无 vector capability | 不走 hybrid 单 SQL 路径 |
