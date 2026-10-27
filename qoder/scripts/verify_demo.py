"""验证样例一键复跑：对运行中的 SeekMem 服务执行四步验证，输出核对结果与证据。

用法：先启动服务（uvicorn main:app --port 9100），再 python verify_demo.py [APP_URL]
默认 APP_URL=http://127.0.0.1:9100
"""
import json
import sys
import time
from pathlib import Path

import requests

APP = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9100"
OUT = Path(__file__).resolve().parent.parent / "evidence"
OUT.mkdir(exist_ok=True)
report = []


def step(title, expect, actual, passed):
    report.append({"step": title, "expect": expect, "actual": actual, "pass": bool(passed)})
    print(f"[{'PASS' if passed else 'FAIL'}] {title}")
    print(f"       expect: {expect}")
    print(f"       actual: {actual}")


def main():
    user = "验证员小赛"

    # ---- 步骤 0：健康检查（四个组件）----
    h = requests.get(f"{APP}/api/health", timeout=120).json()
    step("健康检查：seekdb/embedding/powercontext 就绪",
         "seekdb=ok, embedding=ok, powercontext=ok（llm 允许降级）",
         json.dumps(h["components"], ensure_ascii=False),
         h["components"]["seekdb"] == "ok" and h["components"]["embedding"] == "ok"
         and h["components"]["powercontext"] == "ok")

    # ---- 样例 1：混合检索命中资料 ----
    r1 = requests.post(f"{APP}/api/chat", json={"question": "报销流程是什么？逾期提交会怎样？", "user": user}, timeout=600).json()
    docs = {c["doc_title"] for c in r1["citations"]}
    step("样例1：提问报销流程 → 命中《差旅与报销制度》",
         "citations 含 '差旅与报销制度（2026年10月修订版）'",
         f"命中文档: {sorted(docs)}",
         any("报销制度" in d for d in docs))
    step("样例1：seekdb 单语句混合检索（hybrid 通道）实际生效",
         "retrieval.channels.hybrid 非空（WHERE MATCH AGAINST + ORDER BY l2_distance APPROXIMATE）",
         f"hybrid 命中 {len(r1['retrieval']['channels']['hybrid'])} 条, vector 命中 {len(r1['retrieval']['channels']['vector'])} 条",
         len(r1["retrieval"]["channels"]["hybrid"]) > 0)

    # ---- 样例 2：记忆写入 PowerContext ----
    r2 = requests.post(f"{APP}/api/chat", json={
        "question": "记住：我们组的检索服务叫星桥，下周三要做压测第二轮，目标 P99 300ms 以内。",
        "user": user}, timeout=600).json()
    saved = r2.get("memory_saved") or []
    step("样例2：告知事项 → 记忆写入 PowerContext（memory_saved 非空）",
         "memory_saved ≥ 1 条，含 task/note 类记忆",
         json.dumps([{k: m[k] for k in ('kind', 'entry_id')} for m in saved], ensure_ascii=False),
         len(saved) >= 1)

    # ---- 样例 3：跨会话记忆复用（新会话再问，应想起星桥压测）----
    time.sleep(1)
    r3 = requests.post(f"{APP}/api/chat", json={"question": "我下周的工作安排里压测是哪天？目标多少？", "user": user}, timeout=600).json()
    recalled_text = json.dumps(r3.get("memory_recalled") or [], ensure_ascii=False)
    answer_has = ("星桥" in r3["answer"]) or ("300" in r3["answer"]) or ("周三" in r3["answer"])
    step("样例3：新会话提问 → PowerContext 记忆被召回并用于回答",
         "memory_recalled 含'星桥/压测'相关记忆，或回答中体现",
         f"recalled={recalled_text[:200]} answer片段={r3['answer'][:120]}",
         ("压测" in recalled_text or "星桥" in recalled_text) and answer_has)

    # ---- 样例 4：记忆面板可核对（entries list）----
    m = requests.get(f"{APP}/api/memory", params={"user": user}, timeout=60).json()
    step("样例4：记忆面板列出该工位全部活跃记忆（scope 隔离）",
         f"entries ≥ {len(saved)} 条，scope_id 非空",
         f"scope_id={m['scope_id']} entries={len(m['entries'])}条",
         bool(m["scope_id"]) and len(m["entries"]) >= len(saved))

    # ---- 证据文件核对 ----
    ev = OUT / "sql_trace.jsonl"
    sql_lines = ev.read_text(encoding="utf-8").strip().splitlines() if ev.exists() else []
    step("证据留痕：sql_trace.jsonl 有 seekdb 查询记录（含混合检索 SQL）",
         "≥ 3 行且含 APPROXIMATE",
         f"{len(sql_lines)} 行",
         len(sql_lines) >= 3 and any("APPROXIMATE" in ln for ln in sql_lines))

    (OUT / "demo_result.json").write_text(
        json.dumps({"app": APP, "time": time.strftime("%F %T"), "results": report},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    n_pass = sum(1 for r in report if r["pass"])
    print(f"\n==== {n_pass}/{len(report)} 项通过，明细已写入 evidence/demo_result.json ====")
    sys.exit(0 if n_pass == len(report) else 1)


if __name__ == "__main__":
    main()
