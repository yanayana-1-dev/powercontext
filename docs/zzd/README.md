# zzd 分支开发说明

本目录记录 `zzd` 分支针对 2026 PowerContext 大赛赛题二（`source-01-table-evidence`：CSV 行级采集与原始记录回查）的设计与验证过程。

## 分支基线

- 基线：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 分支：`zzd`
- 实现范围：仅新增源码文件，不修改基线已有实现

## 交付内容

- `src/powercontext/sources/csv_rows.py`：赛题要求的公共接口、CSV 解析、行 identity、Source Definition 与 text-evidence 投影
- `tests/test_csv_rows.py`：解析契约、identity 边界与投影形状的回归测试
- `docs/zzd/source-01-table-evidence.md`：详细设计与验证记录

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 官方公开自测 `challenges/source-01-table-evidence/acceptance` | 17 passed |
| 产品回归测试 `tests/test_csv_rows.py` | 16 passed |
| 既有 Source 测试 `tests/test_sources.py`、`tests/test_source_observations.py` | 12 passed |
| `ruff check` / `ruff format --check` | 通过 |

公开自测覆盖 12 条机场记录逐行采集、BOM/多行引号/空行/表头/主键/宽度/编码/malformed CSV 边界、identity 稳定性与 256 字符上限，以及真实 SQLite 入库、重启后 projection 一致和 MemoryCitation 精确回查。

详细设计见 [source-01-table-evidence.md](source-01-table-evidence.md)。
