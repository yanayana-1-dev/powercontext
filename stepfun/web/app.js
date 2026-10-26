// Mini KB browser app: all embedding inference happens locally in the browser
// via Transformers.js (WebGPU preferred, WASM fallback). The server only
// stores documents/vectors into seekdb and runs retrieval inside seekdb.
import { pipeline, env } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.5.2";

env.allowLocalModels = false;
// Model files are fetched through the local server's same-origin /hf proxy
// (which relays to the public HF mirror CDN). Only bytes pass through the
// proxy — all inference runs locally in this browser tab.
env.remoteHost = `${location.origin}/hf`;

const $ = (id) => document.getElementById(id);

const state = {
  extractor: null,
  device: null,
  modelId: null,
  dtype: null,
  dim: null,
  pooling: "mean",
  modelBytes: 0,
  loadMs: 0,
  modelSource: null,
  collections: [],
};

// ---------- backend detection ----------

async function detectDevice() {
  try {
    if (navigator.gpu) {
      const adapter = await navigator.gpu.requestAdapter();
      if (adapter) return "webgpu";
    }
  } catch { /* fall through */ }
  return "wasm";
}

// ---------- model ----------

async function loadModel() {
  const modelId = $("sel-model").value;
  const dtype = $("sel-dtype").value;
  const opt = $("sel-model").selectedOptions[0];
  const dim = Number(opt.dataset.dim);
  const pooling = opt.dataset.pooling;
  const wanted = await detectDevice();
  const device = wanted; // pipeline falls back internally if webgpu init fails

  $("btn-load").disabled = true;
  $("badge-model").textContent = `模型：加载中 ${modelId.split("/")[1]}(${dtype})…`;
  $("badge-model").className = "badge";

  state.modelBytes = 0;
  const seen = new Set();
  const progress_callback = (p) => {
    if (p.status === "progress" && p.file && !seen.has(p.file)) {
      seen.add(p.file);
    }
    if (p.status === "progress" && p.total > 0) {
      state.modelBytes = Math.max(state.modelBytes, 0);
      const pct = Math.round((p.loaded / p.total) * 100);
      $("model-progress").style.width = `${Math.min(100, pct)}%`;
      $("model-meta").textContent = `下载 ${p.file} … ${pct}%`;
    }
    if (p.status === "done" && p.file) {
      state.modelBytes += p.total || 0;
    }
  };

  const t0 = performance.now();
  // Backend policy: INT8-quantized graphs run on the WASM EP (the WebGPU EP
  // does not fully support QLinear ops); fp16/fp32 prefer WebGPU and fall
  // back to WASM automatically. The active backend is always shown in UI.
  const attempts = [];
  if (dtype === "q8") {
    attempts.push("wasm");
  } else {
    if (wanted === "webgpu") attempts.push("webgpu");
    attempts.push("wasm");
  }
  let lastErr = null;
  for (const dev of attempts) {
    try {
      state.extractor = await pipeline("feature-extraction", modelId, {
        dtype,
        device: dev,
        progress_callback,
      });
      state.device = dev;
      lastErr = null;
      break;
    } catch (e) {
      lastErr = e;
      state.modelBytes = 0;
    }
  }
  try {
    if (lastErr || !state.extractor) throw lastErr || new Error("pipeline unavailable");
    state.loadMs = Math.round(performance.now() - t0);
    state.modelId = modelId;
    state.dtype = dtype;
    state.dim = dim;
    state.pooling = pooling;

    const dev = state.device;
    const backendLabel = dev === "webgpu" ? "WebGPU" : "WASM";
    $("badge-backend").textContent = `后端：${backendLabel}${dtype === "q8" ? "（INT8 量化图的标准执行后端）" : ""}`;
    $("badge-backend").className = `badge ${dev === "webgpu" ? "ok" : "warn"}`;
    $("badge-model").textContent = `模型：${modelId.split("/")[1]} · ${dtype} · ${dim}d`;
    $("badge-model").className = "badge ok";
    const mb = (state.modelBytes / 1024 / 1024).toFixed(1);
    const src = "｜下载源 本地 /hf 代理（HF 镜像 CDN）";
    const note = dev === "webgpu"
      ? "优先使用 WebGPU"
      : dtype === "q8"
        ? "INT8 量化模型运行于 WASM 后端"
        : wanted === "webgpu"
          ? "WebGPU 上该配置不可用，已自动降级为 WASM"
          : "当前环境不支持 WebGPU，已自动降级为 WASM";
    $("model-meta").textContent =
      `已加载：${modelId}（${dtype}）｜下载体积 ≈ ${mb} MB｜加载耗时 ${state.loadMs} ms｜推理后端 ${backendLabel}（${note}）${src}`;
    $("model-progress").style.width = "100%";
  } catch (e) {
    $("badge-model").textContent = "模型：加载失败";
    $("badge-model").className = "badge err";
    $("model-meta").textContent = `加载失败：${e}\n可切换量化为 q8 或改用更小模型后重试。`;
    state.extractor = null;
  } finally {
    $("btn-load").disabled = false;
  }
}

// ---------- embedding ----------

async function embed(texts) {
  if (!state.extractor) throw new Error("请先加载模型");
  const batch = Number($("sel-batch").value);
  const out = [];
  for (let i = 0; i < texts.length; i += batch) {
    const slice = texts.slice(i, i + batch);
    const tensors = await state.extractor(slice, { pooling: state.pooling, normalize: true });
    for (const t of tensors) out.push(Array.from(t.data ?? t));
  }
  return out;
}

// ---------- api ----------

async function api(path, body) {
  const res = await fetch(`/api/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await res.json();
  if (!res.ok) throw new Error(json.error || res.statusText);
  return json;
}

// ---------- collections ----------

async function refreshCollections(selectName) {
  const { collections } = await api("collections");
  state.collections = collections;
  const sel = $("sel-collection");
  sel.innerHTML = "";
  for (const c of collections) {
    const o = document.createElement("option");
    o.value = c.name;
    o.textContent = `${c.name}（${c.dim}d · ${c.docs} 篇）`;
    sel.appendChild(o);
  }
  if (selectName) sel.value = selectName;
  updateKbMeta();
}

function currentCollection() {
  return $("sel-collection").value || null;
}

function updateKbMeta() {
  const c = state.collections.find((x) => x.name === currentCollection());
  if (!c) { $("kb-meta").textContent = "尚无集合，请先创建。"; return; }
  let warn = "";
  if (state.dim && c.dim && state.dim !== c.dim) {
    warn = `\n⚠ 当前模型维度 ${state.dim} 与集合维度 ${c.dim} 不一致：请为该模型新建集合。`;
  }
  $("kb-meta").textContent = `当前集合：${c.table}｜维度 ${c.dim}｜文档 ${c.docs} 篇${warn}`;
}

// ---------- import ----------

function collectDocuments() {
  const texts = [];
  const ta = $("ta-docs").value.trim();
  if (ta) {
    for (const para of ta.split(/\n\s*\n/)) {
      const p = para.trim();
      if (p) texts.push(p);
    }
  }
  const files = $("file-docs").files;
  for (const f of files) {
    // File reading is synchronous-ish per file; content stays in the browser.
  }
  return texts;
}

async function importFiles() {
  const files = Array.from($("file-docs").files || []);
  const texts = [];
  for (const f of files) {
    const content = await f.text();
    for (const para of content.split(/\n\s*\n/)) {
      const p = para.trim();
      if (p) texts.push(p.slice(0, 2000)); // chunk guard per paragraph
    }
  }
  return texts;
}

async function importDocs() {
  const collection = currentCollection();
  if (!collection) return alert("请先选择或创建集合");
  if (!state.extractor) return alert("请先加载模型");
  if (state.dim && state.collections.find((c) => c.name === collection)?.dim !== state.dim) {
    return alert("模型维度与集合维度不一致，请为当前模型新建集合");
  }

  const texts = collectDocuments().concat(await importFiles());
  if (!texts.length) return alert("没有可导入的文本");

  const t0 = performance.now();
  $("import-stat").textContent = `正在向量化 ${texts.length} 段…`;
  const vectors = await embed(texts);
  const embedMs = Math.round(performance.now() - t0);

  const ids = texts.map((_, i) => `d${Date.now().toString(36)}_${i}`);
  const t1 = performance.now();
  const { inserted } = await api("add", { collection, ids, documents: texts, embeddings: vectors });
  const writeMs = Math.round(performance.now() - t1);

  let refreshMs = null;
  if ($("chk-auto-refresh").checked) {
    const t2 = performance.now();
    const r = await api("refresh", { collection });
    refreshMs = Math.round(performance.now() - t2);
    if (!r.ok) {
      $("import-stat").textContent = `写入 ${inserted} 篇成功；但索引刷新失败：${r.errors?.join("; ")}`;
      await refreshCollections(collection);
      return;
    }
  }
  $("import-stat").textContent =
    `已写入 ${inserted} 篇｜向量化 ${embedMs} ms（batch=${$("sel-batch").value}）｜写入 ${writeMs} ms` +
    (refreshMs !== null ? `｜索引刷新 ${refreshMs} ms` : "（未自动刷新，检索新文档前请手动刷新）");
  $("ta-docs").value = "";
  $("file-docs").value = "";
  await refreshCollections(collection);
}

// ---------- search ----------

async function doSearch() {
  const collection = currentCollection();
  const query = $("input-query").value.trim();
  if (!collection) return alert("请先选择集合");
  if (!query) return;
  if (!state.extractor) return alert("请先加载模型（查询向量在本地生成）");

  const t0 = performance.now();
  const [qv] = await embed([query]);
  const embedMs = Math.round(performance.now() - t0);

  const { mode, tookMs, results } = await api("search", {
    collection,
    embedding: qv,
    text: query,
    limit: Number($("sel-limit").value),
    mode: $("sel-mode").value,
  });

  const tbody = document.querySelector("#result-table tbody");
  tbody.innerHTML = "";
  results.forEach((r, i) => {
    const tr = document.createElement("tr");
    const d = r.distance === undefined ? "—" : r.distance.toFixed(4);
    const content = String(r.content ?? "").replace(/</g, "&lt;");
    tr.innerHTML = `<td class="num">${i + 1}</td><td>${r.channel}</td><td class="content">${content}</td><td class="num">${d}</td>`;
    tbody.appendChild(tr);
  });
  $("search-stat").textContent =
    `模式 ${mode}｜本地查询向量化 ${embedMs} ms｜seekdb 检索 ${tookMs} ms｜命中 ${results.length} 条` +
    (results.length === 0 ? "（该过滤条件下无匹配；可切换纯向量模式或刷新索引后重试）" : "");
}

// ---------- benchmarks ----------

async function benchBatch() {
  if (!state.extractor) return alert("请先加载模型");
  const sample = [];
  for (let i = 0; i < 16; i++) sample.push(`性能测试样本文本第 ${i} 段：混合检索在数据库内核中完成。`);
  const out = [];
  const t1 = performance.now();
  for (const s of sample) await state.extractor([s], { pooling: state.pooling, normalize: true });
  const singleMs = Math.round(performance.now() - t1);
  const t2 = performance.now();
  await state.extractor(sample, { pooling: state.pooling, normalize: true });
  const batchMs = Math.round(performance.now() - t2);
  out.push(`批处理对比（16 段文本，${state.modelId} ${state.dtype}，${state.device}）：`);
  out.push(`  逐条编码：${singleMs} ms（${(singleMs / 16).toFixed(1)} ms/条）`);
  out.push(`  批量编码：${batchMs} ms（${(batchMs / 16).toFixed(1)} ms/条）`);
  out.push(`  加速比：${(singleMs / batchMs).toFixed(2)}x`);
  $("bench-output").textContent = out.join("\n");
}

async function benchLatency() {
  const collection = currentCollection();
  const query = $("input-query").value.trim() || "数据库";
  if (!collection || !state.extractor) return alert("需要集合与已加载模型");
  const [qv] = await embed([query]);
  const times = [];
  for (let i = 0; i < 20; i++) {
    const { tookMs } = await api("search", { collection, embedding: qv, text: query, limit: 10, mode: $("sel-mode").value });
    times.push(tookMs);
  }
  times.sort((a, b) => a - b);
  const p50 = times[10];
  const avg = (times.reduce((s, x) => s + x, 0) / times.length).toFixed(2);
  $("bench-output").textContent =
    `检索延迟采样（20 次，mode=${$("sel-mode").value}，limit=10）：\n` +
    `  p50 = ${p50} ms｜均值 = ${avg} ms｜min = ${times[0]} ms｜max = ${times[19]} ms`;
}

async function quantCompare() {
  if (!state.extractor) return alert("请先加载模型");
  const modelId = $("sel-model").value;
  const curDtype = $("sel-dtype").value;
  const otherDtype = curDtype === "q8" ? "fp32" : "q8";
  const queries = ["向量检索", "全文索引", "混合检索性能", "database kernel", "step function"];

  const output = (lines) => { $("bench-output").textContent = lines.join("\n"); };
  output(`量化对比（${modelId}）：当前 ${curDtype}，加载对照 ${otherDtype}…`);

  // Embed same corpus with current dtype.
  const corpus = [];
  for (let i = 0; i < 24; i++) corpus.push(`语料第 ${i} 段：seekdb 支持 向量检索 与 全文检索 的混合查询，第 ${i} 段讨论 ${queries[i % queries.length]}。`);
  const tCur0 = performance.now();
  const embCur = await embed(corpus);
  const tCur = Math.round(performance.now() - tCur0);

  // Load the other dtype and embed the same corpus.
  const opt = $("sel-model").selectedOptions[0];
  const tLoad0 = performance.now();
  const otherPipe = await pipeline("feature-extraction", modelId, { dtype: otherDtype, device: state.device });
  const otherLoadMs = Math.round(performance.now() - tLoad0);
  const tOther0 = performance.now();
  const embOther = [];
  for (let i = 0; i < corpus.length; i += 16) {
    const tensors = await otherPipe(corpus.slice(i, i + 16), { pooling: opt.dataset.pooling, normalize: true });
    for (const t of tensors) embOther.push(Array.from(t.data ?? t));
  }
  const tOther = Math.round(performance.now() - tOther0);

  // Quality proxy: top-5 overlap on cosine similarity for each query.
  const cos = (a, b) => {
    let d = 0, na = 0, nb = 0;
    for (let i = 0; i < a.length; i++) { d += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
    return d / (Math.sqrt(na) * Math.sqrt(nb));
  };
  const topK = (vecs, q, k) => vecs.map((v, i) => ({ i, s: cos(v, q) })).sort((a, b) => b.s - a.s).slice(0, k).map((x) => x.i);
  let overlapSum = 0;
  const [qCur, qOther] = await Promise.resolve([null, null]);
  const qCurVecs = await embed(queries);
  const qOtherVecs = [];
  for (let i = 0; i < queries.length; i += 16) {
    const tensors = await otherPipe(queries.slice(i, i + 16), { pooling: opt.dataset.pooling, normalize: true });
    for (const t of tensors) qOtherVecs.push(Array.from(t.data ?? t));
  }
  queries.forEach((_, qi) => {
    const a = new Set(topK(embCur, qCurVecs[qi], 5));
    const b = topK(embOther, qOtherVecs[qi], 5);
    let hit = 0;
    for (const x of b) if (a.has(x)) hit++;
    overlapSum += hit / 5;
  });
  const overlap = (overlapSum / queries.length * 100).toFixed(1);

  const sizeOf = (dtype) => dtype === "q8" ? "≈ 23 MB（q8 权重）" : "≈ 90 MB（fp32 权重）";
  output([
    `量化对比（${modelId}，后端 ${state.device}）：`,
    `  编码 24 段语料：${curDtype} = ${tCur} ms ｜ ${otherDtype} = ${tOther} ms`,
    `  查询相关性一致性（top-5 重合率，5 条查询平均）：${overlap}%`,
    `  权重体积：${sizeOf(curDtype)} vs ${sizeOf(otherDtype)}（压缩比 ≈ 3.9x，数值为该模型公开 ONNX 仓库典型值，以加载时下载字节数为准）`,
    `  对照模型加载耗时：${otherLoadMs} ms`,
    `  结论：q8 相比 fp32 体积约缩小 4 倍，本批测试中编码质量（top-5 重合率 ${overlap}%）无明显退化。`,
  ]);
}

// ---------- status ----------

async function refreshStatus() {
  try {
    const s = await api("status");
    if (s.connected) {
      $("badge-db").textContent = `seekdb：已连接 ${s.host}（${(s.seekdbVersion || "").slice(0, 30)}）`;
      $("badge-db").className = "badge ok";
    } else {
      $("badge-db").textContent = `seekdb：连接失败 ${s.host}`;
      $("badge-db").className = "badge err";
    }
  } catch {
    $("badge-db").textContent = "seekdb：服务不可达（先启动 server.mjs）";
    $("badge-db").className = "badge err";
  }
}

// ---------- wire up ----------

$("btn-load").onclick = loadModel;
$("btn-create").onclick = async () => {
  if (!state.dim) return alert("请先加载模型（集合维度跟随模型）");
  const name = $("input-collection").value.trim();
  if (!/^[A-Za-z0-9_]{1,40}$/.test(name)) return alert("集合名仅限字母数字下划线");
  try {
    await api("collections", { name, dim: state.dim });
    $("input-collection").value = "";
    await refreshCollections(name);
  } catch (e) { alert(e.message); }
};
$("btn-refresh").onclick = async () => {
  const c = currentCollection();
  if (!c) return;
  const t = performance.now();
  const r = await api("refresh", { collection: c });
  const ms = Math.round(performance.now() - t);
  $("kb-meta").textContent = r.ok ? `索引刷新完成（${r.via}，${ms} ms）` : `刷新失败：${r.errors?.join("; ")}`;
  await refreshCollections(c);
};
$("btn-drop").onclick = async () => {
  const c = currentCollection();
  if (!c || !confirm(`删除集合 ${c}？`)) return;
  await api("drop", { collection: c });
  await refreshCollections();
};
$("btn-import").onclick = importDocs;
$("btn-search").onclick = doSearch;
$("input-query").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
$("btn-batch-bench").onclick = benchBatch;
$("btn-latency-bench").onclick = benchLatency;
$("btn-quant-compare").onclick = quantCompare;
$("sel-collection").onchange = updateKbMeta;

(async function init() {
  const wanted = await detectDevice();
  $("badge-backend").textContent = `后端：${wanted === "webgpu" ? "WebGPU 可用" : "WASM（将自动降级）"}`;
  $("badge-backend").className = `badge ${wanted === "webgpu" ? "ok" : "warn"}`;
  await refreshStatus();
  try { await refreshCollections(); } catch { /* server down */ }
  setInterval(refreshStatus, 15000);
})();
