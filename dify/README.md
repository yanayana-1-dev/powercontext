# 雾隐镇编年史（Mistvale Chronicle）

> OceanBase 大赛 2026 · 生态赛道 · Dify 题（Just for Fun——用 Dify 做一个好玩的智能应用）
> 技术选型：**Dify（核心编排） + seekdb（世界档案库，向量/全文/结构化混合检索） + PowerContext（旅行者个人记忆）**

## 一、这是什么

雾隐镇是一座终年被雾笼罩的小镇：喷泉不许洗澡、帽子税年年全票通过、墓碑会自己换位置。你是一位初来乍到的旅行者，向镇志官（AI 扮演的档案员）口述你的行动——

- **报上名号**（"我叫墨鸦"），镇志官会在档案库为你建档；
- 你说的每件事都会被写进**《镇志》**，正式存入雾隐镇档案库——**世界因此永久改变**；
- 之后无论你换不换会话回来，小镇都记得你干过的事：市政官会翻旧账，当铺老板会提你的旧闻，你上次摸过的猫下次还认得你。

**核心乐趣**：你的胡作非为不会被 AI 一笑置之，而是变成"官方史实"，反哺后续剧情。胡闹 → 存档 → 小镇用历史回应你 → 更荒诞的连锁反应。

一句话样例：

```
你：我叫墨鸦，我要在广场喷泉里洗个澡
镇志官：（一段严肃叙事：全街抽气声、市政官捏皱《喷泉洁净条例》……）
        📜 【镇志 · 纪元4年】 纪元4年：墨鸦擅入中心喷泉广场洗浴，市政官格雷布·扳脸当场开具罚单。
        （本条已由镇志官存入雾隐镇档案库 · 机密等级 4）
```

## 二、技术架构与分工

```
┌────────────────────────────── Dify Chatflow（12 节点，核心编排） ──────────────────────────────┐
│ 开始 → 清洗输入 → [HTTP] 识别身份 → 取出身份 → [HTTP] 查个人记忆 → [HTTP] 查世界档案          │
│      → 拼装上下文 → LLM 镇志官执笔 → 解析镇志输出 → [HTTP] 存入镇志 → [HTTP] 写入记忆 → 回复   │
└──────────────────────────────────────────────┬───────────────────────────────────────────────┘
                                               │ HTTP（全部经 Chronicle Bridge）
┌──────────────────────────────────────────────▼───────────────────────────────────────────────┐
│ Chronicle Bridge（FastAPI 桥接服务，backend/bridge.py）                                        │
│  - /v1/chronicle/identify     身份识别+建档   → seekdb players/sessions 表 + PowerContext     │
│  - /v1/chronicle/memory_search 转发个人记忆检索 → PowerContext /v1/memory/search               │
│  - /v1/chronicle/recall       世界档案三条混合检索 SQL → seekdb                                │
│  - /v1/chronicle/archive      新条目 embed 后写入 seekdb（推进编年时钟）                        │
│  - /v1/chronicle/remember     增量记忆写入 → PowerContext /v1/memory/remember                   │
└───────────────┬──────────────────────────────────────────────────────────────────────────────┘
                │
    ┌───────────▼────────────┐            ┌───────────────────────────────┐
    │ seekdb (MySQL :2881)   │            │ PowerContext Server (:8010)   │
    │  mistvale_lore    39条 │            │  持久化后端 = 嵌入式 seekdb    │
    │  mistvale_events  史事 │            │  （PowerContext[seekdb] 配置） │
    │  mistvale_players 登记 │            │  记忆检索 = auto（FTS/向量）    │
    │  mistvale_sessions 会话│            │  embedding = bge-m3 (1024维)  │
    │  mistvale_clock   编年 │            └───────────────────────────────┘
    │  VECTOR + FULLTEXT(IK) │
    └────────────────────────┘
```

### 三个组件各自干了什么（对应赛题"技术结合有效"）

| 组件 | 实际作用 | 证据位置 |
|---|---|---|
| **Dify** | 12 节点 Chatflow 承担全部流程编排：输入清洗（防 JSON 注入）、HTTP 调用、失败降级分支（fail-branch + 重试）、LLM 调用、JSON 解析容错、Answer 模板渲染。DSL 全文见 `mistvale-chronicle.yml` | `evidence/workflow_canvas.png` |
| **seekdb** | ① 应用数据存储：世界设定、编年史、玩家登记、会话映射、编年时钟共 5 张表，全部增删改查走 seekdb；② 检索引擎：每轮对话跑三条单语句混合检索（见下）；③ 向量写入：每个新事件现场 embedding 后 `INSERT ... VECTOR`，HNSW 索引即时生效 | `evidence/sql_trace.jsonl`（逐条 SQL + 耗时）、`evidence/seekdb_state.txt` |
| **PowerContext** | 旅行者个人记忆的存取：建档时写身份种子记忆；每轮把"值得长期记住的事"写入 `/v1/memory/remember`；对话前用 `/v1/memory/search` 按语义召回该玩家的过往。PowerContext 自身的数据持久化后端也配置为 **嵌入式 seekdb**（`POWERCONTEXT_SERVER_DATABASE_KIND=seekdb`），即记忆索引同样落在 seekdb 生态内 | `evidence/demo_chat.json` 中第二轮起镇志官能喊出玩家名号 |

### seekdb 混合检索（本作品的技术核心）

`/v1/chronicle/recall` 每轮执行三条单语句 SQL（均可在 `evidence/sql_trace.jsonl` 逐条核对）：

1. **世界设定语义召回**（纯向量 + 结构化过滤）：
   `SELECT ... WHERE importance >= 2 ORDER BY l2_distance(content_vec, '[...]') APPROXIMATE LIMIT 3` —— 玩家行动的语义嵌入直接命中相关设定（实测："在喷泉洗澡" 召回 喷泉广场 / 喷泉洁净条例 / 市政官，正是剧情要触发的三件套）。
2. **史事混合检索**（全文过滤 + 向量排序，一条 SQL 同时吃两种检索）：
   `SELECT ... WHERE MATCH(content) AGAINST('喷泉') AND importance >= 1 ORDER BY l2_distance(content_vec, '[...]') APPROXIMATE LIMIT 3` —— IK 分词全文命中作为预过滤，向量距离排序取最相关的历史事件。EXPLAIN 显示 `VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)`（见 `evidence/sql_trace.jsonl` 同目录说明）。全文为空时自动降级为"重要史事向量召回"。
3. **近事**（纯结构化）：`ORDER BY year DESC, id DESC LIMIT 3` 维持剧情连续性，内容前缀去重防止 LLM 复述。

**编年时钟**（`mistvale_clock`，单行表）：新事件入库时 `clock = max(clock, 事件年 + year_advance)`，下一轮镇志官的"当前时间"由此读取——时间流逝持久化在 seekdb 里，跨会话、跨设备一致。

## 三、目录结构

```
dify/
├── README.md                      # 本文件（作品说明 + 运行说明 + 验证样例）
├── mistvale-chronicle.yml         # 交付物①：Dify Chatflow DSL（Dify 1.18.0 / DSL 0.7.0）
├── backend/                       # 交付物②：桥接服务与初始化脚本
│   ├── bridge.py                  #   FastAPI 桥接服务（Dify HTTP 节点 ⇄ seekdb / PowerContext）
│   ├── bridge_start.sh            #   启动脚本
│   ├── init_seekdb.py             #   seekdb 建表 + 种子数据灌入（现场调用 embedding）
│   ├── requirements.txt
│   ├── .env.example               #   配置占位示例（复制为 .env 使用）
│   └── seed/
│       ├── lore.jsonl             #   世界设定 39 条（地点/NPC/条例/传说/物品）
│       └── events_prehistory.jsonl#   纪元1-3年史前事件 10 条
├── deploy/
│   └── docker-compose.override.yml#   Dify compose 增强（host-gateway + 数据卷名化）
└── evidence/                      # 交付物③：运行证据
    ├── demo_chat.json             #   三轮验证对话全文（message_id 可核对）
    ├── chat_conversations.json    #   Dify 控制台会话记录
    ├── sql_trace.jsonl            #   桥接服务记录的逐条 seekdb SQL + 耗时
    ├── seekdb_state.txt           #   事件表/时钟/玩家表/会话表运行后状态
    ├── webapp_home.png            #   WebApp 首页截图
    ├── webapp_chat_round1.png     #   WebApp 首轮对话截图
    └── workflow_canvas.png        #   Dify 工作流编排画布截图（12 节点）
```

## 四、运行说明

### 4.1 环境与版本

| 组件 | 版本 | 说明 |
|---|---|---|
| Dify | **1.18.0**（官方 docker compose 部署） | 需要 `docker compose v2` |
| seekdb | v1.4.0.0（MySQL 协议端口 2881） | 也可用官方 deb 部署，要求支持 VECTOR + IK 全文索引 |
| PowerContext | v1.2.0（2026-competition 基线 dab24006） | `pip install -e ".[server,cli]"` + `pylibseekdb>=1.4.0.post1` |
| LLM | Qwen2.5-3B-Instruct（GGUF q4_k_m，llama-cpp-python OpenAI 兼容服务） | 任何 OpenAI 兼容 chat 服务均可（vLLM/Ollama/云 API），改 `.env` 即可 |
| Embedding | **BAAI/bge-m3**（1024 维，sentence-transformers 服务化） | 本地 CPU 服务或 SiliconFlow 云端均可 |
| Python | 3.12 | bridge / init 脚本 |
| 硬件 | 16GB 内存即可（本作品在 AMD 8845H 笔记本 CPU 上全程本地运行） | LLM 约 2.1GB 常驻 |

网络说明：全程中国境内直连可用（模型走 ModelScope，Dify 插件走 marketplace.dify.ai，无 GitHub 依赖）。

### 4.2 部署步骤

以下步骤在本机（Linux/WSL2）实测通过；各服务同机部署，跨机部署时把 `127.0.0.1` 换成对应地址。

#### ① 启动 seekdb

```bash
systemctl start seekdb          # 或官方 deb 安装后启动
mysql -h127.0.0.1 -P2881 -uroot -e "SELECT VERSION()"   # 应显示 5.7.25-OceanBase seekdb-v1.4.x
```

#### ② 启动模型服务（LLM + Embedding）

```bash
# Embedding：bge-m3 → :11435（OpenAI 兼容 /v1/embeddings）
python3 -m venv ~/venvs/mistvale-ai
~/venvs/mistvale-ai/bin/pip install torch sentence-transformers modelscope llama-cpp-python 'pymysql<2' requests
~/venvs/mistvale-ai/bin/python -c "from modelscope import snapshot_download; print(snapshot_download('BAAI/bge-m3'))"
BGE_M3_PATH=<上一步打印的快照目录> nohup ~/venvs/mistvale-ai/bin/python embed_server.py &   # :11435

# LLM：Qwen2.5-3B GGUF → :11434（OpenAI 兼容）
~/venvs/mistvale-ai/bin/python -c "from modelscope import snapshot_download; snapshot_download('Qwen/Qwen2.5-3B-Instruct-GGUF')"  # 取 q4_k_m 文件
nohup ~/venvs/mistvale-ai/bin/python -m llama_cpp.server \
  --model ~/mistvale-models/qwen2.5-3b-instruct-q4_k_m.gguf \
  --port 11434 --n_ctx 4096 --n_threads 12 --n_gpu_layers 0 &
```

`embed_server.py` 的 11 行核心就是 sentence-transformers 的 OpenAI 兼容包装，见仓库附带的 `tools/embed_server.py`（或直接用 Ollama `ollama pull bge-m3` + `qwen2.5:3b` 替代，改 `.env` 端口即可）。

#### ③ 启动 PowerContext Server（持久化后端 = 嵌入式 seekdb）

```bash
python3.12 -m venv ~/venvs/pc-server
~/venvs/pc-server/bin/pip install -e "powercontext-src[server,cli]"   # 官方源码，pyproject 含 server 附加依赖
~/venvs/pc-server/bin/pip install "https://files.pythonhosted.org/.../pylibseekdb-1.4.0.post1-cp312-abi3-manylinux_2_28_x86_64.whl" "pymysql==1.1.1"

export POWERCONTEXT_HOME=$HOME/pc-mistvale
export POWERCONTEXT_SERVER_DATABASE_KIND=seekdb
export POWERCONTEXT_SERVER_DATABASE_PATH=$POWERCONTEXT_HOME/seekdb-data
export POWERCONTEXT_SERVER_HTTP_HOST=0.0.0.0
export POWERCONTEXT_SERVER_HTTP_PORT=8010
export POWERCONTEXT_SERVER_ALLOW_UNAUTHENTICATED_NON_LOOPBACK=true
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_MODEL=openai:bge-m3
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_BASE_URL=http://127.0.0.1:11435/v1
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_PROFILE_ID=bge-m3
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_DIMENSION=1024
nohup ~/venvs/pc-server/bin/powercontext server run --no-env-file &
curl -s http://127.0.0.1:8010/health/ready    # 期待 database:ready, inference.embedding:ready
```

> 注意：`pymysql` 必须 1.1.x（2.x 与 aiomysql 0.3 的 escape 接口不兼容，会导致 remember 500）。

#### ④ 启动 Chronicle Bridge 并初始化 seekdb

```bash
cd dify/backend
cp .env.example .env           # 按需修改端口/密码；占位值默认同机部署可用
bash bridge_start.sh           # 首次自动建 venv 装依赖，:9010
curl -s http://127.0.0.1:9010/healthz

# 初始化世界（建 5 张表 + 灌 49 条种子数据，现场调用 embedding）
python3 init_seekdb.py --drop  # --drop 重置世界；验证脚本见 4.2⑦
```

#### ⑤ 部署 Dify

```bash
git clone --depth 1 --branch 1.18.0 https://github.com/langgenius/dify.git
cd dify/docker
cp .env.example .env
sed -i 's/^EXPOSE_NGINX_PORT=80$/EXPOSE_NGINX_PORT=9020/' .env
# 允许 Dify 容器访问同机服务（PowerContext/bridge/模型服务）：
sed -i 's|^SSRF_PROXY_ALLOW_PRIVATE_IPS=$|SSRF_PROXY_ALLOW_PRIVATE_IPS=192.168.65.0/24,172.16.0.0/12,10.0.0.0/8,fc00::/7|' .env
cp <本仓库>/deploy/docker-compose.override.yml docker-compose.override.yml
docker compose up -d
```

浏览器打开 `http://<host>:9020/install` 创建管理员账户，然后登录。

#### ⑥ 导入 DSL 并配置模型

1. 控制台 → 工作室 → **导入 DSL 文件** → 选 `mistvale-chronicle.yml`（导入后零警告；也可用 API：`POST /console/api/apps/imports {"mode":"yaml-content","yaml_content":<文件内容>}`）。
2. 控制台 → 右上头像 → **设置 → 模型供应商** → 安装 **OpenAI-API-compatible** 插件（marketplace 一键安装）。
3. 在该供应商下**添加两个模型**（凭据见 `.env.example` 注释）：

   | 模型名称 | 类型 | endpoint_url | 其他 |
   |---|---|---|---|
   | `qwen2.5-3b-instruct` | LLM | `http://host.docker.internal:11434/v1` | mode=chat, context_size=4096, api_key 任意非空 |
   | `bge-m3` | Text Embedding | `http://host.docker.internal:11435/v1` | context_size=8192 |

4. 应用编排页右上角**发布**。WebApp 入口：`http://<host>:9020/chat/<访问码>`（应用"访问入口"页可查）。

#### ⑦ 一键验证 seekdb 侧部署

```bash
cd dify/backend && python3 init_seekdb.py --verify
```

会执行一条 `MATCH(...) AGAINST(...) ORDER BY l2_distance(...) APPROXIMATE` 的混合检索并打印结果，作为 seekdb 读写能力的独立核验。

### 4.3 已知限制

- 本地 3B CPU 推理每轮约 40-70s（16 核 8845H 实测）；换云端 LLM 可即时响应，不影响工作流。
- 3B 小模型偶发镇志条目措辞偏差（如条目复述叙事）；叙事主体与功能链路不受影响。
- Dify 的 conversation variable 在 published 版引擎的写回在本环境不稳定，因此身份/会话/编年时钟全部持久化在 seekdb（bridge 管理），不依赖 Dify 会话变量——这也是"档案库是唯一真相源"的设计立场。
- PowerContext 的 scope 为每位旅行者独立创建；删除玩家需同时清 `mistvale_players/sessions` 与 PowerContext scope。

## 五、验证样例（评审可直接执行）

**初始条件**：按第四章部署完成；`python3 init_seekdb.py --drop` 重置世界（39 设定 + 10 史前事件，时钟=纪元4年，玩家表清空可另执行 `TRUNCATE mistvale_players; TRUNCATE mistvale_sessions;`）。

**样例 A：首轮建档 + 条例后果（体验约 1 分钟）**

1. 打开 WebApp，输入：`我叫墨鸦，我要在广场喷泉里洗个澡`
2. 预期：叙事引用《喷泉洁净条例》与市政官格雷布·扳脸，输出镇志条目（机密等级 ≥3）；条目含名号"墨鸦"。
3. 同时可在 seekdb 核对（`evidence/seekdb_state.txt` 为实际记录）：

   ```sql
   SELECT id, year, actor, LEFT(content,60) FROM mistvale_events ORDER BY id DESC LIMIT 1;
   SELECT * FROM mistvale_players;   -- 新增 player_slug + PowerContext scope_id
   SELECT current_year FROM mistvale_clock;
   ```

**样例 B：跨回合记忆（同一会话继续）**

4. 紧接着输入：`我去当铺把上次洗喷泉用的肥皂卖掉`
5. 预期：叙事与镇志条目关联"上次洗喷泉"的前情（召回样例 A 写入的史事）；PowerContext 里新增 deed 记忆。

**样例 C：跨会话记忆 + 编年推进（核心演示）**

6. 点击 **Start New chat**（全新会话），输入：`我叫墨鸦，你想起来我是谁吗`
7. 预期：镇志官能直接喊出"墨鸦"并提及他干过的事——记忆来自 PowerContext（`/v1/memory/search`），玩家身份映射来自 seekdb `mistvale_players` 表（`conversation_id → scope_id` 会话绑定见 `mistvale_sessions`）。
8. 再输入一件大事件（如 `我要在钟楼上宣布自己才是真正的市政官`），随后核对 `mistvale_clock.current_year` 是否按 `year_advance` 推进；下一轮对话的叙事时间应变为新年份。

**样例 D：Dify 工作流核验**

9. 控制台 → 应用 → 监控/日志：可看到每轮 12 节点全部成功、HTTP 节点 URL 与耗时、LLM 输入（含召回的世界档案与个人记忆块）。
10. `evidence/sql_trace.jsonl`：每轮 6 条 seekdb SQL（3 检索 + 1 INSERT + 2 时钟/登记类），含执行耗时。

**恢复初始状态**：`python3 init_seekdb.py --drop`，再手工清两张登记表（见上）；Dify 侧无需重置（会话记录不影响世界状态）。

## 六、参考与致谢

- 世界观与玩法为原创设计；对话生成模型为 Qwen2.5-3B-Instruct（Apache-2.0），Embedding 为 BAAI/bge-m3（MIT）。
- Dify DSL 由本团队按 1.18.0 schema 手工编写（参考官方 `docker/.env.example` 与 graphon 节点实体定义），未复用第三方工作流。
- PowerContext / seekdb 部署方式遵循两项目官方文档；PowerContext 以 `powercontext[seekdb]` 嵌入式后端运行。
