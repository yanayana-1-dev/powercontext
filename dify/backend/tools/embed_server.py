# 本地 Embedding 服务（开发用）：把 bge-m3 包成 OpenAI 兼容 /v1/embeddings
# 依赖：sentence-transformers + modelscope 下载的 BAAI/bge-m3
# 用法: python embed_server.py  (默认 0.0.0.0:11435)
import os
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

MODEL_PATH = os.environ.get("BGE_M3_PATH", "")
PORT = int(os.environ.get("EMBED_PORT", "11435"))

app = FastAPI(title="local-bge-m3-embeddings")
_model = None


def get_model():
    global _model
    if _model is None:
        t0 = time.time()
        _model = SentenceTransformer(MODEL_PATH, device="cpu")
        print(f"model loaded in {time.time()-t0:.1f}s", flush=True)
    return _model


class EmbedRequest(BaseModel):
    model: str = "bge-m3"
    input: object


@app.get("/healthz")
def healthz():
    return {"ok": True, "model_path": MODEL_PATH}


@app.post("/v1/embeddings")
def embeddings(req: EmbedRequest):
    texts = req.input if isinstance(req.input, list) else [req.input]
    texts = [str(t)[:4000] for t in texts]
    if not texts:
        raise HTTPException(400, "empty input")
    model = get_model()
    vecs = model.encode(texts, normalize_embeddings=True, batch_size=8)
    return {
        "object": "list",
        "model": req.model,
        "data": [{"object": "embedding", "index": i, "embedding": [float(x) for x in v]}
                 for i, v in enumerate(vecs)],
        "usage": {"prompt_tokens": sum(len(t) for t in texts), "total_tokens": sum(len(t) for t in texts)},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
