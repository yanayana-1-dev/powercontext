# zzd 分支开发说明

本目录记录 `zzd` 分支针对 2026 PowerContext 大赛赛题二、三、四、五的设计与验证过程。

## 分支基线

- 基线：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 分支：`zzd`
- 实现范围：仅新增源码、测试与文档，不修改基线已有实现

## 已实现赛题

| 赛题 | 编号 | 实现 | 设计说明 |
| --- | --- | --- | --- |
| 赛题二 | `source-01-table-evidence` | `src/powercontext/sources/csv_rows.py` | [source-01-table-evidence.md](source-01-table-evidence.md) |
| 赛题三 | `source-02-pi-history` | `src/powercontext/sources/pi_session.py` | [source-02-pi-history.md](source-02-pi-history.md) |
| 赛题四 | `source-03-pdf-retrieval` | `src/powercontext/sources/pdf_pages.py` | [source-03-pdf-retrieval.md](source-03-pdf-retrieval.md) |
| 赛题五 | `retrieval-01-reranking` | `src/powercontext/builtin/artifacts/memory/http_reranker.py` | [retrieval-01-reranking.md](retrieval-01-reranking.md) |

## 交付内容

- `src/powercontext/sources/csv_rows.py`：CSV 行级 Source、解析、identity、Definition 与 text-evidence 投影
- `src/powercontext/sources/pi_session.py`：Pi 会话分支解析、事件 Source、增量 connector 与 checkpoint
- `src/powercontext/sources/pdf_pages.py`：PDF 全页解析、页级 Source、精确证据页检索
- `src/powercontext/builtin/artifacts/memory/http_reranker.py`：HTTP 分批重排、跨批次全局稳定排序
- `tests/test_csv_rows.py`、`tests/test_pi_session.py`、`tests/test_pdf_pages.py`、`tests/test_http_reranker.py`：四道题的产品侧回归测试
- `docs/zzd/`：实现与验证记录

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 官方公开自测 `source-01-table-evidence` | 17 passed |
| 官方公开自测 `source-02-pi-history` | 15 passed |
| 官方公开自测 `source-03-pdf-retrieval` | 6 passed（503 页全文、5 问题精确引用） |
| 官方公开自测 `retrieval-01-reranking` | 10 passed |
| 产品回归测试 `tests/test_csv_rows.py` | 16 passed |
| 产品回归测试 `tests/test_pi_session.py` | 17 passed |
| 产品回归测试 `tests/test_pdf_pages.py` | 7 passed |
| 产品回归测试 `tests/test_http_reranker.py` | 17 passed |
| 既有 Source 测试 `tests/test_sources.py`、`tests/test_source_observations.py` | 12 passed |
| `ruff check` / `ruff format --check` | 通过 |

公开自测覆盖：

- 赛题二：12 条机场记录逐行采集、BOM/多行引号/空行/表头/主键/宽度/编码/malformed CSV 边界、identity 稳定性与 256 字符上限，以及真实 SQLite 入库、重启后 projection 一致和 MemoryCitation 精确回查。
- 赛题三：所选分支全部可见事件、控制节点连接、工具调用/结果、压缩与分支摘要、BOM 与自定义消息；逐事件持久化和 Memory 精确引用；分支切换、重放、追加、坏输入修复与历史改写拒绝。
- 赛题四：完整解析 503 个物理页（含空白页）、整份 PDF 摘要与修订身份、空密码权限、损坏文件原子失败、重复入库幂等，以及 5 个 FinanceBench 原始问题的不同证据页精确 citation（公开 Recall@20 = 7/8）。
- 赛题五：分批上限与覆盖、乱序响应、全局排序与平分、缺失/重复/跨批 rank、非法 schema、NaN/Infinity、HTTP/网络错误传播、usage 与原生 Memory 搜索身份保持。
