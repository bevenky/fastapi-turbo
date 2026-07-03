// Fastify bench app — mirrors benchmarks/matrix/app.py (the CONTRACT)
// endpoint-for-endpoint with byte-identical JSON. Key ORDER follows app.py;
// responses are built as ordered plain objects sent WITHOUT a response schema
// so JSON.stringify preserves insertion order (no key reordering).
//
// Run:  PORT=8005 node server.js
// Multi-core: CLUSTER=N forks N workers sharing the port (round-robin). The
// primary only manages workers — it does NOT create pools or listen. With
// CLUSTER unset/<=1 this is a single process (original behavior).
const cluster = require("node:cluster");
const CLUSTERN = Number(process.env.CLUSTER || 0);
if (CLUSTERN > 1 && cluster.isPrimary) {
  for (let i = 0; i < CLUSTERN; i++) cluster.fork();
  return; // primary doesn't load fastify/pg/redis
}

const fastify = require("fastify")({ logger: false });
const { Readable } = require("node:stream");
const { Pool } = require("pg");
const Redis = require("ioredis");

// ── shared backends ──────────────────────────────────────────────────
const pg = new Pool({
  host: "127.0.0.1",
  port: 5432,
  database: "fastapi_turbo_bench",
  user: "venky",
  min: 4,
  max: 8,
});
const redis = new Redis(6379, "127.0.0.1");

// ── shared payload builders (byte-identical across languages) ─────────
function largeList() {
  const items = new Array(1000);
  for (let i = 0; i < 1000; i++) items[i] = { id: i, name: `item-${i}` };
  return { items };
}

const XML_SMALL =
  '<?xml version="1.0" encoding="UTF-8"?><item><id>1</id><name>item-1</name></item>';

const XML_LARGE = (() => {
  const parts = ['<?xml version="1.0" encoding="UTF-8"?><items>'];
  for (let i = 0; i < 1000; i++) {
    parts.push(`<item><id>${i}</id><name>item-${i}</name></item>`);
  }
  parts.push("</items>");
  return parts.join("");
})();

// ── streaming contract: 10 chunks, each a fixed 64-byte line ──────────
const STREAM_CHUNKS = 10;
const STREAM_CT = "text/plain; charset=utf-8";
function streamChunk(i) {
  return Buffer.from(`chunk-${i}: ` + "=".repeat(54) + "\n");
}

// Body schemas: accept the contract bodies (loose — no extra validation that
// would reject). We avoid RESPONSE schemas entirely to preserve key order.
const itemBodySchema = {
  body: {
    type: "object",
    properties: {
      sku: { type: "string" },
      qty: { type: "integer" },
      tags: { type: "array", items: { type: "string" } },
    },
    additionalProperties: true,
  },
};
const patchBodySchema = {
  body: {
    type: "object",
    properties: { qty: { type: "integer" } },
    additionalProperties: true,
  },
};

// ── static baseline ──────────────────────────────────────────────────
fastify.get("/_ping", (req, reply) => {
  reply.type("application/json").send('{"ping":"pong"}');
});

// ── plain JSON: simple ───────────────────────────────────────────────
fastify.get("/hello", async () => ({ message: "hello" }));
fastify.get("/async/hello", async () => ({ message: "hello" }));

// ── plain JSON: large ────────────────────────────────────────────────
fastify.get("/json/large", async () => largeList());
fastify.get("/async/json/large", async () => largeList());

// ── body methods: POST / PUT / PATCH / DELETE ────────────────────────
fastify.post("/items", { schema: itemBodySchema }, async (req) => {
  const { sku, qty, tags = [] } = req.body;
  return { ok: true, sku, qty, tag_count: tags.length };
});
fastify.post("/async/items", { schema: itemBodySchema }, async (req) => {
  const { sku, qty, tags = [] } = req.body;
  return { ok: true, sku, qty, tag_count: tags.length };
});

fastify.put("/items/:id", { schema: itemBodySchema }, async (req) => {
  const id = Number(req.params.id);
  const { sku, qty, tags = [] } = req.body;
  return { ok: true, id, sku, qty, tag_count: tags.length };
});

fastify.patch("/items/:id", { schema: patchBodySchema }, async (req) => {
  const id = Number(req.params.id);
  const { qty } = req.body;
  return { ok: true, id, qty };
});

fastify.delete("/items/:id", async (req) => {
  const id = Number(req.params.id);
  return { ok: true, deleted: id };
});

// ── XML ──────────────────────────────────────────────────────────────
fastify.get("/xml/small", (req, reply) => {
  reply.type("application/xml").send(XML_SMALL);
});
fastify.get("/xml/large", (req, reply) => {
  reply.type("application/xml").send(XML_LARGE);
});

// ── streaming: 10 chunks, no artificial delay ────────────────────────
function streamHandler(req, reply) {
  async function* gen() {
    for (let i = 0; i < STREAM_CHUNKS; i++) yield streamChunk(i);
  }
  reply.type(STREAM_CT);
  return reply.send(Readable.from(gen()));
}
fastify.get("/stream-sync", streamHandler);
fastify.get("/stream-async", streamHandler);
fastify.get("/stream-await", streamHandler);

// ── Redis ────────────────────────────────────────────────────────────
fastify.get("/redis/get/sync", async (req, reply) => {
  const val = await redis.get("bench:item");
  reply.type("application/json").send(val);
});
fastify.get("/redis/get/async", async (req, reply) => {
  const val = await redis.get("bench:item");
  reply.type("application/json").send(val);
});
fastify.post("/redis/set/sync", async () => {
  await redis.set("bench:wkey", "bench-value");
  return { ok: true };
});
fastify.post("/redis/set/async", async () => {
  await redis.set("bench:wkey", "bench-value");
  return { ok: true };
});

// ── Postgres ─────────────────────────────────────────────────────────
async function pgItem(req, reply) {
  const id = Number(req.params.id);
  const { rows } = await pg.query(
    "SELECT id, sku, name, qty FROM items WHERE id = $1",
    [id]
  );
  if (rows.length === 0) {
    reply.code(404).send();
    return;
  }
  const r = rows[0];
  return { id: r.id, sku: r.sku, name: r.name, qty: r.qty };
}
fastify.get("/pg/item/:id/sync", pgItem);
fastify.get("/pg/item/:id/async", pgItem);

async function pgItems(req) {
  const limit = req.query.limit !== undefined ? Number(req.query.limit) : 10;
  const { rows } = await pg.query(
    "SELECT id, sku, name, qty FROM items ORDER BY id LIMIT $1",
    [limit]
  );
  return {
    items: rows.map((r) => ({ id: r.id, sku: r.sku, name: r.name, qty: r.qty })),
  };
}
fastify.get("/pg/items/sync", pgItems);
fastify.get("/pg/items/async", pgItems);

// ══════════════════════════════════════════════════════════════════════
// app_db.py CROSS-FRAMEWORK contract: /pg/{op}/{sync|async} and
// /redis/{sync|async}/{op}. Node has ONE native pg driver (node-postgres)
// and ONE native redis driver (ioredis), and no sync/async language split,
// so the .../sync and .../async paths share the SAME handler code and
// return identical output. JSON built as ordered plain objects (no response
// schema) so JSON.stringify preserves key insertion order.
// ══════════════════════════════════════════════════════════════════════

// ── Postgres (app_db.py contract) ────────────────────────────────────
const SEL_ONE = "SELECT id, sku, name, qty FROM items WHERE id = $1";
const SEL_LIST = "SELECT id, sku, name, qty FROM items ORDER BY id LIMIT $1";
const INS = "INSERT INTO bench_writes (val) VALUES ($1)";
const UPD = "UPDATE bench_writes SET val = $1 WHERE id = 5";
const DEL = "DELETE FROM bench_writes WHERE id = $1";
const DEL_ID = 999999999;

function pgRow(r) {
  return { id: r.id, sku: r.sku, name: r.name, qty: r.qty };
}

// commit flag from ?commit=true|false (string), default true
function commitFlag(req) {
  const c = req.query.commit;
  return c === undefined ? true : c !== "false";
}

// Reads: autocommit pool.query = 1 wire round trip (contract fairness: pgx
// and psycopg3-autocommit reads are 1 RTT; explicit BEGIN/COMMIT would be 3).
async function pgSelectOne() {
  const { rows } = await pg.query(SEL_ONE, [5]);
  return pgRow(rows[0]);
}
fastify.get("/pg/select_one/sync", pgSelectOne);
fastify.get("/pg/select_one/async", pgSelectOne);

async function pgSelectList() {
  const { rows } = await pg.query(SEL_LIST, [10]);
  return { items: rows.map(pgRow) };
}
fastify.get("/pg/select_list/sync", pgSelectList);
fastify.get("/pg/select_list/async", pgSelectList);

// writes: explicit per-request txn; honor commit (true→COMMIT, false→ROLLBACK)
async function pgWrite(sql, params, commit) {
  const client = await pg.connect();
  try {
    await client.query("BEGIN");
    await client.query(sql, params);
    await client.query(commit ? "COMMIT" : "ROLLBACK");
    return { ok: true, committed: commit };
  } catch (e) {
    await client.query("ROLLBACK");
    throw e;
  } finally {
    client.release();
  }
}

async function pgInsert(req) {
  return pgWrite(INS, ["x"], commitFlag(req));
}
fastify.post("/pg/insert/sync", pgInsert);
fastify.post("/pg/insert/async", pgInsert);

async function pgUpdate(req) {
  return pgWrite(UPD, ["y"], commitFlag(req));
}
fastify.post("/pg/update/sync", pgUpdate);
fastify.post("/pg/update/async", pgUpdate);

async function pgDelete(req) {
  return pgWrite(DEL, [DEL_ID], commitFlag(req));
}
fastify.post("/pg/delete/sync", pgDelete);
fastify.post("/pg/delete/async", pgDelete);

// ── Redis (app_db.py contract) ───────────────────────────────────────
const RKEY = "bench:item";
const RWKEY = "bench:wkey";
const RVAL = "bench-value";

async function redisGet(req, reply) {
  const val = await redis.get(RKEY);
  reply.type("application/json").send(val);
}
fastify.get("/redis/sync/get", redisGet);
fastify.get("/redis/async/get", redisGet);

async function redisSet() {
  await redis.set(RWKEY, RVAL);
  return { ok: true };
}
fastify.post("/redis/sync/set", redisSet);
fastify.post("/redis/async/set", redisSet);

async function redisSetDurable() {
  await redis.set(RWKEY, RVAL);
  try {
    // durability: force an AOF fsync via WAITAOF (local fsync, 0 replicas).
    await redis.call("WAITAOF", 1, 0, 100);
  } catch (e) {
    // AOF off / WAITAOF unsupported — ignore
  }
  return { ok: true };
}
fastify.post("/redis/sync/set_durable", redisSetDurable);
fastify.post("/redis/async/set_durable", redisSetDurable);

async function redisPipeline() {
  const pipe = redis.pipeline(); // NON-transactional batch
  for (let i = 0; i < 10; i++) pipe.set(`${RWKEY}:${i}`, RVAL);
  await pipe.exec();
  return { ok: true, n: 10 };
}
fastify.post("/redis/sync/pipeline", redisPipeline);
fastify.post("/redis/async/pipeline", redisPipeline);

async function redisMulti() {
  const tx = redis.multi(); // MULTI/EXEC transaction
  for (let i = 0; i < 10; i++) tx.set(`${RWKEY}:${i}`, RVAL);
  await tx.exec();
  return { ok: true, n: 10 };
}
fastify.post("/redis/sync/multi", redisMulti);
fastify.post("/redis/async/multi", redisMulti);

// ── WebSocket echo (contract: receive text -> echo SAME text back) ────
// @fastify/websocket v11 (fastify v5): handler gets the raw `ws` socket.
// Echo preserves the frame type (text stays text) and the exact payload.
fastify.register(require("@fastify/websocket"));
fastify.register(async function (fastify) {
  fastify.get("/ws", { websocket: true }, (socket /* ws.WebSocket */) => {
    socket.on("message", (data, isBinary) => {
      socket.send(data, { binary: isBinary });
    });
    // close: ws answers the closing handshake automatically — nothing to do.
  });
});

// ── boot ─────────────────────────────────────────────────────────────
const port = Number(process.env.PORT || 8005);
fastify
  .listen({ host: "127.0.0.1", port })
  .then(() => console.log(`Fastify matrix bench on http://127.0.0.1:${port}`));
