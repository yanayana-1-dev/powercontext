"""PowerContext 客户端：工位（scope）管理 + 记忆读写。

每名用户一个独立 scope（记忆互相隔离）；所有读写均写入 memory_trace.jsonl 供核对。
PowerContext 不可用时静默降级（返回空结果），不阻断检索主流程。
"""
import hashlib
import logging

import requests

from . import config, db, traces

_LOG = logging.getLogger("seekmem.memory")
_SESSION = requests.Session()
_KINDS = ("note", "decision", "task", "preference")


def user_key(name: str) -> str:
    return hashlib.md5(("seekmem|" + name.strip()).encode("utf-8")).hexdigest()[:16]


def ensure_scope(name: str) -> dict:
    """返回 {scope_id, created}；优先查本地绑定表，否则用 idempotency_key 找回/创建。"""
    key = user_key(name)
    rows, _ = db.query("SELECT scope_id FROM sm_users WHERE user_key=%s", (key,), channel="memory.binding")
    if rows:
        return {"scope_id": rows[0]["scope_id"], "created": False}

    created = False
    try:
        resp = _SESSION.post(
            f"{config.POWERCONTEXT_BASE_URL.rstrip('/')}/v1/scopes",
            json={
                "title": f"SeekMem · 工位 {name}",
                "summary": f"工位 {name} 的工作记忆：进行中的事项、已确认的结论、个人偏好。",
                "idempotency_key": f"seekmem-user-{key}",
            },
            timeout=30,
        )
        resp.raise_for_status()
        scope_id = resp.json()["scope_id"]
        created = True
    except requests.RequestException as exc:
        _LOG.warning("powercontext scope create failed: %s", exc)
        return {"scope_id": "", "created": False, "skipped": "powercontext-unavailable"}

    db.execute(
        "INSERT INTO sm_users (user_key, display_name, scope_id) VALUES (%s,%s,%s)",
        (key, name, scope_id), channel="memory.binding",
    )
    traces.record("memory_trace.jsonl", {
        "kind": "scope.create", "user": name, "scope_id": scope_id,
        "service": config.POWERCONTEXT_BASE_URL,
    })
    return {"scope_id": scope_id, "created": created}


def remember(scope_id: str, kind: str, text: str) -> dict:
    if not scope_id or not text.strip():
        return {"saved": False, "skipped": "no-scope-or-text"}
    kind = kind if kind in _KINDS else "note"
    try:
        resp = _SESSION.post(
            f"{config.POWERCONTEXT_BASE_URL.rstrip('/')}/v1/memory/remember",
            json={"scope_id": scope_id, "kind": kind, "text": text.strip()[:2000]},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        _LOG.warning("powercontext remember failed: %s", exc)
        return {"saved": False, "skipped": "powercontext-unavailable"}
    entry_id = ((data.get("entry") or {}).get("citation") or {}).get("entry_id", "")
    traces.record("memory_trace.jsonl", {
        "kind": "memory.remember", "scope_id": scope_id, "entry_id": entry_id,
        "category": kind, "text": text.strip()[:200],
    })
    return {"saved": True, "entry_id": entry_id, "kind": kind}


def search(scope_id: str, query: str, limit: int = 5) -> list[dict]:
    if not scope_id:
        return []
    try:
        resp = _SESSION.post(
            f"{config.POWERCONTEXT_BASE_URL.rstrip('/')}/v1/memory/search",
            json={"scope_id": scope_id, "query": query[:2000], "limit": limit, "mode": "auto"},
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        _LOG.warning("powercontext search failed: %s", exc)
        return []
    hits = []
    for h in (data.get("hits") or [])[:limit]:
        cite = h.get("citation") or {}
        hits.append({
            "text": h.get("text", ""),
            "kind": cite.get("kind", "") or "memory",
            "score": h.get("score"),
            "matched_by": h.get("matched_by", []),
            "entry_id": cite.get("entry_id", ""),
        })
    traces.record("memory_trace.jsonl", {
        "kind": "memory.search", "scope_id": scope_id, "query": query[:200],
        "hits": [{"kind": h["kind"], "text": h["text"][:120]} for h in hits],
    })
    return hits


def list_entries(scope_id: str) -> list[dict]:
    if not scope_id:
        return []
    try:
        resp = _SESSION.post(
            f"{config.POWERCONTEXT_BASE_URL.rstrip('/')}/v1/memory/entries/list",
            json={"scope_id": scope_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        _LOG.warning("powercontext list failed: %s", exc)
        return []
    entries = []
    for e in (data.get("entries") or []):
        if (e.get("state") or "active") != "active":
            continue
        entries.append({
            "kind": e.get("kind", "note"),
            "text": e.get("text", ""),
            "entry_id": (e.get("citation") or {}).get("entry_id", ""),
        })
    return entries
