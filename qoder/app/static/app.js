/* SeekMem 工作台前端逻辑（无构建依赖，原生 JS） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { user: localStorage.getItem("seekmem.user") || "guest", lastChat: null };

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2600);
}

async function api(path, opts) {
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    throw new Error("网络请求失败：" + e.message);
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = typeof data.detail === "string" ? data.detail : data.detail ? JSON.stringify(data.detail) : "";
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return data;
}

/* ---------- 页签 ---------- */
function switchTab(name) {
  document.querySelectorAll("nav.tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "docs") loadDocs();
  if (name === "memory") loadMemory();
  if (name === "evidence") loadEvidence();
}

/* ---------- 健康状态 ---------- */
async function loadHealth() {
  try {
    const h = await api("/api/health");
    const parts = Object.entries(h.components || {}).map(([k, v]) => {
      const text = String(v);
      const good = text === "ok" || text.startsWith("degraded");
      return `${escapeHtml(k)}: <span class="${good ? "pill-ok" : "pill-warn"}">${escapeHtml(text)}</span>`;
    });
    $("#health-pill").innerHTML = parts.join(" · ") || "状态未知";
  } catch (e) {
    $("#health-pill").textContent = "后端未连接";
  }
}

/* ---------- 记忆横幅（欢迎回来） ---------- */
async function loadWelcome() {
  $("#welcome").style.display = "none";
  if (!state.user) return;
  try {
    const mem = await api(`/api/memory?user=${encodeURIComponent(state.user)}`);
    const items = (mem.welcome || []).filter((h) => h.text);
    if (!mem.scope_id || items.length === 0) {
      $("#welcome").style.display = "none";
      return;
    }
    $("#welcome").style.display = "";
    $("#welcome-list").innerHTML =
      `<div class="m-item muted">欢迎回来，${escapeHtml(state.user)} —— 从你的工作记忆里想起来了：</div>` +
      items.map((h) => `<div class="m-item">・${escapeHtml(h.text)}</div>`).join("");
  } catch (e) { /* 静默 */ }
}

/* ---------- 问答 ---------- */
async function sendChat() {
  const btn = $("#chat-send");
  if (btn.disabled) return; // 防重入：生成中忽略重复发送
  const q = $("#chat-input").value.trim();
  if (!q) return;
  saveUser();
  btn.disabled = true; btn.textContent = "思考中…";
  $("#answer-box").innerHTML = `<div class="muted">正在检索资料与回忆…</div>`;
  try {
    const r = await api("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, user: state.user }),
    });
    state.lastChat = r;
    renderChat(r);
  } catch (e) {
    $("#answer-box").innerHTML = `<div class="pill-warn">出错：${escapeHtml(String(e.message || e))}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "提问";
  }
}

function renderChat(r) {
  let html = `<div class="answer">${renderAnswer(r.answer)}</div>`;
  if (Array.isArray(r.memory_saved) && r.memory_saved.length) {
    html += `<div class="muted" style="margin-top:10px">🧠 已沉淀到工作记忆（PowerContext）：` +
      r.memory_saved.map((m) => `<span class="badge mem">${escapeHtml(m.kind)}</span>`).join(" ") + `</div>`;
  }
  if (!r.llm_used) html += `<div class="muted" style="margin-top:8px">LLM 未接入，当前为纯检索模式（seekdb 检索正常）。</div>`;
  $("#answer-box").innerHTML = html;

  const elapsed = r.retrieval && r.retrieval.elapsed_ms;
  const timing = Number.isFinite(elapsed)
    ? `<div class="muted" style="margin-bottom:6px">混合检索完成，耗时 ${elapsed} ms</div>`
    : "";
  const sources = (Array.isArray(r.citations) ? r.citations : []).map((c) => `
    <div class="source-item">
      <div class="head"><b>[${c.n}] ${escapeHtml(c.doc_title)}</b>
        ${c.heading ? `<span class="badge">${escapeHtml(c.heading)}</span>` : ""}
        ${(c.channels || []).map((ch) => `<span class="badge ${ch === "hybrid" ? "hyb" : ch === "vector" ? "vec" : "rec"}">${ch}</span>`).join("")}
        ${c.distance != null ? `<span class="muted">l2=${c.distance}</span>` : ""}
      </div>
      <div class="body">${escapeHtml(c.preview || "")}…</div>
    </div>`).join("") || `<div class="empty">没有检索到相关资料</div>`;
  $("#sources-box").innerHTML = timing + sources;

  if (Array.isArray(r.memory_recalled) && r.memory_recalled.length) {
    $("#recalled-box").style.display = "";
    $("#recalled-list").innerHTML = r.memory_recalled.map((h) =>
      `<div class="memory-item"><span class="kind">${escapeHtml(h.kind)}</span>${escapeHtml(h.text)}</div>`).join("");
  } else {
    $("#recalled-box").style.display = "none";
  }
}

function renderAnswer(text) {
  return escapeHtml(text)
    .replace(/\[(\d+)\]/g, '<span class="cite">[$1]</span>')
    .replace(/\n/g, "<br>");
}

/* ---------- 资料库 ---------- */
async function loadDocs() {
  try {
    const d = await api("/api/documents");
    $("#docs-tbody").innerHTML = (d.documents || []).map((doc) => `
      <tr>
        <td>${escapeHtml(doc.title)}</td>
        <td>${escapeHtml(doc.tags).split(/[,，]/).filter(Boolean).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</td>
        <td>${doc.n_chunks}</td>
        <td class="muted">${escapeHtml(doc.created_at || "")}</td>
        <td>
          <button class="ghost" data-action="export" data-id="${doc.id}">导出</button>
          <button class="ghost" data-action="delete" data-id="${doc.id}">删除</button>
        </td>
      </tr>`).join("") || `<tr><td colspan="5" class="empty">还没有资料，先在下方导入。</td></tr>`;
  } catch (e) { toast("加载资料失败：" + e.message); }
}

async function delDoc(id) {
  if (!confirm("确定删除这篇资料？删除后需要重新导入。")) return;
  try {
    await api(`/api/documents/${id}`, { method: "DELETE" });
    toast("已删除");
    loadDocs();
  } catch (e) { toast("删除失败：" + e.message); }
}

async function exportDoc(id) {
  try {
    const d = await api(`/api/documents/${id}/chunks`);
    const blob = new Blob([JSON.stringify(d, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${safeFileName(d.document && d.document.title)}-${id}-chunks.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    toast(`已导出 ${(d.chunks || []).length} 个分块`);
  } catch (e) { toast("导出失败：" + e.message); }
}

async function ingestFile() {
  const f = $("#doc-file").files[0];
  const title = $("#doc-title").value.trim();
  const tags = $("#doc-tags").value.trim();
  let content = $("#doc-content").value.trim();
  saveUser();
  if (f) {
    if (!title) { toast("请先填标题"); return; }
    try { content = await f.text(); } catch (e) { toast("读取文件失败：" + e.message); return; }
  }
  if (!title || !content) { toast("标题和内容都要有"); return; }
  const btn = $("#doc-submit");
  btn.disabled = true; btn.textContent = "入库中（向量化稍慢）…";
  try {
    const r = await api("/api/ingest", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, source: f ? f.name : "手动录入", tags, content }),
    });
    toast(`已入库：${r.title}，切分 ${r.chunks} 块`);
    $("#doc-title").value = ""; $("#doc-content").value = ""; $("#doc-file").value = "";
    loadDocs();
  } catch (e) { toast("入库失败：" + e.message); }
  finally { btn.disabled = false; btn.textContent = "导入资料库"; }
}

/* ---------- 记忆页 ---------- */
async function loadMemory() {
  saveUser();
  try {
    const m = await api(`/api/memory?user=${encodeURIComponent(state.user)}`);
    $("#mem-scope").textContent = m.scope_id || "（PowerContext 未连接）";
    const kinds = { note: "结论", decision: "决定", task: "事项", preference: "偏好" };
    $("#mem-entries").innerHTML = (m.entries || []).map((e) =>
      `<div class="memory-item"><span class="kind">[${kinds[e.kind] || e.kind}]</span>${escapeHtml(e.text)}</div>`
    ).join("") || `<div class="empty">这位工位还没有记忆。去「问答」页聊几句，确认过的结论会被记在这里。</div>`;
  } catch (e) { toast("加载记忆失败：" + e.message); }
}

/* ---------- 证据页 ---------- */
async function loadEvidence() {
  try {
    const t = await api("/api/traces?limit=25");
    const fmt = (arr) => (arr || []).map((x) => JSON.stringify(x, null, 0)).join("\n") || "（暂无记录）";
    $("#trace-sql").textContent = fmt(t.sql);
    $("#trace-retrieval").textContent = fmt(t.retrieval);
    $("#trace-memory").textContent = fmt(t.memory);
    $("#trace-chat").textContent = fmt(t.chat);
  } catch (e) { toast("加载证据失败：" + e.message); }
}

/* ---------- 工具 ---------- */
function saveUser() {
  const u = $("#user-input").value.trim() || "guest";
  state.user = u;
  localStorage.setItem("seekmem.user", u);
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function safeFileName(s) {
  const cleaned = String(s || "document").replace(/[\\/:*?"<>|\r\n\t]+/g, "_").trim().slice(0, 80);
  return cleaned || "document";
}

window.addEventListener("DOMContentLoaded", () => {
  $("#user-input").value = state.user;
  $("#user-input").addEventListener("change", () => { saveUser(); loadWelcome(); });
  document.querySelectorAll("nav.tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  $("#chat-send").addEventListener("click", sendChat);
  $("#chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) sendChat(); });
  $("#doc-submit").addEventListener("click", ingestFile);
  $("#docs-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    if (!Number.isInteger(id)) return;
    if (btn.dataset.action === "delete") delDoc(id);
    else if (btn.dataset.action === "export") exportDoc(id);
  });
  $("#health-pill").addEventListener("click", loadHealth);
  loadHealth();
  loadWelcome();
  switchTab("chat");
});
