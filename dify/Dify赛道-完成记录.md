# OceanBase 大赛 · Dify 生态赛道（雾隐镇编年史）完成记录

> 2026-10-26 完成。赛道：2026 生态赛道 · Dify 题（Just for Fun，20 分）。
> 技术选型：Dify 1.18.0（12 节点 Chatflow 核心编排）+ seekdb v1.4.0（世界档案库：5 张表 + 向量/全文/结构化混合检索）+ PowerContext v1.2.0（旅行者个人记忆，持久化后端 = 嵌入式 seekdb）。

## 提测信息

| 项 | 值 |
|---|---|
| 仓库 | `https://github.com/yanayana-1-dev/powercontext`（报名私有仓库） |
| 分支 | **`crq-ecosystem`**（与高德题同分支；本提交在 gaode/SKILL.md 之上 fast-forward） |
| Commit | **`07c51edd0f258fba998c2273ff2b718539536cc9`** |
| 基线 | crq-ecosystem 前一提交（最终上游基线仍是 d647275c308e...） |
| 变更 | 新增 `dify/` 目录 19 个文件：DSL（25KB）+ backend（bridge/init/seed/tools）+ deploy override + evidence 运行证据 |

提测平台填：仓库 URL + 分支 `crq-ecosystem` + 完整 commit `07c51edd0f258fba998c2273ff2b718539536cc9`。提交动作等队长明确命令。

## 作品：雾隐镇编年史（Mistvale Chronicle）

文字冒险游戏：玩家向"镇志官"（AI）口述行动（如"我叫墨鸦，我要在喷泉里洗澡"），镇志官依世界设定续写故事、把行动写成镇志条目**永久存入 seekdb**、决定要长期记住什么写入 PowerContext。核心趣味：**玩家的胡闹变成官方史实，小镇用历史回应每一个人**（跨会话、跨设备一致）。

- DSL：`dify/mistvale-chronicle.yml`（12 节点线性 Chatflow：清洗输入→识别身份→查个人记忆→查世界档案→拼装上下文→LLM 执笔→解析→存入镇志→写入记忆→回复；HTTP 节点全部 fail-branch + 重试降级）
- seekdb 五张表：`mistvale_lore`（39 条设定，VECTOR+FULLTEXT IK）、`mistvale_events`（编年史，每轮新增带向量）、`mistvale_players`（名号→scope 登记）、`mistvale_sessions`（Dify conversation→scope 绑定）、`mistvale_clock`（编年时钟单行表）
- 混合检索 SQL（每轮 3 条）：向量召回设定 / `MATCH AGAINST` 过滤 + `l2_distance APPROXIMATE` 排序史事（EXPLAIN = VECTOR INDEX ADAPTIVE SCAN PRE-FILTER）/ 近事结构化排序；`evidence/sql_trace.jsonl` 逐条留痕
- 模型：Qwen2.5-3B-Instruct GGUF（llama.cpp :11434）+ bge-m3 1024 维（sentence-transformers :11435），全本地 CPU；云端 OpenAI 兼容服务改 .env 即换

## 本机运行状态（供队长体验）

- Dify WebApp：`http://127.0.0.1:9020/chat/A9c449LwqObKtqBC`（公开访问码；控制台 `mistvale@example.com / Mistvale2026`）
- 最终应用 app_id：`552ea8c6-7e31-47cf-99f2-c0ff5cb7e46b`（API key `app-OndayVqdvoxgHE7nSSVEhD4r`）
- 服务：bridge :9010（WSL nohup）、PowerContext :8010（WSL nohup）、seekdb 2881（systemd）、llama :11434 + embed :11435（WSL systemd 用户单元 `mistvale-llama` / `mistvale-embed`）
- WSL 服务管理：`bash /mnt/d/AAAK/game/oceanbase/tmp/dify/mistvale_services.sh status`（install/uninstall/status）

## 验证样例（三轮，全部实测通过）

1. `我叫墨鸦，我要在广场喷泉里洗个澡` → 建档 + 叙事引用《喷泉洁净条例》+ 镇志条目入库（evidence/webapp_chat_round1.png）
2. `我去当铺把上次洗喷泉用的肥皂卖掉` → 召回上一轮史事关联叙事
3. `我想半夜去雾林里喊雾的名字` → 引用北境雾林/守墓人老乌鸦设定
   - 会话 `eee295de-7299-4b8e-8e62-1cf6353dcb6c`（evidence/demo_chat.json + chat_conversations.json）；事件表新增 id 22/23/24（evidence/seekdb_state.txt）

## 部署过程中踩的坑（复用价值）

1. **Dify 端口 8080 被 ZCode IAB（CEF 调试端口）占用** → `EXPOSE_NGINX_PORT=9020`。
2. **Docker Desktop bind mount 上跑 postgres 必坏**（pg_filenode.map 丢失循环 crash recovery）→ override 里把 db/weaviate/redis 卷改命名卷（`!reset` 覆盖）。
3. **dify-sandbox 0.2.15 的 conf 目录被空 bind mount 覆盖** → 从镜像 `docker run --rm ... cat /conf/config.yaml` 拷回 `volumes/sandbox/conf/config.yaml`。
4. **local_sandbox 需要 `DIFY_AGENT_SHELLCTL_AUTH_TOKEN`**（纯 Chatflow 不用它，可忽略重启循环）。
5. **HTTP 节点访问 host.docker.internal 被 ssrf_proxy（squid）拦截** → `.env` 设 `SSRF_PROXY_ALLOW_PRIVATE_IPS=192.168.65.0/24,172.16.0.0/12,10.0.0.0/8,fc00::/7`。
6. **HTTP 节点 JSON body 的变量插值不做转义** → 所有动态文本先经 code 节点清洗（引号/换行）再进 body。
7. **LLM 首轮冷启动超时**（PluginDaemonInternalServerError: killed by timeout）→ 压缩 prompt + max_tokens 900 + llama server 常驻（systemd 用户单元）。
8. **HTTP 节点 keep-alive 复用已断连接**（uvicorn 默认 5s keep-alive）→ bridge `--timeout-keep-alive 120` + HTTP 节点 retry_config（max_retries=2）。
9. **Dify 登录密码是 base64(明文)**（console API 无浏览器初始化/登录时用）；CSRF 头名是 `X-CSRF-Token`；Cookie 认证（access_token/csrf_token/refresh_token 三件套）。
10. **published 版引擎对 conversation variable 的 assigner 写回不可靠**（draft 正常）→ 身份/会话/时钟全部持久化 seekdb，Dify 侧无 conversation variables（也更贴"档案库唯一真相源"叙事）。
11. **PowerContext[seekdb] 嵌入式后端**：`POWERCONTEXT_SERVER_DATABASE_KIND=seekdb` + pylibseekdb 1.4.0.post1 + **pymysql 必须 1.1.x**（2.x escape 与 aiomysql 0.3 不兼容 → remember 500）；embedding 配置三件套（model 带 `openai:` 前缀 + profile_id + dimension=1024 + normalization=unit）。
12. **3B 模型 prompt 调教**：few-shot 实值示例（不是"……"占位）+ temperature 0.6 + max_tokens 900 是质量/速度平衡点；memory 关闭（recent 块+PC 记忆已保证连贯，且 answer 模板回流会污染输出）。

## 复现入口

- 部署手册（含逐命令）：`dify/README.md` 第四章
- 一键脚本（本机）：`tmp/dify/` 下 `world_reset.sh`（重置世界+bridge）、`gen_dsl.py`（重新生成 DSL）、`chat_two_rounds.py`/内联脚本（多轮验证）、`dify_push.sh`（Git Data API 推送）
- 推送核验：`gh api repos/yanayana-1-dev/powercontext/contents/dify?ref=crq-ecosystem` → 19 文件 + .gitkeep
