# 赛题六实现说明：上下文增量传输与完整重建

本文说明 `context-02-incremental-pack`（上下文增量传输与完整重建）在 PowerContext 中的实现思路与验证方式。

## 1. 题目与基线

- 赛题编号：`context-02-incremental-pack`
- 冻结版本：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 开发分支：`zzd`
- 目标：把普通 `prepare` 的完整正文表示为带校验的 Context Pack；首轮发送完整包，后续只发送「可复用前缀 + 目标后缀」的 delta，接收端仅凭序列化数据完整重建。

## 2. 新增内容

| 文件 | 作用 |
| --- | --- |
| `src/powercontext/builtin/runtime/context_pack.py` | Pack/Delta 协议、分块、序列化、校验与重建 |
| `src/powercontext/builtin/runtime/application.py` | `ScopedContextApplication.prepare_incremental` |
| `tests/test_context_pack.py` | wire 编码、分块、delta、错误输入与 Runtime 集成回归测试 |
| `docs/zzd/context-02-incremental-pack.md` | 本文 |

普通 `prepare` 的实现与行为未改动。

## 3. Wire 协议

完整 Pack（canonical JSON，UTF-8，key 排序，分隔符 `,` `:`）：

```json
{"byte_count":123,"digest":"sha256:...","items":["<base64>"],"schema":"powercontext.context-pack.v1"}
```

- `items` 为内存 bytes 元组在 wire 上的标准 Base64 字符串数组。
- `digest = "sha256:" + SHA-256(canonical JSON({"schema","items"}))`。
- `byte_count` 是包含自身在内的 `to_bytes()` 长度；构造时用不动点迭代求出。

增量 Delta：

```json
{"appended":["<base64>"],"base_digest":"sha256:...","prefix_items":3,"schema":"powercontext.context-pack-delta.v1","target_digest":"sha256:..."}
```

- `prefix_items` 为非负整数，保留 base pack 的前 N 个块。
- `appended` 为目标 pack 在 N 之后的全部块。
- `byte_count` 是只读属性，不写入 JSON。

## 4. 分块

`ContextPack.from_content(content, *, chunk_bytes=512)`：

- 接受 `str` 或 UTF-8 `bytes`；空内容得到空 pack。
- 逐字符累积字节数，超过上限就切块；不会切断多字节 UTF-8 序列。
- 每块有效 UTF-8 且 ≤ `chunk_bytes`；重组后与原始字节完全一致。
- 单个字符的 UTF-8 长度大于 `chunk_bytes`（如 `"海"` 配 2 字节）时抛 `ContextPackError`。
- 相同输入与上限得到完全相同的块序列。

## 5. Delta 与重建

- `ContextPackDelta.between(base, target)` 选择**最长公共块前缀**（协议允许任意有效前缀），只携带目标后缀。
- `apply_context_pack_delta(base, delta)` 校验：
  - `delta.base_digest == base.digest`；
  - `prefix_items` 是非布尔整数且在 `[0, len(base.items)]`；
  - 保留前缀 + `appended` 构造出的目标 digest 与 `delta.target_digest` 一致；
  - 全部 `appended` 必须是 bytes。
- 解码 `ContextPack.from_bytes` 校验字段集合、schema、Base64、digest 格式、摘要与 `byte_count`；`ContextPackDelta.from_bytes` 校验字段集合、schema、digest 格式、非负整数 prefix 与 Base64。
- 解码只依赖序列化数据；重建不读取发送端进程内缓存或共享对象。

## 6. Runtime 集成

`ScopedContextApplication.prepare_incremental(request, base_pack=None)`：

1. 调用既有 `prepare(request)` 得到本轮目标正文（`content` 为 `None` 时为空）。
2. 目标正文按默认 512 字节分块为 `ContextPack`。
3. 未提供 base 时返回完整 Pack；提供 base 时返回 `ContextPackDelta.between(base, target)`。
4. base 类型非法时抛 `ContextPackError`。

普通 `prepare`、正文和 citation 语义保持不变。

## 7. 验证

官方公开自测：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src;D:\workspace\_comp\powercontext-2026-competition\competition"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
python -m pytest -q -c pytest.ini --confcutdir=. challenges/context-02-incremental-pack/acceptance
```

结果：**14 passed**。覆盖 wire 字段/摘要/字节数 oracle、分块与 UTF-8 边界、delta 任意位置变化、空 pack、错误 base/prefix/摘要、序列化后独立重建、Runtime 首轮 prepare + Memory 追加 + 增量 prepare。

产品侧测试：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src"
python -m pytest -q tests/test_context_pack.py -p no:cacheprovider
```

结果：**9 passed**。

传输成本方面，用「8 条初始 turn + 每轮追加 + 每 10 轮切换查询」的合成序列验证分块/前缀策略：40 轮下增量协议相对普通正文的节省约 **80%–90%**，远高于 45% 门槛；完整 v1 pack 与逐轮 gzip 对照也由测量脚本按题面口径报告。正式 40 轮序列由评测方固定，客户端只负责最长前缀复用与协议字节计量。

## 8. 设计决策

- **最长公共前缀**：协议只要求任意有效前缀；最长前缀使 delta 字节数最小。
- **固定容量分块**：确定性、每块有界且 UTF-8 有效；正文尾部追加时只有最后一个未满块会变化，前缀复用效果最好。
- **canonical JSON + 摘要**：pack 的摘要只覆盖 `schema` 与 `items`，避免自引用；`byte_count` 用不动点迭代保证 `byte_count == len(to_bytes())`。
- **解码宽严相济**：严格校验字段/类型/摘要/字节数，但接受语义等价、非 canonical 排版（公开测试中手工构造的 delta 就是默认 `json.dumps` 排版）。
- **不引入缓存**：`prepare_incremental` 每轮重新 prepare 并重新分块，delta 仅依赖调用方传入的 base pack。

## 9. 边界清单

| 场景 | 行为 |
| --- | --- |
| 空正文 / `content=None` | 空 pack（items 为空） |
| 单字符超过 `chunk_bytes` | `ContextPackError` |
| `chunk_bytes <= 0` / 非整数 | `ContextPackError` |
| 非法 UTF-8 输入 | `ContextPackError` |
| 缺字段 / 额外字段 / 非法 Base64 | `ContextPackError` |
| digest 或 byte_count 不匹配 | `ContextPackError` |
| prefix 为 bool/浮点/负数 | `ContextPackError` |
| prefix 超过 base 块数 | `ContextPackError` |
| base digest 或 target digest 不符 | `ContextPackError` |
| 任意有效前缀（含 0、非最长） | 可正确重建 |
| 首部/中部/尾部变化、目标变短 | 通过最长公共前缀 + 后缀重建 |
