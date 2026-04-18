// Database benchmark API -- Node.js Fastify implementation.
//
// Tests real PostgreSQL (pg) and Redis (ioredis) performance.
// Same endpoints as Go Gin version for fair comparison.
//
// Run: node server.js

'use strict';

const Fastify = require('fastify');
const { Pool } = require('pg');
const Redis = require('ioredis');

const PORT = parseInt(process.env.PORT || '19034', 10);

// ── Database setup ─────────────────────────────────────────────────────

const pgPool = new Pool({
  host: 'localhost',
  port: 5432,
  database: 'fastapi_rs_bench',
  user: 'venky',
  min: 5,
  max: 20,
});

const redisClient = new Redis({
  host: '127.0.0.1',
  port: 6379,
});

// ── App setup ──────────────────────────────────────────────────────────

const app = Fastify({
  logger: false,
});

// ── Routes ─────────────────────────────────────────────────────────────

app.get('/health', async () => {
  return { status: 'ok' };
});

// Simple query: SELECT single row with JOIN
app.get('/products/:id', async (request, reply) => {
  const { id } = request.params;
  const { rows } = await pgPool.query(
    `SELECT p.id, p.name, p.price, p.stock, c.name as category_name
     FROM products p JOIN categories c ON p.category_id = c.id
     WHERE p.id = $1`,
    [id]
  );
  if (rows.length === 0) {
    reply.code(404);
    return { detail: 'Product not found' };
  }
  const row = rows[0];
  return {
    id: row.id,
    name: row.name,
    price: parseFloat(row.price),
    stock: row.stock,
    category_name: row.category_name,
  };
});

// List with pagination
app.get('/products', async (request) => {
  const limit = parseInt(request.query.limit || '10', 10);
  const offset = parseInt(request.query.offset || '0', 10);
  const { rows } = await pgPool.query(
    `SELECT p.id, p.name, p.price, p.stock, c.name as category_name
     FROM products p JOIN categories c ON p.category_id = c.id
     ORDER BY p.id LIMIT $1 OFFSET $2`,
    [limit, offset]
  );
  return rows.map(row => ({
    id: row.id,
    name: row.name,
    price: parseFloat(row.price),
    stock: row.stock,
    category_name: row.category_name,
  }));
});

// Insert with RETURNING
app.post('/products', async (request, reply) => {
  const { name, description = '', price, category_id, stock = 0 } = request.body;
  const { rows } = await pgPool.query(
    `INSERT INTO products (name, description, price, category_id, stock)
     VALUES ($1, $2, $3, $4, $5) RETURNING id, name, price, stock`,
    [name, description, price, category_id, stock]
  );
  const row = rows[0];
  reply.code(201);
  return {
    id: row.id,
    name: row.name,
    price: parseFloat(row.price),
    stock: row.stock,
    category_name: '',
  };
});

// Full update (PUT)
app.put('/products/:id', async (request, reply) => {
  const { id } = request.params;
  const { name, description = '', price, category_id, stock = 0 } = request.body;
  const { rows } = await pgPool.query(
    `UPDATE products SET name=$1, description=$2, price=$3, category_id=$4, stock=$5
     WHERE id=$6 RETURNING id, name, price, stock`,
    [name, description, price, category_id, stock, id]
  );
  if (rows.length === 0) {
    reply.code(404);
    return { detail: 'Product not found' };
  }
  const row = rows[0];
  return { id: row.id, name: row.name, price: parseFloat(row.price), stock: row.stock };
});

// Partial update (PATCH)
app.patch('/products/:id', async (request, reply) => {
  const { id } = request.params;
  const body = request.body;
  const fields = ['name', 'description', 'price', 'category_id', 'stock'];
  const setClauses = [];
  const values = [];
  let idx = 1;
  for (const key of fields) {
    if (body[key] !== undefined) {
      setClauses.push(`${key}=$${idx}`);
      values.push(body[key]);
      idx++;
    }
  }
  if (setClauses.length === 0) {
    reply.code(400);
    return { detail: 'No fields to update' };
  }
  values.push(id);
  const query = `UPDATE products SET ${setClauses.join(', ')} WHERE id=$${idx} RETURNING id, name, price, stock`;
  const { rows } = await pgPool.query(query, values);
  if (rows.length === 0) {
    reply.code(404);
    return { detail: 'Product not found' };
  }
  const row = rows[0];
  return { id: row.id, name: row.name, price: parseFloat(row.price), stock: row.stock };
});

// Delete product
app.delete('/products/:id', async (request, reply) => {
  const { id } = request.params;
  const { rowCount } = await pgPool.query('DELETE FROM products WHERE id=$1', [id]);
  if (rowCount === 0) {
    reply.code(404);
    return { detail: 'Product not found' };
  }
  return { deleted: true, id: parseInt(id) };
});

// Complex JOIN + GROUP BY aggregation
app.get('/categories/stats', async () => {
  const { rows } = await pgPool.query(
    `SELECT c.id, c.name, COUNT(p.id) as product_count,
     COALESCE(AVG(p.price), 0) as avg_price,
     COALESCE(SUM(p.stock), 0) as total_stock
     FROM categories c LEFT JOIN products p ON c.id = p.category_id
     GROUP BY c.id, c.name ORDER BY c.name`
  );
  return rows.map(row => ({
    id: row.id,
    name: row.name,
    product_count: parseInt(row.product_count, 10),
    avg_price: parseFloat(row.avg_price),
    total_stock: parseInt(row.total_stock, 10),
  }));
});

// Redis read-through cache
app.get('/cached/products/:id', async (request, reply) => {
  const { id } = request.params;
  const cacheKey = `product:${id}`;

  // Check Redis cache
  const cached = await redisClient.get(cacheKey);
  if (cached) {
    return JSON.parse(cached);
  }

  // Cache miss -- query DB
  const { rows } = await pgPool.query(
    `SELECT p.id, p.name, p.price, p.stock, c.name as category_name
     FROM products p JOIN categories c ON p.category_id = c.id
     WHERE p.id = $1`,
    [id]
  );
  if (rows.length === 0) {
    reply.code(404);
    return { detail: 'Product not found' };
  }
  const row = rows[0];
  const product = {
    id: row.id,
    name: row.name,
    price: parseFloat(row.price),
    stock: row.stock,
    category_name: row.category_name,
  };

  // Store in Redis with 60s TTL
  await redisClient.setex(cacheKey, 60, JSON.stringify(product));
  return product;
});

// Order with multi-table JOIN
app.get('/orders/:id', async (request, reply) => {
  const { id } = request.params;

  // Fetch order
  const orderResult = await pgPool.query(
    'SELECT id, user_id, total, status, created_at FROM orders WHERE id = $1',
    [id]
  );
  if (orderResult.rows.length === 0) {
    reply.code(404);
    return { detail: 'Order not found' };
  }
  const orderRow = orderResult.rows[0];
  const order = {
    id: orderRow.id,
    user_id: orderRow.user_id,
    total: parseFloat(orderRow.total),
    status: orderRow.status,
    created_at: orderRow.created_at,
  };

  // Fetch order items
  const itemsResult = await pgPool.query(
    `SELECT oi.id, oi.order_id, oi.product_id, oi.quantity, oi.unit_price,
     p.name as product_name
     FROM order_items oi
     JOIN products p ON oi.product_id = p.id
     WHERE oi.order_id = $1`,
    [id]
  );
  const items = itemsResult.rows.map(row => ({
    id: row.id,
    order_id: row.order_id,
    product_id: row.product_id,
    quantity: row.quantity,
    unit_price: parseFloat(row.unit_price),
    product_name: row.product_name,
  }));

  return { order, items };
});

// ── Start server ───────────────────────────────────────────────────────

async function start() {
  try {
    // Verify PG connection
    const client = await pgPool.connect();
    await client.query('SELECT 1');
    client.release();

    // Verify Redis connection (ioredis auto-connects)
    await redisClient.ping();

    await app.listen({ port: PORT, host: '127.0.0.1' });
    console.log(`Fastify DB server listening on :${PORT}`);
  } catch (err) {
    console.error('Failed to start:', err);
    process.exit(1);
  }
}

start();
