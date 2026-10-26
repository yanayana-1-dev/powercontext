# 雾隐镇编年史 · Chronicle Bridge
# Dify HTTP 节点与 seekdb 之间的桥接服务：
#   POST /v1/chronicle/recall  —— 单条混合检索 SQL（向量 + 全文 + 结构化）召回世界设定与历史事件
#   POST /v1/chronicle/archive —— 新事件 embed 后写入 seekdb，世界因此永久改变
#   GET  /healthz
# seekdb 只说 MySQL 协议，Dify 的 HTTP Request 节点说不了 MySQL，所以需要这个小桥。
# 配置全部走环境变量，见 .env.example。
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import logging
from contextlib import contextmanager

import pymysql
import pymysql.cursors
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

LOG = logging.getLogger("chronicle")
logging.basicConfig(level=os.environ.get("BRIDGE_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

SEEKDB_HOST = os.environ.get("SEEKDB_HOST", "127.0.0.1")
SEEKDB_PORT = int(os.environ.get("SEEKDB_PORT", "2881"))
SEEKDB_USER = os.environ.get("SEEKDB_USER", "root")
SEEKDB_PASSWORD = os.environ.get("SEEKDB_PASSWORD", "")
SEEKDB_DATABASE = os.environ.get("SEEKDB_DATABASE", "test")

EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11435/v1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-m3")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "ollama")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))

LORE_TABLE = os.environ.get("LORE_TABLE", "mistvale_lore")
EVENT_TABLE = os.environ.get("EVENT_TABLE", "mistvale_events")
PLAYER_TABLE = os.environ.get("PLAYER_TABLE", "mistvale_players")
SESSION_TABLE = os.environ.get("SESSION_TABLE", "mistvale_sessions")
CLOCK_TABLE = os.environ.get("CLOCK_TABLE", "mistvale_clock")

POWERCONTEXT_BASE_URL = os.environ.get("POWERCONTEXT_BASE_URL", "http://127.0.0.1:8010")

SQL_TRACE_PATH = os.environ.get("SQL_TRACE_PATH", "")  # 设了就把每条检索 SQL 落盘（评审取证用）

app = FastAPI(title="Mistvale Chronicle Bridge", version="1.0.0")


@contextmanager
def db():
    conn = pymysql.connect(
        host=SEEKDB_HOST, port=SEEKDB_PORT, user=SEEKDB_USER, password=SEEKDB_PASSWORD,
        database=SEEKDB_DATABASE, charset="utf8mb4", autocommit=True,
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=5, read_timeout=30,
    )
    try:
        yield conn
    finally:
        conn.close()


def trace_sql(label: str, sql: str, elapsed_ms: float, rows: int) -> None:
    if not SQL_TRACE_PATH:
        return
    line = json.dumps({"ts": time.strftime("%H:%M:%S"), "op": label, "elapsed_ms": round(elapsed_ms, 1),
                       "rows": rows, "sql": re.sub(r"\s+", " ", sql)[:800]}, ensure_ascii=False)
    with open(SQL_TRACE_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def embed(text: str) -> list[float]:
    resp = requests.post(
        f"{EMBEDDING_BASE_URL.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {EMBEDDING_API_KEY}"} if EMBEDDING_API_KEY else {},
        json={"model": EMBEDDING_MODEL, "input": text[:2000]},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    vec = data[0]["embedding"]
    if len(vec) != EMBEDDING_DIM:
        raise HTTPException(500, f"embedding dim mismatch: got {len(vec)}, expect {EMBEDDING_DIM}")
    return vec


def vec_lit(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


class RecallRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500, description="玩家本轮行动原文")
    k: int = Field(default=4, ge=1, le=8)
    keyword: str = Field(default="", max_length=120, description="可选：全文检索关键词（空则自动截取 query）")


class ArchiveRequest(BaseModel):
    year: int = Field(ge=0, le=9999)
    location: str = Field(min_length=1, max_length=64)
    actor: str = Field(min_length=1, max_length=128)
    entry_type: str = Field(default="deed", max_length=16)
    content: str = Field(min_length=2, max_length=2000, description="镇志条目正文")
    importance: int = Field(default=2, ge=1, le=5)
    session_id: str = Field(default="", max_length=64)
    year_advance: int = Field(default=0, ge=0, le=10)


def _keywords(text: str, limit: int = 60) -> str:
    """全文检索的查询串：seekdb 的 IK 分词器自己切中文词，这里只做长度与引号清洗。"""
    cleaned = text.replace("'", " ").replace('"', " ").replace("\\", " ")
    return cleaned.strip()[:limit]


@app.get("/healthz")
def healthz():
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return {"ok": True, "seekdb": f"{SEEKDB_HOST}:{SEEKDB_PORT}", "embedding": EMBEDDING_MODEL}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"seekdb unreachable: {exc}") from exc


@app.post("/v1/chronicle/recall")
def recall(req: RecallRequest):
    """三条单语句混合检索：
    A. 世界设定：向量语义为主 + 结构化过滤 importance
    B. 历史事件：全文 MATCH 作过滤 + 向量排序 + 结构化过滤 —— seekdb 一条 SQL 同时吃三种检索
    C. 近事：纯结构化，维持剧情连续性
    """
    kw = _keywords(req.keyword or req.query)
    qvec = vec_lit(embed(req.query))
    out = {"lore": [], "events": [], "recent": []}

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT current_year FROM {CLOCK_TABLE} WHERE id = 1")
            clock_row = cur.fetchone()
            if clock_row and clock_row.get("current_year"):
                out["latest_year"] = int(clock_row["current_year"])
            else:
                cur.execute(f"SELECT MAX(year) AS y FROM {EVENT_TABLE}")
                out["latest_year"] = int((cur.fetchone() or {}).get("y") or 4)

            sql_a = (
                f"SELECT id, category, name, content, importance, "
                f"l2_distance(content_vec, '{qvec}') AS dist "
                f"FROM {LORE_TABLE} WHERE importance >= 2 "
                f"ORDER BY l2_distance(content_vec, '{qvec}') APPROXIMATE LIMIT {req.k}"
            )
            t0 = time.time()
            cur.execute(sql_a)
            rows = cur.fetchall()
            trace_sql("recall.lore", sql_a, (time.time() - t0) * 1000, len(rows))
            out["lore"] = [
                {"src": "lore", "id": r["id"], "category": r["category"], "name": r["name"],
                 "content": r["content"], "importance": r["importance"], "dist": float(r["dist"])}
                for r in rows
            ]

            sql_b = (
                f"SELECT id, year, location, actor, entry_type, content, importance, "
                f"l2_distance(content_vec, '{qvec}') AS dist "
                f"FROM {EVENT_TABLE} "
                f"WHERE MATCH(content) AGAINST('{kw}') AND importance >= 1 "
                f"ORDER BY l2_distance(content_vec, '{qvec}') APPROXIMATE LIMIT {req.k}"
            )
            t0 = time.time()
            try:
                cur.execute(sql_b)
                rows = cur.fetchall()
            except pymysql.err.MySQLError as exc:
                LOG.warning("fulltext branch failed (%s), fallback to important events", exc)
                sql_b = (
                    f"SELECT id, year, location, actor, entry_type, content, importance, "
                    f"l2_distance(content_vec, '{qvec}') AS dist "
                    f"FROM {EVENT_TABLE} WHERE importance >= 4 "
                    f"ORDER BY l2_distance(content_vec, '{qvec}') APPROXIMATE LIMIT {req.k}"
                )
                cur.execute(sql_b)
                rows = cur.fetchall()
            if not rows:
                LOG.info("fulltext branch empty for kw=%r, fallback to important events", kw)
                sql_b = (
                    f"SELECT id, year, location, actor, entry_type, content, importance, "
                    f"l2_distance(content_vec, '{qvec}') AS dist "
                    f"FROM {EVENT_TABLE} WHERE importance >= 4 "
                    f"ORDER BY l2_distance(content_vec, '{qvec}') APPROXIMATE LIMIT {req.k}"
                )
                cur.execute(sql_b)
                rows = cur.fetchall()
            trace_sql("recall.events_hybrid", sql_b, (time.time() - t0) * 1000, len(rows))
            out["events"] = [
                {"src": "event", "id": r["id"], "year": r["year"], "location": r["location"],
                 "actor": r["actor"], "entry_type": r["entry_type"], "content": r["content"],
                 "importance": r["importance"], "dist": float(r["dist"])}
                for r in rows
            ]

            sql_c = (
                f"SELECT id, year, location, actor, entry_type, content, importance "
                f"FROM {EVENT_TABLE} ORDER BY year DESC, id DESC LIMIT 3"
            )
            t0 = time.time()
            cur.execute(sql_c)
            rows = cur.fetchall()
            trace_sql("recall.recent", sql_c, (time.time() - t0) * 1000, len(rows))
            seen_prefixes = set()
            recent_items = []
            for r in rows:
                prefix = re.sub(r"\s", "", str(r["content"]))[:18]
                if prefix in seen_prefixes:
                    continue
                seen_prefixes.add(prefix)
                recent_items.append(
                    {"src": "recent", "id": r["id"], "year": r["year"], "location": r["location"],
                     "actor": r["actor"], "entry_type": r["entry_type"], "content": r["content"],
                     "importance": r["importance"]}
                )
            out["recent"] = recent_items

    for group in ("lore", "events"):
        seen, uniq = set(), []
        for item in out[group]:
            key = (item["src"], item["id"])
            if key not in seen:
                seen.add(key)
                uniq.append(item)
        out[group] = uniq
    return out


@app.post("/v1/chronicle/archive")
def archive(req: ArchiveRequest):
    """把镇志官写好的新条目 embed 后写入 seekdb —— 从此它就是小镇历史的一部分。
    同时推进编年时钟：clock = max(clock, 事件年份 + year_advance)。
    importance<1 表示镇志官没能产出有效条目（解析失败的占位符），拒收以免污染正史。"""
    if req.importance < 1:
        raise HTTPException(422, "chronicle rejected: parser fallback (importance=0)")
    qvec = vec_lit(embed(req.content))
    content = req.content.replace("\\", "\\\\").replace("'", "''")
    sql = (
        f"INSERT INTO {EVENT_TABLE} (year, location, actor, entry_type, content, importance, session_id, content_vec) "
        f"VALUES ({int(req.year)}, '{_escape(req.location)}', '{_escape(req.actor)}', "
        f"'{_escape(req.entry_type)}', '{content}', {int(req.importance)}, '{_escape(req.session_id)}', '{qvec}')"
    )
    with db() as conn:
        with conn.cursor() as cur:
            t0 = time.time()
            cur.execute(sql)
            cur.execute("SELECT MAX(id) AS id FROM " + EVENT_TABLE)
            row = cur.fetchone()
            cur.execute(
                f"UPDATE {CLOCK_TABLE} SET current_year = GREATEST(current_year, %s) WHERE id = 1",
                (int(req.year) + int(req.year_advance),),
            )
    trace_sql("archive.insert", sql, (time.time() - t0) * 1000, 1)
    return {"archived": True, "id": row["id"] if row else None,
            "entry": {"year": req.year, "location": req.location, "actor": req.actor,
                      "entry_type": req.entry_type, "importance": req.importance}}


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "''").strip()[:200]


class PlayerRequest(BaseModel):
    player_slug: str = Field(min_length=1, max_length=64)
    player_name: str = Field(min_length=1, max_length=64)


@app.post("/v1/chronicle/player")
def ensure_player(req: PlayerRequest):
    """（保留给测试用）按玩家名号返回 PowerContext scope_id，没有则登记。"""
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT scope_id FROM {PLAYER_TABLE} WHERE player_slug = %s", (req.player_slug,))
            row = cur.fetchone()
    if row:
        return {"scope_id": row["scope_id"], "created": False, "player_name": req.player_name}

    resp = requests.post(
        f"{POWERCONTEXT_BASE_URL.rstrip('/')}/v1/scopes",
        json={"title": f"雾隐镇·旅行者 {req.player_name}",
              "summary": f"旅行者 {req.player_name} 在雾隐镇的角色记忆：身份、恩怨、未了结的事。",
              "idempotency_key": f"mistvale-player-{req.player_slug}"},
        timeout=30,
    )
    resp.raise_for_status()
    scope_id = resp.json()["scope_id"]

    seed_memory = (
        f"{req.player_name}初到雾隐镇：一位来历不明的旅行者，声望平平，"
        "口袋里有三枚铜币和一把不知道开什么锁的钥匙。市政厅的档案里还没有这个人的任何记录。"
    )
    resp2 = requests.post(
        f"{POWERCONTEXT_BASE_URL.rstrip('/')}/v1/memory/remember",
        json={"scope_id": scope_id, "kind": "identity", "text": seed_memory},
        timeout=30,
    )
    resp2.raise_for_status()

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {PLAYER_TABLE} (player_slug, player_name, scope_id) VALUES (%s, %s, %s)",
                (req.player_slug, req.player_name, scope_id),
            )
    return {"scope_id": scope_id, "created": True, "player_name": req.player_name}


_NAME_RE = re.compile(r"(?:我叫|我叫作|我的名字[是叫]?|我自称|我号)([^，。,.!！?？' ]{1,16})")


class IdentifyRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    conversation_id: str = Field(default="", max_length=64)


def _get_or_create_player(conn, name: str) -> tuple[str, str, bool]:
    """返回 (player_slug, scope_id, created)。名号为空返回 ("", "", False)。"""
    if not name:
        return "", "", False
    slug = hashlib.md5(("mistvale|" + name).encode("utf-8")).hexdigest()[:12]
    with conn.cursor() as cur:
        cur.execute(f"SELECT scope_id FROM {PLAYER_TABLE} WHERE player_slug = %s", (slug,))
        row = cur.fetchone()
    if row:
        return slug, row["scope_id"], False

    resp = requests.post(
        f"{POWERCONTEXT_BASE_URL.rstrip('/')}/v1/scopes",
        json={"title": f"雾隐镇·旅行者 {name}",
              "summary": f"旅行者 {name} 在雾隐镇的角色记忆：身份、恩怨、未了结的事。",
              "idempotency_key": f"mistvale-player-{slug}"},
        timeout=30,
    )
    resp.raise_for_status()
    scope_id = resp.json()["scope_id"]
    seed_memory = (
        f"{name}初到雾隐镇：一位来历不明的旅行者，声望平平，"
        "口袋里有三枚铜币和一把不知道开什么锁的钥匙。市政厅的档案里还没有这个人的任何记录。"
    )
    resp2 = requests.post(
        f"{POWERCONTEXT_BASE_URL.rstrip('/')}/v1/memory/remember",
        json={"scope_id": scope_id, "kind": "identity", "text": seed_memory},
        timeout=30,
    )
    resp2.raise_for_status()
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {PLAYER_TABLE} (player_slug, player_name, scope_id) VALUES (%s, %s, %s)",
            (slug, name, scope_id),
        )
    return slug, scope_id, True


@app.post("/v1/chronicle/identify")
def identify(req: IdentifyRequest):
    """身份识别：从行动文本提取名号；有名号则查/建 PowerContext scope 并绑定会话；
    无名号则按 conversation_id 查会话绑定（老玩家继续用同一身份）。
    全部落 seekdb（mistvale_players / mistvale_sessions）。"""
    m = _NAME_RE.search(req.text)
    name = m.group(1).strip() if m else ""
    with db() as conn:
        with conn.cursor() as cur:
            if name:
                slug, scope_id, created = _get_or_create_player(conn, name)
                if slug:
                    cur.execute(
                        f"INSERT INTO {SESSION_TABLE} (conversation_id, player_slug, scope_id) "
                        f"VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE player_slug=VALUES(player_slug), "
                        f"scope_id=VALUES(scope_id)",
                        (req.conversation_id, slug, scope_id),
                    )
                return {"scope_id": scope_id, "player_name": name,
                        "first_meeting": created, "from_text": True}
            if req.conversation_id:
                cur.execute(
                    f"SELECT p.player_name, s.scope_id FROM {SESSION_TABLE} s "
                    f"JOIN {PLAYER_TABLE} p ON p.player_slug = s.player_slug "
                    f"WHERE s.conversation_id = %s",
                    (req.conversation_id,),
                )
                row = cur.fetchone()
                if row:
                    return {"scope_id": row["scope_id"], "player_name": row["player_name"],
                            "first_meeting": False, "from_text": False}
    return {"scope_id": "", "player_name": "", "first_meeting": False, "from_text": False}


class RememberRequest(BaseModel):
    scope_id: str = Field(default="", max_length=64)
    updates: list[dict] = Field(default_factory=list, max_length=4)


@app.post("/v1/chronicle/remember")
def remember(req: RememberRequest):
    """把镇志官决定记住的 0-2 条内容写入 PowerContext 记忆（过滤空文本）。
    单独存在这里是为了让 Dify 只发一个 HTTP 请求，且由服务端过滤空更新。
    无名旅人（scope_id 为空）或 PowerContext 不可用时静默降级，不影响叙事主流程。"""
    if not req.scope_id:
        return {"saved": 0, "skipped": "no-scope"}
    saved = 0
    for upd in req.updates[:2]:
        text = str(upd.get("text") or "").strip()
        if not text:
            continue
        try:
            resp = requests.post(
                f"{POWERCONTEXT_BASE_URL.rstrip('/')}/v1/memory/remember",
                json={"scope_id": req.scope_id, "kind": str(upd.get("kind") or "deed")[:64], "text": text[:500]},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            LOG.warning("memory remember failed: %s", exc)
            return {"saved": saved, "skipped": "powercontext-unavailable"}
        saved += 1
    return {"saved": saved}


class MemorySearchRequest(BaseModel):
    scope_id: str = Field(default="", max_length=64)
    query: str = Field(min_length=1, max_length=1000)


@app.post("/v1/chronicle/memory_search")
def memory_search(req: MemorySearchRequest):
    """转发 PowerContext 记忆检索；无名旅人或检索失败时返回空结果，保证主流程可用。"""
    if not req.scope_id:
        return {"hits": [], "skipped": "no-scope"}
    try:
        resp = requests.post(
            f"{POWERCONTEXT_BASE_URL.rstrip('/')}/v1/memory/search",
            json={"scope_id": req.scope_id, "query": req.query[:2000], "limit": 6, "mode": "auto"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        LOG.warning("memory search failed: %s", exc)
        return {"hits": [], "skipped": "powercontext-unavailable"}
    hits = [
        {"text": h.get("text", ""), "kind": (h.get("citation") or {}).get("kind", "") or "memory"}
        for h in (data.get("hits") or [])[:6]
    ]
    return {"hits": hits}


@app.get("/v1/chronicle/stats")
def stats():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM {EVENT_TABLE}")
            events = cur.fetchone()["n"]
            cur.execute(f"SELECT COUNT(*) AS n FROM {LORE_TABLE}")
            lore = cur.fetchone()["n"]
            cur.execute(f"SELECT current_year AS y FROM {CLOCK_TABLE} WHERE id = 1")
            row = cur.fetchone()
            year = int(row["y"]) if row and row.get("y") else 0
            cur.execute(f"SELECT COUNT(*) AS n FROM {PLAYER_TABLE}")
            players = cur.fetchone()["n"]
    return {"events": events, "lore": lore, "players": players, "latest_year": year}
