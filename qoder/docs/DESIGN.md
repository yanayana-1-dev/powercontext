# SeekMem 设计说明：seekdb 与 PowerContext 的分工

## 一、要解决的问题

学习、科研和工作中有两类反复出现的低效：

1. **找不到**：资料散落在笔记、周报、纪要、实验记录里，要用时翻不动。全文搜索只匹配字面，"上次的报销怎么走"搜不到写着"财务审核流程"的文档。
2. **接不上**：跨天/跨会话回来，忘了上次进行到哪、定了什么。工具不记得你，每次都要从零交代背景。

SeekMem 用两个指定技术各解一个问题：**seekdb 解决"找不到"（数据与检索能力），PowerContext 解决"接不上"（记忆与上下文能力）**。二者缺一，作品退化成普通检索或普通聊天，所以两个都是核心功能而非点缀。

## 二、seekdb 的实际作用（不是"装了"，是"用了"）

### 存储模型

资料按标题→分块后写入 seekdb 单表 `sm_chunks`，一表同时承载三类检索维度：

```sql
CREATE TABLE sm_chunks (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  doc_id BIGINT, seq INT,
  doc_title VARCHAR(512), source VARCHAR(512), tags VARCHAR(512),   -- 结构化维度
  heading VARCHAR(512), content TEXT,                                -- 全文维度
  embedding VECTOR(1024),                                            -- 向量维度
  VECTOR INDEX v_emb (embedding) WITH (distance=L2, type=hnsw),
  FULLTEXT INDEX f_content (content) WITH PARSER IK
)
```

### 检索：三路召回 + RRF 融合，全部单条 SQL 完成

每次提问并行执行三条 SQL（`app/seekmem/retrieval.py`，逐条留痕 `evidence/sql_trace.jsonl`）：

| 通道 | SQL 形态 | 解决什么 |
|---|---|---|
| vector | `WHERE ... ORDER BY l2_distance(embedding, ?) APPROXIMATE LIMIT 8` | 语义相近但用词不同（"报销"vs"财务审核"） |
| hybrid | `WHERE MATCH(content) AGAINST(?) ORDER BY l2_distance(embedding, ?) APPROXIMATE LIMIT 8` | **单语句混合检索**：先用 IK 全文索引过滤掉字面不相关的分块，再在剩余集合上按向量距离排序。这是选 seekdb 而不是"向量库+关键词引擎两件套"的决定性理由——融合发生在索引层，应用层没有 N+1 合并 |
| recent | `ORDER BY created_at DESC LIMIT 5` | 结构化条件：最新周报/纪要优先 |

三路结果用 Reciprocal Rank Fusion 融合（recent 权重 0.4 防止旧闻霸屏），Top-6 进入回答，每条带通道徽章与 l2 距离在 UI 展示——评委可以直接看到"哪条 SQL 召回了哪条资料"。

### 为什么这些资料必须进 seekdb 而不是内存/文件系统

- 混合检索的索引（HNSW + IK 倒排）由 seekdb 维护，增量写入后立即可查；
- MySQL 协议意味着可以用 SQL 做结构化审计（比如"统计每个文档的分块数""按标签筛选"）；
- 支持后续演进：换嵌入模型只需重建一张表，检索逻辑不变。

## 三、PowerContext 的实际作用（不是"存了"，是"复用了"）

### 记忆模型

- 每个工位（用户）一个独立 **scope**（`POST /v1/scopes`，idempotency_key 幂等重建），工位之间记忆互不可见（样例 4 验证）。
- 每轮问答结束后，LLM 从对话中提炼 0-2 条值得长期记住的内容，`POST /v1/memory/remember` 写入，kind 四类：note（结论）/ decision（决定）/ task（待办）/ preference（偏好）。
- 下次打开工作台：`POST /v1/memory/search` 召回该工位记忆，注入两处——
  1. **欢迎横幅**："欢迎回来，从你的工作记忆里想起来了：…"（问答页首屏）；
  2. **回答上下文**：作为【工作记忆】块进入 LLM prompt，使回答"知道你是谁、上次到哪"。
- 「记忆」页签通过 `POST /v1/memory/entries/list` 展示该 scope 全部活跃记忆，可直接核对读写了什么。

### 为什么记忆必须进 PowerContext 而不是 seekdb

职责不同：资料是**可检索的公共事实**（要出处、要排序、要过滤），记忆是**跟人走的上下文**（要按工位隔离、要有版本与状态、要被"想起"而不是"搜出"）。PowerContext 的 scope/entry/revision/retire 模型正是为此设计；用 seekdb 手搓这些等于重写一个记忆服务，用 PowerContext 存资料则丢掉混合检索。两者各在其位。

### 记忆循环

```
提问 ──→ recall（想起相关记忆）──→ 注入 prompt ──→ 回答
  └──────────────── 提炼（LLM 判断值得记的 0-2 条）──→ remember（写入）
                                                    下次会话 recall 复用 ↑
```

## 四、工程决策

- **LLM 是增强不是依赖**：LLM 缺席时问答降级为"检索摘要 + 引用"（实测留档 `evidence/degraded_mode_test.txt`），检索与记忆主流程零依赖 LLM。评委没有 GPU/Key 也能完整体验核心功能。
- **证据优先**：所有 seekdb SQL、检索融合明细、记忆读写、问答轮次写 `evidence/*.jsonl` 并在 UI「证据」页可见。技术是否"实际发挥作用"不需要口头解释，看留痕。
- **无构建前端**：原生 HTML/JS/CSS，评审 clone 即跑，不需要 node_modules。
- **配置全部环境变量**：seekdb/PowerContext/嵌入/LLM 四类服务地址均可替换，`.env.example` 给出占位值。
