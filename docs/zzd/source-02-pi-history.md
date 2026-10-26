# 赛题三实现说明：Pi 会话分支采集与断点恢复

本文说明 `source-02-pi-history`（Pi 会话分支采集与断点恢复）在 PowerContext 中的实现思路、关键决策与验证方式。

## 1. 题目与基线

- 赛题编号：`source-02-pi-history`
- 冻结版本：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`
- 开发分支：`zzd`
- 目标：把 Pi 会话文件中「所选叶节点 → 根」这条分支上的原始可见事件逐条采集为 `CAPTURED` Source；切换分支、追加文件、进程重启后增量导入，并且历史事件被改写时失败而不是覆盖。

## 2. 新增内容

| 文件 | 作用 |
| --- | --- |
| `src/powercontext/sources/pi_session.py` | `PiSessionInput`、`PiEventInput`、`parse_pi_session`、`PI_EVENT_SOURCE_DEFINITION`、`PiSessionConnector` |
| `tests/test_pi_session.py` | 分支解析、正文格式、identity/投影、增量与改写检测的回归测试 |
| `docs/zzd/source-02-pi-history.md` | 本文 |

基线源码零改动；`SourceDefinition`、`text-evidence` projection、`ConnectorLifecycle`、checkpoint 与 Runtime ingestion 全部复用现有实现。

## 3. 会话解析

`parse_pi_session(PiSessionInput(content, leaf_id=None))` 是一次「整篇解析 + 校验」：

1. `content` 按 `utf-8-sig` 严格解码（允许 BOM），按 JSONL 读取；空白行忽略。
2. 首条必须是 `{"type": "session", "version": 3, "id": <非空字符串>}`。
3. 其余记录必须是 JSON 对象，具有唯一的非空 `id`；`parentId` 为空或指向文件中**已经出现**的节点。因此重复 ID、缺失 parent、自引用、前向引用、循环都会在解析阶段以 `ValueError` 失败。
4. `leaf_id` 未指定时选文件中最后一条记录；`leaf_id` 不存在时失败；只有 header 没有节点时同样视为缺失 leaf 失败。
5. 从叶节点沿 `parentId` 回溯到根，再反转为根 → 叶的顺序。
6. 只有可见节点生成 `PiEventInput`；控制节点只保留连接关系，不出现在返回值中。返回 `tuple`，失败时不返回部分结果。

`PiEventInput` 保留 `session_id`、`event_id`、`parent_id`、`event_type`、`role`、`content` 和完整输入 `record`；`parent_id` 指向原始图上的父节点，即使父节点是控制节点。

## 4. 正文投影规则

| 事件 | role | 正文 |
| --- | --- | --- |
| `message` 字符串 / text 块 | `message.role` | 原文；多个块按 `\n` 连接 |
| `message` 的 `toolCall` | 同上 | `[tool_call NAME ID] COMPACT_SORTED_JSON_ARGUMENTS` |
| `role=toolResult` | `toolResult` | `[tool_result TOOL_NAME TOOL_CALL_ID] TEXT`，缺少工具名时用 `unknown` |
| `role=bashExecution` | `bashExecution` | `$ COMMAND`，有输出时换行追加 `OUTPUT` |
| `compaction` | `compaction` | `summary` |
| `branch_summary` | `branch_summary` | `summary` |
| `custom_message` | `custom` | 可见字符串 / text 块 |

- 最终 content 固定为 `ROLE: BODY`。
- thinking、image 等非文本块被忽略；没有任何可见正文的事件不生成 Source。
- 工具参数用 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 序列化，保留 Unicode、键排序、无多余空格。
- 压缩与分支摘要只是普通可见事件，不截断它们之前的祖先节点。

## 5. 不可变事件与引用

- Source 名 `pi-event`，version `1`，materialization 固定 `CAPTURED`。
- identity 为 `pi:{quote(session_id, safe="")}:{quote(event_id, safe="")}`，只由 session ID 和 event ID 决定，长度不超过 256；分隔符与 Unicode 都经过百分号编码，不同事件不会碰撞。
- 标准 `powercontext.text-evidence/1` projection：`source_type="pi-event"`、`source_id` 等于 Source name、`content` 为完整事件文本，metadata 恰好包含 `session_id`、`event_id`、`parent_id`、`event_type`、`role`。
- `registry.read(source)` 重建对应的 `PiEventInput`，包含完整 `record`；大工具结果不会被截断。
- 同一事件被改写时，Runtime 按相同 identity、不同 payload 检测为 `SourceConflictError`（`ValueError` 子类），不会覆盖原始记录。

## 6. 增量采集与 checkpoint

`PiSessionConnector(path, leaf_id=None)`：

- `name = "pi-session-history"`，`version = "1"`，`source_definitions = {"pi-event"}`。
- 每次 `run` 完整读取并解析文件，选择分支，只提交该分支上尚未出现在 checkpoint 中的可见事件。
- checkpoint 由 `ConnectorLifecycle` 在整次运行成功后提交；任何解析错误、改写检测失败或提交异常都不会推进进度。

checkpoint 结构（内部格式由实现决定）：

```json
{
  "version": 1,
  "session_id": "<session id>",
  "events": {"<event id>": "<sha256 of canonical record json>"}
}
```

行为约定：

- 去重：checkpoint 中已有的事件不会再次提交；重放分支时 `items == ()` 且 checkpoint 不变。
- 分支切换：共享祖先不重复提交，新分支事件完整导入；其他分支已采集的事件继续保留在 checkpoint 和历史库中。
- 追加与重启：新事件按同样规则导入；重启后从持久化 checkpoint 继续。
- 历史改写：解析后先对本文件中所有「已导入事件」校验摘要（包括当前未选中的其他分支），任一不一致立即 `ValueError`，不提交任何新事件。
- 失败恢复：坏 JSON、缺失 leaf、历史改写都发生在提交与 checkpoint 保存之前；修复输入后从原进度继续。
- session ID 变化时视为新会话，旧 checkpoint 事件不参与去重。

checkpoint 只存在于持久化层，模块没有全局状态，也不执行记录中的代码或命令。

## 7. 验证

官方公开自测（`challenges/source-02-pi-history/acceptance`）：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src;D:\workspace\_comp\powercontext-2026-competition\competition"
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"
python -m pytest -q -c pytest.ini --confcutdir=. challenges/source-02-pi-history/acceptance
```

结果：**15 passed**，覆盖公开 25 KB 会话、分支控制节点、工具调用/结果、压缩/分支摘要、BOM、bash、custom_message、非法图/非法归档、Runtime 持久化与逐事件 Memory 引用，以及分支切换、重放、追加、坏 JSON 修复、历史改写拒绝。

产品侧测试：

```powershell
$env:PYTHONPATH="D:\workspace\powercontext-zzd\src"
python -m pytest -q tests/test_pi_session.py -p no:cacheprovider
```

结果：**17 passed**。

## 8. 关键决策

- **先整篇校验再选分支**：图结构错误在进入 Source 层之前就以 `ValueError` 暴露，避免部分导入。
- **checkpoint 存摘要而不是记录**：身份 + 记录摘要足以去重和发现改写，checkpoint 保持紧凑。
- **改写检测覆盖整份文件**：已导入事件即使不在当前分支上被改写，也视为历史损坏；这样切换分支不会把改写带进新分支。
- **不修改基线**：Source、Definition、projection、ConnectorLifecycle 全部复用，不引入依赖、网络或模型调用。

## 9. 边界清单

| 场景 | 行为 |
| --- | --- |
| 空文件 / 无 header / version 非 3 / id 为空 | `ValueError` |
| 非法 UTF-8 / 非法 JSON / 非对象记录 | `ValueError` |
| 重复 ID / parent 不存在 / 自引用 / 前向引用 | `ValueError` |
| 指定 leaf 不存在 / 只有 header | `ValueError` |
| 无可见正文（thinking、image、空 content） | 忽略 |
| compaction / branch_summary | 正常生成事件，不截断之前的证据 |
| 分支切换 | 只提交新增可见事件 |
| 追加 / 重启 | checkpoint 去重，继续导入 |
| 已导入记录被改写 | `ValueError`，checkpoint 不推进 |
| 大工具结果 | 完整保留在 Source（record + content） |
