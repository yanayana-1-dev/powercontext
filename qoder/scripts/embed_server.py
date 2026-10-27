# bge-m3 嵌入服务（OpenAI 兼容 /v1/embeddings）：SeekMem 与 PowerContext 共用
# 依赖：pip install sentence-transformers modelscope fastapi uvicorn
# 用法：python scripts/embed_server.py  （默认 0.0.0.0:11435；首次运行自动从 ModelScope 下载 BAAI/bge-m3）
# 来源：改编自本队 Dify 赛道（雾隐镇编年史）同款服务，行为一致
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
