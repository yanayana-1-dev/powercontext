"""SeekMem 配置：全部通过环境变量 / .env 覆盖，默认值指向本机全本地部署。"""
import os

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()

# seekdb（服务端模式，MySQL 协议）
SEEKDB_HOST = _env("SEEKDB_HOST", "127.0.0.1")
SEEKDB_PORT = int(_env("SEEKDB_PORT", "2881"))
SEEKDB_USER = _env("SEEKDB_USER", "root")
SEEKDB_PASSWORD = _env("SEEKDB_PASSWORD", "")
SEEKDB_DATABASE = _env("SEEKDB_DATABASE", "seekmem")

# Embedding（OpenAI 兼容 /v1/embeddings，本地 bge-m3 默认）
EMBED_BASE_URL = _env("EMBED_BASE_URL", "http://127.0.0.1:11435/v1")
EMBED_MODEL = _env("EMBED_MODEL", "bge-m3")
EMBED_DIM = int(_env("EMBED_DIM", "1024"))

# LLM（OpenAI 兼容 /v1/chat/completions；不可达时自动降级为纯检索模式）
LLM_ENABLED = _env("LLM_ENABLED", "1") not in ("0", "false", "no")
LLM_BASE_URL = _env("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
LLM_MODEL = _env("LLM_MODEL", "qwen2.5-3b-instruct")
LLM_API_KEY = _env("LLM_API_KEY", "local")
LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", "700"))

# PowerContext（记忆与上下文服务）
POWERCONTEXT_BASE_URL = _env("POWERCONTEXT_BASE_URL", "http://127.0.0.1:8010")

# 证据留痕目录（项目根 evidence/，app 的上一级）
EVIDENCE_DIR = _env("EVIDENCE_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "evidence"))
