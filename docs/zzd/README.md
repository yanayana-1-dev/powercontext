# zzd 分支开发说明

本目录记录 `zzd` 分支针对 2026 PowerContext 大赛赛题二至九的设计与验证过程。

## 分支基线

- 基线：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 分支：`zzd`
- 实现范围：在保留基线公共接口与兼容行为的前提下，新增各赛题能力；核心改动集中在 Source、Runtime、Memory/Inference 与 Topic 检索的扩展点

## 已实现赛题

| 赛题 | 编号 | 实现 | 设计说明 |
| --- | --- | --- | --- |
| 赛题二 | `source-01-table-evidence` | `src/powercontext/sources/csv_rows.py` | [source-01-table-evidence.md](source-01-table-evidence.md) |
| 赛题三 | `source-02-pi-history` | `src/powercontext/sources/pi_session.py` | [source-02-pi-history.md](source-02-pi-history.md) |
| 赛题四 | `source-03-pdf-retrieval` | `src/powercontext/sources/pdf_pages.py` | [source-03-pdf-retrieval.md](source-03-pdf-retrieval.md) |
| 赛题五 | `retrieval-01-reranking` | `src/powercontext/builtin/artifacts/memory/http_reranker.py` | [retrieval-01-reranking.md](retrieval-01-reranking.md) |
| 赛题六 | `context-02-incremental-pack` | `src/powercontext/builtin/runtime/context_pack.py` | [context-02-incremental-pack.md](context-02-incremental-pack.md) |
| 赛题七 | `processing-01-count-trigger` | `src/powercontext/builtin/runtime/artifact_processing.py` | [processing-01-count-trigger.md](processing-01-count-trigger.md) |
| 赛题八 | `memory-02-embedding-batches` | `src/powercontext/builtin/inference/coalescing.py` | [memory-02-embedding-batches.md](memory-02-embedding-batches.md) |
| 赛题九 | `memory-01-source-lifecycle` | `src/powercontext/builtin/runtime/source_lifecycle.py` | [memory-01-source-lifecycle.md](memory-01-source-lifecycle.md) |

## 交付内容

- `src/powercontext/sources/csv_rows.py`：CSV 行级 Source、解析、identity、Definition 与 text-evidence 投影
- `src/powercontext/sources/pi_session.py`：Pi 会话分支解析、事件 Source、增量 connector 与 checkpoint
- `src/powercontext/sources/pdf_pages.py`：PDF 全页解析、页级 Source、精确证据页检索
- `src/powercontext/builtin/artifacts/memory/http_reranker.py`：HTTP 分批重排、跨批次全局稳定排序
- `src/powercontext/builtin/runtime/context_pack.py`：Context Pack/Delta wire 协议、分块、校验与重建
- `src/powercontext/builtin/runtime/application.py`：`prepare_incremental`、capture 后唤醒处理与 lifecycle 应用
- `src/powercontext/builtin/runtime/artifact_processing.py`：按 journal 条目数的自动触发与窗口调度
- `src/powercontext/builtin/runtime/config.py`：`memory_source_count_threshold` / `topic_memory_source_count_threshold`
- `src/powercontext/builtin/inference/coalescing.py`：并发 Embedding 合并、LRU 缓存、usage 与取消隔离
- `src/powercontext/builtin/runtime/source_lifecycle.py`：来源依赖 DAG、事件版本/序号、all/any 传播、generation/audit
- `src/powercontext/builtin/persistence/tables.py`：`pc_source_lifecycle` 生命周期持久化表
- `src/powercontext/builtin/persistence/topic_memory.py`：Topic 搜索按生命周期状态过滤
- `tests/`：八道题的产品侧回归测试
- `docs/zzd/`：实现与验证记录

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 官方公开自测 `source-01-table-evidence` | 17 passed |
| 官方公开自测 `source-02-pi-history` | 15 passed |
| 官方公开自测 `source-03-pdf-retrieval` | 6 passed（503 页全文、5 问题精确引用） |
| 官方公开自测 `retrieval-01-reranking` | 10 passed |
| 官方公开自测 `context-02-incremental-pack` | 14 passed |
| 官方公开自测 `processing-01-count-trigger` | 17 passed（本地 Windows 需放大 worker 超时；原配置见说明） |
| 官方公开自测 `memory-02-embedding-batches` | 8 passed |
| 官方公开自测 `memory-01-source-lifecycle` | 4 passed |
| 产品回归测试 `tests/test_csv_rows.py` | 16 passed |
| 产品回归测试 `tests/test_pi_session.py` | 17 passed |
| 产品回归测试 `tests/test_pdf_pages.py` | 7 passed |
| 产品回归测试 `tests/test_http_reranker.py` | 17 passed |
| 产品回归测试 `tests/test_context_pack.py` | 9 passed |
| 产品回归测试 `tests/test_count_trigger.py` | 15 passed |
| 产品回归测试 `tests/test_coalescing.py` | 7 passed |
| 产品回归测试 `tests/test_source_lifecycle.py` | 5 passed |
| 既有 Source 测试 `tests/test_sources.py`、`tests/test_source_observations.py` | 12 passed |
| `ruff check` / `ruff format --check` | 通过 |

公开自测覆盖：

- 赛题二：12 条机场记录逐行采集、BOM/多行引号/空行/表头/主键/宽度/编码/malformed CSV 边界、identity 稳定性与 256 字符上限，以及真实 SQLite 入库、重启后 projection 一致和 MemoryCitation 精确回查。
- 赛题三：所选分支全部可见事件、控制节点连接、工具调用/结果、压缩与分支摘要、BOM 与自定义消息；逐事件持久化和 Memory 精确引用；分支切换、重放、追加、坏输入修复与历史改写拒绝。
- 赛题四：完整解析 503 个物理页（含空白页）、整份 PDF 摘要与修订身份、空密码权限、损坏文件原子失败、重复入库幂等，以及 5 个 FinanceBench 原始问题的不同证据页精确 citation（公开 Recall@20 = 7/8）。
- 赛题五：分批上限与覆盖、乱序响应、全局排序与平分、缺失/重复/跨批 rank、非法 schema、NaN/Infinity、HTTP/网络错误传播、usage 与原生 Memory 搜索身份保持。
- 赛题六：wire 字段/Base64/摘要/字节数 oracle、UTF-8 有界分块、任意位置变化与空结果重建、错误 base/prefix/digest、序列化后独立重建，以及 Runtime 首轮 prepare + Memory 追加 + 增量 prepare。
- 赛题七：严格正整数/默认关闭、重复 capture 不计数、Memory 单窗与剩余数量重判、Topic wave、scope/family 隔离、时间条件 OR、显式 flush、receipt 信任升级、lineage-only Source 计数与排除、重启恢复与写入失败重试。
- 赛题八：并发去重与原始顺序、batch 上限、空输入、LRU 与缓存关闭、晚到调用共享执行中批次、失败不缓存/可重试、取消隔离、usage 与 token 汇总、向量数量校验、原生向量索引，以及 6 scope / 24 文本 / 4 次底层请求预算与缓存查询。
- 赛题九：20 节点多层 all/any DAG、撤回/恢复传播、原生 Memory 列表/搜索/Context、Topic 搜索/Context、精确历史引用、generation/audit、事件版本与序号、重放无新 Memory 修订、中断后重试、重启与 scope 隔离。
