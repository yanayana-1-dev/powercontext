# 赛题十实现说明：按查询和预算选择原文证据

本文说明 `context-01-evidence-selection`（按查询和预算选择原文证据）在 PowerContext 中的实现思路与验证方式。

## 1. 题目与基线

- 赛题编号：`context-01-evidence-selection`
- 冻结版本：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 开发分支：`zzd`
- 目标：为 `PrepareContextRequest` 增加默认关闭的 `evidence_selection`。开启后，`PreparedContextBuilder.build_scopes_result` 根据 query 从 Memory 候选正文中选择**原文片段**，返回精确字符区间、精确引用和可回查的地址，并让完整输出（trust policy、envelope、JSON、citation、正文）满足 UTF-8 字节预算。

## 2. 接口

`PrepareContextRequest` 新增：

```python
evidence_selection: bool = Field(default=False, strict=True)
```

`PreparedContextBuild` 新增：

```python
fragments: tuple[PreparedFragment, ...] = ()
```

`PreparedFragment`：

```python
@dataclass(frozen=True)
class PreparedFragment:
    address: MemoryEntryAddress
    start: int
    end: int
    text: str
```

- `address` 精确标识 scope、Memory revision、entry ID 与 entry version ID。
- `start`/`end` 是原 `entry.text` 的 Python 字符半开坐标，满足 `0 <= start < end <= len(text)`。
- `text == entry.text[start:end]`，不做改写、翻译或生成。
- `origins` 与 `fragments` 逐项、同序对应；本实现使用带 scope 的 `MemoryEntryAddress`。

## 3. 选择算法

选择只消费 Memory 候选，按候选分组保留 scope 身份：

1. 对每个候选 entry 做句子/换行切分，记录原文字符区间；同一 entry 重复命中只保留一次。
2. 查询词项小写 token 化，使用跨候选句子的 IDF、词干归并、query bigram/短语匹配、数字匹配和标题/结果线索加权打分。
3. 生成候选：
   - 单句候选；
   - 相邻 2/3 句窗口候选；
   - 整条 entry 候选。
4. 按分数排序后贪心装入：同 entry 重叠候选跳过；每条候选先尝试完整装入，超预算时只对单句候选做尾部裁剪；窗口/整条候选必须完整装入，避免把预算浪费在“长文前缀”而丢掉文末证据。
5. 对已选片段做相邻句扩展与同 entry 合并：预算允许时把连续/同文档的片段合并，减少 citation 开销并提升完整证据覆盖。
6. 若完全没有词项匹配，回退到首个 entry 的首个句子；若仍装不下则返回空 Context。

`max_bytes` 的计算始终基于最终完整输出：

```text
TRUST_POLICY + envelope markers + JSON trust/items + citation + content
```

`PreparedContext.content_bytes == len(content.encode("utf-8")) <= max_bytes`。

## 4. 输出格式

非空输出保留既有 trust policy 与 `BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1` / `END_POWERCONTEXT_PREPARED_CONTEXT_V1` envelope。中间 JSON：

```json
{
  "trust": "untrusted_history",
  "items": [
    {
      "citation": {
        "scope_id": "...",
        "memory_ref": {"family": "memory", "artifact_id": "...", "revision": 2},
        "entry_id": "...",
        "entry_version_id": "..."
      },
      "content": "原文片段",
      "start": 123,
      "end": 180
    }
  ]
}
```

没有 `items` 的 `truncated` 字段；片段本身就是原文，不需要摘要截断标记。无可用片段时返回既有空 `PreparedContext`（`status="empty"`、`content=None`、`content_bytes=0`）。

## 5. 兼容性

- `evidence_selection=False` 时完全走原有 `_select_entries` / `_select_text` 路径；`fragments=()`，`origins`、`context`、字节预算与旧行为一致。
- `include_code`、Topic Memory、Experience、Profile 的普通路径不受影响；选择模式只要求处理 Memory，忽略其他家族。
- `probe_budget` 在 `evidence_selection=True` 时也走同一片段选择，报告片段数量与剩余字节。
- `PreparedContextBuild` 新字段带默认空元组，既有构造方式兼容。

## 6. 验证

官方公开自测：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src;D:\workspace\_comp\powercontext-2026-competition\competition"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
$env:PYTHONUTF8="1"   # 仅 Windows 本地读取 UTF-8 fixture 需要
python -m pytest -q -c pytest.ini --confcutdir=. challenges/context-01-evidence-selection/acceptance
```

结果：**3 passed**。覆盖：

- 同一候选集合替换 query 后，分别选中 alpha 文档的 “37” 与 beta 文档的 “19”；
- Unicode 内容的完整 UTF-8 输出预算；
- 默认关闭时普通 prepare 的正文与行为；
- 公开 SciFact 样例经真实 Memory 写入/搜索后产生非空片段且不超预算。

产品侧测试：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src"
python -m pytest -q tests/test_evidence_selection.py -p no:cacheprovider
```

结果：**5 passed**：query 驱动与后部证据、精确 offset/text、fragments/origins/envelope 对齐、Unicode 字节预算、空候选、跨 scope 地址与重复 entry 去重、默认 prepare 兼容。

### 离线质量自测

使用 SciFact `claims_train`（505 条带 evidence 的 claim）和原始摘要，构造每条 claim 三篇摘要（gold + 两篇干扰），在 1,400 / 1,800 bytes 两档预算下复算 evidence token recall 与完整区间覆盖：

| 干扰选择 | 平均 token recall | 完整覆盖比例 |
| --- | --- | --- |
| 随机两篇干扰 | 0.80 / 0.89 | 0.54 / 0.69 |
| 引用集合内干扰 | 0.76 / 0.86 | 0.50 / 0.67 |
| 词面最相似两篇干扰 | 0.70 / 0.81 | 0.47 / 0.62 |

两档预算合并时，最相似的对抗性干扰下约为 **0.75 recall / 0.54 complete**，高于题目要求的 0.65 / 0.40；随机/引用干扰下余量更大。该自测只用于调参，不进入产品运行路径，也不读取任何评测标注。

## 7. 设计决策

- **原文片段而非摘要**：所有片段都是 entry.text 的切片，citation 和 address 直接指向原 entry version，保证可按引用回查。
- **完整输出计费**：每次候选装入都用 `_render_fragments` 计算完整 UTF-8 输出；不是只算正文。
- **窗口 + 单句 + 整条候选**：单句保证精度，窗口/整条保证多句证据完整覆盖，尾部裁剪只用于单句，避免文末证据丢失。
- **扩展/合并**：在预算内把相邻或同 entry 片段合并，降低 citation 重复开销，提高完整覆盖。
- **scope 身份优先**：地址始终带候选来源 scope；相同正文在不同 scope 不会被合并去重。
- **默认关闭**：新增行为由显式开关控制，旧 prepare 不受影响。

## 8. 边界清单

| 场景 | 行为 |
| --- | --- |
| `evidence_selection=False` | 原有 prepare 完全兼容，`fragments=()` |
| 空候选 | 空 Context，`content_bytes=0` |
| 预算连 envelope/citation 都装不下 | 合法空 Context |
| 查询只匹配文档后部 | 按 query 选择后部句子，不截前缀 |
| 同一 entry 多片段 | 允许多个，各自 offsets 精确 |
| 同 entry 重复命中 | 去重 |
| 同文本不同 scope | 地址/身份独立，不混用 |
| 片段完整装入 | 不裁剪，`text` 精确等于原文切片 |
| 单句超预算 | 仅尾部裁剪，仍满足 `start < end` |
| 窗口/整条超预算 | 跳过，改由单句候选装入 |
| Unicode | 按完整输出 UTF-8 字节计费 |
| 无词项命中 | 回退首 entry 首句；装不下则空 Context |
