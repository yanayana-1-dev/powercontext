"""seekdb 连接与查询封装（MySQL 协议）。"""
import time

import pymysql

from . import config, traces


def connect(database: str | None = None) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=config.SEEKDB_HOST,
        port=config.SEEKDB_PORT,
        user=config.SEEKDB_USER,
        password=config.SEEKDB_PASSWORD,
        database=database or config.SEEKDB_DATABASE,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=10,
    )


def query(sql: str, args: tuple | list | None = None, channel: str = "sql") -> tuple[list[dict], float]:
    """执行 SELECT，返回 (rows, elapsed_ms) 并留痕。"""
    t0 = time.time()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            rows = cur.fetchall()
    finally:
        conn.close()
    ms = (time.time() - t0) * 1000
    traces.sql_trace(channel, cur._executed if hasattr(cur, "_executed") else sql, ms, len(rows))
    return rows, ms


def execute(sql: str, args: tuple | list | None = None, channel: str = "sql") -> int:
    """执行 DDL/DML，返回 affected rows 并留痕。"""
    t0 = time.time()
    conn = connect()
    try:
        with conn.cursor() as cur:
            affected = cur.execute(sql, args or ())
    finally:
        conn.close()
    ms = (time.time() - t0) * 1000
    traces.sql_trace(channel, sql if len(sql) < 300 else sql[:300] + "...", ms, affected)
    return affected
