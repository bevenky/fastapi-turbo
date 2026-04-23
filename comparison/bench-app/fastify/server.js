// Mini E-commerce API -- Node.js Fastify implementation.
//
// Exercises: CRUD, query params, path params, JSON body, auth,
// CORS, form data login, WebSocket echo with timestamps.

'use strict';

const fastify = require('fastify')({
    logger: false,
    keepAliveTimeout: 60000,
    connectionTimeout: 0,
});
fastify.server.maxRequestsPerSocket = 0;  // unlimited keep-alive requests
fastify.server.keepAliveTimeout = 60000;

// ---------------------------------------------------------------------------
// CORS
// ---------------------------------------------------------------------------
fastify.register(require('@fastify/cors'), {
    origin: true,
    methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    allowedHeaders: ['*'],
    credentials: true,
});

// ---------------------------------------------------------------------------
// Form body support
// ---------------------------------------------------------------------------
fastify.register(require('@fastify/formbody'));

// ---------------------------------------------------------------------------
// WebSocket support
// ---------------------------------------------------------------------------
fastify.register(require('@fastify/websocket'));

// ---------------------------------------------------------------------------
// In-memory database (pre-seeded)
// ---------------------------------------------------------------------------
const db = new Map([
    [1, { id: 1, name: 'Widget',    price: 9.99,  description: null }],
    [2, { id: 2, name: 'Gadget',    price: 19.99, description: null }],
    [3, { id: 3, name: 'Doohickey', price: 29.99, description: null }],
]);
let nextId = 4;

const SECRET_TOKEN = 'secret-token-123';

// ---------------------------------------------------------------------------
// Dependency simulation helpers
// ---------------------------------------------------------------------------
function getDB() { return db; }

function verifyToken(request) {
    const auth = request.headers.authorization || '';
    if (!auth.startsWith('Bearer ')) {
        return { error: { statusCode: 401, detail: 'Missing or invalid token' } };
    }
    const token = auth.slice(7);
    if (token !== SECRET_TOKEN) {
        return { error: { statusCode: 401, detail: 'Invalid token' } };
    }
    return { token };
}

function getCurrentUser(request) {
    const tokenResult = verifyToken(request);
    if (tokenResult.error) return tokenResult;
    getDB(); // simulate db dep
    return { user: { username: 'demo_user', email: 'demo@example.com' } };
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

// Health
fastify.get('/health', async () => ({ status: 'ok' }));

// List items
fastify.get('/items', async (request) => {
    const limit = parseInt(request.query.limit, 10) || 10;
    const offset = parseInt(request.query.offset, 10) || 0;
    const items = Array.from(db.values())
        .sort((a, b) => a.id - b.id);
    return items.slice(offset, offset + limit);
});

// Get item
fastify.get('/items/:id', async (request, reply) => {
    const id = parseInt(request.params.id, 10);
    if (!db.has(id)) {
        reply.code(404);
        return { detail: 'Item not found' };
    }
    return db.get(id);
});

// Create item
fastify.post('/items', async (request, reply) => {
    const { name, price, description } = request.body || {};
    if (!name || price === undefined) {
        reply.code(422);
        return { detail: 'name and price are required' };
    }
    const item = { id: nextId++, name, price, description: description || null };
    db.set(item.id, item);
    reply.code(201);
    return item;
});

// Update item
const updateHandler = async (request, reply) => {
    const id = parseInt(request.params.id, 10);
    if (!db.has(id)) {
        reply.code(404);
        return { detail: 'Item not found' };
    }
    const { name, price, description } = request.body || {};
    const item = { id, name, price, description: description || null };
    db.set(id, item);
    return item;
};
fastify.put('/items/:id', updateHandler);
fastify.patch('/items/:id', updateHandler);

// Delete item — return 200 with empty body so keep-alive bench clients don't
// trip on Fastify's 204-no-content closing the connection.
fastify.delete('/items/:id', async (request, reply) => {
    const id = parseInt(request.params.id, 10);
    db.delete(id);
    reply.code(200);
    return {};
});

// Get current user (auth required)
fastify.get('/users/me', async (request, reply) => {
    const result = getCurrentUser(request);
    if (result.error) {
        reply.code(result.error.statusCode);
        return { detail: result.error.detail };
    }
    return result.user;
});

// Login (form data)
fastify.post('/login', async () => ({
    access_token: SECRET_TOKEN,
    token_type: 'bearer',
}));

// WebSocket chat
fastify.register(async function (fastify) {
    fastify.get('/ws/chat', { websocket: true }, (socket, req) => {
        socket.on('message', (raw) => {
            try {
                const msg = JSON.parse(raw.toString());
                msg.server_ts = Date.now() / 1000;
                socket.send(JSON.stringify(msg));
            } catch (e) {
                // ignore malformed JSON
            }
        });
    });
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
const port = parseInt(process.env.PORT, 10) || 19004;
fastify.listen({ port, host: '127.0.0.1' }).then(() => {
    console.log(`Fastify e-commerce running on http://127.0.0.1:${port}`);
});
