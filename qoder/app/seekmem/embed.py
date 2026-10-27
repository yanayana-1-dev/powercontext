"""Embedding 客户端：OpenAI 兼容 /v1/embeddings（默认本地 bge-m3 服务）。"""
import requests

from . import config, traces

_SESSION = requests.Session()


class EmbedError(RuntimeError):
    pass


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化；单条超长截断（与本地 embed 服务一致）。"""
    if not texts:
        return []
    payload = {"model": config.EMBED_MODEL, "input": [str(t)[:4000] for t in texts]}
    try:
        resp = _SESSION.post(
            f"{config.EMBED_BASE_URL.rstrip('/')}/embeddings", json=payload, timeout=120
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda d: d.get("index", 0))
        vecs = [d["embedding"] for d in data]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise EmbedError(f"embedding 服务不可用或返回格式异常（{config.EMBED_BASE_URL}）: {exc}") from exc
    for v in vecs:
        if len(v) != config.EMBED_DIM:
            raise EmbedError(f"embedding 维度不符: got {len(v)}, expect {config.EMBED_DIM}")
    traces.record("retrieval_trace.jsonl", {
        "kind": "embed", "service": config.EMBED_BASE_URL, "model": config.EMBED_MODEL,
        "inputs": len(texts), "dim": config.EMBED_DIM,
    })
    return vecs


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]
