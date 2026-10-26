# 赛题四实现说明：财报 PDF 全文解析与证据页检索

本文说明 `source-03-pdf-retrieval`（财报 PDF 全文解析与证据页检索）在 PowerContext 中的实现思路、检索策略与验证方式。

## 1. 题目与基线

- 赛题编号：`source-03-pdf-retrieval`
- 冻结版本：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 开发分支：`zzd`
- 目标：完整解析财报 PDF 的全部物理页，逐页形成 `CAPTURED` Source；按自然语言问题检索可精确回查的证据页，并满足解析质量与 Recall@20 门槛。

## 2. 新增内容

| 文件 | 作用 |
| --- | --- |
| `src/powercontext/sources/pdf_pages.py` | `PDFDocumentInput`、`PDFPageInput`、`parse_pdf_document`、`PDF_PAGE_SOURCE_DEFINITION`、`search_pdf_pages` |
| `tests/test_pdf_pages.py` | 空白页/修订身份、空密码权限、损坏文件、投影与精确检索的回归测试 |
| `docs/zzd/source-03-pdf-retrieval.md` | 本文 |

基线源码零改动；标准 `text-evidence` 投影、Runtime ingestion 与 scoped Memory API 全部复用。检索只通过公开 `memory.search` / `memory.get` 访问数据，不绕过 Memory 构造引用。

## 3. 全文解析

`parse_pdf_document(PDFDocumentInput(document_id, content))`：

1. 校验 `document_id` 非空且非纯空白；对完整输入字节计算 SHA-256（小写十六进制）。
2. 用共享环境锁定的 `pypdf` 打开原 PDF；若带权限标记，则用空用户密码解密（合法的空密码 PDF 可读）。
3. 逐物理页调用 `extract_text()`，页码从 1 开始，包括空白页；返回 `tuple[PDFPageInput, ...]`。
4. 空文件、非 PDF、损坏、加密无法用空密码打开、或任何页面解析异常，统一 `ValueError`，不返回部分页。

解析质量直接用 `pypdf` 默认抽取，与 FinanceBench `evidence_text_full_page` 的词项对比（见第 6 节）接近 1.0，说明无需自定义版面重建。

## 4. 页级 Source 与投影

- Definition：名称 `pdf-page`、版本 `1`、`input_class=PDFPageInput`、`source_class=PDFPageSource`。
- 每页 materialization 固定 `CAPTURED`；`registry.read(source)` 返回原 `PDFPageInput`。
- identity：`pdf:{quote(document_id, safe="")}:{sha256}:{page_number}`，由文档 ID、整份 PDF 摘要和物理页码共同决定，长度不超过 256。
  - 同一 PDF 修订后摘要变化 → 新 identity；旧引用继续指向旧修订。
  - 文档 ID 中的分隔符/Unicode 经百分号编码，不会碰撞。
- 标准 `powercontext.text-evidence/1` 投影：
  - `source_type="pdf-page"`，`source_id` 等于 Source name；
  - `content = DOCUMENT_ID + "\n" + PAGE_TEXT`；
  - metadata 恰好包含 `document_id`、`document_sha256`、`page_number`。

## 5. 证据页检索

`search_pdf_pages(memory, question, limit=10)` 只使用公开 Memory API，流程如下：

1. **问题词项**：小写化后用 `[a-z]+|\d+` 分词，去掉功能词和 FinanceBench 指令性套话（`respond`、`perspective`、`calculate` 等），去重后得到内容词。
2. **候选通道**：
   - 每个词项单独做一次 `memory.search(mode="fts")`，保留其有序命中；
   - 另按 6 个词一窗做组合检索，补充“匹配多个词但单词排名不靠前”的页面。
3. **融合排序**：对每个通道按 RRF 累加 `weight / (60 + rank)`；单词通道权重随命中数变化（越稀有越高），窗口通道权重较低。
4. **标题加成**：把页面文本中短小、标题式或 `Item N.` 形式的行识别为标题，若问题词项出现在标题中则按该词项在候选集合中的 IDF 加权加成。财报的证据页往往是报表/章节标题页，这一步显著提升此类页面的排序。文档 ID 行也参与标题识别，在多文档集合中同时起到“文档一致性”信号的作用。
5. **精确引用**：按排序逐个 `memory.get`，只接受恰好关联一个 `pdf-page` Source 的条目，按 `source_id` 去重，最多返回 `limit` 个 `MemoryCitation`。

所有引用都来自真实 Memory 条目，`limit <= 0` 或没有可用词项时返回 `()`。

## 6. 解析质量验证（公开 PDF）

使用公开的 PepsiCo 2022 10-K（503 页）与 FinanceBench 固定 revision 的标注页对比，词项定义与题面一致（小写、`[a-z]+|\d+`、多重集合交集）：

| 证据页（物理页） | 证据词项召回 | 整页词项 F1 |
| --- | ---: | ---: |
| 4 | 0.996 | 0.994 |
| 5 | 1.000 | 0.979 |
| 26 | 1.000 | 1.000 |
| 62 | 1.000 | 1.000 |
| 64 | 1.000 | 1.000 |
| 78 | 1.000 | 1.000 |

均高于题面 0.95 门槛；每页词项数与标注全文也基本一致。这一对比只用于离线开发验证，运行时代码不读取任何标注、答案或预解析文本。

## 7. 检索验证（公开 5 问）

以 `limit=20` 检索公开的 5 个原始问题，与 FinanceBench 标注页（0-based 页码 +1 转为物理页）对比：

| 问题 | 标注页 | 命中 |
| --- | --- | --- |
| `financebench_id_01009` | 4, 5 | 5（第 5 名） |
| `financebench_id_00735` | 26 | 26（第 9 名） |
| `financebench_id_01328` | 78 | 78（第 7 名） |
| `financebench_id_03620` | 62, 64 | 62（第 5）、64（第 1） |
| `financebench_id_04481` | 62, 64 | 62（第 4）、64（第 1） |

公开集合 Recall@20 = **7/8 = 0.875**，高于 0.70 门槛。未命中的第 4 页正文只包含公司名，没有与问题相关的可检索词项，属于标注噪声；其余标注页均在前 10 名。

## 8. 官方公开自测

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src;D:\workspace\_comp\powercontext-2026-competition\competition"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
python -m pytest -q -s -c pytest.ini --confcutdir=. challenges/source-03-pdf-retrieval/acceptance
```

结果：**6 passed**。完整解析 503 页、持久化全部页面、重复提交幂等、空白页与修订身份、空密码权限、损坏文件原子失败，以及 5 个问题的 20 条不同页精确引用均通过。

产品侧测试：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src"
python -m pytest -q tests/test_pdf_pages.py -p no:cacheprovider
```

结果：**7 passed**。

## 9. 关键决策

- **使用 pypdf 默认抽取而不是自定义版面重建**：与 FinanceBench 整页文本的词项 F1 接近 1.0，满足 90%–110% 词项数门槛；版面空白不影响词项统计。
- **逐词项 + 窗口双通道候选**：单词通道保证稀有词（如章节标题中的词）不会被“多词覆盖”类候选淹没；窗口通道补充长问题的组合证据。
- **RRF 而不是单一 BM25**：不同查询通道的分值不可比，RRF 只依赖名次，稳定且无需全局语料统计。
- **标题加成**：财报证据页常是报表或章节标题页，标题词命中是强信号；权重由候选集合中的 IDF 计算，避免常见词把标题页抬得过高。
- **引用必须来自 `memory.get`**：先去重再返回，保证 `limit`、不同页和精确回查三个合同同时成立。

## 10. 边界清单

| 场景 | 行为 |
| --- | --- |
| 空文件 / 非 PDF / 损坏 / 无法完整解析 | `ValueError`，不返回部分页 |
| 空密码权限 PDF | 可读 |
| 非空用户密码 PDF | `ValueError` |
| 空白页 | 保留，`text=""`，页码连续 |
| PDF 修订 | SHA-256 变化 → 新 identity，旧引用保留 |
| 重复入库 | 同 identity 幂等，不增加 Source |
| `limit <= 0` 或无可用词项 | `()` |
| 同一页多条命中 | 按 `source_id` 去重后只返回一条 |
| 多文档集合 | 文档 ID 行参与标题/一致性信号，问题中的公司/年份词可定位到对应文档 |
