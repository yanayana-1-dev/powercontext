// Mini KB server: thin Node middle layer between the browser app and seekdb.
// All embeddings are computed in the browser; this server only forwards
// storage and retrieval to seekdb over its MySQL-compatible protocol
// (mysql2, the access path recommended by the seekdb README). The same SQL
// works verbatim through seekdb-js' underlying connection.
import http from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import mysql from "mysql2/promise";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WEB_ROOT = path.join(__dirname, "..", "web");

const SEEKB_HOST = process.env.SEEKDB_HOST || "127.0.0.1";
const SEEKB_PORT = Number(process.env.SEEKDB_PORT || 2881);
const SEEKB_USER = process.env.SEEKDB_USER || "root";
const SEEKB_PASSWORD = process.env.SEEKDB_PASSWORD || "";
const DATABASE = process.env.SEEKDB_DATABASE || "stepfun_kb";
const HTTP_PORT = Number(process.env.PORT || 5173);

const pool = mysql.createPool({
  host: SEEKB_HOST,
  port: SEEKB_PORT,
  user: SEEKB_USER,
  password: SEEKB_PASSWORD,
  // no default database here: it is created on demand and every statement
  // below addresses tables with an explicit `database`.`table` qualifier.
  connectionLimit: 4,
  timezone: "+00:00",
});

async function execute(sql, params = []) {
  const [rows] = await pool.query(sql, params);
  return rows;
}

// ---------- helpers ----------

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
};

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "content-type": "application/json",
    "cache-control": "no-store",
    "access-control-allow-origin": "*",
  });
  res.end(body);
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

function tableName(collection) {
  if (!/^[A-Za-z0-9_]+$/.test(collection)) throw new Error("bad collection name");
  // fully qualified so no default database is needed on the connection
  return `\`${DATABASE}\`.\`kb_${collection}\``;
}

function bareTable(collection) {
  if (!/^[A-Za-z0-9_]+$/.test(collection)) throw new Error("bad collection name");
  return `kb_${collection}`;
}

function ensureVectorLiteral(vec) {
  if (!Array.isArray(vec) || vec.length === 0 || vec.some((v) => typeof v !== "number" || !Number.isFinite(v))) {
    throw new Error("embedding must be a non-empty finite number array");
  }
  return JSON.stringify(vec);
}

// ---------- seekdb operations ----------

async function ensureDatabase() {
  await execute(`CREATE DATABASE IF NOT EXISTS \`${DATABASE}\``);
}

async function tableExists(name) {
  const rows = await execute(
    `SELECT table_name FROM information_schema.tables WHERE table_schema = ? AND table_name = ?`,
    [DATABASE, name]
  );
  return rows.length > 0;
}

async function createTable(name, dim) {
  // Schema verified against seekdb v1.4: HEAP table + ik fulltext + vsag HNSW.
  await execute(`CREATE TABLE ${name} (
    id VARCHAR(64) PRIMARY KEY,
    content TEXT,
    embedding VECTOR(${Number(dim)}),
    FULLTEXT INDEX idx_ft (content) WITH PARSER ik,
    VECTOR INDEX idx_vec (embedding) WITH (DISTANCE = l2, TYPE = hnsw, LIB = vsag)
  ) ORGANIZATION = HEAP`);
}

async function createCollection({ name, dim }) {
  await ensureDatabase();
  const table = tableName(name);
  if (await tableExists(bareTable(name))) throw new Error(`collection "${name}" already exists`);
  await createTable(table, dim);
  return { name, table, dim };
}

async function listCollections() {
  await ensureDatabase();
  const rows = await execute(
    `SELECT table_name FROM information_schema.tables WHERE table_schema = ? AND table_name LIKE 'kb_%'`,
    [DATABASE]
  );
  const result = [];
  for (const row of rows) {
    const table = row.table_name ?? row.TABLE_NAME;
    const cols = await execute(
      `SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = ? AND table_name = ?`,
      [DATABASE, table]
    );
    let dim = null;
    let count = 0;
    for (const c of cols) {
      if ((c.column_name ?? c.COLUMN_NAME) === "embedding") {
        const t = String(c.data_type ?? c.DATA_TYPE ?? "");
        const m = t.match(/(\d+)/);
        if (m) dim = Number(m[1]);
      }
    }
    try {
      const cnt = await execute(`SELECT COUNT(*) AS c FROM ${table}`);
      count = Number(cnt[0]?.c ?? cnt[0]?.C ?? 0);
    } catch { /* count best-effort */ }
    result.push({ name: table.replace(/^kb_/, ""), table, dim, docs: count });
  }
  return result;
}

async function addDocuments({ collection, ids, documents, embeddings }) {
  const table = tableName(collection);
  if (!(await tableExists(bareTable(collection)))) throw new Error(`collection "${collection}" does not exist`);
  if (!Array.isArray(ids) || ids.length !== documents.length || embeddings.length !== ids.length) {
    throw new Error("ids/documents/embeddings must be parallel arrays");
  }
  // Chunked inserts to stay within bind-parameter limits.
  const CHUNK = 100;
  let inserted = 0;
  for (let i = 0; i < ids.length; i += CHUNK) {
    const slice = ids.slice(i, i + CHUNK).map((_, j) => [
      String(ids[i + j]).slice(0, 64),
      String(documents[i + j]),
      ensureVectorLiteral(embeddings[i + j]),
    ]);
    const values = slice.map(() => "(?, ?, ?)").join(", ");
    const params = slice.flat();
    await execute(
      `INSERT INTO ${table} (id, content, embedding) VALUES ${values}`,
      params
    );
    inserted += slice.length;
  }
  return { inserted };
}

async function refreshIndex({ collection }) {
  const table = tableName(collection);
  if (!(await tableExists(bareTable(collection)))) throw new Error(`collection "${collection}" does not exist`);
  // Prefer seekdb-js built-in refresh; fall back to DBMS_VECTOR.REFRESH_INDEX.
  const errors = [];
  try {
    await execute("CALL dbms_index_manager.refresh()");
    return { ok: true, via: "dbms_index_manager.refresh" };
  } catch (e1) {
    errors.push(String(e1?.message || e1));
  }
  try {
    await execute(
      `CALL DBMS_VECTOR.REFRESH_INDEX('idx_vec', 'kb_${collection}', NULL, 1, 'FAST')`
    );
    return { ok: true, via: "DBMS_VECTOR.REFRESH_INDEX(FAST)" };
  } catch (e2) {
    errors.push(String(e2?.message || e2));
  }
  return { ok: false, errors };
}

async function search({ collection, embedding, text, limit, mode }) {
  const table = tableName(collection);
  if (!(await tableExists(bareTable(collection)))) throw new Error(`collection "${collection}" does not exist`);
  const vec = ensureVectorLiteral(embedding);
  const k = Math.max(1, Math.min(Number(limit) || 10, 100));
  const t0 = process.hrtime.bigint();
  let rows;
  if (mode === "vector") {
    rows = await execute(
      `SELECT id, content, l2_distance(embedding, ?) AS d FROM ${table} ORDER BY d APPROXIMATE LIMIT ${k}`,
      [vec]
    );
    rows = rows.map((r) => ({ id: r.id, content: r.content, distance: r.d, channel: "vector" }));
  } else if (mode === "fts") {
    rows = await execute(
      `SELECT id, content FROM ${table} WHERE MATCH(content) AGAINST(?) LIMIT ${k}`,
      [text || ""]
    );
    rows = rows.map((r) => ({ id: r.id, content: r.content, channel: "fts" }));
  } else {
    // Hybrid: full-text filter + vector ordering fused inside one seekdb statement.
    rows = await execute(
      `SELECT id, content, l2_distance(embedding, ?) AS d FROM ${table} WHERE MATCH(content) AGAINST(?) ORDER BY d APPROXIMATE LIMIT ${k}`,
      [vec, text || ""]
    );
    rows = rows.map((r) => ({ id: r.id, content: r.content, distance: r.d, channel: "hybrid" }));
  }
  const ms = Number(process.hrtime.bigint() - t0) / 1e6;
  return { mode, tookMs: Math.round(ms * 100) / 100, results: rows };
}

async function status() {
  const info = {};
  try {
    const v = await execute("SELECT VERSION() AS v");
    info.seekdbVersion = v[0]?.v;
    info.connected = true;
  } catch (e) {
    info.connected = false;
    info.error = String(e?.message || e);
  }
  info.host = `${SEEKB_HOST}:${SEEKB_PORT}`;
  info.database = DATABASE;
  return info;
}

async function dropCollection({ collection }) {
  const table = tableName(collection);
  await execute(`DROP TABLE IF EXISTS ${table}`);
  return { dropped: collection };
}

// ---------- http server ----------

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const route = `${req.method} ${url.pathname}`;

  try {
    if (route === "GET /api/status") return sendJson(res, 200, await status());
    if (route === "GET /api/collections") return sendJson(res, 200, { collections: await listCollections() });
    if (req.method === "POST" && url.pathname.startsWith("/api/")) {
      const body = await readBody(req);
      if (route === "POST /api/collections") return sendJson(res, 200, await createCollection(body));
      if (route === "POST /api/add") return sendJson(res, 200, await addDocuments(body));
      if (route === "POST /api/refresh") return sendJson(res, 200, await refreshIndex(body));
      if (route === "POST /api/search") return sendJson(res, 200, await search(body));
      if (route === "POST /api/drop") return sendJson(res, 200, await dropCollection(body));
    }
    // /hf/* — same-origin reverse proxy for model downloads (hf-mirror CDN
    // does not send permissive CORS headers, so the browser fetches through
    // this local proxy). Only model files pass through; inference is local.
    if (req.method === "GET" && url.pathname.startsWith("/hf/")) {
      const target = "https://hf-mirror.com" + url.pathname.slice(3) + url.search;
      try {
        const upstream = await fetch(target);
        // Node's fetch transparently decompresses gzip/br, so the decoded
        // byte count can exceed the upstream content-length. Never forward
        // that header (a truncated body corrupts the ONNX file); use chunked.
        const headers = {
          "content-type": upstream.headers.get("content-type") || "application/octet-stream",
          "cache-control": "public, max-age=86400",
          "access-control-allow-origin": "*",
        };
        res.writeHead(upstream.status, headers);
        if (upstream.body) {
          for await (const chunk of upstream.body) res.write(chunk);
        }
        return res.end();
      } catch (e) {
        return sendJson(res, 502, { error: `model proxy failed: ${e?.message || e}` });
      }
    }

    // static files
    let file = url.pathname === "/" ? "/index.html" : url.pathname;
    file = path.normalize(file).replace(/^([/\\])+/, "");
    const full = path.join(WEB_ROOT, file);
    if (full.startsWith(WEB_ROOT) && existsSync(full)) {
      const data = readFileSync(full);
      res.writeHead(200, {
        "content-type": MIME[path.extname(full)] || "application/octet-stream",
        "cache-control": "no-store",
      });
      return res.end(data);
    }
    sendJson(res, 404, { error: "not found" });
  } catch (e) {
    sendJson(res, 400, { error: String(e?.message || e) });
  }
});

server.listen(HTTP_PORT, "127.0.0.1", () => {
  console.log(`[mini-kb] web UI   -> http://127.0.0.1:${HTTP_PORT}`);
  console.log(`[mini-kb] seekdb  -> ${SEEKB_HOST}:${SEEKB_PORT}/${DATABASE}`);
});
