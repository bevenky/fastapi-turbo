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

// ── boot ─────────────────────────────────────────────────────────────
const port = Number(process.env.PORT || 8005);
fastify
  .listen({ host: "127.0.0.1", port })
  .then(() => console.log(`Fastify matrix bench on http://127.0.0.1:${port}`));
