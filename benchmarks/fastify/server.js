// Fastify bench app — mirrors benchmarks/_bench_app.py endpoint-for-endpoint
// (plus /_ping, matching fastapi-turbo's built-in pure-Rust baseline) so the
// comparison matrix is apples-to-apples.
//
// Run:  PORT=8004 node server.js     (npm i fastify here first)
const fastify = require("fastify")({ logger: false });

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

const port = Number(process.env.PORT || 8004);
fastify.listen({ host: "127.0.0.1", port }).then(() => {
  console.log(`Fastify bench app on http://127.0.0.1:${port}`);
});
