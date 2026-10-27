# SeekMem 运行手册（从零到跑通）

本文面向评审：按顺序执行即可完整复现 SeekMem。整体约 30-60 分钟（大头是 PowerContext 依赖安装与模型下载）。

## 0. 架构与组件

```
┌─────────────────────────────── 浏览器（工作台 UI，:9100）───────────────────────────────┐
│  问答页（提问→回答+引用）  资料库页（导入/删除）  记忆页（PowerContext 记忆面板）  证据页  │
└──────────────────────────────────────────┬─────────────────────────────────────────────┘
                                           │ HTTP
┌──────────────────────────────────────────▼─────────────────────────────────────────────┐
│ SeekMem 后端（FastAPI，app/main.py）                                                    │
│  ├─ ingest   文档分块→向量化→入库                                                        │
│  ├─ retrieval 三路召回：纯向量 / 单语句混合检索 / 结构化最近   ← seekdb                   │
│  ├─ memory   工位(scope)管理 + 记忆读写                ← PowerContext                    │
│  ├─ chat     编排：记忆召回→检索→LLM 综合→记忆沉淀（LLM 可缺席，自动降级）                │
│  └─ traces   全部 SQL/检索/记忆读写留痕 evidence/*.jsonl                                 │
└───────┬──────────────────────┬──────────────────────┬─────────────────────────┬────────┘
        │ MySQL 协议 :2881     │ HTTP :8010           │ HTTP :11435             │ HTTP :11434
   ┌────▼─────┐          ┌─────▼──────┐        ┌──────▼──────┐         ┌────────▼───────┐
   │  seekdb  │          │PowerContext│        │ bge-m3 嵌入  │         │ LLM(可选)      │
   │ 服务端    │          │ v1.2.0     │        │ OpenAI 兼容  │         │ OpenAI 兼容     │
   └──────────┘          └────────────┘        └─────────────┘         └────────────────┘
```

| 组件 | 版本（本机实测） | 作用 | 是否必需 |
|---|---|---|---|
| seekdb | v1.4.0 | 资料存储 + 向量/全文/结构化混合检索 | **必需** |
| PowerContext | v1.2.0 | 工位记忆（scope/remember/search/entries） | **必需** |
| bge-m3 嵌入服务 | sentence-transformers | 文本向量化（1024 维，OpenAI 兼容协议） | **必需** |
| LLM | qwen2.5-3b（llama-cpp-python） | 综合回答 + 记忆提炼（OpenAI 兼容协议） | 可选，缺席自动降级 |
| Python | 3.10+（实测 3.12/3.13 均可） | 运行 SeekMem | 必需 |

## 1. 部署 seekdb

任选其一（seekdb 兼容 MySQL 协议，端口 2881）：

**方式 A：Docker（官方镜像，最快）**

```bash
docker run -d --name seekdb -p 2881:2881 -p 2886:2886 -v ./data:/var/lib/oceanbase oceanbase/seekdb:latest
```

**方式 B：Linux 一键脚本**

```bash
curl -fsSL https://obportal.s3.ap-southeast-1.amazonaws.com/download-center/opensource/seekdb/seekdb_install.sh | bash
```

就绪检查：`mysql -h127.0.0.1 -P2881 -uroot -e "SELECT version();"`（无 mysql 客户端时跳过，SeekMem 启动会自动验证）。

## 2. 部署 PowerContext

```bash
# Python 3.12 + uv
git clone https://github.com/oceanbase/powercontext.git && cd powercontext
git checkout v1.2.0        # 冻结基线 tag
uv venv .venv && source .venv/bin/activate
uv pip sync competition/requirements.txt    # pylibseekdb 轮子走 files.pythonhosted.org 直链
```

启动（持久化后端选一种；下面用嵌入式 seekdb，数据落本地目录，最简）：

```bash
export POWERCONTEXT_HOME=$HOME/pc-seekmem && mkdir -p $POWERCONTEXT_HOME
export POWERCONTEXT_SERVER_DATABASE_KIND=seekdb
export POWERCONTEXT_SERVER_DATABASE_PATH="$POWERCONTEXT_HOME/seekdb-data"
export POWERCONTEXT_SERVER_HTTP_HOST=0.0.0.0
export POWERCONTEXT_SERVER_HTTP_PORT=8010
export POWERCONTEXT_SERVER_ALLOW_UNAUTHENTICATED_NON_LOOPBACK=true
# 记忆检索的向量化能力（下一步会启动该服务）
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_MODEL=openai:bge-m3
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_PROFILE_ID=bge-m3
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_BASE_URL=http://127.0.0.1:11435/v1
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_DIMENSION=1024
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_NORMALIZATION=unit
export POWERCONTEXT_SERVER_INFERENCE_EMBEDDING_BATCH_SIZE=8
powercontext server run --no-env-file
```

就绪检查：`curl http://127.0.0.1:8010/health/ready` 返回 JSON 即可。

> 注意：PowerContext 用 pymysql + aiomysql 组合时 pymysql 需 1.1.x（2.x 的 escape 与 aiomysql 0.3 不兼容）；requirements.txt 已锁版本，uv pip sync 不会踩这个坑。

## 3. 启动 bge-m3 嵌入服务

```bash
pip install sentence-transformers modelscope fastapi uvicorn
python scripts/embed_server.py   # 监听 :11435，首次启动自动从 ModelScope 下载 BAAI/bge-m3（约 2.3GB）
```

就绪检查：`curl http://127.0.0.1:11435/healthz`。

已有 OpenAI 兼容嵌入服务（如硅基流动）也可直接替换：改 `EMBED_BASE_URL`/`EMBED_MODEL`/`EMBED_DIM` 三个环境变量即可（维度必须与已入库向量一致，建议首次就定好）。

## 4. （可选）启动本地 LLM

```bash
pip install llama-cpp-python huggingface_hub
huggingface-cli download Qwen/Qwen2.5-3B-Instruct-GGUF qwen2.5-3b-instruct-q4_k_m.gguf --local-dir ./models
python -m llama_cpp.server --model ./models/qwen2.5-3b-instruct-q4_k_m.gguf --port 11434 --n_ctx 4096 --n_threads 8
```

或改 `LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY` 接任意 OpenAI 兼容服务（DeepSeek、Qwen、GLM 等均可）。完全不接 LLM 也能跑：问答自动降级为"纯检索摘要"模式，seekdb 检索与 PowerContext 记忆功能不受影响（实测证据见 `evidence/degraded_mode_test.txt`）。

## 5. 启动 SeekMem

```bash
cd app
pip install -r requirements.txt
# 如需改连接配置：cp .env.example .env 并编辑（全部有默认值，指向本机各服务）
uvicorn main:app --host 0.0.0.0 --port 9100
```

启动日志出现 `Application startup complete` 即可。浏览器打开 `http://127.0.0.1:9100`，右上角健康徽章应显示 seekdb/embedding/powercontext/llm 四项状态。

## 6. 导入种子数据并跑验证

```bash
python scripts/seed.py          # 把 seed_docs/ 下 10 篇示例资料入库（约 1 分钟，向量化为主）
python scripts/verify_demo.py   # 四步验证样例一键复跑，输出 evidence/demo_result.json
```

之后按 `docs/VERIFY.md` 在网页上手工体验验证样例。

## 常见问题

- **seekdb 连不上**：确认 2881 端口与账号密码（`SEEKDB_*` 环境变量）。seekdb 首次启动 bootstrap 约需 1-2 分钟。
- **入库报 503 embedding 不可用**：嵌入服务未启动或维度与 `EMBED_DIM` 不符。注意：换嵌入模型必须清空重建（删除 seekmem 库重新 seed），否则新旧向量不在同一空间。
- **PowerContext 健康检查 degraded**：`/health/ready` 里 `inference.embedding` 超时通常是嵌入服务没起；记忆的语义检索会退化，但 remember/entries 仍可用。
- **LLM 回答慢**：本地 3B 模型 CPU 生成约 30-90 秒/次；云端 API 则秒级。生成期间可切其他页签。
