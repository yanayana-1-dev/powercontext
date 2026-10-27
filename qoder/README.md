# SeekMem · 追忆工作台

> 把散落的文档喂进去，它变成一个"会记得你"的工作知识库：**seekdb 帮你找到（向量+全文+结构化混合检索，给出处），PowerContext 帮你记住（工位记忆跨会话续上）**。
> 2026 OceanBase 大赛 · 生态赛道 Qoder 题（Build with Qoder）参赛作品。用 Qoder 开发，通过 Qoder Sites 发布展示站。

## 它解决什么问题

- **找不到**：资料散在笔记/周报/纪要/实验记录里，字面搜索搜不到语义相关的内容 → seekdb 一条 SQL 完成混合检索，回答带来源编号。
- **接不上**：跨天回来忘了上次进行到哪、定了什么 → PowerContext 按工位记住你的事项/结论/偏好，下次打开自动"想起来"并注入回答。
- **多人共用互相污染**：每个工位（用户）一个独立记忆 scope，互不可见。

## 核心功能（30 秒版）

1. **资料库**：Markdown/文本导入 → 按标题智能分块 → bge-m3 向量化 → seekdb（VECTOR(1024)+HNSW 向量索引 + IK 全文索引 + 结构化列）。
2. **问答**：三路召回（纯向量 / 单语句混合检索 / 最近资料）+ RRF 融合 → LLM 综合回答带 `[n]` 引用；每个引用标注命中通道与 l2 距离。
3. **工作记忆**：每轮自动提炼 0-2 条记忆写入 PowerContext（note/decision/task/preference）；欢迎横幅与回答上下文复用记忆；记忆面板可视化全部条目。
4. **证据页**：所有 seekdb SQL、检索融合、记忆读写、问答轮次逐条留痕（evidence/*.jsonl + UI 展示），技术作用可直接核对。

LLM 可选：不接 LLM 自动降级为"纯检索摘要"模式，seekdb 与 PowerContext 功能不受影响。

## 快速开始

```bash
# 前置：seekdb(:2881) 与 PowerContext(:8010)、bge-m3 嵌入服务(:11435) 已就绪 —— 见 docs/RUNBOOK.md 第 1-3 节
cd app && pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 9100
python ../scripts/seed.py        # 导入 10 篇种子资料
# 浏览器打开 http://127.0.0.1:9100
python ../scripts/verify_demo.py # 四步验证样例一键复跑（7 项断言）
```

完整从零部署（seekdb/PowerContext/嵌入/LLM 全部步骤）见 **[docs/RUNBOOK.md](docs/RUNBOOK.md)**；网页手工验证步骤见 **[docs/VERIFY.md](docs/VERIFY.md)**；seekdb 与 PowerContext 的分工设计见 **[docs/DESIGN.md](docs/DESIGN.md)**。

## 目录结构

```
qoder/
├── app/                  # SeekMem 应用（FastAPI 后端 + 原生 JS 前端，无构建依赖）
│   ├── main.py           # 入口：REST API + 静态页
│   ├── seekmem/          # config/db/schema/embed/llm/memory/ingest/retrieval/chat/traces
│   └── static/           # 工作台 UI（问答/资料库/记忆/证据 四页签）
├── docs/                 # RUNBOOK 运行说明 · VERIFY 验证样例 · DESIGN 设计 · QODER-GUIDE 开发与发布指引
├── scripts/              # seed.py 种子入库 · verify_demo.py 一键验证 · embed_server.py bge-m3 服务
├── seed_docs/            # 10 篇种子资料（制度/周报/纪要/调研/实验/复盘/需求/规范/论文笔记/新人指南）
├── site/                 # Qoder Sites 展示站（静态单页）
└── evidence/             # 2026-10-27 实测留档：sql/retrieval/memory/chat 四类 trace + 7/7 验证结果 + 降级与隔离测试
```

## 技术栈与实测环境

| 层 | 选型 | 实测版本 |
|---|---|---|
| 检索存储 | seekdb（MySQL 协议，VECTOR+HNSW+IK FULLTEXT） | v1.4.0.0 |
| 记忆服务 | PowerContext（HTTP API：scopes/memory） | v1.2.0 |
| 嵌入 | bge-m3 1024 维（OpenAI 兼容本地服务） | sentence-transformers |
| LLM（可选） | qwen2.5-3b-instruct（OpenAI 兼容本地服务） | llama-cpp-python |
| 应用 | Python 3.13 + FastAPI + PyMySQL + 原生 JS | Windows/WSL2 实测 |

## 验证结果（2026-10-27，evidence/ 有完整留档）

- `scripts/verify_demo.py`：**7/7 通过**（健康检查、检索命中正确文档、单语句混合检索 SQL 生效、记忆写入 PowerContext、跨会话记忆召回并用于回答、scope 隔离、SQL 留痕含 APPROXIMATE）。
- 降级模式（无 LLM）：检索+引用正常，留档 `evidence/degraded_mode_test.txt`。
- 多工位隔离：不同 scope 记忆互不可见，留档 `evidence/scope_isolation_test.txt`。

## 复用与引用说明

- `scripts/embed_server.py` 改编自本队 Dify 赛道（雾隐镇编年史）的同款服务，行为一致。
- 种子文档为本队为演示撰写的虚构工作资料，不含真实人员与客户信息。
