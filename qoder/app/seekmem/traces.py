"""证据留痕：把每一次 seekdb SQL、检索融合、记忆读写、问答轮次追加写入 JSONL。

评委可用「证据」页签或直接打开 evidence/*.jsonl 核对 seekdb / PowerContext 的实际作用。
"""
import json
import os
import threading
import time
from collections import deque

from . import config

_LOCK = threading.Lock()
_RING: dict[str, deque] = {}
_RING_MAX = 200


def _path(name: str) -> str:
    d = config.EVIDENCE_DIR
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def _ring(name: str) -> deque:
    if name not in _RING:
        _RING[name] = deque(maxlen=_RING_MAX)
    return _RING[name]


def record(name: str, payload: dict) -> dict:
    """追加一条记录到 evidence/<name>.jsonl，并保留内存环形缓冲供 UI 展示。"""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **payload}
    line = json.dumps(entry, ensure_ascii=False)
    with _LOCK:
        with open(_path(name), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _ring(name).append(entry)
    return entry


def tail(name: str, limit: int = 50) -> list:
    with _LOCK:
        items = list(_ring(name))
    return items[-limit:]


def sql_trace(channel: str, sql: str, ms: float, rows: int, note: str = "") -> None:
    """seekdb 查询留痕（脱敏：向量字面量截断）。"""
    shown = sql
    if len(shown) > 300:
        head, tail_part = sql[:220], sql[-60:]
        shown = head + " ...<vector literal elided>... " + tail_part
    record("sql_trace.jsonl", {
        "kind": "seekdb.sql", "channel": channel, "elapsed_ms": round(ms, 1),
        "rows": rows, "sql": shown, "note": note,
    })
