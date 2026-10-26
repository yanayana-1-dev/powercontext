# Mini KB — 浏览器端侧向量化 × seekdb 混合检索

> 2026 OceanBase 数据库大赛 · 阶跃星辰赛道：端侧 AI 推理性能优化与 seekdb 混合检索。

一个迷你知识库应用：**导入语料 → 浏览器本地向量化 → 写入 seekdb → seekdb 端内混合检索**。全部 embedding 推理发生在用户浏览器（Transformers.js，WebGPU 优先 / WASM 自动降级），存储与检索全部由 seekdb 完成。

## 1. 架构

```
┌─────────────── 浏览器（推理位置） ───────────────┐
│  Transformers.js (@huggingface/transformers)     │
│   • all-MiniLM-L6-v2 / bge-small-zh-v1.5 (ONNX) │
│   • dtype: q8 / fp16 / fp32，pooling+normalize    │
│   • device: WebGPU 优先，失败自动降级 WASM（UI 实时显示）│
│   • 批量编码（batch 可调 1/8/16/32）              │
└──────────────┬───────────────────▲───────────────┘
        写入向量/原文      检索结果
┌──────────────▼───────────────────┴───────────────┐
│  Node 中间层（server/server.mjs，无框架）         │
│   • /api/* 存储/检索/刷新（见下）                  │
│   • /hf/* 模型下载同源代理（仅转发字节，不做推理）  │
└──────────────┬───────────────────────────────────┘
               │ MySQL 协议 (mysql2)
┌──────────────▼───────────────────────────────────┐
│  seekdb v1.4（嵌入式或 server 模式）               │
│   • HEAP 表 + VECTOR(dim) + ik 全文 + HNSW(vsayg) │
│   • 混合检索 = 单条 SQL 内完成：                    │
│     WHERE MATCH(...) AGAINST(...)                 │
│     ORDER BY l2_distance(embedding, ?) APPROXIMATE│
│   • CALL dbms_index_manager.refresh()（增量刷新）  │
└──────────────────────────────────────────────────┘
```

**合规说明**：云端零推理调用——文档与查询向量化均在本页浏览器内完成；模型文件经本地 `/hf` 代理从公共 HF 镜像 CDN 下载（仅字节转发）；向量/原文存储与全文+向量混合检索全部在 seekdb 端完成，客户端不做两路结果拼合。

## 2. 快速开始

前提：Node.js ≥ 20；一个运行中的 seekdb（默认 `127.0.0.1:2881`，root 空密码）。

```bash
# 可选：用 Docker 启动 seekdb
docker run -d --name seekdb -p 2881:2881 -p 2886:2886 \
  -v ./data:/var/lib/oceanbase oceanbase/seekdb:latest

cd stepfun
npm install
npm start            # 等价于 node server/server.mjs
# 打开 http://127.0.0.1:5173 （推荐 Chrome / Edge 最新版）
```

环境变量：`SEEKDB_HOST / SEEKDB_PORT / SEEKDB_USER / SEEKDB_PASSWORD / SEEKDB_DATABASE / PORT`。

页面操作顺序：**加载模型 →（自动建集合提示维度）创建集合 → 粘贴/选择文档导入 → 检索**。导入完成默认自动调用索引刷新，保证新文档可检索。

## 3. 模型与量化

| 项 | 说明 |
|---|---|
| 模型 | [Xenova/all-MiniLM-L6-v2](https://huggingface.co/Xenova/all-MiniLM-L6-v2)（384d，英文，默认）；[Xenova/bge-small-zh-v1.5](https://huggingface.co/Xenova/bge-small-zh-v1.5)（512d，中文）。均为公开开源 ONNX 权重 |
| 量化 | `q8`（INT8，权重量化，默认）、`fp16`（半精度）、`fp32`（对照） |
| 体积 | all-MiniLM-L6-v2：fp32 ≈ 90 MB，q8 ≈ 22.7 MB（压缩比 ≈ 4.0×）；fp16 ≈ 45 MB |
| 后端策略 | INT8 量化图运行于 WASM EP（WebGPU EP 对 QLinear 算子支持不完整）；fp16/fp32 优先 WebGPU，初始化失败自动降级 WASM；界面顶栏实时显示当前后端与降级原因 |
| 质量影响 | 应用内置「量化对比」按钮：同一语料与查询集分别用 q8/fp32 编码，输出编码耗时与 top-5 检索重合率（质量代理指标），实测见第 5 节 |

## 4. seekdb 数据层

建表（由“创建集合”自动执行，维度随所选模型）：

```sql
CREATE TABLE `stepfun_kb`.`kb_<name>` (
  id VARCHAR(64) PRIMARY KEY,
  content TEXT,
  embedding VECTOR(<dim>),
  FULLTEXT INDEX idx_ft (content) WITH PARSER ik,
  VECTOR INDEX idx_vec (embedding) WITH (DISTANCE = l2, TYPE = hnsw, LIB = vsag)
) ORGANIZATION = HEAP;
```

三种检索模式（均为 seekdb 端执行，服务端只转发 SQL）：

- **hybrid（默认）**：`SELECT id, content, l2_distance(embedding, ?) AS d FROM t WHERE MATCH(content) AGAINST(?) ORDER BY d APPROXIMATE LIMIT k` —— 全文过滤与向量排序融合在**单条 SQL** 内；
- **vector**：`ORDER BY l2_distance(...) APPROXIMATE`；
- **fts**：`MATCH ... AGAINST`。

索引刷新：导入后自动（可关）执行 `CALL dbms_index_manager.refresh()`（seekdb-js 同款调用），失败时回退 `CALL DBMS_VECTOR.REFRESH_INDEX('idx_vec','kb_<name>',NULL,1,'FAST')`，保证增量写入的向量进入可检索索引。

> 说明：中间层使用 mysql2 走 seekdb 的 MySQL 协议通道（seekdb README 推荐接入方式）。seekdb npm 包（`seekdb@1.4.0`）当前版本在 npm 安装时会因依赖链中 `workspace:*` 协议无法解析（`npm i seekdb` 可复现），故未直接依赖；其 `collection.add` / `hybridSearch` / `refresh_index` 对应的本应用路径均为等价 SQL。

## 5. 基础性能优化与实测

优化点（应用内可一键复测）：

1. **embedding 批处理**：逐条 vs 批量（batch=16）编码对比（「批处理耗时对比」按钮）；
2. **模型加载**：进度回调 + 浏览器 Cache API 二次加载免下载；
3. **检索延迟**：20 次采样输出 p50/均值（「检索延迟采样」按钮）；
4. **索引刷新策略**：手动/导入后自动两种模式，刷新耗时在导入状态栏显示；
5. **写入分块**：INSERT 按 100 行分块，规避绑定参数上限。

**实测数据**（Windows 11 + Docker seekdb v1.4.0.0 + Chrome 最新版 + all-MiniLM-L6-v2 q8/WASM，Playwright 驱动真实浏览器自动化采集，30 段中英文语料）：

| 指标 | 数值 |
|---|---|
| 模型加载（q8，23MB，含代理下载） | 26.4 s（二次加载走浏览器缓存，免下载） |
| 导入 30 篇：向量化（batch=16） | 1,163 ms（≈39 ms/篇） |
| 导入 30 篇：seekdb 写入 | 162 ms |
| 导入 30 篇：索引刷新（增量 HNSW 合并） | 527 ms |
| 批处理对比（16 段） | 逐条 14.4 ms/条 → 批量 12.0 ms/条，**1.20x** |
| seekdb 向量检索延迟（20 次，limit=10） | **p50 = 1.74 ms**，均值 1.75 ms，max 2.29 ms |
| 混合检索单条（hybrid，全文过滤+向量排序） | 3.2 ~ 5 ms |
| 量化对比（24 段编码） | q8 = 592 ms vs fp32 = 976 ms（**q8 快 1.65x**，体积 ≈4x 小）；top-5 检索重合率 76%（本批中文语料上轻度退化，中文任务建议 bge-small-zh 复评） |

**检索正确性抽查**（语料见 `testdata/`，预期矩阵见 `testdata/test-queries.md`）：

- 混合模式 `HNSW` → 精确命中 D02（唯一含该词段落）；同查询切向量模式 → D02/D05/D25/**D16**（D16 无该字面词、靠语义捞回）——直观展示两通道差异；
- 全文 `47.6 亿元` → 精确命中财报段 D13；
- 混合 `数据库 索引` → 命中 D01/D02/D03/D16/D22/D26 全部 6 段相关语料，按语义排序；
- 无关查询 `量子物理超导`：fts/hybrid 返回空（诚实展示"命中 0"）；向量模式返回低相关邻居（无相关性门槛，距离均 >0.97）——按需选模式。

## 6. 文件清单

```
stepfun/
├── README.md            本文件（一页技术说明）
├── package.json         依赖：mysql2
├── server/server.mjs    Node 中间层：API + 静态托管 + /hf 模型代理
├── web/index.html       单页应用
├── web/app.js           Transformers.js 加载/推理 + 全部 UI 逻辑
├── web/style.css        样式
└── run.sh / run.ps1     一键启动脚本
```

## 7. 已知边界

- 模型下载依赖 hf-mirror 公共镜像（经本地代理）；离线环境可将 ONNX 权重放置于本地并调整 `env.remoteHost`。
- INT8 (q8) 图在 WASM EP 运行是刻意选择（WebGPU EP 量化算子支持不完整会抛 offset 越界）；fp16/fp32 完整演示 WebGPU 优先与自动降级。
- 集合维度绑定建表时的模型维度，切换模型请新建集合（界面有提示）。
- all-MiniLM-L6-v2 是英文模型，中文语义查询召回偏弱（实测"浏览器里能跑深度学习吗"未召回 WebGPU 段）——**中文知识库请切换 bge-small-zh-v1.5**（下拉框已内置，512d）。
