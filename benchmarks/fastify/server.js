// Fastify bench app — mirrors benchmarks/_bench_app.py endpoint-for-endpoint
// (plus /_ping, matching fastapi-turbo's built-in pure-Rust baseline) so the
// comparison matrix is apples-to-apples.
//
// Run:  PORT=8004 node server.js     (npm i fastify here first)
const fastify = require("fastify")({ logger: false });
const { Readable } = require("node:stream");

// ── streaming contract ──────────────────────────────────────────────
// Exactly 10 chunks. Each chunk is a fixed 64-byte line:
//   `chunk-${i}: ` (9 bytes for single-digit i) + 54 '=' pad + "\n"
// Total body = 640 bytes. Content-Type: text/plain; charset=utf-8.
// NO artificial per-chunk delay — measures framework streaming OVERHEAD.
// MUST stay byte-identical across all 5 servers.
const STREAM_CHUNKS = 10;
function streamChunk(i) {
  return Buffer.from(`chunk-${i}: ` + "=".repeat(54) + "\n");
}

// Static-string baseline, like fastapi-turbo's Rust /_ping.
fastify.get("/_ping", (req, reply) => {
  reply.type("application/json").send('{"ping":"pong"}');
});

fastify.get("/hello", async () => ({ message: "hello" }));

fastify.get("/path/:id", async (req, reply) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id)) {
    reply.code(422);
    return { detail: "int required" };
  }
  return { id, squared: id * id };
});

fastify.get("/headers", async (req) => ({
  ua: req.headers["user-agent"] || "unknown",
  req_id: req.headers["x-request-id"] || null,
}));

// Simulated 2-level dependency chain (no native DI in fastify).
function getDB() {
  return { connected: true };
}
function getUser(db, authorization) {
  return { name: "alice", db: db.connected };
}

fastify.get("/with-deps", async (req) => {
  const db = getDB();
  const user = getUser(db, req.headers["authorization"] || "tok-demo");
  return { user: user.name, db: db.connected };
});

fastify.get("/list", async () => ({
  items: Array.from({ length: 20 }, (_, i) => ({ id: i, name: `item-${i}` })),
}));

fastify.post(
  "/items",
  {
    schema: {
      body: {
        type: "object",
        required: ["sku", "qty"],
        properties: {
          sku: { type: "string" },
          qty: { type: "integer" },
          tags: { type: "array", items: { type: "string" }, default: [] },
        },
      },
    },
  },
  async (req) => {
    const { sku, qty, tags = [] } = req.body;
    return { ok: true, sku, qty, tag_count: tags.length };
  }
);

// 10-chunk stream via an async generator → Readable. No delay.
fastify.get("/stream", (req, reply) => {
  async function* gen() {
    for (let i = 0; i < STREAM_CHUNKS; i++) {
      yield streamChunk(i);
    }
  }
  reply.type("text/plain; charset=utf-8");
  return reply.send(Readable.from(gen()));
});

const port = Number(process.env.PORT || 8004);
fastify.listen({ host: "127.0.0.1", port }).then(() => {
  console.log(`Fastify bench app on http://127.0.0.1:${port}`);
});
