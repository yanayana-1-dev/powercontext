"""SeekMem · 追忆工作台 —— FastAPI 入口。

启动：cd app && uvicorn main:app --host 0.0.0.0 --port 9100
首次启动自动建库建表（seekdb 服务端模式，MySQL 协议）。
"""
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from seekmem import chat as chat_mod
from seekmem import config, db, embed, ingest, llm, memory, retrieval, schema, traces

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="SeekMem 追忆工作台", version="1.0.0")
_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.on_event("startup")
def _startup() -> None:
    schema.ensure_schema()


# ---------- 基础 ----------

@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


@app.get("/api/health")
def health():
    status = {}
    try:
        db.query("SELECT 1 AS ok", channel="health")
        status["seekdb"] = "ok"
    except Exception as exc:  # noqa: BLE001
        status["seekdb"] = f"fail: {exc}"
    status["embedding"] = "unconfigured"
    try:
        embed.embed_texts(["ping"])
        status["embedding"] = "ok"
    except Exception as exc:  # noqa: BLE001
        status["embedding"] = f"fail: {exc}"
    status["powercontext"] = "unknown"
    try:
        import requests as _rq
        r = _rq.get(f"{config.POWERCONTEXT_BASE_URL.rstrip('/')}/health/ready", timeout=5)
        status["powercontext"] = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        status["powercontext"] = f"fail: {exc}"
    status["llm"] = "ok" if llm.enabled(force_check=True) else "degraded（纯检索模式）"
    return {"app": "SeekMem", "components": status}


# ---------- 资料库 ----------

class IngestBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    source: str = Field(default="", max_length=300)
    tags: str = Field(default="", max_length=200)
    content: str = Field(min_length=1)


@app.post("/api/ingest")
def api_ingest(body: IngestBody):
    try:
        return ingest.ingest_document(body.title, body.source, body.tags, body.content)
    except embed.EmbedError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/documents")
def api_documents():
    return {"documents": ingest.list_documents()}


@app.delete("/api/documents/{doc_id}")
def api_delete_document(doc_id: int):
    ingest.delete_document(doc_id)
    return {"ok": True}


# ---------- 问答 ----------

class ChatBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    user: str = Field(min_length=1, max_length=64)


@app.post("/api/chat")
def api_chat(body: ChatBody):
    return chat_mod.answer(body.question, body.user)


@app.post("/api/search")
def api_search(body: ChatBody):
    """纯检索（不写记忆、不调 LLM），用于直接体验 seekdb 混合检索。"""
    ret = retrieval.retrieve(body.question)
    return {"question": body.question, **ret}


# ---------- 记忆 ----------

@app.get("/api/memory")
def api_memory(user: str):
    scope = memory.ensure_scope(user)
    scope_id = scope.get("scope_id", "")
    welcome = memory.search(scope_id, "当前进行中的事项 未完成的任务 最近确认的结论", limit=5) if scope_id else []
    entries = memory.list_entries(scope_id) if scope_id else []
    return {"user": user, "scope_id": scope_id, "welcome": welcome, "entries": entries}


# ---------- 证据 ----------

@app.get("/api/traces")
def api_traces(limit: int = 30):
    return {
        "sql": traces.tail("sql_trace.jsonl", limit),
        "retrieval": traces.tail("retrieval_trace.jsonl", limit),
        "memory": traces.tail("memory_trace.jsonl", limit),
        "chat": traces.tail("chat_trace.jsonl", limit),
    }
