# 雾隐镇编年史 · seekdb 初始化脚本
# 1) 建两张表：mistvale_lore（世界设定，种子数据） / mistvale_events（编年史事件，含少量史前事件 + 玩家动态写入）
# 2) 每条文本现场调用 Embedding 服务（OpenAI 兼容 /v1/embeddings）后写入 VECTOR 列
# 3) 建全文索引（IK 分词）与 HNSW 向量索引
#
# 用法（先配好环境变量或 .env，见 .env.example）：
#   python init_seekdb.py            # 建表 + 种子数据
#   python init_seekdb.py --drop     # 先删表再建（重置世界）
#   python init_seekdb.py --verify   # 只验证：跑一条混合检索 SQL
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pymysql
import requests


def _load_env_file() -> None:
    """把同目录 .env 读进环境变量（已设置的不覆盖）。"""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

SEEKDB_HOST = os.environ.get("SEEKDB_HOST", "127.0.0.1")
SEEKDB_PORT = int(os.environ.get("SEEKDB_PORT", "2881"))
SEEKDB_USER = os.environ.get("SEEKDB_USER", "root")
SEEKDB_PASSWORD = os.environ.get("SEEKDB_PASSWORD", "")
SEEKDB_DATABASE = os.environ.get("SEEKDB_DATABASE", "test")

EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-m3")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "ollama")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))

SEED_DIR = Path(__file__).parent / "seed"

LORE_DDL = """
CREATE TABLE IF NOT EXISTS mistvale_lore (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  category VARCHAR(16) NOT NULL,
  name VARCHAR(128) NOT NULL,
  content TEXT NOT NULL,
  importance INT DEFAULT 1,
  content_vec VECTOR({dim}),
  FULLTEXT INDEX ft_lore_content (content) WITH PARSER IK,
  VECTOR INDEX vec_lore_content (content_vec) WITH (distance=L2, type=hnsw)
)
"""

EVENT_DDL = """
CREATE TABLE IF NOT EXISTS mistvale_events (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  year INT NOT NULL,
  location VARCHAR(64) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  entry_type VARCHAR(16) NOT NULL,
  content TEXT NOT NULL,
  importance INT DEFAULT 2,
  session_id VARCHAR(64) DEFAULT '',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  content_vec VECTOR({dim}),
  FULLTEXT INDEX ft_event_content (content) WITH PARSER IK,
  VECTOR INDEX vec_event_content (content_vec) WITH (distance=L2, type=hnsw)
)
"""

PLAYER_DDL = """
CREATE TABLE IF NOT EXISTS mistvale_players (
  player_slug VARCHAR(64) PRIMARY KEY,
  player_name VARCHAR(128) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

SESSION_DDL = """
CREATE TABLE IF NOT EXISTS mistvale_sessions (
  conversation_id VARCHAR(64) PRIMARY KEY,
  player_slug VARCHAR(64) NOT NULL,
  scope_id VARCHAR(64) NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

CLOCK_DDL = """
CREATE TABLE IF NOT EXISTS mistvale_clock (
  id INT PRIMARY KEY,
  current_year INT NOT NULL DEFAULT 4
)
"""

CLOCK_SEED = "INSERT INTO mistvale_clock (id, current_year) SELECT 1, 4 FROM DUAL WHERE NOT EXISTS (SELECT 1 FROM mistvale_clock WHERE id = 1)"


def embed_batch(client: requests.Session, texts: list[str]) -> list[list[float]]:
    resp = client.post(
        f"{EMBEDDING_BASE_URL.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {EMBEDDING_API_KEY}"} if EMBEDDING_API_KEY else {},
        json={"model": EMBEDDING_MODEL, "input": texts},
        timeout=120,
    )
    resp.raise_for_status()
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    vecs = [d["embedding"] for d in data]
    for v in vecs:
        assert len(v) == EMBEDDING_DIM, f"dim mismatch: {len(v)} != {EMBEDDING_DIM}"
    return vecs


def vec_lit(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def load_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip() and not line.startswith("#")]


def sql_str(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return "'" + text + "'"


def insert_rows(conn, table: str, rows: list[dict], cols: tuple[str, ...]) -> None:
    """cols 最后一个元素必须是 content_vec（由这里现场 embedding 生成）。"""
    client = requests.Session()
    t0 = time.time()
    data_cols = cols[:-1]
    with conn.cursor() as cur:
        for i in range(0, len(rows), 8):
            chunk = rows[i : i + 8]
            vecs = embed_batch(client, [r["content"] for r in chunk])
            for r, v in zip(chunk, vecs):
                values = ", ".join(
                    str(r[c]) if isinstance(r.get(c), int) else sql_str(r.get(c, ""))
                    for c in data_cols
                )
                sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({values}, '{vec_lit(v)}')"
                cur.execute(sql)
            print(f"  {table}: {i + len(chunk)}/{len(rows)}")
    print(f"  {table} done in {time.time() - t0:.1f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true", help="删除旧表重建（重置世界）")
    ap.add_argument("--verify", action="store_true", help="只跑一条混合检索验证")
    ap.add_argument("--skip-lore", action="store_true", help="跳过设定表灌数")
    ap.add_argument("--skip-events", action="store_true", help="跳过史前事件灌数")
    args = ap.parse_args()

    conn = pymysql.connect(host=SEEKDB_HOST, port=SEEKDB_PORT, user=SEEKDB_USER,
                           password=SEEKDB_PASSWORD, database=SEEKDB_DATABASE,
                           charset="utf8mb4", autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION()")
        print("seekdb:", cur.fetchone())

    if args.verify:
        with conn.cursor() as cur:
            qv = vec_lit(embed_batch(requests.Session(), ["喷泉"])[0])
            sql = (
                "SELECT id, year, location, content, "
                f"l2_distance(content_vec, '{qv}') AS dist FROM mistvale_events "
                "WHERE MATCH(content) AGAINST('喷泉') "
                f"ORDER BY l2_distance(content_vec, '{qv}') APPROXIMATE LIMIT 3"
            )
            cur.execute(sql)
            for r in cur.fetchall():
                print(r)
        conn.close()
        return

    with conn.cursor() as cur:
        if args.drop:
            cur.execute("DROP TABLE IF EXISTS mistvale_events")
            cur.execute("DROP TABLE IF EXISTS mistvale_lore")
            print("dropped old tables")
        cur.execute(LORE_DDL.format(dim=EMBEDDING_DIM))
        cur.execute(EVENT_DDL.format(dim=EMBEDDING_DIM))
        cur.execute(PLAYER_DDL)
        cur.execute(SESSION_DDL)
        cur.execute(CLOCK_DDL)
        cur.execute(CLOCK_SEED)
        print("tables ready (dim =", EMBEDDING_DIM, ")")

    if not args.skip_lore:
        lore = load_jsonl(SEED_DIR / "lore.jsonl")
        print(f"seeding {len(lore)} lore entries ...")
        insert_rows(conn, "mistvale_lore", lore, ("category", "name", "content", "importance", "content_vec"))

    if not args.skip_events:
        events = load_jsonl(SEED_DIR / "events_prehistory.jsonl")
        print(f"seeding {len(events)} prehistory events ...")
        insert_rows(conn, "mistvale_events", events,
                    ("year", "location", "actor", "entry_type", "content", "importance", "content_vec"))

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM mistvale_lore")
        print("lore rows:", cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM mistvale_events")
        print("event rows:", cur.fetchone()[0])
    conn.close()
    print("init done")


if __name__ == "__main__":
    sys.exit(main())
