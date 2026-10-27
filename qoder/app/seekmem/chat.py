"""问答编排：记忆召回 → 资料混合检索 → LLM 综合（可降级）→ 记忆沉淀。"""
import logging

from . import llm, memory, retrieval, traces

_LOG = logging.getLogger("seekmem.chat")

_ANSWER_SYSTEM = (
    "你是 SeekMem 工作台的助手。根据【工作记忆】（关于这位用户的持久记忆）和【资料检索结果】"
    "（从其文档库用 seekdb 向量+全文混合检索出的片段）回答问题。要求：\n"
    "1. 优先使用检索到的资料，引用时在句尾标注来源编号如 [1][2]；\n"
    "2. 资料不足以回答时如实说明，并可根据工作记忆提示相关背景；\n"
    "3. 用简体中文，简洁、直接、面向工作场景。"
)

_EXTRACT_SYSTEM = (
    "你从一段工作问答中提炼值得长期记住的记忆。只输出 JSON 数组（可为空数组），"
    "每项形如 {\"kind\": \"...\", \"text\": \"...\"}。kind 只能是："
    "note（事实/结论）、decision（决定/约定）、task（进行中或待办的事）、preference（用户偏好）。"
    "text 用一句话、第三人称、带主语（用户名字）。最多 2 条，宁缺毋滥；不要编造。示例："
    '[{"kind":"decision","text":"张三决定报销流程改为季度统一提交"}]'
)


def answer(question: str, user: str) -> dict:
    question = question.strip()
    scope = memory.ensure_scope(user)
    scope_id = scope.get("scope_id", "")

    # 1. 记忆召回（PowerContext）
    recalled = memory.search(scope_id, question, limit=4) if scope_id else []

    # 2. 资料混合检索（seekdb）
    ret = retrieval.retrieve(question)
    hits = ret["hits"]

    # 3. 生成回答（LLM 不可达时降级为纯检索摘要）
    llm_used = False
    if llm.enabled() and (hits or recalled):
        try:
            answer_text, llm_used = _compose(question, user, recalled, hits), True
        except llm.LLMError as exc:
            _LOG.warning("LLM 失败，降级纯检索: %s", exc)
            answer_text = _fallback_answer(hits)
    else:
        answer_text = _fallback_answer(hits)

    # 4. 记忆沉淀（LLM 提取 0-2 条写入 PowerContext）
    saved = []
    if llm_used and scope_id:
        saved = _extract_and_remember(scope_id, user, question, answer_text)

    result = {
        "question": question,
        "user": user,
        "scope_id": scope_id,
        "answer": answer_text,
        "llm_used": llm_used,
        "citations": [
            {"n": r["final_rank"], "doc_title": r["doc_title"], "heading": r["heading"],
             "source": r["source"], "chunk_id": r["chunk_id"], "channels": r["channels"],
             "distance": r["distance"]}
            for r in hits
        ],
        "retrieval": {"channels": ret["channels"], "elapsed_ms": ret["elapsed_ms"]},
        "memory_recalled": recalled,
        "memory_saved": saved,
        "scope_created": scope.get("created", False),
    }
    traces.record("chat_trace.jsonl", {
        "kind": "chat.turn", "user": user, "question": question[:200],
        "llm_used": llm_used, "citations": len(hits),
        "memory_recalled": len(recalled), "memory_saved": [s.get("entry_id", "") for s in saved],
    })
    return result


def _compose(question: str, user: str, recalled: list[dict], hits: list[dict]) -> str:
    mem_lines = [f"- ({h['kind']}) {h['text']}" for h in recalled] or ["- （暂无相关记忆）"]
    doc_lines = []
    for r in hits[:5]:
        head = f"{r['doc_title']}" + (f" · {r['heading']}" if r["heading"] else "")
        doc_lines.append(f"[{r['final_rank']}] {head}\n{r['content'][:420]}")
    user_prompt = (
        f"【工作记忆】（PowerContext 中关于 {user} 的记忆）\n" + "\n".join(mem_lines)
        + f"\n\n【资料检索结果】（seekdb 混合检索，共 {len(hits)} 段）\n" + "\n\n".join(doc_lines)
        + f"\n\n【问题】{question}"
    )
    return llm.chat([
        {"role": "system", "content": _ANSWER_SYSTEM},
        {"role": "user", "content": user_prompt},
    ])


def _fallback_answer(hits: list[dict]) -> str:
    if not hits:
        return "资料库里没有检索到相关内容。请先在「资料库」导入文档，或换个问法。"
    lines = ["（当前未接入 LLM，以下为 seekdb 混合检索的直接结果摘要）"]
    for r in hits[:4]:
        head = f"{r['doc_title']}" + (f" · {r['heading']}" if r["heading"] else "")
        lines.append(f"[{r['final_rank']}] {head}：{r['content'][:180]}…")
    return "\n\n".join(lines)


def _extract_and_remember(scope_id: str, user: str, question: str, answer_text: str) -> list[dict]:
    try:
        raw = llm.chat([
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": f"用户：{user}\n问题：{question}\n回答：{answer_text[:800]}"},
        ], max_tokens=200, temperature=0.2)
    except llm.LLMError as exc:
        _LOG.warning("memory extraction failed: %s", exc)
        return []
    items = llm.extract_json_array(raw)[:2]
    saved = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text", "")).strip()
        kind = str(it.get("kind", "note")).strip()
        if len(text) < 4:
            continue
        res = memory.remember(scope_id, kind, text)
        if res.get("saved"):
            saved.append(res)
    return saved
