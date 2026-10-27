"""LLM 客户端：OpenAI 兼容 /v1/chat/completions（默认本地 llama.cpp）。

LLM 是增强项而非依赖：服务不可达时 `enabled()` 返回 False，
问答自动降级为「纯检索 + 摘要拼接」模式，检索与记忆主流程不受影响。
"""
import json
import threading
import time

import requests

from . import config, traces

_SESSION = requests.Session()
_state = {"probed_at": 0.0, "ok": False}
_LOCK = threading.Lock()
_PROBE_TTL = 300.0


class LLMError(RuntimeError):
    pass


def enabled(force_check: bool = False) -> bool:
    """探测（带 5 分钟缓存）本地/云端 LLM 是否可用。"""
    if not config.LLM_ENABLED:
        return False
    with _LOCK:
        now = time.time()
        if not force_check and now - _state["probed_at"] < _PROBE_TTL:
            return _state["ok"]
    ok = False
    try:
        # 探测超时给足 15s：本地 llama 生成忙时 /models 也会排队，4s 会误判为未接入
        resp = _SESSION.get(f"{config.LLM_BASE_URL.rstrip('/')}/models", timeout=15)
        ok = resp.status_code == 200
    except requests.RequestException:
        ok = False
    with _LOCK:
        _state["probed_at"] = now = time.time()
        _state["ok"] = ok
    return ok


def chat(messages: list[dict], max_tokens: int | None = None, temperature: float = 0.5, attempts: int = 2) -> str:
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "max_tokens": max_tokens or config.LLM_MAX_TOKENS,
        "temperature": temperature,
        "stream": False,
    }
    t0 = time.time()
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            resp = _SESSION.post(
                f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions", json=payload, timeout=420
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            traces.record("chat_trace.jsonl", {
                "kind": "llm", "model": config.LLM_MODEL, "elapsed_ms": round((time.time() - t0) * 1000),
                "prompt_roles": [m["role"] for m in messages], "output_chars": len(text),
            })
            return text.strip()
        except (requests.RequestException, KeyError, ValueError) as exc:
            last_exc = exc
    raise LLMError(f"LLM 调用失败: {last_exc}") from last_exc


def extract_json_array(text: str) -> list:
    """从模型输出里抠出第一个 JSON 数组（3B 小模型输出不够稳定，做宽容解析）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, depth, end = text.find("["), 0, -1
    if start < 0:
        return []
    for i, ch in enumerate(text[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return []
    try:
        arr = json.loads(text[start:end + 1])
        return arr if isinstance(arr, list) else []
    except ValueError:
        return []
