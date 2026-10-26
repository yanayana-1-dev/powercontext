# 赛题二实现说明：CSV 行级采集与原始记录回查

本文说明 `source-01-table-evidence`（CSV 行级采集与原始记录回查）在 PowerContext 中的实现思路、关键决策与验证方式。

## 1. 题目与基线

- 赛题编号：`source-01-table-evidence`
- 冻结版本：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 开发分支：`zzd`（直接基于冻结 commit，未改动基线已有源码）
- 目标：把离线 CSV 按主键逐行采集为独立 `CAPTURED` Source，让检索引用可以精确回查完整原始记录；重排文件或重启进程后，行 identity 与 projection 保持一致。

## 2. 新增文件

| 文件 | 作用 |
| --- | --- |
| `src/powercontext/sources/csv_rows.py` | 题目要求的全部公共接口：输入模型、行 Source、解析、identity、Definition 与 text-evidence projection |
| `tests/test_csv_rows.py` | 产品侧回归测试，覆盖解析契约、identity 边界与投影形状 |
| `docs/zzd/source-01-table-evidence.md` | 本文 |

基线源码零改动：`Source`、`SourceDefinition`、`SourceDefinitionRegistry`、`manifest_for_definition`、`project_source_for_transport` 以及 Runtime ingestion 均直接复用。

## 3. 接口实现

`powercontext.sources.csv_rows` 提供：

```python
class CSVDocumentInput(BaseModel): ...
class CSVRowInput(BaseModel): ...
class CSVRowSource(Source): ...

def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]: ...
def csv_row_source_id(document_id: str, primary_key_value: str) -> str: ...
CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput]
```

`CSVRowSource` 在 `Source` 之上固定 `materialization=CAPTURED`，并校验 `name == source_id`、identity 长度以及 `columns`/`values` 等长。未显式传入 `source_id`/`name` 时，会用 `document_id + primary_key_value` 自动推导，便于直接构造。

## 4. 解析契约

`parse_csv_document` 是一次「全量解析 + 全量校验」，任何错误都在返回前以 `ValueError` 抛出，不会返回部分行：

1. **解码**：`bytes.decode("utf-8-sig")`，严格 UTF-8，接受文件头 BOM；非法字节直接失败。
2. **CSV 语义**：`csv.reader(io.StringIO(text, newline=""), strict=True)`，与题目要求的 `csv.reader` 固定语义一致；`csv.Error` 统一转换为 `ValueError`。
3. **表头**：第一条记录必须是表头；字段非空、非纯空白、精确唯一；首尾空格保留，不做 trim。
4. **主键列**：`primary_key` 必须非空，并与表头字段逐字符完全匹配（`" id "` 只能由 `" id "` 指定）。
5. **数据行**：字段数必须等于表头宽度；主键值非空且非纯空白、同一文档内唯一；完全空的物理行忽略；只有表头的文档返回 `()`。
6. **值保真**：所有字段始终是原始字符串，不进行 trim、数值/日期/布尔转换。

`document_id` 必须非空且首尾无空白，`parse_csv_document` 与 `csv_row_source_id` 都会校验。

## 5. 行 identity

固定格式：

```text
csv:{quote(document_id, safe="")}:{quote(primary_key_value, safe="")}
```

- 两个组件分别用 `urllib.parse.quote(component, safe="")` 编码，`:`、`/`、空格、非 ASCII 字符都会被百分号编码，避免分隔符碰撞。
- 生成结果超过 256 个字符时抛 `ValueError`。
- identity 只依赖 `document_id` 与主键值，因此行重排、非主键字段修改、进程重启都不会改变行身份。

## 6. text-evidence 投影

`CSV_ROW_SOURCE_DEFINITION` 注册标准 `powercontext.text-evidence`（version `1`）投影：

- `source_type = "csv-row"`，`source_id` 等于行 identity；
- `content` 为按原表头顺序构造的字段对象，`json.dumps(..., ensure_ascii=False, separators=(",", ":"))`；
- `metadata` 恰好包含 `document_id`、`primary_key`、`primary_key_value`、`columns`、`row` 五个键；
- 空字段保持 `""`，数字样式字符串保持字符串，不做类型改写。

投影的输出类型直接使用标准 `TextEvidence`，因此 `manifest_for_definition` 生成的 projection schema 与 Runtime 对标准投影的校验完全一致。

## 7. 持久化与回查

实现不新增 pipeline 或导出 API，完全沿用现有链路：

1. `SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))` → `resolve` 得到 `CSVRowSource`；
2. `project_source_for_transport` 生成带 manifest 指纹的 `SourceObservation`；
3. `runtime.ingestion.register(manifest)` + `submit(...)` 落库；
4. `contexts.get(scope).sources.journal.entries()` 与 Memory 检索均从持久化的 projection 读取，`MemoryCitation` 精确回查目标行。

## 8. 验证

本地环境：Windows 11 + Python 3.13.15（评测环境为 Python 3.12 / Linux x86_64），使用基线源码路径运行官方公开测试：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src"
python -m pytest -q -c pytest.ini -o addopts="-p no:cacheprovider --import-mode=importlib" `
  --confcutdir=. challenges/source-01-table-evidence/acceptance
```

结果：**17 passed**，覆盖 12 条公开机场记录、BOM/多行引号/空行/表头/主键/宽度/编码/malformed CSV 边界、identity 稳定性与长度、以及 SQLite 持久化 + 重启 + Memory 精确引用回查。

产品侧测试：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src"
python -m pytest -q tests/test_csv_rows.py -p no:cacheprovider
```

结果：**16 passed**。同时 `ruff check` 与 `ruff format --check` 通过。

## 9. 关键决策

- **先整篇解析再构造行**：先 `list(csv.reader(...))`，确保 malformed CSV 不会在已经构造部分行之后才暴露；随后再统一做表头、宽度、主键校验。
- **identity 只编码、不 trim**：主键首尾空格按原值参与 `quote`，因此 `" key "` 与 `"key "` 是不同的行，同时不会产生首尾空白字符。
- **复用标准投影类型**：projection 的 `output_class` 直接使用 `TextEvidence`，避免自定义 schema 与 Runtime 的标准投影校验产生偏差。
- **不扩展 Runtime**：题目只要求行级 Source 与投影，所有持久化、manifest、observation 行为都交给现有实现。

## 10. 边界清单

| 场景 | 行为 |
| --- | --- |
| 空文件 / 空表头 / 纯空白表头字段 | `ValueError` |
| 表头字段重复 | `ValueError` |
| `primary_key` 不存在或为空白 | `ValueError` |
| 数据行字段数与表头不一致 | `ValueError` |
| 主键为空 / 纯空白 / 文档内重复 | `ValueError` |
| 完全空的物理行 | 忽略 |
| 只有表头 | 返回 `()` |
| 非法 UTF-8 / 未闭合引号 / 引号后多余字符 | `ValueError` |
| identity 超过 256 字符 | `ValueError` |
| 行重排、非主键字段修改 | identity 不变，projection 跟随内容更新 |
