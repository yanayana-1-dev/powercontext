"""混合检索（seekdb 核心）：三路召回 + RRF 融合。

- channel "vector"  ：纯向量召回 —— ORDER BY l2_distance(...) APPROXIMATE
- channel "hybrid"  ：单语句混合检索 —— WHERE MATCH(content) AGAINST(...) 过滤
                       + ORDER BY l2_distance(...) APPROXIMATE 向量排序（向量索引自适应扫描）
- channel "recent"  ：结构化条件 —— ORDER BY created_at DESC 取最近资料
融合用 Reciprocal Rank Fusion，全部 SQL 与命中明细写入 retrieval/sql trace 供核对。
"""
import time

from . import db, embed, traces

RRF_K = 60
VECTOR_TOPK = 8
HYBRID_TOPK = 8
RECENT_TOPK = 5
FINAL_TOPK = 6

_SELECT = (
    "SELECT c.id, c.doc_id, c.seq, c.doc_title, c.source, c.tags, c.heading, c.content, "
    "c.created_at, l2_distance(c.embedding, %s) AS dist "
    "FROM sm_chunks c "
)


def _fmt_row(r: dict, channel: str, rank: int) -> dict:
    return {
        "chunk_id": r["id"],
        "doc_id": r["doc_id"],
        "doc_title": r["doc_title"],
        "source": r["source"],
        "tags": r["tags"],
        "heading": r["heading"],
        "content": r["content"],
        "distance": round(float(r["dist"]), 6) if r.get("dist") is not None else None,
        "channel": channel,
        "rank": rank,
    }


def retrieve(question: str, k: int = FINAL_TOPK) -> dict:
    """返回 {"hits": 融合后 top-k, "channels": {name: [命中]}, "elapsed_ms": ...}"""
    t0 = time.time()
    qvec = embed.embed_one(question)
    vec_literal = _vec_literal(qvec)

    # A. 纯向量召回
    sql_a = _SELECT + "ORDER BY l2_distance(c.embedding, %s) APPROXIMATE LIMIT %s"
    rows_a, ms_a = db.query(sql_a, (vec_literal, vec_literal, VECTOR_TOPK), channel="retrieval.vector")

    # B. 单语句混合检索：全文过滤 + 向量排序
    sql_b = (
        _SELECT
        + "WHERE MATCH(c.content) AGAINST(%s) "
        + "ORDER BY l2_distance(c.embedding, %s) APPROXIMATE LIMIT %s"
    )
    rows_b, ms_b = db.query(sql_b, (vec_literal, question, vec_literal, HYBRID_TOPK), channel="retrieval.hybrid")

    # C. 结构化：最近资料
    sql_c = (
        "SELECT c.id, c.doc_id, c.seq, c.doc_title, c.source, c.tags, c.heading, c.content, "
        "c.created_at, NULL AS dist FROM sm_chunks c "
        "ORDER BY c.created_at DESC, c.id DESC LIMIT %s"
    )
    rows_c, ms_c = db.query(sql_c, (RECENT_TOPK,), channel="retrieval.recent")

    channels = {
        "vector": [_fmt_row(r, "vector", i + 1) for i, r in enumerate(rows_a)],
        "hybrid": [_fmt_row(r, "hybrid", i + 1) for i, r in enumerate(rows_b)],
        "recent": [_fmt_row(r, "recent", i + 1) for i, r in enumerate(rows_c)],
    }

    # RRF 融合（recent 通道权重低，仅补充语境）
    weights = {"vector": 1.0, "hybrid": 1.0, "recent": 0.4}
    fused: dict[int, dict] = {}
    for name, rows in channels.items():
        for r in rows:
            cid = r["chunk_id"]
            gain = weights[name] / (RRF_K + r["rank"])
            if cid in fused:
                fused[cid]["score"] += gain
                fused[cid]["channels"].append(name)
            else:
                fused[cid] = {**r, "score": gain, "channels": [name]}
    ranked = sorted(fused.values(), key=lambda x: -x["score"])[:k]
    for i, r in enumerate(ranked):
        r["final_rank"] = i + 1
        r["preview"] = r["content"][:260]

    elapsed = (time.time() - t0) * 1000
    traces.record("retrieval_trace.jsonl", {
        "kind": "retrieve", "question": question[:200],
        "channels": {n: [r["chunk_id"] for r in rows] for n, rows in channels.items()},
        "fused_top": [{"chunk_id": r["chunk_id"], "doc": r["doc_title"], "score": round(r["score"], 6)} for r in ranked],
        "sql_ms": {"vector": round(ms_a, 1), "hybrid": round(ms_b, 1), "recent": round(ms_c, 1)},
        "total_ms": round(elapsed, 1),
    })
    return {"hits": ranked, "channels": channels, "elapsed_ms": round(elapsed, 1)}


def _vec_literal(vec: list[float]) -> str:
    import json
    return json.dumps([round(float(x), 7) for x in vec])
