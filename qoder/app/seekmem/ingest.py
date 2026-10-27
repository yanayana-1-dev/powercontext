"""文档入库：读取 → 按标题/段落分块 → 向量化 → 写入 seekdb（同源重导即更新）。"""
import re
from datetime import datetime

from . import db, embed, traces

MAX_CHUNK_CHARS = 600
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def split_chunks(text: str) -> list[dict]:
    """按 Markdown 标题切段，段落聚合到 ~MAX_CHUNK_CHARS；无标题文本按空行聚合。"""
    lines = text.replace("\r\n", "\n").split("\n")
    sections: list[tuple[str, list[str]]] = []  # (heading, body_lines)
    heading = ""
    for line in lines:
        m = _HEADING_RE.match(line.strip())
        if m:
            heading = m.group(2).strip()
            sections.append((heading, []))
        else:
            if not sections:
                sections.append((heading := "", []))
            sections[-1][1].append(line)

    chunks: list[dict] = []
    for head, body_lines in sections:
        paras: list[str] = []
        buf: list[str] = []
        buf_len = 0
        for line in body_lines:
            if line.strip() == "" and buf:
                paras.append("\n".join(buf).strip())
                buf, buf_len = [], 0
            else:
                buf.append(line)
                buf_len += len(line)
        if buf:
            paras.append("\n".join(buf).strip())
        cur: list[str] = []
        cur_len = 0
        for p in paras:
            if not p:
                continue
            if cur and cur_len + len(p) > MAX_CHUNK_CHARS:
                chunks.append({"heading": head, "content": "\n\n".join(cur)})
                cur, cur_len = [p], len(p)
            else:
                cur.append(p)
                cur_len += len(p)
        if cur:
            chunks.append({"heading": head, "content": "\n\n".join(cur)})
    return [c for c in chunks if len(c["content"]) >= 10]


def ingest_document(title: str, source: str, tags: str, content: str) -> dict:
    """写入/更新一篇文档：删除旧版本 → 分块 → 向量化 → 双表落库。"""
    title = (title or "").strip() or "未命名文档"
    tags = (tags or "").strip()
    chunks = split_chunks(content)
    if not chunks:
        return {"ok": False, "error": "文档太短，切分后没有有效段落"}

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sm_documents WHERE title=%s", (title,))
            old = cur.fetchone()
            if old:
                cur.execute("DELETE FROM sm_chunks WHERE doc_id=%s", (old["id"],))
                cur.execute("DELETE FROM sm_documents WHERE id=%s", (old["id"],))
            cur.execute(
                "INSERT INTO sm_documents (title, source, tags, n_chunks) VALUES (%s,%s,%s,%s)",
                (title, source, tags, len(chunks)),
            )
            doc_id = cur.lastrowid
            embed_texts = [
                f"{title}\n{c['heading']}\n{c['content']}" if c["heading"] else f"{title}\n{c['content']}"
                for c in chunks
            ]
    finally:
        conn.close()

    vecs = embed.embed_texts(embed_texts)
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            for seq, (c, v) in enumerate(zip(chunks, vecs)):
                cur.execute(
                    "INSERT INTO sm_chunks (doc_id, seq, doc_title, source, tags, heading, content, embedding) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (doc_id, seq, title, source, tags, c["heading"], c["content"], json_vec(v)),
                )
    finally:
        conn.close()
    traces.record("retrieval_trace.jsonl", {
        "kind": "ingest", "doc_id": doc_id, "title": title, "chunks": len(chunks),
        "dim": len(vecs[0]) if vecs else 0,
    })
    return {"ok": True, "doc_id": doc_id, "title": title, "chunks": len(chunks)}


def list_documents() -> list[dict]:
    rows, _ = db.query(
        "SELECT id, title, source, tags, n_chunks, created_at FROM sm_documents ORDER BY id DESC",
        channel="documents.list",
    )
    for r in rows:
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M")
    return rows


def delete_document(doc_id: int) -> int:
    n = db.execute("DELETE FROM sm_chunks WHERE doc_id=%s", (doc_id,), channel="documents.delete")
    db.execute("DELETE FROM sm_documents WHERE id=%s", (doc_id,), channel="documents.delete")
    return n


def json_vec(vec: list[float]) -> str:
    import json
    return json.dumps([round(float(x), 7) for x in vec])
